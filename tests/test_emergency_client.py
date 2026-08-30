import asyncio
from dataclasses import replace
from decimal import Decimal
from time import monotonic, time

import pytest
import requests

from predict_mm.client import PredictClient, PredictOrderSubmissionUnknown
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
