#!/usr/bin/env pwsh
# vault-groom.ps1 — Windows twin of vault-groom.sh (the gardener's hand).
#
# Same contract as the .sh: feed the canonical playbook
# (03-INFRA/vault-grooming-playbook.md) to an LLM runner for ONE grooming pass.
# On-demand only — never scheduled to self-start (see the playbook: two machines
# grooming the shared vault would collide on git).
#
# Modes:
#   plan   read-only dry pass: propose a tranche, cannot edit/commit (safe)
#   run    operative: compress/merge/archive + commit (+push unless GROOM_NOPUSH=1)
#
# Env: VAULT, GROOM_MODEL, GROOM_NOPUSH=1 (run without push, for observed runs)
#
# TODO(windows-verify): confirm on Windows — `claude` resolves on PATH in pwsh,
# array splat to --allowedTools works, and the audit call in the playbook uses
# `python` not `python3`.

param(
  [ValidateSet('plan', 'run')]
  # Default to the read-only lane, matching vault-groom.sh: a first-time
  # caller with no argument must never land in commit+push mode driven by
  # unreviewed LLM judgement -- `run` (and its push) stays an explicit choice.
  [string]$Mode = 'plan'
)
$ErrorActionPreference = 'Stop'

$Vault    = if ($env:VAULT) { $env:VAULT } else { Join-Path $env:USERPROFILE 'KnowledgeVault' }
$Playbook = '03-INFRA/vault-grooming-playbook.md'
$Model    = if ($env:GROOM_MODEL) { $env:GROOM_MODEL } else { 'claude-sonnet-5' }
$Log      = if ($env:GROOM_LOG) { $env:GROOM_LOG } else { Join-Path $env:TEMP ("vault-groom-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss')) }

Set-Location $Vault

# Read-only lane: no Edit/Write/git -> the plan pass physically cannot mutate.
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

switch ($Mode) {
  'plan' {
    $prompt = "Read $Playbook and execute ONLY steps 1-3 (orient, run the audit heat-map, find candidates with semantic_search). Then OUTPUT a proposed grooming tranche: the notes, the action for each (compress / merge / archive / fix-frontmatter), and one line of why. DO NOT edit, write, move, or commit anything - this is a read-only planning pass."
    claude -p $prompt --model $Model --allowedTools $ReadTools 2>&1 | Tee-Object -FilePath $Log
  }
  'run' {
    if ($env:GROOM_NOPUSH -eq '1') {
      $prompt = "Read $Playbook and execute exactly ONE grooming run following it end to end. Commit atomically per tranche with clear messages. Do NOT push - commits stay local for review."
      claude -p $prompt --model $Model --allowedTools $WriteTools --disallowedTools 'Bash(git push:*)' 2>&1 | Tee-Object -FilePath $Log
    }
    else {
      $prompt = "Read $Playbook and execute exactly ONE grooming run following it end to end. Commit atomically per tranche with clear messages, then push."
      claude -p $prompt --model $Model --allowedTools $WriteTools 2>&1 | Tee-Object -FilePath $Log
    }
  }
}

Write-Host "log: $Log"
