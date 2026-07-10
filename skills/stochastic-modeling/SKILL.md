---
name: stochastic-modeling
description: SDE simulation and Monte Carlo engineering - GBM/OU/Heston/jump processes, discretization schemes, variance reduction, calibration, path-dependent pricing. Use for simulation studies, scenario generation, or MC pricing.
---

# Stochastic Modeling & Monte Carlo

## Process toolbox (with exact/robust discretizations)

- **GBM** `dS = μS dt + σS dW` — simulate in logs, exactly:
  `S_{t+Δ} = S_t exp((μ − σ²/2)Δ + σ√Δ Z)`. Never Euler on the price directly.
- **OU / Vasicek** `dx = κ(θ − x)dt + σ dW` — exact:
  `x_{t+Δ} = θ + (x_t − θ)e^{−κΔ} + σ sqrt((1 − e^{−2κΔ})/(2κ)) Z`.
- **CIR** `dv = κ(θ − v)dt + σ√v dW` — use full truncation Euler
  (`v⁺ = max(v,0)` inside drift and diffusion) or exact noncentral-χ² sampling;
  check the Feller condition `2κθ ≥ σ²` and report if violated.
- **Heston** — Quadratic-Exponential (QE) scheme for the variance, correlate with the
  asset via `dW_S = ρ dW_v + sqrt(1−ρ²) dW_⊥`; Euler on Heston variance WILL go negative.
- **Merton jumps** — Poisson `N(λΔ)` count per step, lognormal jump sizes; add
  compensator `−λ(e^{m+δ²/2}−1)Δ` to the drift so the discounted process stays a martingale.

## Simulation engineering

- RNG: `np.random.default_rng(seed)` (PCG64); for parallel paths use
  `rng.spawn(n)` — never the same seed with different offsets.
- Shape discipline: simulate `(n_paths, n_steps)` float64; vectorize across paths;
  Numba `@njit(parallel=True)` for path-dependent payoffs that can't vectorize.
- Antithetic variates (pair Z with −Z) — free 30–50% variance cut for monotone payoffs.
- Control variates: use the closed-form asset (e.g. vanilla BS price under GBM) as
  control for exotic payoffs; report the variance-reduction factor.
- Sobol (`scipy.stats.qmc.Sobol`) + Brownian bridge for high-dim integrals; scramble,
  and use 2^k sample counts.
- Convergence: report MC standard error `std/√n`; halve-step-size test for weak error
  of the scheme; plot log-error vs log-n (slope −1/2) once per new engine.

## Martingale & sanity tests (unit tests, not eyeballs)

- Discounted asset mean: `E[e^{−rT} S_T] = S_0 e^{−qT}` within 3 SE.
- Vanilla reprice: MC vs Black–Scholes within 3 SE for several strikes/maturities.
- OU stationary distribution: mean θ, var σ²/(2κ) at large T.
- Jump model: realized jump frequency ≈ λ.

## Calibration

- Moment matching for OU/GBM from historical data (report half-life `ln 2/κ`).
- Risk-neutral: minimize squared vol-surface error, weight by vega, regularize
  parameter drift day-over-day; check parameter identifiability (Heston κ and σ_v
  trade off badly — fix or bound one if the surface is sparse).
- Always simulate from calibrated parameters and re-fit as a round-trip test.

## Scenario generation for risk

- Block bootstrap (stationary bootstrap, mean block ≈ 20d) preserves autocorrelation
  and fat tails without a parametric model; use for stress paths.
- Correlated multi-asset: Cholesky on a shrunk (Ledoit–Wolf) correlation; for tails use
  a t-copula (fit ν) rather than Gaussian.
- Label every scenario set with generator hash + seed; persist as Parquet.
