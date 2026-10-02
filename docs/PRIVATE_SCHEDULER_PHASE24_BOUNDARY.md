# Private Scheduler: Phase 24 Boundary

Phase 24 is closed through D3B and the secret gate. This document records the final architecture, what it provides and, explicitly, what it does not.

## Architecture

| Phase | Closed capability |
|---|---|
| 24A | trigger envelope (12 fields, caller-supplied authority) |
| 24B1 | timezone-safe scheduled occurrence (IANA wall time to UTC, DST round trip, deterministic hash) |
| 24B2 | civil recurrence (daily / weekly / monthly day-of-month) |
| 24B3A | external calendar evidence with PIT (system-as-of) binding |
| 24B3B | calendar applicability (APPLICABLE / NOT_APPLICABLE / UNAVAILABLE, anti-lookahead) |
| 24C1 | deterministic event occurrence identity |
| 24C2A | canonical run admission (three sources, one run hash, no second identity) |
| 24C2B | run lifecycle: READY, CLAIMED, SUCCEEDED, FAILED, half-open lease, version/CAS domain contract |
| 24D1 | migration 024: current run, append-only history, RLS, service-role-only RPCs (atomic CAS) |
| 24D2A | canonical admission persistence codec |
| 24D2B1 | PostgREST row transport and reconciliation |
| 24D2B2 | RPC-only repository (reads allowed, direct table writes forbidden, exact-version history reconciliation) |
| 24D3A | claim-before-dispatch one-run runtime |
| 24D3B | typed four-route WorkKind dispatcher |
| 24S1 | full-history Gitleaks gate (pinned, exact-fingerprint baseline) |

## Provides

Deterministic scheduled and event occurrence identity; civil recurrence; external-calendar PIT applicability; canonical run admission; lease lifecycle;
atomic persisted version/CAS authority; append-only transition history; canonical persistence codec; PostgREST row reconciliation; RPC-only repository
writes; claim-before-dispatch runtime; typed four-route WorkKind dispatcher; full-history secret scanning.

The whole suite runs remotely: `ci.yml` step "Phase 24 private scheduler correctness" lists the 15 scheduler test files explicitly (no wildcard, so a new
scheduler test needs a deliberate CI review). `test_scheduler_phase24_boundary.py` guards that membership, the exact module inventory, legacy isolation, the
closed WorkKind/scope contract, the single run identity chain, claim-before-dispatch, RPC-only writes, exact-version history reconciliation and the absence of
retry.

## Does not provide

- no scheduler polling daemon, no queue, no Redis execution authority
- no automatic retry
- no automatic renewal / heartbeat: D3A supports one lease; long work must fit inside it
- no automatic expired-claim takeover runtime
- no exactly-once external side effects (handlers may use `work_idempotency_key`, the run hash)
- no concrete source-refresh, portfolio-analysis, portfolio-health or Game Changer workflow: the four injected handlers stay unimplemented application
  workflows because closed admission lacks the business-specific inputs to build them safely. Phase 24 does not execute full Sentinax analysis autonomously.
- no KAP/MKK production adapter (Phase 23D2) and no calendar-provider adapters
- no real PostgreSQL concurrent-client test: current database authority is the migration 024 structure, Supabase Preview migration success, scripted repository
  tests and domain transition tests. Real simultaneous-client CAS testing is deferred.
- no tzdb-version provenance
- no trade execution

## Notes

- GAME_CHANGER_REVIEW is PORTFOLIO scoped in 24A. Phase 23 SYSTEMIC-event semantics stay separate; a systemic event is never reinterpreted as all portfolios,
  all instruments or a global quarantine. System-level review orchestration would need a separate work contract.
- SOURCE_DATA_REFRESH is orchestration intent only: it defines no provider, series, source policy, instrument or endpoint, and no defaults are fabricated.
- Gitleaks files (`.github/workflows/gitleaks.yml`, `.gitleaksignore`, `docs/SECURITY_SECRET_SCANNING.md`) are unchanged; the scan is verified on the candidate
  SHA, not assumed to be a branch-protection required check.
