"""
backend/engine/private/sec/submissions.py
===========================================
SEC EDGAR Submissions Ingestion, Columnar Parallel Array Parser & Historical Traversal.

Official Endpoint:
    - Base URL: https://data.sec.gov/submissions/CIK##########.json
    - Secondary Files: https://data.sec.gov/submissions/{name} (referenced in payload["filings"]["files"])

Core Invariants:
    - Parallel columnar arrays in payload["filings"]["recent"] MUST have matching lengths.
    - Length mismatch strictly raises ProviderSchemaError (no silent truncation).
    - acceptance_datetime is parsed deterministically into UTC-aware datetime.
    - is_amendment is derived from form suffix ("/A") without overwriting the original filing.
    - Historical files references in payload["filings"]["files"] are safely validated against path traversal.
    - Raw snapshot record is generated for immutable auditability.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

from backend.engine.private.domain import (
    DataStatus,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderSchemaError,
)
from backend.engine.private.sec.cik import normalize_cik
from backend.engine.private.sec.client import SECEdgarClient
from backend.engine.private.sec.models import (
    SECFilingRecord,
    SECSubmissionMetadata,
    build_archive_url,
    build_submissions_url,
)
from backend.engine.private.storage_models import RawProviderSnapshotRecord

logger = logging.getLogger(__name__)


def parse_sec_datetime(dt_str: Optional[Union[str, int]]) -> Tuple[Optional[datetime], Optional[str]]:
    """
    Parses an SEC acceptanceDateTime string into a timezone-aware UTC datetime.

    Common SEC formats:
        - ISO 8601: "2024-05-15T16:05:34.000Z" or "2024-05-15 16:05:34"
        - Compact: "20240515160534"
    """
    if dt_str is None:
        return None, None

    s = str(dt_str).strip()
    if not s or s.lower() in ("null", "none"):
        return None, None

    # Format 1: Compact 14 digits (YYYYMMDDHHMMSS)
    if re.match(r"^\d{14}$", s):
        try:
            dt = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return dt, "SECOND_EXACT_UTC"
        except ValueError:
            pass

    # Format 2: ISO 8601 with Z or offset
    # Clean fractional seconds if longer than 6 digits
    clean_iso = re.sub(r"\.(\d{6})\d+Z$", r".\1Z", s)
    clean_iso = clean_iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(clean_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), "SECOND_EXACT_UTC"
    except ValueError:
        pass

    # Format 3: YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
            dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            return dt, "DATE_ONLY_UTC_MIDNIGHT"
        except ValueError:
            pass

    return None, f"UNPARSED_RAW:{s}"


def parse_sec_date(d_str: Optional[str]) -> Optional[date]:
    if not d_str or not isinstance(d_str, str):
        return None
    s = d_str.strip()
    if not s or s.lower() in ("null", "none"):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_sec_boolean(val: Any) -> Optional[bool]:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "t", "yes"):
            return True
        if s in ("0", "false", "f", "no"):
            return False
    return None


class SECSubmissionsParser:
    """
    Parses and validates SEC Submissions JSON payloads.
    """

    @staticmethod
    def parse_metadata(payload: Dict[str, Any]) -> SECSubmissionMetadata:
        raw_cik = payload.get("cik")
        if raw_cik is None:
            raise ProviderSchemaError("SEC submissions payload missing top-level 'cik'.", provider_name="SEC_EDGAR")

        cik = normalize_cik(raw_cik)
        name = payload.get("name") or payload.get("entityName") or ""

        return SECSubmissionMetadata(
            cik=cik,
            entity_name=name,
            entity_type=payload.get("entityType"),
            sic=payload.get("sic"),
            sic_description=payload.get("sicDescription"),
            tickers=payload.get("tickers") or [],
            exchanges=payload.get("exchanges") or [],
            ein=payload.get("ein"),
            description=payload.get("description"),
            website=payload.get("website"),
            investor_website=payload.get("investorWebsite"),
            category=payload.get("category"),
            fiscal_year_end=payload.get("fiscalYearEnd"),
            state_of_incorporation=payload.get("stateOfIncorporation"),
            state_of_incorporation_description=payload.get("stateOfIncorporationDescription"),
            addresses=payload.get("addresses") or {},
            phone=payload.get("phone"),
            flags=payload.get("flags"),
            raw_metadata=payload,
        )

    @classmethod
    def parse_filings(
        cls,
        payload: Dict[str, Any],
        instrument_id: Optional[UUID] = None,
        snapshot_id: Optional[UUID] = None,
    ) -> List[SECFilingRecord]:
        """
        Parses columnar arrays in payload["filings"]["recent"] into SECFilingRecord objects.
        Enforces strict columnar array length validation.
        """
        filings_block = payload.get("filings", {})
        recent = filings_block.get("recent")
        if not recent or not isinstance(recent, dict):
            # No recent filings block
            return []

        raw_cik = payload.get("cik")
        if raw_cik is None:
            raise ProviderSchemaError("Missing top-level 'cik' in submissions payload.", provider_name="SEC_EDGAR")
        canonical_cik = normalize_cik(raw_cik)

        # 1. Parallel Array Length Validation
        accessions = recent.get("accessionNumber")
        if accessions is None or not isinstance(accessions, list):
            return []

        total_records = len(accessions)
        for key, arr in recent.items():
            if isinstance(arr, list) and len(arr) != total_records:
                raise ProviderSchemaError(
                    f"Columnar array length mismatch in submissions recent filings: "
                    f"'accessionNumber' has {total_records} items, but '{key}' has {len(arr)} items. "
                    f"Fail closed to prevent silent zip truncation.",
                    provider_name="SEC_EDGAR",
                )

        # 2. Extract arrays
        filing_dates = recent.get("filingDate") or [None] * total_records
        report_dates = recent.get("reportDate") or [None] * total_records
        acceptance_datetimes = recent.get("acceptanceDateTime") or [None] * total_records
        acts = recent.get("act") or [None] * total_records
        forms = recent.get("form") or [None] * total_records
        file_numbers = recent.get("fileNumber") or [None] * total_records
        film_numbers = recent.get("filmNumber") or [None] * total_records
        items_list = recent.get("items") or [None] * total_records
        sizes = recent.get("size") or [None] * total_records
        is_xbrl_list = recent.get("isXBRL") or [None] * total_records
        is_inline_xbrl_list = recent.get("isInlineXBRL") or [None] * total_records
        primary_docs = recent.get("primaryDocument") or [None] * total_records
        primary_doc_descs = recent.get("primaryDocDescription") or [None] * total_records

        retrieved_at = datetime.now(timezone.utc)
        filings: List[SECFilingRecord] = []

        for i in range(total_records):
            accn = accessions[i]
            if not accn or not isinstance(accn, str):
                continue

            accn_clean = accn.strip()
            form_str = str(forms[i]).strip() if forms[i] is not None else "UNKNOWN"
            is_amend = form_str.upper().endswith("/A")

            f_date = parse_sec_date(filing_dates[i])
            r_date = parse_sec_date(report_dates[i])
            acc_dt, acc_prec = parse_sec_datetime(acceptance_datetimes[i])

            # Parse items if present
            raw_items = items_list[i]
            parsed_items: List[str] = []
            if isinstance(raw_items, list):
                parsed_items = [str(x) for x in raw_items if x is not None]
            elif isinstance(raw_items, str) and raw_items.strip():
                parsed_items = [x.strip() for x in raw_items.split(",") if x.strip()]

            # Size
            sz_val = sizes[i]
            size_int = int(sz_val) if sz_val is not None and str(sz_val).isdigit() else None

            # XBRL flags
            is_xb = parse_sec_boolean(is_xbrl_list[i])
            is_in_xb = parse_sec_boolean(is_inline_xbrl_list[i])

            p_doc = str(primary_docs[i]).strip() if primary_docs[i] else None
            p_desc = str(primary_doc_descs[i]).strip() if primary_doc_descs[i] else None

            rec = SECFilingRecord(
                cik=canonical_cik,
                accession_number=accn_clean,
                form=form_str,
                is_amendment=is_amend,
                filing_date=f_date,
                report_date=r_date,
                acceptance_datetime=acc_dt,
                acceptance_precision=acc_prec,
                act=str(acts[i]).strip() if acts[i] else None,
                file_number=str(file_numbers[i]).strip() if file_numbers[i] else None,
                film_number=str(film_numbers[i]).strip() if film_numbers[i] else None,
                items=parsed_items,
                size=size_int,
                is_xbrl=is_xb,
                is_inline_xbrl=is_in_xb,
                primary_document=p_doc,
                primary_doc_description=p_desc,
                source_url=build_archive_url(canonical_cik, accn_clean, p_doc),
                instrument_id=instrument_id,
                snapshot_id=snapshot_id,
                retrieved_at=retrieved_at,
                raw_metadata={
                    "act": acts[i],
                    "fileNumber": file_numbers[i],
                    "filmNumber": film_numbers[i],
                    "items": raw_items,
                },
            )
            filings.append(rec)

        return filings


class SECSubmissionsProvider:
    """
    Provider service for SEC Submissions ingestion.
    """
    provider_name: str = "SEC_SUBMISSIONS"
    provider_version: str = "1.0.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN

    def __init__(self, client: Optional[SECEdgarClient] = None) -> None:
        self.client = client or SECEdgarClient()

    async def fetch_submissions(
        self,
        cik: Union[str, int],
        instrument_id: Optional[UUID] = None,
        include_archived: bool = False,
        max_archived_files: int = 10,
    ) -> Tuple[SECSubmissionMetadata, List[SECFilingRecord], RawProviderSnapshotRecord]:
        """
        Fetches current submissions JSON for a CIK, parses filings, and optionally traverses historical file references.
        """
        canonical_cik = normalize_cik(cik)
        url = build_submissions_url(canonical_cik)
        retrieved_at = datetime.now(timezone.utc)

        # 1. Fetch main submissions JSON
        payload = await self.client.get_json(url)

        # 2. Build Raw Snapshot Record
        snapshot = RawProviderSnapshotRecord.create(
            provider="SEC_EDGAR",
            endpoint=f"/submissions/CIK{canonical_cik}.json",
            request_params={"cik": canonical_cik},
            raw_payload=payload,
            http_status=200,
            response_metadata={"user_agent_declared": True, "source_role": "SECURITIES_REGULATOR"},
            retrieved_at=retrieved_at,
        )

        # 3. Parse Metadata & Recent Filings
        meta = SECSubmissionsParser.parse_metadata(payload)
        filings = SECSubmissionsParser.parse_filings(
            payload,
            instrument_id=instrument_id,
            snapshot_id=snapshot.id,
        )

        # 4. Optional Historical Submissions Traversal (Bounded)
        if include_archived:
            archived_files = payload.get("filings", {}).get("files", [])
            seen_accessions = {f.accession_number for f in filings}

            count = 0
            for file_entry in archived_files:
                if count >= max_archived_files:
                    break
                fname = file_entry.get("name")
                if not fname or not isinstance(fname, str):
                    continue

                # Path Traversal Guard
                fname_clean = fname.strip()
                if "/" in fname_clean or "\\" in fname_clean or ".." in fname_clean or not fname_clean.endswith(".json"):
                    logger.warning(f"Skipping suspect historical submission filename: '{fname}'")
                    continue

                archived_url = f"https://data.sec.gov/submissions/{fname_clean}"
                try:
                    arch_payload = await self.client.get_json(archived_url)
                    # The archived file payload structure mirrors the recent columnar array structure
                    arch_filings_dict = {"cik": canonical_cik, "filings": {"recent": arch_payload}}
                    arch_filings = SECSubmissionsParser.parse_filings(
                        arch_filings_dict,
                        instrument_id=instrument_id,
                        snapshot_id=snapshot.id,
                    )
                    for af in arch_filings:
                        if af.accession_number not in seen_accessions:
                            filings.append(af)
                            seen_accessions.add(af.accession_number)
                    count += 1
                except Exception as e:
                    logger.error(f"Failed to fetch historical submission file '{fname_clean}': {e}")
                    raise

        return meta, filings, snapshot
