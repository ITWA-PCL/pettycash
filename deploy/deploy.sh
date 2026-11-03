#!/usr/bin/env bash
# Installed once by the owner at /var/www/Pettycash/bin/deploy.sh.
# This trusted launcher must be reviewed/reinstalled when deployment logic changes.
set -Eeuo pipefail
umask 077
ROOT=/var/www/Pettycash
SHA=${1:?pass the exact GitHub main SHA}
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid SHA"; exit 2; }
[[ -d "$ROOT" && ! -L "$ROOT" && "$(realpath "$ROOT")" == "$ROOT" ]] || exit 2
for directory in shared repository releases backups bin; do
  [[ ! -L "$ROOT/$directory" ]] || { echo "Unexpected symlinked deployment directory"; exit 2; }
done
for tool in git docker flock python3 curl tar; do command -v "$tool" >/dev/null; done
docker compose version >/dev/null
[[ -f "$ROOT/shared/production.env" && ! -L "$ROOT/shared/production.env" ]] || exit 2
[[ "$(stat -c %a "$ROOT/shared/production.env")" == 600 ]] || { echo "production.env must be mode 600"; exit 2; }
[[ -d "$ROOT/repository/.git" ]] || { echo "Owner must prepare the repository checkout"; exit 2; }
[[ "$(git -C "$ROOT/repository" remote get-url origin)" == "https://github.com/ITWA-PCL/pettycash.git" ]] || exit 2

exec 9>"$ROOT/deploy.lock"
flock -w 1800 9 || { echo "Deployment lock timeout"; exit 2; }
git -C "$ROOT/repository" fetch --no-tags origin main
[[ "$(git -C "$ROOT/repository" rev-parse FETCH_HEAD)" == "$SHA" ]] || {
  echo "Requested SHA is not current main; refusing stale or unmerged code"
  exit 2
}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RELEASE="$ROOT/releases/$SHA-$STAMP"
[[ ! -e "$RELEASE" ]] || { echo "Release already exists"; exit 2; }
mkdir -p "$RELEASE" "$ROOT/backups"
git -C "$ROOT/repository" archive "$SHA" | tar -x -C "$RELEASE"
export DEPLOY_SHA="$SHA"
dc() {
  docker compose -p pettycash \
    --env-file "$ROOT/shared/production.env" \
    -f "$RELEASE/deploy/compose.yml" "$@"
}
dc config --quiet
NETWORK=$(dc config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["networks"]["proxy"]["name"])')
docker network inspect "$NETWORK" >/dev/null

# Do not touch an unexpected workload under this Compose project.
SERVICES=$(docker ps -a --filter label=com.docker.compose.project=pettycash \
  --format '{{.Label "com.docker.compose.service"}}')
while read -r service; do
  case "$service" in
    web|gateway|db|redis|"") ;;
    *) echo "Unexpected Pettycash service: review before deploying"; exit 2 ;;
  esac
done <<< "$SERVICES"

if docker volume inspect pettycash_database >/dev/null 2>&1; then
  docker volume inspect pettycash_media >/dev/null
  [[ "$(docker volume inspect -f '{{index .Labels "com.docker.compose.project"}}' pettycash_database)" == pettycash ]] || exit 2
  [[ "$(docker volume inspect -f '{{index .Labels "com.docker.compose.project"}}' pettycash_media)" == pettycash ]] || exit 2
else
  [[ -f "$ROOT/shared/INITIAL_DEPLOY_APPROVED" ]] || {
    echo "Initial empty database needs owner approval"
    exit 2
  }
  ! docker volume inspect pettycash_media >/dev/null 2>&1 || {
    echo "Partial state requires recovery review"
    exit 2
  }
fi

dc build web
dc run --rm --no-deps web python manage.py check --deploy
dc run --rm --no-deps web sh -c 'test -w /app/media && test -w /app/staticfiles'

# Failures after stopping leave service unavailable for an explicit recovery decision.
trap 'echo "Deployment failed. Preserve backups and inspect migration state; no automatic DB rollback." >&2' ERR
dc stop gateway web
dc up -d --wait --wait-timeout 120 db redis
BACKUP="$ROOT/backups/$SHA-$STAMP"
mkdir "$BACKUP"
dc exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP/database.dump"
test -s "$BACKUP/database.dump"
dc exec -T db pg_restore --list < "$BACKUP/database.dump" > "$BACKUP/database.list"
dc run --rm --no-deps web tar -C /app/media -cf - . > "$BACKUP/media.tar"
tar -tf "$BACKUP/media.tar" >/dev/null
printf '%s\n' "$SHA" > "$BACKUP/target-sha"
if [[ -f "$ROOT/deployed-sha" ]]; then
  cp "$ROOT/deployed-sha" "$BACKUP/previous-sha"
fi
dc run --rm --no-deps web python manage.py migrate --plan > "$BACKUP/migration-plan.txt"
dc run --rm --no-deps web python manage.py migrate --noinput
dc run --rm --no-deps web python manage.py collectstatic --noinput
dc up -d --no-deps --wait --wait-timeout 180 web
dc exec -T web python -m deploy.readiness
dc up -d --no-deps gateway

# No -k: verify DNS routing and TLS, then verify public raw media remains blocked.
for attempt in {1..24}; do
  code=$(curl --silent --dump-header "$BACKUP/readiness-headers.txt" \
    --output /dev/null --write-out '%{http_code}' --max-time 10 \
    https://pettycash.windowasia.com/login/ || true)
  [[ "$code" == 200 ]] && break
  sleep 5
done
[[ "$code" == 200 ]] || { echo "Public readiness failed"; exit 1; }
tr -d '\r' < "$BACKUP/readiness-headers.txt" |
  grep -iFx "X-Pettycash-Release: $SHA" >/dev/null || {
    echo "Public route is not the deployed release"
    exit 1
  }
code=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --max-time 10 https://pettycash.windowasia.com/media/deployment-probe)
[[ "$code" == 404 ]] || { echo "Raw media exposure check failed"; exit 1; }
printf '%s\n' "$SHA" > "$ROOT/deployed-sha.next"
mv "$ROOT/deployed-sha.next" "$ROOT/deployed-sha"
echo "Deployed exact main commit $SHA; backups: $BACKUP"
