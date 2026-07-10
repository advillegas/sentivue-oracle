---
name: statistical-modeling
description: Statistical and econometric modeling - time-series (ARIMA/GARCH/state-space), stationarity, cointegration, robust inference, hypothesis-testing honesty, Bayesian basics. Use when modeling, forecasting, or testing relationships in data.
---

# Statistical & Econometric Modeling

Stack: `statsmodels`, `arch`, `scipy.stats`, `sklearn`. Financial data is non-stationary,
fat-tailed, autocorrelated, and regime-switching — defaults that assume IID Gaussian lie.

## Before any model

- Plot the series, its returns/differences, ACF/PACF of both, and rolling mean/vol.
- Stationarity: ADF (H0: unit root) AND KPSS (H0: stationary) — agreement matters;
  disagreement usually means near-unit-root or structural break (check with CUSUM).
- Returns: use log returns; document if arithmetic. Prices are I(1); model changes,
  not levels — unless you're explicitly doing cointegration.

## Time series

- **ARIMA**: order via AIC over a small grid + PACF sanity; residuals must pass
  Ljung–Box (no autocorrelation) and be homoskedastic — if vol clusters, you need GARCH.
- **GARCH (arch package)**: GJR-GARCH(1,1,1) with skew-t errors is the workhorse for
  daily equity vol: `arch_model(r*100, p=1, o=1, q=1, dist="skewt")` (scale to % for
  optimizer stability). Check persistence α+β+γ/2 < 1; forecast with `.forecast(reindex=False)`.
- **State space / Kalman** (`statsmodels.tsa.statespace`): time-varying beta, local
  level trend extraction; EM or MLE for hyperparameters; log-likelihood comparisons.
- **Cointegration**: Engle–Granger for pairs (test residual stationarity, use
  MacKinnon critical values); Johansen for baskets. Half-life of the spread from an
  OU fit decides tradability. Re-test rolling — cointegration dies.

## Inference that survives finance data

- HAC (Newey–West) standard errors for any regression on overlapping or autocorrelated
  data: `OLS(...).fit(cov_type="HAC", cov_kwds={"maxlags": int(1.5*horizon)})`.
- Overlapping-horizon regressions inflate t-stats — Hansen–Hodrick or block bootstrap.
- Bootstrap CIs: stationary block bootstrap (block ≈ 20d) for Sharpe/IC/drawdown stats.
- Sharpe difference between two strategies: Ledoit–Wolf test, not eyeballing.
- Multiple hypotheses: Benjamini–Hochberg FDR across a factor zoo; record the full
  trial count (see quant-research skill item 7).

## Distributional honesty

- Fit and report tails: Student-t ν for returns is typically 3–6; Gaussian VaR
  understates 99% risk badly. Use Cornish–Fisher or historical/EVT (POT with GPD)
  for tail quantiles; backtest VaR with Kupiec + Christoffersen tests.

## Bayesian (when priors genuinely help)

- Shrinkage is the point: hierarchical priors across assets for betas/ICs stabilize
  small samples. Conjugate/analytic first; `scipy` + hand-rolled Gibbs for the rest —
  keep it simple offline. Report posterior predictive checks, not just posteriors.

## Reporting standard

Every model artifact ships: spec (formula/orders/priors), sample period, diagnostics
table (residual tests), parameter table with robust SEs, out-of-sample check, and the
falsification you attempted. "Significant in-sample" is not a finding.
