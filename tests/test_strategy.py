from decimal import Decimal

from predict_mm.config import MarketConfig, StrategyConfig
from predict_mm.models import Level, OrderBook, Side
from predict_mm.strategy import PassiveMakerStrategy


def test_strategy_quotes_away_from_touch() -> None:
    strategy = PassiveMakerStrategy(
        StrategyConfig(tick_size=Decimal("0.001"), quote_size=Decimal("1"), min_edge_ticks=2)
    )
    book = OrderBook(
        market_id="m1",
        bids=[Level(Decimal("0.493"), Decimal("100"))],
        asks=[Level(Decimal("0.507"), Decimal("100"))],
    )

    quotes = strategy.build_quotes(MarketConfig(id="m1", outcome="YES"), book)

    assert len(quotes) == 2
    assert quotes[0].side == Side.BUY
    assert quotes[0].price == Decimal("0.491")
    assert quotes[1].side == Side.SELL
    assert quotes[1].price == Decimal("0.509")


def test_strategy_skips_tight_spread() -> None:
    strategy = PassiveMakerStrategy(StrategyConfig(min_spread_to_quote=Decimal("0.006")))
    book = OrderBook(
        market_id="m1",
        bids=[Level(Decimal("0.499"), Decimal("100"))],
        asks=[Level(Decimal("0.502"), Decimal("100"))],
    )

    assert strategy.build_quotes(MarketConfig(id="m1"), book) == []

