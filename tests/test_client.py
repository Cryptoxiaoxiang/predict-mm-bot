from decimal import Decimal
import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

from predict_mm.client import (
    PredictAuthorizationError,
    PredictClient,
    PredictTransientError,
)
from predict_mm.config import Settings
from predict_mm.models import ManagedOrder, OrderStatus, Quote, Side


def test_headers_match_predict_docs() -> None:
    client = PredictClient(Settings(api_key="api-key", jwt_token="jwt"), dry_run=True)

    assert client._headers()["x-api-key"] == "api-key"
    assert client._headers()["Authorization"] == "Bearer jwt"


def test_rest_request_matches_official_requests_usage() -> None:
    client = PredictClient(Settings(api_key="api-key"), dry_run=False)
    response = Mock(status_code=200, content=b'{"data":{"message":"hello"}}')
    response.json.return_value = {"data": {"message": "hello"}}

    with patch("predict_mm.client.requests.request", return_value=response) as request:
        result = client._request_sync(
            "GET",
            "/v1/auth/message",
            query={"empty": "", "limit": 10},
        )

    assert result == {"data": {"message": "hello"}}
    request.assert_called_once_with(
        "GET",
        "https://api.predict.fun/v1/auth/message",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": "api-key",
        },
        params={"limit": 10},
        timeout=10,
    )


def test_rest_request_surfaces_predict_error_message() -> None:
    client = PredictClient(Settings(api_key="api-key"), dry_run=False)
    response = Mock(status_code=403, content=b"blocked")
    response.json.return_value = {
        "error": "The site owner has blocked access based on your browser's signature."
    }

    with patch("predict_mm.client.requests.request", return_value=response):
        try:
            client._request_sync("GET", "/v1/auth/message")
        except RuntimeError as error:
            assert "HTTP 403" in str(error)
            assert "browser's signature" in str(error)
        else:
            raise AssertionError("expected HTTP 403 to be surfaced")


def test_rest_request_classifies_server_error_as_transient() -> None:
    client = PredictClient(Settings(api_key="api-key"), dry_run=False)
    response = Mock(status_code=500, content=b'{"error":"temporary failure"}')
    response.json.return_value = {"error": "temporary failure"}

    with patch("predict_mm.client.requests.request", return_value=response):
        try:
            client._request_sync("GET", "/v1/positions")
        except PredictTransientError as error:
            assert "HTTP 500" in str(error)
        else:
            raise AssertionError("expected HTTP 500 to be classified as transient")


def test_protected_request_retries_transient_server_errors() -> None:
    class StubClient(PredictClient):
        def __init__(self) -> None:
            super().__init__(Settings(api_key="api-key", jwt_token="jwt"), dry_run=False)
            self.request_count = 0

        def _request_sync(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            self.request_count += 1
            if self.request_count < 3:
                raise PredictTransientError("HTTP 500: temporary failure")
            return {"data": []}

    client = StubClient()
    with patch("predict_mm.client.asyncio.sleep", new=AsyncMock()) as sleep:
        result = asyncio.run(client._request("GET", "/v1/orders"))

    assert result == {"data": []}
    assert client.request_count == 3
    assert sleep.await_count == 2


def test_order_creation_does_not_retry_transient_server_error() -> None:
    class StubClient(PredictClient):
        def __init__(self) -> None:
            super().__init__(Settings(api_key="api-key", jwt_token="jwt"), dry_run=False)
            self.request_count = 0

        def _request_sync(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            self.request_count += 1
            raise PredictTransientError("HTTP 500: failed to create order")

    client = StubClient()
    try:
        asyncio.run(client._request("POST", "/v1/orders", {"data": {}}))
    except PredictTransientError:
        pass
    else:
        raise AssertionError("expected order creation failure to surface without retrying")

    assert client.request_count == 1


def test_protected_request_refreshes_expired_jwt_and_retries_once() -> None:
    refreshed_tokens: list[str] = []

    class StubClient(PredictClient):
        def __init__(self) -> None:
            super().__init__(
                Settings(
                    api_key="api-key",
                    jwt_token="expired-jwt",
                    private_key="private-key",
                    predict_account_address="0xpredict-account",
                ),
                dry_run=False,
                jwt_token_updated=refreshed_tokens.append,
            )
            self.request_count = 0

        def _request_sync(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            self.request_count += 1
            if self.request_count == 1:
                raise PredictAuthorizationError("HTTP 401: authorization error")
            assert self.settings.jwt_token == "fresh-jwt"
            return {"data": []}

        async def create_predict_account_jwt(self, private_key, predict_account_address):  # type: ignore[no-untyped-def]
            assert private_key == "private-key"
            assert predict_account_address == "0xpredict-account"
            return "fresh-jwt"

    client = StubClient()
    with patch.dict("os.environ", {}, clear=False):
        result = asyncio.run(client._request("GET", "/v1/orders"))
        assert client.settings.jwt_token == "fresh-jwt"

    assert result == {"data": []}
    assert client.request_count == 2
    assert refreshed_tokens == ["fresh-jwt"]


def test_protected_request_does_not_loop_after_refreshed_jwt_is_rejected() -> None:
    class StubClient(PredictClient):
        def __init__(self) -> None:
            super().__init__(
                Settings(
                    api_key="api-key",
                    jwt_token="expired-jwt",
                    private_key="private-key",
                ),
                dry_run=False,
            )
            self.request_count = 0
            self.refresh_count = 0

        def _request_sync(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            self.request_count += 1
            raise PredictAuthorizationError("HTTP 401: authorization error")

        async def create_eoa_jwt(self, private_key):  # type: ignore[no-untyped-def]
            self.refresh_count += 1
            return "fresh-jwt"

    client = StubClient()
    try:
        asyncio.run(client._request("GET", "/v1/orders"))
    except PredictAuthorizationError:
        pass
    else:
        raise AssertionError("expected the retried request to surface the second 401")

    assert client.request_count == 2
    assert client.refresh_count == 1


def test_balance_requires_saved_private_key() -> None:
    client = PredictClient(Settings(), dry_run=False)

    try:
        asyncio.run(client.get_usdt_balance())
    except RuntimeError as error:
        assert "Private Key" in str(error)
    else:
        raise AssertionError("expected balance lookup without a private key to fail")


def test_dry_run_log_describes_passive_yes_sell_as_no_buy(caplog) -> None:
    client = PredictClient(Settings(), dry_run=True)
    quote = Quote(
        market_id="749916",
        side=Side.SELL,
        price=Decimal("0.509"),
        size=Decimal("1.0"),
        outcome="Yes",
    )

    with caplog.at_level("INFO", logger="predict-mm"):
        asyncio.run(client.create_order(quote))

    assert "DRY-RUN create buy 1.0 No @ 0.509 on 749916" in caplog.text


def test_dry_run_log_keeps_emergency_exit_as_sell(caplog) -> None:
    client = PredictClient(Settings(), dry_run=True)
    quote = Quote(
        market_id="749916",
        side=Side.SELL,
        price=Decimal("0.01"),
        size=Decimal("1.0"),
        outcome="Yes",
    )

    with caplog.at_level("INFO", logger="predict-mm"):
        asyncio.run(client.create_order(quote, post_only=False))

    assert "DRY-RUN create emergency sell 1.0 Yes @ 0.01 on 749916" in caplog.text


def test_parse_orderbook_unwraps_predict_data() -> None:
    client = PredictClient(Settings(), dry_run=True)

    book = client._parse_orderbook(
        "1",
        {
            "marketId": 1,
            "bids": [["0.49", "10"], ["0.48", "5"]],
            "asks": [["0.51", "11"], ["0.52", "6"]],
        },
    )

    assert book.best_bid is not None
    assert book.best_bid.price == Decimal("0.49")
    assert book.best_ask is not None
    assert book.best_ask.price == Decimal("0.51")


def test_market_decimal_precision_sets_tick_size() -> None:
    client = PredictClient(Settings(), dry_run=True)

    assert client._tick_size_from_market({"decimalPrecision": 2}, "market-1") == Decimal("0.01")
    assert client._tick_size_from_market({"decimalPrecision": 3}, "market-1") == Decimal("0.001")


def test_orderbook_uses_cached_market_precision() -> None:
    class StubClient(PredictClient):
        def __init__(self) -> None:
            super().__init__(Settings(api_key="api-key"), dry_run=False)
            self.paths: list[str] = []

        async def _request(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            self.paths.append(path)
            if path == "/v1/markets/1":
                return {"data": {"decimalPrecision": 2}}
            return {"data": {"bids": [["0.50", "10"]], "asks": [["0.55", "10"]]}}

    client = StubClient()
    first = asyncio.run(client.get_orderbook("1"))
    second = asyncio.run(client.get_orderbook("1"))

    assert first.tick_size == Decimal("0.01")
    assert second.tick_size == Decimal("0.01")
    assert client.paths.count("/v1/markets/1") == 1


def test_market_search_uses_official_search_endpoint() -> None:
    class StubClient(PredictClient):
        async def _request(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            assert method == "GET"
            assert path == "/v1/search"
            assert query == {"query": "bitcoin", "limit": 10}
            return {"data": {"markets": [{"id": 123, "question": "Bitcoin?"}]}}

    client = StubClient(Settings(api_key="api-key"), dry_run=False)
    markets = asyncio.run(client.search_markets("bitcoin"))

    assert markets == [{"id": 123, "question": "Bitcoin?"}]


def test_market_search_includes_markets_nested_under_categories() -> None:
    class StubClient(PredictClient):
        async def _request(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            return {
                "data": {
                    "markets": [{"id": 1, "title": "Direct"}],
                    "categories": [
                        {
                            "slug": "fra-esp",
                            "title": "France vs. Spain",
                            "markets": [
                                {
                                    "id": 2,
                                    "title": "Match winner",
                                    "outcomes": [{"name": "FRA"}, {"name": "Draw"}, {"name": "ESP"}],
                                }
                            ],
                        }
                    ],
                }
            }

    client = StubClient(Settings(api_key="api-key"), dry_run=False)
    markets = asyncio.run(client.search_markets("fra-esp"))

    assert [market["id"] for market in markets] == [1, 2]
    assert markets[1]["categoryTitle"] == "France vs. Spain"


def test_public_page_category_payload_is_converted_to_market_choices() -> None:
    hydrated_data = {
        "state": {
            "data": {
                "id": "749900",
                "title": "FRA",
                "category": {
                    "id": "fifwc-fra-esp-2026-07-14",
                    "title": "France vs. Spain",
                },
                "outcomes": {"edges": [{"node": {"name": "Yes"}}, {"node": {"name": "No"}}]},
            }
        }
    }
    client = PredictClient(Settings(), dry_run=True)

    markets = client._markets_from_next_query_payload(
        "48:" + json.dumps([hydrated_data]),
        "fifwc-fra-esp-2026-07-14",
        json.JSONDecoder(),
    )

    assert markets[0]["id"] == "749900"
    assert markets[0]["title"] == "FRA"
    assert markets[0]["categoryTitle"] == "France vs. Spain"


def test_eoa_jwt_signs_the_dynamic_message_locally() -> None:
    class StubClient(PredictClient):
        def __init__(self) -> None:
            super().__init__(Settings(api_key="api-key"), dry_run=False)
            self.auth_payload: dict | None = None

        async def _request(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            if path == "/v1/auth/message":
                assert method == "GET"
                return {"data": {"message": "Sign this one-time message"}}
            assert method == "POST"
            assert path == "/v1/auth"
            self.auth_payload = payload
            return {"data": {"token": "generated-jwt"}}

    client = StubClient()
    token = asyncio.run(client.create_eoa_jwt("0x" + "1" * 64))

    assert token == "generated-jwt"
    assert client.auth_payload is not None
    assert client.auth_payload["signer"] == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
    assert client.auth_payload["signature"].startswith("0x")
    assert client.auth_payload["message"] == "Sign this one-time message"


def test_real_order_requires_sdk_inputs() -> None:
    client = PredictClient(
        Settings(api_key="api-key", jwt_token="jwt", private_key="0xabc"),
        dry_run=False,
    )
    quote = Quote(
        market_id="1",
        side=Side.BUY,
        price=Decimal("0.5"),
        size=Decimal("1"),
    )

    try:
        client._build_signed_limit_order_payload(quote)
    except RuntimeError as error:
        assert "token_id" in str(error)
    else:
        raise AssertionError("expected missing token_id to block real order creation")


def test_signed_order_keeps_zero_valued_signature_type() -> None:
    client = PredictClient(Settings(), dry_run=False)

    payload = client._signed_order_to_api_dict(
        {
            "salt": "1",
            "maker": "0xmaker",
            "signer": "0xsigner",
            "taker": "0xtaker",
            "token_id": "123",
            "maker_amount": "24900000000000000000",
            "taker_amount": "100000000000000000000",
            "expiration": "4102444800",
            "nonce": "0",
            "fee_rate_bps": "0",
            "side": 0,
            "signature_type": 0,
            "signature": "0xsigned",
        },
        "0xhash",
    )

    assert payload["side"] == 0
    assert payload["signatureType"] == 0
    assert payload["feeRateBps"] == "0"


def test_limit_order_uses_wei_price_in_rest_payload() -> None:
    client = PredictClient(
        Settings(
            api_key="api-key",
            jwt_token="jwt",
            private_key="0x" + "1" * 64,
        ),
        dry_run=False,
    )
    quote = Quote(
        market_id="631321",
        side=Side.BUY,
        price=Decimal("0.249"),
        size=Decimal("100"),
        outcome="Yes",
        token_id="123",
        fee_rate_bps=200,
        is_neg_risk=False,
        is_yield_bearing=True,
    )

    payload = client._build_signed_limit_order_payload(quote)

    assert payload["data"]["pricePerShare"] == "249000000000000000"
    assert payload["data"]["strategy"] == "LIMIT"
    assert payload["data"]["isPostOnly"] is True
    assert "reservedBalancePolicy" not in payload["data"]
    assert payload["data"]["order"]["makerAmount"] == "24900000000000000000"
    assert payload["data"]["order"]["takerAmount"] == "100000000000000000000"


def test_quote_metadata_can_be_completed_from_market_response() -> None:
    class StubClient(PredictClient):
        async def _request(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            assert method == "GET"
            assert path == "/v1/markets/1"
            return {
                "success": True,
                "data": {
                    "id": 1,
                    "feeRateBps": 12,
                    "isNegRisk": False,
                    "isYieldBearing": True,
                    "outcomes": [{"name": "YES", "tokenId": "token-yes"}],
                },
            }

    client = StubClient(Settings(api_key="api-key", jwt_token="jwt"), dry_run=False)
    quote = Quote(
        market_id="1",
        side=Side.BUY,
        price=Decimal("0.5"),
        size=Decimal("1"),
        outcome="YES",
    )

    completed = asyncio.run(client._complete_quote_with_market_metadata(quote))

    assert completed.token_id == "token-yes"
    assert completed.fee_rate_bps == 12
    assert completed.is_neg_risk is False
    assert completed.is_yield_bearing is True


def test_positions_convert_wei_to_share_quantities() -> None:
    class StubClient(PredictClient):
        async def _request(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            assert (method, path) == ("GET", "/v1/positions")
            return {
                "data": {
                    "positions": [
                        {
                            "amount": "100000000000000000000",
                            "market": {"id": "market-yes"},
                            "outcome": {"name": "YES"},
                        },
                        {
                            "amount": "25000000000000000000",
                            "market": {"id": "market-no"},
                            "outcome": {"name": "NO"},
                        },
                    ]
                }
            }

    client = StubClient(Settings(api_key="api-key", jwt_token="jwt"), dry_run=False)

    assert asyncio.run(client.get_positions()) == {
        "market-yes": Decimal("100"),
        "market-no": Decimal("-25"),
    }


def test_wallet_fill_event_uses_confirmed_fill_size() -> None:
    client = PredictClient(Settings(), dry_run=True)

    event = client._wallet_fill_event(
        {
            "type": "orderTransactionSuccess",
            "orderId": "order-1",
            "orderHash": "hash-1",
            "settlementId": "settlement-1",
            "fill": {"executedSizeWei": "2500000000000000000"},
        }
    )

    assert event is not None
    assert event.order_id == "order-1"
    assert event.order_hash == "hash-1"
    assert event.filled_size == Decimal("2.5")
    assert event.settlement_id == "settlement-1"


def test_wallet_fill_event_accepts_match_submission() -> None:
    client = PredictClient(Settings(), dry_run=True)

    event = client._wallet_fill_event(
        {
            "type": "orderTransactionSubmitted",
            "orderId": "order-1",
            "settlementId": "settlement-1",
            "fill": {"executedSizeWei": "1000000000000000000"},
        }
    )

    assert event is not None
    assert event.event_type == "orderTransactionSubmitted"


def test_wallet_fill_event_unwraps_official_websocket_message_envelope() -> None:
    client = PredictClient(Settings(), dry_run=True)
    message = {
        "type": "M",
        "topic": "predictWalletEvents/jwt-token",
        "data": {
            "type": "orderTransactionSuccess",
            "orderId": "order-1",
            "settlementId": "settlement-1",
            "fill": {"executedSizeWei": "3000000000000000000"},
        },
    }

    payload = client._wallet_event_payload(message)
    assert payload is not None
    event = client._wallet_fill_event(payload)

    assert event is not None
    assert event.order_id == "order-1"
    assert event.filled_size == Decimal("3")


def test_wallet_event_parses_order_rejection_reason() -> None:
    client = PredictClient(Settings(), dry_run=True)

    event = client._wallet_event(
        {
            "type": "orderNotAccepted",
            "orderId": "order-1",
            "orderHash": "hash-1",
            "reason": "rejectedPostOnly",
        }
    )

    assert event is not None
    assert event.order_id == "order-1"
    assert event.order_hash == "hash-1"
    assert event.event_type == "orderNotAccepted"
    assert event.reason == "rejectedPostOnly"


def test_wallet_stream_answers_official_heartbeat_and_yields_enveloped_fill() -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.messages = iter(
                [
                    {"type": "R", "requestId": 1, "success": True},
                    {"type": "M", "topic": "heartbeat", "data": 1736696400000},
                    {
                        "type": "M",
                        "topic": "predictWalletEvents/jwt",
                        "data": {
                            "type": "orderTransactionSubmitted",
                            "orderId": "order-1",
                            "settlementId": "settlement-1",
                            "fill": {"executedSizeWei": "1000000000000000000"},
                        },
                    },
                ]
            )

        async def send(self, raw_message: str) -> None:
            self.sent.append(json.loads(raw_message))

        def __aiter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __anext__(self):  # type: ignore[no-untyped-def]
            try:
                return json.dumps(next(self.messages))
            except StopIteration as error:
                raise StopAsyncIteration from error

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, *_args) -> None:  # type: ignore[no-untyped-def]
            return None

    async def collect_events(client: PredictClient) -> list:  # type: ignore[type-arg]
        events = []
        async for event in client.stream_wallet_fill_events():
            assert client.wallet_stream_connected is True
            events.append(event)
        return events

    websocket = FakeWebSocket()
    client = PredictClient(Settings(api_key="api-key", jwt_token="jwt"), dry_run=False)
    with patch(
        "websockets.asyncio.client.connect",
        return_value=FakeConnection(websocket),
    ):
        events = asyncio.run(collect_events(client))

    assert websocket.sent == [
        {
            "method": "subscribe",
            "requestId": 1,
            "params": ["predictWalletEvents/jwt"],
        },
        {"method": "heartbeat", "data": 1736696400000},
    ]
    assert len(events) == 1
    assert events[0].order_id == "order-1"
    assert events[0].filled_size == Decimal("1")
    assert client.wallet_stream_connected is False


def test_get_order_filled_amounts_maps_id_and_hash() -> None:
    class StubClient(PredictClient):
        async def _request(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            assert (method, path, query) == ("GET", "/v1/orders", {"first": 100})
            return {
                "data": [
                    {
                        "id": "order-1",
                        "amountFilled": "2500000000000000000",
                        "order": {"hash": "hash-1"},
                    }
                ]
            }

    client = StubClient(Settings(api_key="api-key", jwt_token="jwt"), dry_run=False)

    assert asyncio.run(client.get_order_filled_amounts()) == {
        "order-1": Decimal("2.5"),
        "hash-1": Decimal("2.5"),
    }


def test_get_order_filled_amounts_follows_every_cursor_page() -> None:
    class StubClient(PredictClient):
        def __init__(self) -> None:
            super().__init__(
                Settings(api_key="api-key", jwt_token="jwt"), dry_run=False
            )
            self.queries: list[dict[str, object]] = []

        async def _request(self, method, path, payload=None, query=None):  # type: ignore[no-untyped-def]
            self.queries.append(dict(query or {}))
            if query and query.get("after") == "next-page":
                return {
                    "data": [
                        {
                            "id": "order-2",
                            "orderHash": "hash-2",
                            "amountFilled": "2000000000000000000",
                        }
                    ]
                }
            return {
                "data": [
                    {
                        "id": "order-1",
                        "orderHash": "hash-1",
                        "amountFilled": "1000000000000000000",
                    }
                ],
                "cursor": "next-page",
            }

    client = StubClient()
    fills = asyncio.run(client.get_order_filled_amounts())

    assert fills["order-1"] == Decimal("1")
    assert fills["order-2"] == Decimal("2")
    assert client.queries == [
        {"first": 100},
        {"first": 100, "after": "next-page"},
    ]


def test_order_safety_journal_restores_removed_buy_order(tmp_path) -> None:
    journal = tmp_path / "orders.json"
    client = PredictClient(
        Settings(order_journal_path=str(journal)), dry_run=False
    )
    order = ManagedOrder(
        order_id="late-fill-order",
        order_hash="0xhash",
        quote=Quote(
            market_id="market-1",
            side=Side.BUY,
            price=Decimal("0.42"),
            size=Decimal("3"),
            outcome="No",
            token_id="123",
            fee_rate_bps=0,
            is_neg_risk=False,
            is_yield_bearing=True,
        ),
        created_at=0,
        status=OrderStatus.CANCELED,
        filled_size=Decimal("1"),
    )

    client.persist_tracked_order(order)
    restored = client.load_tracked_orders()

    assert len(restored) == 1
    assert restored[0].order_id == "late-fill-order"
    assert restored[0].order_hash == "0xhash"
    assert restored[0].status == OrderStatus.CANCELED
    assert restored[0].filled_size == Decimal("1")
    assert restored[0].quote.outcome == "No"
    assert journal.stat().st_mode & 0o777 == 0o600
