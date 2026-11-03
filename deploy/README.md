# Pettycash deployment draft — not activated

Baseline: PR #1 at ecb7c033399ff7814df0ddafd4c35064131100de (checked 2026-11-03).
Keep changes in a separate deployment branch/PR. Do not modify Dev's
docker-compose.yml, business Flow, or development instructions.

## Architecture

- Compose project pettycash with isolated DB/media/static volumes.
- Gunicorn runs as appuser. No host ports or source bind mount.
- Dedicated nginx serves collected static files; it does not mount media.
- Raw /media/ returns 404; authenticated Django download routes remain.
- Only nginx joins the existing external Traefik network.
- HTTPS-only router for pettycash.windowasia.com.
- nginx replaces forwarded scheme headers; Django enables secure cookies.
- No scheduled reminders, automatic receipt confirmation or log cleanup.
- PC_INTEGRATIONS_ENABLED=false clears settings-level AD/Telegram values only.
  It does not guarantee integrations configured in the application database are
  disabled. Inspect the applicable configuration paths before connecting real
  data or exercising actions that could contact external services.
- HSTS is deferred pending TLS/recovery verification.
- Current nginx upload limit is 20 MB; confirm against the existing app.

## Confirmed readiness blocker

The signature setup and claim print templates directly use signature.url.
These images will not work with private media until an appropriate
authenticated signature endpoint is supplied. Do not expose all media.

## Owner confirmations before activation

1. Authorized SSH host/user/port and independently verified host key.
   Do not create credentials or grant persistent/Docker access implicitly.
2. Read-only verification of live DNS A/AAAA, Traefik network, TLS entrypoint
   and certificate resolver. APEX source values root_default/websecure/
   mytlschallenge are not proof of live VPS configuration.
3. Approval to configure pettycash-production Environment, Actions variables
   and secrets; confirm main review controls. A main-push workflow alone
   cannot prove PR approval.
4. Directory ownership, disk/RAM capacity, volume naming, off-host backups
   and a tested restore procedure.
5. Staging build, migration and authenticated-media verification, then
   specific approval for first database/bootstrap/import.
6. Separate decision on AD/Telegram and background business side effects.

Do not alter global Traefik, create its network, restart unrelated apps,
change repository visibility/protection or add secrets without confirmation.

## One-time setup, after approval

- Inspect existing /var/www/Pettycash contents; preserve all prior work.
- Prepare /var/www/Pettycash/repository with origin
  https://github.com/ITWA-PCL/pettycash.git.
- Prepare shared/production.env outside Git, mode 600, from the example.
  Insert real secrets privately; never print docker compose config output.
- Review/install deploy/deploy.sh as /var/www/Pettycash/bin/deploy.sh.
  Changes to this trusted launcher need separate review/installation.
- Create shared/INITIAL_DEPLOY_APPROVED only after approval to initialize
  a new empty database. It never authorizes replacement of existing data.
- Configure the verified GitHub Environment:
  repository secrets VPS_HOST, VPS_USER, VPS_PORT, VPS_SSH_KEY and
  VPS_KNOWN_HOSTS (verified host key entries, not an unverified ssh-keyscan result).
  Keep pettycash-production as the job environment; repository secrets are inherited
  unless environment-level secrets with the same names override them.
  Set PETTYCASH_AUTODEPLOY_ENABLED=true only after readiness approval.
- Publish through a separate PR once GitHub write access is resolved.
  Do not retry or bypass the existing connector 403 with another identity.
- Merge/first live deployment remain separate owner actions.

## Deployment behavior

The workflow runs on main push or manual dispatch from main.
GitHub concurrency and a server flock serialize runs.
The server requires requested SHA to equal freshly fetched main,
rejecting stale or unmerged runs, and archives that exact source.

It validates configuration/network/project ownership and builds before
stopping only Pettycash gateway/web. There is maintenance downtime.
It then starts/checks DB/Redis, saves DB and media backups, records migration
plan and previous/target SHA, migrates, collects static, starts Gunicorn,
checks DB/Redis/login, then verifies HTTPS, exact release header and raw
media rejection. deployed-sha is recorded only after readiness succeeds.

No global prune, volume deletion, automatic data rollback, or unrelated service restart.

## Cloud review and limitations (2026-11-02)

This is an UNPUBLISHED DRAFT, not installed and not runtime verified.
The original child README was truncated; this companion reconstructs the complete
visible handoff and records additional review rather than claiming byte identity.
The package now includes a deployment-only Dockerfile.dockerignore (10 deployment
files total); Dev's root .dockerignore and development Compose remain untouched.

Read-only inspection of baseline config/urls.py confirms /login/ exists.
The baseline .dockerignore does not exclude deploy/, so module invocation is valid
with Python namespace packages. Deployment-specific ignore rules additionally
exclude local databases, environment files, key files and backup folders.
The container uses fixed UID/GID 10001; existing media/static must be writable by
that identity. The launcher checks writability and never chowns existing data.
Connection timeouts bound DB/Redis probes; production REDIS_URL is constrained.
Anonymous login invokes middleware: it is not a guarantee of zero database writes.
If app maintenance mode is enabled, login returns 503 and readiness intentionally
fails; do not silently bypass or disable the owner's maintenance setting.

### Remaining prerequisites / risks

Completed local checks on 2026-11-02: Bash syntax for deploy.sh and the
workflow run block; both YAML files parsed with PyYAML BaseLoader; both Python
modules parsed with ast; static assertions verified no host ports, only gateway
on the proxy network and no gateway media mount. Isolated settings tests passed
one valid case, 12 missing-variable rejection cases and five invalid-value
rejection cases. These stubbed tests do not load the actual Django application.

- No Docker, Django or nginx executable is installed in this review workspace.
  No image build, Compose CLI validation, nginx validation, application tests,
  migrations, live URL probe, backup restoration or live deployment was run.
- Syntax checks are not Compose schema validation, Actions validation or runtime
  verification. Settings tests use an isolated stub for Django/config imports.
- Python 3.14 and all unpinned dependency/image tags need staging compatibility
  testing; exact application SHA does not pin dependencies or base image digests.
- Confirm existing database is PostgreSQL 17 before starting this stack. Never
  point this draft at another version's data directory or existing unknown volumes.
- Backup archive/list checks are not a restore test. Take/verify an off-host backup
  and rehearse restoration before production migrations. Launcher stops only
  Pettycash web/gateway before backup; confirm no other writers use its database.
- On migration/readiness failure, the app may remain unavailable. Preserve the
  backup and release, inspect actual schema state, and obtain a recovery decision;
  automatic image rollback is unsafe after schema changes.
- Django check --deploy emits warnings by default (including deliberately deferred
  HSTS); review warnings explicitly. It does not prove every deployment setting safe.
- Signature templates are an activation blocker, not patched here; use authorized
  application-level access control and test both permitted and denied users.
- Do not enable auto-deploy until staging and signature privacy are verified.
- GitHub write remains blocked by the existing 403. No publish, alternate identity,
  credential workaround, PR approval/merge, SSH or production mutation performed.

## Local revision 2026-11-03

Workflow now references repository secrets VPS_HOST, VPS_USER, VPS_PORT and VPS_SSH_KEY to match the owner-provided GitHub screenshot. VPS_KNOWN_HOSTS is additionally required and must contain independently verified SSH host key entries. PETTYCASH_AUTODEPLOY_ENABLED remains a variable and must stay unset until readiness is established. This revision is local only, not installed or published. A matching authorized_keys fingerprint does not verify the GitHub secret contents. Run deploy/preflight-readonly.sh on the VPS to collect non-secret status first; it never prints environment values, container environment variables, private keys or full Docker inspection data.

## APEX-equivalent verification pipeline (local draft, 2026-11-03)

security-gate.yml checks PRs to main and pushes to process-flow/security.
It scans Git history with Gitleaks, runs Trivy on this repository's root
(Python requirements rather than APEX's app directory), checks Django,
migration consistency, the existing test suite against ephemeral PostgreSQL 17
and Redis 7, and builds the production Dockerfile. These do not alter business
Flow. CI credentials are disposable test-only values and are not VPS secrets.

The main deployment workflow calls this same gate and depends on its success,
so the GitHub commit tested is the one passed to the VPS launcher. Third-party
action references are pinned to verified upstream commit SHAs. Runtime tool
downloads, Python dependencies and container image tags are not fully pinned.
The job keeps the existing gate readiness flag and verified SSH known_hosts.

Required status checks/branch protection are separate GitHub settings; adding
these files does not configure or prove enforcement. No pull_request_target,
production credentials, production database or production integrations are
used by the verification jobs. Tests may still make network calls if application
test code does so; this draft does not claim network isolation.

This matches APEX's scan -> verify/build -> merge main -> verified SSH deployment
process, not its Node commands or global network/cleanup behavior. No global
image prune, no fallback production passwords, no unverified host trust, and no
implicit creation of the shared proxy network are copied from APEX.

Local validation is static only. CI service connectivity, Django tests, scanner
licensing/downloads and image builds still need their first real GitHub run.
Gitleaks action licensing may depend on repository owner type; do not bypass a
failed gate. A successful security gate is not proof of no vulnerabilities.

Gitleaks PR comments are explicitly disabled (GITLEAKS_ENABLE_COMMENTS=false); workflow permissions remain contents: read. Scanner failures still fail the job. Empty CI environment integration values likewise do not prove that application database overrides or mocked test paths cannot contact external services.
