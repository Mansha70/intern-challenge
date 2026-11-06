"""Build and run the live Bybit testnet node."""

from __future__ import annotations

import argparse
import os
from decimal import Decimal

from nautilus_trader.adapters.bybit import BYBIT
from nautilus_trader.adapters.bybit import BybitDataClientConfig
from nautilus_trader.adapters.bybit import BybitEnvironment
from nautilus_trader.adapters.bybit import BybitDataClientFactory
from nautilus_trader.adapters.bybit import BybitExecutionClientConfig
from nautilus_trader.adapters.bybit import BybitExecutionClientFactory
from nautilus_trader.adapters.bybit import BybitProductType
from nautilus_trader.common import Environment
from nautilus_trader.common import LoggerConfig
from nautilus_trader.common import LogLevel
from nautilus_trader.live import LiveNode
from nautilus_trader.model import TraderId

from btc_straddle.strategy import BtcShortStraddle
from btc_straddle.strategy import BtcStraddleConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the BTC short-straddle strategy on Bybit testnet.",
    )
    parser.add_argument(
        "--level",
        choices=("1", "2"),
        default="2",
        help="1 = naked straddle, 2 = 1%% stop-loss and 2%% take-profit (default: 2)",
    )
    parser.add_argument(
        "--quantity",
        default=os.getenv("BTC_STRADDLE_QUANTITY", "0.01"),
        type=Decimal,
        help="Contracts per leg (default: 0.01)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not os.getenv("BYBIT_TESTNET_API_KEY") or not os.getenv("BYBIT_TESTNET_API_SECRET"):
        raise SystemExit(
            "Set BYBIT_TESTNET_API_KEY and BYBIT_TESTNET_API_SECRET before starting.",
        )

    trader_id = TraderId("STRADDLE-001")
    options = (BybitProductType.OPTION,)
    environment = BybitEnvironment.TESTNET
    api_key = os.environ["BYBIT_TESTNET_API_KEY"]
    api_secret = os.environ["BYBIT_TESTNET_API_SECRET"]

    node = (
        LiveNode.builder("BTC-STRADDLE", trader_id, Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.INFO))
        .with_reconciliation(True)
        .with_timeout_connection(30)
        .with_timeout_reconciliation(10)
        .with_timeout_portfolio(10)
        .with_timeout_disconnection_secs(10)
        .with_delay_post_stop_secs(5)
        .add_data_client(
            BYBIT,
            BybitDataClientFactory(),
            BybitDataClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                product_types=options,
                environment=environment,
            ),
        )
        .add_exec_client(
            BYBIT,
            BybitExecutionClientFactory(),
            BybitExecutionClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                product_types=options,
                environment=environment,
            ),
        )
        .build()
    )
    node.add_strategy(
        BtcShortStraddle(
            BtcStraddleConfig(
                quantity=args.quantity,
                enable_risk_management=args.level == "2",
            ),
        ),
    )
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
