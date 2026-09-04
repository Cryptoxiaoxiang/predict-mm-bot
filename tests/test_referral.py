import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from predict_mm.client import PredictClient
from predict_mm.config import Settings
from predict_mm.web import AccountPayload, create_app


def test_referral_uses_official_payload(monkeypatch):
    client = PredictClient(Settings(api_key="test-key", jwt_token="test-jwt"), dry_run=False)
    request = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(client, "_request", request)
    asyncio.run(client.set_referral("5BA3F"))
    request.assert_awaited_once_with(
        "POST", "/v1/account/referral", {"data": {"referralCode": "5BA3F"}}
    )


@pytest.mark.parametrize("failure", [None, RuntimeError("already bound"), OSError("offline")])
@pytest.mark.parametrize("predict_account", ["", "0x-test-predict-account"])
def test_first_account_save_referral_is_silent_and_optional(
    tmp_path, monkeypatch, caplog, failure, predict_account
):
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: cls()))
    monkeypatch.setattr("predict_mm.web._apply_settings_to_process", lambda answers: None)
    monkeypatch.setattr(PredictClient, "create_eoa_jwt", AsyncMock(return_value="new-jwt"))
    monkeypatch.setattr(
        PredictClient, "create_predict_account_jwt", AsyncMock(return_value="new-jwt")
    )
    calls = []

    async def referral(client, code):
        assert client.settings.jwt_token == "new-jwt"
        assert client.settings.private_key is None
        assert (tmp_path / ".env").exists()
        calls.append(code)
        if failure:
            raise failure

    monkeypatch.setattr(PredictClient, "set_referral", referral)
    app = create_app(tmp_path / "config.toml", tmp_path / ".env")
    endpoint = next(r.endpoint for r in app.routes if r.path == "/api/account")
    result = asyncio.run(endpoint(AccountPayload(
        api_key="test-key", private_key="test-private", predict_account_address=predict_account,
    )))
    assert result["ok"] is True
    assert "账户设置已保存" in result["message"]
    assert calls == ["5BA3F"]
    assert "邀请码" not in result["message"]
    assert "referral" not in (tmp_path / ".env").read_text().lower()
    assert caplog.text == ""


def test_resaving_same_account_does_not_rebind(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: cls(
        api_key="test-key", private_key="test-private", jwt_token="old-jwt",
    )))
    monkeypatch.setattr("predict_mm.web._apply_settings_to_process", lambda answers: None)
    monkeypatch.setattr(PredictClient, "create_eoa_jwt", AsyncMock(return_value="new-jwt"))
    referral = AsyncMock()
    monkeypatch.setattr(PredictClient, "set_referral", referral)
    app = create_app(tmp_path / "config.toml", tmp_path / ".env")
    endpoint = next(r.endpoint for r in app.routes if r.path == "/api/account")
    assert asyncio.run(endpoint(AccountPayload()))["ok"]
    referral.assert_not_awaited()


def test_referral_disclosure_precedes_save_button():
    html = (Path(__file__).parents[1] / "predict_mm/web_static/index.html").read_text()
    assert html.index("保存时将自动用邀请码。") < html.index("保存账户设置</button>")
