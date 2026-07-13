#!/usr/bin/env pwsh
# vault-groom.ps1 — Windows twin of vault-groom.sh (the gardener's hand).
#
# Same contract as the .sh: feed the canonical playbook
# (03-INFRA/vault-grooming-playbook.md) to an LLM runner for ONE grooming pass.
# On-demand only — never scheduled to self-start (see the playbook: two machines
# grooming the shared vault would collide on git).
#
# Modes:
#   (default)  guarded run: propose a tranche (read-only), show it to you,
#              require a typed "yes", THEN execute exactly that tranche and
#              commit (+push unless GROOM_NOPUSH=1). Nothing is written or
#              committed without that confirmation.
#   preview    read-only only: propose a tranche and stop. Never executes.
#
# The write pass never re-derives its own tranche: it is handed the exact
# text you approved (with its sha256) and told to execute precisely that.
#
# Env: VAULT, GROOM_MODEL, GROOM_RUNNER (claude|codex|agy, default claude),
#      GROOM_NOPUSH=1 (run without push, for observed runs),
#      GROOM_LOG (override the preview/propose-pass log path),
#      GROOM_STATE_DIR (override where structured audit records land,
#      default $env:USERPROFILE/.local/state/vault-groom)
#
# Runner support mirrors vault-groom.sh: each runner uses ITS OWN verified
# read-only/write-scoping mechanism, not a shared flag set. opencode has no
# per-invocation permission-scoping flag (config-file based, not something
# safe to toggle per run) -- selecting it fails loudly instead of guessing.
#
# TODO(windows-verify): confirm on Windows — `claude`/`codex`/`agy` resolve on
# PATH in pwsh, array splat to --allowedTools works, Get-FileHash on the
# propose log matches sha256sum's output on Linux, and Read-Host correctly
# blocks for the confirmation. Unverified on a physical Windows machine, same
# caveat as the rest of this twin.

param(
  [ValidateSet('guarded', 'preview')]
  [string]$Mode = 'guarded'
)
$ErrorActionPreference = 'Stop'

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

function New-GroomLog([string]$Suffix) {
  $rand = -join ((48..57) + (97..122) | Get-Random -Count 8 | ForEach-Object { [char]$_ })
  Join-Path $env:TEMP "vault-groom-$Ts-$rand.$Suffix.log"
}

Set-Location $Vault

# No manual mode-validity check needed: [ValidateSet('guarded', 'preview')]
# on the param above already rejects anything else before the script body
# ever runs.

switch ($Runner) {
  { $_ -in @('claude', 'codex', 'agy') } { }
  'opencode' {
    Write-Error @'
vault-groom: GROOM_RUNNER=opencode is not supported today.
  opencode has no per-invocation permission-scoping flag (its permission
  model lives in opencode.json's own config, checked once per project, not
  something this script can safely toggle per run): there is no way to
  guarantee the read-only pass is actually read-only, or that the write
  pass doesn't silently inherit broader access than intended. Use claude,
  codex, or agy, or define a dedicated restricted opencode agent profile
  yourself and extend this script's opencode branch to use it explicitly.
'@
    exit 2
  }
  default {
    Write-Error "vault-groom: unknown GROOM_RUNNER '$Runner' (supported: claude, codex, agy)"
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

# Write lane: adds file mutation + git; push is gated separately below.
$WriteTools = @(
  'Read', 'Edit', 'Write', 'Grep', 'Glob',
  'Bash(python3:*)', 'Bash(git:*)', 'Bash(mkdir:*)', 'Bash(mv:*)',
  'mcp__vault-library__semantic_search', 'mcp__vault-library__search_notes',
  'mcp__vault-library__read_note', 'mcp__vault-library__list_related',
  'mcp__vault-library__update_note', 'mcp__vault-library__create_note',
  'mcp__vault-library__append_note'
)

$ProposePrompt = "Read $Playbook and execute ONLY steps 1-3 (orient, run the audit heat-map, find candidates with semantic_search). Then OUTPUT a proposed grooming tranche: the notes, the action for each (compress / merge / archive / fix-frontmatter), and one line of why. DO NOT edit, write, move, or commit anything - this is a read-only planning pass."

function Invoke-Readonly([string]$Prompt, [string]$LogFile) {
  switch ($Runner) {
    'claude' {
      # '' | claude ...: claude has no reason to read stdin in -p mode, but
      # nothing stops it from trying -- and an unredirected native process
      # inherits the console's real stdin (the confirmation gate's future
      # Read-Host source) unless explicitly given something else here.
      # Piping an empty string closes that off, same intent as bash's
      # `< /dev/null` in vault-groom.sh.
      '' | claude -p $Prompt --model $Model --allowedTools $ReadTools 2>&1 | Tee-Object -FilePath $LogFile
    }
    'codex' {
      # -s read-only is a real Codex sandbox policy (verified via
      # `codex exec --help` on Linux; see TODO above for the Windows caveat).
      # Piping $Prompt already gives this command its own stdin, same
      # effect as claude's '' | above.
      $Prompt | codex exec -s read-only -m $Model -C $Vault - 2>&1 | Tee-Object -FilePath $LogFile
    }
    'agy' {
      # Same stdin-isolation reasoning as claude above.
      '' | agy --print --model $Model --mode plan --sandbox --prompt $Prompt 2>&1 | Tee-Object -FilePath $LogFile
    }
  }
}

function Invoke-Write([string]$Prompt, [string]$LogFile) {
  switch ($Runner) {
    'claude' {
      if ($env:GROOM_NOPUSH -eq '1') {
        '' | claude -p $Prompt --model $Model --allowedTools $WriteTools --disallowedTools 'Bash(git push:*)' 2>&1 | Tee-Object -FilePath $LogFile
      }
      else {
        '' | claude -p $Prompt --model $Model --allowedTools $WriteTools 2>&1 | Tee-Object -FilePath $LogFile
      }
    }
    'codex' {
      # NOPUSH on this runner is prompt-level only -- Codex has no
      # per-command block like Claude's --disallowedTools.
      $Prompt | codex exec -s workspace-write -m $Model -C $Vault - 2>&1 | Tee-Object -FilePath $LogFile
    }
    'agy' {
      # NOPUSH here is prompt-level only too, same caveat as codex above.
      '' | agy --print --model $Model --mode accept-edits --prompt $Prompt 2>&1 | Tee-Object -FilePath $LogFile
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

# --- Guarded run: propose, show, confirm, only then execute. ---

$ProposeLog = if ($env:GROOM_LOG) { $env:GROOM_LOG } else { New-GroomLog 'propose' }
Invoke-Readonly $ProposePrompt $ProposeLog

$Tranche = Get-Content -Raw -Path $ProposeLog -ErrorAction SilentlyContinue
if ([string]::IsNullOrWhiteSpace($Tranche)) {
  Write-Error "vault-groom: empty proposal, nothing to review -- aborting."
  exit 1
}
$TrancheHash = (Get-FileHash -Path $ProposeLog -Algorithm SHA256).Hash.ToLower()

Write-Host ""
Write-Host "======================================================================"
Write-Host " Tranche proposta (sha256 $($TrancheHash.Substring(0,12))...) -- leggila prima di confermare"
Write-Host "======================================================================"
Write-Host $Tranche
Write-Host "======================================================================"
Write-Host "Digita esattamente 'yes' per eseguire QUESTA tranche cosi' com'e'."
Write-Host "Qualunque altra risposta annulla: nessuna modifica al vault."
$Answer = Read-Host "Procedere?"

if ($Answer -ne 'yes') {
  # [Console]::Error.WriteLine, not Write-Error: declining is an expected,
  # non-error outcome and must exit 0. Write-Error would be converted into a
  # TERMINATING error by $ErrorActionPreference = 'Stop' above, which stops
  # the script with a non-zero code before the `exit 0` below ever runs --
  # exactly wrong for "the user chose not to proceed, nothing went wrong".
  [Console]::Error.WriteLine("vault-groom: annullato, nessuna modifica al vault.")
  exit 0
}

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$PlanRecord = Join-Path $StateDir "$Ts-plan.txt"
Set-Content -Path $PlanRecord -Value $Tranche -Encoding utf8

if ($env:GROOM_NOPUSH -eq '1') {
  $PushClause = 'Do NOT push -- commits stay local for review.'
  $Pushed = 'false'
}
else {
  $PushClause = 'then push.'
  $Pushed = 'true'
}

$WritePrompt = @"
Read $Playbook. The user already reviewed and approved EXACTLY the following grooming tranche (sha256 $TrancheHash):

---BEGIN APPROVED TRANCHE---
$Tranche
---END APPROVED TRANCHE---

Execute precisely this tranche, nothing more and nothing less -- do not re-derive or expand it. Commit atomically per action with clear messages. $PushClause
"@

$WriteLog = New-GroomLog 'execute'
$HeadBefore = (git rev-parse HEAD).Trim()
Invoke-Write $WritePrompt $WriteLog
$HeadAfter = (git rev-parse HEAD).Trim()

Write-Host ""
$AuditArgs = @(
  '--vault', $Vault,
  '--state-dir', $StateDir,
  '--timestamp', $Ts,
  '--runner', $Runner,
  '--model', $Model,
  '--tranche-sha256', $TrancheHash,
  '--plan-record', $PlanRecord,
  '--head-before', $HeadBefore,
  '--head-after', $HeadAfter,
  '--pushed', $Pushed,
  '--propose-log', $ProposeLog,
  '--write-log', $WriteLog
)
python $AuditScript @AuditArgs
