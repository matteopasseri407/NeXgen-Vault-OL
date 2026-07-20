#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Uso: agent-open-folder <percorso-assoluto-cartella>

Apre una cartella locale nel file manager predefinito.
Il percorso deve esistere ed essere una cartella.
EOF
}

if [[ $# -ne 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  [[ $# -eq 1 ]] && exit 0 || exit 2
fi

requested_path="$1"
if [[ "$requested_path" != /* ]]; then
  echo "Errore: serve un percorso assoluto di una cartella esistente." >&2
  exit 2
fi

folder="$(realpath -e -- "$requested_path")" || {
  echo "Errore: la cartella non esiste." >&2
  exit 2
}
if [[ ! -d "$folder" ]]; then
  echo "Errore: il percorso non indica una cartella." >&2
  exit 2
fi

if command -v gio >/dev/null 2>&1; then
  gio open "$folder"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$folder"
else
  echo "Errore: non trovo un comando per aprire il file manager." >&2
  exit 127
fi

printf 'Cartella aperta: %s\n' "$folder"
