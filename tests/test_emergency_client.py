import asyncio
from dataclasses import replace
from decimal import Decimal
from time import monotonic, time

import pytest
import requests
from unittest.mock import AsyncMock, Mock, patch

from predict_mm.client import (
    PredictClient, PredictInsufficientSharesError, PredictOrderSubmissionUnknown,
    PredictRateLimitError,
    PredictSubmissionAborted,
)
from predict_mm.config import Settings
from predict_mm.models import ExitContext, ManagedOrder, OrderStatus, Quote, Side


def context():
    return ExitContext("group", "source-buy", Decimal("3"))


class SignedClient(PredictClient):
    async def _complete_quote_with_market_metadata(self, quote):
        return quote

    def _build_signed_limit_order_payload(self, quote, post_only=True):
        return {"data": {"order": {"hash": "order-hash", "expiration": str(int(time()) + 300)}}}


def test_timeout_retains_exit_intent_before_post(tmp_path):
    client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                   order_journal_path=str(tmp_path / "orders.json")), False)

    async def request(*args):
        restored = client.load_tracked_orders()
        assert len(restored) == 1
        assert restored[0].exit_context == context()
        assert restored[0].order_hash == "order-hash"
        assert restored[0].status == OrderStatus.UNKNOWN
        raise requests.Timeout("response lost")

    client._request = request
    with pytest.raises(PredictOrderSubmissionUnknown) as error:
        asyncio.run(client.create_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                                        post_only=False, exit_context=context()))
    assert error.value.order.order_hash == "order-hash"
    assert len(client.load_tracked_orders()) == 1


def test_success_replaces_provisional_journal_id(tmp_path):
    client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                   order_journal_path=str(tmp_path / "orders.json")), False)

    async def request(*args):
        return {"data": {"id": "123"}}

    client._request = request
    asyncio.run(client.create_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                                    post_only=False, exit_context=context()))
    orders = client.load_tracked_orders()
    assert len(orders) == 1
    assert orders[0].order_id == "123"
    assert orders[0].order_hash == "order-hash"
    assert orders[0].exit_context == context()
    assert orders[0].is_emergency_exit


def test_buy_cleanup_filters_market_and_side_and_batches(tmp_path):
    client = PredictClient(Settings(api_key="key", jwt_token="jwt"), False)
    rows = [{"id": str(i), "marketId": 1, "order": {"side": 0}} for i in range(105)]
    rows += [{"id": "exit-sell", "marketId": 1, "order": {"side": 1}},
             {"id": "other-market", "marketId": 2, "order": {"side": 0}}]
    removed = []

    async def read(query):
        return rows

    async def request(method, path, payload):
        assert method == "POST" and path == "/v1/orders/remove"
        assert len(payload["data"]["ids"]) <= 100
        removed.extend(payload["data"]["ids"])

    client._all_order_rows = read
    client._request = request
    asyncio.run(client.cancel_market_buy_orders("1"))
    assert removed == [str(i) for i in range(105)]


def test_journal_pruning_never_resurrects_superseded_exit(tmp_path):
    client = PredictClient(Settings(order_journal_path=str(tmp_path / "orders.json")), False)
    quote = Quote("1", Side.SELL, Decimal("0.001"), Decimal("3"))
    first = ManagedOrder("first", quote, monotonic(), OrderStatus.EXPIRED,
                         "first-hash", Decimal("1"), True, context())
    client.persist_tracked_order(first)
    second = ManagedOrder("second", replace(quote, size=Decimal("2")), monotonic(),
                          OrderStatus.FILLED, "second-hash", Decimal("2"), True,
                          replace(context(), sold_before=Decimal("1")))
    client.persist_tracked_order(second)
    assert [o.order_id for o in client.load_tracked_orders()] == ["second"]
    # A large stream of normal orders may age out the completed exit, but cannot
    # bring back the partially filled first attempt as an outstanding liability.
    for i in range(502):
        client.persist_tracked_order(ManagedOrder(str(i), quote, monotonic()))
    assert not any(o.exit_context for o in client.load_tracked_orders())


def test_unknown_success_response_does_not_create_order_named_none(tmp_path):
    client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                   order_journal_path=str(tmp_path / "orders.json")), False)

    async def request(*args):
        return {"data": {}}

    client._request = request
    with pytest.raises(PredictOrderSubmissionUnknown):
        asyncio.run(client.create_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                                        post_only=False, exit_context=context()))
    assert client.load_tracked_orders()[0].order_id == "order-hash"


def test_prepare_only_signs_and_does_not_post_or_persist(tmp_path):
    client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                   order_journal_path=str(tmp_path / "orders.json")), False)
    client._request = AsyncMock()
    prepared = asyncio.run(client.prepare_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3"))))
    assert prepared.quote.size == 3
    assert not prepared.used
    client._request.assert_not_called()
    assert not (tmp_path / "orders.json").exists()


def test_prepared_order_reused_only_after_explicit_insufficient_shares(tmp_path):
    async def run():
        client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                       order_journal_path=str(tmp_path / "orders.json")), False)
        client._pace_emergency_submission = AsyncMock()
        client._request = AsyncMock(side_effect=[
            PredictInsufficientSharesError("HTTP 400: Insufficient shares"), {"data": {"id": "123"}},
        ])
        prepared = await client.prepare_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")))
        with pytest.raises(PredictInsufficientSharesError):
            await client.submit_prepared_order(prepared, exit_context=context())
        assert not prepared.used
        assert client.load_tracked_orders()[0].status == OrderStatus.REJECTED
        await client.submit_prepared_order(prepared, exit_context=context())
        assert prepared.used
        with pytest.raises(RuntimeError, match="already submitted"):
            await client.submit_prepared_order(prepared, exit_context=context())
        assert client._request.await_count == 2
        assert client._request.call_args_list[0].args[2] == client._request.call_args_list[1].args[2]
    asyncio.run(run())


@pytest.mark.parametrize("failure", [requests.Timeout("response lost"), requests.ConnectionError("lost")])
def test_post_transport_failure_is_never_automatically_retried(tmp_path, failure):
    async def run():
        client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                       order_journal_path=str(tmp_path / "orders.json")), False)
        prepared = await client.prepare_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")))
        with patch.object(client, "_request_sync", side_effect=failure) as request:
            with pytest.raises(PredictOrderSubmissionUnknown):
                await client.submit_prepared_order(prepared, exit_context=context())
            with pytest.raises(RuntimeError, match="already submitted"):
                await client.submit_prepared_order(prepared, exit_context=context())
        assert request.call_count == 1
        assert prepared.used
        assert client.load_tracked_orders()[0].status == OrderStatus.UNKNOWN
    asyncio.run(run())


def test_malformed_success_response_is_unknown_not_safe_to_retry(tmp_path):
    async def run():
        client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                       order_journal_path=str(tmp_path / "orders.json")), False)
        response = Mock(status_code=201, content=b"Internal error")
        response.json.side_effect = ValueError("invalid JSON")
        with patch("predict_mm.client.requests.request", return_value=response) as request:
            with pytest.raises(PredictOrderSubmissionUnknown):
                await client.create_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                                          post_only=False, exit_context=context())
        assert request.call_count == 1
        assert client.load_tracked_orders()[0].status == OrderStatus.UNKNOWN
    asyncio.run(run())


def test_http_429_pauses_extra_probes_and_marks_order_rejected(tmp_path):
    async def run():
        client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                       order_journal_path=str(tmp_path / "orders.json")), False)
        response = Mock(status_code=429, content=b"limited", headers={"Retry-After": "7"})
        response.json.return_value = {"error": "rate limited"}
        with patch("predict_mm.client.requests.request", return_value=response) as request:
            with pytest.raises(PredictRateLimitError) as error:
                await client.create_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                                          post_only=False, exit_context=context())
        assert error.value.retry_after == 7
        assert request.call_count == 1
        assert not client.allow_early_fill_probe()
        assert client.load_tracked_orders()[0].status == OrderStatus.REJECTED
    asyncio.run(run())


def test_extra_probe_budget_and_cooldown_are_shared_across_markets():
    client = PredictClient(Settings(), False)
    with patch("predict_mm.client.monotonic", return_value=100.0):
        assert client.allow_early_fill_probe()
        assert not client.allow_early_fill_probe()
    with patch("predict_mm.client.monotonic", return_value=101.0):
        assert client.allow_early_fill_probe()
    client._request_times.extend([101.0] * 180)
    with patch("predict_mm.client.monotonic", return_value=102.0):
        assert not client.allow_early_fill_probe()
    with patch("predict_mm.client.monotonic", return_value=162.0):
        assert client.allow_early_fill_probe()


def test_expired_unused_signature_is_refreshed_before_first_post(tmp_path):
    async def run():
        client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                       order_journal_path=str(tmp_path / "orders.json")), False)
        quote = Quote("1", Side.SELL, Decimal("0.001"), Decimal("3"))
        prepared = await client.prepare_order(quote)
        prepared.payload["data"]["order"]["expiration"] = str(int(time()) + 1)
        client._request = AsyncMock(return_value={"data": {"id": "123"}})
        await client.submit_prepared_order(prepared, exit_context=context())
        sent = client._request.call_args.args[2]["data"]["order"]
        assert float(sent["expiration"]) > time() + 200
        assert client._request.await_count == 1
    asyncio.run(run())


def test_failed_source_during_rate_wait_never_posts(tmp_path):
    async def run():
        client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                       order_journal_path=str(tmp_path / "orders.json")), False)
        active = True
        async def pace():
            nonlocal active
            active = False
        client._pace_emergency_submission = pace
        client._request = AsyncMock()
        with pytest.raises(PredictSubmissionAborted):
            await client.create_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                                      post_only=False, exit_context=context(),
                                      should_submit=lambda: active)
        client._request.assert_not_called()
        assert not client.load_tracked_orders()
    asyncio.run(run())


def test_failed_source_during_expiry_resign_never_posts(tmp_path):
    async def run():
        client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                       order_journal_path=str(tmp_path / "orders.json")), False)
        prepared = await client.prepare_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")))
        prepared.payload["data"]["order"]["expiration"] = str(int(time()) + 1)
        active = True
        original = client.prepare_order
        async def resign(*args, **kwargs):
            nonlocal active
            active = False
            return await original(*args, **kwargs)
        client.prepare_order = resign
        client._request = AsyncMock()
        with pytest.raises(PredictSubmissionAborted):
            await client.submit_prepared_order(prepared, should_submit=lambda: active)
        client._request.assert_not_called()
    asyncio.run(run())


def test_pending_source_plan_and_latest_sell_survive_pruning(tmp_path):
    client = PredictClient(Settings(order_journal_path=str(tmp_path / "orders.json")), False)
    plan = replace(context(), source_settlement_key="source-buy:s1")
    source = ManagedOrder("source-buy", Quote("1", Side.BUY, Decimal("0.6"), Decimal("3")), 0,
                          matched_settlements={"source-buy:s1": Decimal("3")},
                          exit_plans=[plan], exit_baseline_size=Decimal("0"))
    client.persist_tracked_order(source)
    sell = ManagedOrder("sell", replace(source.quote, side=Side.SELL), 0, OrderStatus.FILLED,
                        filled_size=Decimal("3"), is_emergency_exit=True, exit_context=plan)
    client.persist_tracked_order(sell)
    for i in range(502):
        client.persist_tracked_order(ManagedOrder(str(i), source.quote, 0))
    restored = {o.order_id: o for o in client.load_tracked_orders()}
    assert restored["source-buy"].exit_plans == [plan]
    assert restored["sell"].filled_size == 3
    assert restored["source-buy"].matched_settlements == source.matched_settlements


def test_same_prepared_signature_cannot_be_posted_concurrently(tmp_path):
    async def run():
        client = SignedClient(Settings(api_key="key", jwt_token="jwt",
                                       order_journal_path=str(tmp_path / "orders.json")), False)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_post(*_):
            started.set()
            await release.wait()
            return {"data": {"id": "123"}}

        client._request = AsyncMock(side_effect=slow_post)
        prepared = await client.prepare_order(Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")))
        first = asyncio.create_task(client.submit_prepared_order(prepared, exit_context=context()))
        await started.wait()
        with pytest.raises(RuntimeError, match="already submitted"):
            await client.submit_prepared_order(prepared, exit_context=context())
        release.set()
        await first
        assert client._request.await_count == 1
    asyncio.run(run())
