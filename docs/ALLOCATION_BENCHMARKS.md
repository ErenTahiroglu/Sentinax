# Allocation Benchmarks

Phase 18 builds deterministic allocation benchmarks for the Private Investment Decision Engine. It is split into
checkpoints so that each mathematical layer is locked before the next one depends on it.

| Checkpoint | Scope | Status |
|---|---|---|
| 18A | Aligned monthly return panel + exact Decimal sample covariance | this document |
| 18B | Equal weight + inverse volatility | see Phase 18B below |
| 18C | Equal risk contribution (risk parity) | see Phase 18C below |
| 18D | Hierarchical risk parity | see Phase 18D below |
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

All analytical arithmetic (division, `sqrt`, inverse-volatility raw weights, normalization) runs in ONE fresh
`Context(prec=50, ROUND_HALF_EVEN, Emin=MIN_EMIN, Emax=MAX_EMAX)`; the ambient context is never used and no second
Decimal context exists. The sum of the stored weights is then closed to exactly `1` with context-free integer
arithmetic on the exact finite representations produced by that context:

```text
weight_i  = coefficient_i x 10^exponent_i                 # from Decimal.as_tuple()
common    = min(exponent_i, 0)
residual  = 10^(-common) - sum_i(coefficient_i x 10^(exponent_i - common))     # integers only
```

The residual is added to exactly one preliminary weight: the largest, and the lowest canonical source index on ties
(instrument order is the Phase 18A canonical UUID-string order, so the rule is input-order independent). The closed
weight is constructed exactly from sign, digits and exponent; nothing is rounded. A zero residual leaves every weight
untouched. This is numerical representation closure only. It is not an economic preference or an allocation signal.
Every closed weight must be finite with `0 < w <= 1` and the exact total must be `1`.

**Resource / representation ceiling.** Exact closure aligns weights at their common exponent, which costs integer
memory proportional to the exponent spread. `_MAX_CLOSURE_DECIMAL_PLACES = 1000` bounds that spread in base-10 places.
It is a memory/representation ceiling only; it is not an analytical precision, numeric accuracy or a Decimal
calculation precision. A spread beyond it (a pathological volatility ratio) fails closed with
`allocation benchmark exceeds supported Decimal analytics range`. The tiny weight is never rounded, dropped, zeroed,
clamped or floored.

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

## Phase 18C

Module: `backend/engine/private/allocation_risk_parity.py` (pure Private Engine module, in the static-guard
`PURE_MANIFEST`; its only Private Engine dependencies are the Phase 18A `allocation_matrix` and the Phase 18B
`allocation_benchmarks`, both already in the manifest).

A true Equal Risk Contribution (ERC / risk parity) benchmark over the **full** Phase 18A covariance matrix. It is a
long-only, unlevered, descriptive benchmark: not a recommendation, trade instruction or optimizer output.

### Definition

For weights `w_i > 0`, `sum(w_i) = 1`, and portfolio variance `V = w' Sigma w > 0`:

```text
RRC_i = w_i (Sigma w)_i / V          # relative risk contribution
ERC   : RRC_i = 1 / N for every asset (within the numerical tolerance)
```

### Difference from Inverse Volatility

Inverse Volatility (18B) uses only `diag(Sigma)`. ERC uses the diagonal and the off-diagonal covariance, so two
sources with identical variances but different covariance produce identical Inverse Volatility weights and different
ERC weights (tested on three assets). For a diagonal covariance matrix, and for two assets with any admissible
correlation, the ERC solution coincides with Inverse Volatility. Sample means are never read and there is no
expected-return input, leverage, target volatility, custom risk budget, constraint, HRP or CVaR logic.

### Solver: cyclical coordinate descent

Standard risk-budgeting formulation with equal budgets `b_i = 1 / N`. Find a positive `x` with
`x_i (Sigma x)_i = b_i`; the weights are `w_i = x_i / sum(x)`.

```text
initialization:  w0 = Inverse Volatility weights (18B), sigma0 = sqrt(w0' Sigma w0) > 0, x_i = w0_i / sigma0
coordinate i:    a = Sigma_ii,  c = sum_{j != i} Sigma_ij x_j,  b = 1 / N
                 solve a x_i^2 + c x_i - b = 0 for the positive root
                 d = sqrt(c^2 + 4ab)
                 x_i = 2b / (c + d)      if c >= 0
                 x_i = (d - c) / (2a)    if c <  0        # cancellation-free branches
```

Coordinates are updated sequentially (`i = 0 .. N-1`) within a cycle, never in parallel. There is no randomness.

### Convergence

The initialization is checked first, so `cycles = 0` is valid (diagonal and some constant-correlation cases).
Afterwards convergence is tested only after each **complete** cycle (`cycles += 1`, then the full state):

```text
max_i | RRC_i(x) - 1/N | <= _ERC_RELATIVE_TOLERANCE = 1E-24
```

`RRC` is invariant to positive scaling of `x`, so the unnormalized auxiliary vector is used directly.
`_ERC_RELATIVE_TOLERANCE` is a **numerical solver tolerance**, not economic significance, data accuracy, forecast
confidence or an allocation threshold. `_ERC_MAX_CYCLES = 10000` is a deterministic **computational resource cap**,
not a financial threshold.

### Decimal analytics and exact stored-weight closure

All iterative analytics run through one fresh `Context(prec=50, ROUND_HALF_EVEN, Emin=MIN_EMIN, Emax=MAX_EMAX)` passed
explicitly; the ambient context is never used. `x / sum(x)` is then closed to an exact Decimal sum of `1` with the
reviewed Phase 18B coefficient closure (`_close_to_one`, `_sums_to_exactly_one`). The reuse is intentional
package-internal reuse; it is not duplicated and Phase 18B is unchanged. After closure the risk contributions are
recomputed from the **stored** weights; if the stored portfolio misses the tolerance the result is `NON_CONVERGENCE`
and the pre-closure vector is never claimed.

### Unavailable semantics

```text
ZERO_VARIANCE            any Sigma_ii == 0 (no epsilon floor, dropped asset, zero weight or inverse-volatility fallback)
DEGENERATE_RISK_GEOMETRY a positive-weight portfolio has w' Sigma w <= 0, e.g. two perfectly anti-correlated
                         equal-volatility assets; relative risk contributions are undefined, none are fabricated
NON_CONVERGENCE          the cycle cap is reached, or the stored weights miss the tolerance; no partial weights
```

A genuine Decimal analytical overflow is a distinct static error
(`equal risk contribution exceeds supported Decimal analytics range`), never `NON_CONVERGENCE`.

### Result

`EqualRiskContributionResult` stores exactly `source`, `weights`, `cycles`, `unavailable_reason`. It retains the source
by identity, aligns `weights[i]` with `source.instrument_ids[i]` and recomputes the complete canonical solve on
construction, rejecting forged weights, cycles or reasons. Derived (non-stored) diagnostics: `portfolio_variance`
(`w' Sigma w`), `portfolio_volatility` (`sqrt(V)`, no annualization), `relative_risk_contributions` (never clipped or
made absolute) and `max_relative_risk_contribution_error`; all are `None` for an unavailable result. Their sum is 1
within arithmetic tolerance and they are diagnostics, not stored allocations.

### Explicit non-goals of 18C

```text
no expected returns, leverage, borrowing, target volatility
no custom or group / factor risk budgets (equal budgets only)
no min/max weights, turnover, sector or transaction-cost constraints, no solver package
no HRP: correlation distance, clustering, linkage, quasi-diagonalization, recursive bisection
no VaR / CVaR / Expected Shortfall, scenario optimization
```

## Phase 18D

Module: `backend/engine/private/allocation_hrp.py` (pure Private Engine module, in the static-guard `PURE_MANIFEST`;
its only Private Engine dependencies are `allocation_matrix` and `allocation_benchmarks`). It does not use the
Phase 18C ERC module.

One canonical Hierarchical Risk Parity benchmark with no options (no linkage, distance or split variants):

```text
covariance -> correlation -> correlation distance -> distance-of-distance -> single-linkage clustering
           -> quasi-diagonal order -> recursive bisection -> cluster inverse-variance allocation -> exact weights
```

No expected returns, no matrix inversion, no quadratic optimizer, no clustering library.

### Correlation, distance and distance-of-distance

```text
rho_ij = Sigma_ij / sqrt(Sigma_ii * Sigma_jj)     (i != j, one calculation per pair => rho_ij == rho_ji)
rho_ii = 1                                          (exactly, not recomputed)
d_ij   = sqrt((1 - rho_ij) / 2),   d_ii = 0
D_ij   = sqrt( sum_k (d_ki - d_kj)^2 ),   D_ii = 0   # Euclidean distance between correlation-distance profiles
```

`D` (not `d`) is the clustering input; there is no normalization and no Ward transform. Every off-diagonal `rho_ij`
must lie in `[-1, 1]`; a value outside is `INVALID_CORRELATION_GEOMETRY` and is never clipped, capped or made absolute.
Perfect correlation (`rho = +1`, distance 0) and perfect anti-correlation (`rho = -1`, distance 1) are valid.

### Single linkage and deterministic tie-breaking

Bottom-up single linkage only. Leaves are `0 .. N-1` in canonical instrument order; merged clusters receive ids
`N, N+1, ...` in merge order. `distance(A, B) = min D_ij` over `i in A, j in B`, maintained with the exact recurrence
`distance(C, X) = min(distance(A, X), distance(B, X))`. Each step selects the pair by the exact key

```text
(linkage_distance, min(cluster_id_a, cluster_id_b), max(cluster_id_a, cluster_id_b))
```

lexicographically, so ties never depend on dict, set or hash order or on a library. The smaller id is always the
**left** child and the larger the **right** child; orientation is never changed by variance, size or distance.

### Quasi-diagonal order and recursive bisection

The order is the left subtree then the right subtree of the final merge, expanded to leaf indices, with no post-sort.
Recursive bisection then splits that **order** (not the dendrogram branch sizes) with `split = len(group) // 2`,
`left = group[:split]`, `right = group[split:]` (for odd lengths the left half is the smaller one, e.g.
`(0,1,2,3,4) -> (0,1) | (2,3,4)`). Complete levels are processed left to right and only non-singleton children are
carried to the next level. Internal weights are mapped back to canonical `source.instrument_ids` order.

### Inverse-variance cluster allocation and split factor

HRP uses inverse **variance**, not the Phase 18B inverse **volatility**:

```text
raw_i = 1 / Sigma_ii         (NOT 1 / sqrt(Sigma_ii))
q_i   = raw_i / sum(raw)
V(C)  = q' Sigma_C q          # full covariance inside the cluster
alpha = V_right / (V_left + V_right),   left weights *= alpha,   right weights *= 1 - alpha
```

For a diagonal covariance matrix HRP therefore equals the global inverse-variance portfolio `w_i ~ 1 / Sigma_ii`, which
differs materially from Inverse Volatility whenever the variances are unequal.

### Singular covariance and unavailable semantics

Singular (rank-deficient) covariance is supported: there is no determinant, inverse, positive-definiteness or
`T > N` requirement. HRP is available whenever every variance is positive, the correlation geometry is valid and every
child cluster variance used in a split is positive.

```text
ZERO_VARIANCE                any Sigma_ii == 0 (correlation undefined; no epsilon, dropped asset or fallback)
INVALID_CORRELATION_GEOMETRY a covariance-derived correlation outside [-1, 1]
DEGENERATE_CLUSTER_VARIANCE  a child cluster variance <= 0 (e.g. a perfectly anti-correlated equal-variance pair);
                             no 0/100 split and no epsilon variance
```

A genuine Decimal analytical overflow or exact-closure resource failure is the distinct static error
`hierarchical risk parity exceeds supported Decimal analytics range`, never an availability reason.

Known limitation: the correlation check is exact and does not tolerate rounding noise. Perfectly collinear series whose
variances have no finite 50-digit representation could, in principle, produce a correlation a few units in the 50th digit
outside `[-1, 1]` and therefore be reported as `INVALID_CORRELATION_GEOMETRY` (a fail-closed result, never a wrong
weight). This is deliberate: nothing is clipped.

### Decimal context and exact stored-weight closure

One fresh `Context(prec=50, ROUND_HALF_EVEN, Emin=MIN_EMIN, Emax=MAX_EMAX)` is passed through the whole build; the
ambient context is never used. Preliminary weights are closed to an exact Decimal sum of `1` with the reviewed Phase 18B
context-free coefficient closure (`_close_to_one`, `_sums_to_exactly_one`), reused intentionally and not duplicated.
`HierarchicalRiskParityResult` stores `source`, `weights` and `unavailable_reason`, retains the source by identity and
recomputes the complete canonical build on construction, rejecting forged weights, reasons or pairings. The derived,
non-stored views `quasi_diagonal_order` and `ordered_instrument_ids` are `None` for an unavailable result.

### Explicit non-goals of 18D

```text
no expected returns, Black-Litterman, user views or conviction
no matrix inversion / pseudo-inverse / determinant / Cholesky
no quadratic optimizer, objective, gradient, solver, minimum variance, mean variance, CVaR
no configurable linkage (ward / average / complete), distance, split policy, cluster count
no score, rank, confidence or recommendation
```

## Deferred

| Checkpoint | Scope |
|---|---|
| 19 | CVaR optimizer |
