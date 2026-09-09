#!/usr/bin/env bash
# Existing Kind clusters only. Never changes the current Kubernetes context.
set +x
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec "${GRACE_PYTHON:-python3}" "$SCRIPT_DIR/kind-demo.py" "$@"
