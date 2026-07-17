#!/usr/bin/env pwsh
# vault-groom.ps1 — Windows twin of vault-groom.sh (the gardener's hand).
#
# Same contract as the .sh: feed the canonical playbook
# (03-INFRA/vault-grooming-playbook.md) to an LLM runner for ONE grooming pass.
# On-demand only — never scheduled to self-start (see the playbook: two machines
# grooming the shared vault would collide on git).
#
# Modes:
#   (default)  preview: read-only, runs the propose pass, prints the
#              tranche, exits. NEVER prompts, NEVER writes — a bare
#              invocation can never modify the vault.
#   preview    explicit alias of the default above.
#   apply      the guarded flow: propose a tranche (read-only), show it to
#              you, require a typed "yes", THEN execute exactly that
#              tranche inside a throwaway clone and (only if the audit
#              below is clean) promote it into the real vault. Nothing is
#              written to the real vault without that confirmation AND a
#              clean audit — see "the temp-clone gate" below.
#
# The write pass never re-derives its own tranche: it is handed the exact
# text you approved (with its sha256, taken from the PLAN_RECORD file's raw
# bytes) and told to execute precisely that. A cheap TOCTOU re-hash right
# before the write pass starts catches the plan record changing underneath
# the confirmation.
#
# The temp-clone gate (2026-07-13 architect review, mirrors vault-groom.sh's
# own comment — see that file for the full rationale). After "yes" is
# confirmed, this script clones the vault into a fresh dir and IMMEDIATELY
# removes that clone's `origin` remote, making `git push` mechanically
# impossible for the write pass to reach anywhere real. claude's
# --disallowedTools stays as belt-and-suspenders, not the actual guarantee.
# vault_groom_audit.py audits the CLONE (clean working tree, linear
# history, path-exact coverage) and, only if that passes AND the real
# vault hasn't moved since the clone was made, fetches the clone's exact
# audited commit into the real vault and fast-forwards onto it. Any audit
# failure leaves the real vault untouched and quarantines the clone.
#
# Env: VAULT, GROOM_MODEL, GROOM_RUNNER (claude|codex|agy, default claude),
#      GROOM_NOPUSH=1 (skip the auto-publish step after a clean promotion —
#      the promoted commits stay local for review),
#      GROOM_LOG (override the preview/propose-pass log path),
#      GROOM_STATE_DIR (override where structured audit records AND the
#      temp-clone gate's clones land, default
#      $env:USERPROFILE/.local/state/vault-groom),
#      AGENT_ENGINE_ROOT (see Resolve-EngineScripts below).
#
# Runner support mirrors vault-groom.sh: each runner uses ITS OWN verified
# read-only/write-scoping mechanism, not a shared flag set. opencode has no
# per-invocation permission-scoping flag (config-file based, not something
# safe to toggle per run) -- selecting it fails loudly instead of guessing.
#
# Prompt delivery on Windows (2026-07-13 review): npm-installed CLIs
# commonly resolve to a *.cmd shim, and PowerShell invoking a *.cmd routes
# the command line through cmd.exe's OWN reparsing of |, <, and embedded
# newlines -- exactly what the propose-pass tranche (a markdown table full
# of `|`) and the write prompt (which embeds that tranche, newlines and
# all) contain. Resolve-CliInvoker prefers a *.ps1 shim or a real *.exe
# (both invoked directly by the engine/OS, no cmd.exe in the loop) when one
# is on PATH; codex is unaffected (it already delivers its prompt via
# stdin, not an argument, see Invoke-Readonly/-Write below).
#
# TODO(windows-verify): confirm on Windows — `claude`/`codex`/`agy` resolve on
# PATH in pwsh, array splat to --allowedTools works, Get-FileHash on the
# plan-record file matches sha256sum's output on Linux, and Read-Host
# correctly blocks for the confirmation. Unverified on a physical Windows
# machine, same caveat as the rest of this twin.

param(
  [ValidateSet('preview', 'apply', 'plan', 'run', 'guarded')]
  [string]$Mode = 'preview'
)
$ErrorActionPreference = 'Stop'

# Force UTF-8 for every byte that crosses a native-process boundary. Without
# this, Windows PowerShell decodes a runner's stdout with the console's OEM
# code page and re-encodes piped stdin the same way, so a tranche with any
# non-ASCII char (accented Italian, an em dash) round-trips through
# propose-log -> Get-Content -> plan record as mojibake, and the sha256 the
# audit hashes stops matching what the human approved. The bash twin never
# had this problem (its pipes are raw bytes); UTF-8 makes the twins agree.
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
try { [Console]::OutputEncoding = $Utf8NoBom } catch { }
try { [Console]::InputEncoding = $Utf8NoBom } catch { }
$OutputEncoding = $Utf8NoBom

$Vault    = if ($env:VAULT) { $env:VAULT } else { Join-Path $env:USERPROFILE 'KnowledgeVault' }
$Playbook = '03-INFRA/vault-grooming-playbook.md'
# Resolved via $PSScriptRoot (this script's own real directory), not $Vault:
# vault_groom_audit.py is pure engine tooling shipped in the same commit as
# this wrapper, never a per-user content file like the playbook above. A
# $Vault-relative path only works after agent-sync has propagated this file
# into the vault -- see vault-groom.sh's matching comment for the real bug
# this fixes (found on the first live Linux run, 2026-07-13).
$AuditScript = Join-Path $PSScriptRoot 'vault_groom_audit.py'
$Model    = if ($env:GROOM_MODEL) { $env:GROOM_MODEL } else { 'claude-sonnet-5' }
$Runner   = if ($env:GROOM_RUNNER) { $env:GROOM_RUNNER } else { 'claude' }
$StateDir = if ($env:GROOM_STATE_DIR) { $env:GROOM_STATE_DIR } else { Join-Path $env:USERPROFILE '.local/state/vault-groom' }
$Ts       = Get-Date -Format 'yyyyMMdd-HHmmss'

# vault_scripts ($PSScriptRoot, where this wrapper and vault_groom_audit.py
# live) and engine_scripts (where agent_sync.py lives, needed only for the
# post-promotion publish step) are NOT guaranteed co-located -- see
# vault-groom.sh's matching comment. Same intent, adapted to what Windows
# actually has available: AGENT_ENGINE_ROOT wins when set; Windows has no
# resolvable symlink for the persisted-engine-root indirection (utils()
# ships a *.cmd wrapper there, not a symlink agent_sync.py's Env class can
# read), so the sibling-of-self assumption is the fallback, with a clear,
# loud error if agent_sync.py isn't actually there when it's needed.
function Resolve-EngineScripts {
  if ($env:AGENT_ENGINE_ROOT) { return (Join-Path $env:AGENT_ENGINE_ROOT 'scripts') }
  return $PSScriptRoot
}
$EngineScripts = Resolve-EngineScripts

function New-GroomLog([string]$Suffix) {
  # $env:TEMP is Windows-only and unset on POSIX pwsh (this repo's own CI
  # runs pwsh on ubuntu-latest too, not just windows-latest) --
  # [System.IO.Path]::GetTempPath() resolves the OS temp dir cross-platform
  # via .NET itself, same intent as vault-groom.sh's own `/tmp` default.
  $rand = -join ((48..57) + (97..122) | Get-Random -Count 8 | ForEach-Object { [char]$_ })
  $tempDir = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
  Join-Path $tempDir "vault-groom-$Ts-$rand.$Suffix.log"
}

Set-Location $Vault

# 'plan' and 'run'/'guarded' are retired names, kept in the param ValidateSet
# ONLY so they reach this explicit rejection with a migration hint instead
# of PowerShell's generic "not in the ValidateSet" error.
switch ($Mode) {
  'plan' {
    [Console]::Error.WriteLine("vault-groom: 'plan' is retired -- use 'preview' (or run with no argument, same thing).")
    exit 2
  }
  { $_ -in @('run', 'guarded') } {
    [Console]::Error.WriteLine("vault-groom: 'run'/'guarded' is retired -- use 'apply'.")
    exit 2
  }
}

switch ($Runner) {
  { $_ -in @('claude', 'codex', 'agy') } { }
  'opencode' {
    # [Console]::Error.WriteLine, not Write-Error: same reasoning as the
    # decline branch further down -- under $ErrorActionPreference = 'Stop',
    # Write-Error is a TERMINATING error, so the `exit 2` right after it
    # would never run and the process would exit with PowerShell's own
    # default error code instead of the "2" this contract promises.
    [Console]::Error.WriteLine(@'
vault-groom: GROOM_RUNNER=opencode is not supported today.
  opencode has no per-invocation permission-scoping flag (its permission
  model lives in opencode.json's own config, checked once per project, not
  something this script can safely toggle per run): there is no way to
  guarantee the read-only pass is actually read-only, or that the write
  pass doesn't silently inherit broader access than intended. Use claude,
  codex, or agy, or define a dedicated restricted opencode agent profile
  yourself and extend this script's opencode branch to use it explicitly.
'@)
    exit 2
  }
  default {
    # Same [Console]::Error.WriteLine reasoning as the opencode branch above.
    [Console]::Error.WriteLine("vault-groom: unknown GROOM_RUNNER '$Runner' (supported: claude, codex, agy)")
    exit 2
  }
}

# Read-only lane: no Edit/Write/git -> the propose pass physically cannot mutate.
$ReadTools = @(
  'Read', 'Grep', 'Glob', 'Bash(python3:*)',
  'mcp__vault-library__semantic_search', 'mcp__vault-library__search_notes',
  'mcp__vault-library__read_note', 'mcp__vault-library__recent_activity',
  'mcp__vault-library__list_related', 'mcp__vault-library__get_start_here'
)

# Write lane: adds file mutation + git; push is never in this list's reach
# (see Invoke-Write's claude branch below) -- but the REAL guarantee is the
# temp-clone gate itself (the clone's `origin` remote is removed before the
# write pass ever starts): even a `git push` it somehow ran would have
# nowhere real to go.
$WriteTools = @(
  'Read', 'Edit', 'Write', 'Grep', 'Glob',
  'Bash(python3:*)', 'Bash(git:*)', 'Bash(mkdir:*)', 'Bash(mv:*)',
  'mcp__vault-library__semantic_search', 'mcp__vault-library__search_notes',
  'mcp__vault-library__read_note', 'mcp__vault-library__list_related',
  'mcp__vault-library__update_note', 'mcp__vault-library__create_note',
  'mcp__vault-library__append_note'
)

$ProposePrompt = "Read $Playbook and execute ONLY steps 1-3 (orient, run the audit heat-map, find candidates with semantic_search). ALSO run the structural map, python $(Join-Path $PSScriptRoot 'vault-map.py') --vault $Vault --check, and treat orphan notes and broken wikilinks it reports as first-class tranche candidates (orphan -> link-or-archive, broken link -> fix at the source). Then OUTPUT a proposed grooming tranche as a markdown table with EXACTLY these columns: | Nota | Azione | Perché | - one row per note, action is compress / merge / archive / fix-frontmatter / fix-link / nessuna azione, last column is one line of why. DO NOT edit, write, move, or commit anything - this is a read-only planning pass."

function Resolve-CliInvoker([string]$Name) {
  # Prefer whichever of Get-Command's matches is NOT a cmd.exe indirection:
  # a *.ps1 shim (run directly by the PowerShell engine) or a real *.exe
  # (run directly by the OS, CreateProcess, no shell reparsing). Falls back
  # to bare-name resolution -- which may hit a *.cmd shim -- only when
  # neither exists, same as the pre-2026-07-13 behavior.
  $candidates = Get-Command $Name -All -ErrorAction SilentlyContinue
  $direct = $candidates | Where-Object { $_.Source -match '\.(ps1|exe)$' } | Select-Object -First 1
  if ($direct) { return $direct.Source }
  return $Name
}

function Invoke-Readonly([string]$Prompt, [string]$LogFile) {
  switch ($Runner) {
    'claude' {
      # '' | claude ...: claude has no reason to read stdin in -p mode, but
      # nothing stops it from trying -- and an unredirected native process
      # inherits the console's real stdin (the confirmation gate's future
      # Read-Host source) unless explicitly given something else here.
      # Piping an empty string closes that off, same intent as bash's
      # `< /dev/null` in vault-groom.sh.
      $cli = Resolve-CliInvoker 'claude'
      '' | & $cli -p $Prompt --model $Model --allowedTools $ReadTools 2>&1 | Tee-Object -FilePath $LogFile
    }
    'codex' {
      # -s read-only is a real Codex sandbox policy (verified via
      # `codex exec --help` on Linux; see TODO above for the Windows caveat).
      # Piping $Prompt already gives this command its own stdin, same
      # effect as claude's '' | above -- and since the prompt travels via
      # stdin, not an argument, codex was never exposed to the cmd.exe
      # reparsing problem Resolve-CliInvoker exists for.
      $cli = Resolve-CliInvoker 'codex'
      $Prompt | & $cli exec -s read-only -m $Model -C $Vault - 2>&1 | Tee-Object -FilePath $LogFile
    }
    'agy' {
      # Same stdin-isolation reasoning as claude above.
      $cli = Resolve-CliInvoker 'agy'
      '' | & $cli --print --model $Model --mode plan --sandbox --prompt $Prompt 2>&1 | Tee-Object -FilePath $LogFile
    }
  }
}

function Invoke-Write([string]$Prompt, [string]$LogFile, [string]$Workdir) {
  switch ($Runner) {
    'claude' {
      # --disallowedTools is unconditional now, not gated on GROOM_NOPUSH:
      # push is never the write pass's decision to make, full stop --
      # belt-and-suspenders on top of the temp-clone gate's origin-less
      # clone, which is what actually makes a push land nowhere real.
      $cli = Resolve-CliInvoker 'claude'
      '' | & $cli -p $Prompt --model $Model --allowedTools $WriteTools --disallowedTools 'Bash(git push:*)' 2>&1 | Tee-Object -FilePath $LogFile
    }
    'codex' {
      # -C $Workdir, not $Vault: the write pass's working directory is the
      # temp-clone gate's clone, never the real vault. No push instruction
      # here is prompt-level only -- Codex has no per-command block like
      # Claude's --disallowedTools; the clone having no `origin` remote is
      # the actual guarantee for every runner alike.
      $cli = Resolve-CliInvoker 'codex'
      $Prompt | & $cli exec -s workspace-write -m $Model -C $Workdir - 2>&1 | Tee-Object -FilePath $LogFile
    }
    'agy' {
      # Prompt-level only too, same caveat as codex above.
      $cli = Resolve-CliInvoker 'agy'
      '' | & $cli --print --model $Model --mode accept-edits --prompt $Prompt 2>&1 | Tee-Object -FilePath $LogFile
    }
  }
}

if ($Mode -eq 'preview') {
  $Log = if ($env:GROOM_LOG) { $env:GROOM_LOG } else { New-GroomLog 'preview' }
  Invoke-Readonly $ProposePrompt $Log
  Write-Host ""
  Write-Host "log: $Log"
  exit 0
}

# --- apply: propose, show, confirm, only then execute (inside a throwaway
# clone -- see the temp-clone gate comment at the top of this file). ---

$ProposeLog = if ($env:GROOM_LOG) { $env:GROOM_LOG } else { New-GroomLog 'propose' }
Invoke-Readonly $ProposePrompt $ProposeLog

$Tranche = Get-Content -Raw -Path $ProposeLog -ErrorAction SilentlyContinue
if ([string]::IsNullOrWhiteSpace($Tranche)) {
  # [Console]::Error.WriteLine, not Write-Error: same reasoning as the
  # decline branch further down -- Write-Error is a TERMINATING error under
  # $ErrorActionPreference = 'Stop', so the `exit 1` right after it would
  # never run, and vault-groom.sh's matching path is a plain `echo >&2` with
  # no such risk -- the twins must actually exit 1 the same way.
  [Console]::Error.WriteLine("vault-groom: empty proposal, nothing to review -- aborting.")
  exit 1
}

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$PlanRecord = Join-Path $StateDir "$Ts-plan.txt"
# Byte-for-byte parity with vault-groom.sh's `printf '%s\n' "$(cat ...)"`:
# normalize CRLF/CR to LF and strip trailing newlines (what bash's $(...)
# does), then write UTF-8 no-BOM with exactly one trailing LF via .NET --
# NOT Set-Content, whose Windows CRLF and BOM handling would make the plan
# record (and therefore its sha256) differ from the Linux twin's for the
# very same approved tranche. Everything downstream -- the banner, the
# write prompt, the TOCTOU re-hash -- reads this normalized value.
$Tranche = ($Tranche -replace "`r`n", "`n") -replace "`r", "`n"
$Tranche = $Tranche.TrimEnd("`n")
[System.IO.File]::WriteAllText($PlanRecord, $Tranche + "`n", $Utf8NoBom)
# Hash the FILE's raw bytes, not the in-memory string: the confirmation
# banner and the write prompt both quote this hash, and the TOCTOU
# re-check below re-hashes the same file -- all three must agree on
# exactly what they're hashing.
$TrancheHash = (Get-FileHash -Path $PlanRecord -Algorithm SHA256).Hash.ToLower()

Write-Host ""
Write-Host "======================================================================"
Write-Host " Tranche proposta (sha256 $($TrancheHash.Substring(0,12))...) -- leggila prima di confermare"
Write-Host "======================================================================"
Write-Host $Tranche
Write-Host "======================================================================"
Write-Host "Digita esattamente 'yes' per eseguire QUESTA tranche cosi' com'e'."
Write-Host "Qualunque altra risposta annulla: nessuna modifica al vault."
$Answer = Read-Host "Procedere?"

# -cne, NOT -ne: PowerShell's -ne is case-INSENSITIVE for strings by
# default, which would silently accept "Yes"/"YES" -- the banner above
# promises a literal "yes", matching vault-groom.sh's `[ "$ANSWER" != "yes" ]`
# (bash string comparison is always case-sensitive).
if ($Answer -cne 'yes') {
  # [Console]::Error.WriteLine, not Write-Error: declining is an expected,
  # non-error outcome and must exit 0. Write-Error would be converted into a
  # TERMINATING error by $ErrorActionPreference = 'Stop' above, which stops
  # the script with a non-zero code before the `exit 0` below ever runs --
  # exactly wrong for "the user chose not to proceed, nothing went wrong".
  [Console]::Error.WriteLine("vault-groom: annullato, nessuna modifica al vault.")
  exit 0
}

# TOCTOU guard: re-hash the plan record right before it's handed to the
# write pass. Cheap, and it closes the window between "the human approved
# this text" and "the write pass reads it" -- if anything touched the file
# in between, abort loudly instead of executing whatever it now contains.
$Rehash = (Get-FileHash -Path $PlanRecord -Algorithm SHA256).Hash.ToLower()
if ($Rehash -ne $TrancheHash) {
  [Console]::Error.WriteLine("vault-groom: plan record changed after approval (expected $TrancheHash, got $Rehash) -- aborting, zero writes. Re-run and re-approve.")
  exit 1
}

# --- The temp-clone gate. ---
$Base = (git rev-parse HEAD).Trim()
# git status --porcelain returns no lines (empty/$null) when clean, one or
# more lines otherwise -- both are directly usable as a PowerShell boolean.
$DirtyStatus = git status --porcelain
if ($DirtyStatus) {
  [Console]::Error.WriteLine("vault-groom: the vault's working tree is not clean (uncommitted changes present) -- commit or stash them first. Aborting: the temp-clone gate needs a clean HEAD to clone from, zero writes made.")
  exit 1
}
$Branch = (git rev-parse --abbrev-ref HEAD).Trim()

$CloneDir = Join-Path $StateDir "$Ts-clone-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
git clone -q $Vault $CloneDir
if ($LASTEXITCODE -ne 0) {
  [Console]::Error.WriteLine("vault-groom: git clone into the temp-clone gate failed -- aborting, zero writes.")
  exit 1
}
# Immediately -- before the write pass ever runs a single command -- remove
# the clone's `origin` remote. This is what makes `git push` mechanically
# impossible for ANY runner in ANY mode, replacing trust in prompt wording
# with an actual missing destination.
git -C $CloneDir remote remove origin

# Resolves the archive root the write pass's "archive" actions actually
# move notes under, so vault_groom_audit.py's coverage check knows where a
# legitimate archive-move is allowed to land. Read from the vault's own
# playbook config (an `archive_root: <path>` frontmatter-style line);
# falls back to the documented default when absent or unparseable.
function Resolve-ArchiveRoot([string]$PlaybookPath) {
  if (Test-Path -LiteralPath $PlaybookPath -PathType Leaf) {
    $match = Select-String -LiteralPath $PlaybookPath -Pattern '^archive_root:\s*(.+)$' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($match) {
      return ($match.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'"))
    }
  }
  return '99-ARCHIVE'
}
$ArchiveRoot = Resolve-ArchiveRoot $Playbook

$WritePrompt = @"
Read $Playbook. The user already reviewed and approved EXACTLY the grooming tranche recorded at $PlanRecord (sha256 $TrancheHash) -- that file is the single source of truth if anything below looks truncated or reformatted; the same text is reproduced here for convenience:

---BEGIN APPROVED TRANCHE---
$Tranche
---END APPROVED TRANCHE---

Execute precisely this tranche, nothing more and nothing less -- do not re-derive or expand it. Commit atomically per action with clear messages. Do NOT push -- pushing is decided separately after this run by mechanically checking what the commits actually touched, never by you. Before finishing, re-read the approved tranche row by row and end your response with an explicit checklist, one line per note that has a real action (skip rows marked "nessuna azione"): DONE (with the commit it landed in) or NOT DONE (with the concrete reason). Every actioned row must appear on that list -- do not let anything go unmentioned.
"@

$WriteLog = New-GroomLog 'execute'
# $WriteExitCode is captured defensively, not left to $ErrorActionPreference:
# a non-zero write pass must still reach the audit call below so it can
# quarantine the clone and write the audit record. Under PowerShell 7.3+'s
# $PSNativeCommandUseErrorActionPreference, a failing native command can
# surface as a TERMINATING error with $ErrorActionPreference = 'Stop' --
# the try/catch is what stops that from skipping this bookkeeping, matching
# vault-groom.sh's `|| WRITE_EXIT=$?`.
$WriteExitCode = 0
Push-Location $CloneDir
try {
  Invoke-Write $WritePrompt $WriteLog $CloneDir
  $WriteExitCode = $LASTEXITCODE
} catch {
  $WriteExitCode = if ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }
} finally {
  Pop-Location
}
if ($null -eq $WriteExitCode) { $WriteExitCode = 0 }

# --push-if-clean is omitted entirely under GROOM_NOPUSH=1: the audit
# script then never attempts to publish, even after a clean promotion.
$AuditArgs = @(
  '--vault', $Vault,
  '--clone', $CloneDir,
  '--branch', $Branch,
  '--base', $Base,
  '--archive-root', $ArchiveRoot,
  '--state-dir', $StateDir,
  '--timestamp', $Ts,
  '--runner', $Runner,
  '--model', $Model,
  '--tranche-sha256', $TrancheHash,
  '--plan-record', $PlanRecord,
  '--propose-log', $ProposeLog,
  '--write-log', $WriteLog,
  '--write-exit-code', $WriteExitCode,
  '--engine-scripts', $EngineScripts
)
if ($env:GROOM_NOPUSH -ne '1') {
  $AuditArgs += '--push-if-clean'
}

Write-Host ""
python $AuditScript @AuditArgs
exit $LASTEXITCODE
