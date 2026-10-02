# Game Changer Structured Extraction Boundary (Phase 23D1)

| Layer | Authority | Status |
|---|---|---|
| 23A | Source / event evidence, PIT binding, revision metadata | closed |
| 23B | Categorical assessment contract | closed |
| 23C1 | Active revision-family authority | closed |
| 23C2 | Review / new-capital gate | closed |
| 23D1 | Safe structured-extraction boundary (this document) | implemented |
| 23D2 | MKK / KAP access and provider adapter, after access / schema adjudication | deferred |

23B accepts an explicit caller-supplied assessment. 23D1 defines the safe boundary through which a human or a model structured extraction may
become a canonical 23B assessment. The chain is `23A event -> 23D1 extraction -> 23B assessment -> 23C1 revision resolution -> 23C2 gate`.
There is deliberately **no direct model-output-to-quarantine path** and no portfolio path: 23D1 only materializes a closed 23B assessment.

Module: `backend/engine/private/game_changer_extraction.py` (pure, in the static-guard `PURE_MANIFEST`; imports only the standard library, the 23A
binding type and the closed 23B surface).

```python
build_game_changer_structured_assessment_extraction(*, binding, extraction_mode, materiality, urgency, thesis_impact, impact_dimensions,
    materiality_basis, methodology_key, extractor_key, extractor_revision, extracted_at, source_content_sha256, extraction_output_sha256)
materialize_game_changer_materiality_assessment(*, extraction) -> GameChangerMaterialityAssessment
```

## Authority separation

The extraction retains the exact 23A `GameChangerEventPITBinding` by identity and may supply only the five 23B categorical axes plus provenance.
It has no field for source key, event key, tier, scope, instrument ids, event type, effective / published / observed times, content hash, revision
lineage or PIT context. The model cannot supply source identity or canonical instrument ids and cannot alter timestamps, revision lineage or event
type: those remain 23A authority, reachable only through the binding. A model never infers canonical security identity or source timestamps from prose.
Categorical validation is delegated to the closed 23B builder (including `OTHER` exclusivity), so there is a single semantic authority.

## HUMAN vs MODEL provenance

`GameChangerExtractionMode` (`HUMAN`, `MODEL`) is audit provenance only. For identical structured fields both modes materialize equal 23B assessments;
the mode changes no value, validation or gate semantics. No vendor or model is hardcoded.

## Provenance fields

- **Methodology vs extractor identity**: `methodology_key` is the assessment rubric / version (same grammar as 23B). `extractor_key` identifies the
  concrete extractor implementation (strict lowercase identifier) and `extractor_revision` is an exact `int >= 1` implementation revision
  (`bool`, `0`, negatives, floats, `Decimal` and strings are rejected). Neither proves quality.
- **Source-content provenance equality**: `source_content_sha256` must equal the 23A event `content_sha256`. This proves only that the extraction declares the
  same source artifact; it does not prove authenticity and raw bytes are not verified. Nothing is hashed.
- **Extraction-output provenance**: `extraction_output_sha256` is an opaque reference to the upstream structured extraction artifact. It may equal or differ from
  the source hash (no relation implied) and becomes the 23B `assessment_provenance_sha256`. Auditable prompts or provider-specific material live outside this object.

## Extraction time: system-availability semantics

```text
binding.event.observed_at <= extracted_at <= binding.context.knowledge_cutoff      (UTC instants, equality allowed, no tolerance)
```

`extracted_at` is an exact timezone-aware `datetime` supplied by the caller (no clock call). A structured assessment cannot exist before Sentinax observed the
event and cannot be injected into an earlier decision cutoff afterwards. **SOURCE_AS_OF event availability does not imply Sentinax assessment availability**:
for an event published at 10:00, observed at 12:00 and bound at an 11:00 SOURCE_AS_OF cutoff, the 23A binding is valid but every extraction is rejected, because
`extracted_at >= 12:00 > 11:00`. The market could know the disclosure; Sentinax had not yet produced its assessment. No retrospective model extraction is authorized
in 23D1; a future replay methodology would need its own explicit authority.

## Exclusions

```text
no confidence score, probability or certainty (uncertainty is the explicit UNCERTAIN thesis impact)
no raw text retention: no headline, body, HTML, PDF, JSON, attachment bytes or prompt / reasoning in the core object
no LLM runtime or SDK; no provider, KAP, MKK or network access; no credentials or API keys
no direct quarantine, new-capital decision, allocation, rebalance, order or execution
```

## KAP access note (non-runtime architecture note)

Automated KAP access is deferred to Phase 23D2. Official MKK / KAP documentation exposes KAP data-distribution REST services and MKK API Portal test /
developer access, but production entitlement and the API-product contract must be independently established before Sentinax enables an unattended production
adapter. Website scraping and undocumented endpoints are not accepted. No network code exists in this checkpoint.

## Claim limit

Given one already PIT-bound 23A event and explicit structured human or model extraction fields, 23D1 validates extraction provenance and system-time availability
and safely materializes a closed 23B assessment without allowing the extractor to rewrite source / event identity or directly influence portfolio actions. It does not
claim that the model interpreted the event correctly or is accurate, that the source is authentic, that the assessment is legally material, or that the asset should be
quarantined or sold.
