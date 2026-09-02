#!/usr/bin/env bash
set -Eeuo pipefail

app_dir="${TURB_APP_DIR:-/srv/turb-gpt-free-register}"
image_ref="${TURB_IMAGE:-ghcr.io/cong91/turb-gpt-free-register:main}"

if [[ ! "${image_ref}" =~ ^ghcr\.io/cong91/turb-gpt-free-register:(main|sha-[0-9a-f]{40})$ ]]; then
  echo "Refusing deploy: unsupported image reference" >&2
  exit 1
fi

export TURB_IMAGE="${image_ref}"
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

docker volume create turb_gpt_runtime >/dev/null
docker volume create turb_gpt_cloak_cache >/dev/null
callback_network="deploy_sub2api-network"
if ! docker network inspect "${callback_network}" >/dev/null 2>&1; then
  echo "Refusing deploy: required external network ${callback_network} is missing" >&2
  exit 1
fi

docker compose pull web
docker run --rm --user 0:0 --entrypoint /bin/chown \
  -v turb_gpt_runtime:/var/lib/turb \
  "${image_ref}" \
  -R 1000:1000 /var/lib/turb
docker run --rm --user 0:0 --entrypoint /bin/chown \
  -v turb_gpt_cloak_cache:/opt/cloakbrowser \
  "${image_ref}" \
  -R 1000:1000 /opt/cloakbrowser
docker compose up -d --no-build --remove-orphans

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
