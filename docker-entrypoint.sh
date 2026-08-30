#!/bin/sh
set -eu

mkdir -p \
  /var/lib/turb/accounts \
  /var/lib/turb/codex_accounts \
  /var/lib/turb/codex_agent_accounts \
  /var/lib/turb/data \
  /var/lib/turb/roxy_profile_archives \
  /var/lib/turb/roxy_profile_staging \
  "/var/lib/turb/注册日志"

exec "$@"
