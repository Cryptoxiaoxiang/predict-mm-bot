from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from collections import deque
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from predict_mm.client import PredictClient
from predict_mm.config import Settings, load_config
from predict_mm.engine import MarketMakerEngine
from predict_mm.logging import configure_logging
from predict_mm.risk import RiskManager
from predict_mm.setup_wizard import MarketAnswers, WizardAnswers, build_config_text, build_env_text
from predict_mm.strategy import PassiveMakerStrategy

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web_static"


class MarketPayload(BaseModel):
    market_id: str = Field(min_length=1, max_length=200)
    outcome: Literal["YES", "NO"] = "YES"
    quote_size: str = "1.0"


class SetupPayload(BaseModel):
    dry_run: bool = False
    emergency_exit_on_buy_fill: bool = True
    markets: list[MarketPayload] = Field(min_length=1, max_length=50)
    cancel_after_seconds: str = "8"
    max_position_per_market: str = "10.0"
    max_total_position: str = "50.0"


class AccountPayload(BaseModel):
    api_key: str = ""
    jwt_token: str = ""
    private_key: str = ""
    predict_account_address: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class MarketUrlPayload(BaseModel):
    market_url: str = Field(min_length=1, max_length=1000)


class MemoryLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=300)
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class DashboardState:
    def __init__(self, config_path: Path, env_path: Path) -> None:
        self.config_path = config_path
        self.env_path = env_path
        self.engine: MarketMakerEngine | None = None
        self.task: asyncio.Task[None] | None = None
        self.last_error: str | None = None
        self.log_handler = MemoryLogHandler()
        self.logging_ready = False
        self.lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def configured(self) -> bool:
        if not self.config_path.exists() or self.config_path.stat().st_size == 0:
            return False
        try:
            load_config(self.config_path)
        except (OSError, ValueError):
            return False
        return True

    def overview(self) -> dict[str, object]:
        config = None
        if self.configured():
            config = load_config(self.config_path)
        settings = Settings.from_env()
        markets = config.enabled_markets if config else []
        return {
            "configured": self.configured(),
            "running": self.running,
            "last_error": self.last_error,
            "dry_run": config.dry_run if config else False,
            "markets": [
                {
                    "market_id": market.id,
                    "outcome": market.outcome,
                    "quote_size": str(market.quote_size or config.strategy.quote_size),
                }
                for market in markets
            ],
            "max_position_per_market": str(config.risk.max_position_per_market) if config else "10.0",
            "max_total_position": str(config.risk.max_total_position) if config else "50.0",
            "cancel_after_seconds": config.cancel_after_seconds if config else 8,
            "emergency_exit_on_buy_fill": config.emergency_exit_on_buy_fill if config else True,
            "open_order_markets": self.engine.active_order_markets() if self.engine else [],
            "api_key_set": bool(settings.api_key),
            "jwt_token_set": bool(settings.jwt_token),
            "private_key_set": bool(settings.private_key),
            "account_address": settings.predict_account_address or "",
            "log_level": settings.log_level,
        }

    async def start(self) -> None:
        async with self.lock:
            if self.running:
                return
            if not self.configured():
                raise ValueError("请先完成网页配置。")
            settings = Settings.from_env()
            config = load_config(self.config_path)
            if not self.logging_ready:
                configure_logging(settings.log_level)
                logging.getLogger().addHandler(self.log_handler)
                self.logging_ready = True
            client = PredictClient(settings=settings, dry_run=config.dry_run)
            self.engine = MarketMakerEngine(
                config=config,
                client=client,
                strategy=PassiveMakerStrategy(config.strategy),
                risk=RiskManager(config.risk),
            )
            self.last_error = None
            self.task = asyncio.create_task(self._run_engine(self.engine))

    async def _run_engine(self, engine: MarketMakerEngine) -> None:
        try:
            await engine.run()
        except Exception as error:  # noqa: BLE001
            self.last_error = str(error)
            logging.getLogger("predict-mm").exception("机器人已停止：%s", error)
        finally:
            self.engine = None

    async def stop(self) -> None:
        async with self.lock:
            if self.engine is not None:
                self.engine.request_stop()

    async def cancel_all(self) -> None:
        if not self.configured():
            raise ValueError("请先完成网页配置。")
        if self.engine is not None:
            await self.engine.cancel_all_orders()
            return
        config = load_config(self.config_path)
        settings = Settings.from_env()
        client = PredictClient(settings=settings, dry_run=config.dry_run)
        try:
            for market in config.enabled_markets:
                await client.cancel_all_orders(market.id)
        finally:
            await client.close()


def create_app(config_path: str | Path = "config.toml", env_path: str | Path = ".env") -> FastAPI:
    state = DashboardState(Path(config_path), Path(env_path))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await state.stop()

    app = FastAPI(title="Predict.fun 自动挂单机器人", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        return state.overview()

    @app.get("/api/logs")
    async def logs() -> dict[str, list[str]]:
        return {"lines": list(state.log_handler.lines)}

    @app.post("/api/setup")
    async def setup(payload: SetupPayload) -> dict[str, object]:
        if state.running:
            raise HTTPException(status_code=409, detail="请先停止机器人，再修改配置。")
        _validate_setup(payload)
        current = Settings.from_env()
        market_answers = [
            MarketAnswers(
                market_id=market.market_id.strip(),
                outcome=market.outcome,
                quote_size=market.quote_size.strip(),
            )
            for market in payload.markets
        ]
        max_quote_size = max(Decimal(market.quote_size) for market in market_answers)
        answers = WizardAnswers(
            api_base_url=current.api_base_url,
            api_key=current.api_key or "",
            jwt_token=current.jwt_token or "",
            private_key=current.private_key or "",
            predict_account_address=current.predict_account_address or "",
            log_level=current.log_level,
            dry_run=payload.dry_run,
            emergency_exit_on_buy_fill=payload.emergency_exit_on_buy_fill,
            market_id=market_answers[0].market_id,
            outcome=market_answers[0].outcome,
            quote_size=str(max_quote_size),
            cancel_after_seconds=payload.cancel_after_seconds.strip(),
            max_position_per_market=payload.max_position_per_market.strip(),
            max_total_position=payload.max_total_position.strip(),
        )
        state.config_path.write_text(build_config_text(answers, markets=market_answers), encoding="utf-8")
        return {"ok": True, "message": "市场与风控配置已保存。"}

    @app.post("/api/account")
    async def account(payload: AccountPayload) -> dict[str, object]:
        current = Settings.from_env()
        answers = WizardAnswers(
            api_base_url=current.api_base_url,
            api_key=payload.api_key.strip() or current.api_key or "",
            jwt_token=payload.jwt_token.strip() or current.jwt_token or "",
            private_key=payload.private_key.strip() or current.private_key or "",
            predict_account_address=payload.predict_account_address.strip()
            or current.predict_account_address
            or "",
            log_level=payload.log_level,
        )
        state.env_path.write_text(build_env_text(answers), encoding="utf-8")
        _apply_settings_to_process(answers)
        message = "账户设置已保存。"
        if state.running:
            message += " 机器人会在下次启动时使用新的账户设置。"
        return {"ok": True, "message": message}

    @app.post("/api/resolve-market")
    async def resolve_market(payload: MarketUrlPayload) -> dict[str, object]:
        try:
            slug = _market_slug_from_url(payload.market_url)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        settings = Settings.from_env()
        if not settings.api_key:
            raise HTTPException(
                status_code=400,
                detail="请先在“账户设置”保存 API Key，才能从市场网址识别 Market ID。",
            )

        client = PredictClient(settings=settings, dry_run=False)
        try:
            markets = await client.search_markets(_search_query_from_slug(slug))
            if not markets:
                markets = await client.search_markets(slug)
        except Exception as error:  # noqa: BLE001
            logging.getLogger("predict-mm").warning("无法从市场网址识别 ID: %s", error)
            raise HTTPException(
                status_code=502,
                detail="未能查询 Predict.fun 市场。请检查 API Key、网络后重试，或直接填写数字 Market ID。",
            ) from error
        finally:
            await client.close()

        matches = [_market_lookup_result(market) for market in markets]
        if len(matches) == 1:
            return {
                "ok": True,
                "market_id": matches[0]["id"],
                "matches": matches,
                "message": f"已识别 Market ID：{matches[0]['id']}。",
            }
        return {
            "ok": True,
            "market_id": None,
            "matches": matches,
            "message": "请选择与网页题目完全一致的市场。" if matches else "未找到匹配市场，请尝试直接填写数字 Market ID。",
        }

    @app.post("/api/start")
    async def start() -> dict[str, object]:
        try:
            await state.start()
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "message": "机器人正在启动。"}

    @app.post("/api/stop")
    async def stop() -> dict[str, object]:
        await state.stop()
        return {"ok": True, "message": "已请求停止机器人，并将执行撤单。"}

    @app.post("/api/cancel-all")
    async def cancel_all() -> dict[str, object]:
        try:
            await state.cancel_all()
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "message": "已请求撤销所有已配置市场的订单。"}

    return app


def _validate_setup(payload: SetupPayload) -> None:
    for market in payload.markets:
        if "predict.fun/market/" in market.market_id.lower():
            raise HTTPException(
                status_code=422,
                detail="请先点击“识别网址”并选择正确市场，再保存配置。",
            )
    try:
        for value in (
            payload.cancel_after_seconds,
            payload.max_position_per_market,
            payload.max_total_position,
        ):
            if Decimal(value) <= 0:
                raise ValueError
        for market in payload.markets:
            if not market.market_id.strip() or Decimal(market.quote_size) <= 0:
                raise ValueError
    except (InvalidOperation, ValueError) as error:
        raise HTTPException(status_code=422, detail="数量必须是有效的正数。") from error


def _market_slug_from_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if host not in {"predict.fun", "www.predict.fun"}:
        raise ValueError("请粘贴 predict.fun 的市场网址，例如 https://predict.fun/market/xxx")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        position = parts.index("market")
        slug = parts[position + 1].strip()
    except (ValueError, IndexError) as error:
        raise ValueError("网址中没有找到市场路径，请粘贴完整的 /market/… 链接。") from error
    if not slug:
        raise ValueError("网址中没有找到市场名称。")
    return slug


def _search_query_from_slug(slug: str) -> str:
    without_timestamp = re.sub(r"[-_]\d{8,}$", "", slug)
    query = re.sub(r"[-_]+", " ", without_timestamp).strip()
    return query or slug


def _market_lookup_result(market: dict) -> dict[str, object]:
    return {
        "id": str(market.get("id", "")),
        "title": str(market.get("title") or ""),
        "question": str(market.get("question") or market.get("title") or ""),
        "trading_status": str(market.get("tradingStatus") or ""),
    }


def _apply_settings_to_process(answers: WizardAnswers) -> None:
    for key, value in {
        "PREDICT_API_BASE_URL": answers.api_base_url,
        "PREDICT_API_KEY": answers.api_key,
        "PREDICT_JWT_TOKEN": answers.jwt_token,
        "PREDICT_PRIVATE_KEY": answers.private_key,
        "PREDICT_ACCOUNT_ADDRESS": answers.predict_account_address,
        "LOG_LEVEL": answers.log_level,
    }.items():
        os.environ[key] = value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Predict.fun 网页控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
