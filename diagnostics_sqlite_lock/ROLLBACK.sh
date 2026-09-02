#!/usr/bin/env bash
set -euo pipefail

target="${1:?target path required}"
baseline="${2:?baseline path required}"
cp -- "$baseline" "$target"
printf 'restored %s from %s\n' "$target" "$baseline"
