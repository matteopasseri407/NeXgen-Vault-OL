#!/usr/bin/env bash
# tests/run.sh — guscio sottile su Fedora/Linux. Il runner CANONICO e'
# `python3 -m pytest` (verdetto Fable sulla review Codex, 2026-07-08, punto 5):
# questo script esiste solo per comodita' da riga di comando e per includere
# il leak-check sulle fixture (criterio di accettazione B1). Il gemello
# Windows (run.ps1) invochera' lo stesso `python -m pytest`, non questo file.
set -u
cd "$(dirname "$0")"

if ! python3 -c "import pytest" >/dev/null 2>&1; then
  echo "tests/run.sh: manca pytest. Installa con: pip3 install --user pytest" >&2
  exit 1
fi
if ! python3 -c "import yaml" >/dev/null 2>&1; then
  echo "tests/run.sh: manca PyYAML. Installa con: pip3 install --user pyyaml" >&2
  exit 1
fi

echo "== leak-check sulle fixture (criterio di accettazione B1) =="
LEAK_SCAN="../sanitize/leak_scan.py"
PATTERNS="../sanitize/leak_patterns.yaml"
if [ -f "$LEAK_SCAN" ] && [ -f "$PATTERNS" ]; then
  if ! python3 "$LEAK_SCAN" --patterns "$PATTERNS" --tree fixtures; then
    echo "tests/run.sh: LEAK nelle fixture — bloccante, vedi sopra." >&2
    exit 1
  fi
  echo "fixtures pulite."
else
  echo "tests/run.sh: leak_scan.py/leak_patterns.yaml non trovati, salto il check (S0 non ancora presente?)" >&2
fi

echo
echo "== pytest =="
exec python3 -m pytest -v "$@"
