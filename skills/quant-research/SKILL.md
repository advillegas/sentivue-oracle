---
name: quant-research
description: Alpha research discipline - the leakage checklist, point-in-time data, factor construction, IC analysis, portfolio construction, multiple-testing honesty. MANDATORY reference before any backtest or signal work.
---

# Quant Research Discipline

The purpose of research is a decision you can trust, not a beautiful equity curve.

## THE LEAKAGE CHECKLIST (run before trusting any result)

1. **Lookahead** — does the signal at time T use any data stamped after T? Prove it:
   shift all input data forward one period; the signal must change accordingly. Common
   sins: same-bar close for a decision executed at that close; unlagged fundamentals
   (use report *publication* date, not period end); "as-of today" index membership.
2. **Survivorship** — universe must include delisted names with their death dates.
   A universe from today's constituents inflates long-only returns 1–4%/yr.
3. **Point-in-time joins** — every join to slow data (fundamentals, ratings) is an
   ASOF join on availability date.
4. **Costs** — commissions + spread + impact + borrow on shorts + slippage assumption
   documented. Zero-cost backtests are fiction; report net AND gross.
5. **Execution realism** — decide at T, execute at T+1 open (or worse). Never fill at
   the same price that generated the signal.
6. **Train/test contamination** — normalization, feature selection, hyperparameters
   chosen using any test-period data invalidates the test (see machine-learning skill).
7. **Multiple testing** — count every variant you tried (including abandoned ones).
   Deflate: with N trials, expected max Sharpe under the null grows ~ `sqrt(2 ln N / T_years)`.
   Report the trial count in the research note. Haircut or use White reality check.
8. **Regime honesty** — does the result survive splitting the sample in half? Removing
   the best 5 days? Excluding 2008/2020-style windows?

## Factor construction

- Winsorize cross-sectionally (1st/99th pct) → z-score or rank → neutralize what you
  don't want to bet on (sector, size, beta) via cross-sectional regression residuals.
- Information Coefficient: `IC_t = spearman(signal_t, forward_return_t)` per period.
  Report mean IC, IC t-stat (`mean/std * sqrt(N)`), IC decay by horizon. |mean IC| of
  0.02–0.05 with t>2 is a real single factor; 0.15 means you have a bug.
- Turnover: autocorrelation of the signal ranks; alpha must clear costs × turnover.

## Portfolio construction

- Start with rank deciles long-short (transparent, robust). Only then mean-variance:
  shrink the covariance (Ledoit–Wolf), constrain weights, penalize turnover.
- Position limits, gross/net exposure targets, and rebalance schedule are part of the
  strategy definition — record them with results.
- Vol targeting: scale to ex-ante vol using a rolling covariance; cap leverage.

## Evaluation (report all, net of costs)

CAGR, annualized vol, Sharpe (with SE ≈ `sqrt((1 + SR²/2)/T_years)`), Sortino, max
drawdown + duration, Calmar, hit rate, turnover, capacity sketch, exposure over time,
rolling 1y Sharpe plot. State the number of independent bets (breadth), not just years.

## Pre-registration discipline (how decisions stay honest)

- Any promote/reject/allocate decision gets its rule REGISTERED before results are
  computed: metric, population, significance bar, and what each outcome implies —
  written to the ledger/protocol doc first, dated.
- Verdicts are recorded regardless of sign. "FAIL — bar not met, sealed data stays
  sealed" is a first-class outcome and is written exactly as prominently as a pass.
- Attempt counts against the same bar are tracked; each re-attempt erodes the
  protection the bar provides and must be justified in the registration.
- Every count reconciles down the funnel (universe → eligible → priced → selected →
  evaluated) — a number that can't be traced through the funnel is not reported.
- Corrections to published numbers are dated addenda that state whether the verdict
  survives; never silent edits.

## Research note template (docs/notes/)

Hypothesis (economic rationale BEFORE looking at returns) → Data (source, range, PIT
guarantees) → Method → Results (net/gross table, plots from artifacts) → Leakage
checklist sign-off (all 8 items, explicitly) → Trials count → Limitations → Decision.

A result that fails any checklist item is not a result; it is a bug report.
