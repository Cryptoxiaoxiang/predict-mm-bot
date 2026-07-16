import asyncio
from decimal import Decimal
from time import monotonic

from predict_mm.config import BotConfig, MarketConfig, RiskConfig, StrategyConfig
from predict_mm.engine import MarketMakerEngine
from predict_mm.models import (
    Level,
    ManagedOrder,
    OrderBook,
    OrderStatus,
    Quote,
    Side,
    WalletFillEvent,
)
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy


class EmergencyClient:
    def __init__(self) -> None:
        self.cancelled_markets: list[str] = []
        self.created: list[tuple[Quote, bool]] = []

    async def cancel_all_orders(self, market_id: str) -> None:
        self.cancelled_markets.append(market_id)

    async def create_order(self, quote: Quote, *, post_only: bool = True) -> ManagedOrder:
        self.created.append((quote, post_only))
        return ManagedOrder(order_id="emergency-exit", quote=quote, created_at=0)


async def handle_fill_and_wait(engine: MarketMakerEngine, event: WalletFillEvent) -> None:
    await engine._handle_wallet_fill(event)
    if engine._emergency_tasks:
        await asyncio.gather(*engine._emergency_tasks)


def test_buy_fill_cancels_market_and_creates_emergency_sell() -> None:
    client = EmergencyClient()
    engine = MarketMakerEngine(
        config=BotConfig(markets=[MarketConfig(id="market-1")]),
        client=client,  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig()),
        risk=RiskManager(RiskConfig()),
    )
    maker_order = ManagedOrder(
        order_id="maker-order",
        quote=Quote(
            market_id="market-1",
            side=Side.BUY,
            price=Decimal("0.50"),
            size=Decimal("3"),
        ),
        created_at=0,
    )
    engine.open_orders[maker_order.order_id] = maker_order

    asyncio.run(
        handle_fill_and_wait(
            engine,
            WalletFillEvent(order_id="maker-order", filled_size=Decimal("2")),
        )
    )

    assert client.cancelled_markets == ["market-1"]
    assert "market-1" in engine._halted_markets
    assert maker_order.status.value == "canceled"
    quote, post_only = client.created[0]
    assert quote.side == Side.SELL
    assert quote.price == Decimal("0.01")
    assert quote.size == Decimal("2")
    assert post_only is False


def test_cancelled_buy_fill_still_exits_once_per_settlement() -> None:
    client = EmergencyClient()
    engine = MarketMakerEngine(
        config=BotConfig(markets=[MarketConfig(id="market-1")]),
        client=client,  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig()),
        risk=RiskManager(RiskConfig()),
    )
    maker_order = ManagedOrder(
        order_id="maker-order",
        quote=Quote("market-1", Side.BUY, Decimal("0.50"), Decimal("3")),
        created_at=0,
        status=OrderStatus.CANCELED,
    )
    engine.open_orders[maker_order.order_id] = maker_order
    submitted = WalletFillEvent(
        order_id="maker-order",
        filled_size=Decimal("2"),
        settlement_id="settlement-1",
        event_type="orderTransactionSubmitted",
    )
    success = WalletFillEvent(
        order_id="maker-order",
        filled_size=Decimal("2"),
        settlement_id="settlement-1",
    )

    async def exercise() -> None:
        await engine._handle_wallet_fill(submitted)
        assert client.created == []
        await handle_fill_and_wait(engine, success)

    asyncio.run(exercise())

    assert len(client.created) == 1
    assert client.created[0][0].price == Decimal("0.01")


def test_active_orders_exposes_each_open_order() -> None:
    engine = MarketMakerEngine(
        config=BotConfig(markets=[MarketConfig(id="market-1")]),
        client=EmergencyClient(),  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig()),
        risk=RiskManager(RiskConfig()),
    )
    engine.open_orders["buy"] = ManagedOrder(
        order_id="buy",
        quote=Quote("market-1", Side.BUY, Decimal("0.50"), Decimal("1")),
        created_at=0,
    )
    engine.open_orders["sell"] = ManagedOrder(
        order_id="sell",
        quote=Quote("market-1", Side.SELL, Decimal("0.60"), Decimal("1")),
        created_at=0,
    )

    assert engine.active_orders() == [
        {
            "order_id": "buy",
            "market_id": "market-1",
            "side": "buy",
            "outcome": "YES",
            "price": "0.50",
            "size": "1",
            "is_emergency_exit": False,
        },
        {
            "order_id": "sell",
            "market_id": "market-1",
            "side": "sell",
            "outcome": "YES",
            "price": "0.60",
            "size": "1",
            "is_emergency_exit": False,
        },
    ]


class RepriceClient:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.created: list[Quote] = []

    async def get_positions(self) -> dict[str, Decimal]:
        return {}

    async def get_order_filled_amounts(self) -> dict[str, Decimal]:
        return {}

    async def get_orderbook(self, market_id: str) -> OrderBook:
        return OrderBook(
            market_id=market_id,
            bids=[Level(Decimal("0.49"), Decimal("100"))],
            asks=[Level(Decimal("0.55"), Decimal("100"))],
            tick_size=Decimal("0.01"),
        )

    async def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)

    async def create_order(self, quote: Quote, *, post_only: bool = True) -> ManagedOrder:
        self.created.append(quote)
        return ManagedOrder(order_id=f"new-{len(self.created)}", quote=quote, created_at=monotonic())


def test_tick_only_adds_the_missing_dual_outcome_quote() -> None:
    client = RepriceClient()
    engine = MarketMakerEngine(
        config=BotConfig(
            dry_run=True,
            markets=[MarketConfig(id="market-1", outcome="YES_NO", quote_size=Decimal("1"))],
        ),
        client=client,  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig()),
        risk=RiskManager(RiskConfig(max_open_orders_per_market=2)),
    )
    engine.open_orders["existing-yes"] = ManagedOrder(
        order_id="existing-yes",
        quote=Quote("market-1", Side.BUY, Decimal("0.45"), Decimal("1"), "Yes"),
        created_at=monotonic(),
    )

    asyncio.run(engine._tick())

    assert [(quote.side, quote.outcome) for quote in client.created] == [(Side.BUY, "No")]


def test_rejected_passive_quote_does_not_stop_tick_or_fill_monitoring(caplog) -> None:
    class PartiallyFundedClient(RepriceClient):
        async def create_order(self, quote: Quote, *, post_only: bool = True) -> ManagedOrder:
            if quote.market_id == "market-1":
                raise RuntimeError(
                    "HTTP 400: Insufficient collateral: available balance is less than the total bid amount."
                )
            return await super().create_order(quote, post_only=post_only)

        async def get_orderbook(self, market_id: str) -> OrderBook:
            return OrderBook(
                market_id=market_id,
                bids=[Level(Decimal("0.49"), Decimal("100"))],
                asks=[Level(Decimal("0.55"), Decimal("100"))],
                tick_size=Decimal("0.01"),
            )

    client = PartiallyFundedClient()
    engine = MarketMakerEngine(
        config=BotConfig(
            markets=[
                MarketConfig(id="market-1"),
                MarketConfig(id="market-2"),
            ]
        ),
        client=client,  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig()),
        risk=RiskManager(RiskConfig()),
    )

    with caplog.at_level("WARNING", logger="predict-mm"):
        asyncio.run(engine._tick())

    assert [quote.market_id for quote in client.created] == ["market-2"]
    assert list(engine.open_orders) == ["new-1"]
    assert "keeping the bot running" in caplog.text


def test_approached_buy_quote_is_canceled_and_repriced_in_same_tick() -> None:
    client = RepriceClient()
    engine = MarketMakerEngine(
        config=BotConfig(markets=[MarketConfig(id="market-1")]),
        client=client,  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig(min_edge_ticks=2)),
        risk=RiskManager(RiskConfig()),
    )
    engine.open_orders["old-buy"] = ManagedOrder(
        order_id="old-buy",
        quote=Quote("market-1", Side.BUY, Decimal("0.48"), Decimal("1")),
        created_at=monotonic(),
    )

    asyncio.run(engine._tick())

    assert client.cancelled == ["old-buy"]
    assert [quote.price for quote in client.created] == [Decimal("0.47")]


def test_approached_sell_quote_is_canceled() -> None:
    client = RepriceClient()
    engine = MarketMakerEngine(
        config=BotConfig(markets=[MarketConfig(id="market-1")]),
        client=client,  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig()),
        risk=RiskManager(RiskConfig()),
    )
    engine.open_orders["old-sell"] = ManagedOrder(
        order_id="old-sell",
        quote=Quote("market-1", Side.SELL, Decimal("0.57"), Decimal("1")),
        created_at=monotonic(),
    )
    book = OrderBook(
        market_id="market-1",
        bids=[Level(Decimal("0.49"), Decimal("100"))],
        asks=[Level(Decimal("0.56"), Decimal("100"))],
        tick_size=Decimal("0.01"),
    )

    asyncio.run(engine._cancel_orders_approached_by_market("market-1", book))

    assert client.cancelled == ["old-sell"]


def test_temporary_cancel_failure_keeps_engine_running_and_order_open(caplog) -> None:
    class FailingCancelClient(RepriceClient):
        async def cancel_order(self, order_id: str) -> None:
            raise RuntimeError("HTTP 500: verify and cancel orders by id")

    client = FailingCancelClient()
    engine = MarketMakerEngine(
        config=BotConfig(markets=[MarketConfig(id="market-1")], cancel_after_seconds=0),
        client=client,  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig()),
        risk=RiskManager(RiskConfig()),
    )
    order = ManagedOrder(
        order_id="old-buy",
        quote=Quote("market-1", Side.BUY, Decimal("0.48"), Decimal("1")),
        created_at=0,
    )
    engine.open_orders[order.order_id] = order

    with caplog.at_level("WARNING", logger="predict-mm"):
        asyncio.run(engine._cancel_stale_orders())

    assert order.status == OrderStatus.OPEN
    assert "retrying next cycle" in caplog.text


def test_rest_reconciliation_recovers_missed_buy_fill() -> None:
    class ReconciliationClient(EmergencyClient):
        async def get_order_filled_amounts(self) -> dict[str, Decimal]:
            return {"maker-order": Decimal("2")}

    client = ReconciliationClient()
    engine = MarketMakerEngine(
        config=BotConfig(markets=[MarketConfig(id="market-1")]),
        client=client,  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig()),
        risk=RiskManager(RiskConfig()),
    )
    maker_order = ManagedOrder(
        order_id="maker-order",
        quote=Quote("market-1", Side.BUY, Decimal("0.50"), Decimal("3")),
        created_at=0,
    )
    engine.open_orders[maker_order.order_id] = maker_order

    async def reconcile_and_wait() -> None:
        await engine._reconcile_buy_fills()
        if engine._emergency_tasks:
            await asyncio.gather(*engine._emergency_tasks)

    asyncio.run(reconcile_and_wait())

    assert maker_order.filled_size == Decimal("2")
    assert client.created[0][0].side == Side.SELL
    assert client.created[0][0].price == Decimal("0.01")
    assert client.created[0][1] is False


def test_emergency_sell_retries_when_settled_shares_are_not_yet_available(caplog) -> None:
    class DelayedSharesClient(EmergencyClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def create_order(self, quote: Quote, *, post_only: bool = True) -> ManagedOrder:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError(
                    "HTTP 400: Insufficient shares: token balance is less than the total ask amount."
                )
            return await super().create_order(quote, post_only=post_only)

    client = DelayedSharesClient()
    engine = MarketMakerEngine(
        config=BotConfig(markets=[MarketConfig(id="market-1")]),
        client=client,  # type: ignore[arg-type]
        strategy=PassiveMakerStrategy(StrategyConfig()),
        risk=RiskManager(RiskConfig()),
    )
    engine._emergency_retry_base_seconds = 0
    maker_order = ManagedOrder(
        order_id="maker-order",
        quote=Quote("market-1", Side.BUY, Decimal("0.50"), Decimal("100")),
        created_at=0,
    )
    engine.open_orders[maker_order.order_id] = maker_order

    with caplog.at_level("CRITICAL", logger="predict-mm"):
        asyncio.run(
            handle_fill_and_wait(
                engine,
                WalletFillEvent(order_id="maker-order", filled_size=Decimal("100")),
            )
        )

    assert client.attempts == 2
    assert len(client.created) == 1
    assert client.created[0][0].price == Decimal("0.01")
    assert "waiting for 100 shares" in caplog.text
