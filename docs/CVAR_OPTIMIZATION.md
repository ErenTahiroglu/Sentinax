# CVaR Optimization

Phase 19 builds a minimum-CVaR allocation in two checkpoints so that the mathematical authority is locked before any
numerical solver depends on it.

| Checkpoint | Scope | Status |
|---|---|---|
| 19A | Exact historical portfolio scenarios + Rockafellar-Uryasev CVaR objective for GIVEN weights | this document |
| 19B | Minimum-CVaR linear-program optimizer | IMPLEMENTED (see Phase 19B below) |

Sentinax is a decision-support system. Nothing here sends orders or recommends a trade.

## Phase 19A

Module: `backend/engine/private/allocation_cvar.py` (pure Private Engine module, in the static-guard `PURE_MANIFEST`; its
only Private Engine dependencies are `allocation_matrix` and the Phase 18B exact-sum helper). Standard library only: no
numpy, pandas, scipy, cvxpy, pulp or highspy.

Phase 19A answers: given one Phase 18A aligned monthly return panel, one exact long-only weight vector and one explicit
confidence level, what are the portfolio scenario returns, the scenario losses, the Rockafellar-Uryasev objective
`F_alpha(zeta)` and its exact empirical minimum over `zeta`. It does **not** choose weights and contains no optimizer.

### Source: the return panel, not the covariance matrix

CVaR is scenario/tail based. A covariance matrix cannot reconstruct the historical loss distribution, so 19A consumes the
`AllocationReturnPanel` and never touches `AllocationCovarianceMatrix`, `covariance` or `sample_means`.

### Loss sign convention and equal scenarios

Every aligned calendar month of the panel is one equally weighted historical scenario, in exactly the panel period order
(no sorting, bootstrap, resampling, Monte Carlo, synthetic stress or recency weighting):

```text
R_t(w) = sum_i w_i * r_it          # no compounding, no annualization
L_t(w) = -R_t(w)                   # loss; a profitable scenario has a NEGATIVE loss, never floored at zero
```

A negative zero is canonicalized to plain `Decimal("0")`. Because every Phase 18A simple return is `>= -1` and the
weights are long-only with sum 1, every loss is `<= 1`; there is no lower bound on a loss.

### Weights

`CVarPortfolioWeights(source, weights)` requires exact `Decimal` weights with `0 <= w_i <= 1`, a mathematically exact
sum of `1` (the context-free Phase 18B coefficient arithmetic, no ambient Decimal context) and length equal to the panel
instrument count. **Zero weights are valid** (unlike the strictly positive ERC/HRP benchmarks): no instrument is dropped,
nothing is renormalized and the source stays full-dimensional. This prepares the corner solutions of a later LP.

### Rockafellar-Uryasev objective

For a loss variable `L` and confidence `alpha`:

```text
CVaR_alpha(L) = min_zeta [ zeta + 1/(1 - alpha) * E[(L - zeta)+] ]
```

For `T` equally weighted scenarios the authority is:

```text
F_alpha(zeta) = zeta + sum_t max(L_t - zeta, 0) / (T * (1 - alpha))
```

`evaluate_cvar_objective(scenarios, confidence_level, threshold)` evaluates this at a caller-specified finite `zeta`
(which may be negative). The `0` in `max(L_t - zeta, 0)` is the mathematical positive part; it is not missing-data
substitution. `confidence_level` must be an exact `Decimal` with `0 < alpha < 1`; there is no default and nothing is
hard-coded to 0.95.

### Canonical threshold minimization

`F_alpha` is convex and piecewise linear with breakpoints at the scenario losses, so its minimum is attained at one of
them. `calculate_historical_portfolio_cvar` evaluates `F_alpha` at every unique scenario loss (ascending) and takes the
minimum objective value. If several thresholds attain exactly the same minimum, the **smallest threshold** is reported.
The `threshold` is therefore one canonical minimizer; the minimum CVaR value is the economic authority.

Hand fixture: losses `[-0.10, 0, 0.10, 0.20]`, `alpha = 0.75`, so `1/(T(1-alpha)) = 1`:

```text
F(-0.10) = 0.50    F(0) = 0.30    F(0.10) = 0.20    F(0.20) = 0.20
CVaR = 0.20, threshold = 0.10   (0.10 and 0.20 tie; the smaller wins)
```

### Difference from Phase 16J

Phase 16J (`fund_expected_shortfall`) is a single-fund, rolling-cumulative-return measure using nearest-rank VaR and the
mean of every loss `>= VaR` (ties included). Phase 19A is multi-asset, portfolio-weight dependent and defined by the
Rockafellar-Uryasev objective minimum; it does not import or reuse Phase 16J. In the fixture above, the tail mean at the
selected threshold would be `0.15`, whereas the objective minimum is `0.20`; the tail-mean rule is never substituted.

### Negative CVaR and value range

The CVaR value must be `<= 1` and has no finite lower bound. A portfolio whose every historical scenario is profitable has
a **negative** CVaR (a loss measure below zero); this is valid and is never floored.

### Decimal authority

Sums, products and divisions use one fresh `Context(prec=50, ROUND_HALF_EVEN, Emin=MIN_EMIN, Emax=MAX_EMAX)` passed
explicitly; the ambient context is never used and no module-global Context exists. Negation uses the exact, context-free
`Decimal.copy_negate`. Only a genuine `decimal.Overflow` is translated, into
`portfolio CVaR exceeds supported Decimal analytics range`. Results retain their source by identity and recompute their
canonical value on construction, rejecting forged scenarios, thresholds and CVaR values.

### Explicit non-goals of 19A

```text
no LP / solver / optimizer / decision-variable search / optimal weights
no minimum-return, target-return, mean-return or Sharpe constraint, no utility
no turnover, fees, tax, current portfolio or rebalance (Phase 21)
no user views, Black-Litterman or conviction (Phase 20)
no bootstrap, Monte Carlo, random or synthetic stress scenarios
```

## Phase 19B (IMPLEMENTED)

Module: `backend/engine/private/allocation_cvar_optimizer.py`. Public entry point:

```python
optimize_minimum_cvar_portfolio(*, return_panel: AllocationReturnPanel, confidence_level: Decimal) -> MinimumCVarOptimizationResult
```

A long-only, fully invested minimum historical CVaR optimizer. No expected-return constraint, return target, turnover,
fees, holdings, user views, short selling or leverage; the solver configuration is canonical and not configurable.

### Two numerical domains

```text
economic authority   Phase 19A: exact Decimal scenarios, exact weights, exact Rockafellar-Uryasev objective
numerical engine     SciPy linprog, HiGHS dual simplex, binary64
```

The solver output is a **candidate**. The public `weights`, `threshold` and `conditional_value_at_risk` are reconstructed
and validated through Phase 19A. The raw solver `x`, the raw solver `zeta`, the slacks `u_t` and the raw solver objective are
**not** stored and are **not** the economic authority (the solver objective is only a post-validation cross-check).
The module is deliberately **outside** the static-guard `PURE_MANIFEST` because it crosses from exact Decimal to
binary64; Phase 19A stays in it. Targeted G1-G5 are still zero on the optimizer module.

### Linear program

Variables, in this exact order, for `N` assets and `T` scenarios (dimension `N + 1 + T`):

```text
x = [ w_0 .. w_(N-1), zeta, u_0 .. u_(T-1) ]

minimize     zeta + 1 / [T (1 - alpha)] * sum_t u_t        (cost of every w_i is 0)
subject to   u_t >= L_t(w) - zeta ,  L_t(w) = -sum_i r_it w_i
             sum_i w_i = 1
bounds       0 <= w_i <= 1 ,  zeta in (-inf, +inf) ,  u_t >= 0
```

In SciPy form `A_ub x <= b_ub`, scenario row `t` is `-r_it` on every weight column, `-1` on the `zeta` column, `-1` on its own
`u_t` and `0` on every other `u`, with `b_ub = 0`. `A_eq` has a single row of ones on the weight columns with `b_eq = 1`.
The `zeta` bound is passed explicitly as `(None, None)`. The program is continuous (no integrality), has no regularizer, no
epsilon coefficient and no tie-break objective.

### Solver configuration

```text
method = "highs-ds"                      (no automatic solver selection)
presolve = True
primal_feasibility_tolerance = 1e-9
dual_feasibility_tolerance   = 1e-9
simplex_dual_edge_weight_strategy = "steepest-devex"
maxiter = 100000
disp = False
no time_limit                            (acceptance never depends on machine speed)
```

These are numerical solver tolerances and a deterministic resource cap. They are not financial thresholds, risk
tolerances, allocation preferences or confidence levels. Only options documented for SciPy's `highs-ds` interface are used.
SciPy is pinned in `backend/requirements.txt` as `scipy==1.18.0` (the root `requirements.txt` remains a pure proxy and
`highspy` is not added) because the result constructor canonically re-runs the solver and must see identical behavior.

### Binary64 boundary and support envelope

Decimal reaches binary64 only through `_decimal_to_solver64` (the decimal string; no built-in `float`, no ambient
Decimal arithmetic). Every coefficient must be finite. Every **nonzero** return coefficient and the generated objective
coefficient `1 / [T (1 - alpha)]` must additionally satisfy `1e-9 <= |value| <= 1e15` after conversion (exact zero is valid
zero). Otherwise the input is refused with `minimum CVaR solver input exceeds supported binary64 range` before the solver is
called. This conservative envelope is not an economic filter: a small coefficient is never rounded up or zeroed and a
large one is never clipped. Consequently the exact Phase 19A domain is a superset of the supported optimizer domain: an
extreme confidence level (objective scale above `1e15`) can be evaluated exactly but is not optimized.

### Raw candidate validation and exact reconstruction

1. Accept only `status == 0`, `success is True`, a finite `x` of length `N + 1 + T` and a finite objective; every other
   state (status 1 iteration limit, 2 infeasible, 3 unbounded, 4 numerical failure, malformed result) fails with
   `minimum CVaR solver did not return an optimal solution`. No partial weights are returned and volatile HiGHS message text
   is never the error authority.
2. A raw weight in `[-1e-9, 0]` becomes exact `Decimal("0")` (signed zero included); in `[1, 1 + 1e-9]` exact
   `Decimal("1")`; outside `[-1e-9, 1 + 1e-9]` it fails exact post-validation. Other weights use `Decimal(str(binary64))`.
3. A zero-allowing exact closure (Phase 18B context-free coefficient helpers): zeros stay zero and the residual `1 - sum` is
   added to the largest weight (lowest canonical index on ties). The residual must be `<= 1e-8` beforehand; a materially
   infeasible candidate is never force-closed.
4. The closed weights pass the Phase 19A `CVarPortfolioWeights` constructor; the scenarios are rebuilt exactly (solver slacks
   are never used as losses); Phase 19A supplies the public `threshold` (its smallest-minimizer rule) and CVaR.
5. The exact CVaR must agree with the solver objective within `1e-8`.
   Any failure raises `minimum CVaR solver candidate failed exact post-validation`; Phase 19A range errors are preserved.

### Non-unique optimum

The returned weights are **one solver-selected optimal candidate**. The model does not claim the weight vector is
mathematically unique: identical assets or CVaR plateaus have several optimal vectors with the same exact CVaR, and no
secondary objective, epsilon perturbation or economic tie-break (minimum turnover, closest to equal weight, minimum norm)
is added. The result constructor re-runs the canonical optimization and rejects forged results; this is canonical
implementation identity under the pinned solver, not a claim that other optimal portfolios are economically inferior.

### Explicit non-goals of 19B

```text
no expected-return, target-return or mean constraint, no Sharpe / utility
no turnover, fees, tax, holdings, cash-first logic (Phase 21)
no Black-Litterman, user views or conviction (Phase 20)
no short selling, leverage, cardinality or lot constraints
no Monte Carlo, bootstrap or random portfolio search
no covariance, sample means, ERC / HRP / benchmark dependence in production (tests may compare against them)
```
