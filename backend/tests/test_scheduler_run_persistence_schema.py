"""
backend/tests/test_scheduler_run_persistence_schema.py
======================================================
Phase 24D1: static source verification of migration 024 (private scheduler run persistence). The migration mirrors the CLOSED Python authorities
(24A trigger, 24C2A admission, 24C2B lifecycle) with durable uniqueness, an immutable admission identity, an atomic state_version CAS RPC, an
append-only transition history that persists `transition_at` (the renewal instant the pure 24C2B record cannot hold), and a service-role-only write
surface. Python stays the semantic authority; these tests read the SQL text only (no database is available locally; the exact-SHA Supabase Preview
exercises the migration).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.engine.private.scheduler_run_admission import PrivateSchedulerRunAdmissionSource
from backend.engine.private.scheduler_run_lifecycle import PrivateSchedulerRunState, PrivateSchedulerRunTransitionKind
from backend.engine.private.scheduler_trigger import (
    PrivateSchedulerEventCauseKind,
    PrivateSchedulerScope,
    PrivateSchedulerTriggerKind,
    PrivateSchedulerWorkKind,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "024_private_scheduler_run_persistence.sql"
RUNS = "private_scheduler_runs"
HISTORY = "private_scheduler_run_transitions"
INIT_FN = "initialize_private_scheduler_run"
APPLY_FN = "apply_private_scheduler_run_transition"


@pytest.fixture(scope="module")
def raw_sql() -> str:
    assert MIGRATION.is_file(), "migration 024_private_scheduler_run_persistence.sql must exist"
    return MIGRATION.read_text(encoding="utf-8")


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


@pytest.fixture(scope="module")
def sql(raw_sql: str) -> str:
    return strip_comments(raw_sql)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


@pytest.fixture(scope="module")
def nsql(sql: str) -> str:
    return norm(sql)


def balanced(text: str, start: int) -> str:
    """Text between the parenthesis that opens at `start` and its closing parenthesis."""
    assert text[start] == "("
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
    raise AssertionError("unbalanced parentheses")


def table_body(sql: str, table: str) -> str:
    match = re.search(rf"create\s+table\s+(?:if\s+not\s+exists\s+)?public\.{table}\s*\(", sql, flags=re.I)
    assert match, f"CREATE TABLE public.{table} not found"
    return balanced(sql, match.end() - 1)


def top_level_segments(body: str) -> list[str]:
    segments, depth, current = [], 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            segments.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        segments.append("".join(current).strip())
    return segments


def columns(sql: str, table: str) -> list[str]:
    names = []
    for segment in top_level_segments(table_body(sql, table)):
        first = segment.split()[0].lower()
        if first not in {"constraint", "primary", "foreign", "unique", "check"}:
            names.append(first)
    return names


def constraint(sql: str, table: str, name: str) -> str:
    for segment in top_level_segments(table_body(sql, table)):
        if re.match(rf"constraint\s+{name}\b", segment, flags=re.I):
            return norm(segment)
    raise AssertionError(f"constraint {name} not found in {table}")


def literals(expression: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", expression))


def function_text(sql: str, name: str) -> str:
    match = re.search(rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\(", sql, flags=re.I)
    assert match, f"function {name} not found"
    end = sql.index("$$;", sql.index("$$", match.end()) + 2) + 3
    return sql[match.start():end]


def function_signature(sql: str, name: str) -> str:
    match = re.search(rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\(", sql, flags=re.I)
    return norm(balanced(sql, match.end() - 1))


RUN_COLUMNS = ["run_idempotency_sha256", "admission_source", "trigger_kind", "work_kind", "scope", "owner_id", "portfolio_id", "scheduled_for",
               "event_cause_kind", "cause_key", "cause_available_at", "policy_key", "policy_revision", "admission_payload", "state", "state_version",
               "claim_key", "claimed_at", "lease_expires_at", "terminal_at", "failure_code", "created_at", "updated_at"]
HISTORY_COLUMNS = ["run_idempotency_sha256", "after_state_version", "transition_kind", "before_state_version", "transition_at", "after_state", "claim_key",
                   "claimed_at", "lease_expires_at", "terminal_at", "failure_code", "recorded_at"]


# --- file / numbering / tables -----------------------------------------------------------------------------------

def test_migration_024_is_the_next_number_and_older_migrations_are_untouched(raw_sql: str) -> None:
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert len([n for n in names if n.startswith("024")]) == 1
    index_024 = names.index("024_private_scheduler_run_persistence.sql")
    assert index_024 > 0 and names[index_024 - 1] == "023_macro_state_input_history_rpc.sql"
    assert raw_sql.strip()


def test_exactly_two_tables_are_created_and_nothing_pre_existing_is_altered(sql: str) -> None:
    created = re.findall(r"create\s+table\s+(?:if\s+not\s+exists\s+)?public\.(\w+)", sql, flags=re.I)
    assert sorted(created) == sorted([RUNS, HISTORY])
    altered = set(re.findall(r"alter\s+table\s+(?:only\s+)?public\.(\w+)", sql, flags=re.I))
    assert altered <= {RUNS, HISTORY}
    assert not re.search(r"\bdrop\s+(table|column|constraint|policy)\b", sql, flags=re.I)
    assert not re.search(r"legacy_portfolio_watchlists|job_queue|backgroundtasks", sql, flags=re.I)


def test_current_run_columns_are_exact(sql: str) -> None:
    assert columns(sql, RUNS) == RUN_COLUMNS
    assert not set(columns(sql, RUNS)) & {"run_id", "id", "job_id", "attempt", "retry_count", "retry_after", "max_attempts", "result", "result_json",
                                          "analysis_result", "provider_response", "exception_text", "stack_trace"}
    assert re.search(r"admission_payload\s+jsonb\s+not\s+null", sql, flags=re.I)


def test_history_columns_are_exact(sql: str) -> None:
    assert columns(sql, HISTORY) == HISTORY_COLUMNS
    assert not set(columns(sql, HISTORY)) & {"id", "attempt", "retry_count", "result", "exception_text", "stack_trace"}


# --- identity / admission / trigger / owner constraints ----------------------------------------------------------

def test_run_hash_is_the_explicitly_named_primary_key_and_lowercase_64_hex(sql: str) -> None:
    assert re.search(r"run_idempotency_sha256\s+varchar\(64\)\s+not\s+null", sql, flags=re.I)
    assert "constraint pk_private_scheduler_runs primary key (run_idempotency_sha256)" in norm(table_body(sql, RUNS))
    check = constraint(sql, RUNS, "ck_psr_run_hash")
    assert "^[0-9a-f]{64}$" in check
    assert not re.search(r"gen_random_uuid|uuid_generate_v4|random\s*\(", sql, flags=re.I)


def test_enum_universes_mirror_the_closed_python_authorities(sql: str) -> None:
    assert literals(constraint(sql, RUNS, "ck_psr_admission_source")) == {m.value for m in PrivateSchedulerRunAdmissionSource}
    assert literals(constraint(sql, RUNS, "ck_psr_trigger_kind")) == {m.value for m in PrivateSchedulerTriggerKind}
    assert literals(constraint(sql, RUNS, "ck_psr_work_kind")) == {m.value for m in PrivateSchedulerWorkKind}
    assert literals(constraint(sql, RUNS, "ck_psr_scope")) == {m.value for m in PrivateSchedulerScope}
    assert literals(constraint(sql, RUNS, "ck_psr_event_cause_kind")) == {m.value for m in PrivateSchedulerEventCauseKind}
    assert literals(constraint(sql, RUNS, "ck_psr_state")) == {m.value for m in PrivateSchedulerRunState} == {"ready", "claimed", "succeeded", "failed"}
    assert literals(constraint(sql, HISTORY, "ck_psrt_transition_kind")) == {m.value for m in PrivateSchedulerRunTransitionKind}
    assert literals(constraint(sql, HISTORY, "ck_psrt_after_state")) == {m.value for m in PrivateSchedulerRunState}
    for legacy in ("pending", "running", "completed", "error", "retrying", "cancelled", "skipped", "raw_trigger", "legacy_job"):
        assert f"'{legacy}'" not in norm(sql)


def test_source_kind_matrix_and_trigger_shapes(sql: str) -> None:
    matrix = constraint(sql, RUNS, "ck_psr_source_kind_matrix")
    assert "'scheduled_direct'" in matrix and "'scheduled_calendar_applicable'" in matrix and "trigger_kind = 'scheduled'" in matrix
    assert "admission_source = 'event_driven' and trigger_kind = 'event_driven'" in matrix
    shape = constraint(sql, RUNS, "ck_psr_trigger_shape")
    assert "scheduled_for is not null" in shape and "scheduled_for is null" in shape
    for column in ("event_cause_kind", "cause_key", "cause_available_at"):
        assert f"{column} is null" in shape and f"{column} is not null" in shape


def test_owner_isolation_and_work_scope_matrix(sql: str) -> None:
    owner = constraint(sql, RUNS, "ck_psr_scope_owner")
    assert "scope = 'system' and owner_id is null and portfolio_id is null" in owner
    assert "scope = 'portfolio' and owner_id is not null and portfolio_id is not null" in owner
    matrix = constraint(sql, RUNS, "ck_psr_work_scope")
    assert "work_kind = 'source_data_refresh' and scope = 'system'" in matrix
    assert re.search(r"work_kind\s*(<>|!=)\s*'source_data_refresh' and scope = 'portfolio'", matrix)


def test_cause_key_policy_and_payload_constraints(sql: str) -> None:
    cause = constraint(sql, RUNS, "ck_psr_cause_key")
    assert "cause_key is null" in cause and "char_length(cause_key) between 1 and 128" in cause and "cntrl" in cause
    assert "lower(" not in cause and "upper(" not in cause                                      # case is preserved, never normalized
    policy = constraint(sql, RUNS, "ck_psr_policy_key")
    assert "^[a-z0-9][a-z0-9._-]{0,127}$" in policy
    assert re.search(r"policy_revision\s+bigint\s+not\s+null", sql, flags=re.I) and "policy_revision >= 1" in constraint(sql, RUNS, "ck_psr_policy_revision")
    assert "jsonb_typeof(admission_payload) = 'object'" in constraint(sql, RUNS, "ck_psr_admission_payload")
    claim = constraint(sql, RUNS, "ck_psr_claim_key")
    assert "claim_key is null" in claim and "char_length(claim_key) between 1 and 128" in claim and "cntrl" in claim
    failure = constraint(sql, RUNS, "ck_psr_failure_code")
    assert "^[a-z0-9][a-z0-9._-]{0,127}$" in failure


# --- lifecycle shapes --------------------------------------------------------------------------------------------

def test_lifecycle_shape_constraints_mirror_the_closed_24c2b_shapes(sql: str) -> None:
    shape = constraint(sql, RUNS, "ck_psr_lifecycle_shape")
    ready = shape[shape.index("state = 'ready'"):shape.index("state = 'claimed'")]
    for fragment in ("state_version = 1", "claim_key is null", "claimed_at is null", "lease_expires_at is null", "terminal_at is null", "failure_code is null"):
        assert fragment in ready
    claimed = shape[shape.index("state = 'claimed'"):shape.index("state = 'succeeded'")]
    for fragment in ("state_version >= 2", "claim_key is not null", "claimed_at is not null", "lease_expires_at is not null", "claimed_at < lease_expires_at",
                     "terminal_at is null", "failure_code is null"):
        assert fragment in claimed
    succeeded = shape[shape.index("state = 'succeeded'"):shape.index("state = 'failed'")]
    for fragment in ("state_version >= 3", "terminal_at is not null", "failure_code is null", "claimed_at <= terminal_at", "terminal_at < lease_expires_at"):
        assert fragment in succeeded
    failed = shape[shape.index("state = 'failed'"):]
    for fragment in ("state_version >= 3", "terminal_at is not null", "failure_code is not null", "claimed_at <= terminal_at", "terminal_at < lease_expires_at"):
        assert fragment in failed


def test_persistence_clocks_are_database_metadata_only(sql: str) -> None:
    for column in ("created_at", "updated_at"):
        assert re.search(rf"{column}\s+timestamptz\s+not\s+null\s+default\s+now\(\)", sql, flags=re.I)
    assert re.search(r"recorded_at\s+timestamptz\s+not\s+null\s+default\s+now\(\)", sql, flags=re.I)
    for domain_column in ("scheduled_for", "cause_available_at", "claimed_at", "lease_expires_at", "terminal_at", "transition_at"):
        assert not re.search(rf"{domain_column}\s+timestamptz[^,]*default", sql, flags=re.I)        # domain instants are never generated by the database


# --- immutability / finality / version step ----------------------------------------------------------------------

def test_update_guard_trigger_protects_admission_identity_terminal_rows_and_the_version_step(sql: str, nsql: str) -> None:
    assert re.search(rf"create\s+trigger\s+\w+\s+before\s+update\s+on\s+public\.{RUNS}\s+for\s+each\s+row", sql, flags=re.I)
    body = norm(function_text(sql, "private_scheduler_runs_guard_update"))
    for column in ("run_idempotency_sha256", "admission_source", "trigger_kind", "work_kind", "scope", "owner_id", "portfolio_id", "scheduled_for",
                   "event_cause_kind", "cause_key", "cause_available_at", "policy_key", "policy_revision", "admission_payload", "created_at"):
        assert f"new.{column} is distinct from old.{column}" in body, column
    assert "raise exception" in body
    assert "old.state in ('succeeded', 'failed')" in body                                         # terminal rows are final
    assert "new.state_version <> old.state_version + 1" in body or "new.state_version is distinct from old.state_version + 1" in body
    assert re.search(rf"create\s+trigger\s+\w+\s+before\s+delete\s+on\s+public\.{RUNS}", sql, flags=re.I)


# --- history table -----------------------------------------------------------------------------------------------

def test_history_keys_and_foreign_key(sql: str) -> None:
    body = norm(table_body(sql, HISTORY))
    assert "constraint pk_private_scheduler_run_transitions primary key (run_idempotency_sha256, after_state_version)" in body
    assert re.search(r"foreign key \(run_idempotency_sha256\) references public\.private_scheduler_runs ?\(run_idempotency_sha256\) on delete restrict", body)


def test_history_shapes(sql: str) -> None:
    initialize = constraint(sql, HISTORY, "ck_psrt_initialize_shape")
    assert "transition_kind <> 'initialize' or (" in initialize                                  # implication: every initialize row has exactly this shape
    for fragment in ("before_state_version is null", "after_state_version = 1", "transition_at is null",
                     "after_state = 'ready'", "claim_key is null", "claimed_at is null", "lease_expires_at is null", "terminal_at is null",
                     "failure_code is null"):
        assert fragment in initialize
    step = constraint(sql, HISTORY, "ck_psrt_version_step")
    assert "transition_kind = 'initialize' or (" in step                                          # every non-initialize row must satisfy the step
    assert "before_state_version is not null" in step and "after_state_version = before_state_version + 1" in step and "transition_at is not null" in step
    shape = constraint(sql, HISTORY, "ck_psrt_after_snapshot_shape")
    for fragment in ("after_state = 'ready'", "after_state = 'claimed'", "after_state = 'succeeded'", "after_state = 'failed'", "claimed_at < lease_expires_at",
                     "claimed_at <= terminal_at", "terminal_at < lease_expires_at", "after_state_version >= 2", "after_state_version >= 3",
                     "failure_code is not null", "failure_code is null"):
        assert fragment in shape
    assert "^[a-z0-9][a-z0-9._-]{0,127}$" in constraint(sql, HISTORY, "ck_psrt_failure_code")
    assert "cntrl" in constraint(sql, HISTORY, "ck_psrt_claim_key")
    assert "transition_at" in columns(sql, HISTORY)


def test_history_is_append_only(sql: str) -> None:
    assert re.search(rf"create\s+trigger\s+\w+\s+before\s+update\s+or\s+delete\s+on\s+public\.{HISTORY}\s+for\s+each\s+row", sql, flags=re.I)
    body = norm(function_text(sql, "private_scheduler_run_transitions_append_only"))
    assert "raise exception" in body and "append-only" in body
    assert re.search(rf"before\s+truncate\s+on\s+public\.{HISTORY}", sql, flags=re.I)


# --- initialization RPC ------------------------------------------------------------------------------------------

def test_initialization_rpc_inputs_and_outputs(sql: str) -> None:
    signature = function_signature(sql, INIT_FN)
    for parameter in ("p_run_idempotency_sha256", "p_admission_source", "p_trigger_kind", "p_work_kind", "p_scope", "p_owner_id", "p_portfolio_id",
                      "p_scheduled_for", "p_event_cause_kind", "p_cause_key", "p_cause_available_at", "p_policy_key", "p_policy_revision",
                      "p_admission_payload"):
        assert parameter in signature
    assert not re.search(r"p_state|p_state_version|p_claim|p_lease|p_terminal|p_failure", signature)           # the database owns the lifecycle fields
    assert not re.search(r"\bdefault\b|=\s*null", signature)
    body = function_text(sql, INIT_FN)
    returns = norm(body[:body.lower().index("language")])
    for column in ("status", "run_idempotency_sha256", "state", "state_version"):
        assert re.search(rf"\b{column}\b", returns)


def test_initialization_creates_ready_version_one_with_atomic_initialize_history(sql: str) -> None:
    body = norm(function_text(sql, INIT_FN))
    assert f"insert into public.{RUNS}" in body and f"insert into public.{HISTORY}" in body
    assert "'ready'" in body and re.search(r"values \([^;]*'ready', 1, null, null, null, null, null \)", body)
    assert "'initialize'" in body
    for status in ("'initialized'", "'idempotent_duplicate'", "'conflict'"):
        assert status in body
    assert body.index(f"insert into public.{RUNS}") < body.index(f"insert into public.{HISTORY}")


def test_initialization_race_safety_follows_the_migration_015_pattern(sql: str) -> None:
    body = norm(function_text(sql, INIT_FN))
    assert "exception when unique_violation then" in body
    assert "get stacked diagnostics" in body and "constraint_name" in body
    assert "is distinct from 'pk_private_scheduler_runs'" in body and re.search(r"then raise;", body)       # an unexplained violation is re-raised
    assert "is not distinct from" in body                                                                    # immutable identity comparison on re-read
    assert "admission_payload" in body
    assert "on conflict" not in body                                                                         # no blind swallow


# --- transition RPC ----------------------------------------------------------------------------------------------

def test_transition_rpc_explicit_inputs_and_no_caller_after_state(sql: str) -> None:
    signature = function_signature(sql, APPLY_FN)
    for parameter in ("p_run_idempotency_sha256", "p_expected_version", "p_transition_kind", "p_claim_key", "p_transition_at", "p_lease_expires_at",
                      "p_failure_code"):
        assert parameter in signature
    assert signature.count("p_") == 7
    assert not re.search(r"p_after|p_state|p_claimed_at|p_terminal_at|p_before", signature)
    assert not re.search(r"\bdefault\b|=\s*null", signature)


def test_transition_rpc_status_universe_and_cas_predicates(sql: str) -> None:
    body = norm(function_text(sql, APPLY_FN))
    for status in ("'applied'", "'not_found'", "'version_conflict'", "'transition_conflict'"):
        assert status in body
    assert body.count("update public.private_scheduler_runs") >= 5                                           # one conditional UPDATE per transition kind
    updates = re.findall(r"update public\.private_scheduler_runs as r set .*? returning", body)
    assert len(updates) >= 5
    for update in updates:
        assert "r.run_idempotency_sha256 = p_run_idempotency_sha256" in update
        assert "r.state_version = p_expected_version" in update                                              # the atomic compare-and-swap predicate
        assert "state_version = r.state_version + 1" in update
    assert "found" in body and "row_count" in body


def test_transition_branches_mirror_the_closed_24c2b_boundaries(sql: str) -> None:
    body = norm(function_text(sql, APPLY_FN))
    for kind in ("'claim'", "'renew_claim'", "'take_over_expired_claim'", "'succeed'", "'fail'"):
        assert kind in body
    assert "scheduled_for" in body and "cause_available_at" in body                                           # claim: not-before frontier by trigger kind
    assert re.search(r"p_transition_at >= v_not_before|p_transition_at >= case", body)
    assert "p_transition_at < p_lease_expires_at" in body                                                     # claim: valid lease
    assert re.search(r"v_row\.claimed_at <= p_transition_at and p_transition_at < v_row\.lease_expires_at", body)    # renew / succeed / fail: active lease
    assert "p_lease_expires_at > v_row.lease_expires_at" in body                                              # renew: strictly extended
    assert re.search(r"p_transition_at >= v_row\.lease_expires_at", body)                                     # takeover: only at or after expiry
    assert re.search(r"p_claim_key (is distinct from|<>) v_row\.claim_key|p_claim_key <> v_row\.claim_key", body)     # takeover: a new claim key
    assert re.search(r"p_claim_key (is not distinct from|=) v_row\.claim_key", body)                          # renew / succeed / fail: the exact current claim
    assert "p_lease_expires_at > p_transition_at" in body                                                     # takeover: new lease interval
    assert not re.search(r"interval '|\+ \w+ interval", body)                                                 # no tolerance


def test_terminal_rows_never_match_a_transition_branch(sql: str) -> None:
    body = norm(function_text(sql, APPLY_FN))
    assert "v_row.state = 'ready'" in body and "v_row.state = 'claimed'" in body
    assert "'succeeded'" not in body.split("returning")[0].split("begin")[0]                                  # terminal states only appear as outputs
    assert "transition_conflict" in body


def test_applied_transition_and_history_insert_share_one_transaction(sql: str) -> None:
    body = norm(function_text(sql, APPLY_FN))
    assert f"insert into public.{HISTORY}" in body
    assert body.index("update public.private_scheduler_runs") < body.index(f"insert into public.{HISTORY}")
    assert "p_transition_at" in body[body.index(f"insert into public.{HISTORY}"):]                            # renewal / claim / terminal instant is persisted
    assert "p_expected_version" in body[body.index(f"insert into public.{HISTORY}"):]                         # before_state_version
    assert not re.search(r"\bcommit\b|\bsavepoint\b|pg_sleep|dblink", body)
    assert "returning" in body and "into v_after" in body or "into v_state" in body


def test_domain_instants_come_only_from_explicit_inputs(sql: str) -> None:
    body = norm(function_text(sql, APPLY_FN)) + norm(function_text(sql, INIT_FN))
    assert not re.search(r"\bnow\(\)|clock_timestamp|current_timestamp|transaction_timestamp|statement_timestamp", body)


# --- security ----------------------------------------------------------------------------------------------------

def test_rls_enabled_and_no_user_policies_or_write_grants(sql: str, nsql: str) -> None:
    for table in (RUNS, HISTORY):
        assert re.search(rf"alter\s+table\s+public\.{table}\s+enable\s+row\s+level\s+security", sql, flags=re.I)
        assert re.search(rf"revoke\s+all\s+on\s+(table\s+)?public\.{table}\s+from\s+public\s*,\s*anon\s*,\s*authenticated", sql, flags=re.I)
    assert not re.search(r"create\s+policy", sql, flags=re.I)                                                  # no authenticated / anon policy of any kind
    grants = re.findall(r"grant\s+([a-z, ]+?)\s+on\s+(?:table\s+)?public\.(\w+)\s+to\s+([a-z_, ]+);", sql, flags=re.I)
    assert grants
    for privileges, table, roles in grants:
        assert table in {RUNS, HISTORY}
        assert roles.strip().lower() == "service_role"
        assert not re.search(r"delete|truncate|all", privileges, flags=re.I)
    by_table = {table: privileges.lower().replace(" ", "") for privileges, table, _ in grants}
    assert by_table[HISTORY] == "select,insert"                                                                 # history can never be updated
    assert "update" in by_table[RUNS] and "insert" in by_table[RUNS]
    assert "authenticated" not in "".join(roles for _, _, roles in grants).lower()


def test_both_functions_are_invoker_volatile_pinned_and_service_role_only(sql: str) -> None:
    for name, argument_types in ((INIT_FN, None), (APPLY_FN, None)):
        text = function_text(sql, name)
        head = norm(text[:text.index("$$")])
        assert "volatile" in head and "security invoker" in head and "set search_path = public, pg_temp" in head and "language plpgsql" in head
        assert "security definer" not in head
        assert re.search(rf"revoke\s+execute\s+on\s+function\s+public\.{name}\s*\([^)]*\)\s+from\s+public\s*;", sql, flags=re.I)
        assert re.search(rf"revoke\s+execute\s+on\s+function\s+public\.{name}\s*\([^)]*\)\s+from\s+anon\s*;", sql, flags=re.I)
        assert re.search(rf"revoke\s+execute\s+on\s+function\s+public\.{name}\s*\([^)]*\)\s+from\s+authenticated\s*;", sql, flags=re.I)
        assert re.search(rf"grant\s+execute\s+on\s+function\s+public\.{name}\s*\([^)]*\)\s+to\s+service_role\s*;", sql, flags=re.I)
        assert not re.search(rf"grant\s+execute\s+on\s+function\s+public\.{name}[^;]*\b(authenticated|anon|public)\b", sql, flags=re.I)


def test_table_references_are_fully_qualified(sql: str) -> None:
    for body in (function_text(sql, INIT_FN), function_text(sql, APPLY_FN)):
        for match in re.finditer(r"\b(?:from|into|update|join)\s+(?!public\.|\()(\w+)", norm(body)):
            assert match.group(1) in {"v_row", "v_existing", "v_after", "v_state", "v_rows", "found", "diagnostics"} or match.group(1).startswith("v_"), match.group(1)


# --- negative scope guards ---------------------------------------------------------------------------------------

def test_no_randomness_retry_result_queue_or_delete_surface(sql: str, nsql: str) -> None:
    assert not re.search(r"gen_random_uuid|uuid_generate_v4|\brandom\s*\(|md5\(|digest\(", sql, flags=re.I)
    assert not re.search(r"retry_count|retry_after|max_attempt|backoff|attempt\b", sql, flags=re.I)
    assert not re.search(r"result_json|analysis_result|provider_response|exception_text|stack_trace", sql, flags=re.I)
    assert not re.search(r"\bpgmq\b|redis|backgroundtasks|worker|create\s+table[^;]*queue|dispatch", sql, flags=re.I)
    assert not re.search(r"\bdelete\s+from\b|\btruncate\s+table\b", sql, flags=re.I)
    assert not re.search(r"security\s+definer", sql, flags=re.I)


def test_documentation_exists_and_states_the_boundaries() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_SCHEDULER_RUN_PERSISTENCE.md")
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")
    for needle in ("transition_at", "renew", "idempotent_duplicate", "version_conflict", "transition_conflict", "service_role", "append-only",
                   "state_version", "cannot reconstruct renewed_at", "no retry", "no result payload", "24D2", "24D3"):
        assert needle in text, needle
