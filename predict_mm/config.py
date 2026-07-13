from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_base_url: str = "https://api.predict.fun"
    api_key: str | None = None
    jwt_token: str | None = None
    private_key: str | None = None
    predict_account_address: str | None = None
    chain_id: int = 56
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path(".env"))
        return cls(
            api_base_url=os.getenv("PREDICT_API_BASE_URL", cls.api_base_url),
            api_key=os.getenv("PREDICT_API_KEY") or None,
            jwt_token=os.getenv("PREDICT_JWT_TOKEN") or None,
            private_key=os.getenv("PREDICT_PRIVATE_KEY") or None,
            predict_account_address=os.getenv("PREDICT_ACCOUNT_ADDRESS") or None,
            chain_id=int(os.getenv("PREDICT_CHAIN_ID", str(cls.chain_id))),
            log_level=os.getenv("LOG_LEVEL", cls.log_level),
        )


@dataclass(frozen=True)
class StrategyConfig:
    tick_size: Decimal = Decimal("0.001")
    quote_size: Decimal = Decimal("1")
    join_best_price: bool = False
    min_edge_ticks: int = 2
    max_spread_to_quote: Decimal = Decimal("0.20")
    min_spread_to_quote: Decimal = Decimal("0.006")


@dataclass(frozen=True)
class RiskConfig:
    max_order_size: Decimal = Decimal("1")
    max_position_per_market: Decimal = Decimal("10")
    max_total_position: Decimal = Decimal("50")
    max_open_orders_per_market: int = 2
    pause_after_fill_seconds: float = 60


@dataclass(frozen=True)
class MarketConfig:
    id: str
    enabled: bool = True
    outcome: str = "YES"
    quote_size: Decimal | None = None
    token_id: str | None = None
    fee_rate_bps: int | None = None
    is_neg_risk: bool | None = None
    is_yield_bearing: bool | None = None


@dataclass(frozen=True)
class BotConfig:
    dry_run: bool = True
    poll_interval_seconds: float = 2
    cancel_after_seconds: float = 8
    replace_on_orderbook_change: bool = True
    cancel_all_on_start: bool = True
    cancel_all_on_shutdown: bool = True
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    markets: list[MarketConfig] = field(default_factory=list)

    @property
    def enabled_markets(self) -> list[MarketConfig]:
        return [market for market in self.markets if market.enabled]


def load_config(path: str | Path) -> BotConfig:
    with Path(path).open("rb") as file:
        raw = tomllib.load(file)

    markets = [_market(market) for market in raw.get("markets", [])]
    config = BotConfig(
        dry_run=bool(raw.get("dry_run", True)),
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 2)),
        cancel_after_seconds=float(raw.get("cancel_after_seconds", 8)),
        replace_on_orderbook_change=bool(raw.get("replace_on_orderbook_change", True)),
        cancel_all_on_start=bool(raw.get("cancel_all_on_start", True)),
        cancel_all_on_shutdown=bool(raw.get("cancel_all_on_shutdown", True)),
        strategy=_strategy(raw.get("strategy", {})),
        risk=_risk(raw.get("risk", {})),
        markets=markets,
    )
    if not config.enabled_markets:
        raise ValueError("at least one enabled market is required")
    return config


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _decimal(value: object, default: str) -> Decimal:
    return Decimal(str(value if value is not None else default))


def _strategy(raw: dict) -> StrategyConfig:
    return StrategyConfig(
        tick_size=_decimal(raw.get("tick_size"), "0.001"),
        quote_size=_decimal(raw.get("quote_size"), "1"),
        join_best_price=bool(raw.get("join_best_price", False)),
        min_edge_ticks=int(raw.get("min_edge_ticks", 2)),
        max_spread_to_quote=_decimal(raw.get("max_spread_to_quote"), "0.20"),
        min_spread_to_quote=_decimal(raw.get("min_spread_to_quote"), "0.006"),
    )


def _market(raw: dict) -> MarketConfig:
    market = dict(raw)
    if market.get("quote_size") is not None:
        market["quote_size"] = _decimal(market["quote_size"], "1")
    return MarketConfig(**market)


def _risk(raw: dict) -> RiskConfig:
    return RiskConfig(
        max_order_size=_decimal(raw.get("max_order_size"), "1"),
        max_position_per_market=_decimal(raw.get("max_position_per_market"), "10"),
        max_total_position=_decimal(raw.get("max_total_position"), "50"),
        max_open_orders_per_market=int(raw.get("max_open_orders_per_market", 2)),
        pause_after_fill_seconds=float(raw.get("pause_after_fill_seconds", 60)),
    )
