"""
backend/engine/private/portfolio/import_instrument_resolver.py
=============================================================
PIT-Safe Instrument Resolver Execution Port & Complete Batch Harness (Phase 13K).

This module establishes the execution boundary consuming a Phase 13I ImportDraftBatchManifest
and an instrument-resolver adapter, producing an authoritative Phase 13J ImportInstrumentResolutionBatch.

Key Architectural Invariants:
1. Source-Neutral Execution:
   - Resolver adapter receives ONLY verbatim instrument_reference and exact PIT effective_date.
   - Zero broker name, zero filename, zero parsed field access, zero currency inference.
2. Snapshot & TOCTOU Hardening:
   - resolver_key, resolver_revision, and resolve_candidates callable are snapshotted exactly ONCE
     before draft iteration begins.
   - Descriptor/property lookups occur once; dynamic mutation during execution is prevented.
3. Candidate Cardinality Mapping:
   - 0 candidates  -> UNRESOLVED with code 'instrument_not_found'
   - 1 candidate   -> RESOLVED with selected instrument UUID
   - >= 2 candidates -> AMBIGUOUS with canonical sorted candidate UUIDs and code 'ambiguous_reference'
   - NOT_REQUIRED drafts bypass resolver execution entirely.
4. Fail-Closed Error Domain:
   - Resolver metadata/return-shape violations raise PortfolioImportInstrumentResolverError.
   - Duplicate candidate UUIDs fail closed immediately.
   - Resolver execution exceptions propagate unchanged.
5. Immutability & Pre-Ledger Boundary:
   - Pure domain composition: no network, DB, or filesystem calls.
   - Zero PortfolioTransaction creation, zero external identity derivations, zero cash bucket assignments.
"""

from __future__ import annotations

from datetime import date
import re
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Union,
    runtime_checkable,
)
from uuid import UUID

from backend.engine.private.portfolio.import_draft import ImportTransactionDraft
from backend.engine.private.portfolio.import_draft_batch import ImportDraftBatchManifest
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolution,
    ImportInstrumentResolutionBatch,
    ImportInstrumentResolutionDiagnostic,
    ImportInstrumentResolutionStatus,
    build_import_instrument_resolution,
    build_import_instrument_resolution_batch,
)

_RESOLVER_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_DIAG_CODE_INSTRUMENT_NOT_FOUND = "instrument_not_found"
_DIAG_MSG_INSTRUMENT_NOT_FOUND = (
    "No canonical instrument candidate was resolved for the supplied instrument reference."
)

_DIAG_CODE_AMBIGUOUS_REFERENCE = "ambiguous_reference"
_DIAG_MSG_AMBIGUOUS_REFERENCE = (
    "Multiple canonical instrument candidates were resolved for the supplied instrument reference."
)


class PortfolioImportInstrumentResolverError(ValueError):
    """Raised when resolver adapter contracts or resolver return values fail validation."""
    pass


@runtime_checkable
class PortfolioImportInstrumentResolver(Protocol):
    """
    Source-neutral protocol for instrument resolution adapters.
    """
    @property
    def resolver_key(self) -> str:
        ...

    @property
    def resolver_revision(self) -> int:
        ...

    def resolve_candidates(
        self,
        instrument_reference: str,
        as_of_date: date,
    ) -> Sequence[UUID]:
        ...


def _snapshot_and_validate_resolver(
    resolver: Any,
) -> Tuple[str, int, Callable[[str, date], Sequence[UUID]]]:
    """
    Snapshots resolver_key, resolver_revision, and resolve_candidates callable exactly ONCE
    and validates their contracts.
    """
    # 1. Snapshot resolver_key
    try:
        resolver_key = getattr(resolver, "resolver_key")
    except AttributeError as exc:
        raise PortfolioImportInstrumentResolverError(
            f"resolver is missing required 'resolver_key' attribute: {exc}"
        ) from exc
    except Exception as exc:
        raise PortfolioImportInstrumentResolverError(
            f"Error accessing resolver 'resolver_key': {exc}"
        ) from exc

    if isinstance(resolver_key, bool) or type(resolver_key) is not str:
        raise PortfolioImportInstrumentResolverError(
            f"resolver_key must be a str instance, got {type(resolver_key).__name__}"
        )
    if not _RESOLVER_KEY_PATTERN.fullmatch(resolver_key):
        raise PortfolioImportInstrumentResolverError(
            f"resolver_key must match '^[a-z0-9][a-z0-9._-]{{0,63}}$', got {resolver_key!r}"
        )

    # 2. Snapshot resolver_revision
    try:
        resolver_revision = getattr(resolver, "resolver_revision")
    except AttributeError as exc:
        raise PortfolioImportInstrumentResolverError(
            f"resolver is missing required 'resolver_revision' attribute: {exc}"
        ) from exc
    except Exception as exc:
        raise PortfolioImportInstrumentResolverError(
            f"Error accessing resolver 'resolver_revision': {exc}"
        ) from exc

    if isinstance(resolver_revision, bool) or type(resolver_revision) is not int:
        raise PortfolioImportInstrumentResolverError(
            f"resolver_revision must be an int instance, got {type(resolver_revision).__name__}"
        )
    if resolver_revision < 1:
        raise PortfolioImportInstrumentResolverError(
            f"resolver_revision must be >= 1, got {resolver_revision}"
        )

    # 3. Snapshot resolve_candidates callable
    try:
        resolve_candidates = getattr(resolver, "resolve_candidates")
    except AttributeError as exc:
        raise PortfolioImportInstrumentResolverError(
            f"resolver is missing required 'resolve_candidates' method: {exc}"
        ) from exc
    except Exception as exc:
        raise PortfolioImportInstrumentResolverError(
            f"Error accessing resolver 'resolve_candidates': {exc}"
        ) from exc

    if not callable(resolve_candidates):
        raise PortfolioImportInstrumentResolverError(
            f"'resolve_candidates' on resolver must be callable, got {type(resolve_candidates).__name__}"
        )

    return resolver_key, resolver_revision, resolve_candidates


def _execute_resolution_for_draft(
    draft: ImportTransactionDraft,
    resolver_key: str,
    resolver_revision: int,
    resolve_candidates_fn: Callable[[str, date], Sequence[UUID]],
) -> ImportInstrumentResolution:
    """
    Executes instrument resolution for a single economic transaction draft.
    """
    # If draft does not require instrument resolution, return NOT_REQUIRED directly
    if draft.instrument_reference is None:
        return build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=draft.effective_date,
            resolver_key=None,
            resolver_revision=None,
            instrument_id=None,
            candidate_instrument_ids=(),
            diagnostics=(),
        )

    # Invoke captured resolver callable with verbatim reference and exact PIT effective_date
    # Note: Exceptions from the resolver callable propagate unchanged.
    raw_candidates = resolve_candidates_fn(
        draft.instrument_reference,
        draft.effective_date,
    )

    # Validate return shape (must be materialized list or tuple)
    if type(raw_candidates) not in (list, tuple):
        raise PortfolioImportInstrumentResolverError(
            f"Resolver must return a list or tuple of UUIDs, got {type(raw_candidates).__name__}"
        )

    seen_uuids: Set[UUID] = set()
    validated_candidates: List[UUID] = []

    for idx, cand in enumerate(raw_candidates):
        if not isinstance(cand, UUID):
            raise PortfolioImportInstrumentResolverError(
                f"Candidate at index {idx} must be a UUID instance, got {type(cand).__name__}: {cand!r}"
            )
        if cand in seen_uuids:
            raise PortfolioImportInstrumentResolverError(
                f"Resolver returned duplicate candidate UUID: {cand}"
            )
        seen_uuids.add(cand)
        validated_candidates.append(cand)

    # Cardinality mapping:
    # 1. Zero candidates -> UNRESOLVED
    if len(validated_candidates) == 0:
        diag = ImportInstrumentResolutionDiagnostic(
            code=_DIAG_CODE_INSTRUMENT_NOT_FOUND,
            message=_DIAG_MSG_INSTRUMENT_NOT_FOUND,
        )
        return build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key=resolver_key,
            resolver_revision=resolver_revision,
            instrument_id=None,
            candidate_instrument_ids=(),
            diagnostics=[diag],
        )

    # 2. Exactly one candidate -> RESOLVED
    if len(validated_candidates) == 1:
        return build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key=resolver_key,
            resolver_revision=resolver_revision,
            instrument_id=validated_candidates[0],
            candidate_instrument_ids=(),
            diagnostics=(),
        )

    # 3. Multiple candidates (>= 2) -> AMBIGUOUS
    sorted_candidates = tuple(sorted(validated_candidates, key=str))
    diag = ImportInstrumentResolutionDiagnostic(
        code=_DIAG_CODE_AMBIGUOUS_REFERENCE,
        message=_DIAG_MSG_AMBIGUOUS_REFERENCE,
    )
    return build_import_instrument_resolution(
        draft=draft,
        status=ImportInstrumentResolutionStatus.AMBIGUOUS,
        resolution_as_of_date=draft.effective_date,
        resolver_key=resolver_key,
        resolver_revision=resolver_revision,
        instrument_id=None,
        candidate_instrument_ids=sorted_candidates,
        diagnostics=[diag],
    )


def resolve_import_draft_batch_instruments(
    draft_manifest: ImportDraftBatchManifest,
    resolver: PortfolioImportInstrumentResolver,
) -> ImportInstrumentResolutionBatch:
    """
    Executes PIT instrument resolution over a complete ImportDraftBatchManifest.

    - Validates draft_manifest type.
    - Snapshots and prevalidates resolver metadata (resolver_key, resolver_revision) and callable.
    - Iterates drafts in canonical order.
    - Maps 0/1/N candidates to UNRESOLVED/RESOLVED/AMBIGUOUS outcomes.
    - Bypasses resolver for NOT_REQUIRED drafts.
    - Produces a verified, immutable ImportInstrumentResolutionBatch.
    """
    if not isinstance(draft_manifest, ImportDraftBatchManifest):
        raise PortfolioImportInstrumentResolverError(
            f"draft_manifest must be an ImportDraftBatchManifest instance, got {type(draft_manifest).__name__}"
        )

    # Snapshot resolver metadata and callable once (TOCTOU hardening)
    resolver_key, resolver_revision, resolve_fn = _snapshot_and_validate_resolver(resolver)

    resolutions: List[ImportInstrumentResolution] = []

    for draft in draft_manifest.drafts:
        resolution = _execute_resolution_for_draft(
            draft=draft,
            resolver_key=resolver_key,
            resolver_revision=resolver_revision,
            resolve_candidates_fn=resolve_fn,
        )
        resolutions.append(resolution)

    return build_import_instrument_resolution_batch(
        draft_manifest=draft_manifest,
        resolutions=resolutions,
    )
