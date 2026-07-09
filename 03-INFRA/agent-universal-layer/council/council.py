#!/usr/bin/env python3
"""Consiglio delle AI: orchestratore locale che convoca CLI consulenti (via
abbonamento flat, mai API a consumo) per brainstorming, challenge e code
review incrociata. Vedi la nota di progetto per l'architettura completa.

A2: tre mode (brainstorm multi-round, challenge, code-review), prompt di
ruolo dedicati, parsing VERDICT per ogni round.
"""
from __future__ import annotations
import argparse, importlib.util, json, os, re, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

VERDICT_RE = re.compile(r"(?i)verdict\s*:\s*(APPROVE|REVISE|REJECT)\b")
REQUIRED_SEAT_FIELDS = ("vendor", "cli", "model")

ENGINE_ROOT = Path(__file__).resolve().parent
LEAK_SCAN_DIR = ENGINE_ROOT.parent / "leak-scan"
SESSIONS_DIR = Path.home() / ".local" / "state" / "council" / "sessions"
DEFAULT_TTL_DAYS = 7
DEFAULT_MAX_ROUNDS = 3
SEAT_TIMEOUT_SECONDS = 300


def _vault_data_root() -> Path:
    """Stesso pattern AGENT_ENGINE_ROOT/AGENT_VAULT_DATA di agent_sync.py:
    i dati utente (quali seat, quali modelli) vivono nel piano dati, mai nel
    motore pubblico, a prescindere da dove il motore è installato."""
    vault = Path(os.environ.get("KNOWLEDGE_VAULT_PATH") or str(Path.home() / "KnowledgeVault"))
    return Path(os.environ.get("AGENT_VAULT_DATA") or str(vault))


SEATS_PATH = _vault_data_root() / "03-INFRA" / "agent-universal-layer" / "council" / "seats.yaml"


def _load_leak_scan():
    spec = importlib.util.spec_from_file_location("leak_scan", LEAK_SCAN_DIR / "leak_scan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_seats() -> dict:
    if not SEATS_PATH.is_file():
        sys.exit(
            f"[council] nessun seats.yaml nel piano dati ({SEATS_PATH}): espansione inerte.\n"
            f"Copia {ENGINE_ROOT / 'seats.yaml.example'} in quel percorso e personalizzalo."
        )
    data = yaml.safe_load(SEATS_PATH.read_text(encoding="utf-8")) or {}
    seats = data.get("seats", {})
    if not seats:
        sys.exit(f"[council] {SEATS_PATH} è vuoto: espansione inerte, niente da fare.")
    for name, seat in seats.items():
        missing = [f for f in REQUIRED_SEAT_FIELDS if f not in seat]
        if missing:
            sys.exit(f"[council] seat '{name}' in {SEATS_PATH} incompleto: mancano {', '.join(missing)}.")
    return seats


def resolve_seat(args: argparse.Namespace) -> tuple[str, dict]:
    seats = load_seats()
    seat_name = args.seat or next(iter(seats))
    if seat_name not in seats:
        sys.exit(f"[council] seat sconosciuto: {seat_name}. Disponibili: {', '.join(seats)}")
    seat = seats[seat_name]
    if not seat.get("zero_retention", False) and not args.allow_training_risk:
        sys.exit(
            f"[council] STOP: il seat '{seat_name}' NON ha garanzia zero-retention "
            "(i dati inviati possono finire nel training del modello). "
            "Usa --allow-training-risk solo per test tecnici con contenuto non sensibile, "
            "mai per brief reali."
        )
    author_vendor = getattr(args, "author_vendor", None)
    if author_vendor and seat["vendor"].lower() == author_vendor.lower():
        sys.exit(
            f"[council] STOP: il seat '{seat_name}' è dello stesso vendor ({seat['vendor']}) "
            "del materiale in esame. La review incrociata richiede un vendor diverso da chi "
            "ha prodotto il materiale (--author-vendor)."
        )
    return seat_name, seat


def egress_gate(text: str) -> None:
    leak_scan = _load_leak_scan()
    patterns, allow = leak_scan.load_patterns(LEAK_SCAN_DIR / "leak_patterns.yaml")
    units = [
        leak_scan.Unit("brief", i, line)
        for i, line in enumerate(text.splitlines(), 1)
    ]
    findings = leak_scan.scan_units(units, patterns, allow, [])
    blocking = [f for f in findings if f.blocking]
    soft = [f for f in findings if not f.blocking]
    if soft:
        print("[council] avviso (non bloccante): possibili dati identificativi nel brief.")
        for f in soft:
            print(f"  ? {f.label}:{f.lineno}  [{f.kind}]  match={f.redacted}")
    if blocking:
        print("[council] STOP: il brief contiene possibili segreti, invio bloccato.")
        for f in blocking:
            print(f"  ! {f.label}:{f.lineno}  [{f.kind}]  match={f.redacted}")
        sys.exit(1)


def slugify(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text[:40]]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "session"


def _read_or_exit(path_str: str, label: str) -> str:
    path = Path(path_str)
    if not path.is_file():
        sys.exit(f"[council] file {label} non trovato: {path_str}")
    return path.read_text(encoding="utf-8")


def build_brief(question: str | None, context_path: str | None, diff_path: str | None = None) -> str:
    parts = []
    if question:
        parts.append(f"Domanda: {question}")
    if diff_path:
        diff_text = _read_or_exit(diff_path, "diff")
        parts.append(f"\nDiff da revisionare:\n```diff\n{diff_text}\n```")
    if context_path:
        context_text = _read_or_exit(context_path, "di contesto")
        parts.append(f"\nContesto:\n{context_text}")
    if not parts:
        sys.exit("[council] brief vuoto: serve almeno una domanda, un diff o un file di contesto.")
    return "\n".join(parts)


def new_session_dir(label: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_dir = SESSIONS_DIR / f"council-{slugify(label)}-{timestamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def run_seat(model: str, prompt: str, session_dir: Path) -> tuple[str, dict]:
    try:
        proc = subprocess.run(
            ["opencode", "run", prompt, "-m", model, "--format", "json", "--dir", str(session_dir)],
            capture_output=True, text=True, timeout=SEAT_TIMEOUT_SECONDS,
        )
    except OSError as e:
        sys.exit(f"[council] impossibile invocare il seat (brief troppo grande per la riga di comando?): {e}")
    except subprocess.TimeoutExpired:
        sys.exit(f"[council] il seat '{model}' non ha risposto entro {SEAT_TIMEOUT_SECONDS}s: timeout, nessun verdetto per questo round.")
    if proc.returncode != 0:
        sys.exit(f"[council] il seat non ha risposto (exit {proc.returncode}):\n{proc.stderr}")

    text_chunks = []
    usage = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "error":
            sys.exit(f"[council] errore dal seat: {event.get('error')}")
        part = event.get("part") or {}
        if event.get("type") == "text" and "text" in part:
            text_chunks.append(part["text"])
        if event.get("type") == "step_finish":
            usage = {"tokens": part.get("tokens"), "cost": part.get("cost")}
    if not text_chunks:
        sys.exit("[council] il seat ha risposto ma senza testo utilizzabile (output vuoto).")
    return "".join(text_chunks), usage


def extract_verdict(text: str) -> str:
    match = VERDICT_RE.search(text)
    return match.group(1).upper() if match else "(assente)"


def run_rounds(
    seat_name: str, seat: dict, session_dir: Path, mode_label: str, brief: str,
    role_prompt_initial: str, role_prompt_continue: str | None, rounds: int,
) -> tuple[list[str], list[str]]:
    responses: list[str] = []
    verdicts: list[str] = []
    prompt = role_prompt_initial.replace("{brief}", brief)
    for r in range(1, rounds + 1):
        print(f"[council] round {r}/{rounds} — seat: {seat_name} ({seat['model']})")
        response, _usage = run_seat(seat["model"], prompt, session_dir)
        seat_file = session_dir / f"{r:02d}-{seat_name}-{mode_label}-r{r}.md"
        seat_file.write_text(response, encoding="utf-8")
        verdict = extract_verdict(response)
        if verdict == "(assente)":
            print(f"[council] ATTENZIONE: nessuna riga VERDICT trovata nella risposta del round {r}.")
        responses.append(response)
        verdicts.append(verdict)
        print(f"[council] round {r} verdetto: {verdict}")
        if r < rounds:
            if role_prompt_continue is None:
                break
            prompt = role_prompt_continue.replace("{brief}", brief).replace("{previous}", response)
    return responses, verdicts


def write_verdict(session_dir: Path, seat_name: str, seat: dict, mode: str, verdicts: list[str], final_response: str) -> None:
    lines = [
        "# Verdetto", "",
        f"Seat: {seat_name} ({seat['model']})",
        f"Mode: {mode}",
        f"Round eseguiti: {len(verdicts)}",
    ]
    for i, v in enumerate(verdicts, 1):
        lines.append(f"Verdict round {i}: {v}")
    lines.append("")
    lines.append(f"## Risposta finale (round {len(verdicts)})")
    lines.append("")
    lines.append(final_response)
    (session_dir / "verdict.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_mode(args: argparse.Namespace, mode: str, label: str, brief: str, role_initial_name: str, role_continue_name: str | None, rounds: int) -> None:
    seat_name, seat = resolve_seat(args)
    egress_gate(brief)

    session_dir = new_session_dir(label)
    (session_dir / "00-brief.md").write_text(brief, encoding="utf-8")

    role_initial = (ENGINE_ROOT / "prompts" / role_initial_name).read_text(encoding="utf-8")
    role_continue = (
        (ENGINE_ROOT / "prompts" / role_continue_name).read_text(encoding="utf-8")
        if role_continue_name else None
    )

    print(f"[council] sessione: {session_dir}")
    print(f"[council] seat: {seat_name} ({seat['model']}) — mode: {mode}")

    responses, verdicts = run_rounds(seat_name, seat, session_dir, mode, brief, role_initial, role_continue, rounds)

    write_verdict(session_dir, seat_name, seat, mode, verdicts, responses[-1])

    print(f"[council] verdetto finale: {verdicts[-1]}")
    print(f"[council] file: {session_dir / 'verdict.md'}")
    print()
    print(responses[-1])


def cmd_brainstorm(args: argparse.Namespace) -> None:
    rounds = args.rounds
    if rounds > args.max_rounds:
        print(f"[council] --rounds {rounds} supera --max-rounds {args.max_rounds}: eseguo solo {args.max_rounds} round.")
        rounds = args.max_rounds
    brief = build_brief(args.question, args.context)
    _run_mode(args, "brainstorm", args.question, brief, "brainstorm.md", "brainstorm-continue.md", rounds)


def cmd_challenge(args: argparse.Namespace) -> None:
    brief = build_brief(args.plan, args.context)
    _run_mode(args, "challenge", args.plan, brief, "challenge.md", None, 1)


def cmd_code_review(args: argparse.Namespace) -> None:
    brief = build_brief(None, args.context, diff_path=args.diff)
    _run_mode(args, "code-review", Path(args.diff).name, brief, "code-review.md", None, 1)


def cmd_clean(args: argparse.Namespace) -> None:
    if not SESSIONS_DIR.is_dir():
        print("[council] nessuna sessione da pulire.")
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.ttl_days)
    removed = 0
    for session_dir in sorted(SESSIONS_DIR.iterdir()):
        if not session_dir.is_dir():
            continue
        if not args.all:
            mtime = datetime.fromtimestamp(session_dir.stat().st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                continue
        shutil.rmtree(session_dir)
        removed += 1
        print(f"[council] rimossa: {session_dir.name}")
    print(f"[council] pulizia completata: {removed} sessione/i rimossa/e.")


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seat", metavar="NAME", help="seat da usare (default: il primo in seats.yaml)")
    parser.add_argument(
        "--allow-training-risk", action="store_true",
        help="consenti l'uso di un seat senza garanzia zero-retention (solo test tecnici)",
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="council", description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)

    brainstorm = sub.add_parser("brainstorm", help="brainstorming, 1+ round con replica del proponente")
    brainstorm.add_argument("question", help="la domanda da porre al consiglio")
    brainstorm.add_argument("--context", metavar="FILE", help="file di contesto da allegare")
    brainstorm.add_argument("--rounds", type=int, default=1, help="numero di round (default: 1)")
    brainstorm.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, help=f"tetto invalicabile ai round (default: {DEFAULT_MAX_ROUNDS})")
    _add_common_args(brainstorm)
    brainstorm.set_defaults(func=cmd_brainstorm)

    challenge = sub.add_parser("challenge", help="un seat avversario cerca il difetto dominante di un piano")
    challenge.add_argument("plan", help="il piano/proposta da mettere alla prova")
    challenge.add_argument("--context", metavar="FILE", help="file di contesto da allegare")
    _add_common_args(challenge)
    challenge.set_defaults(func=cmd_challenge)

    code_review = sub.add_parser("code-review", help="review incrociata di un diff (vendor diverso da chi l'ha scritto)")
    code_review.add_argument("diff", metavar="DIFF_FILE", help="file col diff/patch da revisionare")
    code_review.add_argument("--context", metavar="FILE", help="file di contesto aggiuntivo (es. perché del cambiamento)")
    code_review.add_argument("--author-vendor", metavar="VENDOR", help="vendor che ha scritto il codice: blocca se coincide col vendor del seat")
    _add_common_args(code_review)
    code_review.set_defaults(func=cmd_code_review)

    clean = sub.add_parser("clean", help="rimuove le sessioni oltre il TTL (retention)")
    clean.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS, help=f"default: {DEFAULT_TTL_DAYS}")
    clean.add_argument("--all", action="store_true", help="rimuove tutte le sessioni, ignora il TTL")
    clean.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
