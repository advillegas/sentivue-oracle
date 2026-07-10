---
name: rust-quant
description: Rust for quantitative systems - project setup, numerics (ndarray/polars-rs), PyO3 bindings to the Python stack, low-latency patterns, property testing. Use when writing or reviewing Rust in this stack.
---

# Rust (quant systems)

Use Rust where Python is too slow after vectorization/Numba: order books, event engines,
tick processing, heavy simulations. Expose to research via PyO3 — Rust accelerates the
Python stack, it does not replace it.

## Project conventions

- `cargo new --lib crates/<name>`; workspace at repo root when >1 crate.
- Toolchain pinned via `rust-toolchain.toml`. Lints in `Cargo.toml`:
  `[lints.clippy] pedantic = "warn"`, deny `unwrap_used` in lib code.
- `cargo clippy --all-targets -- -D warnings` and `cargo test` gate every commit.
- Errors: `thiserror` for libraries, `anyhow` only in binaries. No panics on data paths;
  parse, don't validate (newtypes: `Price(f64)`, `Qty(i64)`, `SymbolId(u32)`).

## Numerics

- `f64` everywhere money flows; compare with explicit tolerance (`approx` crate).
  Represent prices in integer ticks (`i64`) inside matching/book code — float equality
  on price levels is a bug factory.
- `ndarray` for matrices, `polars` (rust) for frames, `rand` + `rand_distr` with a
  seeded `StdRng` for sims. Parallelism: `rayon` par_iter for path-parallel Monte Carlo.
- Timestamps: `i64` nanos since epoch UTC (matches Arrow/Polars); convert at the edges.

## Low-latency patterns (order books, engines)

- Preallocate: `Vec::with_capacity`, object pools; zero allocation on the hot path
  (verify with `#[global_allocator]` counting in tests or dhat).
- Book sides: `BTreeMap<TickPrice, Level>` is correct and fast enough to start;
  contiguous ladder (Vec indexed by tick offset) when profiling demands it.
- Prefer channels (`crossbeam`) and single-writer ownership over shared mutexes;
  the event loop owns state, everything else sends messages.
- Benchmark with `criterion`, profile before optimizing; correctness tests first.

## PyO3 bridge (the standard integration)

```toml
[lib] crate-type = ["cdylib", "rlib"]
[dependencies] pyo3 = { version = "0.23", features = ["extension-module"] }
numpy = "0.23"
```
```rust
#[pyfunction]
fn ewma(py: Python<'_>, x: numpy::PyReadonlyArray1<f64>, lam: f64) -> Py<numpy::PyArray1<f64>> {
    let x = x.as_array();
    let mut out = Vec::with_capacity(x.len());
    let mut m = x[0];
    for &v in x.iter() { m = lam * m + (1.0 - lam) * v; out.push(m); }
    numpy::PyArray1::from_vec(py, out).into()
}
```
Build into the uv env with `maturin develop --release -m crates/<name>/Cargo.toml`.
Release the GIL (`py.allow_threads`) around compute; accept/return NumPy or Arrow, never
Python lists for bulk data.

## Testing

- `proptest` for invariants: book add/cancel/match sequences preserve (qty ≥ 0,
  bid < ask, conservation of shares); PnL accounting round-trips.
- Golden tests against the Python reference implementation on shared fixtures —
  Rust and Python must agree to 1e-12 before the fast path ships.
