#!/usr/bin/env bash
# Read-only inventory. No keys, environment contents or full inspect output.
set -u
ROOT=/var/www/Pettycash
printf '\n== Identity and directory metadata ==\n'
id
for p in "$ROOT" "$ROOT/repository" "$ROOT/bin/deploy.sh" "$ROOT/shared/production.env"; do
  if [[ -e "$p" ]]; then stat -c '%F %a %U:%G %n' -- "$p"; else printf 'MISSING %s\n' "$p"; fi
done
printf '\n== Tools ==\n'
for tool in git docker flock python3 curl tar; do command -v "$tool" || true; done
if command -v docker >/dev/null; then
  docker version --format '{{.Server.Version}}' 2>/dev/null || true
  docker compose version 2>/dev/null || true
  printf '\n== Containers: names, images, status, ports only ==\n'
  docker ps -a --format '{{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}' 2>/dev/null || true
  printf '\n== Networks: names and drivers only ==\n'
  docker network ls --format '{{.Name}} | {{.Driver}}' 2>/dev/null || true
  printf '\n== Existing Pettycash volume names only ==\n'
  docker volume ls --format '{{.Name}}' 2>/dev/null | grep '^pettycash_' || true
fi
printf '\n== Capacity ==\n'
df -h /var/www 2>/dev/null || true
free -h 2>/dev/null || true
printf '\n== SSH public host-key fingerprints ==\n'
for p in /etc/ssh/ssh_host_*_key.pub; do
  [[ -f "$p" ]] || continue
  ssh-keygen -lf "$p" -E sha256 | awk '{print $1, $2, $NF}'
done
printf '\n== Checkout commit, without remote URL or config values ==\n'
if [[ -d "$ROOT/repository/.git" ]]; then
  git -C "$ROOT/repository" rev-parse HEAD 2>/dev/null || true
  git -C "$ROOT/repository" branch --show-current 2>/dev/null || true
fi
printf '\nInventory only: no changes, secret validation or deployment performed.\n'
