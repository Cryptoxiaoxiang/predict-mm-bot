import asyncio
from decimal import Decimal
from time import monotonic

from predict_mm.config import BotConfig, MarketConfig, RiskConfig, StrategyConfig
from predict_mm.engine import MarketMakerEngine
from predict_mm.models import Level, ManagedOrder, OrderBook, Quote, Side, WalletFillEvent
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
        engine._handle_wallet_fill(WalletFillEvent(order_id="maker-order", filled_size=Decimal("2")))
    )

    assert client.cancelled_markets == ["market-1"]
    assert "market-1" in engine._halted_markets
    assert maker_order.status.value == "canceled"
    quote, post_only = client.created[0]
    assert quote.side == Side.SELL
    assert quote.price == Decimal("0.01")
    assert quote.size == Decimal("2")
    assert post_only is False


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
    assert [quote.price for quote in client.created] == [Decimal("0.47"), Decimal("0.57")]


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