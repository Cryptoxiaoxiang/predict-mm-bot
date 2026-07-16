from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal
from time import monotonic

from predict_mm.client import PredictClient
from predict_mm.config import BotConfig
from predict_mm.models import ManagedOrder, OrderBook, OrderStatus, Side, WalletFillEvent
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
        self._emergency_tasks: set[asyncio.Task[None]] = set()
        self._halted_markets: set[str] = set()
        self._submitted_fill_settlements: set[str] = set()
        self._handled_fill_settlements: set[str] = set()
        self._emergency_retry_base_seconds = 0.5

    def request_stop(self) -> None:
        self._stop.set()

    def active_orders(self) -> list[dict[str, object]]:
        orders: list[dict[str, object]] = []
        for order in self.open_orders.values():
            if order.status != OrderStatus.OPEN:
                continue
            orders.append(
                {
                    "order_id": order.order_id,
                    "market_id": order.quote.market_id,
                    "side": order.quote.side.value,
                    "outcome": order.quote.outcome,
                    "price": str(order.quote.price),
                    "size": str(order.quote.size),
                    "is_emergency_exit": order.is_emergency_exit,
                }
            )
        return orders

    async def cancel_all_orders(self) -> None:
        await self._cancel_all_known_markets()

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
            for task in self._emergency_tasks:
                task.cancel()
            if self._emergency_tasks:
                await asyncio.gather(*self._emergency_tasks, return_exceptions=True)
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
        await self._reconcile_buy_fills()
        await self._cancel_stale_orders()
        positions = await self.client.get_positions()

        for market in self.config.enabled_markets:
            if market.id in self._halted_markets:
                continue
            orderbook = await self.client.get_orderbook(market.id)
            if self.config.replace_on_orderbook_change:
                await self._cancel_orders_approached_by_market(market.id, orderbook)
            quotes = self.strategy.build_quotes(market, orderbook)
            if not quotes:
                logger.info("No safe quote for %s", market.id)
                continue

            active = [order for order in self.open_orders.values() if order.status == OrderStatus.OPEN]
            missing_quotes = [
                quote
                for quote in quotes
                if not any(
                    order.quote.market_id == quote.market_id
                    and order.quote.side == quote.side
                    and order.quote.outcome.strip().casefold() == quote.outcome.strip().casefold()
                    and not order.is_emergency_exit
                    for order in active
                )
            ]
            approved = self.risk.filter_quotes(missing_quotes, active, positions)
            for quote in approved:
                try:
                    order = await self.client.create_order(quote)
                except Exception as error:  # noqa: BLE001
                    # A rejected passive quote must not bring down the wallet
                    # event stream. In particular, available collateral can
                    # change between risk evaluation and API submission while a
                    # different order is settling. Keeping the engine alive is
                    # essential so a later settlement-success event can still
                    # trigger the emergency exit.
                    logger.warning(
                        "Create quote failed on %s (%s %s %s @ %s); "
                        "skipping this quote and keeping the bot running: %s",
                        quote.market_id,
                        quote.side.value,
                        quote.size,
                        quote.outcome,
                        quote.price,
                        error,
                    )
                    continue
                self.open_orders[order.order_id] = order

    async def _cancel_orders_approached_by_market(self, market_id: str, orderbook: OrderBook) -> None:
        """Cancel quotes once the market touch is only one tick away from them."""
        tick_size = orderbook.tick_size or self.config.strategy.tick_size
        for order in list(self.open_orders.values()):
            if (
                order.status != OrderStatus.OPEN
                or order.is_emergency_exit
                or order.quote.market_id != market_id
            ):
                continue

            best_price = orderbook.best_bid if order.quote.side == Side.BUY else orderbook.best_ask
            if best_price is None:
                continue

            is_approached = (
                best_price.price <= order.quote.price + tick_size
                if order.quote.side == Side.BUY
                else best_price.price >= order.quote.price - tick_size
            )
            if not is_approached:
                continue

            logger.info(
                "Canceling %s quote %s on %s: market touch %s is within one tick",
                order.quote.side.value,
                order.quote.price,
                market_id,
                best_price.price,
            )
            await self._cancel_order_safely(order)

    async def _cancel_stale_orders(self) -> None:
        for order in list(self.open_orders.values()):
            if order.status != OrderStatus.OPEN or order.is_emergency_exit:
                continue
            if order.age_seconds < self.config.cancel_after_seconds:
                continue
            await self._cancel_order_safely(order)

    async def _cancel_order_safely(self, order: ManagedOrder) -> bool:
        """Keep a temporary cancel API failure from stopping the entire engine."""
        try:
            await self.client.cancel_order(order.order_id)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Cancel failed for order %s; keeping it active and retrying next cycle: %s",
                order.order_id,
                error,
            )
            return False
        order.status = OrderStatus.CANCELED
        return True

    async def _reconcile_buy_fills(self) -> None:
        """Recover fills missed while the no-snapshot wallet stream was disconnected."""
        if self.config.dry_run or not self.config.emergency_exit_on_buy_fill:
            return
        candidates = [
            order
            for order in self.open_orders.values()
            if not order.is_emergency_exit
            and order.quote.side == Side.BUY
            and order.filled_size < order.quote.size
        ]
        if not candidates:
            return
        try:
            filled_amounts = await self.client.get_order_filled_amounts()
        except Exception as error:  # noqa: BLE001
            logger.warning("Unable to reconcile order fills from REST: %s", error)
            return

        for order in candidates:
            cumulative = max(
                filled_amounts.get(order.order_id, Decimal("0")),
                filled_amounts.get(order.order_hash or "", Decimal("0")),
            )
            cumulative = min(cumulative, order.quote.size)
            delta = cumulative - order.filled_size
            if delta <= Decimal("0"):
                continue
            await self._handle_wallet_fill(
                WalletFillEvent(
                    order_id=order.order_id,
                    order_hash=order.order_hash,
                    filled_size=delta,
                    settlement_id=f"rest:{order.order_id}:{cumulative}",
                    event_type="REST order reconciliation",
                )
            )

    async def _handle_wallet_fill(self, event: WalletFillEvent) -> None:
        order = self.open_orders.get(event.order_id)
        if order is None and event.order_hash:
            order = next(
                (candidate for candidate in self.open_orders.values() if candidate.order_hash == event.order_hash),
                None,
            )
        if order is None or order.is_emergency_exit or order.quote.side != Side.BUY:
            return

        fill_size = event.filled_size
        if fill_size <= Decimal("0"):
            return

        settlement_key = event.settlement_id or (
            f"{event.order_id}:{event.order_hash or ''}:{fill_size}"
        )

        # Submitted means the match is being settled on-chain. Stop exposing the
        # market immediately, but do not try to sell yet: the bought ERC-1155
        # shares do not exist in the wallet until settlement succeeds.
        if event.event_type == "orderTransactionSubmitted":
            if settlement_key in self._submitted_fill_settlements:
                return
            self._submitted_fill_settlements.add(settlement_key)
            logger.critical(
                "Buy order %s matched; canceling market quotes while on-chain settlement completes",
                order.order_id,
            )
            await self._prepare_emergency_exit(order)
            return

        # A success event can race with a local cancellation. The order's local
        # CANCELED state therefore must not make us discard the confirmed fill.
        if settlement_key in self._handled_fill_settlements:
            return
        self._handled_fill_settlements.add(settlement_key)

        fill_size = min(fill_size, max(Decimal("0"), order.quote.size - order.filled_size))
        if fill_size <= Decimal("0"):
            return
        order.filled_size += fill_size
        logger.critical(
            "Detected %s for buy order %s; starting emergency exit",
            event.event_type,
            order.order_id,
        )
        task = asyncio.create_task(self._emergency_exit(order, fill_size))
        self._emergency_tasks.add(task)
        task.add_done_callback(self._emergency_tasks.discard)

    async def _prepare_emergency_exit(self, filled_order: ManagedOrder) -> None:
        market_id = filled_order.quote.market_id
        self._halted_markets.add(market_id)
        try:
            await self.client.cancel_all_orders(market_id)
        except Exception as error:  # noqa: BLE001
            logger.critical(
                "Could not cancel all market quotes before emergency exit; "
                "the market remains halted and the sell will still be attempted: %s",
                error,
            )
            return
        for order in self.open_orders.values():
            if order.quote.market_id == market_id and order.status == OrderStatus.OPEN:
                order.status = OrderStatus.CANCELED

    async def _emergency_exit(self, filled_order: ManagedOrder, fill_size: Decimal) -> None:
        market_id = filled_order.quote.market_id
        logger.critical(
            "BUY order filled on %s; canceling market quotes and selling %s at emergency limit 0.01",
            market_id,
            fill_size,
        )
        await self._prepare_emergency_exit(filled_order)

        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            try:
                exit_order = await self.client.create_order(
                    replace(
                        filled_order.quote,
                        side=Side.SELL,
                        price=Decimal("0.01"),
                        size=fill_size,
                    ),
                    post_only=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                retry_delay = min(
                    self._emergency_retry_base_seconds * (2 ** min(attempt - 1, 3)),
                    5.0,
                )
                if "insufficient shares" in str(error).casefold():
                    logger.critical(
                        "Emergency sell is waiting for %s shares to become available on %s; "
                        "retrying in %.1f seconds (attempt %s)",
                        fill_size,
                        market_id,
                        retry_delay,
                        attempt,
                    )
                else:
                    logger.critical(
                        "Emergency sell attempt %s failed on %s; retrying in %.1f seconds: %s",
                        attempt,
                        market_id,
                        retry_delay,
                        error,
                    )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=retry_delay)
                except asyncio.TimeoutError:
                    continue
                return

            exit_order.is_emergency_exit = True
            self.open_orders[exit_order.order_id] = exit_order
            logger.critical(
                "Emergency 0.01 sell order submitted for market %s: %s",
                market_id,
                exit_order.order_id,
            )
            return

    async def _cancel_all_known_markets(self) -> None:
        for market in self.config.enabled_markets:
            await self.client.cancel_all_orders(market.id)
        for order in self.open_orders.values():
            order.status = OrderStatus.CANCELED
