from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import monotonic

from predict_mm.client import PredictClient
from predict_mm.config import BotConfig, MarketConfig
from predict_mm.models import (
    ManagedOrder,
    OrderBook,
    OrderStatus,
    Quote,
    Side,
    WalletFillEvent,
    WalletOrderStatusEvent,
)
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy

logger = logging.getLogger("predict-mm")

MarketTaskKey = tuple[str, str]


@dataclass(frozen=True)
class QuoteReference:
    best_bid: Decimal | None
    best_ask: Decimal | None
    target_price: Decimal


class MarketMakerEngine:
    MARKET_BATCH_SIZE = 20
    MARKET_BATCH_INTERVAL_SECONDS = 1.0
    MARKET_FETCH_CONCURRENCY = 5
    ORDER_SUBMIT_CONCURRENCY = 5
    NO_SAFE_QUOTE_BACKOFF_SECONDS = 15.0

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
        self._fill_events: asyncio.Queue[
            WalletFillEvent | WalletOrderStatusEvent | OrderBook
        ] = asyncio.Queue()
        self._wallet_task: asyncio.Task[None] | None = None
        self._orderbook_task: asyncio.Task[None] | None = None
        self._emergency_tasks: set[asyncio.Task[None]] = set()
        self._halted_markets: set[str] = set()
        self._prepared_emergency_markets: set[str] = set()
        self._submitted_fill_settlements: set[str] = set()
        self._handled_fill_settlements: set[str] = set()
        self._market_tick_sizes: dict[str, Decimal] = {}
        self._latest_orderbooks: dict[str, OrderBook] = {}
        self._order_quote_references: dict[str, QuoteReference] = {}
        self._extended_lifetime_orders: set[str] = set()
        self._active_market_queue: deque[MarketTaskKey] = deque()
        self._normal_market_queue: deque[MarketTaskKey] = deque(
            self._market_task_key(market) for market in self.config.enabled_markets
        )
        self._no_safe_quote_until: dict[MarketTaskKey, float] = {}
        self._degraded_fill_reconcile_interval_seconds = 0.5
        self._healthy_fill_reconcile_interval_seconds = max(
            2.0, self.config.poll_interval_seconds
        )
        self._emergency_retry_base_seconds = 0.5
        self._shutdown_cancel_retry_base_seconds = 0.5
        self._order_acceptance_timeout_seconds = max(
            5.0, self.config.poll_interval_seconds * 2
        )
        self._run_deadline: float | None = (
            monotonic() + self.config.run_duration_seconds
            if self.config.run_duration_seconds > 0
            else None
        )
        self._run_expires_at: datetime | None = (
            datetime.now(timezone.utc) + timedelta(seconds=self.config.run_duration_seconds)
            if self.config.run_duration_seconds > 0
            else None
        )

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def run_expires_at(self) -> str | None:
        return self._run_expires_at.isoformat() if self._run_expires_at else None

    @property
    def run_remaining_seconds(self) -> int | None:
        if self._run_deadline is None:
            return None
        return max(0, math.ceil(self._run_deadline - monotonic()))

    def market_title(self, market_id: str) -> str:
        configured = next(
            (market.title for market in self.config.markets if market.id == market_id and market.title),
            None,
        )
        cached_title = getattr(self.client, "cached_market_title", None)
        return configured or (cached_title(market_id) if cached_title else "")

    def active_orders(self) -> list[dict[str, object]]:
        orders: list[dict[str, object]] = []
        for order in self.open_orders.values():
            if order.status != OrderStatus.OPEN:
                continue
            orders.append(
                {
                    "order_id": order.order_id,
                    "market_id": order.quote.market_id,
                    "market_title": self.market_title(order.quote.market_id),
                    "side": order.quote.side.value,
                    "outcome": order.quote.outcome,
                    "price": str(order.quote.price),
                    "size": str(order.quote.size),
                    "is_emergency_exit": order.is_emergency_exit,
                    "age_seconds": max(0, math.floor(order.age_seconds)),
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

        self._restore_tracked_orders()
        started = False
        try:
            if self.config.cancel_all_on_start:
                await self._cancel_all_known_markets()

            if not self.config.dry_run:
                self._wallet_task = asyncio.create_task(self._watch_wallet_fills())
                self._orderbook_task = asyncio.create_task(self._watch_active_orderbooks())

            if self._run_deadline is not None:
                logger.info(
                    "Run duration enabled: orders will be cancelled and the market maker "
                    "will stop in %s seconds",
                    self.config.run_duration_seconds,
                )

            started = True
            next_quote_at = monotonic()
            next_fill_reconcile_at = (
                monotonic() + self._degraded_fill_reconcile_interval_seconds
            )
            next_lifetime_check_at = monotonic() + 1.0
            while not self._stop.is_set():
                now = monotonic()
                if self._run_deadline is not None and now >= self._run_deadline:
                    logger.info("Run duration reached; cancelling orders and stopping market maker")
                    self._stop.set()
                    break
                if not self._wallet_stream_connected():
                    next_fill_reconcile_at = min(
                        next_fill_reconcile_at,
                        now + self._degraded_fill_reconcile_interval_seconds,
                    )
                if now >= next_fill_reconcile_at:
                    await self._reconcile_buy_fills()
                    next_fill_reconcile_at = (
                        monotonic() + self._fill_reconcile_interval()
                    )
                if now >= next_lifetime_check_at:
                    await self._manage_active_order_lifetimes_from_cache()
                    next_lifetime_check_at = monotonic() + 1.0
                if monotonic() >= next_quote_at:
                    await self._tick()
                    next_quote_at = monotonic() + self.MARKET_BATCH_INTERVAL_SECONDS
                next_deadline = min(
                    next_quote_at,
                    next_fill_reconcile_at,
                    next_lifetime_check_at,
                )
                if self._run_deadline is not None:
                    next_deadline = min(next_deadline, self._run_deadline)
                await self._wait_for_fill_or_deadline(next_deadline)
        finally:
            if self._wallet_task is not None:
                self._wallet_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._wallet_task
            if self._orderbook_task is not None:
                self._orderbook_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._orderbook_task
            for task in self._emergency_tasks:
                task.cancel()
            if self._emergency_tasks:
                await asyncio.gather(*self._emergency_tasks, return_exceptions=True)
            if started and self.config.cancel_all_on_shutdown:
                await self._cancel_all_known_markets_safely()
            await self.client.close()
            logger.info("Market maker stopped")

    def _wallet_stream_connected(self) -> bool:
        return bool(getattr(self.client, "wallet_stream_connected", False))

    def _orderbook_stream_connected(self) -> bool:
        return bool(getattr(self.client, "orderbook_stream_connected", False))

    def _fill_reconcile_interval(self) -> float:
        if self._wallet_stream_connected():
            return self._healthy_fill_reconcile_interval_seconds
        return self._degraded_fill_reconcile_interval_seconds

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

    def _active_order_market_ids(self) -> set[str]:
        return {
            order.quote.market_id
            for order in self._working_orders()
            if not order.is_emergency_exit
        }

    async def _watch_active_orderbooks(self) -> None:
        while not self._stop.is_set():
            try:
                async for orderbook in self.client.stream_orderbook_updates(
                    self._active_order_market_ids
                ):
                    await self._fill_events.put(orderbook)
                    if self._stop.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "Active-order WebSocket disconnected: %s; using REST fallback until reconnect",
                    error,
                )
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=1)

    async def _wait_for_fill_or_deadline(self, deadline: float) -> None:
        while not self._stop.is_set():
            timeout = max(0, min(0.2, deadline - monotonic()))
            if timeout == 0:
                return
            try:
                event = await asyncio.wait_for(self._fill_events.get(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            if isinstance(event, OrderBook):
                await self._handle_orderbook_update(event)
            elif isinstance(event, WalletOrderStatusEvent):
                self._handle_wallet_order_status(event)
            else:
                await self._handle_wallet_fill(event)

    async def _handle_orderbook_update(self, orderbook: OrderBook) -> None:
        tick_size = orderbook.tick_size or self._market_tick_sizes.get(orderbook.market_id)
        if tick_size is not None and orderbook.tick_size is None:
            orderbook = replace(orderbook, tick_size=tick_size)
        self._latest_orderbooks[orderbook.market_id] = orderbook
        if tick_size is not None:
            self._market_tick_sizes[orderbook.market_id] = tick_size
        if self.config.replace_on_orderbook_change:
            await self._cancel_orders_approached_by_market(orderbook.market_id, orderbook)

    async def _manage_active_order_lifetimes_from_cache(self) -> None:
        for market in self.config.enabled_markets:
            if not any(
                self._order_matches_market_config(order, market)
                for order in self._working_orders()
                if not order.is_emergency_exit
            ):
                continue
            orderbook = self._latest_orderbooks.get(market.id)
            if orderbook is None:
                continue
            outcome_side = None
            resolve_outcome_side = getattr(self.client, "cached_outcome_side", None)
            if callable(resolve_outcome_side):
                outcome_side = resolve_outcome_side(market.id, market.outcome)
                if outcome_side is None:
                    continue
            quotes = self.strategy.build_quotes(
                market,
                orderbook,
                outcome_side=outcome_side,
            )
            await self._manage_order_lifetimes(market, orderbook, quotes)

    async def _tick(self) -> None:
        await self._reconcile_order_statuses()
        markets = self._next_market_batch()
        if not markets:
            return
        try:
            positions = await self.client.get_positions()
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Unable to read positions from Predict.fun; pausing new quotes for this cycle: %s",
                error,
            )
            return

        quotes_to_submit: list[Quote] = []
        quote_references: dict[tuple[str, Side, str, str, Decimal], QuoteReference] = {}
        reserved_orders: list[ManagedOrder] = []
        for market, orderbook in await self._fetch_orderbooks(markets):
            if orderbook is None:
                continue
            self._latest_orderbooks[market.id] = orderbook
            if orderbook.tick_size is not None:
                self._market_tick_sizes[market.id] = orderbook.tick_size
            if self.config.replace_on_orderbook_change:
                await self._cancel_orders_approached_by_market(market.id, orderbook)
            outcome_side = None
            resolve_outcome_side = getattr(self.client, "cached_outcome_side", None)
            if callable(resolve_outcome_side):
                outcome_side = resolve_outcome_side(market.id, market.outcome)
                if outcome_side is None:
                    logger.warning(
                        "Unable to map outcome %s on %s to Predict's YES/NO orderbook; "
                        "skipping this market for safety",
                        market.outcome,
                        market.id,
                    )
                    continue
            quotes = self.strategy.build_quotes(
                market,
                orderbook,
                outcome_side=outcome_side,
            )
            await self._manage_order_lifetimes(market, orderbook, quotes)
            task_key = self._market_task_key(market)
            if not quotes:
                logger.info("No safe quote for %s", market.id)
                self._no_safe_quote_until[task_key] = (
                    monotonic() + self.NO_SAFE_QUOTE_BACKOFF_SECONDS
                )
                continue
            self._no_safe_quote_until.pop(task_key, None)

            active = self._working_orders() + reserved_orders
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
                quotes_to_submit.append(quote)
                quote_references[self._quote_key(quote)] = self._quote_reference(
                    orderbook,
                    quote,
                )
                reserved_orders.append(
                    ManagedOrder(
                        order_id=f"reserved:{len(reserved_orders)}",
                        quote=quote,
                        created_at=monotonic(),
                        status=OrderStatus.PENDING,
                    )
                )
        await self._submit_quotes(quotes_to_submit, quote_references)
        await self._reconcile_order_statuses()

    def _next_market_batch(self, *, now: float | None = None) -> list[MarketConfig]:
        now = monotonic() if now is None else now
        enabled = [
            market
            for market in self.config.enabled_markets
            if market.id not in self._halted_markets
        ]
        configured_by_key = {
            self._market_task_key(market): market
            for market in enabled
        }
        working_orders = [
            order for order in self._working_orders() if not order.is_emergency_exit
        ]
        active_keys = {
            task_key
            for task_key, market in configured_by_key.items()
            if any(
                self._order_matches_market_config(order, market)
                for order in working_orders
            )
        }

        normal_order = [
            self._market_task_key(market)
            for market in enabled
            if self._market_task_key(market) not in active_keys
        ]
        active_order = [
            self._market_task_key(market)
            for market in enabled
            if self._market_task_key(market) in active_keys
        ]
        self._sync_market_queue(self._normal_market_queue, normal_order)
        self._sync_market_queue(self._active_market_queue, active_order)

        selected_keys: list[MarketTaskKey] = []
        if not self._orderbook_stream_connected():
            selected_keys.extend(
                self._take_market_keys(
                    self._active_market_queue,
                    self.MARKET_BATCH_SIZE,
                )
            )

        remaining = self.MARKET_BATCH_SIZE - len(selected_keys)
        if remaining:
            selected_keys.extend(
                self._take_market_keys(
                    self._normal_market_queue,
                    remaining,
                    eligible=lambda task_key: (
                        self._no_safe_quote_until.get(task_key, 0) <= now
                    ),
                )
            )

        return [configured_by_key[task_key] for task_key in selected_keys]

    @staticmethod
    def _sync_market_queue(
        queue: deque[MarketTaskKey],
        ordered_keys: list[MarketTaskKey],
    ) -> None:
        allowed = set(ordered_keys)
        retained = [task_key for task_key in queue if task_key in allowed]
        seen = set(retained)
        retained.extend(task_key for task_key in ordered_keys if task_key not in seen)
        queue.clear()
        queue.extend(retained)

    @staticmethod
    def _take_market_keys(
        queue: deque[MarketTaskKey],
        limit: int,
        *,
        eligible: Callable[[MarketTaskKey], bool] | None = None,
    ) -> list[MarketTaskKey]:
        if limit <= 0 or not queue:
            return []
        is_eligible = eligible or (lambda _task_key: True)
        selected: list[MarketTaskKey] = []
        for _ in range(len(queue)):
            task_key = queue.popleft()
            queue.append(task_key)
            if is_eligible(task_key):
                selected.append(task_key)
                if len(selected) >= limit:
                    break
        return selected

    @staticmethod
    def _market_task_key(market: MarketConfig) -> MarketTaskKey:
        return market.id, market.outcome.strip().casefold()

    @staticmethod
    def _order_matches_market_config(
        order: ManagedOrder,
        market: MarketConfig,
    ) -> bool:
        if order.quote.market_id != market.id:
            return False
        selected = market.outcome.strip().upper()
        if selected in {"YES_NO", "YES&NO", "YES AND NO"}:
            canonical_outcome = (
                order.quote.outcome_side or order.quote.outcome
            ).strip().upper()
            return canonical_outcome in {"YES", "NO"}
        return (
            order.quote.outcome.strip().casefold()
            == market.outcome.strip().casefold()
        )

    async def _fetch_orderbooks(
        self,
        markets: list[MarketConfig],
    ) -> list[tuple[MarketConfig, OrderBook | None]]:
        semaphore = asyncio.Semaphore(self.MARKET_FETCH_CONCURRENCY)

        async def fetch(market_id: str) -> tuple[str, OrderBook | None]:
            async with semaphore:
                try:
                    return market_id, await self.client.get_orderbook(market_id)
                except Exception as error:  # noqa: BLE001
                    logger.warning(
                        "Unable to read orderbook for %s; skipping this market for this cycle: %s",
                        market_id,
                        error,
                    )
                    return market_id, None

        market_ids = list(dict.fromkeys(market.id for market in markets))
        books = dict(await asyncio.gather(*(fetch(market_id) for market_id in market_ids)))
        return [(market, books[market.id]) for market in markets]

    async def _submit_quotes(
        self,
        quotes: list[Quote],
        quote_references: dict[
            tuple[str, Side, str, str, Decimal],
            QuoteReference,
        ] | None = None,
    ) -> None:
        semaphore = asyncio.Semaphore(self.ORDER_SUBMIT_CONCURRENCY)

        async def submit(quote: Quote) -> None:
            async with semaphore:
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
                    return

                # Register each successful response immediately. Waiting for the
                # entire concurrent batch lets a fast wallet fill overtake local
                # registration when another POST in the batch is still pending.
                reference = (quote_references or {}).get(self._quote_key(order.quote))
                self._register_order(order, reference)

        await asyncio.gather(*(submit(quote) for quote in quotes))

    def _register_order(
        self,
        order: ManagedOrder,
        reference: QuoteReference | None = None,
    ) -> ManagedOrder:
        existing = self.open_orders.get(order.order_id)
        if existing is None:
            registered = order
            self.open_orders[order.order_id] = registered
        else:
            # A wallet event can reconstruct the order before POST /v1/orders
            # returns. Preserve fill/status progress while enriching its quote
            # with the complete metadata from the eventual response.
            existing.quote = order.quote
            existing.order_hash = existing.order_hash or order.order_hash
            existing.is_emergency_exit = existing.is_emergency_exit or order.is_emergency_exit
            registered = existing
        if reference is not None:
            self._order_quote_references[registered.order_id] = reference
        self._remember_order(registered)
        return registered

    def _working_orders(self) -> list[ManagedOrder]:
        return [
            order
            for order in self.open_orders.values()
            if order.status in {OrderStatus.PENDING, OrderStatus.OPEN}
        ]

    async def _reconcile_order_statuses(self) -> None:
        """Treat Predict's OPEN orders response as the dashboard source of truth."""
        if self.config.dry_run:
            return
        candidates = self._working_orders()
        if not candidates:
            return
        try:
            official_open_ids = await self.client.get_open_order_ids()
        except Exception as error:  # noqa: BLE001
            logger.warning("Unable to confirm OPEN orders from Predict.fun: %s", error)
            return

        for order in candidates:
            if order.order_id in official_open_ids:
                if order.status == OrderStatus.PENDING:
                    order.status = OrderStatus.OPEN
                    self._remember_order(order)
                    logger.info(
                        "Order accepted and OPEN on Predict.fun: %s (%s %s %s @ %s on %s)",
                        order.order_id,
                        order.quote.side.value,
                        order.quote.size,
                        order.quote.outcome,
                        order.quote.price,
                        order.quote.market_id,
                    )
                continue

            if (
                order.status == OrderStatus.OPEN
                and order.age_seconds < self._order_acceptance_timeout_seconds
            ):
                # The wallet stream can confirm acceptance before the REST list
                # catches up. Give that authoritative event a short grace period.
                continue
            if order.status == OrderStatus.OPEN:
                order.status = OrderStatus.CANCELED
                self._remember_order(order)
                logger.warning(
                    "Order %s is no longer OPEN according to Predict.fun; removed from dashboard",
                    order.order_id,
                )
            elif order.age_seconds >= self._order_acceptance_timeout_seconds:
                logger.warning(
                    "Order submission %s was not confirmed OPEN by Predict.fun within %.1f seconds; "
                    "removing it defensively and keeping it off the dashboard",
                    order.order_id,
                    self._order_acceptance_timeout_seconds,
                )
                await self._cancel_order_safely(order)

    def _handle_wallet_order_status(self, event: WalletOrderStatusEvent) -> None:
        order = self.open_orders.get(event.order_id)
        if order is None and event.order_hash:
            order = next(
                (
                    candidate
                    for candidate in self.open_orders.values()
                    if candidate.order_hash == event.order_hash
                ),
                None,
            )
        if order is None:
            order = self._order_from_wallet_context(event)
            if order is not None:
                order.status = (
                    OrderStatus.OPEN
                    if event.event_type == "orderAccepted"
                    else OrderStatus.CANCELED
                )
                self._register_order(order)
                logger.warning(
                    "Recovered wallet %s for order %s from Predict.fun event details",
                    event.event_type,
                    event.order_id,
                )
            else:
                logger.warning(
                    "Predict.fun wallet event %s for unresolved order %s%s",
                    event.event_type,
                    event.order_id,
                    f" ({event.reason})" if event.reason else "",
                )
                return

        if event.event_type == "orderAccepted":
            order.status = OrderStatus.OPEN
            logger.info("Predict.fun accepted order %s into the orderbook", order.order_id)
        else:
            order.status = OrderStatus.CANCELED
            logger.warning(
                "Predict.fun %s order %s%s",
                {
                    "orderNotAccepted": "rejected",
                    "orderExpired": "expired",
                    "orderCancelled": "cancelled",
                }.get(event.event_type, event.event_type),
                order.order_id,
                f": {event.reason}" if event.reason else "",
            )
        self._remember_order(order)

    async def _cancel_orders_approached_by_market(self, market_id: str, orderbook: OrderBook) -> None:
        """Cancel quotes once the market touch is only one tick away from them."""
        tick_size = orderbook.tick_size or self.config.strategy.tick_size
        for order in list(self.open_orders.values()):
            if (
                order.status not in {OrderStatus.PENDING, OrderStatus.OPEN}
                or order.is_emergency_exit
                or order.quote.market_id != market_id
            ):
                continue

            canonical_outcome = (
                order.quote.outcome_side or order.quote.outcome
            ).strip().upper()
            if order.quote.side == Side.BUY and canonical_outcome == "NO":
                # Predict publishes only the YES book.  The current NO bid is
                # the complement of the best YES ask.
                if orderbook.best_ask is None:
                    continue
                touch_price = Decimal("1") - orderbook.best_ask.price
            else:
                best_price = (
                    orderbook.best_bid
                    if order.quote.side == Side.BUY
                    else orderbook.best_ask
                )
                if best_price is None:
                    continue
                touch_price = best_price.price

            is_approached = (
                touch_price <= order.quote.price + tick_size
                if order.quote.side == Side.BUY
                else touch_price >= order.quote.price - tick_size
            )
            if not is_approached:
                continue

            logger.info(
                "Canceling %s quote %s on %s: market touch %s is within one tick",
                order.quote.side.value,
                order.quote.price,
                market_id,
                touch_price,
            )
            await self._cancel_order_safely(order)

    async def _manage_order_lifetimes(
        self,
        market: MarketConfig,
        orderbook: OrderBook,
        target_quotes: list[Quote],
    ) -> None:
        """Refresh quotes after one lifetime, with at most one unchanged extension."""
        lifetime = self.config.cancel_after_seconds
        targets = {
            self._quote_selection_key(quote): quote
            for quote in target_quotes
        }
        for order in list(self.open_orders.values()):
            if (
                order.status not in {OrderStatus.PENDING, OrderStatus.OPEN}
                or order.is_emergency_exit
                or not self._order_matches_market_config(order, market)
            ):
                continue
            if order.age_seconds < lifetime:
                continue

            target_quote = targets.get(self._quote_selection_key(order.quote))
            if target_quote is None:
                logger.info(
                    "Refreshing order %s after %.1f seconds: no safe current target quote",
                    order.order_id,
                    order.age_seconds,
                )
                await self._cancel_order_safely(order)
                continue

            current_reference = self._quote_reference(orderbook, target_quote)
            original_reference = self._order_quote_references.setdefault(
                order.order_id,
                current_reference,
            )
            if order.age_seconds >= lifetime * 2:
                logger.info(
                    "Refreshing order %s after maximum %.1f-second lifetime",
                    order.order_id,
                    lifetime * 2,
                )
                await self._cancel_order_safely(order)
                continue

            if current_reference != original_reference:
                logger.info(
                    "Refreshing order %s after %.1f seconds: orderbook or target price changed",
                    order.order_id,
                    order.age_seconds,
                )
                await self._cancel_order_safely(order)
                continue

            if order.order_id not in self._extended_lifetime_orders:
                self._extended_lifetime_orders.add(order.order_id)
                logger.info(
                    "Keeping order %s for one extra %.1f-second lifetime: "
                    "orderbook and target price are unchanged",
                    order.order_id,
                    lifetime,
                )

    @staticmethod
    def _quote_selection_key(quote: Quote) -> tuple[Side, str]:
        canonical_outcome = (quote.outcome_side or quote.outcome).strip().upper()
        return quote.side, canonical_outcome

    @classmethod
    def _quote_key(cls, quote: Quote) -> tuple[str, Side, str, str, Decimal]:
        return (
            quote.market_id,
            quote.side,
            quote.outcome.strip().casefold(),
            (quote.outcome_side or "").strip().upper(),
            quote.price,
        )

    @staticmethod
    def _quote_reference(orderbook: OrderBook, quote: Quote) -> QuoteReference:
        return QuoteReference(
            best_bid=orderbook.best_bid.price if orderbook.best_bid else None,
            best_ask=orderbook.best_ask.price if orderbook.best_ask else None,
            target_price=quote.price,
        )

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
        self._order_quote_references.pop(order.order_id, None)
        self._extended_lifetime_orders.discard(order.order_id)
        self._remember_order(order)
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
        if order is None:
            order = await self._recover_wallet_fill_order(event)
            if order is None:
                logger.critical(
                    "Received a wallet fill that could not be recovered from memory, "
                    "the safety journal, event details, or the order hash: "
                    "order_id=%s order_hash=%s event=%s. Manual review is required.",
                    event.order_id,
                    event.order_hash,
                    event.event_type,
                )
                return
        if order.is_emergency_exit or order.quote.side != Side.BUY:
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
        self._remember_order(order)
        logger.critical(
            "Detected %s for buy order %s; starting emergency exit",
            event.event_type,
            order.order_id,
        )
        task = asyncio.create_task(self._emergency_exit(order, fill_size))
        self._emergency_tasks.add(task)
        task.add_done_callback(self._emergency_tasks.discard)

    def _order_from_wallet_context(
        self, event: WalletFillEvent | WalletOrderStatusEvent
    ) -> ManagedOrder | None:
        if not event.market_id or event.side is None or not event.outcome:
            return None
        event_fill_size = (
            event.filled_size if isinstance(event, WalletFillEvent) else Decimal("0")
        )
        size = event.order_size or event_fill_size
        if size <= Decimal("0"):
            return None
        return ManagedOrder(
            order_id=event.order_id,
            order_hash=event.order_hash,
            quote=Quote(
                market_id=event.market_id,
                side=event.side,
                price=event.price or Decimal("0"),
                size=size,
                outcome=event.outcome,
                outcome_side=event.outcome.strip().upper()
                if event.outcome.strip().upper() in {"YES", "NO"}
                else None,
            ),
            created_at=monotonic(),
            status=OrderStatus.PENDING,
        )

    async def _recover_wallet_fill_order(
        self, event: WalletFillEvent
    ) -> ManagedOrder | None:
        order = self._order_from_wallet_context(event)
        if order is None and event.order_hash:
            recover = getattr(self.client, "get_order_by_hash", None)
            if callable(recover):
                try:
                    order = await recover(event.order_hash)
                except Exception as error:  # noqa: BLE001
                    logger.critical(
                        "Unable to recover wallet fill %s by order hash: %s",
                        event.order_id,
                        error,
                    )
        if order is None:
            return None

        order.order_id = event.order_id
        order.order_hash = order.order_hash or event.order_hash
        # The event being handled is the source of truth for this delta. A hash
        # lookup may already report the cumulative fill, which would otherwise
        # make the emergency-exit calculation incorrectly discard this event.
        order.filled_size = Decimal("0")
        registered = self._register_order(order)
        logger.critical(
            "Recovered previously unregistered %s order %s on market %s; "
            "continuing wallet fill handling",
            registered.quote.side.value,
            registered.order_id,
            registered.quote.market_id,
        )
        return registered

    async def _prepare_emergency_exit(self, filled_order: ManagedOrder) -> None:
        market_id = filled_order.quote.market_id
        self._halted_markets.add(market_id)
        if market_id in self._prepared_emergency_markets:
            return
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
            if order.quote.market_id == market_id and order.status in {
                OrderStatus.PENDING,
                OrderStatus.OPEN,
            }:
                order.status = OrderStatus.CANCELED
        self._prepared_emergency_markets.add(market_id)

    async def _emergency_exit(self, filled_order: ManagedOrder, fill_size: Decimal) -> None:
        market_id = filled_order.quote.market_id
        exit_price = await self._emergency_exit_price(market_id)
        logger.critical(
            "BUY order filled on %s; canceling market quotes and selling %s "
            "at emergency limit %s",
            market_id,
            fill_size,
            exit_price,
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
                        price=exit_price,
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
            self._remember_order(exit_order)
            logger.critical(
                "Emergency %s sell order submitted for market %s: %s",
                exit_price,
                market_id,
                exit_order.order_id,
            )
            return

    async def _emergency_exit_price(self, market_id: str) -> Decimal:
        """Use the lowest aggressive limit supported by the current market tick."""
        fallback = Decimal("0.01")
        tick_size = self._market_tick_sizes.get(market_id)
        if tick_size is not None:
            return Decimal("0.001") if tick_size <= Decimal("0.001") else fallback
        try:
            orderbook = await self.client.get_orderbook(market_id)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Unable to read tick size for emergency exit on %s; using %s: %s",
                market_id,
                fallback,
                error,
            )
            return fallback
        tick_size = orderbook.tick_size
        if tick_size is not None:
            self._market_tick_sizes[market_id] = tick_size
        if tick_size is not None and tick_size <= Decimal("0.001"):
            return Decimal("0.001")
        return fallback

    async def _cancel_all_known_markets(self) -> None:
        # Predict's remove endpoint only hides orders from the public book. Use
        # one account-wide removal so stale orders from deleted market configs
        # cannot remain visible. New signatures also expire after 300 seconds.
        await self.client.cancel_all_orders(None)
        for order in self.open_orders.values():
            order.status = OrderStatus.CANCELED
            self._remember_order(order)

    async def _cancel_all_known_markets_safely(self) -> bool:
        """Retry shutdown cancellation without masking the error that stopped the engine."""
        for attempt in range(1, 6):
            try:
                await self._cancel_all_known_markets()
                return True
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                if attempt == 5:
                    logger.critical(
                        "Unable to cancel all orders during shutdown after %s attempts; "
                        "orders still expire after 300 seconds: %s",
                        attempt,
                        error,
                    )
                    return False
                retry_delay = min(
                    self._shutdown_cancel_retry_base_seconds * (2 ** (attempt - 1)),
                    4.0,
                )
                logger.warning(
                    "Shutdown cancellation attempt %s failed; retrying in %.1f seconds: %s",
                    attempt,
                    retry_delay,
                    error,
                )
                await asyncio.sleep(retry_delay)
        return False

    def _restore_tracked_orders(self) -> None:
        loader = getattr(self.client, "load_tracked_orders", None)
        if loader is None:
            return
        restored = loader()
        for order in restored:
            self.open_orders[order.order_id] = order
        if restored:
            logger.warning(
                "Restored %s bot-created orders from the local safety journal",
                len(restored),
            )

    def _remember_order(self, order: ManagedOrder) -> None:
        persist = getattr(self.client, "persist_tracked_order", None)
        if persist is not None:
            persist(order)
