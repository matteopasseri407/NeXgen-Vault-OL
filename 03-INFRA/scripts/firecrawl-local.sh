#!/usr/bin/env bash
set -euo pipefail

API_URL="${FIRECRAWL_API_URL:-http://127.0.0.1:33002}"
API_KEY="${FIRECRAWL_API_KEY:-local-self-hosted}"

# curl config file (mode 600) holding the bearer token as an Authorization
# header, so the token never appears as a curl argv element -- an argv
# element is visible to any other local user via `ps` or
# /proc/<pid>/cmdline, a curl config file is not.
#
# Built eagerly here, at the top level of the script, rather than lazily
# inside a function called from post_json(): post_json() is itself always
# invoked as `response="$(post_json ...)"` (its curl output is captured),
# which runs post_json -- and anything it calls -- in a subshell. A
# subshell's variable assignments never propagate back to the parent shell,
# so building/assigning $AUTH_CFG lazily from inside that subshell would
# leave the parent's $AUTH_CFG empty and silently break the cleanup trap
# below (confirmed live: the temp file was never removed). Building it here
# means every invocation pays one extra mktemp, even `status`/`--help`
# which don't need it, but that's the price of a cleanup trap that's
# actually reachable.
AUTH_CFG="$(mktemp)"
chmod 600 "$AUTH_CFG"
printf 'header = "Authorization: Bearer %s"\n' "${API_KEY//\"/\\\"}" > "$AUTH_CFG"
cleanup_auth_cfg() {
  rm -f "$AUTH_CFG"
}
trap cleanup_auth_cfg EXIT

usage() {
  cat <<'EOF'
Usage:
  firecrawl-local status
  firecrawl-local scrape <url> [--format markdown,links] [--json] [-o file]
  firecrawl-local search <query> [--limit n] [--sources web,news,images] [--scrape] [--scrape-formats markdown] [--json] [-o file]

Defaults:
  FIRECRAWL_API_URL=http://127.0.0.1:33002
  FIRECRAWL_API_KEY=local-self-hosted
  search --limit=20
EOF
}

need_jq() {
  if ! command -v jq >/dev/null 2>&1; then
    echo "firecrawl-local requires jq" >&2
    exit 127
  fi
}

csv_json() {
  jq -Rnc --arg v "$1" '$v | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0))'
}

post_json() {
  local endpoint="$1"
  local payload="$2"
  curl -fsS \
    -X POST "$API_URL$endpoint" \
    -K "$AUTH_CFG" \
    -H "Content-Type: application/json" \
    -d "$payload"
}

write_or_print() {
  local output="$1"
  local data="$2"
  if [[ -n "$output" ]]; then
    printf '%s\n' "$data" > "$output"
  else
    printf '%s\n' "$data"
  fi
}

cmd="${1:-}"
if [[ -z "$cmd" || "$cmd" == "-h" || "$cmd" == "--help" ]]; then
  usage
  exit 0
fi
shift || true

case "$cmd" in
  status)
    code="$(curl -sS -o /tmp/firecrawl-local-status.out -w '%{http_code}' "$API_URL/" || true)"
    echo "firecrawl-local"
    echo "  url: $API_URL"
    echo "  auth: ${FIRECRAWL_API_KEY:+env FIRECRAWL_API_KEY}${FIRECRAWL_API_KEY:-dummy local-self-hosted}"
    echo "  root_http: $code"
    if [[ "$code" == "200" || "$code" == "302" ]]; then
      echo "  status: ok"
    else
      echo "  status: not reachable"
      exit 1
    fi
    ;;

  scrape)
    need_jq
    url=""
    formats="markdown"
    json_output=0
    output=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --format|-f) formats="${2:?missing format}"; shift 2 ;;
        --json) json_output=1; shift ;;
        --output|-o) output="${2:?missing output file}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --*) echo "Unsupported scrape option: $1" >&2; exit 2 ;;
        *) if [[ -z "$url" ]]; then url="$1"; shift; else echo "Unexpected arg: $1" >&2; exit 2; fi ;;
      esac
    done
    [[ -n "$url" ]] || { echo "Missing URL" >&2; exit 2; }
    formats_json="$(csv_json "$formats")"
    payload="$(jq -nc --arg url "$url" --argjson formats "$formats_json" '{url:$url, formats:$formats}')"
    response="$(post_json /v2/scrape "$payload")"
    if [[ "$json_output" == "1" || "$formats" == *","* ]]; then
      write_or_print "$output" "$response"
    else
      key="$formats"
      extracted="$(jq -r --arg key "$key" '.data[$key] // empty' <<<"$response")"
      if [[ -n "$extracted" ]]; then
        write_or_print "$output" "$extracted"
      else
        write_or_print "$output" "$response"
      fi
    fi
    ;;

  search)
    need_jq
    query=""
    limit=20
    sources=""
    scrape=0
    scrape_formats="markdown"
    json_output=0
    output=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --limit) limit="${2:?missing limit}"; shift 2 ;;
        --sources) sources="${2:?missing sources}"; shift 2 ;;
        --scrape) scrape=1; shift ;;
        --scrape-formats) scrape_formats="${2:?missing scrape formats}"; shift 2 ;;
        --json) json_output=1; shift ;;
        --output|-o) output="${2:?missing output file}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --*) echo "Unsupported search option: $1" >&2; exit 2 ;;
        *) if [[ -z "$query" ]]; then query="$1"; shift; else query="$query $1"; shift; fi ;;
      esac
    done
    [[ -n "$query" ]] || { echo "Missing query" >&2; exit 2; }
    if [[ -n "$sources" && "$scrape" == "1" ]]; then
      payload="$(jq -nc \
        --arg query "$query" \
        --argjson limit "$limit" \
        --argjson sources "$(csv_json "$sources")" \
        --argjson formats "$(csv_json "$scrape_formats")" \
        '{query:$query, limit:$limit, sources:$sources, scrapeOptions:{formats:$formats}}')"
    elif [[ -n "$sources" ]]; then
      payload="$(jq -nc --arg query "$query" --argjson limit "$limit" --argjson sources "$(csv_json "$sources")" '{query:$query, limit:$limit, sources:$sources}')"
    elif [[ "$scrape" == "1" ]]; then
      payload="$(jq -nc --arg query "$query" --argjson limit "$limit" --argjson formats "$(csv_json "$scrape_formats")" '{query:$query, limit:$limit, scrapeOptions:{formats:$formats}}')"
    else
      payload="$(jq -nc --arg query "$query" --argjson limit "$limit" '{query:$query, limit:$limit}')"
    fi
    response="$(post_json /v2/search "$payload")"
    if [[ "$json_output" == "1" ]]; then
      write_or_print "$output" "$response"
    else
      pretty="$(jq -r '
        .data.web[]? | "- " + (.title // "Untitled") + "\n  " + (.url // "") + (if .description then "\n  " + .description else "" end)
      ' <<<"$response")"
      if [[ -n "$pretty" ]]; then
        write_or_print "$output" "$pretty"
      else
        write_or_print "$output" "$response"
      fi
    fi
    ;;

  *)
    echo "Unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
