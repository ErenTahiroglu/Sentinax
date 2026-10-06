# Private backtest candidate eligibility replay (Phase 26D2)

`backend/engine/private/backtest_candidate_eligibility_replay.py` is the **second Phase 26D deterministic decision replay slice**. The narrow question: given these explicitly
supplied strategic sleeves, did every positive-weight candidate have PIT-admissible candidate-universe membership at this replay frontier?

## Contract

`replay_private_backtest_candidate_eligibility(*, input_bundle, sleeves)` returns a frozen `PrivateBacktestCandidateEligibilityReplay` with exactly three fields: `input_bundle`,
`sleeves` and `eligibility_binding` (a closed `CandidateUniverseSleeveBinding`). Nothing the bundle already carries is duplicated, and there is no status, hash, score or decision id.
Exact types: an exact bundle, an exact `tuple` of exact `CrossAssetSleeve` objects (no list, set, generator or subclass), retained by identity and never reconstructed.

- **Requires a COMPLETE C2E bundle** with no missing requirement; there is no partial replay and no missing candidate evidence is filled in.
- **Exact manifest:** exactly one PORTFOLIO_HISTORY requirement plus one or more CANDIDATE_UNIVERSE requirements, nothing else (Game Changer, macro, risk, market-data and
  user-view requirements are rejected because D2 would ignore their evidence). Zero candidate-universe requirements is rejected: it never means "all candidates eligible".
- **Sleeves are required and non-empty.** Ordering and structure are validated by the closed authority; D2 never sorts caller sleeves.

## Fixed replay policy semantics (conditional, counterfactual)

The sleeves are explicit **fixed replay policy parameters**. They are **not claimed** to be historically persisted settings, a historically approved allocation, historically
available information or the strategy actually used at that date. D2 is a conditional (counterfactual) replay: had this sleeve configuration been evaluated at this replay
point, would Phase 22B have admitted every candidate using only PIT-safe historical universe evidence? No strategy-configuration history is implemented.

## One universe per sleeve; C2E order is not sleeve order

C2E orders candidate evidence by requirement key; Phase 22B needs universes in the sleeves' AssetClass order. D2 maps the bundle's resolutions by exact `AssetClass` identity
(exactly one per sleeve; a missing, duplicate or undeclared asset class is rejected; no source preference, no first-wins), then builds a transient tuple of the **existing**
resolution objects in sleeve order. This is representation alignment only: resolutions are preserved by object identity, nothing is selected or cloned, and the bundle is not reordered.

## Closed Phase 22B is the sole eligibility authority

D2 calls `bind_candidate_universes_to_sleeves(sleeves=..., universes=...)` exactly once and copies none of its rules (SELECTED status, one shared evaluation date and PIT context,
sleeve/universe AssetClass correspondence, positive-weight candidate membership).

- **Non-SELECTED evidence cannot establish eligibility.** NO_SOURCE_SNAPSHOT, NO_EFFECTIVE_SNAPSHOT, NO_SNAPSHOT_AS_OF and FRONTIER_CONFLICT are present historical evidence for C2E but
  fail closed here through Phase 22B. Unavailable never becomes empty, a fallback source, another snapshot or the current universe.
- **The selected-empty state is distinct.** A SELECTED universe with no members is a valid declared state; a sleeve with positive-weight candidates fails Phase 22B membership. It is not
  reinterpreted as missing data.
- **CURATED_CANDIDATES claim limit:** inclusion in a selected curated set can bind a candidate; absence from it never means global ineligibility and no complete investable-universe
  claim is made. **COMPLETE_MEMBERSHIP claim limit:** it stays an upstream claim; D2 does not prove provider completeness.
- **Context and date authority:** each candidate binding must carry the very analysis context object, its query the very analysis PIT context and the replay evaluation date. No
  current date and no new frontier. Direct construction re-proves this.

## Validation and determinism

One private validation path serves the builder and `__post_init__`: exact types, COMPLETE and surface, one resolution per sleeve asset class, then the identity of the closed binding's
sleeves and universes (the very supplied sleeves, the very bundle resolutions in sleeve order). An independently constructed canonical `CandidateUniverseSleeveBinding` over those same
objects is accepted; equal-valued cloned resolutions, reordered universes or a foreign binding are rejected. Output is a pure function of the immutable bundle and sleeves.

## Not here

Portfolio history is only the replay anchor retained through the bundle (no holding, transaction or value is read; candidates are not filtered by holdings). No candidate discovery,
ranking or weight change; no cross-asset composition and no `CrossUniverseAuthority`; no rebalance; no Game Changer composition (D1 stays independent); no user-view or optimizer path;
no market data, so C2C2 remains deferred; no execution or performance. Later slices require an independent Red Team first.

D4B consumes the exact D2 sleeves only after proving the D2 and D3C sides share the same replay analysis context and exact portfolio projection binding (`docs/PRIVATE_BACKTEST_CROSS_UNIVERSE_COMPOSITION.md`).
D2 remains candidate eligibility only.
