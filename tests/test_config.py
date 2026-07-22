from predict_mm.config import load_config, update_dotenv_value


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
