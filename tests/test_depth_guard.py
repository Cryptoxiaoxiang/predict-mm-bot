import asyncio
from dataclasses import replace
from decimal import Decimal as D
from time import monotonic, perf_counter

import pytest

from predict_mm.config import BotConfig, DepthProtectionConfig, RiskConfig, StrategyConfig
from predict_mm.depth_guard import DepthGuard, depth_ahead, selection
from predict_mm.engine import MarketMakerEngine
from predict_mm.models import Level, ManagedOrder, OrderBook, OrderStatus, Quote, Side
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy


def quote(market="m", outcome="YES", price=".498", size="200"):
    return Quote(market, Side.BUY, D(price), D(size), outcome=outcome, outcome_side=outcome)


def book(depth="1500", at=0, market="m", bid=".500", ask=".502", source="websocket"):
    return OrderBook(market, [Level(D(bid), D(depth))], [Level(D(ask), D(depth))],
                     D(".001"), received_at=at, source=source,
                     update_timestamp_ms=int(at * 1000))


def order(q=None, oid="o", status=OrderStatus.OPEN):
    return ManagedOrder(oid, q or quote(), 0, status=status)


def test_depth_counts_strictly_better_prices_and_excludes_all_own_equivalent_liquidity():
    b = OrderBook("m", [Level(D(".51"), D(300)), Level(D(".50"), D(200)),
                         Level(D(".498"), D(99999))], [])
    partial = order(quote(price=".51", size="100"))
    partial.filled_size = D(20)
    synthetic = order(replace(quote(outcome="NO", price=".50", size="50"), side=Side.SELL))
    canceled = order(quote(price=".51", size="1000"), status=OrderStatus.CANCELED)
    assert depth_ahead(quote(), b, [partial, synthetic, canceled]) == D(370)
    # Subtract only at the level actually present, not from unrelated liquidity.
    absent = order(quote(price=".55", size="5000"))
    assert depth_ahead(quote(), b, [absent]) == D(500)


def test_no_depth_uses_yes_asks_and_canonical_side_for_localized_labels():
    b = OrderBook("m", [], [Level(D(".4"), D(300)), Level(D(".41"), D(200)),
                            Level(D(".42"), D(99999))])
    q = replace(quote(outcome="NO", price=".58"), outcome="球队乙")
    owned = [order(quote(outcome="NO", price=".6", size="100")),
             order(replace(quote(price=".41", size="50"), side=Side.SELL))]
    assert depth_ahead(q, b, owned) == D(350)


@pytest.mark.parametrize("size,cancel,resume", [("1", 200, 400), ("100", 200, 400),
                                               ("200", 400, 800), ("500", 1000, 2000)])
def test_thresholds_scale_with_quantity_and_absolute_floor(size, cancel, resume):
    g = DepthGuard(DepthProtectionConfig())
    q = quote(size=size)
    result = g.observe(q, book(str(cancel), 0), [], now=0, live=True)
    assert result.cancel_threshold == cancel
    assert result.resume_threshold == resume
    assert result.reason is None  # Equality does not cancel.
    assert g.observe(q, book(str(cancel - 1), .1), [], now=.1, live=True).reason == "low_depth"


def test_drop_window_is_immediate_and_requires_low_remaining_depth():
    g = DepthGuard(DepthProtectionConfig())
    g.observe(quote(), book("1500", 0), [], now=0, live=True)
    r = g.observe(quote(), book("650", .1), [], now=.1, live=True)
    assert r.reason == "rapid_drop"  # No two-second delay.
    g = DepthGuard(DepthProtectionConfig())
    g.observe(quote(), book("10000", 0), [], now=0, live=True)
    assert g.observe(quote(), book("4000", .1), [], now=.1, live=True).reason is None


def test_old_peak_expires_and_repricing_resets_drop_baseline():
    g = DepthGuard(DepthProtectionConfig())
    g.observe(quote(), book("1500", 0), [], now=0, live=True)
    assert g.observe(quote(), book("650", 2.001), [], now=2.001, live=True).reason is None
    g.observe(quote(), book("1500", 3), [], now=3, live=True)
    assert g.observe(quote(price=".497"), book("650", 3.1), [], now=3.1, live=True).reason is None


def test_recovery_requires_stability_and_cooldown_from_ack_not_trigger():
    g = DepthGuard(DepthProtectionConfig())
    q = quote()
    g.observe(q, book("100", 0), [], now=0, live=True)
    g.block(q)
    assert not g.observe(q, book("800", 1), [], now=1, live=True).ready
    assert not g.observe(q, book("800", 20), [], now=20, live=True).ready  # No ACK yet.
    g.acknowledged(q, 20)
    assert not g.observe(q, book("800", 29.9), [], now=29.9, live=True).ready
    assert g.observe(q, book("800", 30), [], now=30, live=True).ready
    g.observe(q, book("799", 31), [], now=31, live=True)
    assert not g.observe(q, book("800", 32), [], now=32, live=True).ready
    assert not g.observe(q, book("800", 34.99), [], now=34.99, live=True).ready
    assert g.observe(q, book("800", 35), [], now=35, live=True).ready


def test_stale_rest_cannot_allow_submission_and_disconnect_resets_stability():
    g = DepthGuard(DepthProtectionConfig())
    q, b = quote(), book("800", 0, source="rest")
    g.observe(q, b, [], now=0, live=False)
    assert not g.observe(q, b, [], now=4, live=False).ready
    g.observe(q, book("800", 5), [], now=5, live=True)
    g.disconnected()
    assert not g.observe(q, book("800", 9), [], now=9, live=True).ready
    assert g.observe(q, book("800", 12), [], now=12, live=True).ready


def test_peak_memory_is_bounded_and_overflow_fails_closed():
    g = DepthGuard(DepthProtectionConfig())
    for i in range(600):
        t = i / 10000
        result = g.observe(quote(), book(str(10000 - i), t), [], now=t, live=True)
    assert len(g.states[selection(quote())].peaks) == g.MAX_PEAKS
    assert result.reason == "history_overflow"
    assert not result.ready
    assert g.observe(quote(), book("9000", 3), [], now=3, live=True).reason is None


class Client:
    orderbook_stream_connected = True
    wallet_stream_connected = True

    def __init__(self):
        self.calls = []
        self.created = []

    async def cancel_order(self, oid):
        self.calls.append(oid)

    async def create_order(self, q):
        self.created.append(q)
        return order(q)

    async def close(self):
        pass


def engine(client=None, **kwargs):
    return MarketMakerEngine(BotConfig(cancel_all_on_start=False, cancel_all_on_shutdown=False,
                                      **kwargs), client or Client(),
                             PassiveMakerStrategy(StrategyConfig()), RiskManager(RiskConfig()))


def test_depth_cancels_without_price_approach_and_deduplicates_repeated_updates():
    async def run():
        release, entered = asyncio.Event(), asyncio.Event()

        class Slow(Client):
            async def cancel_order(self, oid):
                self.calls.append(oid)
                entered.set()
                await release.wait()

        e = engine(Slow())
        o = order()
        e.open_orders[o.order_id] = o
        b = book("350", monotonic())
        assert e._approached_touch(o.quote, b) is None
        for _ in range(10):
            await e._handle_orderbook_update(b, wait_for_cancels=False)
        await asyncio.wait_for(entered.wait(), .5)
        assert e.client.calls == [o.order_id]
        assert e._depth_guard.cooldowns[selection(o.quote)] is None
        release.set()
        await asyncio.gather(*list(e._guard_cancel_tasks.values()))
        assert o.status == OrderStatus.CANCELED
        assert e._depth_guard.cooldowns[selection(o.quote)] > monotonic() + 9
        assert "m" in e._active_order_market_ids()  # Temporary recovery subscription.

    asyncio.run(run())


def test_depth_cancel_retries_without_new_updates():
    async def run():
        class Retry(Client):
            async def cancel_order(self, oid):
                self.calls.append(oid)
                if len(self.calls) == 1:
                    raise RuntimeError("temporary")

        e = engine(Retry())
        e.open_orders["o"] = order()
        await e._handle_orderbook_update(book("350", monotonic()))
        assert e._depth_guard.cooldowns[selection(quote())] is None
        await asyncio.wait_for(asyncio.gather(*list(e._guard_cancel_tasks.values())), 1)
        assert e.client.calls == ["o", "o"]

    asyncio.run(run())


def test_price_guard_still_cancels_when_depth_is_high_and_floor_depth_guard_is_independent():
    async def run():
        e = engine()
        e.open_orders["o"] = order()
        await e._handle_orderbook_update(book("10000", monotonic(), bid=".499"))
        assert e.client.calls == ["o"]
        e = engine()
        q = quote(price=".001")
        e.open_orders["o"] = order(q)
        b = book("10000", monotonic(), bid=".001", ask=".002")
        assert e._approached_touch(q, b) is None
        await e._handle_orderbook_update(b)
        assert e.client.calls == ["o"]  # Nothing strictly better than the floor price.

    asyncio.run(run())


def test_new_quote_waits_for_stability_then_rechecks_after_submit_slot(monkeypatch):
    async def run():
        clock = [0.0]
        monkeypatch.setattr("predict_mm.engine.monotonic", lambda: clock[0])
        e, q = engine(), quote()
        e._latest_orderbooks["m"] = book("1500", 0)
        await e._submit_quotes([q])
        assert not e.client.created
        clock[0] = 3
        e._latest_orderbooks["m"] = book("650", 3)
        await e._submit_quotes([q])
        assert not e.client.created
        clock[0] = 4
        e._latest_orderbooks["m"] = book("1500", 4)
        await e._submit_quotes([q])
        clock[0] = 7
        e._latest_orderbooks["m"] = book("1500", 7)
        await e._submit_quotes([q])
        assert len(e.client.created) == 1

    asyncio.run(run())


def test_depth_change_while_post_is_in_flight_cancels_on_registration(monkeypatch):
    async def run():
        clock = [0.0]
        monkeypatch.setattr("predict_mm.engine.monotonic", lambda: clock[0])

        class Changing(Client):
            async def create_order(self, q):
                e._latest_orderbooks["m"] = book("100", clock[0])
                return await super().create_order(q)

        e, q = engine(Changing()), quote()
        e._latest_orderbooks["m"] = book("1500", 0)
        await e._submit_quotes([q])
        clock[0] = 3
        e._latest_orderbooks["m"] = book("1500", 3)
        await e._submit_quotes([q])
        await asyncio.gather(*list(e._guard_cancel_tasks.values()))
        assert e.client.calls == ["o"]

    asyncio.run(run())


def test_watch_subscriptions_are_capped_expire_and_rest_candidates_can_still_warm(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("predict_mm.engine.monotonic", lambda: clock[0])
    e = engine()
    for i in range(100):
        q = quote(str(i))
        assert not e._depth_allows_submission(q, book("1500", 0, str(i), source="rest"))
    assert len(e._active_order_market_ids()) == e.DEPTH_WATCH_LIMIT
    clock[0] = 3
    e._prune_depth_watches()
    # Not granted a subscription, but fresh REST observations can permit entry.
    assert e._depth_allows_submission(quote("99"), book("1500", 3, "99", source="rest"))
    clock[0] = 61
    e._prune_depth_watches()
    assert not e._active_order_market_ids()
    assert not e._depth_allows_submission(quote("1"), book("1500", 61, "1"))


def test_default_guard_enabled_but_disabling_it_preserves_old_submission():
    assert BotConfig().depth_protection.enabled

    async def run():
        e = engine(depth_protection=DepthProtectionConfig(enabled=False))
        await e._submit_quotes([quote()])
        assert len(e.client.created) == 1

    asyncio.run(run())


def test_depth_checks_do_not_wait_for_quote_batch():
    async def run():
        entered, release, canceled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Streaming(Client):
            async def stream_wallet_fill_events(self):
                await asyncio.Event().wait()
                yield None

            async def stream_orderbook_updates(self, _):
                await entered.wait()
                yield book("350", monotonic())
                await asyncio.Event().wait()

            async def cancel_order(self, oid):
                self.calls.append(oid)
                canceled.set()

        e = engine(Streaming())
        e.open_orders["o"] = order()

        async def tick():
            entered.set()
            await release.wait()

        e._tick = tick
        task = asyncio.create_task(e.run())
        try:
            await asyncio.wait_for(canceled.wait(), .5)
            assert not release.is_set()
        finally:
            e.request_stop()
            release.set()
            await asyncio.wait_for(task, 1)
        assert not e._guard_cancel_tasks

    asyncio.run(run())


def test_synthetic_depth_replay_is_bounded(capsys):
    g = DepthGuard(DepthProtectionConfig())
    quotes = [quote(str(i)) for i in range(200)]
    started = perf_counter()
    for i in range(10000):
        q = quotes[i % len(quotes)]
        t = i / 1000
        result = g.observe(q, book(str(1500 + i % 300), t, q.market_id), [], now=t, live=True)
        assert result.reason is None
    elapsed = perf_counter() - started
    assert len(g.states) == 200
    assert all(len(s.peaks) <= g.MAX_PEAKS for s in g.states.values())
    print(f"Depth replay: 10000 updates / 200 markets: {elapsed:.3f}s, {elapsed / 10:.3f}ms/update")
    assert "Depth replay" in capsys.readouterr().out
