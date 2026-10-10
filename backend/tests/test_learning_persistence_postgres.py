"""Phase 28B-1A: real PostgreSQL verification of migration 030 (learning evidence bindings).

No mocks: the REAL repository migrations 001 through 030 are executed against a disposable database through the same fixture contract as the Phase 27 advisor test
(roles anon / authenticated / service_role with Supabase-style default grants). The Python verifier and repository are exercised against the real database through an
injected psycopg transport; nothing here touches the network or a provider.

Runs only when SENTINAX_TEST_POSTGRES_URL points at a disposable database; when it is set a missing driver or unreachable server FAILS (never skips).
"""
import threading
from datetime import date, datetime, timezone

import pytest

from backend.tests.test_supabase_security_advisor_postgres import BOOTSTRAP_SQL, MIGRATIONS, POSTGRES_URL, Env, prepared  # skips when the URL is absent

import psycopg  # noqa: E402  (unguarded: ImportError must FAIL when the URL is set)
from psycopg import errors as pgerr  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Json  # noqa: E402

from backend.engine.learning.adapters import source_authority_from_raw_snapshot, verify_evidence_binding  # noqa: E402
from backend.engine.learning.evidence_binding import EvidenceBindingDeclaration, EvidenceBindingError  # noqa: E402
from backend.engine.learning.source_authority import AvailabilityStatus, LicensingStatus, SourceAuthorityClass  # noqa: E402
from backend.engine.learning.temporal_provenance import TemporalProvenance  # noqa: E402
from backend.engine.learning_store.evidence_binding_repository import BindingDisposition, EvidenceBindingRepository  # noqa: E402
from backend.engine.private.storage_models import RawProviderSnapshotRecord, compute_payload_hash  # noqa: E402

T0 = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)
TABLE = "public.learning_evidence_bindings"
RPC = "public.record_learning_evidence_bindings"
PAYLOAD = {"funds": [{"code": "AAA", "price": "1.5"}]}
API = ("anon", "authenticated")


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


# --- helpers -----------------------------------------------------------------------------------------------------------------------------------
def seed_snapshot(pg, payload=None, *, provider="TEFAS", endpoint="/api/funds/fonFiyatBilgiGetir", retrieved_at=T0, payload_hash=None):
    """Insert a retained raw snapshot exactly as the existing PIT store would hold it (admin-seeded fixture; the learning layer never writes this table)."""
    payload = PAYLOAD if payload is None else payload
    rec = RawProviderSnapshotRecord.create(provider=provider, endpoint=endpoint, request_params={}, raw_payload=payload, retrieved_at=retrieved_at)
    pg.admin.execute(
        "INSERT INTO public.raw_provider_snapshots (id, provider, endpoint, request_params, retrieved_at, content_type, raw_payload, payload_hash)"
        " VALUES (%s, %s, %s, '{}'::jsonb, %s, 'application/json', %s, %s)",
        (rec.id, provider, endpoint, retrieved_at, Json(payload), payload_hash or rec.payload_hash),
    )
    if payload_hash:
        rec.payload_hash = payload_hash
    return rec


def declaration(rec, *, revision=1, economic_date=date(2026, 10, 9), source_id="tefas.fund-price", attempted=None, **over) -> EvidenceBindingDeclaration:
    src = source_authority_from_raw_snapshot(
        rec, source_id=source_id, source_reference="https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir",
        authority_class=SourceAuthorityClass.OFFICIAL_PUBLIC_PROVIDER_SURFACE, known_limitations=("undocumented endpoint",),
        availability=AvailabilityStatus.PUBLICLY_AVAILABLE, licensing=LicensingStatus.UNRESOLVED,
    ) if rec.payload_hash == compute_payload_hash(rec.raw_payload) else None
    if src is None:   # forged-hash record: build the authority without the (correctly refusing) adapter so the verifier path can be tested
        from backend.engine.learning.source_authority import SourceAuthorityRecord
        src = SourceAuthorityRecord(source_id=source_id, source_reference="r", document_version=None, retrieved_at=rec.retrieved_at, content_sha256=rec.payload_hash,
                                    authority_class=SourceAuthorityClass.UNCLASSIFIED, known_limitations=("forged",), availability=AvailabilityStatus.UNKNOWN,
                                    licensing=LicensingStatus.UNRESOLVED, raw_snapshot_id=rec.id)
    prov = TemporalProvenance(economic_date=economic_date, retrieved_at=rec.retrieved_at, capture_attempted_at=attempted or rec.retrieved_at)
    return EvidenceBindingDeclaration(source_authority=src, provenance=prov, expected_provider="TEFAS", expected_endpoint="/api/funds/fonFiyatBilgiGetir", revision=revision, **over)


class Transport:
    """psycopg-backed injected reader/rpc for one service_role connection."""

    def __init__(self, conn):
        self.conn = conn

    def reader(self, snapshot_id):
        row = self.conn.execute("SELECT * FROM public.raw_provider_snapshots WHERE id = %s", (snapshot_id,)).fetchone()
        if row is None:
            return None
        return RawProviderSnapshotRecord(
            provider=row["provider"], endpoint=row["endpoint"], request_params=row["request_params"], retrieved_at=row["retrieved_at"], content_type=row["content_type"],
            raw_payload=row["raw_payload"], payload_hash=row["payload_hash"], http_status=row["http_status"], storage_ref=row["storage_ref"], id=row["id"], created_at=row["created_at"],
        )

    def rpc(self, name, params):
        return self.conn.execute(f"SELECT * FROM public.{name}(%s::jsonb)", (Json(params["p_bindings"]),)).fetchall()


def repo(pg, conn=None):
    t = Transport(conn or pg.connect("service_role"))
    return EvidenceBindingRepository(snapshot_reader=t.reader, rpc=t.rpc)


def count(pg):
    return pg.scalar(f"SELECT count(*) FROM {TABLE}")


def rpc_json(pg, items, role="service_role"):
    return pg.connect(role).execute(f"SELECT * FROM {RPC}(%s::jsonb)", (Json(items),)).fetchall()


def verified_payload(rec, **kw):
    return verify_evidence_binding(declaration(rec, **kw), rec).to_rpc_dict()


# --- migration ran ----------------------------------------------------------------------------------------------------------------------------
def test_migration_030_exists_and_all_migrations_ran(pg):
    assert any(m.name.startswith("030_") for m in MIGRATIONS)
    assert pg.scalar("SELECT to_regclass(%s) IS NOT NULL", (TABLE,)) is True
    print(f"EVIDENCE postgres_server_version={pg.scalar('SHOW server_version')} migrations={len(MIGRATIONS)}")


# --- valid binding ------------------------------------------------------------------------------------------------------------------------------
def test_valid_binding_is_persisted_with_database_recorded_time_distinct_from_retrieval(pg):
    rec = seed_snapshot(pg, retrieved_at=datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc))           # an old retrieval instant, imported only now
    before = pg.scalar("SELECT clock_timestamp()")
    out = repo(pg).persist((declaration(rec, economic_date=date(2019, 12, 31)),))
    after = pg.scalar("SELECT clock_timestamp()")
    assert [o.disposition for o in out] == [BindingDisposition.INSERTED]
    row = pg.admin.execute(f"SELECT * FROM {TABLE}").fetchone()
    assert row["raw_snapshot_id"] == rec.id and row["snapshot_retrieved_at"] == rec.retrieved_at and row["economic_date"] == date(2019, 12, 31)
    assert before <= row["recorded_at"] <= after and row["recorded_at"] != row["snapshot_retrieved_at"]      # newly imported historical data stays newly imported
    assert row["publication_time"] is None and row["content_sha256"] == rec.payload_hash and row["revision"] == 1 and row["prev_revision"] is None


def test_verification_levels_and_authorization_columns_are_constant_and_unverified(pg):
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec),))
    row = pg.admin.execute(f"SELECT * FROM {TABLE}").fetchone()
    assert row["stored_content_integrity"] == "WRITER_REHASH_ASSERTED" and row["record_reference_existence"] == "VERIFIED_BY_DATABASE" and row["source_identity_binding"] == "VERIFIED_BY_DATABASE"
    for col in ("document_assertion_verification", "licensing_access_verification", "economic_completeness_verification", "publication_time_authority"):
        assert row[col] == "UNVERIFIED", col
    assert row["capture_authorized"] is False
    base = (
        f"INSERT INTO {TABLE} (raw_snapshot_id, source_id, revision, expected_provider, expected_endpoint, content_sha256, snapshot_retrieved_at, capture_attempted_at,"
        " authority_class, availability_declared, licensing_declared, known_limitations, source_reference, {col}) VALUES"
        " (%s, 'probe.source', 1, 'TEFAS', '/api/funds/fonFiyatBilgiGetir', %s, %s, %s, 'unclassified', 'unknown', 'unresolved', ARRAY['l'], 'r', %s)"
    )
    for col, bad in (("licensing_access_verification", "VERIFIED"), ("publication_time_authority", "VERIFIED"), ("document_assertion_verification", "VERIFIED"),
                     ("economic_completeness_verification", "VERIFIED"), ("capture_authorized", True)):
        with pytest.raises(pgerr.CheckViolation):
            pg.admin.execute(base.format(col=col), (rec.id, rec.payload_hash, rec.retrieved_at, rec.retrieved_at, bad))
    assert count(pg) == 1


# --- nonexistent / mismatch / forged ---------------------------------------------------------------------------------------------------
def test_nonexistent_snapshot_fails_in_python_and_in_the_database(pg):
    ghost = RawProviderSnapshotRecord.create(provider="TEFAS", endpoint="/api/funds/fonFiyatBilgiGetir", request_params={}, raw_payload=PAYLOAD, retrieved_at=T0)   # never seeded
    with pytest.raises(EvidenceBindingError):
        repo(pg).persist((declaration(ghost),))
    item = verify_evidence_binding(declaration(ghost), ghost).to_rpc_dict()                                 # bypass the Python guard and hit the database directly
    with pytest.raises(pgerr.ForeignKeyViolation):
        rpc_json(pg, [item])
    assert count(pg) == 0


def test_database_rejects_hash_provider_endpoint_and_retrieval_mismatches(pg):
    rec = seed_snapshot(pg)
    good = verified_payload(rec)
    for field, bad, error in (
        ("content_sha256", "a" * 64, pgerr.CheckViolation),
        ("expected_provider", "KAP", pgerr.CheckViolation),
        ("expected_endpoint", "/other", pgerr.CheckViolation),
        ("snapshot_retrieved_at", "2026-10-12T09:00:01+00:00", pgerr.CheckViolation),
    ):
        with pytest.raises(error):
            rpc_json(pg, [{**good, field: bad}])
    assert count(pg) == 0


def test_database_rejects_a_snapshot_that_retains_no_payload(pg):
    rec = RawProviderSnapshotRecord.create(provider="TEFAS", endpoint="/api/funds/fonFiyatBilgiGetir", request_params={}, raw_payload=PAYLOAD, retrieved_at=T0)
    pg.admin.execute(
        "INSERT INTO public.raw_provider_snapshots (id, provider, endpoint, request_params, retrieved_at, content_type, raw_payload, storage_ref, payload_hash)"
        " VALUES (%s, 'TEFAS', '/api/funds/fonFiyatBilgiGetir', '{}'::jsonb, %s, 'application/json', NULL, 's3://elsewhere/blob', %s)", (rec.id, T0, rec.payload_hash))
    item = verify_evidence_binding(declaration(rec), rec).to_rpc_dict()                                      # the in-memory record still has its payload; the stored row does not
    with pytest.raises(pgerr.CheckViolation, match="retains no payload"):
        rpc_json(pg, [item])
    assert count(pg) == 0


def test_forged_valid_looking_hash_is_stopped_by_the_python_verifier_and_the_db_trust_boundary_is_explicit(pg):
    forged = "f" * 64
    rec = seed_snapshot(pg, payload_hash=forged)                                                            # the raw table itself accepts any well-formed hash
    with pytest.raises(EvidenceBindingError):
        repo(pg).persist((declaration(rec),))
    assert count(pg) == 0
    # The database cannot recompute Python's canonical payload hash: a trusted service_role writer that skips the verifier is only checked against the stored column.
    item = {**verified_payload(seed_snapshot(pg)), "raw_snapshot_id": str(rec.id), "content_sha256": forged,
            "snapshot_retrieved_at": rec.retrieved_at.isoformat()}
    rpc_json(pg, [item])
    assert pg.scalar(f"SELECT stored_content_integrity FROM {TABLE}") == "WRITER_REHASH_ASSERTED"             # asserted by the writer, never presented as database-verified


# --- idempotency, conflicts, revisions -----------------------------------------------------------------------------------------------
def test_resubmission_is_idempotent_and_keeps_the_original_row(pg):
    rec = seed_snapshot(pg)
    first = repo(pg).persist((declaration(rec),))
    original = pg.admin.execute(f"SELECT * FROM {TABLE}").fetchone()
    second = repo(pg).persist((declaration(rec),))
    assert first[0].disposition is BindingDisposition.INSERTED and second[0].disposition is BindingDisposition.EXISTING
    assert count(pg) == 1 and pg.admin.execute(f"SELECT * FROM {TABLE}").fetchone() == original


def test_same_identity_with_conflicting_content_is_rejected_and_nothing_changes(pg):
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec),))
    original = pg.admin.execute(f"SELECT * FROM {TABLE}").fetchone()
    with pytest.raises(pgerr.UniqueViolation):
        repo(pg).persist((declaration(rec, economic_date=date(2026, 10, 8)),))
    with pytest.raises(pgerr.UniqueViolation):
        repo(pg).persist((declaration(rec, attempted=datetime(2026, 10, 12, 8, 59, tzinfo=timezone.utc)),))
    assert count(pg) == 1 and pg.admin.execute(f"SELECT * FROM {TABLE}").fetchone() == original


def test_revisions_are_distinct_records_linked_by_a_contiguous_chain(pg):
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec),))
    repo(pg).persist((declaration(rec, revision=2, economic_date=date(2026, 10, 8)),))
    rows = pg.admin.execute(f"SELECT revision, prev_revision, economic_date FROM {TABLE} ORDER BY revision").fetchall()
    assert [(r["revision"], r["prev_revision"]) for r in rows] == [(1, None), (2, 1)] and rows[0]["economic_date"] == date(2026, 10, 9)
    with pytest.raises(pgerr.ForeignKeyViolation):                                                          # no gap: revision 4 without revision 3
        repo(pg).persist((declaration(rec, revision=4),))
    assert count(pg) == 2


def test_different_sources_may_bind_the_same_snapshot_independently(pg):
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec), declaration(rec, source_id="spk.derived-view")))
    assert count(pg) == 2


def test_concurrent_identical_submissions_yield_one_canonical_record(pg):
    rec = seed_snapshot(pg)
    payload = verified_payload(rec)
    attempts, outcomes, errors = 12, [], []
    barrier = threading.Barrier(attempts)

    def worker():
        conn = None
        try:
            conn = psycopg.connect(POSTGRES_URL, autocommit=True, row_factory=dict_row)
            conn.execute("SET ROLE service_role")
            barrier.wait(timeout=30)
            outcomes.append(conn.execute(f"SELECT * FROM {RPC}(%s::jsonb)", (Json([payload]),)).fetchone()["o_disposition"])
        except Exception as error:
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
    assert sorted(outcomes) == ["EXISTING"] * (attempts - 1) + ["INSERTED"] and count(pg) == 1


def test_failed_multi_record_write_rolls_back_atomically(pg):
    rec = seed_snapshot(pg)
    good = verified_payload(rec)
    with pytest.raises(pgerr.ForeignKeyViolation):
        rpc_json(pg, [good, {**good, "raw_snapshot_id": "00000000-0000-4000-8000-0000000000ff", "source_id": "other.source"}])
    assert count(pg) == 0
    conn = pg.connect("service_role", autocommit=False)
    conn.execute(f"SELECT * FROM {RPC}(%s::jsonb)", (Json([good]),))
    conn.rollback()
    assert count(pg) == 0


def test_rpc_validates_its_input_shape(pg):
    rec = seed_snapshot(pg)
    good = verified_payload(rec)
    for bad in (None, {}, [], [1], [{**good, "unexpected": 1}], [{**good, "capture_authorized": True}], [{**good, "recorded_at": "2020-01-01T00:00:00+00:00"}],
                [{k: v for k, v in good.items() if k != "content_sha256"}], [{**good, "revision": 0}], [{**good, "source_id": "Bad Id"}], [{**good, "content_sha256": "A" * 64}]):
        with pytest.raises((pgerr.InvalidParameterValue, pgerr.CheckViolation, pgerr.RaiseException, pgerr.NotNullViolation, pgerr.InvalidTextRepresentation,
                            pgerr.UndefinedColumn, pgerr.NullValueNotAllowed)):
            rpc_json(pg, bad)
    assert count(pg) == 0


def test_client_supplied_recorded_time_cannot_be_forged_on_direct_insert(pg):
    rec = seed_snapshot(pg)
    g = verified_payload(rec)
    svc = pg.connect("service_role")
    svc.execute(
        f"INSERT INTO {TABLE} (raw_snapshot_id, source_id, revision, expected_provider, expected_endpoint, content_sha256, snapshot_retrieved_at, capture_attempted_at,"
        " authority_class, availability_declared, licensing_declared, known_limitations, source_reference, recorded_at)"
        " VALUES (%s, %s, 1, 'TEFAS', '/api/funds/fonFiyatBilgiGetir', %s, %s, %s, 'official_public_provider_surface', 'publicly_available', 'unresolved', ARRAY['l'], 'r', '2001-01-01T00:00:00Z')",
        (rec.id, g["source_id"], rec.payload_hash, rec.retrieved_at, rec.retrieved_at),
    )
    assert pg.scalar(f"SELECT recorded_at > TIMESTAMPTZ '2020-01-01' FROM {TABLE}") is True


# --- append-only, privileges, RLS ------------------------------------------------------------------------------------------------------------
def test_update_delete_truncate_are_denied_for_every_role_including_a_superuser(pg):
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec),))
    for role in (None, "service_role", "anon", "authenticated"):
        conn = pg.connect(role) if role else pg.admin
        errors = (pgerr.RestrictViolation,) if role is None else (pgerr.InsufficientPrivilege,)
        for sql in (f"UPDATE {TABLE} SET source_reference = 'x'", f"DELETE FROM {TABLE}", f"TRUNCATE {TABLE}"):
            with pytest.raises(errors):
                conn.execute(sql)
    assert count(pg) == 1


def test_table_privileges_are_least_privilege(pg):
    for role in ("public", "anon", "authenticated"):
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not pg.scalar("SELECT has_table_privilege(%s, %s, %s)", (role, TABLE, priv)), (role, priv)
    for priv in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert not pg.scalar("SELECT has_table_privilege('service_role', %s, %s)", (TABLE, priv)), priv
    assert all(pg.scalar("SELECT has_table_privilege('service_role', %s, %s)", (TABLE, p)) for p in ("SELECT", "INSERT"))


def test_rls_enabled_with_no_permissive_policy_and_api_roles_have_no_access(pg):
    assert pg.scalar("SELECT relrowsecurity FROM pg_class WHERE oid = %s::regclass", (TABLE,)) is True
    assert pg.scalar("SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = 'learning_evidence_bindings'") == 0
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec),))
    for role in API:
        conn = pg.connect(role)
        with pytest.raises(pgerr.InsufficientPrivilege):
            conn.execute(f"SELECT * FROM {TABLE}")
        with pytest.raises(pgerr.InsufficientPrivilege):
            conn.execute(f"INSERT INTO {TABLE} (raw_snapshot_id) VALUES (gen_random_uuid())")
        with pytest.raises(pgerr.InsufficientPrivilege):
            conn.execute(f"SELECT * FROM {RPC}(%s::jsonb)", (Json([verified_payload(rec)]),))


def test_rpc_privileges_and_function_properties(pg):
    sig = "public.record_learning_evidence_bindings(jsonb)"
    for role in ("public", "anon", "authenticated"):
        assert not pg.scalar("SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')", (role, sig)), role
    assert pg.scalar("SELECT has_function_privilege('service_role', %s::regprocedure, 'EXECUTE')", (sig,))
    rows = pg.admin.execute(
        "SELECT proname, prosecdef, proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND proname IN"
        " ('record_learning_evidence_bindings', 'enforce_learning_evidence_binding_insert', 'reject_learning_evidence_binding_mutation', 'guard_learning_bound_raw_snapshot')"
    ).fetchall()
    assert len(rows) == 4
    for r in rows:
        assert r["prosecdef"] is False and r["proconfig"] == ["search_path=pg_catalog, public, pg_temp"], r["proname"]
    for fn in ("enforce_learning_evidence_binding_insert()", "reject_learning_evidence_binding_mutation()", "guard_learning_bound_raw_snapshot()"):
        for role in ("public", "anon", "authenticated", "service_role"):
            assert not pg.scalar("SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')", (role, f"public.{fn}")), (role, fn)


# --- the existing raw store: audit-driven guarantees -----------------------------------------------------------------------------------
def test_audit_existing_raw_store_tamper_triggers_and_hash_validation_gap(pg):
    """Documents pre-existing facts the design must not assume away. The legacy TRUNCATE grant that this audit originally recorded was removed by migration 031 (post-28B-1A
    hardening H1); its historical existence through migration 030 is asserted in test_legacy_pit_truncate_hardening_postgres.py, not here."""
    for role in ("anon", "authenticated", "service_role"):
        assert not pg.scalar("SELECT has_table_privilege(%s, 'public.raw_provider_snapshots', 'TRUNCATE')", (role,)), role
    names = {r["tgname"] for r in pg.admin.execute("SELECT tgname FROM pg_trigger WHERE tgrelid = 'public.raw_provider_snapshots'::regclass AND NOT tgisinternal").fetchall()}
    assert {"trg_protect_raw_snapshot_immutability", "trg_supersede_raw_snapshot"} <= names
    seed_snapshot(pg, payload_hash="f" * 64)                                                                # the raw table does not validate that payload_hash matches the payload


def test_bound_snapshot_is_protected_even_if_the_original_tamper_trigger_is_absent(pg):
    """Live-history drift: migration 028 once found prevent_raw_snapshot_tamper() absent. The binding layer must not depend on it."""
    rec = seed_snapshot(pg)
    free = seed_snapshot(pg, payload={"other": 1})
    repo(pg).persist((declaration(rec),))
    pg.admin.execute("DROP FUNCTION public.prevent_raw_snapshot_tamper() CASCADE")
    svc = pg.connect("service_role")
    svc.execute("UPDATE public.raw_provider_snapshots SET raw_payload = '{\"other\": 2}'::jsonb WHERE id = %s", (free.id,))           # unbound row: drift makes it mutable (pre-existing gap)
    for sql in ("UPDATE public.raw_provider_snapshots SET raw_payload = '{\"x\": 1}'::jsonb WHERE id = %s",
                "UPDATE public.raw_provider_snapshots SET payload_hash = repeat('0', 64) WHERE id = %s",
                "UPDATE public.raw_provider_snapshots SET retrieved_at = now() WHERE id = %s"):
        with pytest.raises(pgerr.RestrictViolation):
            svc.execute(sql, (rec.id,))
    with pytest.raises((pgerr.RestrictViolation, pgerr.ForeignKeyViolation)):
        svc.execute("DELETE FROM public.raw_provider_snapshots WHERE id = %s", (rec.id,))
    svc.execute("UPDATE public.raw_provider_snapshots SET is_superseded = true, superseded_at = now() WHERE id = %s", (rec.id,))          # system supersession stays allowed
    assert pg.scalar("SELECT payload_hash FROM public.raw_provider_snapshots WHERE id = %s", (rec.id,)) == rec.payload_hash


def test_truncate_cascade_of_the_raw_store_is_blocked_for_service_role_once_bindings_exist(pg):
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec),))
    svc = pg.connect("service_role")
    with pytest.raises(pgerr.InsufficientPrivilege):
        svc.execute("TRUNCATE public.raw_provider_snapshots CASCADE")
    assert count(pg) == 1 and pg.scalar("SELECT count(*) FROM public.raw_provider_snapshots WHERE id = %s", (rec.id,)) == 1


def test_no_orphan_bindings_can_exist_and_deleting_the_referenced_snapshot_is_restricted(pg):
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec),))
    assert pg.scalar(f"SELECT count(*) FROM {TABLE} b LEFT JOIN public.raw_provider_snapshots s ON s.id = b.raw_snapshot_id WHERE s.id IS NULL") == 0
    with pytest.raises((pgerr.RestrictViolation, pgerr.ForeignKeyViolation)):
        pg.admin.execute("DELETE FROM public.raw_provider_snapshots WHERE id = %s", (rec.id,))


def test_the_learning_layer_stores_no_universe_or_observation_claims_and_copies_no_payloads(pg):
    cols = {r["column_name"] for r in pg.admin.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'learning_evidence_bindings'").fetchall()}
    assert "raw_payload" not in cols and not any("member" in c or "observed_unit_price" in c or "coverage" in c for c in cols)
    tables = {r["tablename"] for r in pg.admin.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'learning%%'").fetchall()}
    assert tables == {"learning_evidence_bindings"}


def test_migration_030_is_rerunnable_without_losing_rows_or_weakening_protection(pg):
    rec = seed_snapshot(pg)
    repo(pg).persist((declaration(rec),))
    sql = prepared(next(m for m in MIGRATIONS if m.name.startswith("030_")), True, True)
    pg.admin.execute(sql)
    pg.admin.execute(sql)
    assert count(pg) == 1
    with pytest.raises(pgerr.RestrictViolation):
        pg.admin.execute(f"DELETE FROM {TABLE}")
    assert pg.scalar("SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal", (TABLE,)) == 3          # insert integrity, update/delete guard, truncate guard
