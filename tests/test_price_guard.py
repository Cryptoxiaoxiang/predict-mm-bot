import asyncio
from dataclasses import replace
from decimal import Decimal as D
from time import monotonic

from predict_mm.client import PredictRateLimitError
from predict_mm.config import BotConfig, RiskConfig, StrategyConfig
from predict_mm.engine import MarketMakerEngine
from predict_mm.models import Level, ManagedOrder, OrderBook, OrderStatus, Quote, Side
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy


class Client:
    wallet_stream_connected = True
    orderbook_stream_connected = True

    def __init__(self):
        self.calls = []

    async def cancel_order(self, oid):
        self.calls.append(oid)

    async def close(self):
        pass


def engine_for(client):
    return MarketMakerEngine(
        BotConfig(cancel_all_on_start=False, cancel_all_on_shutdown=False), client,
        PassiveMakerStrategy(StrategyConfig()), RiskManager(RiskConfig()),
    )


def add_order(engine, oid="buy", market="m"):
    order = ManagedOrder(oid, Quote(market, Side.BUY, D('.038'), D('100')),
                         monotonic(), status=OrderStatus.OPEN)
    engine.open_orders[oid] = order
    return order


def book(market="m", bid='.039', ts=100):
    return OrderBook(market, [Level(D(bid), D('100'))], [Level(D('.041'), D('100'))],
                     D('.001'), update_timestamp_ms=ts, source="websocket")


async def drain(engine):
    await asyncio.gather(*list(engine._guard_cancel_tasks.values()))


def test_websocket_cancels_while_quote_batch_is_blocked():
    async def exercise():
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class StreamClient(Client):
            async def stream_wallet_fill_events(self):
                await asyncio.Event().wait()
                yield None

            async def stream_orderbook_updates(self, _):
                await entered.wait()
                yield book()
                await asyncio.Event().wait()

            async def cancel_order(self, oid):
                await super().cancel_order(oid)
                cancelled.set()

        engine = engine_for(StreamClient())
        add_order(engine)

        async def blocked_tick():
            entered.set()
            await release.wait()

        engine._tick = blocked_tick
        task = asyncio.create_task(engine.run())
        try:
            await asyncio.wait_for(cancelled.wait(), .5)
            assert not release.is_set()
            assert engine.client.calls == ['buy']
            assert engine._fill_events.empty()
        finally:
            engine.request_stop()
            release.set()
            await asyncio.wait_for(task, 1)
        assert not engine._guard_cancel_tasks
        assert not engine._cancel_tasks

    asyncio.run(exercise())


def test_slow_cancellation_does_not_block_other_market_or_duplicate_order():
    async def exercise():
        release, other_started = asyncio.Event(), asyncio.Event()

        class SlowClient(Client):
            async def cancel_order(self, oid):
                self.calls.append(oid)
                if oid == 'slow':
                    await release.wait()
                else:
                    other_started.set()

        engine = engine_for(SlowClient())
        add_order(engine, 'slow', 'a')
        add_order(engine, 'fast', 'b')
        await engine._handle_orderbook_update(book('a'), wait_for_cancels=False)
        for _ in range(10):
            await engine._handle_orderbook_update(book('a'), wait_for_cancels=False)
        await engine._handle_orderbook_update(book('b'), wait_for_cancels=False)
        await asyncio.wait_for(other_started.wait(), .5)
        assert engine.client.calls.count('slow') == 1
        assert not release.is_set()
        release.set()
        await drain(engine)

    asyncio.run(exercise())


def test_cancel_concurrency_is_bounded_and_shared_with_lifetime_removals():
    async def exercise():
        release, saturated = asyncio.Event(), asyncio.Event()

        class SlowClient(Client):
            active = 0
            peak = 0

            async def cancel_order(self, oid):
                self.calls.append(oid)
                self.active += 1
                self.peak = max(self.peak, self.active)
                if self.active == 3:
                    saturated.set()
                await release.wait()
                self.active -= 1

        engine = engine_for(SlowClient())
        orders = [add_order(engine, str(i), str(i)) for i in range(8)]
        for order in orders:
            await engine._handle_orderbook_update(book(order.quote.market_id), wait_for_cancels=False)
        await asyncio.wait_for(saturated.wait(), .5)
        also = asyncio.create_task(engine._cancel_order_safely(orders[0]))
        await asyncio.sleep(.01)
        assert len(engine.client.calls) == 3
        release.set()
        await drain(engine)
        await also
        assert engine.client.peak == 3
        assert len(engine.client.calls) == len(set(engine.client.calls)) == 8

    asyncio.run(exercise())


def test_failed_guard_retries_without_another_book_and_returns_first_result():
    async def exercise():
        retried = asyncio.Event()

        class FailingClient(Client):
            async def cancel_order(self, oid):
                self.calls.append(oid)
                if len(self.calls) == 1:
                    raise RuntimeError('temporary error')
                retried.set()

        engine = engine_for(FailingClient())
        order = add_order(engine)
        await asyncio.wait_for(engine._handle_orderbook_update(book()), .2)
        assert order.status == OrderStatus.OPEN
        for _ in range(10):
            await engine._handle_orderbook_update(book(), wait_for_cancels=False)
        await asyncio.wait_for(retried.wait(), 1)
        await drain(engine)
        assert engine.client.calls == ['buy', 'buy']
        assert order.status == OrderStatus.CANCELED

    asyncio.run(exercise())


def test_429_cooldown_applies_to_removal_requests():
    async def exercise():
        class LimitedClient(Client):
            async def cancel_order(self, oid):
                self.calls.append(monotonic())
                if len(self.calls) == 1:
                    raise PredictRateLimitError('limited', retry_after=.65)

        engine = engine_for(LimitedClient())
        add_order(engine)
        await engine._handle_orderbook_update(book())
        await asyncio.wait_for(drain(engine), 1.5)
        assert len(engine.client.calls) == 2
        assert engine.client.calls[1] - engine.client.calls[0] >= .64

    asyncio.run(exercise())


def test_cancel_response_does_not_overwrite_fill_received_in_flight():
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowClient(Client):
            async def cancel_order(self, oid):
                entered.set()
                await release.wait()

        engine = engine_for(SlowClient())
        order = add_order(engine)
        await engine._handle_orderbook_update(book(), wait_for_cancels=False)
        await asyncio.wait_for(entered.wait(), .5)
        order.status = OrderStatus.FILLED
        order.filled_size = order.quote.size
        release.set()
        await drain(engine)
        assert order.status == OrderStatus.FILLED

    asyncio.run(exercise())


def test_old_book_does_not_replace_newer_cache_or_trigger_cancellation():
    async def exercise():
        engine = engine_for(Client())
        add_order(engine)
        await engine._handle_orderbook_update(book(bid='.040', ts=200))
        await engine._handle_orderbook_update(book(bid='.039', ts=100))
        assert not engine.client.calls
        assert engine._latest_orderbooks['m'].update_timestamp_ms == 200

    asyncio.run(exercise())


def test_price_guard_timing_and_bounded_prefill_history(caplog):
    async def exercise():
        engine = engine_for(Client())
        order = add_order(engine)
        for i in range(40):
            engine._cache_orderbook(replace(book(bid='.040', ts=i), received_at=i * 2))
        assert len(engine._orderbook_history['m']) == 32
        await engine._handle_orderbook_update(book(ts=100))
        engine._log_prefill_orderbooks(order)

    with caplog.at_level('INFO', logger='predict-mm'):
        asyncio.run(exercise())
    assert 'Price guard triggered: order=buy' in caplog.text
    assert 'receive_to_trigger_ms=' in caplog.text
    assert 'trigger_to_request_ms=' in caplog.text
    assert 'Cancel removal acknowledged:' in caplog.text
    assert 'trigger_to_ack_ms=' in caplog.text
    assert caplog.text.count('Pre-fill orderbook:') == 8


def test_stale_queued_quote_is_not_submitted():
    async def exercise():
        engine = engine_for(Client())
        engine._cache_orderbook(book())
        # Client intentionally has no create_order: an unsafe quote must never call it.
        await engine._submit_quotes([Quote('m', Side.BUY, D('.038'), D('100'))])
        assert not engine.open_orders

    asyncio.run(exercise())


def test_update_during_post_is_checked_after_registration():
    async def exercise():
        class PostingClient(Client):
            async def create_order(self, quote):
                await engine._handle_orderbook_update(book(ts=200), wait_for_cancels=False)
                return ManagedOrder('late-post', quote, monotonic(), status=OrderStatus.OPEN)

        engine = engine_for(PostingClient())
        engine._cache_orderbook(book(bid='.040'))
        await engine._submit_quotes([Quote('m', Side.BUY, D('.038'), D('100'))])
        await drain(engine)
        assert engine.client.calls == ['late-post']

    asyncio.run(exercise())


def test_stop_ends_guard_retry_without_another_request():
    async def exercise():
        class FailingClient(Client):
            async def cancel_order(self, oid):
                self.calls.append(oid)
                raise RuntimeError('temporary error')

        engine = engine_for(FailingClient())
        add_order(engine)
        await engine._handle_orderbook_update(book())
        engine.request_stop()
        await asyncio.wait_for(drain(engine), .2)
        assert engine.client.calls == ['buy']

    asyncio.run(exercise())
