"""
backend/tests/test_private_storage.py
======================================
Tests for the Private Engine's canonical Point-In-Time (PIT) storage models
and Supabase migration schema definitions.

Verifies:
    - Raw snapshot creation and deterministic SHA-256 hash calculation
    - Normalized observation timestamp semantics (effective_date, observed_at, published_at, etc.)
    - Immutability & supersession record linkage
    - Enum mapping consistency between domain.py and migration 004 SQL
    - SQL migration structural validity (table names, columns, triggers, RLS)
"""

import hashlib
import os
import re
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from backend.engine.private.domain import (
    AssetClass,
    Currency,
    DataConfidenceLevel,
    DataStatus,
    InstrumentType,
    SourceTier,
)
from backend.engine.private.storage_models import (
    NormalizedObservationRecord,
    RawProviderSnapshotRecord,
    compute_payload_hash,
)

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "supabase", "migrations", "004_private_engine_pit_storage.sql"
)


class TestRawProviderSnapshot:
    """Tests for raw snapshot storage model and hashing."""

    def test_compute_payload_hash_deterministic(self):
        payload1 = {"symbol": "THYAO", "price": 300.5, "volume": 1500000}
        payload2 = {"volume": 1500000, "price": 300.5, "symbol": "THYAO"} # reordered keys
        
        hash1 = compute_payload_hash(payload1)
        hash2 = compute_payload_hash(payload2)
        
        assert hash1 == hash2
        assert len(hash1) == 64
        assert re.match(r"^[0-9a-f]{64}$", hash1)

    def test_compute_payload_hash_different_payloads(self):
        hash1 = compute_payload_hash({"price": 100})
        hash2 = compute_payload_hash({"price": 101})
        assert hash1 != hash2

    def test_raw_snapshot_create_factory(self):
        snapshot = RawProviderSnapshotRecord.create(
            provider="bist_direct",
            endpoint="/api/v1/quotes",
            request_params={"symbol": "THYAO.IS"},
            raw_payload={"close": 312.5, "open": 310.0},
            http_status=200,
            response_metadata={"latency_ms": 42},
            license_profile="PROPRIETARY",
        )
        
        assert snapshot.provider == "bist_direct"
        assert snapshot.endpoint == "/api/v1/quotes"
        assert snapshot.http_status == 200
        assert snapshot.payload_hash == compute_payload_hash({"close": 312.5, "open": 310.0})
        assert snapshot.is_superseded is False
        assert snapshot.supersedes_record_id is None

    def test_raw_snapshot_supersession_link(self):
        old_id = uuid4()
        revised_snapshot = RawProviderSnapshotRecord.create(
            provider="bist_direct",
            endpoint="/api/v1/quotes",
            request_params={"symbol": "THYAO.IS"},
            raw_payload={"close": 313.0, "open": 310.0},
            supersedes_record_id=old_id,
        )
        
        assert revised_snapshot.supersedes_record_id == old_id
        record_dict = revised_snapshot.to_record_dict()
        assert record_dict["supersedes_record_id"] == str(old_id)
        assert record_dict["payload_hash"] == compute_payload_hash({"close": 313.0, "open": 310.0})


class TestNormalizedObservation:
    """Tests for Point-In-Time (PIT) normalized observation records."""

    def test_normalized_observation_creation_and_pit_semantics(self):
        snap_id = uuid4()
        eff_date = date(2024, 3, 31) # Q1 financial period end
        pub_date = datetime(2024, 5, 10, 18, 0, 0, tzinfo=timezone.utc) # KAP announcement
        obs_date = datetime(2024, 5, 11, 8, 30, 0, tzinfo=timezone.utc) # Scraped by Sentinax

        obs = NormalizedObservationRecord(
            snapshot_id=snap_id,
            instrument_id="THYAO.IS",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.BIST_STOCK,
            observation_type="FINANCIAL_STATEMENT",
            observation_data={"revenue": 150_000_000_000, "net_income": 25_000_000_000},
            data_status=DataStatus.COMPLETE,
            confidence_level=DataConfidenceLevel.HIGH,
            source_tier=SourceTier.TIER_1_REGULATORY,
            effective_date=eff_date,
            observed_at=obs_date,
            published_at=pub_date,
            currency=Currency.TRY,
            missing_inputs=[],
            warnings=[],
            source_refs=["kap:THYAO@2024-03-31"],
        )

        assert obs.effective_date == date(2024, 3, 31)
        assert obs.published_at == pub_date
        assert obs.observed_at == obs_date
        assert obs.ingested_at is not None
        assert obs.is_superseded is False

        record_dict = obs.to_record_dict()
        assert record_dict["asset_class"] == "equity"
        assert record_dict["instrument_type"] == "bist_stock"
        assert record_dict["data_status"] == "complete"
        assert record_dict["confidence_level"] == "high"
        assert record_dict["source_tier"] == "tier_1"
        assert record_dict["effective_date"] == "2024-03-31"

    def test_partial_observation_tracks_missing_inputs(self):
        snap_id = uuid4()
        obs = NormalizedObservationRecord(
            snapshot_id=snap_id,
            instrument_id="NEWCO.IS",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.BIST_STOCK,
            observation_type="VALUATION_METRICS",
            observation_data={"pe_ratio": 12.5},
            data_status=DataStatus.PARTIAL,
            confidence_level=DataConfidenceLevel.MEDIUM,
            source_tier=SourceTier.TIER_3_AGGREGATOR,
            effective_date=date(2024, 6, 30),
            observed_at=datetime.now(timezone.utc),
            missing_inputs=["ev_ebitda", "fcf_yield"],
            warnings=["Operating cash flow missing; FCF yield omitted."],
        )

        assert obs.data_status == DataStatus.PARTIAL
        assert "ev_ebitda" in obs.missing_inputs
        assert len(obs.warnings) == 1


class TestMigrationSchemaValidity:
    """Verifies SQL migration structure and domain model alignment."""

    def test_migration_file_exists(self):
        assert os.path.exists(MIGRATION_PATH), f"Migration file not found at {MIGRATION_PATH}"

    def test_migration_contains_required_tables_and_columns(self):
        with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
            sql = f.read()

        # Tables
        assert "CREATE TABLE IF NOT EXISTS public.raw_provider_snapshots" in sql
        assert "CREATE TABLE IF NOT EXISTS public.normalized_observations" in sql

        # Raw snapshot columns
        for col in [
            "provider",
            "endpoint",
            "request_params",
            "retrieved_at",
            "http_status",
            "response_metadata",
            "content_type",
            "raw_payload",
            "storage_ref",
            "payload_hash",
            "schema_version",
            "parser_version",
            "license_profile",
            "supersedes_record_id",
            "is_superseded",
        ]:
            assert col in sql, f"Column '{col}' missing in raw_provider_snapshots migration"

        # Normalized observations PIT columns
        for col in [
            "effective_date",
            "published_at",
            "observed_at",
            "ingested_at",
            "revised_at",
            "supersedes_record_id",
            "is_superseded",
            "superseded_at",
            "data_status",
            "confidence_level",
            "source_tier",
            "missing_inputs",
            "warnings",
        ]:
            assert col in sql, f"Column '{col}' missing in normalized_observations migration"

    def test_migration_asset_classes_match_domain_enum(self):
        with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
            sql = f.read()

        for asset_class in AssetClass:
            assert f"'{asset_class.value}'" in sql, (
                f"AssetClass enum member '{asset_class.value}' not present in migration CHECK constraint"
            )

    def test_migration_data_status_matches_domain_enum(self):
        with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
            sql = f.read()

        for status in DataStatus:
            assert f"'{status.value}'" in sql, (
                f"DataStatus enum member '{status.value}' not present in migration CHECK constraint"
            )

    def test_migration_confidence_levels_match_domain_enum(self):
        with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
            sql = f.read()

        for conf in DataConfidenceLevel:
            assert f"'{conf.value}'" in sql, (
                f"DataConfidenceLevel enum member '{conf.value}' not present in migration CHECK constraint"
            )

    def test_migration_source_tiers_match_domain_enum(self):
        with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
            sql = f.read()

        for tier in SourceTier:
            assert f"'{tier.value}'" in sql, (
                f"SourceTier enum member '{tier.value}' not present in migration CHECK constraint"
            )

    def test_migration_has_supersession_trigger_and_pit_rpc(self):
        with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
            sql = f.read()

        assert "handle_record_supersession" in sql
        assert "trg_supersede_raw_snapshot" in sql
        assert "trg_supersede_norm_observation" in sql
        assert "get_pit_observation" in sql
        assert "ROW LEVEL SECURITY" in sql
