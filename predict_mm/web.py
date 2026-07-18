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
from predict_mm.config import Settings, load_config, update_dotenv_value
from predict_mm.engine import MarketMakerEngine
from predict_mm.logging import configure_logging
from predict_mm.risk import RiskManager
from predict_mm.setup_wizard import MarketAnswers, WizardAnswers, build_config_text, build_env_text
from predict_mm.strategy import PassiveMakerStrategy

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web_static"


class MarketPayload(BaseModel):
    market_id: str = Field(min_length=1, max_length=200)
    market_title: str = Field(default="", max_length=500)
    outcome: str = Field(default="YES", min_length=1, max_length=120)
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
        self.market_titles: dict[str, str] = {}

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
        if self.engine is not None:
            for market in markets:
                title = self.engine.market_title(market.id)
                if title:
                    self.market_titles[market.id] = title
        return {
            "configured": self.configured(),
            "running": self.running,
            "last_error": self.last_error,
            "dry_run": config.dry_run if config else False,
            "markets": [
                {
                    "market_id": market.id,
                    "market_title": market.title or self.market_titles.get(market.id, ""),
                    "outcome": market.outcome,
                    "quote_size": str(market.quote_size or config.strategy.quote_size),
                }
                for market in markets
            ],
            "max_position_per_market": str(config.risk.max_position_per_market) if config else "10.0",
            "max_total_position": str(config.risk.max_total_position) if config else "50.0",
            "cancel_after_seconds": config.cancel_after_seconds if config else 8,
            "emergency_exit_on_buy_fill": config.emergency_exit_on_buy_fill if config else True,
            "open_orders": self.engine.active_orders() if self.engine else [],
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
            if not config.dry_run:
                approval_status = await self.trade_approval_status()
                if not approval_status["ready"]:
                    raise ValueError(
                        "当前账户缺少交易授权。请展开“账户设置”，先检查并设置交易授权。"
                    )
            if not self.logging_ready:
                configure_logging(settings.log_level)
                logging.getLogger().addHandler(self.log_handler)
                self.logging_ready = True
            client = PredictClient(
                settings=settings,
                dry_run=config.dry_run,
                jwt_token_updated=self._persist_refreshed_jwt,
            )
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
        client = PredictClient(
            settings=settings,
            dry_run=config.dry_run,
            jwt_token_updated=self._persist_refreshed_jwt,
        )
        try:
            await client.cancel_all_orders(None)
        finally:
            await client.close()

    async def account_balance(self) -> dict[str, str]:
        """Return the configured wallet's raw USDT balance without touching the bot."""
        settings = Settings.from_env()
        client = PredictClient(settings=settings, dry_run=False)
        try:
            balance, account_address = await client.get_usdt_balance()
        finally:
            await client.close()
        return {
            "asset": "USDT",
            "balance": format(balance, "f"),
            "account_address": account_address,
        }

    def _persist_refreshed_jwt(self, jwt_token: str) -> None:
        """Keep an automatically refreshed JWT across web-service restarts."""
        update_dotenv_value(self.env_path, "PREDICT_JWT_TOKEN", jwt_token)

    async def _trade_approval_plan(self) -> tuple[object, list[object]]:
        """Build the minimal official-SDK approval plan for configured markets."""
        if not self.configured():
            raise RuntimeError("请先保存市场配置，再检查交易授权。")
        settings = Settings.from_env()
        if not settings.private_key:
            raise RuntimeError("请先在账户设置中保存钱包 Private Key。")
        try:
            from predict_sdk import (  # type: ignore[import-not-found]
                ApprovalScope,
                ChainId,
                OrderBuilder,
                OrderBuilderOptions,
            )
        except ImportError as error:
            raise RuntimeError("检查交易授权需要安装 Predict.fun 官方 SDK。") from error

        options = (
            OrderBuilderOptions(predict_account=settings.predict_account_address)
            if settings.predict_account_address
            else None
        )
        builder = await asyncio.to_thread(
            OrderBuilder.make,
            ChainId(settings.chain_id),
            settings.private_key,
            options,
        )
        config = load_config(self.config_path)
        client = PredictClient(settings=settings, dry_run=False)
        steps_by_id: dict[str, object] = {}
        try:
            for market in config.enabled_markets:
                metadata = await client._get_market_metadata(market.id)
                is_neg_risk = client._optional_bool(
                    client._first_present(metadata, "isNegRisk", "is_neg_risk")
                )
                is_yield_bearing = client._optional_bool(
                    client._first_present(metadata, "isYieldBearing", "is_yield_bearing")
                )
                if is_neg_risk is None or is_yield_bearing is None:
                    raise RuntimeError(
                        f"市场 {market.id} 缺少官方授权类型信息，已停止以避免错误授权。"
                    )
                steps = builder.get_approval_steps(
                    ApprovalScope(
                        operation="TRADE",
                        is_neg_risk=is_neg_risk,
                        is_yield_bearing=is_yield_bearing,
                        side=None,
                    )
                )
                for step in steps:
                    steps_by_id.setdefault(str(step.id), step)
        finally:
            await client.close()
        return builder, list(steps_by_id.values())

    async def trade_approval_status(self) -> dict[str, object]:
        """Read approval state only; this never signs or submits a transaction."""
        builder, steps = await self._trade_approval_plan()
        checks = await builder.check_approvals_async(steps)
        native_balance_wei, gas_wallet_address = await self._native_gas_balance(builder)
        rows = [
            {
                "id": str(check.step.id),
                "type": str(check.step.type),
                "label": str(check.step.label),
                "satisfied": bool(check.satisfied),
            }
            for check in checks
        ]
        missing = sum(not row["satisfied"] for row in rows)
        return {
            "ok": True,
            "ready": missing == 0,
            "required": len(rows),
            "missing": missing,
            "steps": rows,
            "gas_asset": "BNB",
            "gas_balance": format(Decimal(native_balance_wei) / Decimal(10**18), "f"),
            "gas_wallet_address": gas_wallet_address,
        }

    async def _native_gas_balance(self, builder: object) -> tuple[int, str]:
        """Read the SDK signer's BNB balance without signing or sending a transaction."""
        web3 = getattr(builder, "_web3", None)
        signer = getattr(builder, "_signer", None)
        signer_address = str(getattr(signer, "address", ""))
        if web3 is None or not signer_address:
            raise RuntimeError("官方 SDK 未提供签名钱包信息，无法检查授权 Gas 余额。")
        balance_wei = await asyncio.to_thread(web3.eth.get_balance, signer_address)
        return int(balance_wei), signer_address

    async def set_trade_approvals(self) -> dict[str, object]:
        """Submit only missing approval transactions after the UI confirmation."""
        if self.running:
            raise RuntimeError("请先停止机器人，再设置交易授权。")
        builder, steps = await self._trade_approval_plan()
        checks = await builder.check_approvals_async(steps)
        missing_steps = [check.step for check in checks if not check.satisfied]
        if not missing_steps:
            return {
                "ok": True,
                "ready": True,
                "message": "交易授权已经完整，无需重复设置。",
            }
        native_balance_wei, _ = await self._native_gas_balance(builder)
        if native_balance_wei <= 0:
            settings = Settings.from_env()
            wallet_name = "Privy 签名钱包" if settings.predict_account_address else "EOA 钱包"
            raise RuntimeError(
                f"{wallet_name}的 BNB 余额为 0，无法支付 SDK 授权交易的 Gas。"
                "请向页面显示的“授权 Gas 钱包地址”转入少量 BNB；"
                "使用 Predict Account 时不要转到 Predict Account Address。"
            )
        try:
            report = await builder.run_approvals_async(
                missing_steps,
                skip_satisfied=True,
                stop_on_error=True,
            )
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(
                f"官方 SDK 设置交易授权失败：{error}"
            ) from error
        if not report.success:
            failed_details: list[str] = []
            for result in report.steps:
                if str(result.status).lower() != "failed":
                    continue
                transaction = getattr(result, "transaction", None)
                cause = getattr(transaction, "cause", None)
                detail = str(cause) if cause else "链上交易未成功"
                failed_details.append(f"{result.step.id}: {detail}")
            details = "；".join(failed_details) or "官方 SDK 未返回失败详情"
            logging.getLogger("predict-mm").error("设置交易授权失败：%s", details)
            raise RuntimeError(f"设置交易授权失败：{details}")
        return {
            "ok": True,
            "ready": True,
            "message": "交易授权已设置完成。此操作没有创建订单。",
        }


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

    @app.get("/api/balance")
    async def balance() -> dict[str, object]:
        try:
            return {"ok": True, **await state.account_balance()}
        except RuntimeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/approvals")
    async def approvals() -> dict[str, object]:
        try:
            return await state.trade_approval_status()
        except RuntimeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/approvals")
    async def set_approvals() -> dict[str, object]:
        try:
            return await state.set_trade_approvals()
        except RuntimeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

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
                market_title=market.market_title.strip(),
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
        api_key = payload.api_key.strip() or current.api_key or ""
        private_key = payload.private_key.strip() or current.private_key or ""
        predict_account_address = (
            payload.predict_account_address.strip() or current.predict_account_address or ""
        )
        jwt_token = current.jwt_token or ""
        generated_jwt = False
        if private_key:
            if not api_key:
                raise HTTPException(status_code=422, detail="自动生成 JWT 需要 API Key。")
            client = PredictClient(
                settings=Settings(api_base_url=current.api_base_url, api_key=api_key),
                dry_run=False,
            )
            try:
                jwt_token = (
                    await client.create_predict_account_jwt(private_key, predict_account_address)
                    if predict_account_address
                    else await client.create_eoa_jwt(private_key)
                )
                generated_jwt = True
            except RuntimeError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            except Exception as error:  # noqa: BLE001
                logging.getLogger("predict-mm").warning("自动生成 JWT 失败: %s", error)
                raise HTTPException(
                    status_code=502,
                    detail="无法连接 Predict.fun 生成 JWT，请检查 API Key 和网络后重试。",
                ) from error
            finally:
                await client.close()
        answers = WizardAnswers(
            api_base_url=current.api_base_url,
            api_key=api_key,
            jwt_token=jwt_token,
            private_key=private_key,
            predict_account_address=predict_account_address,
            log_level=payload.log_level,
        )
        state.env_path.write_text(build_env_text(answers), encoding="utf-8")
        _apply_settings_to_process(answers)
        message = "账户设置已保存。"
        if generated_jwt:
            message += (
                " 已使用 Predict Account 的 Privy Wallet 本地签名并自动生成 JWT。"
                if predict_account_address
                else " 已使用 EOA 钱包本地签名并自动生成 JWT。"
            )
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
        client = PredictClient(settings=settings, dry_run=False)
        api_search_failed = False
        localized_page = bool(_market_locale_from_url(payload.market_url))
        try:
            markets: list[dict] = []
            if localized_page:
                # The public localized page contains translated category titles,
                # market titles and questions. The API search response is English.
                markets = await client.markets_from_public_page(payload.market_url, slug)
            elif settings.api_key:
                try:
                    markets = _markets_matching_slug(await client.search_markets(slug), slug)
                    if not markets:
                        markets = _markets_matching_slug(
                            await client.search_markets(_search_query_from_slug(slug)),
                            slug,
                        )
                except Exception as error:  # noqa: BLE001
                    api_search_failed = True
                    logging.getLogger("predict-mm").warning("官方市场搜索不可用，改用公开页面: %s", error)
            if not markets:
                markets = await client.markets_from_public_page(payload.market_url, slug)
        except Exception as error:  # noqa: BLE001
            logging.getLogger("predict-mm").warning("无法从市场网址识别 ID: %s", error)
            raise HTTPException(
                status_code=502,
                detail="未能读取 Predict.fun 市场页面。请检查网络后重试，或直接填写数字 Market ID。",
            ) from error
        finally:
            await client.close()

        matches = [_market_lookup_result(market) for market in markets]
        return {
            "ok": True,
            "market_id": None,
            "matches": matches,
            "message": (
                "已从中文页面读取市场名称。请选择要挂单的市场和 Yes / No 选项。"
                if matches and localized_page
                else
                "官方搜索接口不可用，已从公开页面读取市场。请选择要挂单的市场和选项；实盘前仍需确认 API Key 有效。"
                if matches and api_search_failed
                else "请选择要挂单的市场和选项。"
                if matches
                else "未找到匹配市场，请尝试直接填写数字 Market ID。"
            ),
        }

    @app.post("/api/start")
    async def start() -> dict[str, object]:
        try:
            await state.start()
        except (ValueError, RuntimeError) as error:
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
        if _is_predict_market_url(market.market_id):
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
            if not market.market_id.strip() or not market.outcome.strip() or Decimal(market.quote_size) <= 0:
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


def _market_locale_from_url(value: str) -> str:
    parsed = urlparse(value.strip())
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        market_position = parts.index("market")
    except ValueError:
        return ""
    if market_position < 1:
        return ""
    locale = parts[market_position - 1].strip().lower()
    return locale if re.fullmatch(r"[a-z]{2}-[a-z]{2}", locale) else ""


def _is_predict_market_url(value: str) -> bool:
    try:
        _market_slug_from_url(value)
    except ValueError:
        return False
    return True


def _search_query_from_slug(slug: str) -> str:
    without_timestamp = re.sub(r"[-_]\d{8,}$", "", slug)
    query = re.sub(r"[-_]+", " ", without_timestamp).strip()
    return query or slug


def _markets_matching_slug(markets: list[dict], slug: str) -> list[dict]:
    """Keep only markets that belong to the exact Predict.fun URL."""
    expected = slug.strip().lower()
    matches: list[dict] = []
    seen_ids: set[str] = set()
    for market in markets:
        category = market.get("category")
        category_values: tuple[object, ...] = ()
        if isinstance(category, dict):
            category_values = (category.get("slug"), category.get("id"))
        candidates = (
            market.get("slug"),
            market.get("categorySlug"),
            *category_values,
        )
        if not any(str(value or "").strip().lower() == expected for value in candidates):
            continue
        market_id = str(market.get("id") or "")
        if not market_id or market_id in seen_ids:
            continue
        matches.append(market)
        seen_ids.add(market_id)
    return matches


def _market_lookup_result(market: dict) -> dict[str, object]:
    outcomes: list[str] = []
    raw_outcomes = market.get("outcomes") or []
    if isinstance(raw_outcomes, dict):
        raw_outcomes = raw_outcomes.get("edges") or []
    for outcome in raw_outcomes:
        if isinstance(outcome, dict) and isinstance(outcome.get("node"), dict):
            outcome = outcome["node"]
        if not isinstance(outcome, dict):
            continue
        name = str(outcome.get("name") or outcome.get("outcome") or outcome.get("title") or "").strip()
        name = {"是": "Yes", "否": "No"}.get(name, name)
        if name and name not in outcomes:
            outcomes.append(name)
    return {
        "id": str(market.get("id", "")),
        "title": str(market.get("title") or ""),
        "question": str(market.get("question") or market.get("title") or ""),
        "category_title": str(market.get("categoryTitle") or ""),
        "trading_status": str(market.get("tradingStatus") or ""),
        "outcomes": outcomes,
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
