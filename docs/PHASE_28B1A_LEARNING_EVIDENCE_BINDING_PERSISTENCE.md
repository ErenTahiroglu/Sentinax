# Phase 28B-1A — Offline Learning Evidence Binding & Durable Persistence

Baseline: `784808e7730bf2d048221182dd7cc5b41846b086`. Offline only: no live collection, no labels, no models, no financial decision.

## 1. What is stored, what is referenced

| Data | Where | Notes |
|---|---|---|
| Raw provider payload, `payload_hash`, `retrieved_at`, provider, endpoint | existing `public.raw_provider_snapshots` | Referenced by foreign key, never copied. No second raw store exists. |
| Evidence binding (declared source metadata + references + verification columns) | new `public.learning_evidence_bindings` (migration `030`) | Append-only, system-scoped (no owner column). |
| Universe snapshots, observation claims | **not persisted in this checkpoint** | Phase 28B-0 semantics are unchanged; persistence is deferred to a later, separately reviewed checkpoint. |

## 2. Audit of the existing raw store (real PostgreSQL 16, fresh replay of migrations 001-029)

- `anon`, `authenticated` and `service_role` hold every table privilege, including `TRUNCATE`, on `raw_provider_snapshots` (Supabase-style default grants; nothing in 001-029 narrows them). Row-level UPDATE and DELETE are stopped only by trigger `trg_protect_raw_snapshot_immutability`.
- That trigger's function was absent from the live project once (migration 028 reported it missing), so the guarantee cannot be assumed live.
- `TRUNCATE ... CASCADE` by `service_role` succeeds on the raw store today.
- The raw table does not validate that `payload_hash` matches `raw_payload`: a forged well-formed hash is accepted.
- `payload_hash` uses Python's canonical JSON hashing (`compute_payload_hash`). PostgreSQL cannot recompute it (JSONB key order and number/Unicode formatting differ), so the database cannot prove payload integrity.

Smallest corrections made (all in migration 030, additive, not touching unbound rows or Phase 1-27 semantics):

1. A bound snapshot is protected by `guard_learning_bound_raw_snapshot()` independently of the original trigger: only `is_superseded`/`superseded_at` may change; deletion is refused.
2. Because the binding table references the raw store and no role may `TRUNCATE` the binding table, `TRUNCATE ... CASCADE` of the raw store is denied to `service_role` (permission denied on the dependent table). A superuser/owner can still act; that is outside this threat model.
3. No grant on pre-existing tables was changed by migration 030. The pre-existing `TRUNCATE` grant was reported and then closed by migration 031 (section 12); the missing hash validation on the raw table remains reported, not altered.

## 3. Evidence-binding contract

`EvidenceBindingDeclaration` (pure) pairs a `SourceAuthorityRecord` (with `raw_snapshot_id`) and a `TemporalProvenance` with the expected provider/endpoint and an explicit `revision`. `adapters.verify_evidence_binding` fails closed (`EvidenceBindingError`) unless:

1. the retained record exists and is exactly a `RawProviderSnapshotRecord`;
2. its id is the referenced snapshot;
3. provider and endpoint equal the declared source identity;
4. a payload is retained (`raw_payload` not null; an external `storage_ref` alone cannot be re-hashed);
5. the retained payload re-hashes (closed `compute_payload_hash`) to its stored hash and to the declared hash;
6. the declared retrieval instant is the retained record's `retrieved_at` (an economic date can never substitute for it).

A `VerifiedEvidenceBinding` can only be issued by that function. The repository (`backend/engine/learning_store`) verifies every declaration before sending anything, then makes one atomic RPC call.

## 4. Verification levels (what each proves)

| Level | Status | Proves | Does not prove |
|---|---|---|---|
| stored-content integrity | verified (writer re-hash) | the retained payload re-hashes to the declared hash | provider authenticity |
| record-reference existence | verified (database) | the referenced snapshot row exists | anything about its truth |
| source-identity binding | verified (database) | provider and endpoint equal the declared identity | that the provider is who it claims |
| document-assertion verification | constant UNVERIFIED | nothing | that a cited document contains an asserted text |
| licensing/access verification | constant UNVERIFIED | nothing | any permission |
| economic-completeness verification | constant UNVERIFIED | nothing | that a list is complete |
| publication-time authority | constant UNVERIFIED | nothing | when anything was first published |

In the database the content-integrity column reads `WRITER_REHASH_ASSERTED`: a trusted `service_role` writer that bypasses the Python verifier is only checked against the stored column, not against the payload. This trust boundary is tested explicitly, not hidden.

## 5. Temporal semantics

Kept separate: `economic_date`, `publication_time` (only with an evidence hash), `snapshot_retrieved_at` (copied from and checked against the retained record), `capture_attempted_at` (<= retrieval), and `recorded_at` (database-assigned by trigger; a client-supplied value is overwritten). `recorded_at` is not the provider retrieval time: data imported today for an earlier period is newly imported historical data. Nothing infers original publication time.

## 6. Idempotency, conflicts, revisions, atomicity

- Natural idempotency key `(raw_snapshot_id, source_id, revision)`; no random or time-derived keys.
- Same key, identical content: reported `EXISTING`, no new row. Same key, different content: rejected (`unique_violation`). A change requires revision n+1; revisions form a contiguous chain by a composite foreign key (no gaps, no orphans).
- Concurrent identical submissions yield one `INSERTED` and the rest `EXISTING` (tested with 12 simultaneous writers).
- The RPC runs in one transaction: any failing item rolls back the whole batch. The Python repository additionally sends nothing if any declaration fails verification.

## 7. Database permissions and immutability

- RLS enabled, no policy; `anon`/`authenticated`/`PUBLIC` hold no privilege; `service_role` holds only `SELECT` and `INSERT`. `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER` are revoked from everyone.
- Triggers also reject UPDATE/DELETE/TRUNCATE for owners and superusers; constants are enforced by CHECK constraints (`capture_authorized = false`, the four UNVERIFIED columns).
- All four new functions are `SECURITY INVOKER` with a pinned `search_path`; trigger functions have no caller EXECUTE; the RPC is executable only by `service_role`. No `SECURITY DEFINER` function is added, so the Phase 27 inventory is unchanged.
- A frozen Python dataclass is not treated as a database guarantee; everything above is exercised against real PostgreSQL.

## 8. Claims that remain unverified

`CURATED_PILOT` and `OBSERVED_LIST` are not complete universes; `SOURCE_CLAIMED_COMPLETE` stays a source assertion; `NOT_OBSERVED_IN_RESPONSE` establishes no absence; `EXPLICITLY_UNAVAILABLE_BY_SOURCE` stays unverified; no missing-to-zero conversion exists. None of these are persisted or upgraded here.

## 9. Licensing boundary and why no collection occurs

`capture_authorized` is constant false in the contract and the table. Storing a licensing document hash records a declaration only; it grants no permission. Takasbank General Letter 2154 (6 July 2026) and its comparison table were supplied by the project owner and are not encoded or relied on here; public API licensing implications and the consolidated 20.07.2026 procedure remain unverified. Nothing in this checkpoint contacts TEFAS, KAP or any provider.

## 10. Dependency direction

`learning_store -> learning` and the closed private storage type, via injected reader/transport callables. The pure `learning` package imports neither the store nor any database driver; `learning_store` contains no driver, clock, SQL or raw-store write. Guards live in `backend/tests/test_learning_boundary.py`.

## 11. Next source-access requirements (not started)

An independent verifier for referenced licensing/access evidence; a source-access and operational-limit policy for any automated collection; verification of the consolidated 2026 Takasbank procedure; and, separately, persistence of universe and observation claims.

## 12. Post-28B-1A hardening H1: legacy PIT store TRUNCATE closure (migration 031)

Finding, measured on disposable PostgreSQL 16 with Supabase-style default grants (`TRUNCATE` bypasses RLS and does not fire row DELETE triggers):

| Stage | `TRUNCATE raw_provider_snapshots` (RESTRICT) | `... CASCADE` | `TRUNCATE normalized_observations` (RESTRICT and CASCADE) |
|---|---|---|---|
| through migration 029 | denied, SQLSTATE `0A000` (dependency error) | **allowed** for anon, authenticated, service_role | **allowed** for all three roles |
| through migration 030 | denied, `0A000` | denied, `42501`, only because the dependent binding table grants nobody TRUNCATE (indirect; also with zero bindings) | **allowed** for all three roles |
| after migration 031 | denied, `42501` (the role no longer holds the privilege) | denied, `42501` | denied, `42501` |

The cascade closure of the raw store reaches `macro_observations`, `normalized_observations`, `sec_filings`, `sec_raw_facts`, `sec_fact_filing_links` (and the binding table since 030).

- **Reachability:** direct SQL sessions only. The Supabase Data API (PostgREST) exposes no TRUNCATE verb, no function in the public schema truncates, and no public REST exploit was established. Live-project privilege state was not inspected and may differ from this replay.
- **Fix:** migration 031 is two table-scoped `REVOKE TRUNCATE ... FROM PUBLIC, anon, authenticated, service_role` statements. SELECT, INSERT, UPDATE (including system supersession) and all other privileges, ownership, triggers, policies, constraints, RLS and default privileges are unchanged (asserted by a before/after fingerprint test). Re-running it is a no-op.
- **Not changed (residual):** `macro_observations`, `sec_filings`, `sec_raw_facts`, `sec_fact_filing_links` and other public tables still carry the default `TRUNCATE` grant; the global default privileges still grant `TRUNCATE` on future tables (the binding table needed an explicit revoke); owner/superuser paths are out of scope; the row-level `DELETE` grant on the legacy tables remains behind the existing triggers.

Trust boundaries, restated:

- **A. Source identity.** `source_identity_binding = VERIFIED_BY_DATABASE` means only that the stored provider and endpoint equal the declared provider and endpoint. The caller-chosen `source_id` and `source_reference` are neither authenticated nor mapped to a canonical source registry; different `source_id` values may reference the same raw snapshot (test `test_different_sources_may_bind_the_same_snapshot_independently`).
- **B. Content integrity.** The Python adapter re-hashes the retained payload with the existing canonical function. The database only compares the declared hash with the stored column and does not recompute it. `WRITER_REHASH_ASSERTED` is a writer assertion, never an independent database hash verification; the direct-`service_role` bypass test (`test_forged_valid_looking_hash_is_stopped_by_the_python_verifier_and_the_db_trust_boundary_is_explicit`) remains valid.
- **C. Temporal authority.** Economic date, publication evidence, retrieval time, capture-attempt time and database recording time stay distinct. Neither historical source publication nor historical Sentinax knowledge is inferred from an economic date.
- **D. Licensing.** `capture_authorized` stays false. A licensing hash or a public dataset URL grants no collection permission.
