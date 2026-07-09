#!/usr/bin/env python3
"""Consiglio delle AI: orchestratore locale che convoca CLI consulenti (via
abbonamento flat, mai API a consumo) per brainstorming, challenge e code
review incrociata. Vedi la nota di progetto per l'architettura completa.

MVP (A1): un solo mode (`brainstorm`), un seat, un round.
"""
from __future__ import annotations
import argparse, importlib.util, json, os, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ENGINE_ROOT = Path(__file__).resolve().parent
LEAK_SCAN_DIR = ENGINE_ROOT.parent / "leak-scan"
SESSIONS_DIR = Path.home() / ".local" / "state" / "council" / "sessions"
DEFAULT_TTL_DAYS = 7


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
    return seats


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


def build_brief(question: str, context_path: str | None) -> str:
    parts = [f"Domanda: {question}"]
    if context_path:
        context_text = Path(context_path).read_text(encoding="utf-8")
        parts.append(f"\nContesto:\n{context_text}")
    return "\n".join(parts)


def run_seat(model: str, prompt: str, session_dir: Path) -> tuple[str, dict]:
    proc = subprocess.run(
        ["opencode", "run", prompt, "-m", model, "--format", "json", "--dir", str(session_dir)],
        capture_output=True, text=True, timeout=180,
    )
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
        if event.get("type") == "text":
            text_chunks.append(event["part"]["text"])
        if event.get("type") == "step_finish":
            usage = {"tokens": event["part"].get("tokens"), "cost": event["part"].get("cost")}
    if not text_chunks:
        sys.exit("[council] il seat ha risposto ma senza testo utilizzabile (output vuoto).")
    return "".join(text_chunks), usage


def extract_verdict(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("VERDICT:"):
            return line.removeprefix("VERDICT:").strip()
    return None


def cmd_brainstorm(args: argparse.Namespace) -> None:
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

    brief = build_brief(args.question, args.context)
    egress_gate(brief)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_dir = SESSIONS_DIR / f"council-{slugify(args.question)}-{timestamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "00-brief.md").write_text(brief, encoding="utf-8")

    role_prompt = (ENGINE_ROOT / "prompts" / "brainstorm.md").read_text(encoding="utf-8")
    full_prompt = role_prompt.replace("{brief}", brief)

    print(f"[council] sessione: {session_dir}")
    print(f"[council] seat: {seat_name} ({seat['model']})")

    response, usage = run_seat(seat["model"], full_prompt, session_dir)

    seat_file = session_dir / f"01-{seat_name}-brainstorm.md"
    seat_file.write_text(response, encoding="utf-8")

    verdict = extract_verdict(response)
    if verdict is None:
        print("[council] ATTENZIONE: nessuna riga VERDICT trovata nella risposta.")
        verdict = "(assente)"

    verdict_text = (
        f"# Verdetto\n\nSeat: {seat_name} ({seat['model']})\n"
        f"Round: 1\nVerdict: {verdict}\nCosto: {usage.get('cost')}\n\n"
        f"## Risposta\n\n{response}\n"
    )
    (session_dir / "verdict.md").write_text(verdict_text, encoding="utf-8")

    print(f"[council] verdetto: {verdict}")
    print(f"[council] file: {seat_file}")
    print()
    print(response)


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


def main() -> int:
    ap = argparse.ArgumentParser(prog="council", description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)

    brainstorm = sub.add_parser("brainstorm", help="brainstorming a 1 seat, 1 round (MVP A1)")
    brainstorm.add_argument("question", help="la domanda da porre al consiglio")
    brainstorm.add_argument("--context", metavar="FILE", help="file di contesto da allegare")
    brainstorm.add_argument("--seat", metavar="NAME", help="seat da usare (default: il primo in seats.yaml)")
    brainstorm.add_argument(
        "--allow-training-risk", action="store_true",
        help="consenti l'uso di un seat senza garanzia zero-retention (solo test tecnici)",
    )
    brainstorm.set_defaults(func=cmd_brainstorm)

    clean = sub.add_parser("clean", help="rimuove le sessioni oltre il TTL (retention)")
    clean.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS, help=f"default: {DEFAULT_TTL_DAYS}")
    clean.add_argument("--all", action="store_true", help="rimuove tutte le sessioni, ignora il TTL")
    clean.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
