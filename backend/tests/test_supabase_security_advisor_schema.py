"""
backend/tests/test_supabase_security_advisor_schema.py
======================================================
Phase 27 FIX D: SQL-text and source-contract tests for migration 028 and the `analyze` Edge Function. Supplemental: the authoritative privilege / RLS / atomicity regression is the real
PostgreSQL test_supabase_security_advisor_postgres.py. Repository tests do not prove the LIVE Supabase project: the Security Advisor must be re-run after deployment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
M028 = MIGRATIONS / "028_supabase_security_advisor_hardening.sql"
EDGE = ROOT / "supabase" / "functions" / "analyze" / "index.ts"
DOC = ROOT / "docs" / "PHASE_27_SUPABASE_SECURITY_ADVISOR_CLOSURE.md"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def code(path: Path) -> str:
    return re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))


def flat(path: Path) -> str:
    return re.sub(r"\s+", " ", code(path))


@pytest.fixture(scope="module")
def sql() -> str:
    assert M028.is_file(), "migration 028 is missing"
    return flat(M028)


# --- migration 028 ---------------------------------------------------------------------------------------------------------------------------------------------
def test_rate_limits_rls_is_enabled_with_no_policy_and_no_client_grants(sql):
    assert "ALTER TABLE public.rate_limits ENABLE ROW LEVEL SECURITY;" in sql
    for role in ("PUBLIC", "anon", "authenticated", "service_role"):
        assert f"REVOKE ALL PRIVILEGES ON TABLE public.rate_limits FROM {role};" in sql
    assert "CREATE POLICY" not in sql and not re.search(r"GRANT [^;]* ON TABLE public\.rate_limits", sql)


def test_consume_rate_limit_has_only_the_identifier_parameter_and_a_fixed_policy(sql):
    assert "DROP FUNCTION IF EXISTS public.consume_rate_limit(TEXT, INTEGER, DOUBLE PRECISION);" in sql
    signature = re.search(r"CREATE OR REPLACE FUNCTION public\.consume_rate_limit\((.*?)\)\s*RETURNS", sql).group(1)
    assert signature.strip() == "p_identifier TEXT"
    assert "c_capacity CONSTANT DOUBLE PRECISION := 15;" in sql and "c_refill_rate CONSTANT DOUBLE PRECISION := 0.25;" in sql
    assert "ON CONFLICT (identifier) DO NOTHING" in sql and "FOR UPDATE" in sql
    assert "p_capacity" not in sql and "p_refill_rate" not in sql


def test_every_security_definer_function_in_028_pins_the_strict_search_path_and_qualifies_relations(sql):
    blocks = re.findall(r"CREATE OR REPLACE FUNCTION (public\.\w+)\(.*?\$\$ .*?\$\$;", sql)
    assert set(blocks) == {"public.consume_rate_limit", "public.get_user_api_key_for_service"}
    for name in blocks:
        header = re.search(rf"CREATE OR REPLACE FUNCTION {re.escape(name)}\(.*?AS \$\$", sql).group(0)
        assert "SECURITY DEFINER" in header and "SET search_path = pg_catalog, pg_temp" in header
    body = " ".join(re.findall(r"AS \$\$(.*?)\$\$;", sql))
    for match in re.finditer(r"\b(?:FROM|JOIN|INTO|UPDATE) ([a-z_][a-z0-9_.]*)", body):
        ref = match.group(1)
        assert ref.startswith(("public.", "vault.", "v_")), ref
    assert "EXECUTE '" not in body and "format(" not in body.lower()


def test_execute_is_revoked_from_public_and_api_roles_and_only_service_role_or_reviewed_roles_are_granted(sql):
    grants = re.findall(r"GRANT (.*?) ON (.*?) TO ([^;]*);", sql)
    allowed = {
        "public.consume_rate_limit(TEXT)": "service_role",
        "public.get_user_api_key_for_service(UUID)": "service_role",
        "public.get_pit_macro_observation(VARCHAR, DATE, TIMESTAMPTZ, VARCHAR)": "authenticated, service_role",
    }
    seen = {}
    for privilege, target, roles in grants:
        assert privilege == "EXECUTE" and target.startswith("FUNCTION "), (privilege, target)
        seen[target.replace("FUNCTION ", "")] = roles
    assert seen == allowed
    for signature in ("public.consume_rate_limit(TEXT)", "public.get_user_api_key_for_service(UUID)", "public.upsert_user_api_key(TEXT)", "public.check_user_has_api_key()"):
        for role in ("PUBLIC", "anon", "authenticated"):
            assert f"REVOKE ALL ON FUNCTION {signature} FROM {role};" in sql, (signature, role)
    assert "anon" not in seen.get("public.get_pit_macro_observation(VARCHAR, DATE, TIMESTAMPTZ, VARCHAR)", "")


def test_legacy_caller_scoped_decryption_rpc_is_dropped_and_dormant_helpers_lose_every_api_role(sql):
    assert "DROP FUNCTION IF EXISTS public.get_user_api_key();" in sql
    for signature in ("public.upsert_user_api_key(TEXT)", "public.check_user_has_api_key()"):
        assert f"REVOKE ALL ON FUNCTION {signature} FROM service_role;" in sql


def test_macro_pit_authority_is_only_re_flagged_never_redefined(sql):
    assert "CREATE OR REPLACE FUNCTION public.get_pit_macro_observation" not in sql
    assert "SECURITY INVOKER SET search_path = public, pg_temp" in sql


def test_every_function_defined_by_any_migration_has_a_pinned_search_path_after_028(sql):
    last_header: dict = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = re.sub(r"\s+", " ", code(path))
        for match in re.finditer(r"CREATE (?:OR REPLACE )?FUNCTION public\.(\w+)\(.*?(\$\w*\$).*?\2[^;]*;", text):
            last_header[match.group(1)] = re.sub(r"\$\w*\$.*?\$\w*\$", " ", match.group(0))     # the declaration with the body removed
    altered = set(re.findall(r"ALTER FUNCTION public\.(\w+)\([^)]*\)[^;]*SET search_path", sql))
    dropped = set(re.findall(r"DROP FUNCTION IF EXISTS public\.(\w+)\(", sql))
    missing = [
        name for name, header in last_header.items()
        if name not in dropped and "SET search_path" not in header and name not in altered
    ]
    assert missing == []


def test_forward_default_function_privileges_are_hardened_without_touching_service_role(sql):
    assert "ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;" in sql
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM anon, authenticated;" in sql


def test_scheduler_tables_get_no_policy_from_this_migration(sql):
    assert "private_scheduler" not in sql and "CREATE POLICY" not in sql


# --- analyze Edge Function contract ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def edge() -> str:
    return EDGE.read_text(encoding="utf-8")


def test_user_jwt_client_stays_separate_and_remains_the_only_authentication_authority(edge):
    assert re.search(r"const supabaseClient = createClient\(\s*supabaseUrl,\s*Deno\.env\.get\('SUPABASE_ANON_KEY'\)", edge)
    assert "Authorization: req.headers.get('Authorization')!" in edge
    assert "await supabaseClient.auth.getUser()" in edge and "adminClient.auth" not in edge
    assert "supabaseClient.rpc(" not in edge                                       # the user-scoped client performs no privileged RPC
    assert ".from('allowed_assets')" in edge and "supabaseClient\n      .from('allowed_assets')" in edge


def test_privileged_client_is_server_side_only_and_the_service_credential_never_leaves_the_function(edge):
    assert edge.count("SUPABASE_SERVICE_ROLE_KEY") == 1
    assert re.search(r"const adminClient = createClient\(\s*supabaseUrl,\s*Deno\.env\.get\('SUPABASE_SERVICE_ROLE_KEY'\) \?\? '',\s*\{ auth: \{ persistSession: false, autoRefreshToken: false \} \}", edge)
    assert not re.search(r"(?i)service_role|SERVICE_ROLE_KEY|adminClient", "\n".join(l for l in edge.splitlines() if "console." in l or "Response(" in l or "JSON.stringify" in l))
    assert "eyJ" not in edge and "sb_secret" not in edge                              # nothing hard-coded
    assert "x-service" not in edge.lower()


def test_rate_limit_is_consumed_only_through_the_admin_client_with_only_a_server_derived_identifier(edge):
    call = re.search(r"adminClient\.rpc\('consume_rate_limit', \{(.*?)\}\)", edge, re.S)
    assert call and call.group(1).strip() == "p_identifier: userIdentifier"
    assert "p_capacity" not in edge and "p_refill_rate" not in edge
    derivation = edge.split("const { data: rateLimitPassed")[0]
    assert "req.json" not in derivation and "body." not in derivation               # the request body can never influence the bucket
    assert re.search(r"let userIdentifier = `ip:\$\{forwardedFor \|\| 'anonymous'\}`", edge)
    assert "userIdentifier = `user:${user.id}`" in edge                              # the verified user id, namespaced
    assert "x-forwarded-for" in edge and ".slice(0, 128)" in edge


def test_vault_key_retrieval_is_service_side_for_the_verified_user_and_the_legacy_rpc_is_gone(edge):
    assert "rpc('get_user_api_key')" not in edge and "rpc('get_user_api_key'," not in edge
    assert "adminClient.rpc('get_user_api_key_for_service', { p_user_id: user.id })" in edge
    assert edge.index("supabaseClient.auth.getUser()") < edge.index("get_user_api_key_for_service") < edge.index("consume_rate_limit")


def test_byok_behavior_is_preserved(edge):
    assert "req.headers.get('x-gemini-key')" in edge
    assert "'API Key not found in Vault. Please configure your key.'" in edge
    assert "error: 'Missing Gemini API Key'" in edge and "status: 401" in edge
    assert "error: 'Rate limit exceeded (15 RPM)'" in edge and "status: 429" in edge
    assert "if (rateError || !rateLimitPassed)" in edge                             # fail closed


# --- documentation and CI wiring -----------------------------------------------------------------------------------------------------------------------------
def test_documentation_records_the_adjudication_and_the_live_verification_requirement():
    doc = DOC.read_text(encoding="utf-8")
    for needle in (
        "public.rate_limits", "RLS Disabled in Public", "consume_rate_limit", "p_capacity", "ACCEPTED_INTENTIONAL_INFO", "ACCEPTED_PLATFORM_LIMITATION_FREE_PLAN",
        "get_user_api_key_for_service", "upsert_user_api_key", "check_user_has_api_key", "get_pit_macro_observation", "search_path", "Security Advisor", "re-run",
        "default function privileges", "does not prove the LIVE",
    ):
        assert needle.lower() in doc.lower(), needle


def test_ci_runs_the_three_gates_permanently():
    ci = CI.read_text(encoding="utf-8")
    assert "Phase 27 Supabase security schema and Edge Function contract" in ci
    assert "Phase 27 PostgreSQL Supabase security advisor regression" in ci
    for path in ("test_supabase_security_advisor_schema.py", "test_supabase_security_advisor_postgres.py"):
        assert f"backend/tests/{path}" in ci
    for kept in ("Phase 26 PostgreSQL history-coverage concurrency", "Phase 27 PostgreSQL ledger trigger privilege regression", "Phase 14 fee-tax attribution correctness"):
        assert kept in ci
