# CVaR Optimization

Phase 19 builds a minimum-CVaR allocation in two checkpoints so that the mathematical authority is locked before any
numerical solver depends on it.

| Checkpoint | Scope | Status |
|---|---|---|
| 19A | Exact historical portfolio scenarios + Rockafellar-Uryasev CVaR objective for GIVEN weights | this document |
| 19B | Minimum-CVaR linear-program optimizer | deferred |

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

## Planned Phase 19B formulation (DEFERRED, not implemented)

The first optimizer will be pure minimum-CVaR over the same scenarios (Rockafellar-Uryasev linear program):

```text
decision variables:   w_i   (i = 1..N),   zeta,   u_t   (t = 1..T)

objective:            minimize   zeta + 1 / [T (1 - alpha)] * sum_t u_t

constraints:          u_t >= L_t(w) - zeta        for every scenario t
                      u_t >= 0
                      sum_i w_i = 1
                      w_i >= 0
```

The 19A objective and canonical minimum are the exact reference a 19B solution must reproduce for its returned weights.
Solver choice, dependency and numerical tolerances are decided in 19B.
