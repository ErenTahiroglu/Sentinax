# Allocation Benchmarks

Phase 18 builds deterministic allocation benchmarks for the Private Investment Decision Engine. It is split into
checkpoints so that each mathematical layer is locked before the next one depends on it.

| Checkpoint | Scope | Status |
|---|---|---|
| 18A | Aligned monthly return panel + exact Decimal sample covariance | this document |
| 18B | Equal weight + inverse volatility | see Phase 18B below |
| 18C | Equal risk contribution (risk parity) | deferred |
| 18D | Hierarchical risk parity | deferred |
| 19 | CVaR optimizer | deferred |

Sentinax is a decision-support system. These benchmarks never send orders and never recommend a trade.

## Phase 18A

Module: `backend/engine/private/allocation_matrix.py` (pure Private Engine module, in the static-guard
`PURE_MANIFEST`).

Phase 18A produces the mathematical input of later benchmarks:

```text
canonical multi-instrument monthly return panel
sample arithmetic means
sample covariance matrix
```

It produces **no portfolio weights**.

### Source-neutral mathematical boundary

The module consumes an explicit, generic monthly-return contract. It is deliberately not tied to TEFAS, BIST or
global EOD contracts. Adapters from authoritative market-data series into this contract are deferred.

It is **not** a market-data resolver and makes no source-authority claim:

```text
source / point-in-time resolution occurs upstream
Phase 18A validates mathematical structure only
```

Caller-provided returns are never authoritative merely because they pass these checks. There are no provider,
resolver, database or network calls. The legacy Monte-Carlo MVO under `backend/deprecated` is not imported or revived.

### Monthly alignment semantics

Months are identified by an economic calendar month, `AllocationMonth(year, month)`. A trading date, month-end date,
timezone or publication date is never fabricated, because different markets close on different calendar days.

An instrument series needs at least 2 monthly return observations (the mathematical minimum for a sample variance;
not a statistical sufficiency policy). Within a series months must be strictly increasing, without duplicates, and
consecutive.

All instruments of a panel must carry exactly the same month sequence. A missing month is a hard failure:

```text
no intersection
no forward fill / backfill / interpolation / nearest month
no sorting or repair inside constructors
```

The panel requires canonical instrument order (ascending UUID string). `build_allocation_return_panel` may
canonicalize that order; it never alters return values, months or points, so input order does not change the result.

### Return values

`simple_return` is an exact `Decimal` (no float, int, bool, string or Decimal subclass), finite, and at least `-1`.
Zero is a valid observed return.

### Decimal context

Every top-level calculation uses a fresh context:

```python
decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=decimal.MIN_EMIN, Emax=decimal.MAX_EMAX)
```

The ambient `getcontext()` is never read for arithmetic, set or mutated, and no module-global mutable context exists.
Only genuine `decimal.Overflow` is translated, into the static error
`allocation covariance exceeds supported Decimal analytics range`. `NaN`, `Infinity` and floats are never returned.
numpy, pandas, scipy, sklearn and `statistics` are not used.

### Sample mean and covariance

For instrument `i` with `T` returns:

```text
sample_mean_i = sum_t(r_it) / T
cov_ij        = sum_t[(r_it - mean_i)(r_jt - mean_j)] / (T - 1)
```

The denominator is the sample `T - 1`, with `T >= 2`. There is no weighting, EWMA, shrinkage, Ledoit-Wolf or Bayesian
estimator. `sample_means` is a descriptive arithmetic mean, not an expected-return forecast; there is no
annualization or geometric mean.

`AllocationCovarianceMatrix` retains its `AllocationReturnPanel` by object identity and, on construction, recomputes
the canonical result through the same private helper as the builder. Forged means, matrix values, dimensions,
asymmetry or instrument order are rejected; lists are rejected in favour of exact tuples. The matrix is symmetric by
construction (each off-diagonal value is computed once and mirrored).

### Zero variance and negative covariance

A constant return series has variance `0`. That is a valid observed value, not "unavailable". Whether zero variance
is admissible for weighting is decided by later layers (inverse volatility, risk parity), not here.

Negative off-diagonal covariance is valid. It is never clipped to zero, made absolute, projected to a
positive-semidefinite matrix, eigenvalue-repaired or replaced by a nearest-correlation matrix.

### Explicit non-goals of 18A

```text
no weights, equal weight, inverse volatility, risk parity, ERC, HRP
no minimum-variance / mean-variance / CVaR / optimizer
no expected return, alpha, forecast, Sharpe, Sortino, utility, risk-free rate
no correlation or distance matrix (HRP semantics are locked separately before 18D)
no recommendation, score, rank, rebalance or target allocation
no persistence, migration, repository or frontend
```

## Phase 18B

Module: `backend/engine/private/allocation_benchmarks.py` (pure Private Engine module, in the static-guard
`PURE_MANIFEST`; its only Private Engine dependency is the Phase 18A `allocation_matrix`).

Two closed-form, long-only, descriptive benchmarks over an `AllocationCovarianceMatrix`. They are not
recommendations, optimizer outputs, suitability decisions, tactical tilts or trade instructions.

### Equal Weight

```text
w_i = 1 / N
```

Always mathematically available for any valid Phase 18A covariance source, including a single asset and a source
with a zero-variance asset. Two assets give exactly `0.5 / 0.5`. Three assets have no finite Decimal form for `1/3`,
so the stored values differ only by the residual closure below.

### Inverse Volatility

```text
sigma_i = sqrt(Sigma_ii)
raw_i   = 1 / sigma_i
w_i     = raw_i / sum_j(raw_j)
```

Inverse Volatility is **not** Equal-Risk-Contribution / risk parity. It uses only the covariance diagonal and
deliberately ignores off-diagonal covariance, correlation, marginal portfolio risk and risk contribution; those begin
in 18C. Two sources with the same variances but different covariance or correlation produce identical weights. Zero
or negative off-diagonal covariance is accepted and changes nothing. No annualization is applied: all assets share the
monthly frequency and a common scaling cancels in the normalization. Sample means are never read; these are
risk/structure benchmarks, not expected-return strategies.

### Zero-volatility semantics

Phase 18A admits variance `0` as a valid observed value. If **any** `Sigma_ii == 0`, `1 / sigma_i` is undefined and the
Inverse Volatility result is unavailable:

```text
weights            = None
unavailable_reason = ZERO_VOLATILITY
```

No epsilon, floor, dropped asset, 100% allocation, zero weight or carried-forward volatility is substituted. Undefined
is not zero. Equal Weight can never be unavailable.

### Decimal context and exact-sum residual closure

Preliminary weights use a fresh `Context(prec=50, ROUND_HALF_EVEN, Emin=MIN_EMIN, Emax=MAX_EMAX)`; the ambient context
is never used. The sum of weights is then closed to exactly `1`:

```text
residual = 1 - sum(preliminary_weights)      # exact arithmetic
```

The residual is added to exactly one preliminary weight: the largest, and the lowest canonical source index on ties
(instrument order is the Phase 18A canonical UUID-string order, so the rule is input-order independent). This is
numerical representation closure only. It is not an economic preference or an allocation signal. Every closed weight
must be finite with `0 < w <= 1` and the exact total must be `1`; otherwise the calculation fails closed with
`allocation benchmark exceeds supported Decimal analytics range` (no clamping). The closure arithmetic runs in a second
fresh exact context that traps `Inexact`, so a pathological volatility ratio fails closed instead of being rounded.

`AllocationBenchmarkResult` retains its source by identity, aligns `weights[i]` with `source.instrument_ids[i]`, and
recomputes the canonical result on construction, rejecting forged weights, order, method, availability or reason.

### Explicit non-goals of 18B

```text
no ERC / risk parity / risk contribution / marginal risk
no portfolio volatility, correlation or distance matrix
no optimizer, objective, solver, minimum variance, mean variance, CVaR
no expected return, Sharpe, Sortino
no rank, score, recommendation, rebalance, target or tactical tilt
```

## Deferred

| Checkpoint | Scope |
|---|---|
| 18C | Equal-Risk-Contribution (risk parity) benchmark |
| 18D | Hierarchical Risk Parity benchmark (correlation / distance semantics locked first) |
| 19 | CVaR optimizer |
