"""Pure helpers for selecting a matching BTC option straddle."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable


@dataclass(frozen=True)
class OptionSeries:
    expiration_ns: int
    settlement_currency: str


@dataclass(frozen=True)
class StraddlePair:
    call: Any
    put: Any
    strike: Decimal


def as_decimal(value: Any) -> Decimal:
    """Convert Nautilus numeric values and ordinary numbers without float math."""
    if hasattr(value, "as_decimal"):
        return value.as_decimal()
    return Decimal(str(value))


def _option_kind(instrument: Any) -> str:
    kind = instrument.option_kind
    return str(getattr(kind, "name", kind)).upper()


def _is_btc_option(instrument: Any) -> bool:
    instrument_id = getattr(instrument, "id", None)
    if instrument_id is None:
        return False
    symbol = str(getattr(instrument_id, "symbol", instrument_id)).upper()
    venue = str(getattr(instrument_id, "venue", "")).upper()
    return symbol.startswith("BTC-") and (not venue or venue == "BYBIT")


def eligible_btc_options(
    instruments: Iterable[Any],
    *,
    now_ns: int,
    settlement_currency: str = "USDT",
) -> list[Any]:
    """Return unexpired BTC options for the requested settlement currency."""
    eligible = []
    settlement_currency = settlement_currency.upper()

    for instrument in instruments:
        if not _is_btc_option(instrument):
            continue
        if not hasattr(instrument, "option_kind"):
            continue

        expiration_ns = getattr(instrument, "expiration_ns", None)
        settlement = str(getattr(instrument, "settlement_currency", "")).upper()
        if expiration_ns is None or expiration_ns <= now_ns:
            continue
        if settlement != settlement_currency:
            continue
        if _option_kind(instrument) not in {"CALL", "PUT"}:
            continue
        if not hasattr(instrument, "strike_price"):
            continue
        eligible.append(instrument)

    return eligible


def nearest_option_series(instruments: Iterable[Any], now_ns: int) -> OptionSeries | None:
    """Choose the soonest expiry that has at least one matching call/put strike."""
    contracts: dict[tuple[int, str], dict[str, set[Decimal]]] = {}
    for instrument in instruments:
        if not _is_btc_option(instrument) or not hasattr(instrument, "option_kind"):
            continue
        expiration_ns = getattr(instrument, "expiration_ns", None)
        if expiration_ns is None or expiration_ns <= now_ns:
            continue
        settlement = str(getattr(instrument, "settlement_currency", "")).upper()
        kind = _option_kind(instrument)
        if kind not in {"CALL", "PUT"} or not hasattr(instrument, "strike_price"):
            continue
        by_kind = contracts.setdefault((expiration_ns, settlement), {"CALL": set(), "PUT": set()})
        by_kind[kind].add(as_decimal(instrument.strike_price))

    eligible_series = [
        (expiration_ns, settlement)
        for (expiration_ns, settlement), by_kind in contracts.items()
        if by_kind["CALL"] & by_kind["PUT"]
    ]
    if not eligible_series:
        return None

    nearest_expiration = min(expiration for expiration, _ in eligible_series)
    settlement = next(
        (
            currency
            for expiration, currency in eligible_series
            if expiration == nearest_expiration and currency == "USDT"
        ),
        next(
            currency
            for expiration, currency in eligible_series
            if expiration == nearest_expiration
        ),
    )
    return OptionSeries(nearest_expiration, settlement)


def nearest_matching_straddle(
    instruments: Iterable[Any],
    *,
    expiration_ns: int,
    settlement_currency: str,
    reference_strike: Decimal,
) -> StraddlePair | None:
    """Find call/put instruments sharing the closest available strike."""
    calls: dict[Decimal, Any] = {}
    puts: dict[Decimal, Any] = {}
    settlement_currency = settlement_currency.upper()

    for instrument in instruments:
        if not _is_btc_option(instrument):
            continue
        if getattr(instrument, "expiration_ns", None) != expiration_ns:
            continue
        if str(getattr(instrument, "settlement_currency", "")).upper() != settlement_currency:
            continue

        strike = as_decimal(instrument.strike_price)
        kind = _option_kind(instrument)
        if kind == "CALL":
            calls.setdefault(strike, instrument)
        elif kind == "PUT":
            puts.setdefault(strike, instrument)

    common_strikes = calls.keys() & puts.keys()
    if not common_strikes:
        return None

    strike = min(common_strikes, key=lambda value: (abs(value - reference_strike), value))
    return StraddlePair(call=calls[strike], put=puts[strike], strike=strike)


def combined_premium(
    call_price: Decimal,
    put_price: Decimal,
    call_quantity: Decimal,
    put_quantity: Decimal,
) -> Decimal:
    """Value both legs in the same quote units using their actual quantities."""
    if call_quantity <= 0 or put_quantity <= 0:
        raise ValueError("Both option quantities must be positive")
    return call_price * call_quantity + put_price * put_quantity


def risk_exit_reason(
    entry_premium: Decimal,
    buyback_premium: Decimal,
    *,
    stop_loss_fraction: Decimal = Decimal("0.01"),
    take_profit_fraction: Decimal = Decimal("0.02"),
) -> str | None:
    """Return the first triggered exit using ask-side buyback valuation."""
    if entry_premium <= 0 or buyback_premium < 0:
        return None
    if buyback_premium >= entry_premium * (Decimal("1") + stop_loss_fraction):
        return "stop_loss"
    if buyback_premium <= entry_premium * (Decimal("1") - take_profit_fraction):
        return "take_profit"
    return None
