# Secret Scanning (Gitleaks)

Workflow: `.github/workflows/gitleaks.yml` ("Gitleaks Secret Scan"). It runs on push and pull_request to main/master and on workflow_dispatch, with
`permissions: contents: read` only.

## What it does

- Gitleaks **v8.30.1**, run as the official container pinned by tag and linux/amd64 digest
  `ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:b109bc5f8f76a38196a3e413704fc5b9e3c32360bce4e4b603bd6f45b3721dbb` (never `latest`).
- **Full-history** scan: `actions/checkout` pinned to the v6.1.0 commit SHA with `fetch-depth: 0`, then `gitleaks git` (not `dir`, not a diff-only or
  last-commit scan). The CLI is used instead of gitleaks-action because it makes the full-history scope explicit and independent of event-range behaviour in
  the action wrapper.
- Default upstream rules are retained (no `.gitleaks.toml`, no rule or entropy changes). Findings are redacted (`--redact`); the repository is mounted
  read-only; no token or license is passed; no report, SARIF or other finding artifact is produced. A finding exits non-zero and fails the job (no
  `continue-on-error`, no `|| true`).

## `.gitleaksignore`

Contains only human-adjudicated historical **exact fingerprints** (commit:path:rule:line) and comments. No wildcard, no path-only entry, so future findings
in the same files, and future secrets of any kind, are still detected. The original 65 `generic-api-key` findings were adjudicated as false positives:

- 59 test findings: 58 deterministic Phase 24 scheduler test fixtures (domain keys and idempotency identifiers) and 1 SEC derivation-key assertion fixture.
- 5 Supabase **publishable** browser keys (`sb_publishable_...`), public by design. A publishable key is not a secret or service-role key; any `sb_secret_`,
  `service_role` or backend secret finding must stay blocking.
- 1 Cloudflare Web Analytics `data-cf-beacon` token in a downloaded public HTML page; it is a client-side site token, not a Cloudflare API token.

Broad path allowlists (for example all of `backend/tests/`, `frontend/` or `fintables.html`) are forbidden because they would blind those paths to real
secrets. Future findings are never added to `.gitleaksignore` automatically: each needs independent Red Team adjudication first.

## If a real secret is found

Revoke/rotate the actual credential first (deleting it from HEAD does not remove it from Git history), then remediate history if needed. Never print or
paste secret values in logs, reports or comments.
