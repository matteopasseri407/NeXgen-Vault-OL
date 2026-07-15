#!/usr/bin/env python3
"""Structured audit trail AND write gate for one vault-groom run.

Called by vault-groom.sh/.ps1 AFTER the write pass returns (successfully or
not -- see --write-exit-code below). Since the 2026-07-13 architect review
("the audit must be the only technical route a write can take to main and
the remotes"), the write pass never runs against the real vault at all: the
wrapper clones the vault into a throwaway working dir, strips its `origin`
remote (push becomes mechanically impossible for any runner, not just a
prompt-level "don't push"), and hands THAT clone to the write pass as its
working directory. This script is what turns that clone's result into
something the real vault ever sees:

  1. Audits the CLONE, not the vault: working tree must be clean when the
     write pass returns, history from BASE must be a strictly linear
     first-parent chain (zero merge commits), and coverage (did the
     approved tranche's targets actually get touched, and nothing else)
     must be clean -- path-exact, no basename guessing, with one narrow
     exception for genuine archive moves (see check_coverage).
  2. On a clean audit, checks freshness (the real vault's HEAD must still
     be exactly BASE -- nobody else wrote to it while grooming ran), then
     PROMOTES: fetches the clone's exact audited commit into the real
     vault and fast-forwards onto it. Nothing here re-executes or re-derives
     what the clone did; it moves the *exact* audited OID.
  3. Only after promotion (trusted, deterministic code, no LLM involved)
     does it append the structured backlog line and optionally publish.

On ANY audit failure (dirty clone, non-linear history, dirty/unparseable
coverage, a stale vault, or the write pass itself exiting non-zero) the
real vault is NEVER touched: the clone is left in place with a quarantine
marker and a loud stderr summary telling the user exactly where it is.

Kept in Python, not duplicated in bash and PowerShell, on purpose: this
project's own history is full of bugs from the same logic drifting between
its .sh and .ps1 twins (see CHANGELOG 0.4.0/Unreleased). One implementation,
called the same way from both wrappers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

BACKLOG_NOTE = "99-INDEX/vault-cleanup-backlog.md"
QUARANTINE_MARKER_NAME = ".GROOM_QUARANTINE.json"

FILENAME_RE = re.compile(r"`([\w./-]+\.md)`")
NO_ACTION_RE = re.compile(r"nessuna azione|no action", re.IGNORECASE)
# Deliberately loose (matches "archive", "archivia", "archiviare", ...): the
# tranche's action column is free Italian/English prose, not an enum -- this
# only gates which targets are ELIGIBLE for the archive-move exception in
# check_coverage, never coverage itself.
ARCHIVE_ACTION_RE = re.compile(r"archivi|archive", re.IGNORECASE)

EXIT_OK = 0
EXIT_INTERNAL_OR_PUBLISH_FAILED = 1
EXIT_AUDIT_BLOCKED = 4
EXIT_STALE = 5


def git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _under_archive_root(path: str, archive_root: str) -> bool:
    root = PurePosixPath(archive_root.strip("/"))
    p = PurePosixPath(path.strip("/"))
    return p == root or root in p.parents


def extract_action_targets(tranche_text: str) -> tuple[set[str], set[str], bool]:
    """Best-effort extraction of the files the approved tranche commits to touching.

    Parses the markdown table PROPOSE_PROMPT requires from both wrapper
    twins: "| Nota | Azione | Perché |". Rows whose action column says
    "nessuna azione" (flag-only, no work planned) don't contribute a target
    -- those are legitimately never expected to show up in files_touched --
    but DO still count as a real, successfully-parsed row (see the third
    return value), so an all-"nessuna azione" tranche is not confused with
    one that failed to parse as a table at all.

    Returns (targets, archive_targets, found_any_row). archive_targets is a
    subset of targets whose row's action column reads as an archive-type
    action -- the only targets check_coverage's archive-move exception ever
    applies to.
    """
    targets: set[str] = set()
    archive_targets: set[str] = set()
    found_any_row = False
    for line in tranche_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 2:
            continue
        note_col, action_col = cols[0], cols[1]
        row_targets = FILENAME_RE.findall(note_col)
        is_no_action = bool(NO_ACTION_RE.search(action_col))
        if row_targets or is_no_action:
            found_any_row = True
        if is_no_action:
            continue
        targets.update(row_targets)
        if row_targets and ARCHIVE_ACTION_RE.search(action_col):
            archive_targets.update(row_targets)
    return targets, archive_targets, found_any_row


def check_coverage(
    plan_record: str, files_touched: list[str], added_paths: set[str], archive_root: str
) -> dict:
    """Bidirectional check: did the approved tranche's targets actually get
    touched, and did the commits touch anything the tranche never approved?

    Path-exact ONLY on the "was this target touched" side -- the old
    basename-anywhere fallback is gone (2026-07-13 review: it could mask a
    genuine miss behind an unrelated same-named file); a target either
    appears verbatim in files_touched (an archived note's ORIGINAL path is
    still "touched" -- it was deleted from there) or it's unaddressed, no
    exceptions.

    The one narrow exception lives on the OTHER side: an ADDED path is
    excused from out_of_scope iff it sits under `archive_root` AND its
    basename equals some archive-type target's basename -- the one
    legitimate shape of "a new file showed up that the tranche didn't
    literally name" this script still trusts (a note moved into the
    archive root keeps its own filename, landing at a NEW path the tranche
    never wrote out), and only for rows whose action actually reads as an
    archive. Everything else touched-but-unmatched is out_of_scope.
    """
    plan_path = Path(plan_record)
    if not plan_path.is_file():
        return {
            "coverage_status": "clean",
            "unaddressed_targets": [],
            "matched_by_archive_move": [],
            "out_of_scope_targets": [],
        }

    tranche_text = plan_path.read_text(encoding="utf-8")
    targets, archive_targets, found_any_row = extract_action_targets(tranche_text)

    if tranche_text.strip() and not found_any_row:
        # Non-empty tranche, zero parseable table rows -- never silently
        # treat this as "nothing to check": the table contract broke
        # somewhere and coverage cannot be verified either direction.
        return {
            "coverage_status": "unparseable",
            "unaddressed_targets": [],
            "matched_by_archive_move": [],
            "out_of_scope_targets": [],
        }

    touched_set = set(files_touched)
    unaddressed = sorted(target for target in targets if target not in touched_set)

    # Match either the unchanged basename (plain move into the archive root)
    # or the playbook's own renaming pattern, "<stem>-archive-<date>.md" --
    # step 4 of the shipped playbook literally instructs that rename, so
    # exact-basename-only would false-quarantine a by-the-book archive.
    archive_target_basenames = {Path(target).name for target in archive_targets}
    archive_target_stems = {Path(target).stem for target in archive_targets}
    def _is_archived_shape(name: str) -> bool:
        if name in archive_target_basenames:
            return True
        return any(name.startswith(stem + "-") for stem in archive_target_stems)
    matched_by_archive_move = sorted(
        path for path in added_paths
        if _under_archive_root(path, archive_root) and _is_archived_shape(Path(path).name)
    )
    sanctioned = set(matched_by_archive_move)
    out_of_scope = sorted(
        touched for touched in files_touched
        if touched not in targets and touched != BACKLOG_NOTE and touched not in sanctioned
    )

    status = "dirty" if (unaddressed or out_of_scope) else "clean"
    return {
        "coverage_status": status,
        "unaddressed_targets": unaddressed,
        "matched_by_archive_move": matched_by_archive_move,
        "out_of_scope_targets": out_of_scope,
    }


def clone_is_clean(clone: str) -> bool:
    return git(clone, "status", "--porcelain").strip() == ""


def clone_head(clone: str) -> str:
    return git(clone, "rev-parse", "HEAD").strip()


def history_is_linear(clone: str, base: str, head: str) -> bool:
    """True iff BASE..HEAD is a strictly linear first-parent chain (zero
    merge commits) -- a merge commit in the clone means the write pass (or
    something running alongside it) did something this gate does not trust
    enough to promote verbatim."""
    if base == head:
        return True
    out = git(clone, "rev-list", "--min-parents=2", f"{base}..{head}")
    return out.strip() == ""


def collect_clone_facts(clone: str, base: str, head: str) -> tuple[list[dict], list[str], set[str]]:
    """Returns (commits, files_touched, added_paths) for BASE..HEAD in the
    clone. --no-renames on purpose: a rename is decomposed into its raw
    delete+add pair so the archive-move exception in check_coverage sees an
    honest ADDED path under the archive root, not a collapsed rename that
    hides which side is new."""
    if base == head:
        return [], [], set()
    log_out = git(clone, "log", "--first-parent", "--format=%H|%s", f"{base}..{head}")
    commits = []
    for line in log_out.splitlines():
        if not line.strip():
            continue
        commit_hash, _, subject = line.partition("|")
        commits.append({"hash": commit_hash, "subject": subject})

    status_out = git(clone, "diff", "--no-renames", "--name-status", f"{base}..{head}")
    touched: set[str] = set()
    added: set[str] = set()
    for line in status_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        touched.add(path)
        if status.startswith("A"):
            added.add(path)
    return commits, sorted(touched), added


def append_backlog_line(vault: str, record: dict) -> tuple[str, bool]:
    """Appends the summary line unless this timestamp is already recorded.

    Returns (line, was_appended). The timestamp-marker check makes this
    idempotent: if the audit call for one run is ever invoked twice (a
    retried step, a re-run after a partial failure), it does not duplicate
    the backlog entry. Only ever called AFTER promotion -- the real vault
    is never touched on a blocked or stale run.
    """
    unaddressed = record.get("unaddressed_targets") or []
    out_of_scope = record.get("out_of_scope_targets") or []
    line = (
        f"- {record['timestamp']}: runner={record['runner']} "
        f"commits={len(record['commits'])} files={len(record['files_touched'])} "
        f"coverage={record.get('coverage_status', 'clean')} "
        f"tranche={record['tranche_sha256'][:12]}"
        + (f" UNADDRESSED={','.join(unaddressed)}" if unaddressed else "")
        + (f" UNPLANNED={','.join(out_of_scope)}" if out_of_scope else "")
        + "\n"
    )
    note_path = Path(vault) / BACKLOG_NOTE
    existing = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
    if f"- {record['timestamp']}:" in existing:
        return line, False

    if existing and not existing.endswith("\n"):
        existing += "\n"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(existing + line, encoding="utf-8")
    return line, True


def commit_backlog(vault: str, timestamp: str) -> str | None:
    """Commits the backlog line -- never pushes. Pushing (if any) happens
    afterward, in one shot via agent_sync.py publish, so it carries this
    commit along with the promoted groom commits instead of leaving it one
    behind."""
    git(vault, "add", BACKLOG_NOTE)
    status = git(vault, "status", "--porcelain", "--", BACKLOG_NOTE)
    if not status.strip():
        # Defensive double-check: append_backlog_line's own timestamp guard
        # is what normally prevents this, but nothing changed either way is
        # not an error.
        return None
    git(vault, "commit", "-m", f"chore(groom): record run {timestamp}")
    return git(vault, "rev-parse", "HEAD").strip()


def run_publish(vault: str, engine_scripts: str) -> subprocess.CompletedProcess:
    """Invokes the engine's own `agent_sync.py publish` against this vault.

    sys.executable, NOT a hardcoded 'python3' string: confirmed breakage on
    stock Windows, where only 'python' (not 'python3') exists on PATH.
    KNOWLEDGE_VAULT_PATH is forced to --vault regardless of the ambient
    environment: this subprocess must publish the vault the promotion just
    fast-forwarded, not whatever agent_sync.py would default to on its own.
    """
    env = dict(os.environ)
    env["KNOWLEDGE_VAULT_PATH"] = vault
    exe = sys.executable
    if not exe or not os.path.exists(exe):
        import shutil
        exe = shutil.which("python3") or shutil.which("python") or "python"
        
    try:
        return subprocess.run(
            [exe, str(Path(engine_scripts) / "agent_sync.py"), "publish"],
            cwd=vault,
            env=env,
            capture_output=True,
            text=True,
        )
    except OSError as e:
        print(f"vault-groom: could not launch agent_sync.py publish: {e}", file=sys.stderr)
        raise


def write_quarantine_marker(clone: str, reason: str, timestamp: str) -> None:
    clone_path = Path(clone)
    if not clone_path.is_dir():
        return
    marker = clone_path / QUARANTINE_MARKER_NAME
    marker.write_text(
        json.dumps({"quarantined_at": timestamp, "reason": reason}, indent=2) + "\n",
        encoding="utf-8",
    )


def print_quarantine_summary(clone: str, reason: str) -> None:
    print("=" * 70, file=sys.stderr)
    print(f"vault-groom: AUDIT BLOCKED -- {reason}", file=sys.stderr)
    print("vault-groom: your vault is UNTOUCHED -- nothing was promoted or pushed.", file=sys.stderr)
    print("vault-groom: the quarantined clone (everything the write pass did) is kept at:", file=sys.stderr)
    print(f"  {clone}", file=sys.stderr)
    print("vault-groom: inspect it by hand, then delete it once you're done with it.", file=sys.stderr)
    print("=" * 70, file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, help="the REAL vault -- never touched until promotion")
    parser.add_argument("--clone", required=True, help="the temp-clone gate's working dir")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--base", required=True, help="the real vault's HEAD before the clone was made")
    parser.add_argument("--archive-root", default="99-ARCHIVE")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--timestamp", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--tranche-sha256", required=True)
    parser.add_argument("--plan-record", required=True)
    parser.add_argument("--propose-log", required=True)
    parser.add_argument("--write-log", required=True)
    parser.add_argument(
        "--write-exit-code", required=True, type=int,
        help="exit code of the write-pass runner CLI -- non-zero blocks promotion unconditionally",
    )
    parser.add_argument(
        "--push-if-clean", action="store_true", default=False,
        help="attempt agent_sync.py publish after a successful promotion; omit to never push this run",
    )
    parser.add_argument(
        "--engine-scripts", default=None,
        help="directory containing agent_sync.py (required together with --push-if-clean)",
    )
    args = parser.parse_args()

    if args.push_if_clean and not args.engine_scripts:
        parser.error("--push-if-clean requires --engine-scripts")

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    record_path = state_dir / f"{args.timestamp}.json"

    record: dict = {
        "timestamp": args.timestamp,
        "runner": args.runner,
        "model": args.model,
        "vault": args.vault,
        "clone": args.clone,
        "base": args.base,
        "tranche_sha256": args.tranche_sha256,
        "plan_record": args.plan_record,
        "propose_log": args.propose_log,
        "write_log": args.write_log,
        "write_exit_code": args.write_exit_code,
        "commits": [],
        "files_touched": [],
        "promoted": False,
        "pushed": False,
    }

    def finish(exit_code: int) -> int:
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"audit record: {record_path}")
        return exit_code

    if args.write_exit_code != 0:
        # set -e (.sh) / $ErrorActionPreference (.ps1) must never let a
        # non-zero write-pass exit skip this bookkeeping -- see both
        # wrappers' WRITE_EXIT / $WriteExitCode capture.
        reason = f"the write-pass runner exited {args.write_exit_code}"
        record["blocked_reason"] = reason
        write_quarantine_marker(args.clone, reason, args.timestamp)
        print_quarantine_summary(args.clone, reason)
        return finish(EXIT_AUDIT_BLOCKED)

    if not Path(args.clone).is_dir():
        reason = f"expected clone directory is missing: {args.clone}"
        record["blocked_reason"] = reason
        print(f"vault-groom: {reason}", file=sys.stderr)
        return finish(EXIT_INTERNAL_OR_PUBLISH_FAILED)

    clean = clone_is_clean(args.clone)
    head = clone_head(args.clone)
    linear = history_is_linear(args.clone, args.base, head)
    commits, files_touched, added_paths = collect_clone_facts(args.clone, args.base, head)
    coverage = check_coverage(args.plan_record, files_touched, added_paths, args.archive_root)

    record["clone_head"] = head
    record["clone_clean"] = clean
    record["history_linear"] = linear
    record["commits"] = commits
    record["files_touched"] = files_touched
    record.update(coverage)

    if coverage["coverage_status"] == "unparseable":
        print(
            "WARNING: the approved tranche was non-empty but no rows parsed as "
            "a '| Nota | Azione | Perché |' table -- coverage cannot be checked: "
            + args.plan_record,
            file=sys.stderr,
        )
    if coverage["unaddressed_targets"]:
        print(
            "WARNING: the approved tranche named these files for action, but "
            "none of them appear among the clone's changed files -- the write "
            "pass may have silently left them undone: " + ", ".join(coverage["unaddressed_targets"]),
            file=sys.stderr,
        )
    if coverage["out_of_scope_targets"]:
        print(
            "WARNING: the clone's commits touched files the approved tranche "
            "never named -- scope creep beyond what was approved: "
            + ", ".join(coverage["out_of_scope_targets"]),
            file=sys.stderr,
        )
    if not clean:
        print("WARNING: the clone's working tree is not clean after the write pass.", file=sys.stderr)
    if not linear:
        print("WARNING: the clone's history from BASE contains a merge commit -- not linear.", file=sys.stderr)

    blocked_reason = None
    if not clean:
        blocked_reason = "clone working tree not clean after the write pass"
    elif not linear:
        blocked_reason = "clone history is not a linear first-parent chain (merge commit found)"
    elif coverage["coverage_status"] != "clean":
        blocked_reason = f"coverage {coverage['coverage_status']}"

    if blocked_reason:
        record["blocked_reason"] = blocked_reason
        write_quarantine_marker(args.clone, blocked_reason, args.timestamp)
        print_quarantine_summary(args.clone, blocked_reason)
        return finish(EXIT_AUDIT_BLOCKED)

    # Freshness: only checked once the clone itself is clean -- a stale
    # BASE is a completely different failure mode (someone else wrote to
    # the vault while grooming ran) from anything the write pass did.
    try:
        real_head = git(args.vault, "rev-parse", "HEAD").strip()
    except subprocess.CalledProcessError as exc:
        reason = f"could not read the real vault's HEAD: {exc}"
        record["blocked_reason"] = reason
        write_quarantine_marker(args.clone, reason, args.timestamp)
        print_quarantine_summary(args.clone, reason)
        return finish(EXIT_INTERNAL_OR_PUBLISH_FAILED)

    record["vault_head_at_audit"] = real_head
    if real_head != args.base:
        reason = "vault moved during grooming"
        record["blocked_reason"] = reason
        write_quarantine_marker(args.clone, reason, args.timestamp)
        print(
            f"vault-groom: {reason} (expected HEAD {args.base}, found {real_head}) -- "
            f"nothing promoted, vault untouched. Clone kept for inspection at {args.clone}",
            file=sys.stderr,
        )
        return finish(EXIT_STALE)

    # Promotion: fetch the clone's EXACT audited OID into the real vault and
    # fast-forward onto it -- no re-execution, no re-derivation.
    try:
        git(args.vault, "fetch", args.clone, args.branch)
        fetched_tip = git(args.vault, "rev-parse", "FETCH_HEAD").strip()
        if fetched_tip != head:
            raise RuntimeError(f"fetched tip {fetched_tip} does not match the audited OID {head}")
        git(args.vault, "merge-base", "--is-ancestor", args.base, fetched_tip)
        git(args.vault, "merge", "--ff-only", fetched_tip)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        reason = f"promotion failed: {exc}"
        record["blocked_reason"] = reason
        write_quarantine_marker(args.clone, reason, args.timestamp)
        print_quarantine_summary(args.clone, reason)
        return finish(EXIT_INTERNAL_OR_PUBLISH_FAILED)

    record["promoted"] = True
    record["promoted_oid"] = head

    # Trusted deterministic code, AFTER promotion: the real vault now has
    # the audited commits, so the backlog line and its commit land directly
    # on top of them, in the real vault.
    line, appended = append_backlog_line(args.vault, record)
    backlog_commit = commit_backlog(args.vault, args.timestamp) if appended else None

    exit_code = EXIT_OK
    if args.push_if_clean:
        try:
            proc = run_publish(args.vault, args.engine_scripts)
        except OSError as exc:
            print(f"vault-groom: could not launch agent_sync.py publish: {exc}", file=sys.stderr)
            exit_code = EXIT_INTERNAL_OR_PUBLISH_FAILED
        else:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            if proc.returncode == 0:
                record["pushed"] = True
            else:
                print(
                    f"vault-groom: publish failed (agent_sync.py exited {proc.returncode}) "
                    "-- the promotion already landed locally, inspect or revert by hand.",
                    file=sys.stderr,
                )
                exit_code = EXIT_INTERNAL_OR_PUBLISH_FAILED

    # The promoted clone is fully merged into the real vault: keeping it
    # would accumulate one full vault copy per apply run in the state dir.
    # Quarantined (failed) clones are deliberately NOT touched by this.
    # GROOM_KEEP_CLONE=1 skips the removal (debugging/inspection).
    if os.environ.get("GROOM_KEEP_CLONE") != "1":
        def _on_rm_error(func, path, exc_info):
            import stat
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except OSError:
                pass
        try:
            # onexc is available in Python 3.12+, fallback to onerror for older versions
            shutil.rmtree(args.clone, onerror=_on_rm_error)
        except OSError as exc:
            print(f"vault-groom: could not remove the promoted clone ({exc}) -- safe to delete by hand: {args.clone}", file=sys.stderr)

    print(f"promoted OID: {head}")
    print(f"backlog line: {line.rstrip()}")
    print(f"backlog commit: {backlog_commit or '(nothing to commit)'}")
    print(f"pushed: {str(record['pushed']).lower()}")

    return finish(exit_code)


if __name__ == "__main__":
    sys.exit(main())
