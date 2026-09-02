#!/usr/bin/env bash
set -euo pipefail

artifact_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:?usage: ROLLBACK.sh TARGET_FILE}"

cp "$artifact_dir/ORIGINAL_FILE" "$target"
sha256sum "$target"
