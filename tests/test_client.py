from decimal import Decimal
import asyncio

from predict_mm.client import PredictClient
from predict_mm.config import Settings
from predict_mm.models import Quote, Side


def test_headers_match_predict_docs() -> None:
    client = PredictClient(Settings(api_key="api-key", jwt_token="jwt"), dry_run=True)

    assert client._headers()["x-api-key"] == "api-key"
    assert client._headers()["Authorization"] == "Bearer jwt"


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


def test_wallet_fill_event_uses_confirmed_fill_size() -> None:
    client = PredictClient(Settings(), dry_run=True)

    event = client._wallet_fill_event(
        {
            "type": "orderTransactionSuccess",
            "orderId": "order-1",
            "orderHash": "hash-1",
            "fill": {"executedSizeWei": "2500000000000000000"},
        }
    )

    assert event is not None
    assert event.order_id == "order-1"
    assert event.order_hash == "hash-1"
    assert event.filled_size == Decimal("2.5")
