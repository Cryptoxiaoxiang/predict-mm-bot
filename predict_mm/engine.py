from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal
from time import monotonic

from predict_mm.client import PredictClient
from predict_mm.config import BotConfig
from predict_mm.models import ManagedOrder, OrderStatus, Side, WalletFillEvent
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
        self._fill_events: asyncio.Queue[WalletFillEvent] = asyncio.Queue()
        self._wallet_task: asyncio.Task[None] | None = None
        self._halted_markets: set[str] = set()

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

        if not self.config.dry_run and self.config.emergency_exit_on_buy_fill:
            self._wallet_task = asyncio.create_task(self._watch_wallet_fills())

        try:
            next_quote_at = monotonic()
            while not self._stop.is_set():
                if monotonic() >= next_quote_at:
                    await self._tick()
                    next_quote_at = monotonic() + self.config.poll_interval_seconds
                await self._wait_for_fill_or_next_quote(next_quote_at)
        finally:
            if self._wallet_task is not None:
                self._wallet_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._wallet_task
            if self.config.cancel_all_on_shutdown:
                await self._cancel_all_known_markets()
            await self.client.close()
            logger.info("Market maker stopped")

    async def _watch_wallet_fills(self) -> None:
        while not self._stop.is_set():
            try:
                async for event in self.client.stream_wallet_fill_events():
                    await self._fill_events.put(event)
                    if self._stop.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.warning("Wallet event stream disconnected: %s; retrying shortly", error)
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=1)

    async def _wait_for_fill_or_next_quote(self, next_quote_at: float) -> None:
        while not self._stop.is_set():
            timeout = max(0, min(0.2, next_quote_at - monotonic()))
            if timeout == 0:
                return
            try:
                event = await asyncio.wait_for(self._fill_events.get(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            await self._handle_wallet_fill(event)

    async def _tick(self) -> None:
        await self._cancel_stale_orders()
        positions = await self.client.get_positions()

        for market in self.config.enabled_markets:
            if market.id in self._halted_markets:
                continue
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
            if order.status != OrderStatus.OPEN or order.is_emergency_exit:
                continue
            if order.age_seconds < self.config.cancel_after_seconds:
                continue
            await self.client.cancel_order(order.order_id)
            order.status = OrderStatus.CANCELED

    async def _handle_wallet_fill(self, event: WalletFillEvent) -> None:
        order = self.open_orders.get(event.order_id)
        if order is None and event.order_hash:
            order = next(
                (candidate for candidate in self.open_orders.values() if candidate.order_hash == event.order_hash),
                None,
            )
        if (
            order is None
            or order.status != OrderStatus.OPEN
            or order.is_emergency_exit
            or order.quote.side != Side.BUY
        ):
            return

        fill_size = event.filled_size
        if fill_size <= Decimal("0"):
            return
        order.filled_size += fill_size
        await self._emergency_exit(order, fill_size)

    async def _emergency_exit(self, filled_order: ManagedOrder, fill_size: Decimal) -> None:
        market_id = filled_order.quote.market_id
        self._halted_markets.add(market_id)
        logger.critical(
            "BUY order filled on %s; canceling market quotes and selling %s at emergency limit 0.01",
            market_id,
            fill_size,
        )
        await self.client.cancel_all_orders(market_id)
        for order in self.open_orders.values():
            if order.quote.market_id == market_id and order.status == OrderStatus.OPEN:
                order.status = OrderStatus.CANCELED

        exit_order = await self.client.create_order(
            replace(
                filled_order.quote,
                side=Side.SELL,
                price=Decimal("0.01"),
                size=fill_size,
            ),
            post_only=False,
        )
        exit_order.is_emergency_exit = True
        self.open_orders[exit_order.order_id] = exit_order
        logger.critical("Emergency 0.01 sell order submitted for market %s: %s", market_id, exit_order.order_id)

    async def _cancel_all_known_markets(self) -> None:
        for market in self.config.enabled_markets:
            await self.client.cancel_all_orders(market.id)
        for order in self.open_orders.values():
            order.status = OrderStatus.CANCELED
