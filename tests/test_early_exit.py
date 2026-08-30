import asyncio
from dataclasses import replace
from decimal import Decimal
from time import monotonic, time
from unittest.mock import AsyncMock

import pytest

from predict_mm.client import PredictClient, PreparedOrder
from predict_mm.config import BotConfig, MarketConfig, RiskConfig, Settings, StrategyConfig
from predict_mm.engine import MarketMakerEngine
from predict_mm.models import ManagedOrder, OrderBook, OrderStatus, Quote, Side, WalletFillEvent
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy


class EarlyClient:
    def __init__(self):
        self.prepared = asyncio.Event()
        self.submitted = asyncio.Event()
        self.confirm = asyncio.Event()
        self.created = []
        self.signatures_used = []
        self.cumulative = Decimal("0")
        self.reads = 0
        self.allow = True
        self.get_positions = AsyncMock(return_value={"1": Decimal("999")})

    async def cancel_market_buy_orders(self, _):
        pass

    async def get_orderbook(self, market_id):
        return OrderBook(market_id, [], [], Decimal("0.001"))

    async def prepare_order(self, quote, *, post_only=False):
        self.prepared.set()
        return PreparedOrder(quote, post_only, {"data": {"order": {
            "hash": "signed", "expiration": str(int(time()) + 300)}}})

    async def submit_prepared_order(self, prepared, *, exit_context=None):
        assert not prepared.used
        prepared.used = True
        self.signatures_used.append(prepared)
        return await self.create_order(prepared.quote, post_only=False, exit_context=exit_context)

    async def create_order(self, quote, *, post_only=True, exit_context=None):
        self.created.append(quote)
        self.submitted.set()
        return ManagedOrder(str(len(self.created)), quote, monotonic(), OrderStatus.FILLED,
                            "sell-" + str(len(self.created)), quote.size, True, exit_context)

    def allow_early_fill_probe(self):
        return self.allow

    async def get_order_by_hash(self, order_hash):
        assert order_hash == "buy-hash"
        self.reads += 1
        return ManagedOrder("buy", Quote("1", Side.BUY, Decimal("0.6"), Decimal("100")),
                            0, order_hash=order_hash, filled_size=self.cumulative)


def setup(client):
    engine = MarketMakerEngine(
        BotConfig(markets=[MarketConfig(id="1")], cancel_all_on_shutdown=False), client,
        PassiveMakerStrategy(StrategyConfig()), RiskManager(RiskConfig()),
    )
    engine._early_probe_interval_seconds = 0.001
    engine._early_probe_window_seconds = 0.05
    engine._market_tick_sizes["1"] = Decimal("0.001")
    source = ManagedOrder("buy", Quote("1", Side.BUY, Decimal("0.6"), Decimal("100")),
                          0, order_hash="buy-hash")
    engine._register_order(source)
    return engine, source


def event(kind="orderTransactionSubmitted", size="100", settlement="s1"):
    return WalletFillEvent("buy", Decimal(size), "buy-hash", settlement, kind)


async def drain(engine):
    await asyncio.wait_for(asyncio.gather(*engine._early_fill_tasks.values()), 1)
    await asyncio.wait_for(asyncio.gather(*engine._emergency_tasks), 1)
    await asyncio.gather(*engine._emergency_cancel_tasks.values())


def test_match_prepares_but_does_not_sell_existing_positions_without_source_fill():
    async def run():
        client = EarlyClient()
        engine, source = setup(client)
        await engine._handle_wallet_fill(event())
        await drain(engine)
        assert client.prepared.is_set()
        assert client.reads > 0
        assert not client.created
        assert source.filled_size == 0
        client.get_positions.assert_not_called()
    asyncio.run(run())


def test_source_rest_confirmation_sells_before_ws_success_and_reuses_signature():
    async def run():
        client = EarlyClient()
        engine, source = setup(client)
        await engine._handle_wallet_fill(event())
        await client.prepared.wait()
        client.cumulative = Decimal("100")
        await asyncio.wait_for(client.submitted.wait(), 0.5)
        await drain(engine)
        assert len(client.signatures_used) == 1
        assert source.filled_size == 100
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await drain(engine)
        assert len(client.created) == 1
    asyncio.run(run())


def test_ws_confirmation_wins_race_with_slow_rest_without_duplicate():
    async def run():
        client = EarlyClient()
        read_started, release = asyncio.Event(), asyncio.Event()
        original = client.get_order_by_hash

        async def slow_read(key):
            read_started.set()
            await release.wait()
            return await original(key)

        client.get_order_by_hash = slow_read
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await read_started.wait()
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await asyncio.wait_for(client.submitted.wait(), 0.5)
        client.cumulative = Decimal("100")
        release.set()
        await drain(engine)
        assert len(client.created) == 1
    asyncio.run(run())


def test_actual_partial_fill_never_uses_larger_prepared_quantity():
    async def run():
        client = EarlyClient()
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await client.prepared.wait()
        client.cumulative = Decimal("40")
        await asyncio.wait_for(client.submitted.wait(), 0.5)
        await engine._handle_wallet_fill(event("orderTransactionSuccess", "40"))
        await engine._handle_wallet_fill(event("orderTransactionSuccess", "60", "s2"))
        await drain(engine)
        assert [q.size for q in client.created] == [Decimal("40"), Decimal("60")]
        assert not client.signatures_used  # 100-share signature must be discarded
    asyncio.run(run())


def test_failed_settlement_stops_early_checks_and_never_posts():
    async def run():
        client = EarlyClient()
        engine, source = setup(client)
        await engine._handle_wallet_fill(event())
        await client.prepared.wait()
        await engine._handle_wallet_fill(event("orderTransactionFailed"))
        client.cumulative = Decimal("100")
        await asyncio.gather(*engine._early_fill_tasks.values(), return_exceptions=True)
        await asyncio.gather(*engine._emergency_cancel_tasks.values())
        assert source.filled_size == 0
        assert not client.created
        assert not engine._early_prepare_tasks
    asyncio.run(run())


def test_no_extra_request_budget_still_prepares_and_ws_exit_works():
    async def run():
        client = EarlyClient()
        client.allow = False
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await client.prepared.wait()
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await drain(engine)
        assert client.reads == 0
        assert len(client.created) == 1
        assert len(client.signatures_used) == 1
    asyncio.run(run())


def test_wrong_source_hash_is_not_accepted_as_fill():
    async def run():
        client = EarlyClient()
        original = client.get_order_by_hash

        async def wrong_read(key):
            return replace(await original(key), order_hash="someone-else", filled_size=Decimal("100"))

        client.get_order_by_hash = wrong_read
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await drain(engine)
        assert not client.created
    asyncio.run(run())


def test_slow_probe_is_bounded_and_does_not_block_ws_processing():
    async def run():
        client = EarlyClient()
        started = asyncio.Event()

        async def never_returns(_):
            started.set()
            await asyncio.Event().wait()

        client.get_order_by_hash = never_returns
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await started.wait()
        await asyncio.wait_for(drain(engine), 0.3)
        assert not client.created
        assert not engine._early_fill_tasks
        assert not engine._early_prepare_tasks
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await drain(engine)
        assert len(client.created) == 1
    asyncio.run(run())


def test_failed_signature_preparation_does_not_disable_confirmed_exit():
    async def run():
        client = EarlyClient()
        client.prepare_order = AsyncMock(side_effect=RuntimeError("metadata timeout"))
        engine, _ = setup(client)
        await engine._handle_wallet_fill(event())
        await asyncio.sleep(0)
        await engine._handle_wallet_fill(event("orderTransactionSuccess"))
        await drain(engine)
        assert len(client.created) == 1
    asyncio.run(run())


@pytest.mark.parametrize("ws_already_received", [False, True])
def test_delayed_partial_ws_after_restart_never_sells_early_rest_fill_twice(tmp_path, ws_already_received):
    async def run():
        journal = PredictClient(Settings(order_journal_path=str(tmp_path / "orders.json")), False)
        client = EarlyClient()
        client.persist_tracked_order = journal.persist_tracked_order
        engine, _ = setup(client)
        await engine._handle_wallet_fill(WalletFillEvent(
            "buy", Decimal("40"), "buy-hash", "early:40", "Early source-order reconciliation",
            cumulative_filled_size=Decimal("40")))
        await drain(engine)
        if ws_already_received:
            await engine._handle_wallet_fill(event("orderTransactionSuccess", "40"))
        # New engine with no in-memory dedup sets, and a real journal round-trip.
        restored = journal.load_tracked_orders()
        second, _ = setup(client)
        second.open_orders.clear()
        second._wallet_fill_totals.clear()
        for order in restored:
            second._register_order(order)
        await second._handle_wallet_fill(event("orderTransactionSuccess", "40"))
        await drain(second)
        assert [q.size for q in client.created] == [Decimal("40")]
        await second._handle_wallet_fill(event("orderTransactionSuccess", "60", "s2"))
        await drain(second)
        assert [q.size for q in client.created] == [Decimal("40"), Decimal("60")]
    asyncio.run(run())
