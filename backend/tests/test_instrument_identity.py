"""
backend/tests/test_instrument_identity.py
==========================================
Unit tests for the Instrument Identity, Symbology & Corporate Action Resolution layer.

Verifies:
    - Ticker is NOT the primary key (internal_instrument_id is the canonical identity)
    - Point-in-time provider symbol -> internal ID resolution
    - Point-in-time internal ID -> provider symbol resolution
    - Ticker rename (e.g. FB -> META) preserves historical time-series continuity
    - Fund code changes and split factor calculations
    - Supabase migration 005 SQL schema validity
"""

import os
from datetime import date
from uuid import uuid4

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
    def resolver_with_meta(self) -> InstrumentResolverService:
        """
        Sets up an instrument for Meta Platforms Inc.
        Historical context:
            - Ticker was 'FB' until 2022-06-08
            - Ticker changed to 'META' on 2022-06-09
        """
        service = InstrumentResolverService()
        meta_id = uuid4()

        # 1. Master Instrument
        meta_inst = InstrumentRecord(
            id=meta_id,
            internal_instrument_id="SEC_EQ_US_META",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
            mic="XNAS",
            cik="0001326801",
            isin="US30303M1027",
            status=InstrumentStatus.ACTIVE,
            name="Meta Platforms Inc.",
            valid_from=date(2012, 5, 18),
        )
        service.register_instrument(meta_inst)

        # 2. Historical Alias: 'FB' (2012-05-18 to 2022-06-08)
        alias_fb = ProviderAliasRecord(
            instrument_id=meta_id,
            provider="yfinance",
            provider_symbol="FB",
            valid_from=date(2012, 5, 18),
            valid_to=date(2022, 6, 8),
            is_primary=True,
        )
        service.register_alias(alias_fb)

        # 3. Modern Alias: 'META' (2022-06-09 onwards)
        alias_meta = ProviderAliasRecord(
            instrument_id=meta_id,
            provider="yfinance",
            provider_symbol="META",
            valid_from=date(2022, 6, 9),
            valid_to=None,
            is_primary=True,
        )
        service.register_alias(alias_meta)

        # 4. Corporate Action: Symbol Change
        ca_rename = CorporateActionRecord(
            instrument_id=meta_id,
            action_type=CorporateActionType.SYMBOL_CHANGE,
            effective_date=date(2022, 6, 9),
            old_value="FB",
            new_value="META",
            metadata={"notes": "Company rebranding from Facebook Inc. to Meta Platforms Inc."},
        )
        service.register_corporate_action(ca_rename)

        return service

    def test_ticker_not_primary_key_uses_internal_id(self, resolver_with_meta):
        """Validates that internal_instrument_id is the primary lookup key, not any single ticker."""
        inst = resolver_with_meta.get_instrument_by_internal_id("SEC_EQ_US_META")
        assert inst is not None
        assert inst.internal_instrument_id == "SEC_EQ_US_META"
        assert inst.cik == "0001326801"
        assert inst.asset_class == AssetClass.EQUITY

    def test_resolve_historical_provider_symbol_to_internal_id(self, resolver_with_meta):
        """Querying 'FB' on a historical date (2020) resolves to canonical internal ID."""
        resolved = resolver_with_meta.resolve_provider_symbol_to_internal_id(
            provider="yfinance",
            provider_symbol="FB",
            as_of_date=date(2020, 1, 15),
        )
        assert resolved == "SEC_EQ_US_META"

    def test_resolve_modern_provider_symbol_to_internal_id(self, resolver_with_meta):
        """Querying 'META' on a current date (2024) resolves to the exact same canonical internal ID."""
        resolved = resolver_with_meta.resolve_provider_symbol_to_internal_id(
            provider="yfinance",
            provider_symbol="META",
            as_of_date=date(2024, 1, 15),
        )
        assert resolved == "SEC_EQ_US_META"

    def test_resolve_symbol_at_invalid_dates_returns_none(self, resolver_with_meta):
        """Querying 'META' before it existed (2020) or 'FB' after rename (2024) returns None."""
        # 'META' did not exist in 2020
        assert resolver_with_meta.resolve_provider_symbol_to_internal_id(
            provider="yfinance",
            provider_symbol="META",
            as_of_date=date(2020, 1, 1),
        ) is None

        # 'FB' was no longer valid in 2024
        assert resolver_with_meta.resolve_provider_symbol_to_internal_id(
            provider="yfinance",
            provider_symbol="FB",
            as_of_date=date(2024, 1, 1),
        ) is None

    def test_resolve_internal_id_to_date_specific_symbol(self, resolver_with_meta):
        """Resolves internal ID to date-specific ticker symbol across the rename boundary."""
        sym_past = resolver_with_meta.resolve_internal_id_to_provider_symbol(
            internal_instrument_id="SEC_EQ_US_META",
            provider="yfinance",
            as_of_date=date(2021, 5, 1),
        )
        assert sym_past == "FB"

        sym_current = resolver_with_meta.resolve_internal_id_to_provider_symbol(
            internal_instrument_id="SEC_EQ_US_META",
            provider="yfinance",
            as_of_date=date(2023, 5, 1),
        )
        assert sym_current == "META"

    def test_ticker_change_preserves_historical_series_continuity(self, resolver_with_meta):
        """
        Critical Test: Verifies that time-series queries for historical data points
        (e.g., 2019 data, 2021 data, 2023 data) all map to the identical internal instrument ID.
        """
        historical_points = [
            (date(2015, 6, 1), "FB"),
            (date(2018, 12, 1), "FB"),
            (date(2022, 6, 8), "FB"),      # Last day of FB
            (date(2022, 6, 9), "META"),    # First day of META
            (date(2024, 6, 1), "META"),
        ]

        for dt, expected_symbol in historical_points:
            # 1. Reverse lookup: Symbol -> Internal ID
            internal_id = resolver_with_meta.resolve_provider_symbol_to_internal_id(
                provider="yfinance",
                provider_symbol=expected_symbol,
                as_of_date=dt,
            )
            assert internal_id == "SEC_EQ_US_META", f"Failed reverse lookup at {dt} for {expected_symbol}"

            # 2. Forward lookup: Internal ID -> Symbol
            provider_sym = resolver_with_meta.resolve_internal_id_to_provider_symbol(
                internal_instrument_id="SEC_EQ_US_META",
                provider="yfinance",
                as_of_date=dt,
            )
            assert provider_sym == expected_symbol, f"Failed forward lookup at {dt}: expected {expected_symbol}, got {provider_sym}"


class TestCorporateActionsAndSplits:
    """Tests for corporate actions, split adjustments, and fund code changes."""

    def test_cumulative_split_factor_calculation(self):
        """
        Tests cumulative split adjustments (conceptually referencing LEAN FactorFile).
        Example: NVDA had a 4:1 split on 2021-07-20 and a 10:1 split on 2024-06-10.
        """
        service = InstrumentResolverService()
        nvda_id = uuid4()

        inst = InstrumentRecord(
            id=nvda_id,
            internal_instrument_id="SEC_EQ_US_NVDA",
            asset_class=AssetClass.EQUITY,
            instrument_type=InstrumentType.US_STOCK,
            currency=Currency.USD,
            name="NVIDIA Corporation",
            valid_from=date(1999, 1, 22),
        )
        service.register_instrument(inst)

        # 4:1 Split on 2021-07-20
        service.register_corporate_action(
            CorporateActionRecord(
                instrument_id=nvda_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2021, 7, 20),
                factor=4.0,
            )
        )

        # 10:1 Split on 2024-06-10
        service.register_corporate_action(
            CorporateActionRecord(
                instrument_id=nvda_id,
                action_type=CorporateActionType.SPLIT,
                effective_date=date(2024, 6, 10),
                factor=10.0,
            )
        )

        # 1. Before both splits (2020) to after both splits (2025) -> 4 * 10 = 40x
        total_split = service.get_cumulative_split_factor(
            "SEC_EQ_US_NVDA",
            from_date=date(2020, 1, 1),
            to_date=date(2025, 1, 1),
        )
        assert total_split == 40.0

        # 2. Between first and second split (2022 to 2025) -> 10x
        second_split = service.get_cumulative_split_factor(
            "SEC_EQ_US_NVDA",
            from_date=date(2022, 1, 1),
            to_date=date(2025, 1, 1),
        )
        assert second_split == 10.0

        # 3. Period with no splits (2022 to 2023) -> 1.0x
        no_split = service.get_cumulative_split_factor(
            "SEC_EQ_US_NVDA",
            from_date=date(2022, 1, 1),
            to_date=date(2023, 1, 1),
        )
        assert no_split == 1.0

    def test_tefas_fund_code_change_resolution(self):
        """Tests TEFAS fund code change (e.g. fund merger/code rebranding)."""
        service = InstrumentResolverService()
        fund_id = uuid4()

        fund_inst = InstrumentRecord(
            id=fund_id,
            internal_instrument_id="TEFAS_FND_ABC",
            asset_class=AssetClass.FUND,
            instrument_type=InstrumentType.TEFAS_VARIABLE,
            currency=Currency.TRY,
            name="Ornek Degisken Fon",
            valid_from=date(2020, 1, 1),
        )
        service.register_instrument(fund_inst)

        # Old TEFAS code 'OLD1' (2020-01-01 to 2023-05-31)
        service.register_alias(
            ProviderAliasRecord(
                instrument_id=fund_id,
                provider="tefas",
                provider_symbol="OLD1",
                valid_from=date(2020, 1, 1),
                valid_to=date(2023, 5, 31),
            )
        )

        # New TEFAS code 'NEW1' (2023-06-01 onwards)
        service.register_alias(
            ProviderAliasRecord(
                instrument_id=fund_id,
                provider="tefas",
                provider_symbol="NEW1",
                valid_from=date(2023, 6, 1),
                valid_to=None,
            )
        )

        # Corporate Action
        service.register_corporate_action(
            CorporateActionRecord(
                instrument_id=fund_id,
                action_type=CorporateActionType.FUND_CODE_CHANGE,
                effective_date=date(2023, 6, 1),
                old_value="OLD1",
                new_value="NEW1",
            )
        )

        # Verify resolution
        assert service.resolve_provider_symbol_to_internal_id("tefas", "OLD1", as_of_date=date(2021, 1, 1)) == "TEFAS_FND_ABC"
        assert service.resolve_provider_symbol_to_internal_id("tefas", "NEW1", as_of_date=date(2024, 1, 1)) == "TEFAS_FND_ABC"


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
        for col in ["internal_instrument_id", "asset_class", "instrument_type", "isin", "cik", "mic", "currency", "status", "valid_from", "valid_to"]:
            assert col in sql, f"Column '{col}' missing in instruments table"

        # provider_aliases columns
        for col in ["instrument_id", "provider", "provider_symbol", "valid_from", "valid_to", "is_primary"]:
            assert col in sql, f"Column '{col}' missing in provider_aliases table"

        # corporate_actions columns
        for col in ["instrument_id", "action_type", "effective_date", "old_value", "new_value", "factor", "amount"]:
            assert col in sql, f"Column '{col}' missing in corporate_actions table"

        # Action types check
        for act in ["symbol_change", "split", "dividend", "merger", "delisting", "fund_code_change"]:
            assert f"'{act}'" in sql, f"Action type '{act}' not present in CHECK constraint"

        # Resolver RPCs
        assert "resolve_provider_symbol_to_instrument" in sql
        assert "resolve_instrument_to_provider_symbol" in sql
