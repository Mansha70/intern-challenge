# BTC Options Short Straddle

A testnet-first live trading example using [NautilusTrader](https://nautilustrader.io/)
and the Bybit adapter. It selects a BTC call and put with the same strike and expiry,
submits a short straddle, records order/position events, and optionally monitors
buyback risk.

> **Risk warning:** A naked short straddle has theoretically unlimited loss on the
> call side and substantial loss potential on the put side. This is an educational
> example, not investment advice or production-ready trading software. Start on
> testnet only. Level 1 intentionally has no automatic exits. Stopping the process
> does not close existing positions.

## What it implements

- **Level 1:** sell one call and one put at a common strike and nearest expiry.
- **Level 2 (default):** also buy back both legs if their combined ask-side value
  rises by 1% (stop-loss) or falls by 2% (take-profit) from the combined entry
  premium.
- Uses the Bybit option-chain snapshot's ATM strike, which is based on the
  underlying reference price, then selects the nearest strike present for both
  call and put.
- Uses IOC market orders for entry. A partially filled, rejected, or canceled
  entry is not treated as a successful straddle: pending entry orders are canceled
  and any fills are unwound with reduce-only buy orders.
- Prevents duplicate simultaneous exit attempts, retries canceled partial exits
  up to three times, and logs rejected exits for manual attention.
- Refuses to trade if a position already exists in either selected option
  instrument, avoiding mixing existing exposure with this strategy.

## Where each level starts

Both levels use the same strategy and Bybit testnet runner:

- **Level 1 (naked straddle):** run
  `python -m btc_straddle.runner --level 1`. Entry and option selection are in
  `src/btc_straddle/strategy.py`; automated exits are disabled.
- **Level 2 (risk-managed straddle):** run
  `python -m btc_straddle.runner --level 2` (also the default). Risk monitoring
  begins in `BtcShortStraddle._evaluate_risk` after both entry legs fill; the
  1%/2% thresholds are in `src/btc_straddle/selection.py`, and reduce-only
  buybacks are submitted by `_submit_exit_order` in
  `src/btc_straddle/strategy.py`.

## Requirements

- Windows, macOS, or Linux
- Python 3.13 through 3.15 (NautilusTrader 2.0.0rc9.dev20261104 requirement)
- Bybit testnet account with options enabled
- Bybit **testnet** API key/secret with trading permissions

## Setup (Windows PowerShell)

From the project directory:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e . --extra-index-url=https://packages.nautechsystems.io/simple
```

Set testnet credentials in the current PowerShell session (do not commit keys):

```powershell
$env:BYBIT_TESTNET_API_KEY = "your-testnet-api-key"
$env:BYBIT_TESTNET_API_SECRET = "your-testnet-api-secret"
```

You can copy `.env.example` as a reminder of the required variable names, but the
runner deliberately does not load dotenv files. This avoids silently loading
credentials from a file in the project directory.

## Run

Run Level 2 (default):

```powershell
python -m btc_straddle.runner
```

Run the naked Level 1 strategy without automated stop-loss/take-profit:

```powershell
python -m btc_straddle.runner --level 1
```

Specify a different per-leg contract quantity if Bybit's instrument minimum and
your testnet account allow it:

```powershell
python -m btc_straddle.runner --level 2 --quantity 0.01
```

`BTC_STRADDLE_QUANTITY` can be used instead of `--quantity`. The runner is
hard-coded to `BybitEnvironment.TESTNET`; it does not offer a mainnet switch.
Use API keys that are restricted to testnet.

## Run tests

The selection and risk-math unit tests use only the Python standard library:

```powershell
python -m unittest discover -s tests -v
```

The live adapter itself requires a Bybit account and should be exercised only on
testnet.

## Execution flow

1. `runner.py` validates that both testnet credentials are present and builds a
   NautilusTrader `LiveNode` with Bybit data and execution clients configured
   for `BybitProductType.OPTION`.
2. The strategy reads BTC option instruments from NautilusTrader's instrument
   cache and selects the nearest future expiry, preferring USDT-settled options.
3. It subscribes to an option-chain snapshot for that expiry. The Bybit adapter
   uses the option ticker's underlying reference price and supplies the ATM
   strike in `OptionChainSlice.atm_strike`.
4. `selection.py` finds a call and put at the same closest available common
   strike. The strategy subscribes to both quote streams and submits IOC sell
   market orders.
5. Accepted, rejected, canceled, and filled order events are logged. Position
   opened/changed/closed events are also logged. Entry fills must be complete and
   equal for both legs before risk monitoring starts.
6. For Level 2, the strategy multiplies each option's ask quote by its filled
   quantity and sums both legs. It compares that combined buyback amount with
   actual weighted-average entry premiums. On a threshold, it submits a
   reduce-only IOC buyback for each leg.

## Important limitations

- Level 1 has no stop-loss or take-profit, by design. Level 2 is the default.
- Automated thresholds are based on the latest ask quotes; they are not exchange
  native stop orders. A network outage, stale/absent quote, order rejection, or
  process shutdown can leave positions open. Failed exits are logged and require
  immediate manual verification on Bybit.
- A stop-loss/take-profit is triggered on the combined premium, but the two
  buyback orders are separate and are not atomic. One can fill while the other
  does not; the strategy monitors IOC partial fills and retries canceled
  remainders, with a bounded attempt count.
- Options availability, market liquidity, order minimums, account modes, and
  testnet behavior are exchange-dependent. Confirm contract quantity, account
  settings, and open positions before starting.
- This starter does not implement a production-grade risk engine, exchange outage
  recovery, durable strategy state, margin checks, or an emergency kill switch.

## Walkthrough checklist

- Explain how `LiveNode` starts, connects the data/execution clients, and runs the
  strategy event loop.
- Trace option discovery, expiry selection, reference ATM selection, and matching
  the call and put.
- Demonstrate submission, acknowledgement, fills, position events, and the
  partial-entry unwind path.
- For Level 2, show how ask-side values are combined, when the thresholds trigger,
  and how duplicate/partial exit attempts are handled.
- Discuss the non-atomic two-leg execution and what happens if the process stops
  or an exit order is rejected.

## References

- [NautilusTrader Bybit integration](https://nautilustrader.io/docs/latest/integrations/bybit/)
- [NautilusTrader documentation](https://nautilustrader.io/docs/latest/)
- [Bybit testnet](https://testnet.bybit.com/)
- [Short straddle overview](https://www.investopedia.com/terms/s/straddle.asp)
