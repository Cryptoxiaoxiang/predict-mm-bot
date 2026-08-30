import asyncio
from dataclasses import replace
from decimal import Decimal
from time import monotonic
from unittest.mock import AsyncMock

from predict_mm.client import PredictOrderSubmissionUnknown
from predict_mm.config import BotConfig, MarketConfig, RiskConfig, StrategyConfig
from predict_mm.engine import MarketMakerEngine
from predict_mm.models import (
    ExitContext, ManagedOrder, OrderBook, OrderStatus, Quote, Side,
    WalletFillEvent, WalletOrderStatusEvent,
)
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy


def make_engine(client):
    engine = MarketMakerEngine(
        BotConfig(markets=[MarketConfig(id="1")], cancel_all_on_start=False,
                  cancel_all_on_shutdown=False),
        client, PassiveMakerStrategy(StrategyConfig()), RiskManager(RiskConfig()),
    )
    engine._exit_poll_seconds = 0.001
    engine._emergency_retry_base_seconds = 0.001
    return engine


class Client:
    def __init__(self):
        self.created = []
        self.cancelled = []
        self.submitted = asyncio.Event()

    async def cancel_market_buy_orders(self, market_id):
        self.cancelled.append(market_id)

    async def get_orderbook(self, market_id):
        return OrderBook(market_id, [], [], Decimal("0.001"))

    async def create_order(self, quote, *, post_only=True, exit_context=None):
        self.created.append(quote)
        order = ManagedOrder(str(len(self.created)), quote, monotonic(),
                             OrderStatus.FILLED, f"hash-{len(self.created)}",
                             quote.size, True, exit_context)
        self.submitted.set()
        return order

    async def close(self):
        pass


def buy(engine):
    order = ManagedOrder("buy", Quote("1", Side.BUY, Decimal("0.6"), Decimal("3")),
                         monotonic(), order_hash="buy-hash")
    engine._register_order(order)
    return order


async def finish(engine):
    await asyncio.wait_for(asyncio.gather(*engine._emergency_tasks), 1)
    await asyncio.gather(*engine._emergency_cancel_tasks.values())


def test_wallet_worker_sells_while_quote_batch_is_blocked():
    async def exercise():
        release = asyncio.Event()
        in_tick = asyncio.Event()

        class StreamClient(Client):
            async def stream_wallet_fill_events(self):
                await in_tick.wait()
                yield WalletFillEvent("buy", Decimal("3"), order_hash="buy-hash")
                await release.wait()

            async def stream_orderbook_updates(self, _):
                await release.wait()
                if False:
                    yield None

        client = StreamClient()
        engine = make_engine(client)
        buy(engine)

        async def blocked_tick():
            in_tick.set()
            await release.wait()

        engine._tick = blocked_tick
        task = asyncio.create_task(engine.run())
        try:
            await asyncio.wait_for(client.submitted.wait(), 0.5)
            assert not release.is_set()
            assert client.created[0].side == Side.SELL
        finally:
            engine.request_stop()
            release.set()
            await asyncio.wait_for(task, 1)
        assert engine._wallet_processor_task.done()

    asyncio.run(exercise())


def test_slow_buy_cancellation_does_not_delay_settled_sell():
    async def exercise():
        release = asyncio.Event()
        client = Client()
        async def cancel(_):
            await release.wait()
        client.cancel_market_buy_orders = cancel
        engine = make_engine(client)
        buy(engine)
        await engine._handle_wallet_fill(WalletFillEvent("buy", Decimal("3")))
        await asyncio.wait_for(client.submitted.wait(), 0.5)
        assert not release.is_set()
        release.set()
        await finish(engine)

    asyncio.run(exercise())


def test_rest_and_wallet_confirmation_do_not_sell_same_fill_twice():
    async def exercise():
        client = Client()
        engine = make_engine(client)
        order = buy(engine)
        await engine._handle_wallet_fill(WalletFillEvent(
            "buy", Decimal("2"), event_type="REST order reconciliation",
            settlement_id="rest:2", cumulative_filled_size=Decimal("2")))
        await finish(engine)
        await engine._handle_wallet_fill(WalletFillEvent(
            "buy", Decimal("2"), order_hash="buy-hash", settlement_id="chain-a"))
        await finish(engine)
        assert order.filled_size == 2
        assert len(client.created) == 1
        await engine._handle_wallet_fill(WalletFillEvent(
            "buy", Decimal("1"), order_hash="buy-hash", settlement_id="chain-b"))
        await finish(engine)
        assert [q.size for q in client.created] == [Decimal("2"), Decimal("1")]

    asyncio.run(exercise())


def test_partial_expired_sell_retries_only_remainder():
    class PartialClient(Client):
        async def create_order(self, *args, **kwargs):
            order = await super().create_order(*args, **kwargs)
            if len(self.created) == 1:
                self.first = replace(order, status=OrderStatus.OPEN, filled_size=Decimal("0"))
                return self.first
            return order

        async def get_order_by_hash(self, _):
            return replace(self.first, status=OrderStatus.EXPIRED, filled_size=Decimal("1"))

    async def exercise():
        client = PartialClient()
        engine = make_engine(client)
        buy(engine)
        await engine._handle_wallet_fill(WalletFillEvent("buy", Decimal("3")))
        await finish(engine)
        assert [q.size for q in client.created] == [Decimal("3"), Decimal("2")]
        assert engine.open_orders["2"].exit_context.sold_before == 1

    asyncio.run(exercise())


def test_uncertain_post_is_reconciled_by_hash_without_second_submission():
    class TimeoutClient(Client):
        reads = 0

        async def create_order(self, quote, *, post_only=True, exit_context=None):
            self.created.append(quote)
            self.intent = ManagedOrder("hash", quote, monotonic(), OrderStatus.UNKNOWN,
                                       "hash", is_emergency_exit=True, exit_context=exit_context)
            raise PredictOrderSubmissionUnknown(self.intent)

        async def get_order_by_hash(self, order_hash):
            assert order_hash == "hash"
            self.reads += 1
            if self.reads < 3:
                raise RuntimeError("HTTP 404")
            return replace(self.intent, order_id="numeric-id", status=OrderStatus.FILLED,
                           filled_size=self.intent.quote.size)

    async def exercise():
        client = TimeoutClient()
        engine = make_engine(client)
        buy(engine)
        await engine._handle_wallet_fill(WalletFillEvent("buy", Decimal("3")))
        await finish(engine)
        assert len(client.created) == 1
        assert "hash" not in engine.open_orders
        assert engine.open_orders["numeric-id"].filled_size == 3

    asyncio.run(exercise())


def test_removed_sell_is_not_assumed_safe_to_replace():
    async def exercise():
        client = Client()
        engine = make_engine(client)
        order = ManagedOrder("sell", Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                             monotonic(), OrderStatus.CANCELED, "hash", is_emergency_exit=True)
        client.get_order_by_hash = AsyncMock(return_value=replace(order))
        task = asyncio.create_task(engine._monitor_emergency_sell(order))
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not client.created
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_sell_success_updates_fill_and_wakes_monitor():
    async def exercise():
        client = Client()
        engine = make_engine(client)
        order = ManagedOrder("sell", Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                             monotonic(), OrderStatus.OPEN, "hash", is_emergency_exit=True)
        engine._register_order(order)
        client.get_order_by_hash = AsyncMock(return_value=replace(order))
        task = asyncio.create_task(engine._monitor_emergency_sell(order))
        await engine._handle_wallet_fill(WalletFillEvent(
            "sell", Decimal("3"), "hash", "s1", "orderTransactionSubmitted"))
        await engine._handle_wallet_fill(WalletFillEvent(
            "sell", Decimal("3"), "hash", "s1", "orderTransactionSuccess"))
        assert await asyncio.wait_for(task, 0.5) == 3
        assert order.status == OrderStatus.FILLED
        assert not client.created

    asyncio.run(exercise())


def test_open_order_reconciliation_never_cancels_exit_for_missing_from_list():
    async def exercise():
        client = Client()
        client.get_open_order_ids = AsyncMock(return_value=set())
        client.cancel_order = AsyncMock()
        engine = make_engine(client)
        engine._register_order(ManagedOrder(
            "sell", Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
            0, OrderStatus.PENDING, "hash", is_emergency_exit=True))
        await engine._reconcile_order_statuses()
        client.cancel_order.assert_not_called()

    asyncio.run(exercise())


def test_rejection_before_post_response_recovers_exit_intent():
    client = Client()
    engine = make_engine(client)
    context = ExitContext("group", "buy", Decimal("3"))
    intent = ManagedOrder("hash", Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                          0, OrderStatus.UNKNOWN, "hash", is_emergency_exit=True, exit_context=context)
    client.load_tracked_orders = lambda: [intent]
    engine._handle_wallet_order_status(WalletOrderStatusEvent(
        "numeric", "orderNotAccepted", "hash", "noMarketMatch"))
    assert engine.open_orders["numeric"].is_emergency_exit
    assert engine.open_orders["numeric"].status == OrderStatus.REJECTED


def test_resume_monitors_existing_exit_without_creating_another_sell():
    async def exercise():
        client = Client()
        engine = make_engine(client)
        context = ExitContext("group", "buy", Decimal("3"))
        order = ManagedOrder("sell", Quote("1", Side.SELL, Decimal("0.001"), Decimal("3")),
                             0, OrderStatus.UNKNOWN, "hash", is_emergency_exit=True,
                             exit_context=context)
        engine._register_order(order)
        client.get_order_by_hash = AsyncMock(return_value=replace(
            order, status=OrderStatus.FILLED, filled_size=Decimal("3")))
        engine._resume_emergency_exits()
        await finish(engine)
        assert not client.created

    asyncio.run(exercise())
