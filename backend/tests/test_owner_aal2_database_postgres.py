"""Phase 25D1E0: offline owner/AAL2 database security contract for the Migration 032 CANDIDATE (real PostgreSQL 16, disposable database).

The candidate lives in supabase/candidates/ (NOT supabase/migrations/) so no Supabase integration can apply it by accident; these tests replay the REAL migrations
001-031 and then apply the candidate. Nothing here touches a connected Supabase project.

Fixture limits (stated, not hidden):
  * auth.uid() / auth.jwt() are stand-ins that read the PostgREST GUCs (request.jwt.claims, legacy request.jwt.claim.sub) like Supabase's. A direct SQL session could set
    those GUCs itself; the guarantee is for the PostgREST path (the GUCs are set by PostgREST from the signed JWT). Whether the live PostgREST exposes an `amr` array in
    request.jwt.claims is the documented Supabase behaviour (the signed token carries it) but is UNVERIFIED against the live project.
  * service_role has BYPASSRLS (as on Supabase). RLS therefore protects nothing from service_role; that boundary is the FastAPI owner/AAL2 layer and is exercised separately.
  * This is not a Supabase project: the live grants, live policies, extra tables and the live owner identity are UNKNOWN until the operator runs the read-only inventory
    (supabase/candidates/032_operator_live_inventory.sql).

Runs only when SENTINAX_TEST_POSTGRES_URL points at a disposable database (sentinax_*concurrency*).
"""
import contextlib
import json
import pathlib
import re
import uuid

import pytest

from backend.tests.test_supabase_security_advisor_postgres import BOOTSTRAP_SQL, MIGRATIONS, REPO_ROOT, Env, prepared  # skips when the URL is absent

import psycopg  # noqa: E402
from psycopg import errors as pgerr  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from backend.tests.test_supabase_security_advisor_postgres import POSTGRES_URL  # noqa: E402

CAND_DIR = REPO_ROOT / "supabase" / "candidates"
CANDIDATE = CAND_DIR / "032_owner_aal2_restrictive_rls.sql"
ROLLBACK = CAND_DIR / "032_owner_aal2_rollback.sql"
INVENTORY = CAND_DIR / "032_operator_live_inventory.sql"
ROLLBACK_CONFIRM = "I_HAVE_A_VERIFIED_PRE_032_GRANT_SNAPSHOT"

ROLES = ("anon", "authenticated", "service_role")
# Owner-private tables that must carry the restrictive owner+AAL2 policy.
OWNER_TABLES = (
    "portfolios", "portfolio_accounts", "cash_buckets", "investment_goals", "planned_contributions",
    "portfolio_transactions", "portfolio_fee_tax_attribution_events", "portfolio_import_claim_bindings",
)
MANAGED_TABLES = ("portfolios", "portfolio_accounts", "cash_buckets", "investment_goals", "planned_contributions")   # ALL-command owner policies (pre-existing)
SERVICE_ONLY = ("private_scheduler_runs", "private_scheduler_run_transitions", "learning_evidence_bindings", "rate_limits")
REFERENCE_PUBLIC_READ = ("allowed_assets", "macro_observations", "macro_series")
REFERENCE_AUTHENTICATED_READ = ("instruments", "corporate_actions", "provider_aliases", "normalized_observations", "raw_provider_snapshots", "sec_filings", "sec_raw_facts", "sec_fact_filing_links")
LEGACY_USER_SCOPED = ("user_profiles",)
CLASSIFIED = set(OWNER_TABLES) | set(SERVICE_ONLY) | set(REFERENCE_PUBLIC_READ) | set(REFERENCE_AUTHENTICATED_READ) | set(LEGACY_USER_SCOPED)
OWNER_RPCS = (
    "public.commit_portfolio_import_claim(jsonb,jsonb)",
    "public.commit_portfolio_import_claim_batch(jsonb)",
    "public.lookup_portfolio_transaction_external_identity(uuid,uuid,uuid,text,text)",
)
# Tables that exist in the repository's legacy SQL files but in no migration. A live project may or may not contain them: UNKNOWN.
LEGACY_NON_MIGRATION_TABLES = ("user_settings", "paper_trades", "portfolio_snapshots", "legacy_portfolio_watchlists", "alerts", "llm_usage_logs", "user_events")

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")   # synthetic fixture identities, never real
OTHER = uuid.UUID("22222222-2222-4222-8222-222222222222")
P_OWNER, A_OWNER = uuid.uuid4(), uuid.uuid4()
P_OTHER, A_OTHER = uuid.uuid4(), uuid.uuid4()

AUTH_SQL = """
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT COALESCE(NULLIF(current_setting('request.jwt.claim.sub', true), ''),
                    (NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'))::uuid $$;
CREATE OR REPLACE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT COALESCE(NULLIF(current_setting('request.jwt.claim', true), ''), NULLIF(current_setting('request.jwt.claims', true), ''))::jsonb $$;
-- Supabase grants these to the API roles (policies call them with the caller's own privileges).
GRANT EXECUTE ON FUNCTION auth.uid(), auth.jwt() TO anon, authenticated, service_role;
"""


def claims(sub=OWNER, aal="aal2", amr="totp", **extra):
    c = {"sub": str(sub), "role": "authenticated", "aud": "authenticated"}
    if aal is not None:
        c["aal"] = aal
    if amr == "totp":
        c["amr"] = [{"method": "totp", "timestamp": 1760000000}, {"method": "password", "timestamp": 1759999990}]
    elif amr == "password":
        c["amr"] = [{"method": "password", "timestamp": 1760000000}]
    elif amr is not None:
        c["amr"] = amr
    c.update(extra)
    return c


# name -> (claims, owner_with_full_access)
OWNER_OK = claims()
DENIED_CLAIMS = {
    "owner aal1 (password only)": claims(aal="aal1", amr="password"),
    "owner missing aal": claims(aal=None),
    "owner aal2 but no totp in amr": claims(aal="aal2", amr="password"),
    "owner aal1 claiming totp": claims(aal="aal1", amr="totp"),
    "owner aal2 amr missing": claims(aal="aal2", amr=None),
    "owner aal2 amr is a string": claims(aal="aal2", amr="totp-string"),
    "owner aal2 amr entry without method": claims(aal="aal2", amr=[{"timestamp": 1}]),
    "owner aal2 amr method upper-case": claims(aal="aal2", amr=[{"method": "TOTP", "timestamp": 1}]),
    "owner aal2 amr totp without timestamp": claims(aal="aal2", amr=[{"method": "totp"}]),
    "owner aal2 amr non-list object": claims(aal="aal2", amr={"method": "totp", "timestamp": 1}),
    "other user aal2 totp": claims(sub=OTHER),
    "no sub": {k: v for k, v in claims().items() if k != "sub"},
}


class DbEnv(Env):
    """Env bound to its own database on the same disposable server (roles are cluster-wide; schemas are per database)."""

    def __init__(self, label):
        self.dbname = f"sentinax_owneraal2_{label}_concurrency"
        with psycopg.connect(POSTGRES_URL, autocommit=True) as root:
            root.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')    # pristine every time: default privileges are per database and survive DROP SCHEMA
            root.execute(f'CREATE DATABASE "{self.dbname}"')
        super().__init__()

    def connect(self, role=None, autocommit=True):
        conn = psycopg.connect(make_conninfo(POSTGRES_URL, dbname=self.dbname), autocommit=True, row_factory=dict_row)
        if role:
            conn.execute(f"SET ROLE {role}")
        conn.autocommit = autocommit
        self.conns.append(conn)
        return conn


def _replay(*, candidate, configure=True, before_candidate=None, seed=True, db="tmp"):
    env = DbEnv(db)
    db = env.scalar("SELECT current_database()")
    assert db.startswith("sentinax_") and "concurrency" in db, f"refusing to reset non-test database {db!r}"
    assert env.scalar("SELECT count(*) FROM pg_namespace WHERE nspname IN ('storage', 'realtime', 'graphql')") == 0, "looks like a real Supabase project"
    env.admin.execute("DROP SCHEMA IF EXISTS private_config CASCADE")
    env.admin.execute(BOOTSTRAP_SQL)
    env.admin.execute(AUTH_SQL)
    available = {r["name"] for r in env.admin.execute("SELECT name FROM pg_available_extensions").fetchall()}
    for path in MIGRATIONS:
        env.admin.execute(prepared(path, "btree_gist" in available, "uuid-ossp" in available))
    if before_candidate:
        before_candidate(env)
    if candidate and CANDIDATE.exists():
        env.admin.execute(CANDIDATE.read_text(encoding="utf-8"))
        if configure:
            env.admin.execute("INSERT INTO private_config.owner_identity (owner_id) VALUES (%s)", (OWNER,))
    if seed:
        seed_rows(env)
    return env


def seed_rows(env):
    a = env.admin
    for u in (OWNER, OTHER):
        a.execute("INSERT INTO auth.users (id) VALUES (%s)", (u,))
    for u, p, acc in ((OWNER, P_OWNER, A_OWNER), (OTHER, P_OTHER, A_OTHER)):
        a.execute("INSERT INTO public.portfolios (id, owner_id, mode, name, base_currency) VALUES (%s, %s, 'my_portfolio', 'p', 'TRY')", (p, u))
        a.execute("INSERT INTO public.portfolio_accounts (id, portfolio_id, owner_id, name, base_currency) VALUES (%s, %s, %s, 'a', 'TRY')", (acc, p, u))
        a.execute("INSERT INTO public.cash_buckets (portfolio_id, owner_id, name, currency, purpose, included_in_investable_assets) VALUES (%s, %s, 'c', 'TRY', 'investable', true)", (p, u))
        a.execute("INSERT INTO public.investment_goals (portfolio_id, owner_id, name, target_amount, target_currency) VALUES (%s, %s, 'g', 100, 'TRY')", (p, u))
        a.execute("INSERT INTO public.planned_contributions (portfolio_id, owner_id, expected_date, amount, currency) VALUES (%s, %s, DATE '2026-12-01', 10, 'TRY')", (p, u))
    txs = {}
    for who, (u, p, acc) in {"owner": (OWNER, P_OWNER, A_OWNER), "other": (OTHER, P_OTHER, A_OTHER)}.items():
        for kind, minute in (("cash_deposit", 0), ("fee", 1)):
            tx = uuid.uuid4()
            txs[(who, kind)] = tx
            a.execute(
                "INSERT INTO public.portfolio_transactions (id, portfolio_id, account_id, owner_id, transaction_type, effective_date, cash_amount, cash_currency, recorded_at, economic_fingerprint)"
                " VALUES (%s, %s, %s, %s, %s, '2026-01-02', 5, 'TRY', TIMESTAMPTZ '2026-01-02 00:00+00' + (%s * interval '1 minute'), %s)",
                (tx, p, acc, u, kind, minute, uuid.uuid4().hex + uuid.uuid4().hex))
    for who, (u, p, acc) in {"owner": (OWNER, P_OWNER, A_OWNER), "other": (OTHER, P_OTHER, A_OTHER)}.items():
        a.execute(
            "INSERT INTO public.portfolio_fee_tax_attribution_events (id, portfolio_id, account_id, owner_id, event_type, recorded_at, charge_transaction_id, target_transaction_id, allocated_amount)"
            " VALUES (%s, %s, %s, %s, 'allocation', TIMESTAMPTZ '2026-01-03 00:00+00', %s, %s, 1)", (uuid.uuid4(), p, acc, u, txs[(who, "fee")], txs[(who, "cash_deposit")]))
        a.execute(
            "INSERT INTO public.portfolio_import_claim_bindings (owner_id, portfolio_id, account_id, source_key, file_content_sha256, record_ordinal, record_sha256, expected_plan_sha256, transaction_id)"
            " VALUES (%s, %s, %s, 'fixture', %s, 1, %s, %s, %s)", (u, p, acc, "a" * 64, "b" * 64, "c" * 64, txs[(who, "cash_deposit")]))


@contextlib.contextmanager
def session(env, role, claim_set=None):
    conn = env.connect(role)                                          # SET ROLE in autocommit
    try:
        if claim_set is not None:
            conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (json.dumps(claim_set),))
        conn.autocommit = False
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
            env.conns.remove(conn)


def visible(env, role, claim_set, table):
    """Rows visible to (role, claims) on public.<table>; a privilege error counts as 0 rows visible."""
    with session(env, role, claim_set) as conn:
        try:
            return conn.execute(f"SELECT count(*) AS n FROM public.{table}").fetchone()["n"]
        except pgerr.InsufficientPrivilege:
            return 0


def _try(conn, sql, params=()):
    try:
        cur = conn.execute(sql, params)
        return "ok" if (cur.rowcount != 0) else "denied"
    except psycopg.Error as error:
        return "denied" if error.sqlstate == "42501" else f"error:{error.sqlstate}"


INSERTS = {
    "portfolios": "INSERT INTO public.portfolios (id, owner_id, mode, name, base_currency) VALUES (%(id)s, %(o)s, 'sandbox', 'n', 'TRY')",
    "portfolio_accounts": "INSERT INTO public.portfolio_accounts (id, portfolio_id, owner_id, name, base_currency) VALUES (%(id)s, %(p)s, %(o)s, 'n', 'TRY')",
    "cash_buckets": "INSERT INTO public.cash_buckets (id, portfolio_id, owner_id, name, currency, purpose, included_in_investable_assets) VALUES (%(id)s, %(p)s, %(o)s, 'n', 'TRY', 'investable', true)",
    "investment_goals": "INSERT INTO public.investment_goals (id, portfolio_id, owner_id, name, target_amount, target_currency) VALUES (%(id)s, %(p)s, %(o)s, 'n', 5, 'TRY')",
    "planned_contributions": "INSERT INTO public.planned_contributions (id, portfolio_id, owner_id, expected_date, amount, currency) VALUES (%(id)s, %(p)s, %(o)s, DATE '2026-12-02', 5, 'TRY')",
}
UPDATES = {
    "portfolios": "UPDATE public.portfolios SET name = name WHERE id = %(p)s",
    "portfolio_accounts": "UPDATE public.portfolio_accounts SET name = name WHERE portfolio_id = %(p)s",
    "cash_buckets": "UPDATE public.cash_buckets SET name = name WHERE portfolio_id = %(p)s",
    "investment_goals": "UPDATE public.investment_goals SET name = name WHERE portfolio_id = %(p)s",
    "planned_contributions": "UPDATE public.planned_contributions SET amount = amount WHERE portfolio_id = %(p)s",
}


def ops(env, role, claim_set, owner, portfolio):
    """insert / update / insert-then-delete outcome per managed table, every statement rolled back."""
    out = {}
    for table in MANAGED_TABLES:
        for kind in ("insert", "update", "delete"):
            with session(env, role, claim_set) as conn:
                params = {"id": uuid.uuid4(), "o": owner, "p": portfolio}
                if kind == "insert":
                    out[(table, kind)] = _try(conn, INSERTS[table], params)
                elif kind == "update":
                    out[(table, kind)] = _try(conn, UPDATES[table], params)
                else:
                    ins = _try(conn, INSERTS[table], params)
                    out[(table, kind)] = ins if ins != "ok" else _try(conn, f"DELETE FROM public.{table} WHERE id = %(id)s", params)
    return out


def snapshot(env):
    """Every API-role privilege that exists: table, column, function, sequence and schema level."""
    s = set()
    a = env.admin
    for role in ROLES:
        for r in a.execute(
            "SELECT n.nspname || '.' || c.relname AS o, p.priv FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER')) p(priv)"
            " WHERE n.nspname IN ('public','private_config','auth') AND c.relkind IN ('r','v','m','p') AND has_table_privilege(%s, c.oid, p.priv)", (role,)).fetchall():
            s.add((role, "table", r["o"], r["priv"]))
        for r in a.execute(
            "SELECT n.nspname || '.' || c.relname || '.' || at.attname AS o, p.priv FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute at ON at.attrelid = c.oid AND at.attnum > 0 AND NOT at.attisdropped"
            " CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES')) p(priv)"
            " WHERE n.nspname IN ('public','private_config') AND c.relkind IN ('r','v','m','p') AND has_column_privilege(%s, c.oid, at.attnum, p.priv)", (role,)).fetchall():
            s.add((role, "column", r["o"], r["priv"]))
        for r in a.execute(
            "SELECT n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS o FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname IN ('public','private_config') AND has_function_privilege(%s, p.oid, 'EXECUTE')", (role,)).fetchall():
            s.add((role, "function", r["o"], "EXECUTE"))
        for r in a.execute(
            "SELECT n.nspname || '.' || c.relname AS o, p.priv FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN (VALUES ('USAGE'),('SELECT'),('UPDATE')) p(priv) WHERE n.nspname IN ('public','private_config') AND c.relkind = 'S' AND has_sequence_privilege(%s, c.oid, p.priv)", (role,)).fetchall():
            s.add((role, "sequence", r["o"], r["priv"]))
        for r in a.execute(
            "SELECT n.nspname AS o, p.priv FROM pg_namespace n CROSS JOIN (VALUES ('USAGE'),('CREATE')) p(priv)"
            " WHERE n.nspname IN ('public','private_config','auth') AND has_schema_privilege(%s, n.oid, p.priv)", (role,)).fetchall():
            s.add((role, "schema", r["o"], r["priv"]))
    return s


def policy_catalog(env):
    return sorted(
        (r["tablename"], r["policyname"], r["permissive"], r["cmd"], tuple(r["roles"]), r["qual"], r["with_check"])
        for r in env.admin.execute("SELECT tablename, policyname, permissive, cmd, roles, qual, with_check FROM pg_policies WHERE schemaname = 'public'").fetchall()
    )


@pytest.fixture(scope="module")
def pre():
    env = _replay(candidate=False, db="pre")
    yield env
    env.close()


@pytest.fixture(scope="module")
def post():
    env = _replay(candidate=True, db="post")
    yield env
    env.close()


@pytest.fixture(scope="module")
def post_empty():
    env = _replay(candidate=True, configure=False, db="postempty")
    yield env
    env.close()


# --- 0. artefacts and placement ----------------------------------------------------------------------------------------------------------------
def test_candidate_artefacts_exist_and_are_not_in_the_auto_applied_migrations_directory():
    for f in (CANDIDATE, ROLLBACK, INVENTORY):
        assert f.exists(), f"{f.name} missing"
    assert not [m for m in MIGRATIONS if m.name.startswith("032_")], "032 must stay a candidate until a separately approved promotion"
    assert max(m.name[:3] for m in MIGRATIONS) == "031"


def test_no_real_identity_or_secret_in_the_candidate_artefacts():
    for f in (CANDIDATE, ROLLBACK, INVENTORY):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text, re.I), f"{f.name} contains a UUID literal"
        assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}|sb_secret_|service_role_key|password\s*=", text), f.name


def test_operator_inventory_is_read_only():
    text = INVENTORY.read_text(encoding="utf-8")
    body = re.sub(r"'[^']*'", "''", re.sub(r"--[^\n]*", "", text))     # comments and string literals ('UPDATE' as a privilege name) are not statements
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|COPY|DO|CALL|SET)\b", body, re.I), "inventory must contain SELECT statements only"
    assert "pg_policies" in body and "pg_default_acl" in body and "has_table_privilege" in body


# --- 1. classification: every public relation is accounted for; legacy tables are handled explicitly ------------------------------------------------
def test_every_public_relation_is_classified_and_no_view_exists(pre):
    rels = {r["relname"]: r["relkind"] for r in pre.admin.execute(
        "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','p','f')").fetchall()}
    assert set(rels) == CLASSIFIED, (set(rels) ^ CLASSIFIED)
    assert set(rels.values()) == {"r"}


def test_legacy_non_migration_tables_are_absent_from_migrations_and_block_the_candidate():
    for name in LEGACY_NON_MIGRATION_TABLES:
        env = _replay(candidate=False, seed=False, before_candidate=lambda e, n=name: e.admin.execute(f"CREATE TABLE public.{n} (id int)"))
        try:
            with pytest.raises(pgerr.RaiseException) as err:
                env.admin.execute(CANDIDATE.read_text(encoding="utf-8"))
            assert "owner_aal2_blocker" in str(err.value) and name in str(err.value)
            assert env.scalar("SELECT count(*) FROM pg_namespace WHERE nspname = 'private_config'") == 0, "failed apply must leave no partial state"
            assert env.scalar("SELECT count(*) FROM pg_policies WHERE policyname LIKE 'owner_aal2%%'") == 0
        finally:
            env.close()


def test_the_legacy_sql_files_list_exactly_the_tables_this_contract_treats_as_unverified():
    declared = set()
    for f in ("infrastructure/database/schema.sql", "infrastructure/database/supabase_setup.sql"):
        declared |= set(re.findall(r"CREATE TABLE IF NOT EXISTS public\.(\w+)", (REPO_ROOT / f).read_text(encoding="utf-8")))
    assert declared == set(LEGACY_NON_MIGRATION_TABLES)
    assert declared.isdisjoint(CLASSIFIED)


@pytest.mark.parametrize("breaker", ("view", "rls_off", "no_jwt_fn"))
def test_other_preconditions_block_the_candidate_atomically(breaker):
    def hook(e):
        if breaker == "view":
            e.admin.execute("CREATE VIEW public.owner_leak AS SELECT * FROM public.portfolios")
        elif breaker == "rls_off":
            e.admin.execute("ALTER TABLE public.portfolios DISABLE ROW LEVEL SECURITY")
        else:
            e.admin.execute("DROP FUNCTION auth.jwt()")
    env = _replay(candidate=False, seed=False, before_candidate=hook)
    try:
        with pytest.raises(pgerr.RaiseException) as err:
            env.admin.execute(CANDIDATE.read_text(encoding="utf-8"))
        assert "owner_aal2_blocker" in str(err.value)
        assert env.scalar("SELECT count(*) FROM pg_namespace WHERE nspname = 'private_config'") == 0
        assert env.scalar("SELECT count(*) FROM pg_policies WHERE policyname LIKE 'owner_aal2%%'") == 0
    finally:
        env.close()


# --- 2. access matrix on the real policies --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("table", OWNER_TABLES)
def test_owner_with_aal2_and_totp_still_reads_own_rows(post, table):
    assert visible(post, "authenticated", OWNER_OK, table) >= 1


@pytest.mark.parametrize("table", OWNER_TABLES)
@pytest.mark.parametrize("name", sorted(DENIED_CLAIMS))
def test_every_weaker_or_foreign_identity_sees_nothing(post, table, name):
    assert visible(post, "authenticated", DENIED_CLAIMS[name], table) == 0, (table, name)


@pytest.mark.parametrize("table", OWNER_TABLES)
def test_empty_owner_configuration_denies_even_a_perfect_token(post_empty, table):
    assert visible(post_empty, "authenticated", OWNER_OK, table) == 0
    assert post_empty.admin.execute("SELECT count(*) AS n FROM public.%s" % table).fetchone()["n"] >= 1      # rows exist; only access is denied


def test_the_helper_is_false_without_configuration_and_for_a_nil_or_second_owner(post_empty):
    ok = post_empty.scalar("SELECT private_config.owner_aal2()")                                              # admin session, no claims
    assert ok is False
    with pytest.raises(pgerr.CheckViolation):
        post_empty.admin.execute("INSERT INTO private_config.owner_identity (owner_id) VALUES ('00000000-0000-0000-0000-000000000000')")


@pytest.mark.parametrize("table", OWNER_TABLES)
def test_anon_has_no_privilege_at_all_on_owner_private_tables(post, table):
    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert post.scalar("SELECT has_table_privilege('anon', %s, %s)", (f"public.{table}", priv)) is False, (table, priv)
    assert visible(post, "anon", None, table) == 0


@pytest.mark.parametrize("table", OWNER_TABLES)
def test_authenticated_holds_no_truncate_references_or_trigger_privilege_on_owner_tables(post, table):
    for priv in ("TRUNCATE", "REFERENCES", "TRIGGER"):
        assert post.scalar("SELECT has_table_privilege('authenticated', %s, %s)", (f"public.{table}", priv)) is False, (table, priv)


@pytest.mark.parametrize("signature", OWNER_RPCS)
def test_owner_ledger_rpcs_are_not_executable_by_anon_or_public(post, signature):
    assert post.scalar("SELECT has_function_privilege('anon', %s::regprocedure, 'EXECUTE')", (signature,)) is False
    assert post.scalar("SELECT has_function_privilege('service_role', %s::regprocedure, 'EXECUTE')", (signature,)) is True   # the trusted writer is untouched


def test_public_reference_policies_and_other_non_owner_tables_are_untouched(pre, post):
    keep = [p for p in policy_catalog(pre) if p[0] not in OWNER_TABLES]
    assert [p for p in policy_catalog(post) if p[0] not in OWNER_TABLES] == keep


# --- 3. previously allowed operations stay allowed, nothing else is added ----------------------------------------------------------------------------
def test_owner_aal2_totp_keeps_exactly_the_operations_the_pre_032_policies_allowed(pre, post):
    before = ops(pre, "authenticated", OWNER_OK, OWNER, P_OWNER)
    after = ops(post, "authenticated", OWNER_OK, OWNER, P_OWNER)
    assert set(before.values()) == {"ok"}, before                      # sanity: the fixture really exercises allowed writes
    assert after == before


@pytest.mark.parametrize("name", sorted(DENIED_CLAIMS))
def test_every_weaker_identity_loses_all_writes_on_managed_tables(post, name):
    c = DENIED_CLAIMS[name]
    subject_is_owner = c.get("sub") == str(OWNER)
    after = ops(post, "authenticated", c, OWNER if subject_is_owner else OTHER, P_OWNER if subject_is_owner else P_OTHER)
    assert set(after.values()) <= {"denied"}, after


def test_other_user_could_write_own_rows_before_032_and_cannot_after(pre, post):
    assert set(ops(pre, "authenticated", claims(sub=OTHER), OTHER, P_OTHER).values()) == {"ok"}
    assert set(ops(post, "authenticated", claims(sub=OTHER), OTHER, P_OTHER).values()) == {"denied"}


def test_no_authenticated_direct_insert_into_the_ledger_for_anyone_ever(post, pre):
    for env in (pre, post):
        for c in (OWNER_OK, claims(aal="aal1", amr="password"), claims(sub=OTHER)):
            with session(env, "authenticated", c) as conn:
                outcome = _try(conn, "INSERT INTO public.portfolio_transactions (portfolio_id, account_id, owner_id, transaction_type, effective_date, cash_amount, cash_currency, recorded_at, economic_fingerprint)"
                               " VALUES (%s, %s, %s, 'cash_deposit', '2026-01-02', 1, 'TRY', now(), %s)", (P_OWNER, A_OWNER, OWNER, uuid.uuid4().hex + uuid.uuid4().hex))
            assert outcome == "denied"
        for stmt in ("UPDATE public.portfolio_transactions SET cash_amount = 2", "DELETE FROM public.portfolio_transactions", "TRUNCATE public.portfolio_transactions"):
            with session(env, "authenticated", OWNER_OK) as conn:
                assert _try(conn, stmt) == "denied", stmt


def test_restrictive_composition_adds_one_restrictive_policy_per_owner_table_and_no_permissive_one(pre, post):
    before, after = policy_catalog(pre), policy_catalog(post)
    assert [p for p in after if p[2] == "PERMISSIVE"] == [p for p in before if p[2] == "PERMISSIVE"]       # no permissive policy added, changed or dropped
    added = [p for p in after if p not in before]
    assert sorted(p[0] for p in added) == sorted(OWNER_TABLES)
    for table, name, permissive, cmd, roles, qual, check in added:
        assert (name, permissive, cmd, roles) == ("owner_aal2_required", "RESTRICTIVE", "ALL", ("authenticated",))
        assert "owner_aal2" in qual and "owner_aal2" in check
    assert all(p[2] == "PERMISSIVE" for p in before)                  # nothing restrictive existed before: no policy was silently replaced


# --- 4. nothing is broadened; defaults and the new helper -----------------------------------------------------------------------------------------
def test_candidate_never_broadens_any_privilege_and_adds_only_the_helper_execute_for_authenticated(pre, post):
    before, after = snapshot(pre), snapshot(post)
    added = after - before
    assert added == {("authenticated", "function", "private_config.owner_aal2()", "EXECUTE")}, sorted(added)
    removed = before - after
    assert removed, "the candidate is expected to narrow something"
    assert all(role in ("anon", "authenticated") for role, *_ in removed), "service_role privileges must be unchanged"
    assert not [r for r in removed if r[0] == "service_role"]


def test_default_privileges_and_service_role_table_rights_are_unchanged(pre, post):
    acl = "SELECT defaclrole::regrole::text, defaclnamespace::regnamespace::text, defaclobjtype, defaclacl::text FROM pg_default_acl ORDER BY 1,2,3,4"
    assert [tuple(r.values()) for r in pre.admin.execute(acl).fetchall()] == [tuple(r.values()) for r in post.admin.execute(acl).fetchall()]
    assert {s for s in snapshot(pre) if s[0] == "service_role"} == {s for s in snapshot(post) if s[0] == "service_role" and not s[2].startswith("private_config.")}


def test_helper_is_a_pinned_security_definer_outside_the_exposed_schema_and_cannot_be_called_or_read_by_api_roles(post):
    row = post.admin.execute(
        "SELECT p.prosecdef, p.provolatile, p.prorettype::regtype::text AS ret, n.nspname, p.proconfig, pg_get_userbyid(p.proowner) AS owner, p.prosrc"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.proname = 'owner_aal2'").fetchone()
    assert row["nspname"] == "private_config" and row["prosecdef"] is True and row["ret"] == "boolean" and row["provolatile"] == "s"
    assert row["proconfig"] and any(c.replace(" ", "") in ("search_path=", 'search_path=""') for c in row["proconfig"]), row["proconfig"]
    assert row["owner"] not in ROLES, "definer must be the migration owner, never an API role"
    assert not re.search(r"owner_id\s*=\s*'", row["prosrc"]), "no literal identity inside the function"
    assert post.scalar("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'owner_aal2'") == 0
    for role in ROLES:
        assert post.scalar("SELECT has_schema_privilege(%s, 'private_config', 'USAGE')", (role,)) is False
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert post.scalar("SELECT has_table_privilege(%s, 'private_config.owner_identity', %s)", (role, priv)) is False, (role, priv)
    assert post.scalar("SELECT has_function_privilege('authenticated', 'private_config.owner_aal2()'::regprocedure, 'EXECUTE')") is True
    for role in ("anon", "service_role", "public"):
        assert post.scalar("SELECT has_function_privilege(%s, 'private_config.owner_aal2()'::regprocedure, 'EXECUTE')", (role,)) is False
    for sql in ("SELECT private_config.owner_aal2()",                                  # not reachable by name: the owner identity can never be probed
                "SELECT owner_id FROM private_config.owner_identity", "INSERT INTO private_config.owner_identity (owner_id) VALUES (gen_random_uuid())"):
        with session(post, "authenticated", OWNER_OK) as conn:
            with pytest.raises(pgerr.InsufficientPrivilege):
                conn.execute(sql)


def test_owner_configuration_is_a_singleton_with_rls_and_no_api_visibility(post):
    assert post.scalar("SELECT relrowsecurity FROM pg_class WHERE oid = 'private_config.owner_identity'::regclass") is True
    assert post.scalar("SELECT count(*) FROM private_config.owner_identity") == 1
    with pytest.raises(pgerr.UniqueViolation):
        post.admin.execute("INSERT INTO private_config.owner_identity (owner_id) VALUES (%s)", (OTHER,))
    with session(post, "service_role", OWNER_OK) as conn:
        with pytest.raises(pgerr.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM private_config.owner_identity")


# --- 5. service_role is a separate boundary and is not misreported as RLS-protected --------------------------------------------------------------------
@pytest.mark.parametrize("table", OWNER_TABLES)
def test_service_role_bypasses_rls_by_design_even_with_empty_config_and_no_claims(post_empty, table):
    assert post_empty.scalar("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'service_role'") is True
    assert visible(post_empty, "service_role", None, table) >= 1                  # this is NOT protection by RLS; the FastAPI owner/AAL2 layer guards service-role use


def test_ledger_and_reversal_semantics_are_unchanged_for_service_role(post):
    svc = post.connect("service_role")
    tx = uuid.uuid4()
    svc.execute(
        "INSERT INTO public.portfolio_transactions (id, portfolio_id, account_id, owner_id, transaction_type, effective_date, cash_amount, cash_currency, recorded_at, economic_fingerprint)"
        " VALUES (%s, %s, %s, %s, 'cash_deposit', '2026-02-01', 7, 'TRY', TIMESTAMPTZ '2026-02-01 00:00+00', %s)", (tx, P_OWNER, A_OWNER, OWNER, uuid.uuid4().hex + uuid.uuid4().hex))
    svc.execute(
        "INSERT INTO public.portfolio_transactions (portfolio_id, account_id, owner_id, transaction_type, effective_date, reverses_transaction_id, recorded_at, economic_fingerprint)"
        " VALUES (%s, %s, %s, 'reversal', '2026-02-02', %s, TIMESTAMPTZ '2026-02-02 00:00+00', %s)", (P_OWNER, A_OWNER, OWNER, tx, uuid.uuid4().hex + uuid.uuid4().hex))
    for stmt in ("UPDATE public.portfolio_transactions SET cash_amount = 9", "DELETE FROM public.portfolio_transactions", "TRUNCATE public.portfolio_transactions"):
        with pytest.raises(psycopg.Error):
            svc.execute(stmt)


# --- 6. idempotent install and guarded rollback ---------------------------------------------------------------------------------------------------
def test_installing_twice_changes_nothing_and_keeps_the_configured_owner(post):
    env = _replay(candidate=True)
    try:
        first = (policy_catalog(env), snapshot(env), env.scalar("SELECT pg_get_functiondef('private_config.owner_aal2()'::regprocedure)"), env.scalar("SELECT owner_id FROM private_config.owner_identity"))
        env.admin.execute(CANDIDATE.read_text(encoding="utf-8"))
        second = (policy_catalog(env), snapshot(env), env.scalar("SELECT pg_get_functiondef('private_config.owner_aal2()'::regprocedure)"), env.scalar("SELECT owner_id FROM private_config.owner_identity"))
        assert first == second
        assert visible(env, "authenticated", OWNER_OK, "portfolios") >= 1
    finally:
        env.close()


def test_rollback_requires_explicit_confirmation_of_a_verified_pre_032_snapshot():
    env = _replay(candidate=True)
    try:
        with pytest.raises(pgerr.RaiseException) as err:
            env.admin.execute(ROLLBACK.read_text(encoding="utf-8"))
        assert "rollback_confirm" in str(err.value)
        assert env.scalar("SELECT count(*) FROM pg_policies WHERE policyname = 'owner_aal2_required'") == len(OWNER_TABLES)
    finally:
        env.close()


def test_confirmed_rollback_restores_the_pre_032_policy_surface_without_broadening_anything():
    base = _replay(candidate=False, db="tmp2")
    env = _replay(candidate=True, db="tmp")
    try:
        before_snapshot = snapshot(base)
        env.admin.execute(f"SET sentinax.rollback_confirm = '{ROLLBACK_CONFIRM}'; " + ROLLBACK.read_text(encoding="utf-8"))
        assert policy_catalog(env) == policy_catalog(base)
        assert snapshot(env) <= before_snapshot, sorted(snapshot(env) - before_snapshot)           # never grants more than pre-032 (re-granting needs the operator's snapshot)
        assert env.scalar("SELECT count(*) FROM pg_proc WHERE proname = 'owner_aal2'") == 0
        assert env.scalar("SELECT count(*) FROM private_config.owner_identity") == 1, "operator-provided configuration is never destroyed"
    finally:
        env.close()
        base.close()


def test_helper_and_config_stay_closed_even_if_the_live_default_privileges_are_stock_postgres(post):
    """Migration 028 revokes the default PUBLIC EXECUTE for the migration role, but the live default ACL is UNKNOWN: the candidate must not depend on it."""
    def stock_defaults(env):
        env.admin.execute("ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO PUBLIC")
        env.admin.execute("ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO anon, authenticated, service_role")
        env.admin.execute("ALTER DEFAULT PRIVILEGES GRANT USAGE ON SCHEMAS TO anon, authenticated, service_role")
    env = _replay(candidate=True, before_candidate=stock_defaults)
    try:
        for role in ("anon", "service_role", "public"):
            assert env.scalar("SELECT has_function_privilege(%s, 'private_config.owner_aal2()'::regprocedure, 'EXECUTE')", (role,)) is False, role
        for role in ROLES:
            assert env.scalar("SELECT has_schema_privilege(%s, 'private_config', 'USAGE')", (role,)) is False, role
            assert env.scalar("SELECT has_table_privilege(%s, 'private_config.owner_identity', 'SELECT')", (role,)) is False, role
        assert visible(env, "authenticated", OWNER_OK, "portfolios") >= 1 and visible(env, "authenticated", claims(aal="aal1", amr="password"), "portfolios") == 0
    finally:
        env.close()
