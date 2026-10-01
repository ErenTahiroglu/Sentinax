# User View Overlay

Phase 20 lets a user express explicit return beliefs as a Bayesian overlay on a base model, without ever mutating that
base model. It is split into two checkpoints.

| Checkpoint | Scope | Status |
|---|---|---|
| 20A | Expected-return prior + user-view contracts + confidence-to-uncertainty mapping | this document |
| 20B | Bayesian posterior expected-return overlay | IMPLEMENTED (see Phase 20B below) |

Sentinax is a decision-support system. Nothing here sends orders, ranks assets or recommends a trade.

## Methodological status

Sentinax Phase 20 is **Black-Litterman-LIKE Bayesian view integration**, not a claim that every input is the original
1992 Black-Litterman equilibrium model. The structure is:

```text
prior mean  +  prior uncertainty  +  linear views  +  view uncertainty  ->  Bayesian posterior (20B)
```

The repository has **no** market equilibrium, CAPM-implied or reverse-optimized returns, no market portfolio, no market-cap
weights and no risk aversion. None is invented. There is **no `tau`** anywhere, and
`AllocationCovarianceMatrix.sample_means` is never reinterpreted as equilibrium returns. A future prior-building phase may
populate the same `ExpectedReturnPrior` contract from a genuine equilibrium model without changing the user-view overlay.

```text
Base Expected-Return Prior ----- immutable, independently auditable
            |
            v
      User View Overlay
            |
            v
    Phase 20B Posterior
```

## Phase 20A

Module: `backend/engine/private/allocation_user_views.py` (pure Private Engine module, in the static-guard `PURE_MANIFEST`;
only Private Engine dependencies are `allocation_matrix` and the Phase 18B exact-sum helper). It calculates **no posterior**
and produces no weights, benchmark or recommendation.

### Terms that must not be confused

| Term | Meaning |
|---|---|
| **base expected-return prior** `m` | Explicit caller-supplied expected simple returns of the base model, one per instrument (`>= -1`, no upper cap, no clamping). |
| **prior expected-return uncertainty** `U` | The epistemic covariance of the expected-return estimate. |
| **historical return covariance** | The covariance of realized returns (`AllocationCovarianceMatrix`). `U` is **not** this matrix and is **not** silently `tau * Sigma`. |
| **user view** | A linear statement `E[p . R] = q` with loadings `p` and target `q`. |
| **user confidence** `c` | The user's explicit confidence in that one view, `0 < c <= 1`. |
| **view-noise variance** `omega` | The variance assigned to the view's error, derived from `c` and `U` (below). |

### `ExpectedReturnPrior`

Stores exactly `source` (an `AllocationReturnPanel`), `expected_returns` and `uncertainty_covariance`. The panel supplies
**instrument identity, canonical order and dimension only**; nothing (no sample average, covariance, forecast) is inferred
from its returns. `uncertainty_covariance` must be an exact tuple of tuples of exact finite `Decimal`, **exactly symmetric**
(never averaged or symmetrized) and **strictly positive definite**, checked by a deterministic LDL-transposed decomposition in
the 50-digit Decimal context: every pivot must be `> 0`. There is no numpy, eigenvalue clipping, nearest-PSD repair, jitter,
epsilon diagonal or pseudo-inverse. Singular, indefinite and zero-diagonal matrices are rejected. This rule applies only to
`U`: a rank-deficient historical return panel remains valid elsewhere.

### Views

`UserReturnViewKind` is exactly `ABSOLUTE` and `RELATIVE`. A `UserReturnView` stores `kind`, `loadings`, `target_return`,
`confidence` and is source-neutral until placed in a view set.

- **Absolute** view: exactly one nonzero loading, exactly `1` (e.g. `(0, 1, 0)` means `E[R_1] = q`); `q >= -1`, no upper bound.
- **Relative / group** view: at least one positive and one negative loading; the positive leg sums **exactly** to `+1` and
  the negative leg to **exactly** `-1` (context-free exact coefficient arithmetic), so the loadings sum to `0`.
  `(1, -1, 0)` means `E[R_0 - R_1] = q`; `(0.5, 0.5, -1)` means `E[0.5 R_0 + 0.5 R_1 - R_2] = q`. The first nonzero loading must
  be positive, so `(1, -1)` with `q = +0.02` is canonical and the sign-flipped `(-1, 1)` with `q = -0.02` is rejected: one
  economic view has one representation. `q` is a spread and has no `>= -1` bound.
- **Confidence** is the user's explicit input, not a model probability, forecast accuracy, p-value, LLM confidence or risk
  tolerance, and is never inferred from behavior or history. `0` is invalid (do not supply the view; it is not approximated
  by an epsilon). `1` is allowed and means an exact, noiseless view; it maps to `omega = 0` and is never silently weakened.
  Phase 20B must fail closed if such views make the update system singular or inconsistent.

### `UserReturnViewSet`

Stores `source` (the prior) and `views`. Every view needs exactly one loading per prior instrument (no truncation, padding or
lookup by guess). Views with identical loadings are rejected whatever their target or confidence, so one linear combination is
never double-counted as independent evidence. The constructor requires the canonical order (`ABSOLUTE` before `RELATIVE`,
then numeric lexicographic loadings); `build_user_return_view_set(*, prior, views)` sorts the **original** view instances
(identity preserved, nothing copied or altered), so no result depends on input order. Derived, non-stored views:
`view_matrix` (`P`), `view_targets` (`q`), `view_confidences`, `projected_prior_uncertainties`, `view_noise_variances` and
`view_noise_covariance` (`Omega`).

### Projected prior uncertainty and the confidence mapping

```text
s_k     = p_k U p_k'                    # projected prior uncertainty; full U, no diagonal shortcut, no historical covariance
omega_k = ((1 - c_k) / c_k) * s_k       # Sentinax canonical confidence-to-view-uncertainty mapping
Omega   = diag(omega_1 .. omega_K)      # view errors assumed conditionally independent
```

Higher confidence gives lower view-noise variance (`c = 1` gives `0`; `c -> 0+` grows without bound). Example: with
`s = 4`, `c = 1 -> 0`, `c = 0.5 -> 4`, `c = 0.25 -> 12`; for `U = diag(4, 9, 1)` and the view `(1, -1, 0)`, `s = 13` and at
`c = 0.5`, `omega = 13`. Off-diagonal entries of `U` change `s` and therefore `omega` (the same diagonal with `U_01 = 3`
gives `s = 7` instead of `13`). Because `U` is strictly positive definite and `p != 0`, `s > 0`; otherwise the build fails
closed (no epsilon). This is a defined Gaussian mapping, **not** an empirical probability calibration and **not** the Idzorek
algorithm (Idzorek-style confidence only motivates an intuitive confidence input). Correlated view errors are deferred.
For one isolated view, confidence controls how strongly a later Bayesian update responds relative to the prior uncertainty
along that view direction; 20A does not perform that update.

### Decimal authority

One fresh `Context(prec=50, ROUND_HALF_EVEN, Emin=MIN_EMIN, Emax=MAX_EMAX)` per calculation, passed explicitly; the ambient
context is never used and no module-global Context exists. A genuine Decimal overflow is the static error
`user-view analytics exceeds supported Decimal range`; nothing is clipped and no NaN/Infinity is returned.

### Explicit non-goals of 20A

```text
no posterior mean / posterior uncertainty / Bayesian gain / matrix inversion / linear solve (only the scalar p U p')
no tau, risk aversion, market portfolio, market-cap weights, reverse optimization or implied equilibrium returns
no allocation, benchmark, optimizer or CVaR call; no ranking, score, recommendation or buy / sell / hold
no persistence, migration, repository, user or owner identity, API or frontend
no LLM-generated or behavior-inferred confidence
```

## Phase 20B (IMPLEMENTED)

Module: `backend/engine/private/allocation_user_view_posterior.py` (pure Private Engine module, in the static-guard
`PURE_MANIFEST`; its only Private Engine dependency is `allocation_user_views`). Public entry point:

```python
build_bayesian_expected_return_posterior(*, view_set: UserReturnViewSet) -> BayesianExpectedReturnPosterior
```

It is a **Black-Litterman-LIKE Bayesian user-view overlay**, not the canonical Black-Litterman market-equilibrium model,
because Sentinax still has no market-equilibrium prior. There is no `tau`, risk aversion, market weight or historical
covariance input, and the output is **no allocation**: it is a belief about expected returns, not weights.

### Equations

With `m` the prior expected-return vector, `U` the prior expected-return uncertainty covariance, `P` the view matrix, `q` the
view targets and `Omega` the diagonal view-noise covariance of Phase 20A (all read through one `UserReturnViewSet`):

```text
S      = P U P' + Omega                 innovation covariance, full U, one symmetric calculation path
d      = q - P m                        view innovation (zero is valid)
m_post = m + U P' S^-1 d                posterior expected returns
U_post = U - U P' S^-1 P U              posterior expected-return uncertainty
```

`U_post` is the posterior epistemic covariance of the expected-return **estimates**. It is not the historical return
covariance, a future realized-return covariance or a portfolio covariance, and nothing is added to it.

### Square-root Joseph posterior covariance (PSD by construction)

The mathematical model is the **Joseph form**:

```text
K      = U P' S^-1                    (K = Z' with Z = S^-1 P U, because S and U are symmetric; from the same LDL solves)
A      = I - K P
U_post = A U A' + K Omega K'
```

The direct subtraction `U - U P' S^-1 P U` is not used, and neither is a finite-precision test that asks an already rounded
covariance matrix whether it "looks" positive semidefinite. The covariance is instead **materialized from an explicit factor**:

```text
U       = C C'                 C = L sqrt(D) from the strictly positive definite prior's LDL-transposed decomposition
F_prior = A C                  F_prior F_prior' = A U A'
F_noise = K sqrt(Omega)        F_noise F_noise' = K Omega K'      (sqrt(0) = 0 exactly for confidence 1)
F       = [ F_prior | F_noise ]            N x (N + K)
U_post  = F F'
```

The factor entries are produced by the 50-digit analytical Decimal arithmetic. The **Gram materialization `F F'` itself is
context-free exact coefficient arithmetic**: every Decimal entry is split with `Decimal.as_tuple()` into an integer
coefficient and a base-10 exponent, products are exact integer products, terms are aligned exactly to one common exponent and
summed as integers, and the result is built directly from sign, digits and exponent. No ambient context, no second analytics
context, no `Fraction` and no rounding take part. Each entry `i <= j` is computed once and mirrored; an exactly zero sum is
plain `Decimal("0")`, never signed zero. Because the stored covariance is exactly the Gram matrix of a finite Decimal factor it is
positive semidefinite **by construction**, so there is no separate approximate LDL acceptance test that could reject the
canonical posterior; the result constructor recomputes the canonical posterior and rejects anything that differs from it.

Exact alignment is bounded by `_EXACT_GRAM_MAX_DECIMAL_PLACES = 1000`, a **representation / memory resource ceiling** (checked
before any `10 ** shift`; exceeding it raises the static range error). It is not an analytics precision, a PSD tolerance or an
accuracy claim. Gram entries are exact sums of products of 50-digit factors, so they may carry up to roughly 200 significant digits.

The posterior numbers are the deterministic finite-precision representation of the Gaussian model; they are **not** claimed to
be exact real-arithmetic Gaussian outputs, and the construction does not claim immunity from every finite-precision effect
(factor entries still carry 50-digit rounding, e.g. `sqrt` of a non-square variance). Epsilon, tolerance, eigenvalue clipping,
jitter, nearest-PSD and repair are not used anywhere.

### Solve, not invert

`S^-1` is never formed (no inverse, pseudo-inverse, determinant or adjugate). `S` is factored by a deterministic
LDL-transposed decomposition (`S = L D L'`, unit lower `L`, no row reordering) in the 50-digit Decimal context, and
`S y = d` and `S Z = P U` are solved by forward, diagonal and back substitution; then `m_post = m + U P' y` and
`U_post = U - (U P') Z`. Every `U_post[i][j]` with `i <= j` is computed once and mirrored, so it is exactly symmetric.

### Strictly positive definite `S` and dependent views

Every LDL pivot of `S` must be `> 0`, otherwise the build fails closed with
`user-view posterior system is singular or non-positive-definite` (no jitter, epsilon diagonal, dropped view, lowered
confidence, pseudo-inverse or least squares). This can legitimately happen with several exact (confidence `1`, zero noise)
views that are linearly dependent, e.g. `A` exact, `B` exact and `A - B` exact over two assets: individually valid but
redundant noiseless equations. The canonical policy is to **fail closed**. Algebraic dependence of `P` alone is **not** the
criterion: with enough noise (confidence `< 1`) the same three views give a strictly positive definite `S` and a valid
posterior.

### Posterior support and geometry

- Posterior expected returns are expected simple returns, so each must be finite and `>= -1`
  (`user-view posterior expected return violates simple-return support`); there is no clipping, rescaling or confidence
  change. The bound can be crossed through prior correlation (a view on one asset moving a correlated asset).
- `U_post` is the exact Gram matrix of the factor above, hence symmetric and positive semidefinite by construction. Unlike the
  prior it may be singular and may have zero diagonal entries: a full-confidence view removes all uncertainty along its
  direction. Exact zeros are normalized to `Decimal("0")`.

### Single-view semantics and invariants

For one isolated view with `s = p U p'` and `omega = ((1 - c) / c) s`:

```text
p m_post     = p m + c (q - p m)         c = 1 -> p m_post = q ;  c = 0.5 -> midpoint ;  c = 0.25 -> 25% of the innovation
p U_post p'  = (1 - c) s                 c = 1 -> 0 (singular PSD posterior, allowed)
```

The posterior uncertainty is **independent of the targets** `q` (same prior, `P` and confidence give an identical `U_post`;
only the mean moves). A zero innovation (`q = P m`) leaves the mean unchanged but still reduces the uncertainty. A view on one
asset moves other assets through the prior correlations `U P'`; the same diagonal with different off-diagonal `U` changes the
unviewed assets. View order never matters (Phase 20A canonical order). Derived, non-stored diagnostics:
`projected_prior_returns` (`P m`), `view_innovations` (`d`), `innovation_covariance` (`S`), `posterior_view_returns`
(`P m_post`; noisy views need not equal `q`) and `posterior_shift` (`m_post - m`, the overlay). The prior and the views are
retained by identity and never mutated.

### Decimal authority and regression evidence

One fresh `Context(prec=50, ROUND_HALF_EVEN, Emin=MIN_EMIN, Emax=MAX_EMAX)` per canonical calculation; the ambient context is
never used. A genuine analytical overflow **and** an exact-Gram representation-ceiling breach are both the static error
`user-view posterior analytics exceeds supported Decimal range`. The result constructor recomputes the canonical posterior and
rejects forged means or covariances (including a valid PSD matrix that is not the canonical posterior).

Regression evidence (not a universal guarantee): the two former false rejections (two exact absolute views on a correlated
prior; an exact relative view next to a noisy relative view) now build, and a deterministic corpus of 600 structurally valid
three-asset fixtures (strictly positive definite priors constructed as `L L'`, Phase 20A-valid absolute / relative views,
confidences in `{1, 0.7, 0.5, 0.25}`, no linearly dependent all-noiseless system) builds 600 / 600 with zero false rejections,
where the previous Joseph-subtraction form rejected 43 of the same 600. Validity of each case comes from its construction, not
from asking Phase 20B whether it succeeds. Genuinely singular innovation systems (e.g. `A`, `B` and `A - B` all exact) still fail
closed at the factorization of `S`.

### Explicit non-goals of 20B

```text
no tau, risk aversion, market weights, reverse optimization, CAPM or implied equilibrium returns
no historical return panel or covariance access; no allocation, benchmark, optimizer or CVaR call
no ranking, score, recommendation or buy / sell / hold
no persistence, API or frontend
```

## Deferred

| Checkpoint | Scope |
|---|---|
| Phase 21 | Cash-first rebalance |
| later | A principled relationship between prior uncertainty and historical covariance; correlated view errors; a tolerance policy for exact-view directions |
