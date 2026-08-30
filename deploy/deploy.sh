#!/usr/bin/env bash
set -Eeuo pipefail

app_dir="${TURB_APP_DIR:-/srv/turb-gpt-free-register}"
cd -- "${app_dir}"

test -f secrets/.env
test "$(stat --format='%a' secrets/.env)" = "600"

if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Refusing deploy: server checkout has uncommitted changes" >&2
  exit 1
fi

git fetch --prune origin main
git checkout main
git pull --ff-only origin main

docker compose config --quiet

if [[ -n "$(docker compose ps --status running -q web)" ]]; then
  docker compose exec -T web python -c '
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

source = Path("/var/lib/turb/turb.sqlite3")
backup = Path("/var/lib/turb/backups") / (
    "turb-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".sqlite3"
)
backup.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(source) as source_connection, sqlite3.connect(backup) as backup_connection:
    source_connection.backup(backup_connection)
print("SQLite backup created")
'
fi

docker compose build --pull
docker compose up -d --remove-orphans

for attempt in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:5057/login >/dev/null; then
    docker compose ps
    exit 0
  fi
  sleep 2
done

docker compose ps
docker compose logs --tail=80 web >&2
exit 1
