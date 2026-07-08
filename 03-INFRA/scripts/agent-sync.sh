#!/usr/bin/env bash
# Launcher only: the logic lives in agent_sync.py (single cross-platform
# source, shared with agent-sync.ps1). See agent_sync.py --help for modes.
set -eu
exec python3 "$(dirname "${BASH_SOURCE[0]}")/agent_sync.py" "$@"
