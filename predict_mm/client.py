from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass, replace
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from predict_mm.config import Settings
from predict_mm.models import Level, ManagedOrder, OrderBook, OrderStatus, Quote, WalletFillEvent

logger = logging.getLogger("predict-mm")


class PredictClient:
    """Predict.fun REST + SDK adapter.

    REST endpoints follow the current Predict API beta docs. Real order creation uses the official
    ``predict-sdk`` package to build and sign orders before POSTing them to the order API.
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

        response = await self._request("GET", f"/v1/markets/{market_id}/orderbook")
        return self._parse_orderbook(market_id, self._data(response))

    async def get_positions(self) -> dict[str, Decimal]:
        if self.dry_run:
            return {}

        self._require_api_key()
        self._require_jwt()
        data = await self._request("GET", "/v1/positions", query={"first": 100})
        positions: dict[str, Decimal] = {}
        for row in self._rows(data):
            market = row.get("market") if isinstance(row, dict) else {}
            market_id = str(
                row.get("marketId")
                or row.get("market_id")
                or (market or {}).get("id")
                or ""
            )
            if not market_id:
                continue
            amount = Decimal(str(row.get("amount") or row.get("size") or row.get("quantity") or "0"))
            outcome = self._outcome_name(row.get("outcome"))
            signed_amount = -amount if outcome.upper() == "NO" else amount
            positions[market_id] = positions.get(market_id, Decimal("0")) + signed_amount
        return positions

    async def create_order(self, quote: Quote, *, post_only: bool = True) -> ManagedOrder:
        if self.dry_run:
            order = ManagedOrder(order_id=f"dry-{uuid4().hex[:12]}", quote=quote, created_at=monotonic())
            self._dry_orders[order.order_id] = order
            logger.info(
                "DRY-RUN create%s %s %s %s @ %s on %s",
                " emergency" if not post_only else "",
                quote.side,
                quote.size,
                quote.outcome,
                quote.price,
                quote.market_id,
            )
            return order

        self._require_api_key()
        self._require_jwt()
        quote = await self._complete_quote_with_market_metadata(quote)
        signed_order_payload = await asyncio.to_thread(
            self._build_signed_limit_order_payload, quote, post_only
        )
        response = await self._request("POST", "/v1/orders", signed_order_payload)
        data = self._data(response)
        order_id = str(
            data.get("orderId")
            or data.get("order_id")
            or data.get("id")
            or data.get("orderHash")
            or data.get("order_hash")
        )
        return ManagedOrder(
            order_id=order_id,
            quote=quote,
            created_at=monotonic(),
            order_hash=str(data.get("orderHash") or data.get("order_hash") or "") or None,
        )

    async def stream_wallet_fill_events(self):
        """Yield confirmed wallet fills from Predict's account WebSocket."""
        if self.dry_run:
            return

        self._require_api_key()
        self._require_jwt()
        try:
            from websockets.asyncio.client import connect
        except ImportError as error:
            raise RuntimeError("WebSocket support requires the websockets package.") from error

        async with connect(
            "wss://ws.predict.fun/ws",
            additional_headers={"x-api-key": self.settings.api_key},
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "requestId": 1,
                        "params": [f"predictWalletEvents/{self.settings.jwt_token}"],
                    }
                )
            )
            async for raw_message in websocket:
                message = json.loads(raw_message)
                if message.get("method") == "heartbeat":
                    await websocket.send(json.dumps({"method": "heartbeat", "data": message.get("data")}))
                    continue
                event = self._wallet_fill_event(message)
                if event is not None:
                    yield event

    async def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            if order_id in self._dry_orders:
                self._dry_orders[order_id].status = OrderStatus.CANCELED
            logger.info("DRY-RUN cancel order %s", order_id)
            return

        self._require_api_key()
        self._require_jwt()
        await self._request("POST", "/v1/orders/remove", {"data": {"ids": [order_id]}})

    async def cancel_all_orders(self, market_id: str | None = None) -> None:
        if self.dry_run:
            for order in self._dry_orders.values():
                if market_id is None or order.quote.market_id == market_id:
                    order.status = OrderStatus.CANCELED
            logger.info("DRY-RUN cancel all open orders market=%s", market_id or "*")
            return

        self._require_api_key()
        self._require_jwt()
        open_order_ids = await self._get_open_order_ids(market_id)
        if not open_order_ids:
            return
        await self._request("POST", "/v1/orders/remove", {"data": {"ids": open_order_ids}})

    async def _get_open_order_ids(self, market_id: str | None = None) -> list[str]:
        query: dict[str, object] = {"first": 100, "status": "OPEN"}
        if market_id is not None:
            query["marketId"] = market_id
        response = await self._request("GET", "/v1/orders", query=query)
        ids: list[str] = []
        for row in self._rows(response):
            order_id = row.get("id") or row.get("orderId") or row.get("order_id")
            if order_id:
                ids.append(str(order_id))
        return ids

    async def _complete_quote_with_market_metadata(self, quote: Quote) -> Quote:
        if (
            quote.token_id
            and quote.fee_rate_bps is not None
            and quote.is_neg_risk is not None
            and quote.is_yield_bearing is not None
        ):
            return quote

        response = await self._request("GET", f"/v1/markets/{quote.market_id}")
        market = self._data(response)
        return replace(
            quote,
            token_id=quote.token_id or self._find_outcome_token_id(market, quote.outcome),
            fee_rate_bps=quote.fee_rate_bps
            if quote.fee_rate_bps is not None
            else self._optional_int(self._first_present(market, "feeRateBps", "fee_rate_bps")),
            is_neg_risk=quote.is_neg_risk
            if quote.is_neg_risk is not None
            else self._optional_bool(self._first_present(market, "isNegRisk", "is_neg_risk")),
            is_yield_bearing=quote.is_yield_bearing
            if quote.is_yield_bearing is not None
            else self._optional_bool(self._first_present(market, "isYieldBearing", "is_yield_bearing")),
        )

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict[str, object] | None = None,
    ) -> dict:
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._request_sync, method, path, payload, query)
            except urllib.error.URLError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError("unreachable")

    def _request_sync(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict[str, object] | None = None,
    ) -> dict:
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}{path}"
        if query:
            clean_query = {
                key: value
                for key, value in query.items()
                if value is not None and value != ""
            }
            if clean_query:
                url = f"{url}?{urllib.parse.urlencode(clean_query)}"
        request = urllib.request.Request(
            url,
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
        bids = sorted(
            [self._parse_level(row) for row in bids_raw],
            key=lambda level: level.price,
            reverse=True,
        )
        asks = sorted([self._parse_level(row) for row in asks_raw], key=lambda level: level.price)
        return OrderBook(market_id=market_id, bids=bids, asks=asks)

    def _parse_level(self, row: dict | list) -> Level:
        if isinstance(row, (list, tuple)):
            if len(row) < 2:
                raise ValueError(f"invalid orderbook price level: {row!r}")
            return Level(price=Decimal(str(row[0])), size=Decimal(str(row[1])))
        return Level(
            price=Decimal(str(row.get("price"))),
            size=Decimal(str(row.get("size") or row.get("quantity"))),
        )

    def _build_signed_limit_order_payload(self, quote: Quote, post_only: bool = True) -> dict:
        self._require_real_order_inputs(quote)
        try:
            from predict_sdk import (  # type: ignore[import-not-found]
                BuildOrderInput,
                ChainId,
                LimitHelperInput,
                OrderBuilder,
                OrderBuilderOptions,
                Side as PredictSide,
            )
        except ImportError as error:
            raise RuntimeError(
                "Real order creation requires the official Predict SDK. "
                "Install project dependencies with `pip install -e .`."
            ) from error

        sdk_side = PredictSide.BUY if quote.side.value == "buy" else PredictSide.SELL
        options = None
        if self.settings.predict_account_address:
            options = OrderBuilderOptions(predict_account=self.settings.predict_account_address)

        chain_id = ChainId(self.settings.chain_id)
        builder = OrderBuilder.make(chain_id, self.settings.private_key, options)
        amounts = builder.get_limit_order_amounts(
            LimitHelperInput(
                side=sdk_side,
                price_per_share_wei=self._to_wei(quote.price),
                quantity_wei=self._to_wei(quote.size),
            )
        )
        order = builder.build_order(
            "LIMIT",
            BuildOrderInput(
                side=sdk_side,
                token_id=quote.token_id,
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=int(quote.fee_rate_bps or 0),
            ),
        )
        typed_data = builder.build_typed_data(
            order,
            is_neg_risk=bool(quote.is_neg_risk),
            is_yield_bearing=bool(quote.is_yield_bearing),
        )
        order_hash = builder.build_typed_data_hash(typed_data)
        signed_order = builder.sign_typed_data_order(typed_data)
        signed_order_dict = self._signed_order_to_api_dict(signed_order, order_hash)
        data = {
            "pricePerShare": str(quote.price),
            "strategy": "LIMIT",
            "isPostOnly": post_only,
            "selfTradePrevention": "CANCEL_MAKER",
            "order": signed_order_dict,
        }
        if post_only:
            data["reservedBalancePolicy"] = "REJECT_MARKET_ORDER"
        return {"data": data}

    def _wallet_fill_event(self, message: dict) -> WalletFillEvent | None:
        if message.get("type") != "orderTransactionSuccess":
            return None
        fill = message.get("fill") or (message.get("details") or {}).get("fill") or {}
        size_wei = fill.get("executedSizeWei")
        order_id = message.get("orderId")
        if not order_id or size_wei in (None, ""):
            return None
        return WalletFillEvent(
            order_id=str(order_id),
            order_hash=str(message.get("orderHash") or "") or None,
            filled_size=Decimal(str(size_wei)) / Decimal(10**18),
        )

    def _signed_order_to_api_dict(
        self,
        signed_order: object,
        order_hash: str,
    ) -> dict[str, object]:
        if is_dataclass(signed_order):
            raw = asdict(signed_order)
        elif hasattr(signed_order, "model_dump"):
            raw = signed_order.model_dump()
        elif isinstance(signed_order, dict):
            raw = signed_order
        else:
            raw = vars(signed_order)
        return {
            "hash": raw.get("hash") or order_hash,
            "salt": raw["salt"],
            "maker": raw["maker"],
            "signer": raw["signer"],
            "taker": raw["taker"],
            "tokenId": raw.get("token_id") or raw.get("tokenId"),
            "makerAmount": raw.get("maker_amount") or raw.get("makerAmount"),
            "takerAmount": raw.get("taker_amount") or raw.get("takerAmount"),
            "expiration": raw["expiration"],
            "nonce": raw["nonce"],
            "feeRateBps": raw.get("fee_rate_bps") or raw.get("feeRateBps"),
            "side": self._api_value(raw["side"]),
            "signatureType": self._api_value(raw.get("signature_type") or raw.get("signatureType")),
            "signature": raw["signature"],
        }

    def _require_real_order_inputs(self, quote: Quote) -> None:
        self._require_api_key()
        self._require_jwt()
        if not self.settings.private_key:
            raise RuntimeError("PREDICT_PRIVATE_KEY is required for real signed orders.")
        missing = [
            name
            for name, value in {
                "token_id": quote.token_id,
                "fee_rate_bps": quote.fee_rate_bps,
                "is_neg_risk": quote.is_neg_risk,
                "is_yield_bearing": quote.is_yield_bearing,
            }.items()
            if value is None or value == ""
        ]
        if missing:
            raise RuntimeError(
                "Market config is missing fields required by Predict SDK order signing: "
                + ", ".join(missing)
            )

    def _require_api_key(self) -> None:
        if not self.settings.api_key:
            raise RuntimeError("PREDICT_API_KEY is required for Predict.fun mainnet API requests.")

    def _require_jwt(self) -> None:
        if not self.settings.jwt_token:
            raise RuntimeError("PREDICT_JWT_TOKEN is required for wallet-specific Predict.fun requests.")

    def _data(self, response: dict) -> dict:
        data = response.get("data", response)
        return data if isinstance(data, dict) else {"data": data}

    def _rows(self, response: dict) -> list[dict]:
        data = response.get("data", response)
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("positions", "orders", "items"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
        return []

    def _outcome_name(self, outcome: object) -> str:
        if isinstance(outcome, dict):
            return str(
                outcome.get("name")
                or outcome.get("side")
                or outcome.get("outcome")
                or outcome.get("title")
                or ""
            )
        return str(outcome or "")

    def _to_wei(self, value: Decimal) -> int:
        return int(value * Decimal(10**18))

    def _api_value(self, value: object) -> object:
        return getattr(value, "value", value)

    def _optional_int(self, value: object) -> int | None:
        return None if value is None or value == "" else int(value)

    def _optional_bool(self, value: object) -> bool | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes"}

    def _first_present(self, data: dict, *keys: str) -> object:
        for key in keys:
            if key in data:
                return data[key]
        return None

    def _find_outcome_token_id(self, market: dict, outcome_name: str) -> str | None:
        wanted = outcome_name.strip().lower()
        for outcome in market.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            names = {
                str(outcome.get(key, "")).strip().lower()
                for key in ("name", "outcome", "side", "title")
            }
            if wanted and wanted not in names:
                continue
            token_id = self._extract_token_id(outcome)
            if token_id:
                return token_id
        return None

    def _extract_token_id(self, data: object) -> str | None:
        if isinstance(data, dict):
            for key in ("tokenId", "token_id", "onChainId", "on_chain_id"):
                value = data.get(key)
                if value not in (None, ""):
                    return str(value)
            for value in data.values():
                token_id = self._extract_token_id(value)
                if token_id:
                    return token_id
        elif isinstance(data, list):
            for value in data:
                token_id = self._extract_token_id(value)
                if token_id:
                    return token_id
        return None

    def _sample_orderbook(self, market_id: str) -> OrderBook:
        return OrderBook(
            market_id=market_id,
            bids=[Level(Decimal("0.493"), Decimal("100")), Level(Decimal("0.490"), Decimal("80"))],
            asks=[Level(Decimal("0.507"), Decimal("120")), Level(Decimal("0.510"), Decimal("90"))],
        )
