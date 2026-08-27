"""
backend/engine/private/sec/winner_resolver.py
===============================================
SEC EDGAR Phase 8B.2B / 8B.2B.5 / 8B.2B.6 / 8B.2B.7: Snapshot-Scoped PIT Filing Precedence,
Restatement Reconciliation, Order-Independent Frontier Selection, Logical Deduplication,
and Disclosure Chronology Graph Cycle Fail-Closed Defense.

Core Invariants:
    - Snapshot-Scoped Isolation: Only candidates from the selected evaluation snapshot (or logical snapshot
      equivalence class with identical payload_hash and retrieved_at) are considered.
      Older snapshot candidates are NEVER mixed in or resurrected in CURRENT_REPORTED.
    - Logical Filing Identity: Accession number is the true logical filing identity. Identical in-memory
      duplicate filing records (differing only by storage UUID) are safely deduplicated. Conflicting metadata
      records fail closed.
    - Dual Filing Identifier Consistency: When both accession_number and filing_id are present on candidate,
      both must resolve and agree on the exact same logical filing identity.
    - Local Acceptance Semantics: Naive local acceptance timestamps can only be compared chronologically if
      both have verified matching semantics ("SEC_EST_DOCUMENTED").
    - Snapshot Temporal Lineage: A candidate's filing cannot be dated or accepted after the snapshot was retrieved.
    - Strict Chronology Graph & Cycle Defense: Cross-filing disclosure relations form a strict directed graph.
      Any cycle or inconsistency fails closed with CHRONOLOGY_CONFLICT. UUID/accession cannot break cycles.
    - Order-Independent Frontier Selection: In acyclic chronology graphs, pairwise dominance extracts the
      latest disclosure frontier. Candidate input list permutations never alter the economic result.
    - Deterministic Mode Semantics:
        * CURRENT_REPORTED: Latest valid full CompanyFacts snapshot locally observed for the CIK.
        * SYSTEM_AS_OF: Latest valid full CompanyFacts snapshot with retrieved_at <= as_of.
        * SOURCE_AS_OF: Fail-closed (UNAVAILABLE_SOURCE_AS_OF) because SEC CompanyFacts does not provide
          exact historical external API state reconstruction.
    - Disclosure Chronology: Aware UTC acceptance > Local acceptance with SEC_EST_DOCUMENTED > filing_date fallback.
      Lexicographical accession order, UUIDs, or DB created_at are NEVER used as economic authority.
    - Pure / Deterministic Engine: Zero external network calls, zero database writes, zero metric calculations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from backend.engine.private.sec.cik import normalize_cik
from backend.engine.private.sec.models import (
    SECCanonicalFactCandidate,
    SECFilingRecord,
)
from backend.engine.private.sec.period_context import (
    SECEconomicPeriodKind,
    SECPeriodAlignmentStatus,
    SECPeriodizedFactCandidate,
    build_economic_group_key,
)
from backend.engine.private.storage_models import RawProviderSnapshotRecord


class SECWinnerResolutionMode(Enum):
    """Resolution mode for PIT and current financial facts."""
    CURRENT_REPORTED = "current_reported"
    SYSTEM_AS_OF = "system_as_of"
    SOURCE_AS_OF = "source_as_of"


class SECWinnerStatus(Enum):
    """Detailed deterministic status of winner resolution."""
    SELECTED = "selected"
    NO_VALID_SNAPSHOT = "no_valid_snapshot"
    NO_SNAPSHOT_AS_OF = "no_snapshot_as_of"
    SNAPSHOT_CONFLICT = "snapshot_conflict"
    NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"
    UNRESOLVED_FILING = "unresolved_filing"
    AMBIGUOUS_WITHIN_FILING = "ambiguous_within_filing"
    AMBIGUOUS_DISCLOSURE_ORDER = "ambiguous_disclosure_order"
    CHRONOLOGY_CONFLICT = "chronology_conflict"
    SEMANTIC_SCOPE_CONFLICT = "semantic_scope_conflict"
    UNAVAILABLE_SOURCE_AS_OF = "unavailable_source_as_of"
    INVALID_TEMPORAL_LINEAGE = "invalid_temporal_lineage"


class FilingDisclosureComparison(Enum):
    """Outcome of comparing disclosure chronology between two filings."""
    A_LATER = "a_later"
    B_LATER = "b_later"
    SAME = "same"
    UNORDERABLE = "unorderable"


@dataclass
class SECWinnerResolutionResult:
    """
    Complete audit trail and result of SEC canonical fact winner resolution.
    """
    mode: SECWinnerResolutionMode
    status: SECWinnerStatus
    cik: str
    economic_group_key: Optional[Tuple[str, str, str, str, Optional[str], str]] = None
    as_of: Optional[datetime] = None
    evaluation_snapshot_id: Optional[UUID] = None
    evaluation_snapshot_ids: List[UUID] = field(default_factory=list)
    evaluation_snapshot_retrieved_at: Optional[datetime] = None
    evaluation_snapshot_hash: Optional[str] = None
    selected_candidate: Optional[SECPeriodizedFactCandidate] = None
    selected_raw_fact_id: Optional[UUID] = None
    selected_value: Optional[Decimal] = None
    selected_unit: Optional[str] = None
    selected_source_concept: Optional[str] = None
    selected_filing_id: Optional[UUID] = None
    selected_accession_number: Optional[str] = None
    selected_form: Optional[str] = None
    selection_confidence: Optional[str] = None  # "HIGH", "MEDIUM", "LOW"
    selection_basis: str = ""
    diagnostics: List[str] = field(default_factory=list)
    eligible_candidate_ids: List[UUID] = field(default_factory=list)
    rejected_candidates: List[Dict[str, Any]] = field(default_factory=list)
    corroborating_candidate_ids: List[UUID] = field(default_factory=list)
    superseded_candidate_ids: List[UUID] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "status": self.status.value,
            "cik": self.cik,
            "economic_group_key": list(self.economic_group_key) if self.economic_group_key else None,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "evaluation_snapshot_id": str(self.evaluation_snapshot_id) if self.evaluation_snapshot_id else None,
            "evaluation_snapshot_ids": [str(sid) for sid in self.evaluation_snapshot_ids],
            "evaluation_snapshot_retrieved_at": self.evaluation_snapshot_retrieved_at.isoformat() if self.evaluation_snapshot_retrieved_at else None,
            "evaluation_snapshot_hash": self.evaluation_snapshot_hash,
            "selected_candidate": self.selected_candidate.to_dict() if self.selected_candidate else None,
            "selected_raw_fact_id": str(self.selected_raw_fact_id) if self.selected_raw_fact_id else None,
            "selected_value": str(self.selected_value) if self.selected_value is not None else None,
            "selected_unit": self.selected_unit,
            "selected_source_concept": self.selected_source_concept,
            "selected_filing_id": str(self.selected_filing_id) if self.selected_filing_id else None,
            "selected_accession_number": self.selected_accession_number,
            "selected_form": self.selected_form,
            "selection_confidence": self.selection_confidence,
            "selection_basis": self.selection_basis,
            "diagnostics": self.diagnostics,
            "eligible_candidate_ids": [str(cid) for cid in self.eligible_candidate_ids],
            "rejected_candidates": self.rejected_candidates,
            "corroborating_candidate_ids": [str(cid) for cid in self.corroborating_candidate_ids],
            "superseded_candidate_ids": [str(cid) for cid in self.superseded_candidate_ids],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions: Logical Fingerprint, Semantic Quality & Chronology
# ─────────────────────────────────────────────────────────────────────────────

def filing_logical_fingerprint(filing: SECFilingRecord) -> Tuple[Any, ...]:
    """
    Computes authority-relevant logical fingerprint for an SEC filing.
    UUID alone is storage identity, not economic/logical filing difference.
    """
    return (
        normalize_cik(filing.cik) if filing.cik else "",
        (filing.accession_number or "").strip(),
        (filing.form or "").strip().upper(),
        bool(filing.is_amendment),
        filing.filing_date,
        filing.report_date,
        filing.acceptance_datetime,
        filing.acceptance_local_datetime,
        filing.acceptance_timezone_semantics or "",
    )


def get_semantic_quality_rank(match_strength: str) -> int:
    """Lower rank means higher quality: EXACT (1) > COMPATIBLE (2) > LEGACY_COMPATIBLE (3)."""
    ms = (match_strength or "").lower()
    if ms == "exact":
        return 1
    if ms == "compatible":
        return 2
    if ms == "legacy_compatible":
        return 3
    return 4


def compare_filing_disclosure_order(
    a: SECFilingRecord,
    b: SECFilingRecord,
) -> FilingDisclosureComparison:
    """
    Compares the disclosure chronology of two filings.
    Hierarchy:
        1. Both have timezone-aware acceptance_datetime -> UTC comparison
        2. Both have naive acceptance_local_datetime WITH SAME VERIFIED SEMANTICS ("SEC_EST_DOCUMENTED") -> direct comparison
        3. Fallback to filing_date if different
        4. If same filing_date with uncomparable acceptance timestamps -> UNORDERABLE
    """
    # 1. Aware acceptance timestamps
    if a.acceptance_datetime is not None and b.acceptance_datetime is not None:
        if a.acceptance_datetime > b.acceptance_datetime:
            return FilingDisclosureComparison.A_LATER
        if a.acceptance_datetime < b.acceptance_datetime:
            return FilingDisclosureComparison.B_LATER
        return FilingDisclosureComparison.SAME

    # 2. Local naive acceptance timestamps - require matching trusted semantics basis
    if a.acceptance_local_datetime is not None and b.acceptance_local_datetime is not None:
        a_sem = a.acceptance_timezone_semantics
        b_sem = b.acceptance_timezone_semantics
        if a_sem and b_sem and a_sem == b_sem and a_sem == "SEC_EST_DOCUMENTED":
            if a.acceptance_local_datetime > b.acceptance_local_datetime:
                return FilingDisclosureComparison.A_LATER
            if a.acceptance_local_datetime < b.acceptance_local_datetime:
                return FilingDisclosureComparison.B_LATER
            return FilingDisclosureComparison.SAME

    # 3. Filing date fallback
    if a.filing_date is not None and b.filing_date is not None:
        if a.filing_date > b.filing_date:
            return FilingDisclosureComparison.A_LATER
        if a.filing_date < b.filing_date:
            return FilingDisclosureComparison.B_LATER
        # Same filing date with uncomparable acceptance timestamps is UNORDERABLE
        return FilingDisclosureComparison.UNORDERABLE

    return FilingDisclosureComparison.UNORDERABLE


def validate_company_facts_snapshot(
    snapshot: RawProviderSnapshotRecord,
    target_cik: str,
) -> bool:
    """
    Validates that a RawProviderSnapshotRecord is a valid full CompanyFacts snapshot for target_cik.
    """
    if snapshot.provider != "SEC_EDGAR":
        return False

    if snapshot.http_status != 200:
        return False

    if snapshot.retrieved_at is None or snapshot.retrieved_at.tzinfo is None:
        return False

    # Endpoint check: must be company facts endpoint
    if not snapshot.endpoint or "companyfacts" not in snapshot.endpoint.lower():
        return False

    # Check request params / endpoint CIK
    norm_target = normalize_cik(target_cik)
    if isinstance(snapshot.request_params, dict):
        param_cik = snapshot.request_params.get("cik")
        if param_cik and normalize_cik(str(param_cik)) != norm_target:
            return False

    # Check raw payload
    if not isinstance(snapshot.raw_payload, dict):
        return False

    payload_cik = snapshot.raw_payload.get("cik")
    if payload_cik is None or normalize_cik(str(payload_cik)) != norm_target:
        return False

    facts = snapshot.raw_payload.get("facts")
    if not isinstance(facts, dict):
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Pure Deterministic Winner Resolver
# ─────────────────────────────────────────────────────────────────────────────

class SECWinnerResolver:
    """
    Pure deterministic winner resolver for SEC canonical fact candidates.
    Implements Snapshot-Scoped PIT Filing Precedence, Restatement Reconciliation,
    Order-Independent Frontier Selection, and Graph Cycle Fail-Closed Defense.
    """

    @classmethod
    def resolve_winner(
        cls,
        economic_group_key: Tuple[str, str, str, str, Optional[str], str],
        candidates: List[SECPeriodizedFactCandidate],
        snapshots: List[RawProviderSnapshotRecord],
        filings: List[SECFilingRecord],
        mode: SECWinnerResolutionMode = SECWinnerResolutionMode.CURRENT_REPORTED,
        as_of: Optional[datetime] = None,
    ) -> SECWinnerResolutionResult:
        """
        Resolves the authoritative single winner fact for a given economic group.
        """
        target_cik = economic_group_key[0]
        norm_target_cik = normalize_cik(target_cik)

        # ─────────────────────────────────────────────────────────────────────
        # 1. Mode Validation & Fail-Closed Checks
        # ─────────────────────────────────────────────────────────────────────
        if mode == SECWinnerResolutionMode.SOURCE_AS_OF:
            return SECWinnerResolutionResult(
                mode=mode,
                status=SECWinnerStatus.UNAVAILABLE_SOURCE_AS_OF,
                cik=norm_target_cik,
                economic_group_key=economic_group_key,
                as_of=as_of,
                selection_basis="SOURCE_AS_OF is unavailable because exact historical external CompanyFacts state cannot be reconstructed from current SEC API alone.",
                diagnostics=["SOURCE_AS_OF requested but SEC CompanyFacts does not support historical point-in-time API state replay."],
            )

        if mode == SECWinnerResolutionMode.SYSTEM_AS_OF:
            if as_of is None:
                raise ValueError("as_of datetime is required for SYSTEM_AS_OF resolution mode.")
            if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
                raise ValueError(f"as_of must be a timezone-aware datetime. Got naive: {as_of}")

        # ─────────────────────────────────────────────────────────────────────
        # 2. Evaluation Snapshot Selection & Logical Duplicate Equivalence
        # ─────────────────────────────────────────────────────────────────────
        valid_snapshots = [
            s for s in snapshots
            if validate_company_facts_snapshot(s, norm_target_cik)
        ]

        if not valid_snapshots:
            return SECWinnerResolutionResult(
                mode=mode,
                status=SECWinnerStatus.NO_VALID_SNAPSHOT,
                cik=norm_target_cik,
                economic_group_key=economic_group_key,
                as_of=as_of,
                selection_basis=f"No valid CompanyFacts snapshots found for CIK {norm_target_cik}.",
                diagnostics=[f"No valid CompanyFacts snapshots found for CIK {norm_target_cik}."],
            )

        if mode == SECWinnerResolutionMode.CURRENT_REPORTED:
            max_retrieved = max(s.retrieved_at for s in valid_snapshots)
            latest_candidates = [s for s in valid_snapshots if s.retrieved_at == max_retrieved]
            
            # Check for snapshot conflict (same retrieved_at, different payload_hash)
            unique_hashes = {s.payload_hash for s in latest_candidates}
            if len(unique_hashes) > 1:
                return SECWinnerResolutionResult(
                    mode=mode,
                    status=SECWinnerStatus.SNAPSHOT_CONFLICT,
                    cik=norm_target_cik,
                    economic_group_key=economic_group_key,
                    evaluation_snapshot_retrieved_at=max_retrieved,
                    selection_basis="Multiple snapshots with identical latest retrieved_at have differing payload hashes.",
                    diagnostics=["Snapshot conflict: differing payload_hash on identical retrieved_at."],
                )
            # Deterministic representative selection (sort by string UUID)
            eval_snapshot = min(latest_candidates, key=lambda s: str(s.id))
            eval_snapshot_ids = {s.id for s in latest_candidates}

        elif mode == SECWinnerResolutionMode.SYSTEM_AS_OF:
            eligible_snaps = [s for s in valid_snapshots if s.retrieved_at <= as_of]
            if not eligible_snaps:
                return SECWinnerResolutionResult(
                    mode=mode,
                    status=SECWinnerStatus.NO_SNAPSHOT_AS_OF,
                    cik=norm_target_cik,
                    economic_group_key=economic_group_key,
                    as_of=as_of,
                    selection_basis=f"No valid CompanyFacts snapshot exists at or before as_of {as_of.isoformat()}.",
                    diagnostics=[f"No snapshot found with retrieved_at <= {as_of.isoformat()}."],
                )
            max_retrieved = max(s.retrieved_at for s in eligible_snaps)
            latest_candidates = [s for s in eligible_snaps if s.retrieved_at == max_retrieved]
            unique_hashes = {s.payload_hash for s in latest_candidates}
            if len(unique_hashes) > 1:
                return SECWinnerResolutionResult(
                    mode=mode,
                    status=SECWinnerStatus.SNAPSHOT_CONFLICT,
                    cik=norm_target_cik,
                    economic_group_key=economic_group_key,
                    as_of=as_of,
                    evaluation_snapshot_retrieved_at=max_retrieved,
                    selection_basis="Multiple snapshots at as_of boundary have differing payload hashes.",
                    diagnostics=["Snapshot conflict at as_of boundary."],
                )
            eval_snapshot = min(latest_candidates, key=lambda s: str(s.id))
            eval_snapshot_ids = {s.id for s in latest_candidates}

        # ─────────────────────────────────────────────────────────────────────
        # 3. Collision-Resistant Logical Filing Deduplication & Indexing
        # ─────────────────────────────────────────────────────────────────────
        accession_groups: Dict[str, List[SECFilingRecord]] = {}
        id_groups: Dict[UUID, List[SECFilingRecord]] = {}
        colliding_accessions: Set[str] = set()
        colliding_ids: Set[UUID] = set()

        for f in filings:
            if f.accession_number and f.accession_number.strip():
                acc_key = f.accession_number.strip()
                accession_groups.setdefault(acc_key, []).append(f)
            if f.id:
                id_groups.setdefault(f.id, []).append(f)

        accession_map: Dict[str, SECFilingRecord] = {}
        filing_id_map: Dict[UUID, SECFilingRecord] = {}

        # Evaluate accession groups
        for acc_key, f_list in accession_groups.items():
            fingerprints = {filing_logical_fingerprint(f) for f in f_list}
            if len(fingerprints) > 1:
                colliding_accessions.add(acc_key)
                for f in f_list:
                    if f.id:
                        colliding_ids.add(f.id)
            else:
                # All identical logical duplicates -> pick deterministic canonical representative
                canonical_rep = min(f_list, key=lambda f: str(f.id) if f.id else "")
                accession_map[acc_key] = canonical_rep
                for f in f_list:
                    if f.id:
                        filing_id_map[f.id] = canonical_rep

        # Evaluate remaining ID groups
        for fid, f_list in id_groups.items():
            if fid in colliding_ids:
                continue
            fingerprints = {filing_logical_fingerprint(f) for f in f_list}
            if len(fingerprints) > 1:
                colliding_ids.add(fid)
            else:
                if fid not in filing_id_map:
                    filing_id_map[fid] = f_list[0]

        eligible_candidates: List[Tuple[SECPeriodizedFactCandidate, SECFilingRecord]] = []
        rejected_candidates: List[Dict[str, Any]] = []

        norm_target_group_key = (
            norm_target_cik,
            economic_group_key[1],
            economic_group_key[2],
            economic_group_key[3],
            economic_group_key[4],
            economic_group_key[5],
        )
        canonical_concept = economic_group_key[1]

        for cand in candidates:
            # 3.1 Strict Snapshot Membership (using logical snapshot equivalence class)
            if cand.snapshot_id not in eval_snapshot_ids:
                rejected_candidates.append({
                    "candidate_id": str(cand.id),
                    "raw_fact_id": str(cand.raw_fact_id),
                    "accession_number": cand.accession_number,
                    "reason": f"Snapshot mismatch: candidate belongs to snapshot {cand.snapshot_id}, evaluation snapshot ids are {[str(s) for s in eval_snapshot_ids]}.",
                })
                continue

            # 3.2 Economic Group Key Match
            cand_group_key = build_economic_group_key(cand)
            if cand_group_key != norm_target_group_key:
                rejected_candidates.append({
                    "candidate_id": str(cand.id),
                    "raw_fact_id": str(cand.raw_fact_id),
                    "accession_number": cand.accession_number,
                    "reason": f"Group key mismatch: candidate key {cand_group_key} != target key {norm_target_group_key}.",
                })
                continue

            # 3.3 Value Presence Check
            if cand.value is None:
                rejected_candidates.append({
                    "candidate_id": str(cand.id),
                    "raw_fact_id": str(cand.raw_fact_id),
                    "accession_number": cand.accession_number,
                    "reason": "Value is None.",
                })
                continue

            # 3.4 Alignment Status Eligibility & Cover-Date Defense-In-Depth
            if cand.period_alignment_status in (
                SECPeriodAlignmentStatus.NON_PRIMARY_CONTEXT,
                SECPeriodAlignmentStatus.UNRESOLVED_FILING,
                SECPeriodAlignmentStatus.INSUFFICIENT_EVIDENCE,
                SECPeriodAlignmentStatus.INVALID_CONTEXT,
            ):
                rejected_candidates.append({
                    "candidate_id": str(cand.id),
                    "raw_fact_id": str(cand.raw_fact_id),
                    "accession_number": cand.accession_number,
                    "reason": f"Ineligible period alignment status: {cand.period_alignment_status.value}.",
                })
                continue

            if cand.period_alignment_status == SECPeriodAlignmentStatus.COVER_DATE_CONTEXT:
                if (
                    canonical_concept != "SHARES_OUTSTANDING"
                    or cand.taxonomy != "dei"
                    or cand.source_concept != "EntityCommonStockSharesOutstanding"
                ):
                    rejected_candidates.append({
                        "candidate_id": str(cand.id),
                        "raw_fact_id": str(cand.raw_fact_id),
                        "accession_number": cand.accession_number,
                        "reason": f"COVER_DATE_CONTEXT is only eligible for dei:EntityCommonStockSharesOutstanding under SHARES_OUTSTANDING, got {cand.taxonomy}:{cand.source_concept} ({canonical_concept}).",
                    })
                    continue

            # 3.5 Dual Filing Lineage Resolution & Validation
            has_acc = bool(cand.accession_number and cand.accession_number.strip())
            has_id = bool(cand.filing_id)

            if not has_acc and not has_id:
                rejected_candidates.append({
                    "candidate_id": str(cand.id),
                    "raw_fact_id": str(cand.raw_fact_id),
                    "accession_number": cand.accession_number,
                    "reason": "Candidate lacks both accession_number and filing_id.",
                })
                continue

            filing_by_acc: Optional[SECFilingRecord] = None
            filing_by_id: Optional[SECFilingRecord] = None

            if has_acc:
                acc_clean = cand.accession_number.strip()
                if acc_clean in colliding_accessions:
                    rejected_candidates.append({
                        "candidate_id": str(cand.id),
                        "raw_fact_id": str(cand.raw_fact_id),
                        "accession_number": cand.accession_number,
                        "reason": f"Conflicting duplicate filings provided for accession '{acc_clean}'.",
                    })
                    continue
                filing_by_acc = accession_map.get(acc_clean)
                if not filing_by_acc:
                    rejected_candidates.append({
                        "candidate_id": str(cand.id),
                        "raw_fact_id": str(cand.raw_fact_id),
                        "accession_number": cand.accession_number,
                        "reason": f"Accession '{acc_clean}' could not be resolved from supplied filings.",
                    })
                    continue

            if has_id:
                if cand.filing_id in colliding_ids:
                    rejected_candidates.append({
                        "candidate_id": str(cand.id),
                        "raw_fact_id": str(cand.raw_fact_id),
                        "accession_number": cand.accession_number,
                        "reason": f"Conflicting duplicate filings provided for filing_id '{cand.filing_id}'.",
                    })
                    continue
                filing_by_id = filing_id_map.get(cand.filing_id)
                if not filing_by_id:
                    rejected_candidates.append({
                        "candidate_id": str(cand.id),
                        "raw_fact_id": str(cand.raw_fact_id),
                        "accession_number": cand.accession_number,
                        "reason": f"Filing ID '{cand.filing_id}' could not be resolved from supplied filings.",
                    })
                    continue

            if has_acc and has_id:
                # Both resolved: verify they resolve to the exact same logical filing
                if (
                    filing_by_acc.accession_number.strip() != filing_by_id.accession_number.strip()
                    or filing_logical_fingerprint(filing_by_acc) != filing_logical_fingerprint(filing_by_id)
                ):
                    rejected_candidates.append({
                        "candidate_id": str(cand.id),
                        "raw_fact_id": str(cand.raw_fact_id),
                        "accession_number": cand.accession_number,
                        "reason": "Candidate accession_number and filing_id resolve to different filings.",
                    })
                    continue
                resolved_filing = filing_by_acc
            elif has_acc:
                resolved_filing = filing_by_acc
            else:
                resolved_filing = filing_by_id

            # Check CIK consistency
            if normalize_cik(resolved_filing.cik) != norm_target_cik:
                rejected_candidates.append({
                    "candidate_id": str(cand.id),
                    "raw_fact_id": str(cand.raw_fact_id),
                    "accession_number": cand.accession_number,
                    "reason": f"Filing CIK mismatch: filing CIK '{resolved_filing.cik}' != target CIK '{norm_target_cik}'.",
                })
                continue

            # Check Form consistency
            if cand.form and resolved_filing.form:
                if cand.form.strip().upper() != resolved_filing.form.strip().upper():
                    rejected_candidates.append({
                        "candidate_id": str(cand.id),
                        "raw_fact_id": str(cand.raw_fact_id),
                        "accession_number": cand.accession_number,
                        "reason": f"Form mismatch: candidate form '{cand.form}' != filing form '{resolved_filing.form}'.",
                    })
                    continue

            # 3.6 Snapshot Temporal Lineage Sanity (CURRENT_REPORTED & SYSTEM_AS_OF)
            # A candidate's filing cannot occur after the evaluation snapshot was retrieved!
            if resolved_filing.filing_date and resolved_filing.filing_date > eval_snapshot.retrieved_at.date():
                return SECWinnerResolutionResult(
                    mode=mode,
                    status=SECWinnerStatus.INVALID_TEMPORAL_LINEAGE,
                    cik=norm_target_cik,
                    economic_group_key=economic_group_key,
                    as_of=as_of,
                    evaluation_snapshot_id=eval_snapshot.id,
                    evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                    evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                    evaluation_snapshot_hash=eval_snapshot.payload_hash,
                    selection_basis=f"Temporal lineage error: filing_date {resolved_filing.filing_date} is after snapshot retrieved_at date {eval_snapshot.retrieved_at.date()}.",
                    diagnostics=[f"Snapshot temporal lookahead detected on candidate {cand.id}."],
                    rejected_candidates=rejected_candidates + [{
                        "candidate_id": str(cand.id),
                        "reason": "filing_date > eval_snapshot.retrieved_at.date()",
                    }],
                )

            if resolved_filing.acceptance_datetime and resolved_filing.acceptance_datetime > eval_snapshot.retrieved_at:
                return SECWinnerResolutionResult(
                    mode=mode,
                    status=SECWinnerStatus.INVALID_TEMPORAL_LINEAGE,
                    cik=norm_target_cik,
                    economic_group_key=economic_group_key,
                    as_of=as_of,
                    evaluation_snapshot_id=eval_snapshot.id,
                    evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                    evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                    evaluation_snapshot_hash=eval_snapshot.payload_hash,
                    selection_basis=f"Temporal lineage error: acceptance_datetime {resolved_filing.acceptance_datetime.isoformat()} is after snapshot retrieved_at {eval_snapshot.retrieved_at.isoformat()}.",
                    diagnostics=[f"Snapshot temporal lookahead detected on candidate {cand.id}."],
                    rejected_candidates=rejected_candidates + [{
                        "candidate_id": str(cand.id),
                        "reason": "acceptance_datetime > eval_snapshot.retrieved_at",
                    }],
                )

            # Secondary check for SYSTEM_AS_OF boundary
            if mode == SECWinnerResolutionMode.SYSTEM_AS_OF and as_of is not None:
                if resolved_filing.filing_date and resolved_filing.filing_date > as_of.date():
                    return SECWinnerResolutionResult(
                        mode=mode,
                        status=SECWinnerStatus.INVALID_TEMPORAL_LINEAGE,
                        cik=norm_target_cik,
                        economic_group_key=economic_group_key,
                        as_of=as_of,
                        evaluation_snapshot_id=eval_snapshot.id,
                        evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                        evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                        evaluation_snapshot_hash=eval_snapshot.payload_hash,
                        selection_basis=f"Lookahead inconsistency: filing_date {resolved_filing.filing_date} is after as_of {as_of.date()}.",
                        diagnostics=[f"Temporal lookahead detected on candidate {cand.id}."],
                        rejected_candidates=rejected_candidates + [{
                            "candidate_id": str(cand.id),
                            "reason": "filing_date > as_of.date()",
                        }],
                    )
                if resolved_filing.acceptance_datetime and resolved_filing.acceptance_datetime > as_of:
                    return SECWinnerResolutionResult(
                        mode=mode,
                        status=SECWinnerStatus.INVALID_TEMPORAL_LINEAGE,
                        cik=norm_target_cik,
                        economic_group_key=economic_group_key,
                        as_of=as_of,
                        evaluation_snapshot_id=eval_snapshot.id,
                        evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                        evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                        evaluation_snapshot_hash=eval_snapshot.payload_hash,
                        selection_basis=f"Lookahead inconsistency: acceptance_datetime {resolved_filing.acceptance_datetime.isoformat()} is after as_of {as_of.isoformat()}.",
                        diagnostics=[f"Temporal lookahead detected on candidate {cand.id}."],
                        rejected_candidates=rejected_candidates + [{
                            "candidate_id": str(cand.id),
                            "reason": "acceptance_datetime > as_of",
                        }],
                    )

            eligible_candidates.append((cand, resolved_filing))

        if not eligible_candidates:
            return SECWinnerResolutionResult(
                mode=mode,
                status=SECWinnerStatus.NO_ELIGIBLE_CANDIDATE,
                cik=norm_target_cik,
                economic_group_key=economic_group_key,
                as_of=as_of,
                evaluation_snapshot_id=eval_snapshot.id,
                evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                evaluation_snapshot_hash=eval_snapshot.payload_hash,
                selection_basis="No candidates met snapshot-scoped winner eligibility criteria.",
                diagnostics=["All candidates were filtered out by snapshot isolation or eligibility rules."],
                rejected_candidates=rejected_candidates,
            )

        # ─────────────────────────────────────────────────────────────────────
        # 4. Same-Filing Semantic Reconciliation & Deterministic Tie-Breaking
        # ─────────────────────────────────────────────────────────────────────
        filing_groups: Dict[str, List[Tuple[SECPeriodizedFactCandidate, SECFilingRecord]]] = {}
        for cand, filing in eligible_candidates:
            key = cand.accession_number.strip() if cand.accession_number else (filing.accession_number.strip() if filing.accession_number else str(filing.id))
            if key not in filing_groups:
                filing_groups[key] = []
            filing_groups[key].append((cand, filing))

        filing_representatives: List[Dict[str, Any]] = []
        all_corroborating_ids: List[UUID] = []
        diagnostics_list: List[str] = []

        for f_key, cand_list in filing_groups.items():
            # Sort by semantic quality rank (ascending: EXACT=1 < COMPATIBLE=2 < LEGACY_COMPATIBLE=3),
            # then variant_priority, then deterministic presentation tie-breakers (taxonomy, concept, raw_fact_id)
            sorted_cands = sorted(
                cand_list,
                key=lambda item: (
                    get_semantic_quality_rank(item[0].match_strength),
                    item[0].variant_priority,
                    item[0].taxonomy or "",
                    item[0].source_concept or "",
                    str(item[0].raw_fact_id),
                )
            )

            top_cand, top_filing = sorted_cands[0]
            top_rank = get_semantic_quality_rank(top_cand.match_strength)
            corroborating_for_filing: List[UUID] = []

            # Check for multiple candidates in the same filing
            if len(sorted_cands) > 1:
                # Check for two EXACT candidates with differing values
                exact_cands = [c for c, f in sorted_cands if get_semantic_quality_rank(c.match_strength) == 1]
                if len(exact_cands) > 1:
                    exact_values = {c.value for c in exact_cands}
                    if len(exact_values) > 1:
                        return SECWinnerResolutionResult(
                            mode=mode,
                            status=SECWinnerStatus.AMBIGUOUS_WITHIN_FILING,
                            cik=norm_target_cik,
                            economic_group_key=economic_group_key,
                            as_of=as_of,
                            evaluation_snapshot_id=eval_snapshot.id,
                            evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                            evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                            evaluation_snapshot_hash=eval_snapshot.payload_hash,
                            selection_basis=f"Filing {f_key} contains conflicting EXACT canonical concept variants with differing values {exact_values}.",
                            diagnostics=["Ambiguous within filing: multiple EXACT variants with conflicting values."],
                            eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                            rejected_candidates=rejected_candidates,
                        )

                # Classify lower-rank candidates in the same filing
                for other_cand, _ in sorted_cands[1:]:
                    if other_cand.value == top_cand.value:
                        corroborating_for_filing.append(other_cand.id)
                        all_corroborating_ids.append(other_cand.id)
                    else:
                        diagnostics_list.append(
                            f"Filing {f_key}: Lower semantic candidate ({other_cand.match_strength}, {other_cand.source_concept}) differed in value ({other_cand.value}) from selected fact ({top_cand.value})."
                        )

            filing_representatives.append({
                "candidate": top_cand,
                "filing": top_filing,
                "corroborating_ids": corroborating_for_filing,
                "quality_rank": top_rank,
            })

        # ─────────────────────────────────────────────────────────────────────
        # 5. Order-Independent Latest Disclosure Frontier Selection & Cycle Defense
        # ─────────────────────────────────────────────────────────────────────
        if len(filing_representatives) == 1:
            winner_rep = filing_representatives[0]
            winner_cand = winner_rep["candidate"]
            winner_filing = winner_rep["filing"]

            return SECWinnerResolutionResult(
                mode=mode,
                status=SECWinnerStatus.SELECTED,
                cik=norm_target_cik,
                economic_group_key=economic_group_key,
                as_of=as_of,
                evaluation_snapshot_id=eval_snapshot.id,
                evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                evaluation_snapshot_hash=eval_snapshot.payload_hash,
                selected_candidate=winner_cand,
                selected_raw_fact_id=winner_cand.raw_fact_id,
                selected_value=winner_cand.value,
                selected_unit=winner_cand.unit,
                selected_source_concept=winner_cand.source_concept,
                selected_filing_id=winner_filing.id,
                selected_accession_number=winner_cand.accession_number,
                selected_form=winner_cand.form,
                selection_confidence=winner_cand.classification_confidence,
                selection_basis=f"Uniquely resolved candidate from authoritative filing {winner_filing.accession_number}.",
                diagnostics=diagnostics_list or ["Single filing representative selected."],
                eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                rejected_candidates=rejected_candidates,
                corroborating_candidate_ids=sorted(all_corroborating_ids, key=lambda u: str(u)),
                superseded_candidate_ids=[],
            )

        # Deterministic sorting of filing representatives for presentation & iteration
        filing_representatives = sorted(
            filing_representatives,
            key=lambda rep: (
                rep["filing"].accession_number or "",
                str(rep["filing"].id) if rep["filing"].id else "",
                rep["quality_rank"],
                rep["candidate"].variant_priority,
                str(rep["candidate"].raw_fact_id),
            )
        )

        n = len(filing_representatives)
        # Build strict directed chronology graph: edge u -> v means v is strictly later than u (v > u)
        adj: Dict[int, Set[int]] = {i: set() for i in range(n)}

        for i in range(n):
            for j in range(i + 1, n):
                cmp_ij = compare_filing_disclosure_order(
                    filing_representatives[i]["filing"],
                    filing_representatives[j]["filing"]
                )
                cmp_ji = compare_filing_disclosure_order(
                    filing_representatives[j]["filing"],
                    filing_representatives[i]["filing"]
                )

                # Defensive pair symmetry verification
                if cmp_ij == FilingDisclosureComparison.A_LATER:
                    if cmp_ji != FilingDisclosureComparison.B_LATER:
                        return SECWinnerResolutionResult(
                            mode=mode,
                            status=SECWinnerStatus.CHRONOLOGY_CONFLICT,
                            cik=norm_target_cik,
                            economic_group_key=economic_group_key,
                            as_of=as_of,
                            evaluation_snapshot_id=eval_snapshot.id,
                            evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                            evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                            evaluation_snapshot_hash=eval_snapshot.payload_hash,
                            selection_basis="Asymmetric disclosure comparison detected across filings.",
                            diagnostics=["Asymmetric disclosure comparison detected."],
                            eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                            rejected_candidates=rejected_candidates,
                        )
                    # i is later than j -> j -> i
                    adj[j].add(i)

                elif cmp_ij == FilingDisclosureComparison.B_LATER:
                    if cmp_ji != FilingDisclosureComparison.A_LATER:
                        return SECWinnerResolutionResult(
                            mode=mode,
                            status=SECWinnerStatus.CHRONOLOGY_CONFLICT,
                            cik=norm_target_cik,
                            economic_group_key=economic_group_key,
                            as_of=as_of,
                            evaluation_snapshot_id=eval_snapshot.id,
                            evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                            evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                            evaluation_snapshot_hash=eval_snapshot.payload_hash,
                            selection_basis="Asymmetric disclosure comparison detected across filings.",
                            diagnostics=["Asymmetric disclosure comparison detected."],
                            eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                            rejected_candidates=rejected_candidates,
                        )
                    # j is later than i -> i -> j
                    adj[i].add(j)

                elif cmp_ij in (FilingDisclosureComparison.SAME, FilingDisclosureComparison.UNORDERABLE):
                    if cmp_ji != cmp_ij:
                        return SECWinnerResolutionResult(
                            mode=mode,
                            status=SECWinnerStatus.CHRONOLOGY_CONFLICT,
                            cik=norm_target_cik,
                            economic_group_key=economic_group_key,
                            as_of=as_of,
                            evaluation_snapshot_id=eval_snapshot.id,
                            evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                            evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                            evaluation_snapshot_hash=eval_snapshot.payload_hash,
                            selection_basis="Asymmetric disclosure comparison detected across filings.",
                            diagnostics=["Asymmetric disclosure comparison detected."],
                            eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                            rejected_candidates=rejected_candidates,
                        )
                    # No strict directed edge for SAME or UNORDERABLE

        # Detect directed cycles in chronology graph (DFS 3-color)
        visited = [0] * n  # 0 = unvisited, 1 = in stack, 2 = done
        has_cycle = False

        def dfs_cycle(u: int) -> bool:
            visited[u] = 1
            for v in adj[u]:
                if visited[v] == 1:
                    return True
                if visited[v] == 0:
                    if dfs_cycle(v):
                        return True
            visited[u] = 2
            return False

        for i in range(n):
            if visited[i] == 0:
                if dfs_cycle(i):
                    has_cycle = True
                    break

        if has_cycle:
            return SECWinnerResolutionResult(
                mode=mode,
                status=SECWinnerStatus.CHRONOLOGY_CONFLICT,
                cik=norm_target_cik,
                economic_group_key=economic_group_key,
                as_of=as_of,
                evaluation_snapshot_id=eval_snapshot.id,
                evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                evaluation_snapshot_hash=eval_snapshot.payload_hash,
                selection_basis="Disclosure chronology contains a cycle across filings; no authoritative latest disclosure can be established.",
                diagnostics=["Disclosure chronology contains a cycle across filings; no authoritative latest disclosure can be established."],
                eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                rejected_candidates=rejected_candidates,
            )

        # In an acyclic graph, node u is dominated if it has at least one successor in adj[u] (out-degree > 0)
        dominated_indices = {u for u in range(n) if len(adj[u]) > 0}
        latest_frontier = [
            filing_representatives[i] for i in range(n) if i not in dominated_indices
        ]
        dominated_reps = [
            filing_representatives[i] for i in range(n) if i in dominated_indices
        ]

        # Defensive empty frontier check
        if not latest_frontier:
            return SECWinnerResolutionResult(
                mode=mode,
                status=SECWinnerStatus.CHRONOLOGY_CONFLICT,
                cik=norm_target_cik,
                economic_group_key=economic_group_key,
                as_of=as_of,
                evaluation_snapshot_id=eval_snapshot.id,
                evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                evaluation_snapshot_hash=eval_snapshot.payload_hash,
                selection_basis="Disclosure chronology yielded empty frontier; inconsistent disclosure order.",
                diagnostics=["Empty latest disclosure frontier detected."],
                eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                rejected_candidates=rejected_candidates,
            )

        # ─────────────────────────────────────────────────────────────────────
        # 6. Reconcile Frontier against Dominated/Earlier Disclosures
        # ─────────────────────────────────────────────────────────────────────
        if len(latest_frontier) == 1:
            winner_rep = latest_frontier[0]
            winner_cand = winner_rep["candidate"]
            winner_filing = winner_rep["filing"]
            winner_rank = winner_rep["quality_rank"]
            confidence_level = winner_cand.classification_confidence

            # Semantic-quality check against all earlier/dominated disclosures
            if winner_rank > 1:
                # Later disclosure has lower quality (COMPATIBLE or LEGACY_COMPATIBLE)
                # Check if any earlier disclosure had strictly higher quality
                higher_quality_earlier = [
                    rep for rep in dominated_reps
                    if rep["quality_rank"] < winner_rank
                ]
                if higher_quality_earlier:
                    conflicting_higher = [
                        rep for rep in higher_quality_earlier
                        if rep["candidate"].value != winner_cand.value
                    ]
                    if conflicting_higher:
                        return SECWinnerResolutionResult(
                            mode=mode,
                            status=SECWinnerStatus.SEMANTIC_SCOPE_CONFLICT,
                            cik=norm_target_cik,
                            economic_group_key=economic_group_key,
                            as_of=as_of,
                            evaluation_snapshot_id=eval_snapshot.id,
                            evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                            evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                            evaluation_snapshot_hash=eval_snapshot.payload_hash,
                            selection_basis=f"Later disclosure {winner_filing.accession_number} has lower semantic quality ({winner_cand.match_strength}) with differing value {winner_cand.value} vs prior higher-quality disclosure ({conflicting_higher[0]['candidate'].match_strength}) with value {conflicting_higher[0]['candidate'].value}.",
                            diagnostics=["Semantic scope conflict: cannot overwrite higher-quality fact with lower-quality alias of differing value."],
                            eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                            rejected_candidates=rejected_candidates,
                        )
                    else:
                        # Values match: downgrade confidence to MEDIUM
                        confidence_level = "MEDIUM"
                        diagnostics_list.append(
                            f"Later disclosure {winner_filing.accession_number} has lower semantic quality ({winner_cand.match_strength}) but value corroborates ({winner_cand.value})."
                        )

            # Classify earlier reps into superseded vs corroborating
            superseded_ids: List[UUID] = []
            for rep in dominated_reps:
                if rep["candidate"].value != winner_cand.value:
                    superseded_ids.append(rep["candidate"].id)
                    diagnostics_list.append(
                        f"Later disclosure {winner_filing.accession_number} ({winner_cand.value}) supersedes prior disclosure {rep['filing'].accession_number} ({rep['candidate'].value})."
                    )
                else:
                    all_corroborating_ids.append(rep["candidate"].id)

            basis = (
                f"Resolved via disclosure precedence from authoritative filing {winner_filing.accession_number} "
                f"(form {winner_cand.form}, match {winner_cand.match_strength})."
            )

            return SECWinnerResolutionResult(
                mode=mode,
                status=SECWinnerStatus.SELECTED,
                cik=norm_target_cik,
                economic_group_key=economic_group_key,
                as_of=as_of,
                evaluation_snapshot_id=eval_snapshot.id,
                evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                evaluation_snapshot_hash=eval_snapshot.payload_hash,
                selected_candidate=winner_cand,
                selected_raw_fact_id=winner_cand.raw_fact_id,
                selected_value=winner_cand.value,
                selected_unit=winner_cand.unit,
                selected_source_concept=winner_cand.source_concept,
                selected_filing_id=winner_filing.id,
                selected_accession_number=winner_cand.accession_number,
                selected_form=winner_cand.form,
                selection_confidence=confidence_level,
                selection_basis=basis,
                diagnostics=diagnostics_list or ["Resolved cross-filing disclosure precedence."],
                eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                rejected_candidates=rejected_candidates,
                corroborating_candidate_ids=sorted(all_corroborating_ids, key=lambda u: str(u)),
                superseded_candidate_ids=sorted(superseded_ids, key=lambda u: str(u)),
            )

        # Multiple frontier members:
        frontier_values = {rep["candidate"].value for rep in latest_frontier}
        if len(frontier_values) > 1:
            return SECWinnerResolutionResult(
                mode=mode,
                status=SECWinnerStatus.AMBIGUOUS_DISCLOSURE_ORDER,
                cik=norm_target_cik,
                economic_group_key=economic_group_key,
                as_of=as_of,
                evaluation_snapshot_id=eval_snapshot.id,
                evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                evaluation_snapshot_hash=eval_snapshot.payload_hash,
                selection_basis=f"Multiple latest frontier filings have unorderable or identical disclosure chronology with differing values {frontier_values}.",
                diagnostics=["Ambiguous disclosure order across multiple latest filings with differing values."],
                eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                rejected_candidates=rejected_candidates,
            )

        # All frontier members have the same value
        frontier_val = next(iter(frontier_values))
        best_frontier_rank = min(rep["quality_rank"] for rep in latest_frontier)

        # Check semantic scope conflict against dominated disclosures
        if best_frontier_rank > 1:
            higher_quality_earlier = [
                rep for rep in dominated_reps
                if rep["quality_rank"] < best_frontier_rank
            ]
            conflicting_higher = [
                rep for rep in higher_quality_earlier
                if rep["candidate"].value != frontier_val
            ]
            if conflicting_higher:
                return SECWinnerResolutionResult(
                    mode=mode,
                    status=SECWinnerStatus.SEMANTIC_SCOPE_CONFLICT,
                    cik=norm_target_cik,
                    economic_group_key=economic_group_key,
                    as_of=as_of,
                    evaluation_snapshot_id=eval_snapshot.id,
                    evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
                    evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                    evaluation_snapshot_hash=eval_snapshot.payload_hash,
                    selection_basis=f"Latest frontier disclosures have lower semantic quality with differing value {frontier_val} vs prior higher-quality disclosure ({conflicting_higher[0]['candidate'].match_strength}) with value {conflicting_higher[0]['candidate'].value}.",
                    diagnostics=["Semantic scope conflict: cannot overwrite higher-quality fact with lower-quality alias of differing value."],
                    eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                    rejected_candidates=rejected_candidates,
                )

        # Sort latest_frontier deterministically to pick best representative
        sorted_frontier = sorted(
            latest_frontier,
            key=lambda rep: (
                rep["quality_rank"],
                rep["candidate"].variant_priority,
                rep["candidate"].taxonomy or "",
                rep["candidate"].source_concept or "",
                str(rep["candidate"].raw_fact_id),
                rep["filing"].accession_number or "",
            )
        )
        winner_rep = sorted_frontier[0]
        winner_cand = winner_rep["candidate"]
        winner_filing = winner_rep["filing"]

        # Other frontier members corroborate
        for other_rep in sorted_frontier[1:]:
            all_corroborating_ids.append(other_rep["candidate"].id)

        # Check pairwise chronology between frontier members to determine confidence
        has_unorderable_frontier = False
        for i in range(len(latest_frontier)):
            for j in range(i + 1, len(latest_frontier)):
                cmp = compare_filing_disclosure_order(
                    latest_frontier[i]["filing"],
                    latest_frontier[j]["filing"]
                )
                if cmp == FilingDisclosureComparison.UNORDERABLE:
                    has_unorderable_frontier = True
                    break

        confidence_level = winner_cand.classification_confidence
        if has_unorderable_frontier:
            confidence_level = "MEDIUM"
            diagnostics_list.append(
                "Disclosure order between latest frontier filings is unorderable; corroborated by identical value."
            )

        # Classify dominated reps into superseded vs corroborating
        superseded_ids = []
        for rep in dominated_reps:
            if rep["candidate"].value != winner_cand.value:
                superseded_ids.append(rep["candidate"].id)
                diagnostics_list.append(
                    f"Latest frontier disclosure {winner_filing.accession_number} ({winner_cand.value}) supersedes prior disclosure {rep['filing'].accession_number} ({rep['candidate'].value})."
                )
            else:
                all_corroborating_ids.append(rep["candidate"].id)

        basis = (
            f"Resolved via latest disclosure frontier from authoritative filing {winner_filing.accession_number} "
            f"(form {winner_cand.form}, match {winner_cand.match_strength})."
        )

        return SECWinnerResolutionResult(
            mode=mode,
            status=SECWinnerStatus.SELECTED,
            cik=norm_target_cik,
            economic_group_key=economic_group_key,
            as_of=as_of,
            evaluation_snapshot_id=eval_snapshot.id,
            evaluation_snapshot_ids=sorted(list(eval_snapshot_ids), key=lambda u: str(u)),
            evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
            evaluation_snapshot_hash=eval_snapshot.payload_hash,
            selected_candidate=winner_cand,
            selected_raw_fact_id=winner_cand.raw_fact_id,
            selected_value=winner_cand.value,
            selected_unit=winner_cand.unit,
            selected_source_concept=winner_cand.source_concept,
            selected_filing_id=winner_filing.id,
            selected_accession_number=winner_cand.accession_number,
            selected_form=winner_cand.form,
            selection_confidence=confidence_level,
            selection_basis=basis,
            diagnostics=diagnostics_list or ["Resolved cross-filing disclosure frontier precedence."],
            eligible_candidate_ids=[c.id for c, f in eligible_candidates],
            rejected_candidates=rejected_candidates,
            corroborating_candidate_ids=sorted(all_corroborating_ids, key=lambda u: str(u)),
            superseded_candidate_ids=sorted(superseded_ids, key=lambda u: str(u)),
        )
