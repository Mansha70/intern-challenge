
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.bybit import BYBIT
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import ClientId
from nautilus_trader.model import OptionChainSlice
from nautilus_trader.model import OptionSeriesId
from nautilus_trader.model import OrderFilled
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrikeRange
from nautilus_trader.model import TimeInForce
from nautilus_trader.trading import Strategy

from btc_straddle.selection import OptionSeries
from btc_straddle.selection import as_decimal
from btc_straddle.selection import combined_premium
from btc_straddle.selection import eligible_btc_options
from btc_straddle.selection import nearest_matching_straddle
from btc_straddle.selection import nearest_option_series
from btc_straddle.selection import risk_exit_reason


@dataclass
class LegState:
    instrument: Any
    target_quantity: Decimal
    entry_filled_quantity: Decimal = Decimal("0")
    entry_filled_value: Decimal = Decimal("0")
    entry_order_id: Any | None = None
    entry_terminal: bool = False
    ask_price: Decimal | None = None
    ask_timestamp_ns: int | None = None
    exit_filled_quantity: Decimal = Decimal("0")
    exit_order_id: Any | None = None
    exit_order_pending: bool = False
    exit_complete: bool = False
    exit_failed: bool = False
    exit_attempts: int = 0


class BtcStraddleConfig(StrategyConfig):
    """Settings for the short straddle."""

    quantity: Decimal
    settlement_currency: str
    enable_risk_management: bool
    stop_loss_fraction: Decimal
    take_profit_fraction: Decimal
    option_chain_snapshot_interval_ms: int
    max_exit_attempts: int

    def __init__(
        self,
        *,
        quantity: Decimal = Decimal("0.01"),
        settlement_currency: str = "USDT",
        enable_risk_management: bool = True,
        stop_loss_fraction: Decimal = Decimal("0.01"),
        take_profit_fraction: Decimal = Decimal("0.02"),
        option_chain_snapshot_interval_ms: int = 5_000,
        max_exit_attempts: int = 3,
    ) -> None:
        super().__init__()
        self.quantity = quantity
        self.settlement_currency = settlement_currency
        self.enable_risk_management = enable_risk_management
        self.stop_loss_fraction = stop_loss_fraction
        self.take_profit_fraction = take_profit_fraction
        self.option_chain_snapshot_interval_ms = option_chain_snapshot_interval_ms
        self.max_exit_attempts = max_exit_attempts


class BtcShortStraddle(Strategy):
    """Sell a BTC call and put, then optionally buy both back at risk thresholds."""

    def __init__(self, config: BtcStraddleConfig) -> None:
        if config.quantity <= 0:
            raise ValueError("quantity must be positive")
        if not Decimal("0") < config.stop_loss_fraction < Decimal("1"):
            raise ValueError("stop_loss_fraction must be between 0 and 1")
        if not Decimal("0") < config.take_profit_fraction < Decimal("1"):
            raise ValueError("take_profit_fraction must be between 0 and 1")
        if config.option_chain_snapshot_interval_ms <= 0:
            raise ValueError("option_chain_snapshot_interval_ms must be positive")
        if config.max_exit_attempts <= 0:
            raise ValueError("max_exit_attempts must be positive")

        super().__init__(config)
        self._config = config
        self._settlement_currency = config.settlement_currency.upper()
        self._client_id = ClientId(BYBIT)
        self._options: list[Any] = []
        self._series: OptionSeries | None = None
        self._series_id: OptionSeriesId | None = None
        self._legs: dict[str, LegState] = {}
        self._order_roles: dict[str, tuple[str, str]] = {}
        self._order_quantities: dict[str, Decimal] = {}
        self._order_filled_quantities: dict[str, Decimal] = {}
        self._phase = "starting"
        self._entry_failed = False
        self._stopping = False
        self._entry_premium = Decimal("0")
        self._exit_reason: str | None = None

    def on_start(self) -> None:
        options = eligible_btc_options(
            self.cache.instruments(),
            now_ns=self.clock.timestamp_ns(),
            settlement_currency=self._settlement_currency,
        )
        self._series = nearest_option_series(options, self.clock.timestamp_ns())
        if self._series is None:
            self.log.error("No unexpired BTC options are available from the Bybit cache")
            self.stop()
            return

        self._options = [
            instrument
            for instrument in options
            if instrument.expiration_ns == self._series.expiration_ns
        ]
        self._series_id = OptionSeriesId(
            BYBIT,
            "BTC",
            self._series.settlement_currency,
            self._series.expiration_ns,
        )
        self._phase = "selecting"
        self.log.info(
            f"Watching BTC options expiring at {self._series.expiration_ns} "
            f"({self._series.settlement_currency}) for the reference ATM strike",
        )
        self.subscribe_option_chain(
            series_id=self._series_id,
            strike_range=StrikeRange.atm_relative(
                strikes_above=1,
                strikes_below=1,
            ),
            snapshot_interval_ms=self._config.option_chain_snapshot_interval_ms,
            client_id=self._client_id,
        )

    def on_option_chain(self, slice: OptionChainSlice) -> None:
        if self._phase != "selecting" or self._series is None:
            return
        if slice.atm_strike is None:
            return

        reference_strike = as_decimal(slice.atm_strike)
        pair = nearest_matching_straddle(
            self._options,
            expiration_ns=self._series.expiration_ns,
            settlement_currency=self._series.settlement_currency,
            reference_strike=reference_strike,
        )
        if pair is None:
            self.log.error("No matching call and put are available for the selected expiry")
            return

        if pair.strike != reference_strike:
            self.log.info(
                f"Reference ATM {reference_strike} is unavailable as a matched pair; "
                f"using nearest common strike {pair.strike}",
            )
        if self.cache.positions_open(instrument_id=pair.call.id) or self.cache.positions_open(
            instrument_id=pair.put.id,
        ):
            self.log.error(
                "A position already exists in one of the selected contracts; "
                "refusing to mix it with this strategy",
            )
            self.stop()
            return

        try:
            call_quantity = self._make_quantity(pair.call, self._config.quantity)
            put_quantity = self._make_quantity(pair.put, self._config.quantity)
        except ValueError as exc:
            self.log.error(str(exc))
            self.stop()
            return
        if call_quantity != put_quantity:
            self.log.error(
                f"Contract quantity precision differs between legs "
                f"({call_quantity} vs {put_quantity}); refusing an unbalanced straddle",
            )
            self.stop()
            return

        self._legs = {
            "call": LegState(pair.call, call_quantity),
            "put": LegState(pair.put, put_quantity),
        }
        self._phase = "entering"
        self.unsubscribe_option_chain(series_id=self._series_id, client_id=self._client_id)
        self.subscribe_quotes(
            [pair.call.id, pair.put.id],
            client_id=self._client_id,
        )
        self.log.info(
            f"Selected BTC short straddle: strike={pair.strike}, "
            f"expiry_ns={self._series.expiration_ns}, "
            f"call={pair.call.id}, put={pair.put.id}, quantity={call_quantity}",
        )
        self._submit_entry_order("call")
        self._submit_entry_order("put")

    def on_quote(self, quote: QuoteTick) -> None:
        for leg in self._legs.values():
            if quote.instrument_id != leg.instrument.id:
                continue
            ask = as_decimal(quote.ask_price)
            if ask > 0:
                leg.ask_price = ask
                leg.ask_timestamp_ns = quote.ts_event
                self._evaluate_risk()
            return

    def on_order_submitted(self, event: Any) -> None:
        self.log.info(
            f"Order submitted: id={event.client_order_id}, "
            f"instrument={event.instrument_id}, side={event.order_side}",
        )

    def on_order_accepted(self, event: Any) -> None:
        self.log.info(
            f"Order accepted: id={event.client_order_id}, "
            f"instrument={event.instrument_id}, venue_order_id={event.venue_order_id}",
        )

    def on_order_filled(self, event: OrderFilled) -> None:
        order_key = str(event.client_order_id)
        role = self._order_roles.get(order_key)
        if role is None:
            return

        quantity = as_decimal(event.last_qty)
        price = as_decimal(event.last_px)
        filled = self._order_filled_quantities.get(order_key, Decimal("0")) + quantity
        self._order_filled_quantities[order_key] = filled
        action, leg_name = role
        leg = self._legs[leg_name]

        if action == "entry":
            leg.entry_filled_quantity += quantity
            leg.entry_filled_value += quantity * price
            self.log.info(
                f"Entry fill: leg={leg_name}, quantity={quantity}, price={price}, "
                f"cumulative_quantity={leg.entry_filled_quantity}",
            )
            if filled >= self._order_quantities[order_key]:
                leg.entry_terminal = True
                self._resolve_entry()
            return

        leg.exit_filled_quantity += quantity
        self.log.info(
            f"Exit fill: leg={leg_name}, quantity={quantity}, price={price}, "
            f"remaining={leg.entry_filled_quantity - leg.exit_filled_quantity}",
        )
        if filled >= self._order_quantities[order_key]:
            leg.exit_order_pending = False
            leg.exit_order_id = None
            if leg.exit_filled_quantity >= leg.entry_filled_quantity:
                leg.exit_complete = True
                self._resolve_exit()

    def on_order_canceled(self, event: Any) -> None:
        self._handle_order_failure(event, "canceled")

    def on_order_expired(self, event: Any) -> None:
        self._handle_order_failure(event, "expired")

    def on_order_rejected(self, event: Any) -> None:
        self._handle_order_failure(event, f"rejected: {event.reason}")

    def on_order_denied(self, event: Any) -> None:
        self._handle_order_failure(event, f"denied: {event.reason}")

    def on_position_opened(self, event: Any) -> None:
        self.log.info(
            f"Position opened: id={event.position_id}, instrument={event.instrument_id}",
        )

    def on_position_changed(self, event: Any) -> None:
        self.log.info(
            f"Position changed: id={event.position_id}, instrument={event.instrument_id}",
        )

    def on_position_closed(self, event: Any) -> None:
        self.log.info(
            f"Position closed: id={event.position_id}, instrument={event.instrument_id}",
        )

    def on_stop(self) -> None:
        self._stopping = True
        if self._series_id is not None and self._phase == "selecting":
            self.unsubscribe_option_chain(
                series_id=self._series_id,
                client_id=self._client_id,
            )
        for leg in self._legs.values():
            if leg.entry_order_id is not None and not leg.entry_terminal:
                order = self.cache.order(leg.entry_order_id)
                if order is not None and not order.is_closed:
                    self.cancel_order(order.client_order_id, client_id=self._client_id)
            if leg.exit_order_id is not None and leg.exit_order_pending:
                order = self.cache.order(leg.exit_order_id)
                if order is not None and not order.is_closed:
                    self.cancel_order(order.client_order_id, client_id=self._client_id)

        open_quantity = {
            name: leg.entry_filled_quantity - leg.exit_filled_quantity
            for name, leg in self._legs.items()
            if leg.entry_filled_quantity > leg.exit_filled_quantity
        }
        if open_quantity:
            self.log.error(
                f"Strategy stopped with open short option quantities {open_quantity}. "
                "Stopping the process does not liquidate positions.",
            )

    def _submit_entry_order(self, leg_name: str) -> None:
        leg = self._legs[leg_name]
        if self._entry_failed:
            leg.entry_terminal = True
            self._resolve_entry()
            return

        order = self.order_factory.market(
            instrument_id=leg.instrument.id,
            order_side=OrderSide.SELL,
            quantity=Quantity.from_decimal(leg.target_quantity),
            time_in_force=TimeInForce.IOC,
        )
        order_key = str(order.client_order_id)
        leg.entry_order_id = order.client_order_id
        self._order_roles[order_key] = ("entry", leg_name)
        self._order_quantities[order_key] = leg.target_quantity
        self._order_filled_quantities[order_key] = Decimal("0")
        self.submit_order(order, client_id=self._client_id)

    def _handle_order_failure(self, event: Any, reason: str) -> None:
        order_key = str(event.client_order_id)
        role = self._order_roles.get(order_key)
        if role is None:
            return

        action, leg_name = role
        leg = self._legs[leg_name]
        self.log.warning(
            f"Order {reason}: id={event.client_order_id}, "
            f"instrument={event.instrument_id}",
        )
        if action == "entry":
            self._entry_failed = True
            leg.entry_terminal = True
            self._cancel_pending_entry_orders()
            self._resolve_entry()
            return

        leg.exit_order_pending = False
        leg.exit_order_id = None
        if reason.startswith(("rejected", "denied")):
            leg.exit_failed = True
            self.log.error(
                f"Could not close {leg_name} leg after {reason}; "
                "manual intervention may be required",
            )
            return
        if self._stopping:
            return
        self._submit_exit_order(leg_name)

    def _cancel_pending_entry_orders(self) -> None:
        for leg in self._legs.values():
            if leg.entry_order_id is None or leg.entry_terminal:
                continue
            order = self.cache.order(leg.entry_order_id)
            if order is not None and not order.is_closed:
                self.cancel_order(order.client_order_id, client_id=self._client_id)

    def _resolve_entry(self) -> None:
        if self._phase != "entering" or not all(
            leg.entry_terminal for leg in self._legs.values()
        ):
            return

        call = self._legs["call"]
        put = self._legs["put"]
        balanced = (
            call.entry_filled_quantity == put.entry_filled_quantity
            and call.entry_filled_quantity == call.target_quantity
            and put.entry_filled_quantity == put.target_quantity
        )
        if self._entry_failed or not balanced:
            self._phase = "exiting"
            self._exit_reason = "entry leg did not fill completely and equally"
            self.log.error(
                "Entry was not filled as a complete, balanced pair; "
                "canceling remaining entry orders and unwinding any fills",
            )
            for leg_name, leg in self._legs.items():
                if leg.entry_filled_quantity == 0:
                    leg.exit_complete = True
                else:
                    self._submit_exit_order(leg_name)
            self._resolve_exit()
            return

        self._entry_premium = combined_premium(
            call.entry_filled_value / call.entry_filled_quantity,
            put.entry_filled_value / put.entry_filled_quantity,
            call.entry_filled_quantity,
            put.entry_filled_quantity,
        )
        self._phase = "open"
        self.log.info(
            f"Balanced short straddle opened: combined entry premium={self._entry_premium}",
        )

    def _evaluate_risk(self) -> None:
        if self._phase != "open" or not self._config.enable_risk_management:
            return
        if any(leg.ask_price is None or leg.ask_timestamp_ns is None for leg in self._legs.values()):
            return

        now_ns = self.clock.timestamp_ns()
        max_age_ns = 10_000_000_000
        if any(
            now_ns - leg.ask_timestamp_ns > max_age_ns
            or now_ns < leg.ask_timestamp_ns
            for leg in self._legs.values()
        ):
            return

        call = self._legs["call"]
        put = self._legs["put"]
        buyback_premium = combined_premium(
            call.ask_price,
            put.ask_price,
            call.entry_filled_quantity,
            put.entry_filled_quantity,
        )
        reason = risk_exit_reason(
            self._entry_premium,
            buyback_premium,
            stop_loss_fraction=self._config.stop_loss_fraction,
            take_profit_fraction=self._config.take_profit_fraction,
        )
        if reason is None:
            return

        self._phase = "exiting"
        self._exit_reason = reason
        self.log.warning(
            f"{reason.replace('_', ' ').title()} triggered: "
            f"entry={self._entry_premium}, ask-side buyback={buyback_premium}",
        )
        for leg_name in self._legs:
            self._submit_exit_order(leg_name)

    def _submit_exit_order(self, leg_name: str) -> None:
        if self._stopping:
            return
        leg = self._legs[leg_name]
        remaining = leg.entry_filled_quantity - leg.exit_filled_quantity
        if remaining <= 0:
            leg.exit_complete = True
            self._resolve_exit()
            return
        if leg.exit_order_pending or leg.exit_failed:
            return
        if leg.exit_attempts >= self._config.max_exit_attempts:
            leg.exit_failed = True
            self.log.error(
                f"Reached {self._config.max_exit_attempts} exit attempts for {leg_name}; "
                "manual intervention may be required",
            )
            return

        precision = leg.instrument.size_precision
        quantity_value = remaining.quantize(Decimal("1").scaleb(-precision))
        if quantity_value != remaining:
            leg.exit_failed = True
            self.log.error(
                f"Cannot represent remaining {leg_name} quantity {remaining} "
                f"at instrument precision {precision}",
            )
            return
        quantity = Quantity.from_decimal(quantity_value)

        order = self.order_factory.market(
            instrument_id=leg.instrument.id,
            order_side=OrderSide.BUY,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
        )
        order_key = str(order.client_order_id)
        self._order_roles[order_key] = ("exit", leg_name)
        self._order_quantities[order_key] = remaining
        self._order_filled_quantities[order_key] = Decimal("0")
        leg.exit_order_pending = True
        leg.exit_order_id = order.client_order_id
        leg.exit_attempts += 1
        self.log.info(
            f"Submitting reduce-only buyback for {leg_name}: "
            f"quantity={remaining}, attempt={leg.exit_attempts}",
        )
        self.submit_order(order, client_id=self._client_id)

    def _resolve_exit(self) -> None:
        if self._phase != "exiting":
            return
        if all(leg.exit_complete for leg in self._legs.values()):
            self._phase = "closed"
            self.log.info(f"Straddle closed successfully: reason={self._exit_reason}")

    @staticmethod
    def _make_quantity(instrument: Any, requested: Decimal) -> Decimal:
        precision = instrument.size_precision
        actual = requested.quantize(Decimal("1").scaleb(-precision))
        if actual != requested:
            raise ValueError(
                f"Requested quantity {requested} is not representable for {instrument.id}",
            )
        Quantity.from_decimal(actual)
        minimum = getattr(instrument, "min_quantity", None)
        if minimum is not None and actual < as_decimal(minimum):
            raise ValueError(
                f"Quantity {actual} is below minimum {minimum} for {instrument.id}",
            )
        return actual
