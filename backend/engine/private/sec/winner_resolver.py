"""
backend/engine/private/sec/winner_resolver.py
===============================================
SEC EDGAR Phase 8B.2B: Snapshot-Scoped PIT Filing Precedence,
Restatement Reconciliation & Winner Resolution.

Core Invariants:
    - Snapshot-Scoped Isolation: Only candidates from the selected evaluation snapshot are considered.
      Older snapshot candidates are NEVER mixed in or resurrected in CURRENT_REPORTED.
    - Deterministic Mode Semantics:
        * CURRENT_REPORTED: Latest valid full CompanyFacts snapshot locally observed for the CIK.
        * SYSTEM_AS_OF: Latest valid full CompanyFacts snapshot with retrieved_at <= as_of.
        * SOURCE_AS_OF: Fail-closed (UNAVAILABLE_SOURCE_AS_OF) because SEC CompanyFacts does not provide
          exact historical external API state reconstruction.
    - Disclosure Chronology: Acceptance aware UTC datetime > SEC local naive datetime > filing_date fallback.
      Lexicographical accession order, UUIDs, or DB created_at are NEVER used as economic authority.
    - Pure / Deterministic Engine: Zero external network calls, zero database writes, zero metric calculations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
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
# Helper Functions: Semantic Quality & Disclosure Chronology
# ─────────────────────────────────────────────────────────────────────────────

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
        2. Both have naive acceptance_local_datetime (same local SEC basis) -> direct comparison
        3. Fallback to filing_date if different
        4. If same filing_date with no comparable acceptance -> UNORDERABLE
    """
    # 1. Aware acceptance timestamps
    if a.acceptance_datetime is not None and b.acceptance_datetime is not None:
        if a.acceptance_datetime > b.acceptance_datetime:
            return FilingDisclosureComparison.A_LATER
        if a.acceptance_datetime < b.acceptance_datetime:
            return FilingDisclosureComparison.B_LATER
        return FilingDisclosureComparison.SAME

    # 2. Local naive acceptance timestamps
    if a.acceptance_local_datetime is not None and b.acceptance_local_datetime is not None:
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
    Implements Snapshot-Scoped PIT Filing Precedence and Restatement Reconciliation.
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
        # 2. Evaluation Snapshot Selection
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
            # Pick latest valid snapshot
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
            eval_snapshot = latest_candidates[0]

        elif mode == SECWinnerResolutionMode.SYSTEM_AS_OF:
            # Filter snapshots retrieved_at <= as_of
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
            eval_snapshot = latest_candidates[0]

        # ─────────────────────────────────────────────────────────────────────
        # 3. Snapshot-Scoped Candidate Filtering & Eligibility Checks
        # ─────────────────────────────────────────────────────────────────────
        filing_map: Dict[str, SECFilingRecord] = {}
        filing_id_map: Dict[UUID, SECFilingRecord] = {}
        for f in filings:
            if f.accession_number:
                filing_map[f.accession_number.strip()] = f
            if f.id:
                filing_id_map[f.id] = f

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

        for cand in candidates:
            # 3.1 Strict Snapshot Membership
            if cand.snapshot_id != eval_snapshot.id:
                rejected_candidates.append({
                    "candidate_id": str(cand.id),
                    "raw_fact_id": str(cand.raw_fact_id),
                    "accession_number": cand.accession_number,
                    "reason": f"Snapshot mismatch: candidate belongs to snapshot {cand.snapshot_id}, evaluation snapshot is {eval_snapshot.id}.",
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

            # 3.4 Alignment Status Eligibility
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
                if canonical_concept != "SHARES_OUTSTANDING":
                    rejected_candidates.append({
                        "candidate_id": str(cand.id),
                        "raw_fact_id": str(cand.raw_fact_id),
                        "accession_number": cand.accession_number,
                        "reason": f"COVER_DATE_CONTEXT is only eligible for SHARES_OUTSTANDING, got {canonical_concept}.",
                    })
                    continue

            # 3.5 Filing Lineage Resolution
            resolved_filing: Optional[SECFilingRecord] = None
            if cand.accession_number:
                resolved_filing = filing_map.get(cand.accession_number.strip())
            elif cand.filing_id:
                resolved_filing = filing_id_map.get(cand.filing_id)

            if not resolved_filing:
                rejected_candidates.append({
                    "candidate_id": str(cand.id),
                    "raw_fact_id": str(cand.raw_fact_id),
                    "accession_number": cand.accession_number,
                    "reason": "Filing record could not be resolved from supplied filings.",
                })
                continue

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

            # 3.6 Temporal Lookahead Protection (SYSTEM_AS_OF)
            if mode == SECWinnerResolutionMode.SYSTEM_AS_OF and as_of is not None:
                if resolved_filing.filing_date and resolved_filing.filing_date > as_of.date():
                    return SECWinnerResolutionResult(
                        mode=mode,
                        status=SECWinnerStatus.INVALID_TEMPORAL_LINEAGE,
                        cik=norm_target_cik,
                        economic_group_key=economic_group_key,
                        as_of=as_of,
                        evaluation_snapshot_id=eval_snapshot.id,
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
                evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                evaluation_snapshot_hash=eval_snapshot.payload_hash,
                selection_basis="No candidates met snapshot-scoped winner eligibility criteria.",
                diagnostics=["All candidates were filtered out by snapshot isolation or eligibility rules."],
                rejected_candidates=rejected_candidates,
            )

        # ─────────────────────────────────────────────────────────────────────
        # 4. Same-Filing Semantic Reconciliation
        # ─────────────────────────────────────────────────────────────────────
        filing_groups: Dict[str, List[Tuple[SECPeriodizedFactCandidate, SECFilingRecord]]] = {}
        for cand, filing in eligible_candidates:
            key = cand.accession_number.strip() if cand.accession_number else str(filing.id)
            if key not in filing_groups:
                filing_groups[key] = []
            filing_groups[key].append((cand, filing))

        filing_representatives: List[Dict[str, Any]] = []
        all_corroborating_ids: List[UUID] = []

        for f_key, cand_list in filing_groups.items():
            # Sort by semantic quality rank (ascending: EXACT=1 < COMPATIBLE=2 < LEGACY_COMPATIBLE=3), then variant_priority
            sorted_cands = sorted(
                cand_list,
                key=lambda item: (get_semantic_quality_rank(item[0].match_strength), item[0].variant_priority)
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

            filing_representatives.append({
                "candidate": top_cand,
                "filing": top_filing,
                "corroborating_ids": corroborating_for_filing,
                "quality_rank": top_rank,
            })

        # ─────────────────────────────────────────────────────────────────────
        # 5. Cross-Filing Restatement & Amendment Reconciliation
        # ─────────────────────────────────────────────────────────────────────
        if len(filing_representatives) == 1:
            winner_rep = filing_representatives[0]
            winner_cand: SECPeriodizedFactCandidate = winner_rep["candidate"]
            winner_filing: SECFilingRecord = winner_rep["filing"]

            return SECWinnerResolutionResult(
                mode=mode,
                status=SECWinnerStatus.SELECTED,
                cik=norm_target_cik,
                economic_group_key=economic_group_key,
                as_of=as_of,
                evaluation_snapshot_id=eval_snapshot.id,
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
                diagnostics=["Single filing representative selected."],
                eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                rejected_candidates=rejected_candidates,
                corroborating_candidate_ids=all_corroborating_ids,
                superseded_candidate_ids=[],
            )

        # Multiple filing representatives: sort/reconcile across filings
        superseded_ids: List[UUID] = []
        winner_rep = filing_representatives[0]
        diagnostics_list: List[str] = []
        confidence_level: str = winner_rep["candidate"].classification_confidence

        for next_rep in filing_representatives[1:]:
            cmp = compare_filing_disclosure_order(winner_rep["filing"], next_rep["filing"])

            if cmp == FilingDisclosureComparison.UNORDERABLE or cmp == FilingDisclosureComparison.SAME:
                if next_rep["candidate"].value != winner_rep["candidate"].value:
                    return SECWinnerResolutionResult(
                        mode=mode,
                        status=SECWinnerStatus.AMBIGUOUS_DISCLOSURE_ORDER,
                        cik=norm_target_cik,
                        economic_group_key=economic_group_key,
                        as_of=as_of,
                        evaluation_snapshot_id=eval_snapshot.id,
                        evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                        evaluation_snapshot_hash=eval_snapshot.payload_hash,
                        selection_basis=f"Filings {winner_rep['filing'].accession_number} and {next_rep['filing'].accession_number} have unorderable disclosure chronology with differing values.",
                        diagnostics=["Ambiguous disclosure order across multiple filings with differing values."],
                        eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                        rejected_candidates=rejected_candidates,
                    )
                else:
                    # Same value: prefer higher semantic quality
                    if next_rep["quality_rank"] < winner_rep["quality_rank"]:
                        all_corroborating_ids.append(winner_rep["candidate"].id)
                        winner_rep = next_rep
                    else:
                        all_corroborating_ids.append(next_rep["candidate"].id)

            elif cmp == FilingDisclosureComparison.B_LATER:
                # next_rep is chronologically later
                # Check semantic quality
                if next_rep["quality_rank"] <= winner_rep["quality_rank"]:
                    # Later disclosure is same or higher quality: it wins!
                    if next_rep["candidate"].value != winner_rep["candidate"].value:
                        superseded_ids.append(winner_rep["candidate"].id)
                        diagnostics_list.append(
                            f"Later disclosure {next_rep['filing'].accession_number} ({next_rep['candidate'].value}) supersedes prior disclosure {winner_rep['filing'].accession_number} ({winner_rep['candidate'].value})."
                        )
                    else:
                        all_corroborating_ids.append(winner_rep["candidate"].id)
                    winner_rep = next_rep
                    confidence_level = next_rep["candidate"].classification_confidence

                else:
                    # Later disclosure has LOWER semantic quality (e.g. older EXACT vs later COMPATIBLE)
                    if next_rep["candidate"].value == winner_rep["candidate"].value:
                        # Same value: select later or retain earlier with degraded confidence
                        all_corroborating_ids.append(next_rep["candidate"].id)
                        confidence_level = "MEDIUM"
                        diagnostics_list.append(
                            f"Later disclosure {next_rep['filing'].accession_number} has lower semantic quality ({next_rep['candidate'].match_strength}) but value corroborates ({next_rep['candidate'].value})."
                        )
                    else:
                        # Different values with lower quality later: SEMANTIC_SCOPE_CONFLICT
                        return SECWinnerResolutionResult(
                            mode=mode,
                            status=SECWinnerStatus.SEMANTIC_SCOPE_CONFLICT,
                            cik=norm_target_cik,
                            economic_group_key=economic_group_key,
                            as_of=as_of,
                            evaluation_snapshot_id=eval_snapshot.id,
                            evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                            evaluation_snapshot_hash=eval_snapshot.payload_hash,
                            selection_basis=f"Later disclosure {next_rep['filing'].accession_number} has lower semantic quality ({next_rep['candidate'].match_strength}) with differing value {next_rep['candidate'].value} vs prior exact {winner_rep['candidate'].value}.",
                            diagnostics=["Semantic scope conflict: cannot overwrite higher-quality fact with lower-quality alias of differing value."],
                            eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                            rejected_candidates=rejected_candidates,
                        )

            elif cmp == FilingDisclosureComparison.A_LATER:
                # winner_rep is chronologically later
                if winner_rep["quality_rank"] <= next_rep["quality_rank"]:
                    if winner_rep["candidate"].value != next_rep["candidate"].value:
                        superseded_ids.append(next_rep["candidate"].id)
                    else:
                        all_corroborating_ids.append(next_rep["candidate"].id)
                else:
                    if winner_rep["candidate"].value == next_rep["candidate"].value:
                        all_corroborating_ids.append(next_rep["candidate"].id)
                        confidence_level = "MEDIUM"
                    else:
                        return SECWinnerResolutionResult(
                            mode=mode,
                            status=SECWinnerStatus.SEMANTIC_SCOPE_CONFLICT,
                            cik=norm_target_cik,
                            economic_group_key=economic_group_key,
                            as_of=as_of,
                            evaluation_snapshot_id=eval_snapshot.id,
                            evaluation_snapshot_retrieved_at=eval_snapshot.retrieved_at,
                            evaluation_snapshot_hash=eval_snapshot.payload_hash,
                            selection_basis="Semantic scope conflict between differing quality disclosures.",
                            diagnostics=["Semantic scope conflict across filings."],
                            eligible_candidate_ids=[c.id for c, f in eligible_candidates],
                            rejected_candidates=rejected_candidates,
                        )

        winner_cand = winner_rep["candidate"]
        winner_filing = winner_rep["filing"]

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
            corroborating_candidate_ids=all_corroborating_ids,
            superseded_candidate_ids=superseded_ids,
        )
