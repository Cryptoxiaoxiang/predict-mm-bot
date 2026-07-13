import asyncio
from decimal import Decimal

from predict_mm.config import BotConfig, MarketConfig, RiskConfig, StrategyConfig
from predict_mm.engine import MarketMakerEngine
from predict_mm.models import ManagedOrder, Quote, Side, WalletFillEvent
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


def test_active_order_markets_summarizes_open_orders() -> None:
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

    assert engine.active_order_markets() == [
        {
            "market_id": "market-1",
            "outcome": "YES",
            "buy_orders": 1,
            "sell_orders": 1,
            "emergency_exit_orders": 0,
        }
    ]
