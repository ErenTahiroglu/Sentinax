"""
backend/engine/private/macro/state_query.py
===========================================
Exact persisted PIT macro observation -> `MacroStateInputFact` query bridge (Phase 17B).

Scope: transport and hydration only. No GrowthImpulse / PolicyInflationState / FinancialStress, z-score, regime or
technical overlay.

Architectural Invariants:
    - PostgREST serialises NUMERIC as a JSON number that a client may decode into a float. Therefore this service
      never reads `macro_observations.value`; it calls the migration-022 RPC `get_pit_macro_state_input`, which
      delegates PIT winner selection to the migration-006 authority and returns the value as TEXT
      (`o.value::text`). The text is parsed DIRECTLY into `Decimal` (no float/int/Decimal from transport, no
      quantization). No direct table access, no provider float.
    - I/O boundary: the Supabase/PostgREST-compatible client is injected (must expose a callable `rpc`); no
      environment, secret, factory, global client, ambient clock, UUID generation or randomness.
    - Every query has an explicit `effective_date`, `mode` and aware `as_of`; there is no implicit "current" query.
      The series must be an active VERIFIED registry series (checked before any RPC); `TR_POLICY_RATE` fails
      closed and is never aliased to `TR_TCMB_AOFM`.
    - The RPC returns at most one row: `[]` -> None (no eligible persisted observation), exactly one mapping with the
      exact expected key set -> hydrated fact; anything else fails closed (never "take row zero").
      A returned explicit UNAVAILABLE row (value_text NULL) yields a real fact whose value is None, which stays
      distinct from None (no row).
    - Enum strings are decoded exactly (no strip/case-fold/fallback). Timestamps and UUIDs are parsed strictly.
      Economic and PIT validation is delegated to the closed Phase 17A `build_macro_state_input_fact`; the fact's
      `mode` / `as_of` are the caller-supplied values, unnormalised.
"""

from __future__ import annotations

import decimal
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from backend.engine.private.domain import AsOfMode, DataConfidenceLevel, DataStatus, SourceTier
from backend.engine.private.macro.state_inputs import (
    MacroStateInputFact,
    _verified_definition,
    build_macro_state_input_fact,
)

_RPC_NAME = "get_pit_macro_state_input"
# Migration 006's RPC accepts upper-case tokens, while the Python AsOfMode enum serialises lower-case. The two are
# distinct contracts and are bridged explicitly here (never by case-folding the enum serialization).
_SQL_AS_OF_MODE = {
    AsOfMode.SYSTEM_AS_OF: "SYSTEM_AS_OF",
    AsOfMode.SOURCE_AS_OF: "SOURCE_AS_OF",
}
_ROW_KEYS = frozenset({
    "canonical_key", "observation_id", "snapshot_id", "effective_date", "value_text", "data_status",
    "confidence_level", "source_tier", "published_at", "observed_at", "ingested_at", "superseded_at",
})
_NUMERIC_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)

_ERR_CLIENT = "client must expose a callable rpc"
_ERR_KEY_TYPE = "canonical_key must be an exact str instance"
_ERR_KEY_EMPTY = "canonical_key must be non-empty"
_ERR_DATE_TYPE = "effective_date must be an exact date instance"
_ERR_MODE_TYPE = "mode must be an exact AsOfMode instance"
_ERR_AS_OF_TYPE = "as_of must be an exact timezone-aware datetime instance"
_ERR_RESPONSE = "macro state RPC response must be a list of at most one row"
_ERR_ROW_TYPE = "macro state RPC row must be a mapping"
_ERR_ROW_KEYS = "macro state RPC row must have exactly the expected columns"
_ERR_KEY_MISMATCH = "macro state RPC row canonical_key does not match the request"
_ERR_DATE_MISMATCH = "macro state RPC row effective_date does not match the request"
_ERR_VALUE_TYPE = "value_text must be a str or None"
_ERR_VALUE_PADDED = "value_text must be a non-empty unpadded numeric string"
_ERR_VALUE_NUMERIC = "value_text must be a finite numeric string"


def _parse_value_text(value: object) -> Decimal | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError(_ERR_VALUE_TYPE)
    if value == "" or value != value.strip():
        raise ValueError(_ERR_VALUE_PADDED)
    if _NUMERIC_TEXT.fullmatch(value) is None:
        raise ValueError(_ERR_VALUE_NUMERIC)
    try:
        parsed = Decimal(value)
    except decimal.InvalidOperation:
        raise ValueError(_ERR_VALUE_NUMERIC) from None
    if not parsed.is_finite():
        raise ValueError(_ERR_VALUE_NUMERIC)
    return parsed


def _parse_enum(enum_type, name: str, value: object):
    if type(value) is not str:
        raise TypeError(f"{name} must be a str")
    try:
        return enum_type(value)
    except ValueError:
        raise ValueError(f"unknown {name} value") from None


def _parse_timestamp(name: str, value: object, *, required: bool) -> datetime | None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if type(value) is not str:
        raise TypeError(f"{name} must be an ISO timestamp str")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} is not a valid ISO timestamp") from None


def _parse_uuid(name: str, value: object, *, required: bool) -> UUID | None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if type(value) is not str:
        raise TypeError(f"{name} must be a UUID str")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError(f"{name} is not a valid UUID") from None
    if str(parsed) != value:
        raise ValueError(f"{name} must be a canonical lowercase UUID")
    return parsed


def _parse_effective_date(value: object) -> date:
    if type(value) is not str:
        raise TypeError("effective_date must be an ISO date str")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("effective_date is not a valid ISO date") from None


class MacroStateInputQueryService:
    """Reads one exact, PIT-eligible macro state input through the migration-022 RPC."""

    def __init__(self, client: object) -> None:
        if not callable(getattr(client, "rpc", None)):
            raise TypeError(_ERR_CLIENT)
        self._client = client

    def get_input(
        self,
        *,
        canonical_key: str,
        effective_date: date,
        mode: AsOfMode,
        as_of: datetime,
    ) -> MacroStateInputFact | None:
        if type(canonical_key) is not str:
            raise TypeError(_ERR_KEY_TYPE)
        if len(canonical_key) == 0:
            raise ValueError(_ERR_KEY_EMPTY)
        _verified_definition(canonical_key)
        if type(effective_date) is not date:
            raise TypeError(_ERR_DATE_TYPE)
        if type(mode) is not AsOfMode:
            raise TypeError(_ERR_MODE_TYPE)
        if type(as_of) is not datetime or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise TypeError(_ERR_AS_OF_TYPE)

        response = self._client.rpc(
            _RPC_NAME,
            {
                "p_canonical_key": canonical_key,
                "p_effective_date": effective_date.isoformat(),
                "p_as_of": as_of.isoformat(),
                "p_as_of_mode": _SQL_AS_OF_MODE[mode],
            },
        ).execute()
        data = getattr(response, "data", None)
        if type(data) is not list or len(data) > 1:
            raise ValueError(_ERR_RESPONSE)
        if not data:
            return None
        row = data[0]
        if not isinstance(row, Mapping):
            raise TypeError(_ERR_ROW_TYPE)
        if set(row.keys()) != _ROW_KEYS:
            raise ValueError(_ERR_ROW_KEYS)

        if type(row["canonical_key"]) is not str:
            raise TypeError("canonical_key must be a str")
        if row["canonical_key"] != canonical_key:
            raise ValueError(_ERR_KEY_MISMATCH)
        row_date = _parse_effective_date(row["effective_date"])
        if row_date != effective_date:
            raise ValueError(_ERR_DATE_MISMATCH)

        return build_macro_state_input_fact(
            canonical_key=row["canonical_key"],
            effective_date=row_date,
            value=_parse_value_text(row["value_text"]),
            data_status=_parse_enum(DataStatus, "data_status", row["data_status"]),
            confidence_level=_parse_enum(DataConfidenceLevel, "confidence_level", row["confidence_level"]),
            source_tier=_parse_enum(SourceTier, "source_tier", row["source_tier"]),
            mode=mode,
            as_of=as_of,
            published_at=_parse_timestamp("published_at", row["published_at"], required=False),
            observed_at=_parse_timestamp("observed_at", row["observed_at"], required=True),
            ingested_at=_parse_timestamp("ingested_at", row["ingested_at"], required=True),
            superseded_at=_parse_timestamp("superseded_at", row["superseded_at"], required=False),
            observation_id=_parse_uuid("observation_id", row["observation_id"], required=True),
            snapshot_id=_parse_uuid("snapshot_id", row["snapshot_id"], required=False),
        )
