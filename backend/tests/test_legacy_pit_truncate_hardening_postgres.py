"""Post-28B-1A hardening H1: real PostgreSQL verification of migration 031 (TRUNCATE closure on the legacy PIT store).

TRUNCATE bypasses row-level security and does not fire row DELETE triggers, so the legacy tables' row-level protections never covered it. Fresh replays of migrations 001-030
under Supabase-style default grants left TRUNCATE with anon, authenticated and service_role on raw_provider_snapshots and normalized_observations. This is DIRECT-SQL
reachability only: the Supabase Data API (PostgREST) exposes no TRUNCATE verb, so no public REST exploit is claimed.

Runs only when SENTINAX_TEST_POSTGRES_URL points at a disposable database. All destructive attempts run inside a transaction that is rolled back.
"""
import pytest

from backend.tests.test_supabase_security_advisor_postgres import BOOTSTRAP_SQL, MIGRATIONS, Env, prepared  # skips when the URL is absent

import psycopg  # noqa: E402
from psycopg import errors as pgerr  # noqa: E402

ROLES = ("anon", "authenticated", "service_role")
LEGACY = ("raw_provider_snapshots", "normalized_observations")
M031 = next((m for m in MIGRATIONS if m.name.startswith("031_")), None)
HASH = "0" * 64


def replay(through: str):
    env = Env()
    db = env.scalar("SELECT current_database()")
    assert db.startswith("sentinax_") and "concurrency" in db, f"refusing to reset non-test database {db!r}"
    assert env.scalar("SELECT count(*) FROM pg_namespace WHERE nspname IN ('storage', 'realtime', 'graphql')") == 0, "looks like a real Supabase project"
    env.admin.execute(BOOTSTRAP_SQL)
    available = {r["name"] for r in env.admin.execute("SELECT name FROM pg_available_extensions").fetchall()}
    for path in MIGRATIONS:
        if path.name[:3] <= through:
            env.admin.execute(prepared(path, "btree_gist" in available, "uuid-ossp" in available))
    return env


@pytest.fixture
def pg():
    env = replay("999")
    try:
        yield env
    finally:
        env.close()


@pytest.fixture
def pg030():
    env = replay("030")
    try:
        yield env
    finally:
        env.close()


def has_priv(env, role, table, priv="TRUNCATE"):
    return env.scalar("SELECT has_table_privilege(%s, %s, %s)", (role, f"public.{table}", priv))


def seed_raw(env):
    return env.scalar(
        "INSERT INTO public.raw_provider_snapshots (provider, endpoint, request_params, raw_payload, payload_hash) VALUES ('TEFAS', '/e', '{}'::jsonb, '{\"k\": 1}'::jsonb, %s) RETURNING id",
        (HASH,),
    )


def seed_normalized(env, raw_id):
    """A normalized observation needs a canonical instrument; use the identity table's own constraints."""
    instrument = env.scalar(
        "INSERT INTO public.instruments (canonical_name, asset_class, instrument_type, currency) VALUES ('H1 test fund', 'fund', 'tefas_fund', 'TRY') RETURNING id"
    )
    env.admin.execute(
        "INSERT INTO public.normalized_observations (snapshot_id, instrument_id, asset_class, instrument_type, observation_type, data_status, confidence_level, source_tier, currency, effective_date, observed_at)"
        " VALUES (%s, %s, 'fund', 'tefas_fund', 'PRICE', 'complete', 'high', 'tier_2', 'TRY', DATE '2026-01-02', TIMESTAMPTZ '2026-01-03 00:00+00')", (raw_id, instrument))
    return instrument


def attempt_truncate(env, role, table, mode):
    """Return (outcome, sqlstate, rows_inside_txn_or_None). Always rolled back."""
    conn = env.connect(role, autocommit=False)
    try:
        conn.execute(f"TRUNCATE public.{table} {mode}")
        return "ALLOWED", None
    except psycopg.Error as error:
        return "DENIED", error.sqlstate
    finally:
        conn.rollback()
        conn.close()


def counts(env):
    return {t: env.scalar(f"SELECT count(*) FROM public.{t}") for t in LEGACY}


# --- RED/GREEN: effective privilege --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("table", LEGACY)
def test_no_api_role_or_public_holds_truncate_on_the_legacy_pit_tables(pg, table):
    assert M031 is not None, "migration 031 is missing"
    for role in ("public",) + ROLES:
        assert has_priv(pg, role, table) is False, (role, table)


@pytest.mark.parametrize("mode", ("RESTRICT", "CASCADE"))
@pytest.mark.parametrize("table", LEGACY)
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("bindings", (0, 1))
def test_truncate_is_denied_for_every_api_role_mode_table_with_zero_and_nonzero_bindings(pg, role, table, mode, bindings):
    from backend.tests.test_learning_persistence_postgres import declaration, repo, seed_snapshot            # exercise the real binding path
    rec = seed_snapshot(pg, payload={"h1": 1})
    seed_normalized(pg, rec.id)
    if bindings:
        repo(pg).persist((declaration(rec),))
    before = counts(pg)
    outcome, sqlstate = attempt_truncate(pg, role, table, mode)
    assert (outcome, sqlstate) == ("DENIED", "42501"), (role, table, mode, bindings, outcome, sqlstate)    # insufficient_privilege, not a dependency error
    assert counts(pg) == before and before["raw_provider_snapshots"] == 1 and before["normalized_observations"] == 1


# --- legitimate behaviour is preserved ---------------------------------------------------------------------------------------------------------
def test_service_role_still_reads_inserts_and_supersedes(pg):
    svc = pg.connect("service_role")
    first = svc.execute(
        "INSERT INTO public.raw_provider_snapshots (provider, endpoint, request_params, raw_payload, payload_hash) VALUES ('TEFAS', '/e', '{}'::jsonb, '{\"v\": 1}'::jsonb, %s) RETURNING id", (HASH,)
    ).fetchone()["id"]
    second = svc.execute(
        "INSERT INTO public.raw_provider_snapshots (provider, endpoint, request_params, raw_payload, payload_hash, supersedes_record_id) VALUES ('TEFAS', '/e', '{}'::jsonb, '{\"v\": 2}'::jsonb, %s, %s) RETURNING id",
        ("1" * 64, first),
    ).fetchone()["id"]
    assert svc.execute("SELECT is_superseded FROM public.raw_provider_snapshots WHERE id = %s", (first,)).fetchone()["is_superseded"] is True        # system supersession still works
    assert svc.execute("SELECT count(*) AS c FROM public.raw_provider_snapshots WHERE id = ANY(%s)", ([first, second],)).fetchone()["c"] == 2
    for table in LEGACY:
        for priv in ("SELECT", "INSERT", "UPDATE"):
            assert has_priv(pg, "service_role", table, priv) is True, (table, priv)


def test_authenticated_can_still_read_active_normalized_observations_through_the_existing_policy(pg):
    raw_id = seed_raw(pg)
    seed_normalized(pg, raw_id)
    assert pg.connect("authenticated").execute("SELECT count(*) AS c FROM public.normalized_observations").fetchone()["c"] == 1
    assert pg.connect("anon").execute("SELECT count(*) AS c FROM public.normalized_observations").fetchone()["c"] == 0           # unchanged: no anon policy


def test_row_level_anti_tamper_protections_remain_effective(pg):
    raw_id = seed_raw(pg)
    seed_normalized(pg, raw_id)
    svc = pg.connect("service_role")
    for sql in ("UPDATE public.raw_provider_snapshots SET raw_payload = '{\"x\": 2}'::jsonb", "DELETE FROM public.raw_provider_snapshots",
                "UPDATE public.normalized_observations SET observation_data = '{\"x\": 2}'::jsonb", "DELETE FROM public.normalized_observations"):
        with pytest.raises(pgerr.RaiseException):
            svc.execute(sql)
    assert counts(pg) == {"raw_provider_snapshots": 1, "normalized_observations": 1}


# --- Phase 28B-1A guarantees are unchanged ------------------------------------------------------------------------------------------------------
def test_learning_bindings_remain_append_only_idempotent_and_the_bound_snapshot_stays_guarded(pg):
    from backend.tests.test_learning_persistence_postgres import declaration, repo, seed_snapshot
    rec = seed_snapshot(pg)
    first = repo(pg).persist((declaration(rec),))
    again = repo(pg).persist((declaration(rec),))
    assert [o.disposition.value for o in first] == ["INSERTED"] and [o.disposition.value for o in again] == ["EXISTING"]
    svc = pg.connect("service_role")
    for sql in ("UPDATE public.learning_evidence_bindings SET source_reference = 'x'", "DELETE FROM public.learning_evidence_bindings", "TRUNCATE public.learning_evidence_bindings"):
        with pytest.raises(pgerr.InsufficientPrivilege):
            svc.execute(sql)
    with pytest.raises(pgerr.RestrictViolation):
        svc.execute("UPDATE public.raw_provider_snapshots SET payload_hash = %s WHERE id = %s", ("2" * 64, rec.id))
    assert pg.scalar("SELECT count(*) FROM public.learning_evidence_bindings") == 1


# --- migration 031 is forward-only, minimal and re-runnable ------------------------------------------------------------------------------------
def test_migration_031_can_be_reapplied_without_losing_rows_or_regranting(pg):
    from backend.tests.test_learning_persistence_postgres import declaration, repo, seed_snapshot
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec),))
    seed_normalized(pg, rec.id)
    before = (counts(pg), pg.scalar("SELECT count(*) FROM public.learning_evidence_bindings"))
    sql = prepared(M031, True, True)
    pg.admin.execute(sql)
    pg.admin.execute(sql)
    assert (counts(pg), pg.scalar("SELECT count(*) FROM public.learning_evidence_bindings")) == before
    for table in LEGACY:
        assert not any(has_priv(pg, r, table) for r in ("public",) + ROLES)


def test_migration_031_changes_only_truncate_and_nothing_else_about_the_legacy_tables(pg030):
    """Privilege/ownership/trigger/policy/constraint fingerprint of the legacy tables before and after 031: the only difference is TRUNCATE."""
    def fingerprint(env):
        privs = {(t, r, p): has_priv(env, r, t, p) for t in LEGACY for r in ("public",) + ROLES for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES", "TRIGGER")}
        meta = {
            "owners": env.admin.execute("SELECT tablename, tableowner FROM pg_tables WHERE tablename = ANY(%s) ORDER BY 1", (list(LEGACY),)).fetchall(),
            "triggers": env.admin.execute("SELECT tgrelid::regclass::text t, tgname, tgenabled FROM pg_trigger WHERE tgrelid = ANY(%s::regclass[]) AND NOT tgisinternal ORDER BY 1, 2",
                                          ([f"public.{t}" for t in LEGACY],)).fetchall(),
            "policies": env.admin.execute("SELECT tablename, policyname, cmd, roles::text FROM pg_policies WHERE tablename = ANY(%s) ORDER BY 1, 2", (list(LEGACY),)).fetchall(),
            "constraints": env.admin.execute("SELECT conname, pg_get_constraintdef(oid) d FROM pg_constraint WHERE conrelid = ANY(%s::regclass[]) ORDER BY 1",
                                             ([f"public.{t}" for t in LEGACY],)).fetchall(),
            "rls": env.admin.execute("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = ANY(%s::regclass[]) ORDER BY 1", ([f"public.{t}" for t in LEGACY],)).fetchall(),
        }
        return privs, meta
    before = fingerprint(pg030)
    assert M031 is not None, "migration 031 is missing"
    pg030.admin.execute(prepared(M031, True, True))
    after = fingerprint(pg030)
    assert after == before                                                         # no SELECT/INSERT/UPDATE/DELETE/REFERENCES/TRIGGER change, no ownership/trigger/policy/FK/RLS change


# --- historical evidence is preserved (not asserted to still exist after hardening) --------------------------------------------------------
def test_historical_evidence_through_migration_030_the_privilege_existed_and_cascade_was_indirectly_blocked(pg030):
    for table in LEGACY:
        for role in ROLES:
            assert has_priv(pg030, role, table) is True, (role, table)             # the legacy privilege that H1 removes
    seed_raw(pg030)
    for role in ROLES:
        assert attempt_truncate(pg030, role, "raw_provider_snapshots", "CASCADE") == ("DENIED", "42501")     # blocked only via the dependent binding table, not the raw table's own privilege
        assert attempt_truncate(pg030, role, "normalized_observations", "RESTRICT") == ("ALLOWED", None)      # the derived store was unprotected
