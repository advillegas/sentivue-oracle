---
name: trading-systems
description: Trading system engineering - event-driven architecture, backtest engines (Nautilus Trader, vectorbt, backtesting.py), order/risk management, state persistence, paper-live parity. Use when building backtesting or execution infrastructure.
---

# Trading Systems

## Engine selection

- **vectorbt** — vectorized parameter sweeps over bar data; thousands of variants in
  seconds; not for path-dependent order logic.
- **backtesting.py** — single-strategy bar-level logic with stops/limits; quick and readable.
- **Nautilus Trader** (`env` extra `trading`) — production event-driven: same strategy
  code for backtest and live, realistic fill models, portfolio accounting. Use for
  anything you might actually run.
- Custom engine only when the above demonstrably cannot express the logic; if so, keep
  it < 500 lines and validate it against one of the above on a shared scenario.

## Architecture (event-driven)

```
MarketData -> Strategy(signals) -> RiskManager(pre-trade checks) -> OMS(orders/fills)
     -> Portfolio(positions/PnL) -> Persistence(event journal)      [each a pure component]
```
- Components communicate via typed events; every event carries `ts_event` (exchange
  time) and `ts_init` (arrival time) — the difference is your latency model.
- The strategy is a pure function of (state, event) → orders. All I/O lives at the edges.
- Journal every event append-only (Parquet/DuckDB); any state must be reconstructable
  by replay. Crash recovery = replay journal.

## Order & risk management

- Order lifecycle: NEW → PENDING → PARTIAL/FILLED → CANCELED/REJECTED/EXPIRED.
  Model rejections and partial fills in backtests; strategies must handle them.
- Pre-trade checks (hard, non-bypassable): max position per symbol, max gross/net
  exposure, max order notional, price collar vs last trade, duplicate-order guard,
  kill switch flag checked before EVERY order.
- Position sizing: fixed-fractional or vol-scaled; sizing code unit-tested against
  hand-computed cases, including flip (long→short crosses through flat, two orders).

## Fill realism (backtest)

- Decide on bar T close → execute at T+1 open + slippage. Slippage: half-spread +
  impact term `k * sqrt(order_size / ADV)`; document k.
- Limit orders: fill only if the bar traded through the limit price (high/low test),
  fill probability < 1 at touch.
- Never fill more than a set fraction of bar volume (e.g. 10%) — capacity honesty.

## PnL accounting (test these exactly)

- Realized vs unrealized; average-cost basis per position; short PnL sign; borrow cost
  accrual daily; cash earns/pays the funding rate. Reconciliation test: equity(t) =
  cash(t) + Σ positions marked-to-market — must hold to the cent every event.

## Paper-live parity

Same binary, different adapter. Config chooses `BacktestDataClient | PaperExec | LiveExec`.
Log fills from paper vs backtest on identical data; divergence = bug in fill model.
Latency, throttles (max orders/min), and reconnect logic are part of the adapter, tested
with a chaos wrapper (drop/duplicate/delay events).

## Definition of done

Deterministic replay test (same journal → same PnL), risk-check unit tests including
the kill switch, reconciliation test, and a runbook section in docs/ (start, stop,
recover, flatten-all).
