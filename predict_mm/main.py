from __future__ import annotations

import argparse
import asyncio
import signal

from predict_mm.client import PredictClient
from predict_mm.config import Settings, load_config
from predict_mm.engine import MarketMakerEngine
from predict_mm.logging import configure_logging
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Predict.fun passive market maker bot")
    parser.add_argument("--config", default="config.toml", help="Path to TOML config")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    config = load_config(args.config)

    client = PredictClient(settings=settings, dry_run=config.dry_run)
    engine = MarketMakerEngine(
        config=config,
        client=client,
        strategy=PassiveMakerStrategy(config.strategy),
        risk=RiskManager(config.risk),
    )

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        loop.add_signal_handler(getattr(signal, signame), engine.request_stop)

    await engine.run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
