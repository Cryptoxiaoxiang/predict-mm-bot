import asyncio
from dataclasses import replace
from decimal import Decimal
from time import monotonic, time
from unittest.mock import AsyncMock

import pytest

from predict_mm.client import (
    PredictClient, PreparedOrder, PredictInsufficientSharesError,
    PredictOrderSubmissionUnknown, PredictSubmissionAborted,
)
from predict_mm.config import BotConfig, MarketConfig, RiskConfig, Settings, StrategyConfig
from predict_mm.engine import MarketMakerEngine
from predict_mm.models import ManagedOrder, OrderBook, OrderStatus, Quote, Side, WalletFillEvent
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy


class EarlyClient:
    def __init__(self):
        self.prepared = asyncio.Event()
        self.submitted = asyncio.Event()
        self.created = []
        self.signatures_used = []
        self.get_positions = AsyncMock(return_value={"1": Decimal("999")})

    async def cancel_market_buy_orders(self, _):
        pass

    async def get_orderbook(self, market_id):
        return OrderBook(market_id, [], [], Decimal("0.001"))

    async def prepare_order(self, quote, *, post_only=False):
        self.prepared.set()
        return PreparedOrder(quote, post_only, {"data": {"order": {
            "hash": "signed", "expiration": str(int(time()) + 300)}}})

    async def submit_prepared_order(self, prepared, *, exit_context=None, should_submit=None):
        if should_submit is not None and not should_submit():
            raise PredictSubmissionAborted("source failed")
        assert not prepared.used
        prepared.used = True
        self.signatures_used.append(prepared)
        return await self.create_order(prepared.quote, post_only=False, exit_context=exit_context)

    async def create_order(self, quote, *, post_only=True, exit_context=None, should_submit=None):
        self.created.append(quote)
        self.submitted.set()
        return ManagedOrder(str(len(self.created)), quote, monotonic(), OrderStatus.FILLED,
                            "sell-" + str(len(self.created)), quote.size, True, exit_context)


def setup(client):
    engine = MarketMakerEngine(
        BotConfig(markets=[MarketConfig(id="1")], cancel_all_on_shutdown=False), client,
        PassiveMakerStrategy(StrategyConfig()), RiskManager(RiskConfig()),
    )
    engine._exit_poll_seconds = 0.001
    engine._emergency_retry_base_seconds = 0.001
    engine._market_tick_sizes["1"] = Decimal("0.001")
    source = ManagedOrder("buy", Quote("1", Side.BUY, Decimal("0.6"), Decimal("100")),
                          0, order_hash="buy-hash")
    engine._register_order(source)
    return engine, source


def event(kind="orderTransactionSubmitted", size="100", settlement="s1"):
    return WalletFillEvent("buy", Decimal(size), "buy-hash", settlement, kind)


async def drain(engine):
    await asyncio.wait_for(asyncio.gather(*engine._emergency_tasks), 1)
    await asyncio.gather(*engine._emergency_cancel_tasks.values())


@pytest.mark.parametrize("outcome", ["Yes", "No"])
def test_match_posts_without_waiting_for_any_confirmation(outcome):
    async def run():
        client = EarlyClient()
        engine, source = setup(client)
        source.quote = replace(source.quote, outcome=outcome)
        await engine._handle_wallet_fill(event())
        await drain(engine)
        assert len(client.created) == 1
        assert client.created[0].outcome == outcome
        assert client.created[0].size == 100
        assert source.filled_size == 0
        assert len(client.signatures_used) == 1
        client.get_positions.assert_not_called()
    asyncio.run(run())


def test_duplicate_match_late_success_and_rest_never_resell():
    async def run():
        client = EarlyClient()
        engine, source = setup(client)
        await engine._handle_wallet_fill(event())
        await engine._handle_wallet_fill(event())
        await drain(engine)
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await engine._handle_wallet_fill(WalletFillEvent(
            "buy", Decimal("100"), "buy-hash", "rest:100", "REST order reconciliation",
            cumulative_filled_size=Decimal("100")))
        await drain(engine)
        assert source.filled_size == 100
        assert len(client.created) == 1
    asyncio.run(run())


def test_success_before_submitted_never_creates_second_sell():
    async def run():
        client = EarlyClient()
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await drain(engine)
        await engine._handle_wallet_fill(event())
        await drain(engine)
        assert len(client.created) == 1
    asyncio.run(run())


def test_multiple_partial_matches_are_sold_once_each():
    async def run():
        client = EarlyClient()
        engine, source = setup(client)
        await engine._handle_wallet_fill(event(size="40"))
        await engine._handle_wallet_fill(event(size="60", settlement="s2"))
        await drain(engine)
        await engine._handle_wallet_fill(event("orderTransactionSuccess", "60", "s2"))
        await engine._handle_wallet_fill(event("orderTransactionSuccess", "40"))
        await drain(engine)
        assert [q.size for q in client.created] == [Decimal("40"), Decimal("60")]
        assert source.filled_size == 100
    asyncio.run(run())


def test_failed_match_stops_only_its_unsent_exit_not_other_partial():
    async def run():
        client = EarlyClient()
        engine, source = setup(client)
        await engine._handle_wallet_fill(event(size="40"))
        await engine._handle_wallet_fill(event("orderTransactionFailed", "40"))
        await engine._handle_wallet_fill(event(size="60", settlement="s2"))
        await drain(engine)
        assert [q.size for q in client.created] == [Decimal("60")]
        assert source.filled_size == 0
    asyncio.run(run())


def test_failure_during_signing_prevents_post():
    async def run():
        client = EarlyClient()
        started, release = asyncio.Event(), asyncio.Event()
        original = client.prepare_order
        async def prepare(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)
        client.prepare_order = prepare
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await started.wait()
        await engine._handle_wallet_fill(event("orderTransactionFailed"))
        release.set()
        await drain(engine)
        assert not client.created
    asyncio.run(run())


def test_balance_rejection_reuses_signature_and_success_does_not_duplicate():
    async def run():
        client = EarlyClient()
        original = client.submit_prepared_order
        attempts = []
        async def submit(prepared, **kwargs):
            attempts.append(prepared)
            if len(attempts) == 1:
                raise PredictInsufficientSharesError("Insufficient shares")
            return await original(prepared, **kwargs)
        client.submit_prepared_order = submit
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await client.prepared.wait()
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await drain(engine)
        assert len(attempts) == 2 and attempts[0] is attempts[1]
        assert len(client.created) == 1
    asyncio.run(run())


def test_failure_during_balance_retry_stops_further_posts():
    async def run():
        client = EarlyClient()
        rejected = asyncio.Event()
        async def reject(*args, **kwargs):
            rejected.set()
            raise PredictInsufficientSharesError("Insufficient shares")
        client.submit_prepared_order = AsyncMock(side_effect=reject)
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await rejected.wait()
        # A failure notification need not repeat the original matched quantity.
        await engine._handle_wallet_fill(event("orderTransactionFailed", "0"))
        await drain(engine)
        assert client.submit_prepared_order.await_count == 1
    asyncio.run(run())


def test_failed_buy_with_unknown_post_still_monitors_original_sell():
    async def run():
        client = EarlyClient()
        sent, release = asyncio.Event(), asyncio.Event()
        async def unknown(prepared, **kwargs):
            client.created.append(prepared.quote)
            client.intent = ManagedOrder("hash", prepared.quote, 0, OrderStatus.UNKNOWN,
                                        "hash", is_emergency_exit=True,
                                        exit_context=kwargs["exit_context"])
            sent.set()
            await release.wait()
            raise PredictOrderSubmissionUnknown(client.intent)
        async def read(_):
            return replace(client.intent, status=OrderStatus.EXPIRED, filled_size=Decimal("40"))
        client.submit_prepared_order = unknown
        client.get_order_by_hash = read
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await sent.wait()
        await engine._handle_wallet_fill(event("orderTransactionFailed"))
        release.set()
        await drain(engine)
        assert len(client.created) == 1
    asyncio.run(run())


def journal_client(tmp_path):
    journal = PredictClient(Settings(order_journal_path=str(tmp_path / "orders.json")), False)
    client = EarlyClient()
    client.persist_tracked_order = journal.persist_tracked_order
    return journal, client


def restore(journal, client):
    engine, _ = setup(EarlyClient())
    engine.client = client
    engine.open_orders.clear()
    for order in journal.load_tracked_orders():
        engine._register_order(order)
    return engine


def test_restart_between_plan_and_post_resumes_once(tmp_path):
    async def run():
        journal, client = journal_client(tmp_path)
        first, _ = setup(client)
        first._start_exit_plan = lambda *args: None
        await first._handle_wallet_fill(event())
        await drain(first)
        second = restore(journal, client)
        second._resume_emergency_exits()
        second._resume_emergency_exits()
        await drain(second)
        await second._handle_wallet_fill(event("orderTransactionSuccess"))
        await drain(second)
        assert len(client.created) == 1
    asyncio.run(run())


def test_restart_after_completed_speculative_sell_never_replays(tmp_path):
    async def run():
        journal, client = journal_client(tmp_path)
        first, _ = setup(client)
        await first._handle_wallet_fill(event(size="40"))
        await drain(first)
        second = restore(journal, client)
        second.open_orders = {k: v for k, v in second.open_orders.items() if v.quote.side == Side.BUY}
        second._resume_emergency_exits()
        await second._handle_wallet_fill(event(size="40"))
        await second._handle_wallet_fill(event("orderTransactionSuccess", "40"))
        await drain(second)
        assert [q.size for q in client.created] == [Decimal("40")]
        await second._handle_wallet_fill(event(size="60", settlement="s2"))
        await drain(second)
        assert [q.size for q in client.created] == [Decimal("40"), Decimal("60")]
    asyncio.run(run())


def test_restart_unknown_post_reconciles_hash_before_any_new_post(tmp_path):
    async def run():
        journal, client = journal_client(tmp_path)
        first, source = setup(client)
        first._start_exit_plan = lambda *args: None
        await first._handle_wallet_fill(event())
        await drain(first)
        plan = source.exit_plans[0]
        intent = ManagedOrder("hash", replace(source.quote, side=Side.SELL), 0,
                              OrderStatus.UNKNOWN, "hash", is_emergency_exit=True, exit_context=plan)
        journal.persist_tracked_order(intent)
        client.get_order_by_hash = AsyncMock(return_value=replace(
            intent, status=OrderStatus.FILLED, filled_size=Decimal("100")))
        second = restore(journal, client)
        second._resume_emergency_exits()
        await drain(second)
        await second._handle_wallet_fill(event("orderTransactionSuccess"))
        await drain(second)
        assert not client.created
        client.get_order_by_hash.assert_awaited_once()
    asyncio.run(run())


def test_old_journal_fill_is_not_resold_on_upgrade():
    async def run():
        client = EarlyClient()
        engine, source = setup(client)
        source.filled_size = Decimal("40")
        await engine._handle_wallet_fill(event("orderTransactionSuccess", "40"))
        await engine._handle_wallet_fill(event(size="60", settlement="s2"))
        await drain(engine)
        assert [q.size for q in client.created] == [Decimal("60")]
    asyncio.run(run())


def test_restart_after_fill_persisted_before_plan_allocation(tmp_path):
    async def run():
        journal, client = journal_client(tmp_path)
        first, source = setup(client)
        source.exit_baseline_size = Decimal("0")
        source.filled_size = Decimal("40")
        source.wallet_filled_size = Decimal("40")
        source.wallet_settlement_ids.add("buy-hash:s1")
        journal.persist_tracked_order(source)
        second = restore(journal, client)
        second._resume_emergency_exits()
        await drain(second)
        await second._handle_wallet_fill(event("orderTransactionSuccess", "40"))
        await drain(second)
        assert [q.size for q in client.created] == [Decimal("40")]
    asyncio.run(run())
