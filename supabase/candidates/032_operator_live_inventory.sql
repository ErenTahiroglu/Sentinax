-- Phase 25D1E0 — READ-ONLY live catalog inventory for the Migration 032 candidate. OPERATOR ONLY: never run by the agent.
-- Run in the Supabase SQL editor of the production project (SELECT statements only; changes nothing). Save the output as the pre-032 snapshot
-- and send it (no secrets are in it) to the Red Team. The candidate refuses to apply while any relation in (1) is unclassified.

-- 1. Every relation in public (the candidate aborts for anything it does not list: legacy tables, views, ...)
SELECT c.relname, c.relkind, c.relrowsecurity AS rls, c.relforcerowsecurity AS rls_forced, pg_get_userbyid(c.relowner) AS owner, c.relacl::text AS acl
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p', 'f') ORDER BY 1;

-- 2. Effective table privileges per API role (anon / authenticated / service_role)
SELECT c.relname, r.rolname,
       (SELECT string_agg(p.priv, ',' ORDER BY p.priv) FROM (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS p(priv)
         WHERE has_table_privilege(r.oid, c.oid, p.priv)) AS privileges
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN pg_roles r
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p') AND r.rolname IN ('anon', 'authenticated', 'service_role') ORDER BY 1, 2;

-- 3. Every row level security policy
SELECT schemaname, tablename, policyname, permissive, roles, cmd, qual, with_check FROM pg_policies WHERE schemaname = 'public' ORDER BY tablename, policyname;

-- 4. Functions in public: security mode, pinned config, and which API roles may execute them
SELECT p.oid::regprocedure::text AS signature, p.prosecdef AS security_definer, p.proconfig::text AS config,
       has_function_privilege('anon', p.oid, 'EXECUTE') AS anon_exec,
       has_function_privilege('authenticated', p.oid, 'EXECUTE') AS authenticated_exec,
       has_function_privilege('service_role', p.oid, 'EXECUTE') AS service_role_exec
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.prokind = 'f' ORDER BY 1;

-- 5. Default privileges (what a future table / function inherits)
SELECT d.defaclrole::regrole::text AS role, d.defaclnamespace::regnamespace::text AS schema, d.defaclobjtype AS objtype, d.defaclacl::text AS acl FROM pg_default_acl d ORDER BY 1, 2, 3;

-- 6. Legacy tables that exist only in infrastructure/database/*.sql (a non-null result means the candidate will block until they are classified)
SELECT t.name, to_regclass('public.' || t.name) AS present
FROM (VALUES ('user_settings'), ('paper_trades'), ('portfolio_snapshots'), ('legacy_portfolio_watchlists'), ('alerts'), ('llm_usage_logs'), ('user_events')) AS t(name);

-- 7. Role facts and auth helpers the policies depend on
SELECT rolname, rolbypassrls, rolsuper FROM pg_roles WHERE rolname IN ('anon', 'authenticated', 'service_role', 'postgres', 'authenticator') ORDER BY 1;
SELECT to_regprocedure('auth.uid()') AS auth_uid, to_regprocedure('auth.jwt()') AS auth_jwt, to_regnamespace('private_config') AS private_config_schema;

-- 8. Authentication posture (counts only, no identities): total users and how many have a verified TOTP factor
SELECT (SELECT count(*) FROM auth.users) AS users, (SELECT count(*) FROM auth.mfa_factors WHERE factor_type = 'totp' AND status = 'verified') AS verified_totp_factors;
