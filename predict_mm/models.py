from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import StrEnum
from time import monotonic


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    OPEN = "open"
    FILLED = "filled"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    market_id: str
    bids: list[Level]
    asks: list[Level]
    tick_size: Decimal | None = None

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if not self.best_bid or not self.best_ask:
            return None
        return self.best_ask.price - self.best_bid.price


@dataclass(frozen=True)
class Quote:
    market_id: str
    side: Side
    price: Decimal
    size: Decimal
    outcome: str = "YES"
    token_id: str | None = None
    fee_rate_bps: int | None = None
    is_neg_risk: bool | None = None
    is_yield_bearing: bool | None = None


@dataclass
class ManagedOrder:
    order_id: str
    quote: Quote
    created_at: float
    status: OrderStatus = OrderStatus.OPEN
    order_hash: str | None = None
    filled_size: Decimal = Decimal("0")
    is_emergency_exit: bool = False

    @property
    def age_seconds(self) -> float:
        return monotonic() - self.created_at


@dataclass(frozen=True)
class WalletFillEvent:
    order_id: str
    filled_size: Decimal
    order_hash: str | None = None
    settlement_id: str | None = None
    event_type: str = "orderTransactionSuccess"


def quantize_price(price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
    rounding = ROUND_DOWN if side == Side.BUY else ROUND_UP
    ticks = (price / tick_size).to_integral_value(rounding=rounding)
    return ticks * tick_size
