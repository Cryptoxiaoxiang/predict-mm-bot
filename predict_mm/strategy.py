from __future__ import annotations

from decimal import Decimal

from predict_mm.config import MarketConfig, StrategyConfig
from predict_mm.models import OrderBook, Quote, Side, quantize_price


class PassiveMakerStrategy:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def build_quotes(self, market: MarketConfig, orderbook: OrderBook) -> list[Quote]:
        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask
        spread = orderbook.spread
        if not best_bid or not best_ask or spread is None:
            return []

        if spread < self.config.min_spread_to_quote:
            return []
        if spread > self.config.max_spread_to_quote:
            return []

        edge = self.config.tick_size * Decimal(self.config.min_edge_ticks)
        if self.config.join_best_price:
            raw_bid = best_bid.price
            raw_ask = best_ask.price
        else:
            raw_bid = best_bid.price - edge
            raw_ask = best_ask.price + edge

        bid = quantize_price(raw_bid, self.config.tick_size, Side.BUY)
        ask = quantize_price(raw_ask, self.config.tick_size, Side.SELL)

        if bid <= Decimal("0") or ask >= Decimal("1") or bid >= ask:
            return []

        quote_size = market.quote_size or self.config.quote_size
        return [
            Quote(
                market_id=market.id,
                side=Side.BUY,
                price=bid,
                size=quote_size,
                outcome=market.outcome,
                token_id=market.token_id,
                fee_rate_bps=market.fee_rate_bps,
                is_neg_risk=market.is_neg_risk,
                is_yield_bearing=market.is_yield_bearing,
            ),
            Quote(
                market_id=market.id,
                side=Side.SELL,
                price=ask,
                size=quote_size,
                outcome=market.outcome,
                token_id=market.token_id,
                fee_rate_bps=market.fee_rate_bps,
                is_neg_risk=market.is_neg_risk,
                is_yield_bearing=market.is_yield_bearing,
            ),
        ]
