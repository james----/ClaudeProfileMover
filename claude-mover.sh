#!/usr/bin/env bash
# ==============================================================================
# Claude Profile Mover / Cloner for macOS
# ==============================================================================
# Convenient launcher for claude_profile_mover.py
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_EXEC="$(command -v python3 || echo "")"

if [ -z "$PYTHON_EXEC" ]; then
    echo "Error: python3 is required but was not found in PATH." >&2
    echo "Please install Python 3 or install the macOS Command Line Tools (xcode-select --install)." >&2
    exit 1
fi

exec "$PYTHON_EXEC" "$SCRIPT_DIR/claude_profile_mover.py" "$@"
