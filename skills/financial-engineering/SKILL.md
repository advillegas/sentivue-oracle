---
name: financial-engineering
description: Derivatives pricing, curves, Greeks, and market conventions with QuantLib and closed forms - day counts, discounting, vol surfaces, xVA-lite. Use when pricing instruments, building curves, or computing risk sensitivities.
---

# Financial Engineering

Library: `QuantLib` (Python bindings in the env). Closed forms in `scipy` where simpler.

## Conventions before mathematics

Wrong conventions dwarf model error. Always pin down, per instrument and currency:
- **Day count**: ACT/360 (USD money market), ACT/365F (GBP), 30/360 (US corp bonds),
  ACT/ACT ISDA (governments). Never default silently.
- **Calendars & roll**: settlement lag (T+1/T+2), modified-following, month-end rule.
- **Compounding**: money-market simple vs annual vs continuous — convert explicitly and
  test round-trips.
- **Quote units**: bond yield vs clean/dirty price; vol quoted annualized in %, variance
  in decimal; rates in % vs bp. Store decimals internally, format at the edge.

## Black–Scholes block (keep a tested closed form next to QuantLib)

```python
from math import log, sqrt, exp
from scipy.stats import norm
def bs(S, K, T, r, q, sig, call=True):
    d1 = (log(S/K) + (r - q + sig*sig/2)*T) / (sig*sqrt(T)); d2 = d1 - sig*sqrt(T)
    s = 1 if call else -1
    price = s*(S*exp(-q*T)*norm.cdf(s*d1) - K*exp(-r*T)*norm.cdf(s*d2))
    delta = s*exp(-q*T)*norm.cdf(s*d1)
    gamma = exp(-q*T)*norm.pdf(d1)/(S*sig*sqrt(T))
    vega  = S*exp(-q*T)*norm.pdf(d1)*sqrt(T)          # per 1.00 of vol, not per %
    theta_call = (-S*exp(-q*T)*norm.pdf(d1)*sig/(2*sqrt(T))
                  - s*r*K*exp(-r*T)*norm.cdf(s*d2) + s*q*S*exp(-q*T)*norm.cdf(s*d1))
    return price, delta, gamma, vega, theta_call
```

Sanity identities to unit-test any pricer: put–call parity `C - P = S e^{-qT} - K e^{-rT}`;
monotonicity in vol; gamma/vega ≥ 0 for vanillas; price → intrinsic as `T→0`.

## Curves

- Bootstrap piecewise curves from deposits/futures/swaps; log-linear on discount factors
  is the robust default; monotone-convex if smooth forwards matter.
- Post-2021 world is OIS/SOFR-discounted: separate projection vs discounting curves.
- Test: repricing the bootstrap instruments must return input quotes to <0.1 bp.

## Vol surfaces

- Equity: fit implied vol in (log-moneyness, T); enforce no calendar/butterfly arbitrage
  (Durrleman condition). SVI per expiry is the standard parametrization; check
  `w(k) = a + b(ρ(k−m) + sqrt((k−m)² + σ²))` fits with `b(1+|ρ|) ≤ 4/T` style constraints.
- Rates: SABR per expiry×tenor; beta fixed by desk convention (0.5 common), calibrate
  `alpha, rho, nu`.
- Never interpolate premium; interpolate vol (total variance in time).

## Greeks in practice

- Bump-and-revalue: central differences, bump 1% rel spot, 1bp rates, 1 vol pt;
  re-use the same random numbers/seeds for MC Greeks (common random numbers) or use
  pathwise/likelihood-ratio estimators.
- Report Greeks in cash terms (per 1% move) for risk reports; per-unit for calibration.

## QuantLib gotchas

- `ql.Settings.instance().evaluationDate` is global mutable state — set it explicitly
  at the top of every script and test.
- Handles (`RelinkableYieldTermStructureHandle`) enable live re-link for scenario runs.
- Python objects can be GC'd out from under C++ — keep references to curves/quotes alive.

Definition of done: pricer + parity/limit unit tests + convention doc in the docstring
(day count, calendar, settlement) + ledger entry.
