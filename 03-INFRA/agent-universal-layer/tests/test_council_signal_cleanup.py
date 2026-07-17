"""Regression tests — best-effort cleanup on SIGTERM/SIGINT/interpreter exit.

Security audit finding (LOW, correlated with the codex output-file mkstemp
fix): council.py had no signal handling at all. A SIGTERM (or a crash) left
an active seat subprocess running and the ephemeral session directory
un-cleaned. SIGKILL is uncatchable by any userspace handler and stays out of
scope by construction; SIGTERM, SIGINT and normal interpreter exit are what
these tests cover.

SIGINT hardening covers the supervisor case, not the interactive one: an
interactive Ctrl+C already reaches the vendor CLI child directly (the
kernel delivers SIGINT to the whole foreground process group), so the
child exits on its own without council.py's help. The gap this closes is a
SIGINT delivered only to council.py's own pid -- a supervisor, a timeout
manager, or another agent interrupting just this process.

Tests exercise the cleanup primitives directly (``_best_effort_cleanup``,
``_set_active_proc``, ``_set_active_session``) rather than actually sending
signals to the test process, and monkeypatch ``signal.signal``/``os.kill``
when checking the signal handlers themselves so the pytest process's own
signal disposition is never touched.
"""
from __future__ import annotations

import importlib.util
import io
import signal
import subprocess
import sys
from pathlib import Path

COUNCIL_PATH = Path(__file__).resolve().parents[1] / "council" / "council.py"


def load_council(monkeypatch, tmp_path):
    vault = tmp_path / "KnowledgeVault"
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(vault))
    module_name = f"council_signal_cleanup_under_test_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(module_name, COUNCIL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    mod.SESSIONS_DIR = tmp_path / "sessions"
    mod.SEATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    return mod


class _FakeProc:
    def __init__(self, *, running=True, ignores_terminate=False, raise_on_terminate=False):
        self._running = running
        self.ignores_terminate = ignores_terminate
        self.raise_on_terminate = raise_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self.terminate_calls += 1
        if self.raise_on_terminate:
            raise OSError("simulated: process already reaped")

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.ignores_terminate and self.kill_calls == 0:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        self._running = False
        return 0

    def kill(self):
        self.kill_calls += 1
        self._running = False


def test_cleanup_terminates_active_process_and_removes_session_dir(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-active"
    session_dir.mkdir(parents=True)
    (session_dir / "00-brief.md").write_text("brief", encoding="utf-8")
    proc = _FakeProc(running=True)

    council._set_active_proc(proc)
    council._set_active_session(session_dir, keep=False)
    council._best_effort_cleanup()

    assert proc.terminate_calls == 1
    assert proc.kill_calls == 0
    assert not session_dir.exists()


def test_cleanup_kills_process_that_ignores_terminate(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-stubborn"
    session_dir.mkdir(parents=True)
    proc = _FakeProc(running=True, ignores_terminate=True)

    council._set_active_proc(proc)
    council._set_active_session(session_dir, keep=False)
    council._best_effort_cleanup()

    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1


def test_force_stop_process_tree_uses_taskkill_for_a_windows_shim(monkeypatch, tmp_path):
    """Killing only cmd.exe leaves the npm child alive long enough to keep
    Codex's SQLite files locked. Windows timeouts must terminate the whole
    descendant tree before Council tries to remove the session directory."""
    council = load_council(monkeypatch, tmp_path)
    calls = []

    class FakeWindowsProc:
        pid = 4242

        def __init__(self):
            self.wait_calls = 0
            self.kill_calls = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls += 1
            return 0

        def kill(self):
            self.kill_calls += 1

    proc = FakeWindowsProc()
    monkeypatch.setattr(council.os, "name", "nt")
    monkeypatch.setattr(
        council.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or subprocess.CompletedProcess(argv, 0),
    )

    council._force_stop_process_tree(proc)

    assert calls[0][0] == ["taskkill.exe", "/PID", "4242", "/T", "/F"]
    assert proc.wait_calls == 1
    assert proc.kill_calls == 0


def test_finalize_session_retries_a_transient_windows_file_lock(monkeypatch, tmp_path, capsys):
    """Even after taskkill returns, NTFS may briefly report WinError 32 while
    the child's SQLite handle is closing. A bounded retry must clean the
    ephemeral session instead of leaving it behind as a false hard failure."""
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-transient-lock"
    session_dir.mkdir(parents=True)
    calls = []

    def flaky_rmtree(path):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError(32, "simulated transient Windows lock", str(path))

    monkeypatch.setattr(council.os, "name", "nt")
    monkeypatch.setattr(council.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(council.time, "sleep", lambda seconds: None)

    council._finalize_session(session_dir, keep_session=False)

    assert len(calls) == 2
    assert "cleanup della sessione fallito" not in capsys.readouterr().out


def test_cleanup_preserves_kept_session_dir(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-kept"
    session_dir.mkdir(parents=True)
    marker = session_dir / "00-brief.md"
    marker.write_text("brief", encoding="utf-8")

    council._set_active_proc(None)
    council._set_active_session(session_dir, keep=True)
    council._best_effort_cleanup()

    assert session_dir.exists()
    assert marker.exists()


def test_cleanup_with_no_active_proc_or_session_is_a_safe_no_op(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    council._set_active_proc(None)
    council._set_active_session(None)
    council._best_effort_cleanup()  # must not raise


def test_cleanup_never_raises_even_if_terminate_fails(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-broken-proc"
    session_dir.mkdir(parents=True)
    proc = _FakeProc(running=True, raise_on_terminate=True)

    council._set_active_proc(proc)
    council._set_active_session(session_dir, keep=False)
    council._best_effort_cleanup()  # must not raise despite proc.terminate() raising

    assert not session_dir.exists()


def test_cleanup_runs_at_most_once(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-once"
    session_dir.mkdir(parents=True)
    proc = _FakeProc(running=True)

    council._set_active_proc(proc)
    council._set_active_session(session_dir, keep=False)
    council._best_effort_cleanup()
    assert proc.terminate_calls == 1

    # A second call (e.g. atexit firing after SIGTERM already cleaned up)
    # must be a no-op, not a second terminate/rmtree attempt.
    proc.terminate_calls = 0
    council._best_effort_cleanup()
    assert proc.terminate_calls == 0


def test_sigterm_handler_cleans_up_then_redelivers_the_signal(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-sigterm"
    session_dir.mkdir(parents=True)
    proc = _FakeProc(running=True)
    council._set_active_proc(proc)
    council._set_active_session(session_dir, keep=False)

    restored = {}

    def fake_signal(signum, handler):
        restored["signum"] = signum
        restored["handler"] = handler

    killed = {}

    def fake_kill(pid, signum):
        killed["pid"] = pid
        killed["signum"] = signum

    monkeypatch.setattr(council.signal, "signal", fake_signal)
    monkeypatch.setattr(council.os, "kill", fake_kill)

    council._handle_sigterm(signal.SIGTERM, None)

    # Cleanup ran (process asked to terminate, session dir gone)...
    assert proc.terminate_calls == 1
    assert not session_dir.exists()
    # ...the default disposition was restored...
    assert restored["signum"] == signal.SIGTERM
    assert restored["handler"] == signal.SIG_DFL
    # ...and the signal was re-delivered to self instead of being swallowed.
    assert killed["signum"] == signal.SIGTERM


def test_sigint_handler_cleans_up_then_redelivers_the_signal(monkeypatch, tmp_path):
    """Gemello SIGINT of test_sigterm_handler_cleans_up_then_redelivers_the_signal:
    same cleanup-then-re-raise pattern, wired for the supervisor-SIGINT case."""
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-sigint"
    session_dir.mkdir(parents=True)
    proc = _FakeProc(running=True)
    council._set_active_proc(proc)
    council._set_active_session(session_dir, keep=False)

    restored = {}

    def fake_signal(signum, handler):
        restored["signum"] = signum
        restored["handler"] = handler

    killed = {}

    def fake_kill(pid, signum):
        killed["pid"] = pid
        killed["signum"] = signum

    monkeypatch.setattr(council.signal, "signal", fake_signal)
    monkeypatch.setattr(council.os, "kill", fake_kill)

    council._handle_sigint(signal.SIGINT, None)

    # Cleanup ran (process asked to terminate, session dir gone)...
    assert proc.terminate_calls == 1
    assert not session_dir.exists()
    # ...the default disposition was restored...
    assert restored["signum"] == signal.SIGINT
    assert restored["handler"] == signal.SIG_DFL
    # ...and the signal was re-delivered to self instead of being swallowed.
    assert killed["signum"] == signal.SIGINT


def test_install_shutdown_handlers_wires_sigterm_sigint_and_atexit_without_touching_real_process_state(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    signal_calls = []
    atexit_calls = []

    monkeypatch.setattr(council.signal, "signal", lambda signum, handler: signal_calls.append((signum, handler)))
    monkeypatch.setattr(council.atexit, "register", lambda fn: atexit_calls.append(fn))

    council._install_shutdown_handlers()

    assert signal_calls == [
        (signal.SIGTERM, council._handle_sigterm),
        (signal.SIGINT, council._handle_sigint),
    ]
    assert atexit_calls == [council._best_effort_cleanup]


def test_run_seat_registers_and_clears_active_proc(monkeypatch, tmp_path):
    """run_seat() must expose the child process while it runs (so a SIGTERM
    mid-seat can stop it) and clear it again once the seat is done, on both
    the success and the failure path."""
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-run-seat"
    session_dir.mkdir(parents=True)

    seen_active_proc_while_running = {}

    class _ObservingFakeProc(_FakeProc):
        def __init__(self):
            super().__init__(running=False)  # exits immediately
            self.stdout = iter(["hello\n"])
            self.stderr = iter([])
            self.stdin = io.StringIO()

        def wait(self, timeout=None):
            seen_active_proc_while_running["proc"] = council._ACTIVE_PROC
            return 0

    fake_proc = _ObservingFakeProc()
    monkeypatch.setattr(council.subprocess, "Popen", lambda *a, **k: fake_proc)

    seat = {"cli": "ollama", "model": "test-model"}
    response, _usage = council.run_seat(seat, "hi", session_dir, timeout_seconds=5)

    assert response == "hello\n"
    # While proc.wait() executed, the module had it registered as active...
    assert seen_active_proc_while_running["proc"] is fake_proc
    # ...and run_seat's finally cleared it again afterwards.
    assert council._ACTIVE_PROC is None
