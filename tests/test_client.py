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
