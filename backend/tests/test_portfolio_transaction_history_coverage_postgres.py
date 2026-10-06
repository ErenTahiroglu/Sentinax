"""Phase 26C2B2B3: real PostgreSQL concurrent-writer verification of migrations 025 + 026.

No mocks: independent PostgreSQL connections prove, with transaction state and lock_timeout (not sleeps),
that the SHARE lock of public.get_portfolio_transaction_history_coverage_snapshot conflicts in both
directions with a writer's ROW EXCLUSIVE lock. The real repository SQL of migrations 025 and 026 is
executed unmodified against a minimal fixture (see docs/PORTFOLIO_TRANSACTION_HISTORY_COVERAGE_CONCURRENCY.md).

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
MIGRATION_025 = MIGRATIONS / "025_portfolio_transaction_write_surface_hardening.sql"
MIGRATION_026 = MIGRATIONS / "026_portfolio_transaction_history_coverage_snapshot.sql"

LOCK_NOT_AVAILABLE = "55P03"
INSUFFICIENT_PRIVILEGE = "42501"
LOCK_TIMEOUT = "400ms"
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
CUTOFF = dt.datetime(2024, 6, 30, 12, 0, 0, 999999, tzinfo=dt.timezone.utc)
TABLE = "public.portfolio_transactions"

COVERAGE_SQL = (
    "SELECT * FROM public.get_portfolio_transaction_history_coverage_snapshot(%s, %s, %s)"
)

# Minimum prerequisite fixture only. It does NOT prove migration 011's invariants.
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

CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT NULL::uuid $$;

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;

CREATE TABLE public.portfolios (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL,
    CONSTRAINT uq_portfolios_id_owner UNIQUE (id, owner_id)
);

CREATE TABLE public.portfolio_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portfolio_id UUID NOT NULL,
    owner_id UUID NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    economic_fingerprint TEXT NOT NULL,
    CONSTRAINT fk_portfolio_transactions_portfolio
        FOREIGN KEY (portfolio_id, owner_id) REFERENCES public.portfolios (id, owner_id)
);

-- Supabase-style default privileges that migration 025 must tighten.
GRANT ALL ON public.portfolios TO anon, authenticated, service_role;
GRANT ALL ON public.portfolio_transactions TO anon, authenticated, service_role;

ALTER TABLE public.portfolio_transactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can select own portfolio transactions"
    ON public.portfolio_transactions FOR SELECT TO authenticated
    USING ((SELECT auth.uid()) = owner_id);
CREATE POLICY "Users can insert own portfolio transactions"
    ON public.portfolio_transactions FOR INSERT TO authenticated
    WITH CHECK ((SELECT auth.uid()) = owner_id);
"""


def fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def epoch_micros(value: dt.datetime) -> int:
    return (value - EPOCH) // dt.timedelta(microseconds=1)


class Env:
    def __init__(self):
        self.conns = []
        self.admin = psycopg.connect(POSTGRES_URL, autocommit=True, row_factory=dict_row)
        self.conns.append(self.admin)

    def session(self, role: str = "service_role", lock_timeout: str = LOCK_TIMEOUT):
        """Independent connection; role and lock_timeout are session-level, set before the transaction opens."""
        conn = psycopg.connect(POSTGRES_URL, autocommit=True, row_factory=dict_row)
        conn.execute(f"SET ROLE {role}")
        conn.execute(f"SET lock_timeout = '{lock_timeout}'")
        conn.autocommit = False
        self.conns.append(conn)
        return conn

    def portfolio(self, owner=None):
        owner = owner or uuid.uuid4()
        pid = uuid.uuid4()
        self.admin.execute("INSERT INTO public.portfolios (id, owner_id) VALUES (%s, %s)", (pid, owner))
        return owner, pid

    @staticmethod
    def insert_tx(conn, owner, pid, recorded_at, seed=None, tx_id=None):
        tx_id = tx_id or uuid.uuid4()
        conn.execute(
            "INSERT INTO public.portfolio_transactions (id, portfolio_id, owner_id, recorded_at, economic_fingerprint)"
            " VALUES (%s, %s, %s, %s, %s)",
            (tx_id, pid, owner, recorded_at, fingerprint(seed or str(tx_id))),
        )
        return tx_id

    @staticmethod
    def coverage(conn, owner, pid, cutoff=CUTOFF):
        return conn.execute(COVERAGE_SQL, (owner, pid, cutoff)).fetchone()

    def lock_modes(self, backend_pid):
        rows = self.admin.execute(
            "SELECT mode, granted FROM pg_locks WHERE relation = %s::regclass AND pid = %s",
            (TABLE, backend_pid),
        ).fetchall()
        return {r["mode"]: r["granted"] for r in rows}

    def close(self):
        for conn in self.conns:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()


@pytest.fixture
def pg():
    env = Env()
    try:
        # Fail-closed isolation guard: only a disposable, non-Supabase database may be reset.
        db = env.admin.execute("SELECT current_database() AS db").fetchone()["db"]
        assert db.startswith("sentinax_") and "concurrency" in db, f"refusing to reset non-test database {db!r}"
        assert env.admin.execute("SELECT to_regclass('auth.users') AS r").fetchone()["r"] is None
        env.admin.execute(BOOTSTRAP_SQL)
        env.admin.execute(MIGRATION_025.read_text(encoding="utf-8"))
        env.admin.execute(MIGRATION_026.read_text(encoding="utf-8"))
        yield env
    finally:
        env.close()


def test_server_version_and_real_migrations_executed(pg):
    major = int(pg.admin.execute("SHOW server_version_num").fetchone()["server_version_num"]) // 10000
    version = pg.admin.execute("SHOW server_version").fetchone()["server_version"]
    print(f"EVIDENCE postgres_server_version={version} major={major}")
    assert major >= 16
    fn = pg.admin.execute(
        "SELECT p.prosecdef AS secdef, pg_get_functiondef(p.oid) AS body FROM pg_proc p"
        " WHERE p.proname = 'get_portfolio_transaction_history_coverage_snapshot'"
    ).fetchone()
    assert fn["secdef"] is True
    assert "LOCK TABLE public.portfolio_transactions IN SHARE MODE" in fn["body"]
    assert pg.admin.execute(
        "SELECT count(*) AS n FROM pg_policies WHERE tablename = 'portfolio_transactions' AND cmd = 'INSERT'"
    ).fetchone()["n"] == 0


def test_runtime_privileges_after_migration_025(pg):
    def has(role, priv):
        return pg.admin.execute(
            "SELECT has_table_privilege(%s, %s, %s) AS ok", (role, TABLE, priv)
        ).fetchone()["ok"]

    assert has("service_role", "SELECT") and has("service_role", "INSERT")
    for priv in ("UPDATE", "DELETE", "TRUNCATE"):
        assert not has("service_role", priv)
    assert has("authenticated", "SELECT") and not has("authenticated", "INSERT")
    assert not has("anon", "SELECT") and not has("anon", "INSERT")

    owner, pid = pg.portfolio()
    writer = pg.session("service_role")
    tx_id = pg.insert_tx(writer, owner, pid, CUTOFF - dt.timedelta(days=1))
    writer.commit()
    assert pg.admin.execute("SELECT count(*) AS n FROM public.portfolio_transactions WHERE id = %s", (tx_id,)).fetchone()["n"] == 1

    auth_conn = pg.session("authenticated")
    with pytest.raises(pgerr.InsufficientPrivilege) as exc:
        pg.insert_tx(auth_conn, owner, pid, CUTOFF - dt.timedelta(days=1))
    assert exc.value.sqlstate == INSUFFICIENT_PRIVILEGE
    print(f"EVIDENCE authenticated_insert_denied sqlstate={exc.value.sqlstate}")


def test_scenario_a_writer_first_blocks_snapshot_until_commit(pg):
    owner, pid = pg.portfolio()
    w, r = pg.session(), pg.session()
    tx_id = pg.insert_tx(w, owner, pid, CUTOFF - dt.timedelta(hours=1))  # open, uncommitted

    assert pg.lock_modes(w.info.backend_pid).get("RowExclusiveLock") is True

    with pytest.raises(pgerr.LockNotAvailable) as exc:
        pg.coverage(r, owner, pid)
    assert exc.value.sqlstate == LOCK_NOT_AVAILABLE
    print(f"EVIDENCE writer_first_snapshot_blocked sqlstate={exc.value.sqlstate}")
    r.rollback()
    w.commit()

    row = pg.coverage(r, owner, pid)
    r.commit()
    assert row["transaction_count"] == 1
    assert row["transaction_ids"] == [tx_id]
    print("EVIDENCE writer_first_post_commit_manifest_contains_row=True")


def test_scenario_b_snapshot_first_blocks_new_writer_until_release(pg):
    owner, pid = pg.portfolio()
    r, w = pg.session(), pg.session()
    row = pg.coverage(r, owner, pid)  # transaction stays open: SHARE lock retained
    assert row["transaction_count"] == 0

    assert pg.lock_modes(r.info.backend_pid).get("ShareLock") is True

    with pytest.raises(pgerr.LockNotAvailable) as exc:
        pg.insert_tx(w, owner, pid, CUTOFF - dt.timedelta(hours=1))
    assert exc.value.sqlstate == LOCK_NOT_AVAILABLE
    print(f"EVIDENCE snapshot_first_writer_blocked sqlstate={exc.value.sqlstate}")
    w.rollback()
    r.commit()

    tx_id = pg.insert_tx(w, owner, pid, CUTOFF - dt.timedelta(hours=1))
    w.commit()
    assert pg.admin.execute("SELECT count(*) AS n FROM public.portfolio_transactions WHERE id = %s", (tx_id,)).fetchone()["n"] == 1
    print("EVIDENCE snapshot_first_post_release_insert=succeeded")


def test_scenario_c_snapshot_is_not_permanent_finality(pg):
    owner, pid = pg.portfolio()
    early = pg.session()
    first = pg.insert_tx(early, owner, pid, CUTOFF - dt.timedelta(days=2))
    early.commit()

    s1_conn = pg.session()
    s1 = pg.coverage(s1_conn, owner, pid)
    s1_conn.commit()
    assert s1["transaction_ids"] == [first]

    late = pg.session()
    backdated = pg.insert_tx(late, owner, pid, CUTOFF - dt.timedelta(days=1))  # recorded_at <= same cutoff
    late.commit()

    s2_conn = pg.session()
    s2 = pg.coverage(s2_conn, owner, pid, s1["as_of_recorded_at"])
    s2_conn.commit()

    assert s1["transaction_ids"] == [first] and s1["transaction_count"] == 1  # old manifest unchanged
    assert s2["transaction_ids"] == [first, backdated] and s2["transaction_count"] == 2
    assert s2["as_of_recorded_at"] == s1["as_of_recorded_at"]
    assert s2["observed_at"] > s1["observed_at"]
    print("EVIDENCE post_proof_backdated_write_visible_in_second_snapshot=True")


def test_scenario_d_rolled_back_writer_not_in_manifest(pg):
    owner, pid = pg.portfolio()
    w, r = pg.session(), pg.session()
    ghost = pg.insert_tx(w, owner, pid, CUTOFF - dt.timedelta(hours=1))
    with pytest.raises(pgerr.LockNotAvailable) as exc:
        pg.coverage(r, owner, pid)
    assert exc.value.sqlstate == LOCK_NOT_AVAILABLE
    r.rollback()
    w.rollback()

    row = pg.coverage(r, owner, pid)
    r.commit()
    assert ghost not in row["transaction_ids"]
    assert row["transaction_count"] == 0
    print("EVIDENCE rollback_visibility_rolled_back_row_absent=True")


def test_manifest_alignment_order_and_exact_microseconds(pg):
    owner, pid = pg.portfolio()
    w = pg.session()
    t_late = dt.datetime(2024, 3, 1, 10, 0, 0, 123457, tzinfo=dt.timezone.utc)
    t_tie = dt.datetime(2024, 3, 1, 10, 0, 0, 123456, tzinfo=dt.timezone.utc)
    t_first = dt.datetime(2024, 1, 15, 8, 30, 15, 1, tzinfo=dt.timezone.utc)
    id_lo = uuid.UUID("00000000-0000-4000-8000-000000000001")
    id_hi = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    id_late = uuid.uuid4()
    id_first = uuid.uuid4()
    # inserted deliberately out of canonical order
    pg.insert_tx(w, owner, pid, t_late, "late", id_late)
    pg.insert_tx(w, owner, pid, t_tie, "tie-hi", id_hi)
    pg.insert_tx(w, owner, pid, t_first, "first", id_first)
    pg.insert_tx(w, owner, pid, t_tie, "tie-lo", id_lo)
    pg.insert_tx(w, owner, pid, CUTOFF + dt.timedelta(seconds=1), "after-cutoff")  # excluded: recorded_at > cutoff
    w.commit()

    r = pg.session()
    row = pg.coverage(r, owner, pid)
    r.commit()

    expected = [
        (id_first, t_first, "first"),
        (id_lo, t_tie, "tie-lo"),
        (id_hi, t_tie, "tie-hi"),
        (id_late, t_late, "late"),
    ]
    assert row["transaction_count"] == 4
    assert row["transaction_ids"] == [e[0] for e in expected]
    assert row["recorded_at_epoch_micros"] == [epoch_micros(e[1]) for e in expected]
    assert row["economic_fingerprints"] == [fingerprint(e[2]) for e in expected]
    assert row["owner_id"] == owner and row["portfolio_id"] == pid
    assert row["as_of_recorded_at"] == CUTOFF


def test_empty_history_returns_exactly_one_row_with_empty_arrays(pg):
    owner, pid = pg.portfolio()
    r = pg.session()
    rows = r.execute(COVERAGE_SQL, (owner, pid, CUTOFF)).fetchall()
    r.commit()
    assert len(rows) == 1
    row = rows[0]
    assert row["transaction_count"] == 0
    assert row["transaction_ids"] == []
    assert row["recorded_at_epoch_micros"] == []
    assert row["economic_fingerprints"] == []
    print("EVIDENCE empty_history=one_row_empty_arrays")


def test_future_cutoff_fails_closed(pg):
    owner, pid = pg.portfolio()
    r = pg.session()
    future = r.execute("SELECT clock_timestamp() + interval '1 day' AS t").fetchone()["t"]
    with pytest.raises(psycopg.errors.RaiseException) as exc:
        pg.coverage(r, owner, pid, future)
    print(f"EVIDENCE future_cutoff_rejected sqlstate={exc.value.sqlstate}")
    r.rollback()


def test_owner_and_portfolio_isolation(pg):
    owner_a, pid_a1 = pg.portfolio()
    _, pid_a2 = pg.portfolio(owner_a)
    owner_b, pid_b = pg.portfolio()
    w = pg.session()
    t = CUTOFF - dt.timedelta(days=3)
    tx_a1 = pg.insert_tx(w, owner_a, pid_a1, t)
    tx_a2 = pg.insert_tx(w, owner_a, pid_a2, t)
    tx_b = pg.insert_tx(w, owner_b, pid_b, t)
    w.commit()

    r = pg.session()
    assert pg.coverage(r, owner_a, pid_a1)["transaction_ids"] == [tx_a1]
    r.commit()
    assert pg.coverage(r, owner_a, pid_a2)["transaction_ids"] == [tx_a2]
    r.commit()
    assert pg.coverage(r, owner_b, pid_b)["transaction_ids"] == [tx_b]
    r.commit()
    # wrong owner for a portfolio fails closed rather than leaking another owner's rows
    with pytest.raises(psycopg.errors.RaiseException):
        pg.coverage(r, owner_b, pid_a1)
    r.rollback()
    print("EVIDENCE owner_portfolio_isolation=True")
