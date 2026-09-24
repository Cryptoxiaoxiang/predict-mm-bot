from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request

from predict_mm.desktop import DesktopServer, user_data_directory


def test_windows_data_directory_uses_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert user_data_directory() == tmp_path / "PredictMMBot"


def test_desktop_server_uses_existing_dashboard_and_rejects_foreign_origin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    desktop = DesktopServer()
    desktop.start()
    try:
        with urllib.request.urlopen(f"{desktop.url}/api/status", timeout=5) as response:
            status = json.load(response)
        assert status["configured"] is False
        with urllib.request.urlopen(desktop.url, timeout=5) as response:
            assert "Predict.fun" in response.read().decode("utf-8")

        request = urllib.request.Request(
            f"{desktop.url}/api/start", data=b"{}", method="POST",
            headers={"Origin": "https://another-site.example"},
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError("A foreign site was allowed to start the bot")

        same_origin_request = urllib.request.Request(
            f"{desktop.url}/api/start", data=b"{}", method="POST",
            headers={"Origin": desktop.url},
        )
        try:
            urllib.request.urlopen(same_origin_request, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 400  # No saved market configuration yet.
        else:
            raise AssertionError("Starting without configuration should fail")
    finally:
        desktop.stop()


def test_desktop_close_waits_for_bot_cleanup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    desktop = DesktopServer()
    desktop.start()
    cleaned_up = threading.Event()

    async def install_fake_bot() -> None:
        async def finish_cleanup() -> None:
            await asyncio.sleep(0.15)
            cleaned_up.set()

        async def request_stop() -> None:
            pass

        state = desktop.app.state.dashboard
        state.task = asyncio.create_task(finish_cleanup())
        state.stop = request_stop

    try:
        asyncio.run_coroutine_threadsafe(install_fake_bot(), desktop.loop).result(timeout=5)
        desktop.stop()
        assert cleaned_up.is_set()
    finally:
        if desktop.thread.is_alive():
            desktop.stop()
