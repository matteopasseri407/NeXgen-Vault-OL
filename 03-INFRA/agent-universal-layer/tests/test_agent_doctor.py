"""Test 14 su agent-doctor.sh: smoke in sandbox.

Nota di adattamento (dichiarata, non nascosta): agent-doctor.sh fa MOLTI check
contro infrastruttura reale hardcoded (porte di servizi locali, un backend
remoto raggiunto via SSH, variabili d'ambiente con URL/token, nomi di skill
specifiche dell'installazione) che NON possono mai passare in una
sandbox sintetica, a prescindere da quanto sia "sana" — non e' questo che il
test #14 deve provare. Il comportamento davvero testabile e specifico della
sandbox e' il meccanismo di drift-detection: iniettare un drift controllabile
(qui: un symlink rotto sotto ~/.agents/skills, esattamente l'esempio del
design) deve far AUMENTARE il numero di FAIL rispetto a una baseline nella
STESSA sandbox. Confrontiamo baseline vs drift, non l'exit code assoluto.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from conftest import run_agent_doctor, run_agent_sync

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="agent-doctor.sh is POSIX-only; B2.5 Windows coverage is agent_sync.py smoke.",
)


def _parse_summary(stdout: str) -> tuple[int, int, int]:
    m = re.search(r"PASS=(\d+)\s+WARN=(\d+)\s+FAIL=(\d+)", stdout)
    assert m, f"riga di riepilogo --summary non trovata:\n{stdout}"
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def test_doctor_smoke_detects_injected_broken_symlink(sandbox):
    sb = sandbox
    for rt in (".claude/skills", ".codex/skills"):
        (sb.home / rt).mkdir(parents=True, exist_ok=True)
    priming = run_agent_sync(sb, "apply")
    assert priming.returncode == 0, priming.stdout + priming.stderr

    baseline = run_agent_doctor(sb, "--summary")
    base_pass, base_warn, base_fail = _parse_summary(baseline.stdout)

    # drift iniettato: un symlink rotto/pendente nell'hub (l'esempio del design)
    hub_link = sb.hub / "fake-skill-a"
    assert hub_link.is_symlink(), "precondizione: l'hub deve gia' avere il link creato da agent-sync"
    hub_link.unlink()
    hub_link.symlink_to(sb.home / "questo-target-non-esiste-affatto")

    drifted = run_agent_doctor(sb, "--summary")
    drift_pass, drift_warn, drift_fail = _parse_summary(drifted.stdout)

    assert drift_fail > base_fail, (
        f"il drift iniettato non ha aumentato i FAIL (baseline={base_fail}, dopo drift={drift_fail})\n"
        f"baseline: {baseline.stdout}\ndrift: {drifted.stdout}"
    )
    assert "FAIL:" in drifted.stdout
    assert "fake-skill-a" in drifted.stdout or "ROTTE" in drifted.stdout, drifted.stdout


def test_vault_library_probe_uses_mcp_protocol_headers():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")

    assert "code -X OPTIONS" in bash
    assert "Accept: application/json, text/event-stream" in bash
    assert "httpcode $env:VAULT_LIBRARY_URL" in powershell
    assert "Accept = \"application/json, text/event-stream\"" in powershell
    assert '"Options"' in powershell
