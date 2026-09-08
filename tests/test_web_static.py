from pathlib import Path


def test_depth_controls_save_nested_settings_and_load_status():
    root = Path(__file__).parents[1] / "predict_mm" / "web_static"
    html, js = (root / "index.html").read_text(), (root / "app.js").read_text()
    for field in ("enabled", "cancel_min_shares", "cancel_size_multiplier", "resume_min_shares",
                  "resume_size_multiplier", "drop_window_seconds", "drop_percent",
                  "stable_seconds", "cooldown_seconds"):
        assert f'name="depth_{field}"' in html
    assert "values.depth_protection[key.slice(6)] = value" in js
    assert "status.depth_protection" in js


def test_dashboard_links_to_owner_x_profile() -> None:
    html = (
        Path(__file__).parents[1] / "predict_mm" / "web_static" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'href="https://x.com/cryptoxiaoxiang"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


def test_log_panels_pause_auto_refresh_during_copying() -> None:
    static_dir = Path(__file__).parents[1] / "predict_mm" / "web_static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")

    assert 'id="logs" class="full-log" tabindex="0"' in html
    assert 'id="dashboard-logs" class="log-preview" tabindex="0"' in html
    assert "selectionIsInsideLogs" in javascript
    assert "logInteractionPaused" in javascript
    assert "点击日志外恢复" in javascript


def test_run_duration_controls_and_dashboard_countdown_are_present() -> None:
    static_dir = Path(__file__).parents[1] / "predict_mm" / "web_static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")

    assert 'name="run_duration_enabled"' in html
    assert 'name="run_duration_hours"' in html
    assert 'name="run_duration_minutes"' in html
    assert 'id="expiry-value"' in html
    assert "updateDurationCountdown" in javascript
    assert "run_expires_at" in javascript
    assert "runDurationEnabled.checked = true" in javascript
    assert "runDurationHours.disabled" not in javascript
    assert "市场 tick 为 0.001 时使用 0.001" in html


def test_structured_api_errors_are_rendered_as_readable_messages() -> None:
    javascript = (
        Path(__file__).parents[1] / "predict_mm" / "web_static" / "app.js"
    ).read_text(encoding="utf-8")

    assert "formatErrorDetail(data.detail)" in javascript
    assert "function formatValidationItem" in javascript
    assert "市场网址 / Market ID" in javascript
    assert "new Error(data.detail" not in javascript


def test_open_orders_show_order_age_instead_of_status() -> None:
    static_dir = Path(__file__).parents[1] / "predict_mm" / "web_static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")

    assert "<th>挂单时间</th>" in html
    assert "<th>状态</th>" not in html
    assert "function formatOrderAge" in javascript
    assert "formatOrderAge(order.age_seconds)" in javascript


def test_new_market_quote_size_defaults_to_one_hundred() -> None:
    static_dir = Path(__file__).parents[1] / "predict_mm" / "web_static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")

    assert 'data-field="quote_size" inputmode="decimal" value="100"' in html
    assert "market.quote_size || '100'" in javascript


def test_selected_market_summary_shows_market_id_on_its_own_line() -> None:
    static_dir = Path(__file__).parents[1] / "predict_mm" / "web_static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")
    stylesheet = (static_dir / "styles.css").read_text(encoding="utf-8")

    assert "identifier.textContent = `Market ID：${marketId}`" in javascript
    assert "summary.replaceChildren(selection, identifier)" in javascript
    assert ".selected-market-id { display: block;" in stylesheet
    assert '/static/app.js?v=' in html
    assert '/static/styles.css?v=' in html
