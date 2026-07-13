from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from predict_mm.client import PredictClient
from predict_mm.config import BotConfig
from predict_mm.models import ManagedOrder, OrderStatus
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy

logger = logging.getLogger("predict-mm")


class MarketMakerEngine:
    def __init__(
        self,
        config: BotConfig,
        client: PredictClient,
        strategy: PassiveMakerStrategy,
        risk: RiskManager,
    ) -> None:
        self.config = config
        self.client = client
        self.strategy = strategy
        self.risk = risk
        self.open_orders: dict[str, ManagedOrder] = {}
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info(
            "Starting market maker: dry_run=%s, markets=%s",
            self.config.dry_run,
            [market.id for market in self.config.enabled_markets],
        )

        if self.config.cancel_all_on_start:
            await self._cancel_all_known_markets()

        try:
            while not self._stop.is_set():
                await self._tick()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.config.poll_interval_seconds,
                    )
        finally:
            if self.config.cancel_all_on_shutdown:
                await self._cancel_all_known_markets()
            await self.client.close()
            logger.info("Market maker stopped")

    async def _tick(self) -> None:
        await self._cancel_stale_orders()
        positions = await self.client.get_positions()

        for market in self.config.enabled_markets:
            orderbook = await self.client.get_orderbook(market.id)
            quotes = self.strategy.build_quotes(market, orderbook)
            if not quotes:
                logger.info("No safe quote for %s", market.id)
                continue

            active = [order for order in self.open_orders.values() if order.status == OrderStatus.OPEN]
            approved = self.risk.filter_quotes(quotes, active, positions)
            for quote in approved:
                order = await self.client.create_order(quote)
                self.open_orders[order.order_id] = order

    async def _cancel_stale_orders(self) -> None:
        for order in list(self.open_orders.values()):
            if order.status != OrderStatus.OPEN:
                continue
            if order.age_seconds < self.config.cancel_after_seconds:
                continue
            await self.client.cancel_order(order.order_id)
            order.status = OrderStatus.CANCELED

    async def _cancel_all_known_markets(self) -> None:
        for market in self.config.enabled_markets:
            await self.client.cancel_all_orders(market.id)
        for order in self.open_orders.values():
            order.status = OrderStatus.CANCELED
