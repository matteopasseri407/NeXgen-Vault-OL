#!/usr/bin/env bash
# Launcher only: the logic lives in agent-universal-layer/council/council.py (single cross-platform
# source, shared with council.ps1). See council.py --help for modes.
#
# Resolve links without readlink -f so the same launcher also works on macOS.
# A plain dirname on BASH_SOURCE would resolve the ~/.local/bin symlink's
# directory instead of this script's directory, and would miss council.py.
set -eu
source_path="${BASH_SOURCE[0]}"
while [ -h "$source_path" ]; do
  source_dir="$(cd -P "$(dirname "$source_path")" && pwd)"
  source_path="$(readlink "$source_path")"
  case "$source_path" in
    /*) ;;
    *) source_path="$source_dir/$source_path" ;;
  esac
done
script_dir="$(cd -P "$(dirname "$source_path")" && pwd)"
exec python3 "$script_dir/../agent-universal-layer/council/council.py" "$@"
