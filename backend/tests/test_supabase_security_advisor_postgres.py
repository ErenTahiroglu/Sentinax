"""Phase 27 FIX D: real PostgreSQL verification of migration 028 (Supabase Security Advisor closure).

No mocks: the REAL repository migrations 001 through 028 are executed in order against a disposable database. Fixture limits (the database is not a Supabase project):
roles anon / authenticated / service_role, an auth schema stand-in with auth.uid(), a vault schema stand-in (secrets table and decrypted_secrets view; the supabase_vault
extension cannot exist here, so only that CREATE EXTENSION line is removed), Supabase-style default grants, and, only when the server lacks the contrib extensions
btree_gist / uuid-ossp (the CI postgres:16 image has them), their CREATE EXTENSION lines and migration 005's single gist EXCLUDE constraint are removed.

Runs only when SENTINAX_TEST_POSTGRES_URL points at a disposable database. When it is set, a missing driver or an unreachable server FAILS (never skips).
"""
import os
import pathlib
import re
import threading
import uuid

import pytest

POSTGRES_URL = os.environ.get("SENTINAX_TEST_POSTGRES_URL")
if not POSTGRES_URL:
    pytest.skip(
        "real PostgreSQL integration environment not configured (SENTINAX_TEST_POSTGRES_URL absent)",
        allow_module_level=True,
    )

import psycopg  # noqa: E402  (deliberately unguarded: ImportError must FAIL when the URL is set)
from psycopg import errors as pgerr  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = sorted((REPO_ROOT / "supabase" / "migrations").glob("*.sql"))

BOOTSTRAP_SQL = """
DROP SCHEMA IF EXISTS public CASCADE;
DROP SCHEMA IF EXISTS auth CASCADE;
DROP SCHEMA IF EXISTS vault CASCADE;
CREATE SCHEMA public;
CREATE SCHEMA auth;
CREATE SCHEMA vault;

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END $$;
ALTER ROLE service_role BYPASSRLS;

CREATE TABLE auth.users (id uuid PRIMARY KEY);
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
CREATE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS $$ SELECT current_user::text $$;

CREATE TABLE vault.secrets (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text, secret text, description text);
CREATE VIEW vault.decrypted_secrets AS SELECT id, name, description, secret AS decrypted_secret FROM vault.secrets;

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
-- Supabase-style default privileges that the real migrations must tighten.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role;
"""

# SECURITY DEFINER functions that exist after migration 028, with a short rationale. Any new SECURITY DEFINER function must be added here deliberately.
EXPECTED_SECURITY_DEFINER = {
    "consume_rate_limit": "service-only rate limiter; fixed policy, called by the trusted analyze Edge Function",
    "get_user_api_key_for_service": "service-only Vault retrieval for a caller id verified by the trusted Edge Function",
    "upsert_user_api_key": "dormant Vault writer: no API role may execute it",
    "check_user_has_api_key": "dormant legacy helper: no API role may execute it",
    "get_portfolio_transaction_history_coverage_snapshot": "Phase 26 service-only coverage RPC (migration 026)",
    "lock_portfolio_transaction_reversal_target": "Phase 27 FIX A trigger function (no caller EXECUTE)",
    "validate_fee_tax_attribution_event_integrity": "Phase 27 FIX A trigger function (no caller EXECUTE)",
}
# Authenticated-executable SECURITY DEFINER allowlist. Deliberately empty: every client-reachable RPC is SECURITY INVOKER or service-only. An entry needs a rationale and an adversarial test.
AUTHENTICATED_ALLOWLIST: dict = {}

API_ROLES = ("anon", "authenticated")
CAPACITY = 15


def prepared(path: pathlib.Path, has_btree_gist: bool, has_uuid_ossp: bool) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'(?im)^\s*CREATE EXTENSION IF NOT EXISTS "supabase_vault"[^;]*;\s*$', "", text)
    if not has_btree_gist:
        text = re.sub(r"(?im)^\s*CREATE EXTENSION IF NOT EXISTS btree_gist;\s*$", "", text)
        text = re.sub(
            r",\s*--[^\n]*\n\s*CONSTRAINT provider_aliases_no_overlap EXCLUDE USING gist \(.*?\n    \)\n", "\n", text, flags=re.S
        )
    if not has_uuid_ossp:
        text = re.sub(r'(?im)^\s*CREATE EXTENSION IF NOT EXISTS "uuid-ossp";\s*$', "", text)
    return text


class Env:
    def __init__(self):
        self.conns = []
        self.admin = self.connect(autocommit=True)

    def connect(self, role=None, autocommit=True):
        conn = psycopg.connect(POSTGRES_URL, autocommit=True, row_factory=dict_row)
        if role:
            conn.execute(f"SET ROLE {role}")
        conn.autocommit = autocommit
        self.conns.append(conn)
        return conn

    def scalar(self, sql, params=()):
        row = self.admin.execute(sql, params).fetchone()
        return next(iter(row.values()))

    def close(self):
        for conn in self.conns:
            try:
                conn.close()
            except Exception:
                pass


@pytest.fixture
def pg():
    env = Env()
    try:
        db = env.scalar("SELECT current_database()")
        assert db.startswith("sentinax_") and "concurrency" in db, f"refusing to reset non-test database {db!r}"
        assert env.scalar("SELECT count(*) FROM pg_namespace WHERE nspname IN ('storage', 'realtime', 'graphql')") == 0, "looks like a real Supabase project"
        env.admin.execute(BOOTSTRAP_SQL)
        available = {r["name"] for r in env.admin.execute("SELECT name FROM pg_available_extensions").fetchall()}
        for path in MIGRATIONS:
            env.admin.execute(prepared(path, "btree_gist" in available, "uuid-ossp" in available))
        yield env
    finally:
        env.close()


def has_func(pg, role, signature):
    return pg.scalar("SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')", (role, signature))


def has_table(pg, role, priv, table="public.rate_limits"):
    return pg.scalar("SELECT has_table_privilege(%s, %s, %s)", (role, table, priv))


def test_server_version_and_all_migrations_through_028_ran(pg):
    major = int(pg.scalar("SHOW server_version_num")) // 10000
    print(f"EVIDENCE postgres_server_version={pg.scalar('SHOW server_version')} migrations={len(MIGRATIONS)} last={MIGRATIONS[-1].name}")
    assert major >= 16 and MIGRATIONS[-1].name.startswith("028_")


# --- rate_limits: RLS on, no client table access ----------------------------------------------------------------------------------------------
def test_rate_limits_has_rls_and_no_table_privileges_for_any_role(pg):
    assert pg.scalar("SELECT relrowsecurity FROM pg_class WHERE oid = 'public.rate_limits'::regclass") is True
    assert pg.scalar("SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = 'rate_limits'") == 0   # no permissive policy added to silence the Advisor
    for role in ("public", "anon", "authenticated", "service_role"):
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not has_table(pg, role, priv), (role, priv)


@pytest.mark.parametrize("role", API_ROLES)
def test_direct_table_access_is_denied_for_api_roles(pg, role):
    conn = pg.connect(role)
    for sql in (
        "SELECT * FROM public.rate_limits",
        "INSERT INTO public.rate_limits (identifier) VALUES ('x')",
        "UPDATE public.rate_limits SET tokens = 1e9",
        "DELETE FROM public.rate_limits",
        "TRUNCATE public.rate_limits",
    ):
        with pytest.raises(pgerr.InsufficientPrivilege):
            conn.execute(sql)


# --- consume_rate_limit: threat model ------------------------------------------------------------------------------------------------------------
def test_only_the_fixed_policy_signature_exists_and_callers_cannot_choose_capacity_or_refill(pg):
    rows = pg.admin.execute(
        "SELECT pg_get_function_identity_arguments(p.oid) AS args FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'public' AND p.proname = 'consume_rate_limit'"
    ).fetchall()
    assert [r["args"] for r in rows] == ["p_identifier text"]
    svc = pg.connect("service_role")
    for sql in (
        "SELECT public.consume_rate_limit('a', 1000000, 1000000.0)",
        "SELECT public.consume_rate_limit(p_identifier => 'a', p_capacity => 1000000)",
        "SELECT public.consume_rate_limit(p_identifier => 'a', p_refill_rate => 1000000.0)",
    ):
        with pytest.raises(pgerr.UndefinedFunction):
            svc.execute(sql)


@pytest.mark.parametrize("role", API_ROLES)
def test_anon_and_authenticated_cannot_call_the_rate_limit_rpc(pg, role):
    assert not has_func(pg, role, "public.consume_rate_limit(text)")
    with pytest.raises(pgerr.InsufficientPrivilege):
        pg.connect(role).execute("SELECT public.consume_rate_limit('victim')")


def test_public_and_api_grants_are_absent_and_service_role_may_execute(pg):
    assert not has_func(pg, "public", "public.consume_rate_limit(text)")
    assert has_func(pg, "service_role", "public.consume_rate_limit(text)")
    acl = pg.scalar("SELECT proacl::text FROM pg_proc WHERE oid = 'public.consume_rate_limit(text)'::regprocedure")
    assert "service_role=X" in acl and "anon" not in acl and "authenticated" not in acl and not acl.startswith("{=")


def test_service_role_gets_exactly_the_15_token_burst_then_denial(pg):
    svc = pg.connect("service_role")
    results = [svc.execute("SELECT public.consume_rate_limit('burst-a')").fetchone()["consume_rate_limit"] for _ in range(CAPACITY + 2)]
    assert results == [True] * CAPACITY + [False, False]


def test_buckets_are_isolated_per_identifier(pg):
    svc = pg.connect("service_role")
    for _ in range(CAPACITY):
        assert svc.execute("SELECT public.consume_rate_limit('victim-a')").fetchone()["consume_rate_limit"] is True
    assert svc.execute("SELECT public.consume_rate_limit('victim-a')").fetchone()["consume_rate_limit"] is False
    assert svc.execute("SELECT public.consume_rate_limit('victim-b')").fetchone()["consume_rate_limit"] is True       # another bucket is untouched
    assert pg.scalar("SELECT tokens FROM public.rate_limits WHERE identifier = 'victim-b'") > CAPACITY - 1 - 1e-6


def test_refill_is_deterministic_at_a_quarter_token_per_second_and_capped_at_capacity(pg):
    svc = pg.connect("service_role")
    svc.execute("SELECT public.consume_rate_limit('refill')")                                                          # creates the bucket
    pg.admin.execute("UPDATE public.rate_limits SET tokens = 0, last_updated = now() - interval '2 seconds' WHERE identifier = 'refill'")
    assert svc.execute("SELECT public.consume_rate_limit('refill')").fetchone()["consume_rate_limit"] is False        # about 0.5 token
    pg.admin.execute("UPDATE public.rate_limits SET tokens = 0, last_updated = now() - interval '5 seconds' WHERE identifier = 'refill'")
    assert svc.execute("SELECT public.consume_rate_limit('refill')").fetchone()["consume_rate_limit"] is True         # about 1.25 tokens
    pg.admin.execute("UPDATE public.rate_limits SET tokens = 0, last_updated = now() - interval '1 hour' WHERE identifier = 'refill'")
    assert svc.execute("SELECT public.consume_rate_limit('refill')").fetchone()["consume_rate_limit"] is True
    assert pg.scalar("SELECT tokens FROM public.rate_limits WHERE identifier = 'refill'") == pytest.approx(CAPACITY - 1, abs=1e-6)   # capped at 15, then one consumed


@pytest.mark.parametrize("identifier", [None, "", "x" * 257])
def test_invalid_identifiers_are_rejected_without_creating_a_bucket(pg, identifier):
    svc = pg.connect("service_role")
    with pytest.raises(pgerr.InvalidParameterValue):
        svc.execute("SELECT public.consume_rate_limit(%s)", (identifier,))
    assert pg.scalar("SELECT count(*) FROM public.rate_limits") == 0


def test_concurrent_first_calls_and_consumption_are_atomic(pg):
    attempts, outcomes, errors = 40, [], []
    barrier = threading.Barrier(attempts)

    def worker():
        conn = None
        try:
            conn = psycopg.connect(POSTGRES_URL, autocommit=True, row_factory=dict_row)
            conn.execute("SET ROLE service_role")
            barrier.wait(timeout=30)
            outcomes.append(conn.execute("SELECT public.consume_rate_limit('contended')").fetchone()["consume_rate_limit"])
        except Exception as error:  # a primary-key collision on the first insert would land here
            errors.append(repr(error))
        finally:
            if conn is not None:
                conn.close()

    threads = [threading.Thread(target=worker) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert len(outcomes) == attempts
    assert outcomes.count(True) == CAPACITY and outcomes.count(False) == attempts - CAPACITY
    assert pg.scalar("SELECT count(*) FROM public.rate_limits WHERE identifier = 'contended'") == 1


# --- Vault: service-only retrieval -----------------------------------------------------------------------------------------------------------------------
def seed_user_with_key(pg, secret):
    user = uuid.uuid4()
    secret_id = uuid.uuid4()
    pg.admin.execute("INSERT INTO auth.users (id) VALUES (%s)", (user,))
    pg.admin.execute("INSERT INTO vault.secrets (id, name, secret) VALUES (%s, %s, %s)", (secret_id, f"k-{user}", secret))
    pg.admin.execute("INSERT INTO public.user_profiles (user_id, vault_secret_id) VALUES (%s, %s)", (user, secret_id))
    return user


def test_the_legacy_caller_scoped_decryption_rpc_no_longer_exists(pg):
    assert pg.scalar("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'get_user_api_key'") == 0


@pytest.mark.parametrize("role", API_ROLES)
def test_api_roles_cannot_obtain_decrypted_vault_material(pg, role):
    victim = seed_user_with_key(pg, "SECRET-VICTIM")
    assert not has_func(pg, role, "public.get_user_api_key_for_service(uuid)")
    with pytest.raises(pgerr.InsufficientPrivilege):
        pg.connect(role).execute("SELECT public.get_user_api_key_for_service(%s)", (victim,))
    with pytest.raises(pgerr.InsufficientPrivilege):
        pg.connect(role).execute("SELECT * FROM vault.decrypted_secrets")


def test_service_role_retrieves_only_the_key_of_the_supplied_verified_user(pg):
    alice, bob = seed_user_with_key(pg, "SECRET-ALICE"), seed_user_with_key(pg, "SECRET-BOB")
    svc = pg.connect("service_role")
    get = lambda uid: svc.execute("SELECT public.get_user_api_key_for_service(%s)", (uid,)).fetchone()["get_user_api_key_for_service"]
    assert get(alice) == "SECRET-ALICE" and get(bob) == "SECRET-BOB"
    assert get(uuid.uuid4()) is None and get(None) is None
    assert not has_func(pg, "public", "public.get_user_api_key_for_service(uuid)")


@pytest.mark.parametrize(
    "signature,call,params",
    [
        ("public.upsert_user_api_key(text)", "SELECT public.upsert_user_api_key(%s)", ("k",)),
        ("public.check_user_has_api_key()", "SELECT public.check_user_has_api_key()", ()),
    ],
)
def test_dormant_privileged_helpers_are_not_executable_by_any_api_role(pg, signature, call, params):
    for role in ("public", "anon", "authenticated", "service_role"):
        assert not has_func(pg, role, signature), (role, signature)
    for role in API_ROLES + ("service_role",):
        with pytest.raises(pgerr.InsufficientPrivilege):
            pg.connect(role).execute(call, params)


# --- get_pit_macro_observation ------------------------------------------------------------------------------------------------------------------------------
def test_pit_macro_observation_is_invoker_with_pinned_path_and_not_anon_executable(pg):
    sig = "public.get_pit_macro_observation(character varying, date, timestamp with time zone, character varying)"
    row = pg.admin.execute("SELECT prosecdef, proconfig FROM pg_proc WHERE oid = %s::regprocedure", (sig,)).fetchone()
    assert row["prosecdef"] is False and row["proconfig"] == ["search_path=public, pg_temp"]
    assert not has_func(pg, "public", sig) and not has_func(pg, "anon", sig)
    assert has_func(pg, "authenticated", sig) and has_func(pg, "service_role", sig)
    for role in ("authenticated", "service_role"):
        conn = pg.connect(role)
        rows = conn.execute(
            "SELECT * FROM public.get_pit_macro_observation('NONEXISTENT', DATE '2026-01-01', TIMESTAMPTZ '2026-06-01 00:00+00', 'SYSTEM_AS_OF')"
        ).fetchall()
        assert rows == []
        with pytest.raises(pgerr.RaiseException):
            conn.execute("SELECT * FROM public.get_pit_macro_observation('X', DATE '2026-01-01', TIMESTAMPTZ '2026-06-01 00:00+00', 'BAD')")
    with pytest.raises(pgerr.InsufficientPrivilege):
        pg.connect("anon").execute("SELECT * FROM public.get_pit_macro_observation('X', DATE '2026-01-01', TIMESTAMPTZ '2026-06-01 00:00+00', 'SYSTEM_AS_OF')")


def test_the_phase_17_state_input_rpc_still_works_for_authenticated_and_service_role(pg):
    for role in ("authenticated", "service_role"):
        conn = pg.connect(role)
        conn.execute("SELECT * FROM public.get_pit_macro_state_input('NONEXISTENT', DATE '2026-01-01', TIMESTAMPTZ '2026-06-01 00:00+00', 'SYSTEM_AS_OF')").fetchall()
    with pytest.raises(pgerr.InsufficientPrivilege):
        pg.connect("anon").execute("SELECT * FROM public.get_pit_macro_state_input('X', DATE '2026-01-01', TIMESTAMPTZ '2026-06-01 00:00+00', 'SYSTEM_AS_OF')")


# --- inventories --------------------------------------------------------------------------------------------------------------------------------------------------------
def user_functions(pg):
    return pg.admin.execute(
        "SELECT p.oid, p.proname, p.prosecdef, p.proconfig, p.oid::regprocedure::text AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'public' AND p.prokind = 'f'"
        " AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')"
        " ORDER BY p.proname"
    ).fetchall()


def test_security_definer_inventory_is_exactly_the_reviewed_set_and_never_public_or_anon_executable(pg):
    definers = {f["proname"]: f for f in user_functions(pg) if f["prosecdef"]}
    assert set(definers) == set(EXPECTED_SECURITY_DEFINER), set(definers) ^ set(EXPECTED_SECURITY_DEFINER)
    for name, f in definers.items():
        assert EXPECTED_SECURITY_DEFINER[name]
        assert f["proconfig"] == ["search_path=pg_catalog, pg_temp"], (name, f["proconfig"])
        assert not has_func(pg, "public", f["sig"]), name
        assert not has_func(pg, "anon", f["sig"]), name
        if has_func(pg, "authenticated", f["sig"]):
            assert AUTHENTICATED_ALLOWLIST.get(name), f"{name} is authenticated-executable without an allowlist rationale"


def test_every_public_function_has_an_explicit_search_path(pg):
    missing = [f["sig"] for f in user_functions(pg) if not any(c.startswith("search_path=") for c in (f["proconfig"] or []))]
    assert not missing, missing


def test_every_public_table_has_rls_enabled_and_scheduler_tables_are_intentionally_policy_free(pg):
    rows = pg.admin.execute(
        "SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
    ).fetchall()
    assert rows and [r["relname"] for r in rows if not r["relrowsecurity"]] == []
    for table in ("private_scheduler_runs", "private_scheduler_run_transitions"):
        assert pg.scalar("SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = %s", (table,)) == 0   # ACCEPTED_INTENTIONAL_INFO
        for role in ("public", "anon", "authenticated"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not has_table(pg, role, priv, f"public.{table}"), (table, role, priv)


def test_new_functions_are_no_longer_executable_by_public_anon_or_authenticated_by_default(pg):
    pg.admin.execute("CREATE FUNCTION public.zz_probe() RETURNS int LANGUAGE sql AS 'SELECT 1'")
    for role in ("public", "anon", "authenticated"):
        assert not has_func(pg, role, "public.zz_probe()"), role


def test_the_phase_27_fix_a_ledger_trigger_functions_are_unchanged(pg):
    for name in ("lock_portfolio_transaction_reversal_target", "validate_fee_tax_attribution_event_integrity"):
        row = pg.admin.execute("SELECT prosecdef, proconfig FROM pg_proc WHERE proname = %s", (name,)).fetchone()
        assert row["prosecdef"] is True and row["proconfig"] == ["search_path=pg_catalog, pg_temp"]
    assert not has_table(pg, "service_role", "UPDATE", "public.portfolio_transactions")
