"""Phase 27 FIX A: real PostgreSQL verification of migration 027 (privileged ledger/attribution trigger locking).

No mocks and no re-implemented trigger logic: the REAL repository migrations 011, 018, 019, 020, 021 and 025
(the migration-025 write surface) are executed unmodified, and then the REAL migration 027. The only fixture
SQL is the minimum prerequisite (roles, auth stand-ins, public.instruments stand-in for migration 005,
Supabase-style default privileges). The fixture does not prove migration 005.

Runs only when SENTINAX_TEST_POSTGRES_URL points at a disposable database. When it is set, a missing driver
or an unreachable server FAILS (never skips).
"""
import datetime as dt
import hashlib
import os
import pathlib
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
MIGRATIONS = REPO_ROOT / "supabase" / "migrations"


def migration(prefix: str) -> str:
    matches = sorted(MIGRATIONS.glob(f"{prefix}_*.sql"))
    assert len(matches) == 1, f"expected exactly one migration {prefix}, found {matches}"
    return matches[0].read_text(encoding="utf-8")


PRE_025_MIGRATIONS = ("011", "018", "019", "020", "021")
FIX_MIGRATION = "027"

INSUFFICIENT_PRIVILEGE = "42501"
LOCK_NOT_AVAILABLE = "55P03"
LOCK_TIMEOUT = "400ms"
LEDGER = "public.portfolio_transactions"
ATTR = "public.portfolio_fee_tax_attribution_events"
TRIGGER_FUNCTIONS = (
    "lock_portfolio_transaction_reversal_target",
    "validate_fee_tax_attribution_event_integrity",
)

T0 = dt.datetime(2026, 1, 2, 9, 0, tzinfo=dt.timezone.utc)


def at(minutes: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


# Minimum prerequisite fixture only (roles, auth stand-ins, instruments stand-in, Supabase-style default grants).
BOOTSTRAP_SQL = """
DROP SCHEMA IF EXISTS public CASCADE;
DROP SCHEMA IF EXISTS auth CASCADE;
CREATE SCHEMA public;
CREATE SCHEMA auth;

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END $$;

-- Supabase's service_role bypasses RLS; the fixture must match that runtime fact.
ALTER ROLE service_role BYPASSRLS;

CREATE TABLE auth.users (id uuid PRIMARY KEY);
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
CREATE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS $$ SELECT current_user::text $$;

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;

-- Supabase-style default privileges that the real migrations must tighten.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role;

-- Stand-in for migration 005 (the ledger only references public.instruments(id)).
CREATE TABLE public.instruments (id uuid PRIMARY KEY);
"""


def fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


class Env:
    def __init__(self):
        self.conns = []
        self.admin = psycopg.connect(POSTGRES_URL, autocommit=True, row_factory=dict_row)
        self.conns.append(self.admin)
        self.owner = uuid.uuid4()
        self.portfolio = uuid.uuid4()
        self.account = uuid.uuid4()

    def session(self, role: str = "service_role", lock_timeout: str = LOCK_TIMEOUT, autocommit: bool = True):
        conn = psycopg.connect(POSTGRES_URL, autocommit=True, row_factory=dict_row)
        conn.execute(f"SET ROLE {role}")
        conn.execute(f"SET lock_timeout = '{lock_timeout}'")
        conn.autocommit = autocommit
        self.conns.append(conn)
        return conn

    def seed_portfolio(self):
        self.admin.execute("INSERT INTO auth.users (id) VALUES (%s)", (self.owner,))
        self.admin.execute(
            "INSERT INTO public.portfolios (id, owner_id, mode, name, base_currency)"
            " VALUES (%s, %s, 'my_portfolio', 'p', 'TRY')",
            (self.portfolio, self.owner),
        )
        self.admin.execute(
            "INSERT INTO public.portfolio_accounts (id, portfolio_id, owner_id, name, base_currency)"
            " VALUES (%s, %s, %s, 'a', 'TRY')",
            (self.account, self.portfolio, self.owner),
        )

    def deposit(self, conn, minutes=0, kind="cash_deposit", amount="100"):
        tx_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO public.portfolio_transactions (id, portfolio_id, account_id, owner_id, transaction_type,"
            " effective_date, cash_amount, cash_currency, recorded_at, economic_fingerprint)"
            " VALUES (%s, %s, %s, %s, %s, '2026-01-02', %s, 'TRY', %s, %s)",
            (tx_id, self.portfolio, self.account, self.owner, kind, amount, at(minutes), fingerprint(str(tx_id))),
        )
        return tx_id

    def reversal(self, conn, target, minutes):
        tx_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO public.portfolio_transactions (id, portfolio_id, account_id, owner_id, transaction_type,"
            " effective_date, reverses_transaction_id, recorded_at, economic_fingerprint)"
            " VALUES (%s, %s, %s, %s, 'reversal', '2026-01-03', %s, %s, %s)",
            (tx_id, self.portfolio, self.account, self.owner, target, at(minutes), fingerprint(str(tx_id))),
        )
        return tx_id

    def allocation(self, conn, charge, target, minutes, amount="1"):
        ev_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO public.portfolio_fee_tax_attribution_events (id, portfolio_id, account_id, owner_id,"
            " event_type, recorded_at, charge_transaction_id, target_transaction_id, allocated_amount)"
            " VALUES (%s, %s, %s, %s, 'allocation', %s, %s, %s, %s)",
            (ev_id, self.portfolio, self.account, self.owner, at(minutes), charge, target, amount),
        )
        return ev_id

    def attribution_reversal(self, conn, event_id, minutes):
        ev_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO public.portfolio_fee_tax_attribution_events (id, portfolio_id, account_id, owner_id,"
            " event_type, recorded_at, reverses_attribution_event_id)"
            " VALUES (%s, %s, %s, %s, 'reversal', %s, %s)",
            (ev_id, self.portfolio, self.account, self.owner, at(minutes), event_id),
        )
        return ev_id

    def has(self, role, relation, priv):
        return self.admin.execute(
            "SELECT has_table_privilege(%s, %s, %s) AS ok", (role, relation, priv)
        ).fetchone()["ok"]

    def close(self):
        for conn in self.conns:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()


def make_env(apply_fix: bool):
    env = Env()
    try:
        # Fail-closed isolation guard: only a disposable, non-Supabase database may be reset.
        db = env.admin.execute("SELECT current_database() AS db").fetchone()["db"]
        assert db.startswith("sentinax_") and "concurrency" in db, f"refusing to reset non-test database {db!r}"
        assert env.admin.execute(
            "SELECT count(*) AS n FROM pg_namespace WHERE nspname IN ('storage', 'vault', 'realtime', 'graphql')"
        ).fetchone()["n"] == 0, "refusing to reset a database that looks like a real Supabase project"
        env.admin.execute(BOOTSTRAP_SQL)
        for prefix in PRE_025_MIGRATIONS:
            env.admin.execute(migration(prefix))
        env.admin.execute(migration("025"))
        if apply_fix:
            env.admin.execute(migration(FIX_MIGRATION))
        env.seed_portfolio()
    except Exception:
        env.close()
        raise
    return env


@pytest.fixture
def pg025():
    """Real migrations 011..025 only: the broken (pre-fix) state."""
    env = make_env(apply_fix=False)
    try:
        yield env
    finally:
        env.close()


@pytest.fixture
def pg():
    """Real migrations 011..025 plus the real fix migration 027."""
    env = make_env(apply_fix=True)
    try:
        yield env
    finally:
        env.close()


def test_server_version_is_postgres_16_or_newer(pg):
    version = pg.admin.execute("SHOW server_version").fetchone()["server_version"]
    major = int(pg.admin.execute("SHOW server_version_num").fetchone()["server_version_num"]) // 10000
    print(f"EVIDENCE postgres_server_version={version} major={major}")
    assert major >= 16


# A. RED: the real migration-025 state fails -------------------------------------------------------------------
def test_red_migration_025_state_breaks_service_role_reversal_and_attribution(pg025):
    svc = pg025.session()
    # The ordinary permitted INSERT works.
    charge = pg025.deposit(svc, 0, kind="fee", amount="5")
    target = pg025.deposit(svc, 1)
    plain = pg025.deposit(svc, 2)
    # Ledger reversal: the trigger's SELECT ... FOR UPDATE needs UPDATE privilege that 025 withdrew.
    with pytest.raises(pgerr.InsufficientPrivilege) as ledger_err:
        pg025.reversal(svc, plain, 10)
    assert ledger_err.value.sqlstate == INSUFFICIENT_PRIVILEGE
    # Fee/tax allocation fails for the same reason.
    with pytest.raises(pgerr.InsufficientPrivilege) as alloc_err:
        pg025.allocation(svc, charge, target, 11)
    assert alloc_err.value.sqlstate == INSUFFICIENT_PRIVILEGE
    print(
        f"EVIDENCE red_sqlstate ledger_reversal={ledger_err.value.sqlstate} allocation={alloc_err.value.sqlstate}"
        f" :: {str(ledger_err.value).splitlines()[0]}"
    )
    for fn in TRIGGER_FUNCTIONS:
        row = pg025.admin.execute("SELECT prosecdef FROM pg_proc WHERE proname = %s", (fn,)).fetchone()
        assert row["prosecdef"] is False  # SECURITY INVOKER: the root cause


# B..E. GREEN after the real migration 027 -------------------------------------------------------------------
def test_service_role_ordinary_insert_still_succeeds(pg):
    svc = pg.session()
    assert pg.deposit(svc, 0) is not None


def test_service_role_ledger_reversal_succeeds_and_is_a_single_append(pg):
    svc = pg.session()
    target = pg.deposit(svc, 0)
    rev = pg.reversal(svc, target, 5)
    row = pg.admin.execute(
        "SELECT reverses_transaction_id FROM public.portfolio_transactions WHERE id = %s", (rev,)
    ).fetchone()
    assert row["reverses_transaction_id"] == target
    assert pg.admin.execute("SELECT count(*) AS n FROM public.portfolio_transactions").fetchone()["n"] == 2


def test_service_role_allocation_and_attribution_reversal_succeed(pg):
    svc = pg.session()
    charge = pg.deposit(svc, 0, kind="fee", amount="5")
    target = pg.deposit(svc, 1)
    alloc = pg.allocation(svc, charge, target, 10)
    rev = pg.attribution_reversal(svc, alloc, 20)
    assert pg.admin.execute(f"SELECT count(*) AS n FROM {ATTR}").fetchone()["n"] == 2
    assert rev is not None


def test_phase14_semantics_survive_the_privilege_repair(pg):
    svc = pg.session()
    charge = pg.deposit(svc, 0, kind="fee", amount="5")
    target = pg.deposit(svc, 1)
    # Allocation capacity: cannot allocate more than the charge.
    with pytest.raises(psycopg.Error):
        pg.allocation(svc, charge, target, 10, amount="6")
    pg.allocation(svc, charge, target, 10, amount="5")
    # Cross-stream strict non-backdating: a ledger reversal of the target must be recorded AFTER the attribution.
    with pytest.raises(psycopg.errors.RaiseException) as err:
        pg.reversal(svc, target, 10)
    assert "Cross-stream PIT backdating violation" in str(err.value)
    pg.reversal(svc, target, 11)


# F..J, M. The write surface stays closed ---------------------------------------------------------------------
def test_table_privileges_of_migration_025_are_preserved_and_no_update_was_granted(pg):
    assert pg.has("service_role", LEDGER, "SELECT") and pg.has("service_role", LEDGER, "INSERT")
    for priv in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert not pg.has("service_role", LEDGER, priv), priv
    cols = pg.admin.execute(
        "SELECT count(*) AS n FROM information_schema.column_privileges"
        " WHERE table_schema = 'public' AND table_name = 'portfolio_transactions'"
        " AND grantee = 'service_role' AND privilege_type = 'UPDATE'"
    ).fetchone()["n"]
    assert cols == 0
    for role in ("anon", "authenticated"):
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not pg.has(role, LEDGER, priv), (role, priv)
    assert pg.has("authenticated", LEDGER, "SELECT")


def test_service_role_direct_update_delete_truncate_remain_denied(pg):
    svc = pg.session()
    tx = pg.deposit(svc, 0)
    for sql in (
        "UPDATE public.portfolio_transactions SET cash_amount = 1 WHERE id = %s",
        "DELETE FROM public.portfolio_transactions WHERE id = %s",
        "SELECT 1 FROM public.portfolio_transactions WHERE id = %s FOR UPDATE",
    ):
        with pytest.raises(pgerr.InsufficientPrivilege):
            svc.execute(sql, (tx,))
    with pytest.raises(pgerr.InsufficientPrivilege):
        svc.execute("TRUNCATE public.portfolio_transactions")
    with pytest.raises(pgerr.InsufficientPrivilege):
        svc.execute(f"TRUNCATE {ATTR}")


@pytest.mark.parametrize("role", ["authenticated", "anon"])
def test_non_service_roles_cannot_write_ledger_or_attribution(pg, role):
    svc = pg.session()
    charge = pg.deposit(svc, 0, kind="fee", amount="5")
    target = pg.deposit(svc, 1)
    low = pg.session(role)
    with pytest.raises(pgerr.InsufficientPrivilege):
        pg.deposit(low, 5)
    with pytest.raises(pgerr.InsufficientPrivilege):
        pg.reversal(low, target, 5)
    with pytest.raises(pgerr.InsufficientPrivilege):
        pg.allocation(low, charge, target, 5)
    alloc = pg.allocation(svc, charge, target, 10)
    with pytest.raises(pgerr.InsufficientPrivilege):
        pg.attribution_reversal(low, alloc, 20)


def test_repair_is_not_a_new_write_surface_for_callers(pg):
    for role in ("authenticated", "anon", "service_role"):
        low = pg.session(role)
        for fn in TRIGGER_FUNCTIONS:
            assert not pg.admin.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE') AS ok", (role, f"public.{fn}()")
            ).fetchone()["ok"], (role, fn)
            with pytest.raises(psycopg.Error):  # not callable as an RPC / function call by any API role
                low.execute(f"SELECT public.{fn}()")
    assert not pg.admin.execute(
        "SELECT has_function_privilege('public', 'public.lock_portfolio_transaction_reversal_target()', 'EXECUTE')"
        " AS ok"
    ).fetchone()["ok"]
    # No new SECURITY DEFINER function accepts arguments: only the two argument-less trigger functions changed.
    assert pg.admin.execute(
        "SELECT count(*) AS n FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'public' AND p.proname = ANY(%s) AND p.pronargs <> 0",
        (list(TRIGGER_FUNCTIONS),),
    ).fetchone()["n"] == 0


# K, L. Function definition ---------------------------------------------------------------------------------
def test_trigger_functions_are_security_definer_with_the_pinned_search_path(pg):
    for fn in TRIGGER_FUNCTIONS:
        row = pg.admin.execute(
            "SELECT p.prosecdef, p.proconfig, p.prorettype::regtype::text AS ret, pg_get_userbyid(p.proowner) AS owner,"
            " pg_get_functiondef(p.oid) AS body FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'public' AND p.proname = %s",
            (fn,),
        ).fetchone()
        assert row["prosecdef"] is True
        assert row["proconfig"] == ["search_path=pg_catalog, pg_temp"]
        assert row["ret"] == "trigger"
        assert row["owner"] not in ("anon", "authenticated", "service_role")
        assert "FOR UPDATE" in row["body"]  # the lock is not weakened to a plain read
        assert "EXECUTE '" not in row["body"] and "format(" not in row["body"]  # no dynamic SQL


def test_trigger_bindings_are_unchanged(pg):
    rows = pg.admin.execute(
        "SELECT t.tgname, c.relname, p.proname, t.tgenabled, pg_get_triggerdef(t.oid) AS def"
        " FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_proc p ON p.oid = t.tgfoid"
        " WHERE NOT t.tgisinternal AND p.proname = ANY(%s) ORDER BY t.tgname",
        (list(TRIGGER_FUNCTIONS),),
    ).fetchall()
    assert [(r["tgname"], r["relname"], r["proname"]) for r in rows] == [
        (
            "trg_lock_portfolio_transaction_reversal_target",
            "portfolio_transactions",
            "lock_portfolio_transaction_reversal_target",
        ),
        (
            "trg_validate_fee_tax_attribution_event_integrity",
            "portfolio_fee_tax_attribution_events",
            "validate_fee_tax_attribution_event_integrity",
        ),
    ]
    for r in rows:
        assert r["tgenabled"] == "O"
        assert "BEFORE INSERT" in r["def"] and "FOR EACH ROW" in r["def"]


# P. The Phase 14 lock domain survives ------------------------------------------------------------------------
def test_attribution_locks_the_ledger_rows_against_a_concurrent_reversal(pg):
    seed = pg.session()
    charge = pg.deposit(seed, 0, kind="fee", amount="5")
    target = pg.deposit(seed, 1)

    writer = pg.session(autocommit=False)  # attribution insert, transaction left open
    pg.allocation(writer, charge, target, 10)
    writer_pid = writer.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    assert writer_pid is not None

    # Independent connection: a ledger reversal of the locked target must wait on the writer's row lock.
    rival = pg.session(autocommit=False)
    with pytest.raises(pgerr.LockNotAvailable) as err:
        pg.reversal(rival, target, 11)
    assert err.value.sqlstate == LOCK_NOT_AVAILABLE
    rival.rollback()

    # While the writer is open, the row lock is visible through the real ledger row (not a weaker non-locking read).
    locked = pg.admin.execute(
        "SELECT l.mode FROM pg_locks l WHERE l.pid = %s AND l.relation = %s::regclass AND l.granted",
        (writer_pid, LEDGER),
    ).fetchall()
    assert any(r["mode"] == "RowShareLock" for r in locked)

    writer.commit()

    # After the attribution commits, a reversal recorded at or before it is rejected (cross-stream non-backdating)...
    late = pg.session()
    with pytest.raises(pgerr.RaiseException) as backdated:
        pg.reversal(late, target, 10)
    assert "Cross-stream PIT backdating violation" in str(backdated.value)
    # ...and a strictly later one serializes after it and succeeds.
    pg.reversal(late, target, 12)


def test_ledger_reversal_locks_the_target_against_a_concurrent_attribution(pg):
    seed = pg.session()
    charge = pg.deposit(seed, 0, kind="fee", amount="5")
    target = pg.deposit(seed, 1)

    reverser = pg.session(autocommit=False)
    pg.reversal(reverser, target, 5)  # row lock on target held until commit

    rival = pg.session(autocommit=False)
    with pytest.raises(pgerr.LockNotAvailable):
        pg.allocation(rival, charge, target, 10)
    rival.rollback()
    reverser.commit()

    # Once the reversal committed, the reversed target can no longer receive an active allocation.
    after = pg.session()
    with pytest.raises(psycopg.Error):
        pg.allocation(after, charge, target, 10)
