from __future__ import annotations

import unittest
from dataclasses import dataclass
from decimal import Decimal

from btc_straddle.selection import combined_premium
from btc_straddle.selection import eligible_btc_options
from btc_straddle.selection import nearest_matching_straddle
from btc_straddle.selection import nearest_option_series
from btc_straddle.selection import risk_exit_reason


@dataclass(frozen=True)
class FakeId:
    symbol: str
    venue: str = "BYBIT"


@dataclass(frozen=True)
class FakeInstrument:
    symbol: str
    kind: str
    strike_price: Decimal
    expiration_ns: int
    settlement_currency: str = "USDT"
    venue: str = "BYBIT"

    @property
    def id(self) -> FakeId:
        return FakeId(self.symbol, self.venue)

    @property
    def option_kind(self) -> str:
        return self.kind


def option(
    kind: str,
    strike: str,
    expiry: int = 200,
    settlement: str = "USDT",
    venue: str = "BYBIT",
) -> FakeInstrument:
    return FakeInstrument(
        symbol=f"BTC-01JAN30-{strike}-{kind[0]}-{settlement}",
        kind=kind,
        strike_price=Decimal(strike),
        expiration_ns=expiry,
        settlement_currency=settlement,
        venue=venue,
    )


class SelectionTests(unittest.TestCase):
    def test_selects_nearest_unexpired_usdt_series(self) -> None:
        instruments = [
            option("CALL", "50000", expiry=100),
            option("CALL", "50000", expiry=150, settlement="USDC"),
            option("CALL", "50000", expiry=200),
            option("PUT", "50000", expiry=200),
        ]

        selected = nearest_option_series(instruments, now_ns=99)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.expiration_ns, 200)
        self.assertEqual(selected.settlement_currency, "USDT")
        self.assertEqual(
            eligible_btc_options(instruments, now_ns=100, settlement_currency="USDT"),
            instruments[2:],
        )

    def test_requires_matching_call_and_put_at_common_nearest_strike(self) -> None:
        call_50000 = option("CALL", "50000")
        put_50000 = option("PUT", "50000")
        call_51000 = option("CALL", "51000")
        put_51000 = option("PUT", "51000")
        selected = nearest_matching_straddle(
            [call_50000, put_50000, call_51000, put_51000],
            expiration_ns=200,
            settlement_currency="USDT",
            reference_strike=Decimal("50550"),
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.strike, Decimal("51000"))
        self.assertIs(selected.call, call_51000)
        self.assertIs(selected.put, put_51000)

    def test_never_pairs_different_expiry_settlement_or_venue(self) -> None:
        instruments = [
            option("CALL", "50000"),
            option("PUT", "50000", expiry=201),
            option("PUT", "50000", settlement="USDC"),
            option("PUT", "50000", venue="OTHER"),
        ]
        selected = nearest_matching_straddle(
            instruments,
            expiration_ns=200,
            settlement_currency="USDT",
            reference_strike=Decimal("50000"),
        )

        self.assertIsNone(selected)


class RiskTests(unittest.TestCase):
    def test_combines_both_leg_values_using_actual_quantities(self) -> None:
        self.assertEqual(
            combined_premium(
                Decimal("100"),
                Decimal("50"),
                Decimal("2"),
                Decimal("3"),
            ),
            Decimal("350"),
        )

    def test_triggers_stop_loss_at_one_percent_increase(self) -> None:
        self.assertEqual(
            risk_exit_reason(Decimal("100"), Decimal("101")),
            "stop_loss",
        )

    def test_triggers_take_profit_at_two_percent_decrease(self) -> None:
        self.assertEqual(
            risk_exit_reason(Decimal("100"), Decimal("98")),
            "take_profit",
        )

    def test_does_not_trigger_between_thresholds_or_without_entry_value(self) -> None:
        self.assertIsNone(risk_exit_reason(Decimal("100"), Decimal("100.50")))
        self.assertIsNone(risk_exit_reason(Decimal("0"), Decimal("0")))


if __name__ == "__main__":
    unittest.main()
