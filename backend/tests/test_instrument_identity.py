"""
backend/tests/test_instrument_identity.py
==========================================
Unit tests for the Instrument Identity, Symbology & Corporate Action Resolution layer.

Verifies:
    - Ticker is NOT the primary key (internal_instrument_id is a pure UUID)
    - FB -> META rename resolves to the identical internal UUID across all dates
    - Provider alias validity intervals enforce [valid_from, valid_to) half-open semantics
    - Overlapping provider alias intervals are strictly rejected
    - Historical ticker reuse across non-overlapping periods resolves to distinct instruments
    - Corporate actions enforce action-type-specific semantic validation (split vs dividend)
    - Supabase migration 005 SQL schema validity
"""

import os
from datetime import date
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    AssetClass,
    CorporateActionType,
    Currency,
    InstrumentStatus,
    InstrumentType,
)
from backend.engine.private.identity import (
    CorporateActionRecord,
    InstrumentRecord,
    InstrumentResolverService,
    ProviderAliasRecord,
)

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "supabase", "migrations", "005_instrument_identity.sql"
)


class TestInstrumentIdentityAndSymbology:
    """Tests for core identity, symbology, and PIT resolver service."""

    @pytest.fixture
    def resolver_with_meta(self) -> tuple[InstrumentResolverService, UUID]:
        """
        Sets up an instrument for Meta Platforms Inc.
        Historical context:
            - Ticker was 'FB' until 2022-06-09
            - Ticker changed to 'META' on 2022-06-09
            - Internal instrument identity is a pure UUID.
        """
        service = InstrumentResolverService()
        meta_uuid = uuid4()

        # 1. Master Instrument (Pure UUID, detached from ticker)
        meta_inst = InstrumentRecord(
            id=meta_uuid,
            internal_instrument_id=meta_uuid,
            canonical_name="Meta Platforms Inc.",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
            mic="XNAS",
            cik="0001326801",
            isin="US30303M1027",
            status=InstrumentStatus.ACTIVE,
            valid_from=date(2012, 5, 18),
        )
        service.register_instrument(meta_inst)

        # 2. Historical Alias: 'FB' [2012-05-18, 2022-06-09)
        alias_fb = ProviderAliasRecord(
            instrument_id=meta_uuid,
            provider="yfinance",
            provider_symbol="FB",
            valid_from=date(2012, 5, 18),
            valid_to=date(2022, 6, 9),
            is_primary=True,
        )
        service.register_alias(alias_fb)

        # 3. Modern Alias: 'META' [2022-06-09, infinity)
        alias_meta = ProviderAliasRecord(
            instrument_id=meta_uuid,
            provider="yfinance",
            provider_symbol="META",
            valid_from=date(2022, 6, 9),
            valid_to=None,
            is_primary=True,
        )
        service.register_alias(alias_meta)

        # 4. Corporate Action: Symbol Change
        ca_rename = CorporateActionRecord(
            instrument_id=meta_uuid,
            action_type=CorporateActionType.SYMBOL_CHANGE,
            effective_date=date(2022, 6, 9),
            old_symbol="FB",
            new_symbol="META",
            metadata={"notes": "Company rebranding from Facebook Inc. to Meta Platforms Inc."},
        )
        service.register_corporate_action(ca_rename)

        return service, meta_uuid

    def test_ticker_not_primary_key_uses_internal_uuid(self, resolver_with_meta):
        """Validates that internal_instrument_id is a UUID, not a ticker string."""
        service, meta_uuid = resolver_with_meta
        inst = service.get_instrument_by_internal_id(meta_uuid)
        assert inst is not None
        assert isinstance(inst.internal_instrument_id, UUID)
        assert inst.internal_instrument_id == meta_uuid
        assert inst.cik == "0001326801"
        assert inst.asset_class == AssetClass.EQUITY

    def test_resolve_historical_provider_symbol_to_internal_id(self, resolver_with_meta):
        """Querying 'FB' on a historical date (2020) resolves to canonical internal UUID."""
        service, meta_uuid = resolver_with_meta
        resolved = service.resolve_provider_symbol_to_internal_id(
            provider="yfinance",
            provider_symbol="FB",
            as_of_date=date(2020, 1, 15),
        )
        assert resolved == meta_uuid

    def test_resolve_modern_provider_symbol_to_internal_id(self, resolver_with_meta):
        """Querying 'META' on a current date (2024) resolves to the exact same canonical internal UUID."""
        service, meta_uuid = resolver_with_meta
        resolved = service.resolve_provider_symbol_to_internal_id(
            provider="yfinance",
            provider_symbol="META",
            as_of_date=date(2024, 1, 15),
        )
        assert resolved == meta_uuid

    def test_resolve_symbol_at_invalid_dates_returns_none(self, resolver_with_meta):
        """Querying 'META' before it existed (2020) or 'FB' after rename (2024) returns None."""
        service, _ = resolver_with_meta
        # 'META' did not exist in 2020
        assert service.resolve_provider_symbol_to_internal_id(
            provider="yfinance",
            provider_symbol="META",
            as_of_date=date(2020, 1, 1),
        ) is None

        # 'FB' was no longer valid in 2024
        assert service.resolve_provider_symbol_to_internal_id(
            provider="yfinance",
            provider_symbol="FB",
            as_of_date=date(2024, 1, 1),
        ) is None

    def test_exact_boundary_resolution_half_open_interval(self, resolver_with_meta):
        """
        Tests [valid_from, valid_to) boundary semantics:
        - On 2022-06-08 (day before rename): 'FB' is valid, 'META' is invalid.
        - On 2022-06-09 (effective date): 'FB' is invalid (exclusive), 'META' is valid (inclusive).
        """
        service, meta_uuid = resolver_with_meta

        # 2022-06-08: FB active, META not yet
        assert service.resolve_provider_symbol_to_internal_id("yfinance", "FB", date(2022, 6, 8)) == meta_uuid
        assert service.resolve_provider_symbol_to_internal_id("yfinance", "META", date(2022, 6, 8)) is None

        # 2022-06-09: FB expired, META active
        assert service.resolve_provider_symbol_to_internal_id("yfinance", "FB", date(2022, 6, 9)) is None
        assert service.resolve_provider_symbol_to_internal_id("yfinance", "META", date(2022, 6, 9)) == meta_uuid

    def test_resolve_internal_id_to_date_specific_symbol(self, resolver_with_meta):
        """Resolves internal ID to date-specific ticker symbol across the rename boundary."""
        service, meta_uuid = resolver_with_meta
        sym_past = service.resolve_internal_id_to_provider_symbol(
            internal_instrument_id=meta_uuid,
            provider="yfinance",
            as_of_date=date(2021, 5, 1),
        )
        assert sym_past == "FB"

        sym_current = service.resolve_internal_id_to_provider_symbol(
            internal_instrument_id=meta_uuid,
            provider="yfinance",
            as_of_date=date(2023, 5, 1),
        )
        assert sym_current == "META"

    def test_ticker_change_preserves_historical_series_continuity(self, resolver_with_meta):
        """
        Critical Test: Verifies that time-series queries for historical data points
        all map to the identical internal instrument UUID.
        """
        service, meta_uuid = resolver_with_meta
        historical_points = [
            (date(2015, 6, 1), "FB"),
            (date(2018, 12, 1), "FB"),
            (date(2022, 6, 8), "FB"),      # Last day of FB
            (date(2022, 6, 9), "META"),    # First day of META
            (date(2024, 6, 1), "META"),
        ]

        for dt, expected_symbol in historical_points:
            internal_id = service.resolve_provider_symbol_to_internal_id(
                provider="yfinance",
                provider_symbol=expected_symbol,
                as_of_date=dt,
            )
            assert internal_id == meta_uuid, f"Failed reverse lookup at {dt} for {expected_symbol}"

            provider_sym = service.resolve_internal_id_to_provider_symbol(
                internal_instrument_id=meta_uuid,
                provider="yfinance",
                as_of_date=dt,
            )
            assert provider_sym == expected_symbol, f"Failed forward lookup at {dt}: expected {expected_symbol}, got {provider_sym}"


class TestProviderAliasIntervalIntegrity:
    """Tests for interval overlap rejection and historical ticker reuse."""

    def test_overlapping_interval_rejected(self):
        """Registering an overlapping validity interval for the same (provider, symbol) must raise ValueError."""
        service = InstrumentResolverService()
        inst_a = uuid4()
        inst_b = uuid4()

        service.register_instrument(InstrumentRecord(
            id=inst_a,
            internal_instrument_id=inst_a,
            canonical_name="Company A",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
        ))
        service.register_instrument(InstrumentRecord(
            id=inst_b,
            internal_instrument_id=inst_b,
            canonical_name="Company B",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
        ))

        # Alias 1: XYZ [2010-01-01, 2020-01-01) -> Company A
        service.register_alias(ProviderAliasRecord(
            instrument_id=inst_a,
            provider="yfinance",
            provider_symbol="XYZ",
            valid_from=date(2010, 1, 1),
            valid_to=date(2020, 1, 1),
        ))

        # Alias 2 (OVERLAPPING): XYZ [2019-01-01, 2024-01-01) -> Company B (overlaps 2019)
        with pytest.raises(ValueError, match="Overlapping alias detected"):
            service.register_alias(ProviderAliasRecord(
                instrument_id=inst_b,
                provider="yfinance",
                provider_symbol="XYZ",
                valid_from=date(2019, 1, 1),
                valid_to=date(2024, 1, 1),
            ))

    def test_historical_ticker_reuse_non_overlapping_succeeds(self):
        """
        Historical ticker reuse: Ticker XYZ was used by Company A from 2000 to 2010,
        then reassigned to Company B from 2015 to 2025. Both must resolve correctly without conflict.
        """
        service = InstrumentResolverService()
        comp_a_id = uuid4()
        comp_b_id = uuid4()

        service.register_instrument(InstrumentRecord(
            id=comp_a_id,
            internal_instrument_id=comp_a_id,
            canonical_name="Old Telecom Corp",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
        ))
        service.register_instrument(InstrumentRecord(
            id=comp_b_id,
            internal_instrument_id=comp_b_id,
            canonical_name="New Biotech Inc",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
        ))

        # Period 1: [2000-01-01, 2010-01-01) -> Company A
        service.register_alias(ProviderAliasRecord(
            instrument_id=comp_a_id,
            provider="yfinance",
            provider_symbol="XYZ",
            valid_from=date(2000, 1, 1),
            valid_to=date(2010, 1, 1),
        ))

        # Period 2 (Disjoint): [2015-01-01, 2025-01-01) -> Company B
        service.register_alias(ProviderAliasRecord(
            instrument_id=comp_b_id,
            provider="yfinance",
            provider_symbol="XYZ",
            valid_from=date(2015, 1, 1),
            valid_to=date(2025, 1, 1),
        ))

        # Resolution tests
        # 1. 2005-06-01 -> Company A
        assert service.resolve_provider_symbol_to_internal_id("yfinance", "XYZ", date(2005, 6, 1)) == comp_a_id
        # 2. 2020-06-01 -> Company B
        assert service.resolve_provider_symbol_to_internal_id("yfinance", "XYZ", date(2020, 6, 1)) == comp_b_id
        # 3. 2012-06-01 (dormant period) -> None
        assert service.resolve_provider_symbol_to_internal_id("yfinance", "XYZ", date(2012, 6, 1)) is None


class TestCorporateActionSemanticValidation:
    """Tests for corporate action semantic field isolation and validation."""

    def test_split_action_requires_split_factor_and_no_cash(self):
        inst_id = uuid4()
        
        # Valid split
        split = CorporateActionRecord(
            instrument_id=inst_id,
            action_type=CorporateActionType.SPLIT,
            effective_date=date(2024, 6, 10),
            split_factor=10.0,
        )
        assert split.split_factor == 10.0

        # Invalid: missing split_factor
        with pytest.raises(ValueError, match="split_factor > 0"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2024, 6, 10),
            )

        # Invalid: split with cash_amount
        with pytest.raises(ValueError, match="must not have cash_amount"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2024, 6, 10),
                split_factor=2.0,
                cash_amount=5.0,
            )

    def test_dividend_action_requires_cash_amount_and_no_split(self):
        inst_id = uuid4()

        # Valid dividend
        div = CorporateActionRecord(
            instrument_id=inst_id,
            action_type=CorporateActionType.DIVIDEND,
            effective_date=date(2024, 5, 15),
            cash_amount=3.25,
            currency=Currency.TRY,
        )
        assert div.cash_amount == 3.25

        # Invalid: dividend with split_factor
        with pytest.raises(ValueError, match="must not have split_factor"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.DIVIDEND,
                effective_date=date(2024, 5, 15),
                cash_amount=3.25,
                split_factor=2.0,
            )

    def test_symbol_change_requires_old_and_new_symbols(self):
        inst_id = uuid4()

        # Valid symbol change
        ca = CorporateActionRecord(
            instrument_id=inst_id,
            action_type=CorporateActionType.SYMBOL_CHANGE,
            effective_date=date(2022, 6, 9),
            old_symbol="FB",
            new_symbol="META",
        )
        assert ca.old_symbol == "FB"
        assert ca.new_symbol == "META"

        # Invalid: missing new_symbol
        with pytest.raises(ValueError, match="requires both old_symbol and new_symbol"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.SYMBOL_CHANGE,
                effective_date=date(2022, 6, 9),
                old_symbol="FB",
            )


class TestCumulativeSplitFactor:
    """Tests cumulative split adjustments with pure UUID identity."""

    def test_cumulative_split_factor_calculation(self):
        service = InstrumentResolverService()
        nvda_id = uuid4()

        inst = InstrumentRecord(
            id=nvda_id,
            internal_instrument_id=nvda_id,
            canonical_name="NVIDIA Corporation",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
            valid_from=date(1999, 1, 22),
        )
        service.register_instrument(inst)

        # 4:1 Split on 2021-07-20
        service.register_corporate_action(
            CorporateActionRecord(
                instrument_id=nvda_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2021, 7, 20),
                split_factor=4.0,
            )
        )

        # 10:1 Split on 2024-06-10
        service.register_corporate_action(
            CorporateActionRecord(
                instrument_id=nvda_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2024, 6, 10),
                split_factor=10.0,
            )
        )

        # 1. Before both splits (2020) to after both splits (2025) -> 4 * 10 = 40x
        total_split = service.get_cumulative_split_factor(
            nvda_id,
            from_date=date(2020, 1, 1),
            to_date=date(2025, 1, 1),
        )
        assert total_split == 40.0

        # 2. Between first and second split (2022 to 2025) -> 10x
        second_split = service.get_cumulative_split_factor(
            nvda_id,
            from_date=date(2022, 1, 1),
            to_date=date(2025, 1, 1),
        )
        assert second_split == 10.0


class TestMigration005Schema:
    """Verifies SQL migration 005 schema definitions and constraints."""

    def test_migration_005_exists(self):
        assert os.path.exists(MIGRATION_PATH), f"Migration file not found at {MIGRATION_PATH}"

    def test_migration_005_tables_and_columns(self):
        with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
            sql = f.read()

        # Tables
        assert "CREATE TABLE IF NOT EXISTS public.instruments" in sql
        assert "CREATE TABLE IF NOT EXISTS public.provider_aliases" in sql
        assert "CREATE TABLE IF NOT EXISTS public.corporate_actions" in sql

        # instruments columns
        for col in ["internal_instrument_id", "canonical_name", "asset_class", "instrument_type", "isin", "cik", "mic", "currency", "status", "valid_from", "valid_to"]:
            assert col in sql, f"Column '{col}' missing in instruments table"

        # provider_aliases columns & exclusion constraint
        for col in ["instrument_id", "provider", "provider_symbol", "valid_from", "valid_to", "is_primary"]:
            assert col in sql, f"Column '{col}' missing in provider_aliases table"
        assert "provider_aliases_no_overlap" in sql
        assert "btree_gist" in sql

        # corporate_actions action-specific columns & check constraints
        for col in ["instrument_id", "action_type", "effective_date", "old_symbol", "new_symbol", "split_factor", "cash_amount"]:
            assert col in sql, f"Column '{col}' missing in corporate_actions table"
        assert "chk_ca_split" in sql
        assert "chk_ca_dividend" in sql
        assert "chk_ca_symbol_change" in sql

        # Resolver RPCs
        assert "resolve_provider_symbol_to_instrument" in sql
        assert "resolve_instrument_to_provider_symbol" in sql
