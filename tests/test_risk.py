from decimal import Decimal

from predict_mm.config import RiskConfig
from predict_mm.models import Quote, Side
from predict_mm.risk import RiskManager


def test_risk_rejects_large_order() -> None:
    risk = RiskManager(RiskConfig(max_order_size=Decimal("1")))
    quote = Quote(market_id="m1", side=Side.BUY, price=Decimal("0.5"), size=Decimal("2"))

    assert risk.filter_quotes([quote], [], {}) == []


def test_risk_accepts_small_order() -> None:
    risk = RiskManager(RiskConfig(max_order_size=Decimal("1")))
    quote = Quote(market_id="m1", side=Side.BUY, price=Decimal("0.5"), size=Decimal("1"))

    assert risk.filter_quotes([quote], [], {}) == [quote]

