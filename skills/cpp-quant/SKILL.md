---
name: cpp-quant
description: Modern C++ (20/23) for quant infrastructure - CMake presets, numerics, pybind11 bindings, low-latency idioms, sanitizers and testing. Use when writing or reviewing C++ in this stack.
---

# C++ (quant infrastructure)

Reach for C++ when interfacing existing C++ libraries (QuantLib internals, exchange
SDKs) or when a Rust crate isn't an option. Otherwise prefer Rust for new native code —
say so in the ledger when making the call.

## Project conventions

- C++20 minimum. CMake ≥ 3.28 with presets; single-config Ninja:

```json
// CMakePresets.json (configurePresets entry)
{ "name": "release", "generator": "Ninja", "binaryDir": "build/release",
  "cacheVariables": { "CMAKE_BUILD_TYPE": "RelWithDebInfo",
    "CMAKE_CXX_STANDARD": "20", "CMAKE_EXPORT_COMPILE_COMMANDS": "ON" } }
```
- Warnings are errors: `-Wall -Wextra -Wpedantic -Wconversion -Werror`.
- Dependencies via FetchContent pinned to tags (offline: vendored under `third_party/`).
- clang-format + clang-tidy configs committed; CI target `make test` runs ctest.

## Correctness defaults

- Ownership: `unique_ptr` by default; raw pointers are non-owning views; no `new`.
- `std::span`, `std::string_view` at API boundaries; `const` everything viewable.
- No exceptions on the hot path; `std::expected<T, Error>` (C++23) or status returns.
- Money/prices as integer ticks (`int64_t`); floating point only for analytics; never
  `==` on doubles — `std::abs(a-b) <= tol * std::max(std::abs(a), std::abs(b))`.
- Time: `std::chrono` with explicit clocks; store UTC nanos (`int64_t`), never locale time.

## Low-latency idioms

- Measure first: Google Benchmark + `perf`/Instruments; optimize the measured hot spot only.
- Data layout beats cleverness: SoA over AoS for scans, contiguous vectors, reserve.
- Avoid virtual dispatch in inner loops — CRTP or `std::variant` + `std::visit`.
- SPSC ring buffer for thread handoff; cache-line align producers/consumers
  (`alignas(64)`); false sharing shows up as mysterious 10x slowdowns.
- `[[likely]]/[[unlikely]]` sparingly and only after profiling confirms.

## Sanitizers & testing (non-negotiable)

- Debug CI runs: `-fsanitize=address,undefined`; TSan build for anything threaded.
- Tests: Catch2 or GoogleTest via ctest. Property-style tests with rapidcheck for
  book/accounting invariants; golden files against the Python reference.
- Every numeric kernel gets: a hand-computed case, an edge case (empty, single, NaN),
  and a cross-check vs Python/`scipy` to 1e-12 on shared fixtures.

## pybind11 bridge

```cpp
PYBIND11_MODULE(fastlib, m) {
  m.def("ewma", [](py::array_t<double, py::array::c_style> x, double lam) {
      auto r = x.unchecked<1>(); std::vector<double> out; out.reserve(r.shape(0));
      double mavg = r(0);
      for (py::ssize_t i = 0; i < r.shape(0); ++i) { mavg = lam*mavg + (1-lam)*r(i); out.push_back(mavg); }
      return py::array_t<double>(out.size(), out.data());
  }, py::call_guard<py::gil_scoped_release>());   // release GIL around compute
}
```
Build with scikit-build-core into the uv env; accept NumPy arrays, never Python lists
for bulk data. Match Rust/PyO3 API shape so callers can swap implementations.
