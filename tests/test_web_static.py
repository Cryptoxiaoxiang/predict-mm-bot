from pathlib import Path


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
