"""
backend/tests/test_instrument_identity.py
==========================================
Unit tests for the Instrument Identity, Symbology & Corporate Action Resolution layer.

Verifies:
    - Single canonical instrument identity (`id: UUID`) across Sentinax
    - Absence of redundant internal_instrument_id
    - Elimination of dangerous defaults (currency is required, MIC defaults to None)
    - Case-insensitive alias overlap prevention (e.g. Yahoo/yahoo, META/meta)
    - FB -> META rename resolves to the identical canonical UUID across all dates
    - Provider alias validity intervals enforce [valid_from, valid_to) half-open semantics
    - Historical ticker reuse across non-overlapping periods resolves to distinct instruments
    - Corporate actions enforce strict field exclusivity (SPLIT, DIVIDEND, MERGER, DELISTING)
    - Supabase migration 005 SQL schema validity and foreign key constraints
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
    """Tests for single canonical UUID identity and PIT resolver service."""

    @pytest.fixture
    def resolver_with_meta(self) -> tuple[InstrumentResolverService, UUID]:
        """
        Sets up an instrument for Meta Platforms Inc.
        Historical context:
            - Ticker was 'FB' until 2022-06-09
            - Ticker changed to 'META' on 2022-06-09
            - Single canonical identity is `id: UUID`.
        """
        service = InstrumentResolverService()
        meta_uuid = uuid4()

        # 1. Master Instrument (Single canonical UUID `id`, explicit Currency & MIC)
        meta_inst = InstrumentRecord(
            id=meta_uuid,
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

    def test_single_canonical_uuid_identity(self, resolver_with_meta):
        """Validates that id is the sole canonical identity field."""
        service, meta_uuid = resolver_with_meta
        inst = service.get_instrument_by_id(meta_uuid)
        assert inst is not None
        assert isinstance(inst.id, UUID)
        assert inst.id == meta_uuid
        assert not hasattr(inst, "internal_instrument_id")
        assert inst.cik == "0001326801"
        assert inst.asset_class == AssetClass.EQUITY

    def test_resolve_historical_provider_symbol_to_instrument_id(self, resolver_with_meta):
        """Querying 'FB' on a historical date (2020) resolves to canonical UUID."""
        service, meta_uuid = resolver_with_meta
        resolved = service.resolve_provider_symbol_to_instrument_id(
            provider="yfinance",
            provider_symbol="FB",
            as_of_date=date(2020, 1, 15),
        )
        assert resolved == meta_uuid

    def test_resolve_modern_provider_symbol_to_instrument_id(self, resolver_with_meta):
        """Querying 'META' on a current date (2024) resolves to the exact same canonical UUID."""
        service, meta_uuid = resolver_with_meta
        resolved = service.resolve_provider_symbol_to_instrument_id(
            provider="yfinance",
            provider_symbol="META",
            as_of_date=date(2024, 1, 15),
        )
        assert resolved == meta_uuid

    def test_resolve_symbol_at_invalid_dates_returns_none(self, resolver_with_meta):
        """Querying 'META' before it existed (2020) or 'FB' after rename (2024) returns None."""
        service, _ = resolver_with_meta
        assert service.resolve_provider_symbol_to_instrument_id(
            provider="yfinance",
            provider_symbol="META",
            as_of_date=date(2020, 1, 1),
        ) is None

        assert service.resolve_provider_symbol_to_instrument_id(
            provider="yfinance",
            provider_symbol="FB",
            as_of_date=date(2024, 1, 1),
        ) is None

    def test_exact_boundary_resolution_half_open_interval(self, resolver_with_meta):
        """
        Tests [valid_from, valid_to) boundary semantics:
        - 2022-06-08 (day before rename): 'FB' is valid, 'META' is invalid.
        - 2022-06-09 (effective date): 'FB' is invalid (exclusive), 'META' is valid (inclusive).
        """
        service, meta_uuid = resolver_with_meta

        assert service.resolve_provider_symbol_to_instrument_id("yfinance", "FB", date(2022, 6, 8)) == meta_uuid
        assert service.resolve_provider_symbol_to_instrument_id("yfinance", "META", date(2022, 6, 8)) is None

        assert service.resolve_provider_symbol_to_instrument_id("yfinance", "FB", date(2022, 6, 9)) is None
        assert service.resolve_provider_symbol_to_instrument_id("yfinance", "META", date(2022, 6, 9)) == meta_uuid

    def test_resolve_instrument_id_to_date_specific_symbol(self, resolver_with_meta):
        """Resolves instrument UUID to date-specific ticker symbol across the rename boundary."""
        service, meta_uuid = resolver_with_meta
        sym_past = service.resolve_instrument_id_to_provider_symbol(
            instrument_id=meta_uuid,
            provider="yfinance",
            as_of_date=date(2021, 5, 1),
        )
        assert sym_past == "FB"

        sym_current = service.resolve_instrument_id_to_provider_symbol(
            instrument_id=meta_uuid,
            provider="yfinance",
            as_of_date=date(2023, 5, 1),
        )
        assert sym_current == "META"


class TestDangerousDefaultsElimination:
    """Verifies that missing currency or MIC is not silently defaulted to TRY/XIST."""

    def test_missing_currency_raises_type_error(self):
        """Currency must be explicitly required on InstrumentRecord."""
        with pytest.raises(TypeError):
            InstrumentRecord(
                canonical_name="Test Asset",
                asset_class=AssetClass.EQUITY,
                instrument_type=InstrumentType.US_STOCK,
                # currency intentionally omitted
            )

    def test_mic_defaults_to_none_for_non_exchange_assets(self):
        """MIC is optional and None for TEFAS funds / FX / commodities."""
        inst = InstrumentRecord(
            canonical_name="TEFAS Variable Fund",
            asset_class=AssetClass.FUND,
            instrument_type=InstrumentType.TEFAS_VARIABLE,
            currency=Currency.TRY,
        )
        assert inst.mic is None


class TestProviderAliasIntervalIntegrityAndNormalization:
    """Tests for interval overlap rejection with case/whitespace normalization."""

    def test_case_insensitive_whitespace_trimmed_alias_overlap_rejected(self):
        """
        Aliases registered with different casing or leading/trailing whitespace
        (e.g. 'Yahoo' / 'yahoo', ' META ' / 'meta') must be normalized and rejected on overlap.
        """
        service = InstrumentResolverService()
        inst_a = uuid4()
        inst_b = uuid4()

        service.register_instrument(InstrumentRecord(
            id=inst_a,
            canonical_name="Company A",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
        ))
        service.register_instrument(InstrumentRecord(
            id=inst_b,
            canonical_name="Company B",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
        ))

        # Alias 1: "yfinance", "META" [2010-01-01, 2020-01-01)
        service.register_alias(ProviderAliasRecord(
            instrument_id=inst_a,
            provider="yfinance",
            provider_symbol="META",
            valid_from=date(2010, 1, 1),
            valid_to=date(2020, 1, 1),
        ))

        # Alias 2 (DIFFERENT CASING & WHITESPACE): " Yahoo / YFinance ", " meta " [2019-01-01, 2024-01-01)
        with pytest.raises(ValueError, match="Overlapping alias detected"):
            service.register_alias(ProviderAliasRecord(
                instrument_id=inst_b,
                provider=" YFINANCE ",
                provider_symbol=" meta ",
                valid_from=date(2019, 1, 1),
                valid_to=date(2024, 1, 1),
            ))

    def test_case_insensitive_query_resolution(self):
        """Querying with mixed case resolves correctly via normalized key."""
        service = InstrumentResolverService()
        inst_id = uuid4()

        service.register_instrument(InstrumentRecord(
            id=inst_id,
            canonical_name="Apple Inc.",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
        ))
        service.register_alias(ProviderAliasRecord(
            instrument_id=inst_id,
            provider="YFinance",
            provider_symbol="AAPL",
            valid_from=date(2000, 1, 1),
            valid_to=None,
        ))

        # Query with lowercase provider and symbol
        assert service.resolve_provider_symbol_to_instrument_id("yfinance", "aapl") == inst_id
        # Query with uppercase provider and lowercase symbol with spaces
        assert service.resolve_provider_symbol_to_instrument_id(" YFINANCE ", " aapl ") == inst_id

    def test_historical_ticker_reuse_non_overlapping_succeeds(self):
        """
        Historical ticker reuse: Ticker XYZ was used by Company A from 2000 to 2010,
        then reassigned to Company B from 2015 to 2025. Both resolve correctly without conflict.
        """
        service = InstrumentResolverService()
        comp_a_id = uuid4()
        comp_b_id = uuid4()

        service.register_instrument(InstrumentRecord(
            id=comp_a_id,
            canonical_name="Old Telecom Corp",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
        ))
        service.register_instrument(InstrumentRecord(
            id=comp_b_id,
            canonical_name="New Biotech Inc",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
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

        assert service.resolve_provider_symbol_to_instrument_id("yfinance", "XYZ", date(2005, 6, 1)) == comp_a_id
        assert service.resolve_provider_symbol_to_instrument_id("yfinance", "XYZ", date(2020, 6, 1)) == comp_b_id
        assert service.resolve_provider_symbol_to_instrument_id("yfinance", "XYZ", date(2012, 6, 1)) is None


class TestCorporateActionStrictExclusivity:
    """Tests for corporate action strict field exclusivity."""

    def test_split_action_strictly_forbids_cash_and_symbols(self):
        inst_id = uuid4()
        
        # Valid split
        split = CorporateActionRecord(
            instrument_id=inst_id,
            action_type=CorporateActionType.SPLIT,
            effective_date=date(2024, 6, 10),
            split_factor=10.0,
        )
        assert split.split_factor == 10.0

        # Invalid: split with cash_amount
        with pytest.raises(ValueError, match="must not have cash_amount"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2024, 6, 10),
                split_factor=2.0,
                cash_amount=5.0,
            )

        # Invalid: split with old_symbol
        with pytest.raises(ValueError, match="must not contain old_symbol"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2024, 6, 10),
                split_factor=2.0,
                old_symbol="OLD",
            )

    def test_dividend_action_strictly_requires_currency_and_forbids_splits(self):
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

        # Invalid: missing currency
        with pytest.raises(ValueError, match="requires currency"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.DIVIDEND,
                effective_date=date(2024, 5, 15),
                cash_amount=3.25,
            )

        # Invalid: dividend with split_factor
        with pytest.raises(ValueError, match="must not have split_factor"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.DIVIDEND,
                effective_date=date(2024, 5, 15),
                cash_amount=3.25,
                currency=Currency.TRY,
                split_factor=2.0,
            )

    def test_merger_and_delisting_strictly_forbid_split_and_cash(self):
        inst_id = uuid4()

        # Invalid: merger with split_factor
        with pytest.raises(ValueError, match="must not contain split_factor or cash_amount"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.MERGER,
                effective_date=date(2024, 5, 15),
                split_factor=1.5,
            )

        # Invalid: delisting with cash_amount
        with pytest.raises(ValueError, match="must not contain split_factor or cash_amount"):
            CorporateActionRecord(
                instrument_id=inst_id,
                action_type=CorporateActionType.DELISTING,
                effective_date=date(2024, 5, 15),
                cash_amount=100.0,
            )


class TestCumulativeSplitFactor:
    """Tests cumulative split adjustments with single canonical UUID."""

    def test_cumulative_split_factor_calculation(self):
        service = InstrumentResolverService()
        nvda_id = uuid4()

        inst = InstrumentRecord(
            id=nvda_id,
            canonical_name="NVIDIA Corporation",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
            valid_from=date(1999, 1, 22),
        )
        service.register_instrument(inst)

        service.register_corporate_action(
            CorporateActionRecord(
                instrument_id=nvda_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2021, 7, 20),
                split_factor=4.0,
            )
        )

        service.register_corporate_action(
            CorporateActionRecord(
                instrument_id=nvda_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2024, 6, 10),
                split_factor=10.0,
            )
        )

        total_split = service.get_cumulative_split_factor(
            nvda_id,
            from_date=date(2020, 1, 1),
            to_date=date(2025, 1, 1),
        )
        assert total_split == 40.0


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

        # instruments columns (single canonical id, no internal_instrument_id)
        assert "internal_instrument_id" not in sql
        for col in ["canonical_name", "asset_class", "instrument_type", "isin", "cik", "currency", "status", "valid_from", "valid_to"]:
            assert col in sql, f"Column '{col}' missing in instruments table"

        # provider_aliases columns, generated normalized columns & exclusion constraint
        for col in ["instrument_id", "provider", "provider_symbol", "normalized_provider", "normalized_symbol", "valid_from", "valid_to", "is_primary"]:
            assert col in sql, f"Column '{col}' missing in provider_aliases table"
        assert "provider_aliases_no_overlap" in sql
        assert "btree_gist" in sql

        # Foreign key on normalized_observations
        assert "fk_normalized_observations_instrument" in sql

        # corporate_actions action-specific columns & strict exclusivity check constraint
        for col in ["instrument_id", "action_type", "effective_date", "old_symbol", "new_symbol", "split_factor", "cash_amount"]:
            assert col in sql, f"Column '{col}' missing in corporate_actions table"
        assert "chk_ca_fields_exclusivity" in sql

        # Resolver RPCs
        assert "resolve_provider_symbol_to_instrument" in sql
        assert "resolve_instrument_to_provider_symbol" in sql
