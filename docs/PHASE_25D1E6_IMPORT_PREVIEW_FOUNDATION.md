# Phase 25D1E6 — Private Portfolio Import Preview Foundation

Status: offline, synthetic-only foundation. It is **not** a deployment, **not** portfolio activation and **not** an import. Nothing is connected to a database, a network or a broker, and no real financial data is used.

## 1. Two different import types

| | Transaction-history import | Current-holdings snapshot |
|---|---|---|
| Meaning | An ordered list of things that *happened* (buy, sell, deposit, dividend, fee, FX …) | A list of what is held *now* (symbol, quantity, maybe an average cost) |
| Format | **Sentinax Canonical CSV v1** (13 fixed columns), Phase 13 | none supported |
| Status here | **Implemented as a read-only preview** | **Not implemented. Separate future feature.** |
| Parser | Existing Phase 13 parser, reused unchanged | **No second parser is added** |

**Transaction-history import** is the only supported format. The UI states this explicitly and does not advertise arbitrary broker CSV support.

**Current-holdings snapshot** is *not* a transaction history. A snapshot has no purchase dates, no execution times, no execution prices, no cost basis history, no realized gains and no cash flows. The system must **never invent** any of these to turn a snapshot into transactions: no synthetic BUY at "today", no average cost treated as an execution price, no assumed cash deposit. A snapshot file given to the transaction-history preview is rejected as `FILE_INVALID` (header does not match the canonical schema) or, if it uses canonical columns with a non-transaction type such as `holding`/`position`/`snapshot`, every row is `REJECTED` with `invalid_transaction_type`.

Future contract (not built here): a snapshot would be a distinct, explicitly labelled *opening position* concept with an unknown/declared cost basis flag (roadmap item "25D4 unknown-cost opening positions"), separate from the immutable transaction ledger, with its own parser, its own review envelope and its own approval gate.

## 2. Phase 12/13 inventory (reused, not reimplemented)

All under `backend/engine/private/portfolio/`:

| Stage | Reused component |
|---|---|
| Bytes → provenance → raw/parsed manifests | `import_pipeline.build_import_staging_result`, `parsers/sentinax_csv.SentinaxCanonicalCsvParserV1` |
| Row → assessment → typed economic draft | `parsers/sentinax_csv_semantics.SentinaxCanonicalCsvSemanticInterpreterV1`, `import_assessment`, `import_draft_batch` |
| PIT instrument resolution | `import_instrument_resolver.resolve_import_draft_batch_instruments`, `import_instrument_resolution` |
| **Commit path (NOT used)** | `sentinax_csv_import_execution`, `import_materialization`, `import_commit`, `repository.commit_import_binding_batch` |

`import_preview.py` is a pure composition over the first three rows. It imports none of the commit-path modules (pinned by an AST test and by a test that makes every commit/repository/socket entry point raise while a preview runs).

## 3. Preview envelope (`sentinax_import_preview.v1`)

A frozen, JSON-safe value (`ImportPreview.to_dict()`):

* identity: `contract_version`, `source_key`, `parser_revision`, `file_content_sha256` and the existing `raw/parsed/assessment/draft/resolution` manifest digests (all taken from the existing Phase 13 objects)
* counts: `record_count`, `ready/rejected/unresolved_count`, `resolved/not_required/unresolved_instrument/ambiguous_instrument_count`
* `status`, `file_error_code`, `blocking_reasons`, `resolver_key/revision`
* constants: `commit_authorized=false`, `persistent_state_changed=false`, `ledger_transaction_ids_generated=false`
* per record: ordinal, assessment status, static diagnostics, the typed economic fields (Decimal as exact text, never floats), resolution status, candidate **count** only (no identifiers), and an informational `identical_raw_record_of`.

Not included: raw file bytes, raw record text, file name, any identifier, any exception text.

### Status semantics

| Status | Meaning |
|---|---|
| `file_invalid` | The file/format/header is not Canonical CSV v1. No records are shown. |
| `empty_valid` | Valid canonical header, zero data rows. Distinct from "source unavailable". |
| `has_rejected_records` | One or more rows failed semantic validation; their values are never shown. |
| `resolution_unavailable` | No PIT resolver is connected (or it failed); instrument records are *not evaluated*, never "resolved". |
| `needs_resolution_review` | Instruments are unresolved or ambiguous. |
| `structurally_previewable` | Structure is readable. **This is not "ready to commit".** |

Structurally valid ≠ economically valid ≠ resolved ≠ de-duplicated ≠ approved. There is deliberately no `READY` status. `commit_authorized` is always `false`; the standing blocking reasons `preview_only_no_commit_path`, `idempotency_and_deduplication_not_evaluated` and `owner_approval_not_granted` are always present.

Identical source records stay distinct records; `identical_raw_record_of` merely points at the first earlier record with identical bytes. Nothing is removed, merged or counted differently.

### Finding fixed in this phase

Phase 13 validates the schema per record, so a header-only file was never schema-checked: `junk\n` or a holdings-snapshot header with no rows would have looked like an empty valid import. The preview now requires the canonical 13-key header (order-insensitive, as in Phase 13) before reporting `empty_valid`. Phase 13 code is unchanged.

## 4. Boundaries

* No persistence, DB, network, environment, clock, randomness or logging in the adapter; identities and time are explicit caller arguments.
* **No new private HTTP surface.** The private API remains GET-only and UNAVAILABLE-only (`GET /private/portfolio/summary`). There is no upload/preview endpoint (the legacy idempotency middleware is unscoped and not approved for private writes), no `POST`, and the commit RPC `commit_import_binding_batch` is not referenced.
* The adapter is independently testable in Python; it is not wired to any route in this phase.

## 5. Deep Ocean owner app

The Portfolio area gets a bounded import-preview section:

* **Simple view (Turkish, 4 steps):** select the supported format → review validity → review rejected/unresolved → confirm nothing is saved.
* **Advanced view:** parser revision, provenance hash, assessment counts, diagnostic codes, resolution status and import-prevention reasons — rendered **only** when an authoritative preview result is supplied. In the deployed bundle no preview service is connected, so this panel states plainly that it is not yet connected.
* No upload control, no file input, no fetch, no storage, text nodes only. Synthetic previews are rendered only by isolated tests/dev harness.

## 6. Rollback

Remove `import_preview.py`, its test, the owner preview module/section and the CI step additions. No schema, data or deployment state is touched, so rollback is a plain revert.

## 7. Not done / remaining gates

Real file upload transport, an authenticated scoped private write surface, a connected PIT resolver, de-duplication/idempotency evaluation, owner approval, the commit authorization step, holdings-snapshot import, and any real data are all out of scope and remain gated.
