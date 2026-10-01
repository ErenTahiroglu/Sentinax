# User View Overlay

Phase 20 lets a user express explicit return beliefs as a Bayesian overlay on a base model, without ever mutating that
base model. It is split into two checkpoints.

| Checkpoint | Scope | Status |
|---|---|---|
| 20A | Expected-return prior + user-view contracts + confidence-to-uncertainty mapping | this document |
| 20B | Bayesian posterior expected-return overlay | deferred |

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

## Phase 20B formula (DEFERRED, not implemented)

With `m` the prior expected-return vector, `U` the prior uncertainty covariance, `P` the view matrix, `q` the view targets and
`Omega` the view-error covariance, Phase 20B intends to compute

```text
S      = P U P' + Omega
m_post = m + U P' S^-1 (q - P m)
U_post = U - U P' S^-1 P U
```

`U` is **not** silently replaced with the historical return covariance. A future prior-building phase may choose a principled
relationship between expected-return uncertainty and historical covariance; 20A does not.
