from decimal import Decimal

import pytest

from predict_mm.config import depth_protection_config, load_config, update_dotenv_value
from predict_mm.setup_wizard import WizardAnswers, build_config_text


def test_update_dotenv_value_preserves_other_account_settings(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PREDICT_API_KEY=api-key\n"
        "PREDICT_JWT_TOKEN=expired-jwt\n"
        "PREDICT_PRIVATE_KEY=private-key\n"
        "CUSTOM_SETTING=keep-me\n",
        encoding="utf-8",
    )

    update_dotenv_value(env_path, "PREDICT_JWT_TOKEN", "fresh-jwt")

    assert env_path.read_text(encoding="utf-8") == (
        "PREDICT_API_KEY=api-key\n"
        "PREDICT_JWT_TOKEN=fresh-jwt\n"
        "PREDICT_PRIVATE_KEY=private-key\n"
        "CUSTOM_SETTING=keep-me\n"
    )


def test_load_config_reads_optional_run_duration(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "run_duration_seconds = 16200\n"
        "[[markets]]\n"
        'id = "market-1"\n',
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.run_duration_seconds == 4 * 3600 + 30 * 60


def test_load_config_reads_official_outcome_index_set(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[[markets]]\n"
        'id = "total-kills"\n'
        'outcome = "大 55.5"\n'
        "outcome_index_set = 1\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.markets[0].outcome == "大 55.5"
    assert config.markets[0].outcome_index_set == 1


def test_old_config_enables_depth_defaults_and_custom_settings_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[markets]]\nid="1"\n')
    assert load_config(path).depth_protection.enabled is True
    custom = depth_protection_config({"enabled": False, "cancel_min_shares": "250",
                                      "resume_min_shares": "700", "drop_percent": "60",
                                      "stable_seconds": 5, "cooldown_seconds": 20})
    path.write_text(build_config_text(WizardAnswers(market_id="1", depth_protection=custom)))
    assert load_config(path).depth_protection == custom
    assert custom.cancel_min_shares == Decimal(250)


@pytest.mark.parametrize("raw", [
    {"cancel_min_shares": "NaN"}, {"resume_min_shares": "Infinity"},
    {"cancel_size_multiplier": "0"}, {"resume_size_multiplier": "2"},
    {"resume_min_shares": "200"}, {"drop_percent": "101"},
    {"stable_seconds": float("inf")}, {"drop_window_seconds": -1},
    {"cooldown_seconds": 301}, {"enabled": "false"},
])
def test_invalid_depth_settings_are_rejected(raw):
    with pytest.raises(ValueError):
        depth_protection_config(raw)
