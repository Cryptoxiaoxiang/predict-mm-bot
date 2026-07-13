from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from predict_mm.config import Settings
from predict_mm.models import Level, ManagedOrder, OrderBook, OrderStatus, Quote

logger = logging.getLogger("predict-mm")


class PredictClient:
    """Small Predict.fun API adapter.

    The exact Predict.fun API/SDK surface can change. Keep exchange-specific request signing and
    endpoint paths in this file so strategy/risk code stays stable.
    """

    def __init__(self, settings: Settings, dry_run: bool) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self.base_url = settings.api_base_url.rstrip("/")
        self._dry_orders: dict[str, ManagedOrder] = {}

    async def close(self) -> None:
        return None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.settings.api_key:
            headers["x-api-key"] = self.settings.api_key
        if self.settings.jwt_token:
            headers["Authorization"] = f"Bearer {self.settings.jwt_token}"
        return headers

    async def get_orderbook(self, market_id: str) -> OrderBook:
        if self.dry_run and self.settings.api_base_url == "https://api.predict.fun":
            return self._sample_orderbook(market_id)

        data = await self._request("GET", f"/markets/{market_id}/orderbook")
        return self._parse_orderbook(market_id, data)

    async def get_positions(self) -> dict[str, Decimal]:
        if self.dry_run:
            return {}

        data = await self._request("GET", "/portfolio/positions")
        positions: dict[str, Decimal] = {}
        for row in data.get("positions", data if isinstance(data, list) else []):
            market_id = str(row.get("market_id") or row.get("marketId"))
            size = Decimal(str(row.get("size") or row.get("quantity") or "0"))
            positions[market_id] = size
        return positions

    async def create_order(self, quote: Quote) -> ManagedOrder:
        if self.dry_run:
            order = ManagedOrder(order_id=f"dry-{uuid4().hex[:12]}", quote=quote, created_at=monotonic())
            self._dry_orders[order.order_id] = order
            logger.info(
                "DRY-RUN create %s %s %s @ %s on %s",
                quote.side,
                quote.size,
                quote.outcome,
                quote.price,
                quote.market_id,
            )
            return order

        payload = {
            "market_id": quote.market_id,
            "side": quote.side.value,
            "price": str(quote.price),
            "size": str(quote.size),
            "outcome": quote.outcome,
            "post_only": True,
        }
        data = await self._request("POST", "/orders", payload)
        order_id = str(data.get("id") or data.get("order_id") or data.get("orderId"))
        return ManagedOrder(order_id=order_id, quote=quote, created_at=monotonic())

    async def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            if order_id in self._dry_orders:
                self._dry_orders[order_id].status = OrderStatus.CANCELED
            logger.info("DRY-RUN cancel order %s", order_id)
            return

        await self._request("DELETE", f"/orders/{order_id}")

    async def cancel_all_orders(self, market_id: str | None = None) -> None:
        if self.dry_run:
            for order in self._dry_orders.values():
                if market_id is None or order.quote.market_id == market_id:
                    order.status = OrderStatus.CANCELED
            logger.info("DRY-RUN cancel all open orders market=%s", market_id or "*")
            return

        path = "/orders"
        if market_id:
            path = f"{path}?{urllib.parse.urlencode({'market_id': market_id})}"
        await self._request("DELETE", path)

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._request_sync, method, path, payload)
            except urllib.error.URLError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError("unreachable")

    def _request_sync(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers=self._headers(),
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _parse_orderbook(self, market_id: str, data: dict) -> OrderBook:
        bids_raw = data.get("bids") or data.get("buy") or []
        asks_raw = data.get("asks") or data.get("sell") or []
        bids = sorted([self._parse_level(row) for row in bids_raw], key=lambda level: level.price, reverse=True)
        asks = sorted([self._parse_level(row) for row in asks_raw], key=lambda level: level.price)
        return OrderBook(market_id=market_id, bids=bids, asks=asks)

    def _parse_level(self, row: dict | list) -> Level:
        if isinstance(row, list):
            return Level(price=Decimal(str(row[0])), size=Decimal(str(row[1])))
        return Level(
            price=Decimal(str(row.get("price"))),
            size=Decimal(str(row.get("size") or row.get("quantity"))),
        )

    def _sample_orderbook(self, market_id: str) -> OrderBook:
        return OrderBook(
            market_id=market_id,
            bids=[Level(Decimal("0.493"), Decimal("100")), Level(Decimal("0.490"), Decimal("80"))],
            asks=[Level(Decimal("0.507"), Decimal("120")), Level(Decimal("0.510"), Decimal("90"))],
        )
