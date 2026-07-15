[CmdletBinding()]
param(
  [Parameter(Position = 0)] [string]$BranchOrAction,
  [Parameter(Position = 1)] [string]$TagName
)

$ErrorActionPreference = 'Stop'

function Stop-EnginePush([string]$Message) {
  Write-Error "engine-push: $Message"
  exit 1
}

function Invoke-Git([string[]]$Arguments, [switch]$AllowFailure) {
  # Windows PowerShell 5 promotes native stderr to a terminating
  # NativeCommandError when the script-wide preference is Stop. Capture the
  # command's real exit code first so AllowFailure remains meaningful for
  # expected negative checks such as verify-tag and verify-commit.
  $previousErrorPreference = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $output = & git -C $script:EngineRepo @Arguments 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousErrorPreference
  }
  if (($code -ne 0) -and -not $AllowFailure) {
    Stop-EnginePush "git $($Arguments -join ' ') failed"
  }
  return [pscustomobject]@{ Code = $code; Output = @($output) }
}

function Invoke-LeakScan([string[]]$Arguments) {
  & $script:Python[0] @($script:Python | Select-Object -Skip 1) $script:LeakScan `
    --patterns $script:Patterns --denylist $script:Denylist --repo $script:EngineRepo @Arguments
  return $LASTEXITCODE
}

$HomeDir = [Environment]::GetFolderPath('UserProfile')
$script:EngineRepo = if ($env:ENGINE_REPO) { $env:ENGINE_REPO } else { Join-Path $HomeDir 'NeXgen-Engine' }
$Vault = if ($env:KNOWLEDGE_VAULT_PATH) { $env:KNOWLEDGE_VAULT_PATH } else { Join-Path $HomeDir 'KnowledgeVault' }
$script:LeakScan = Join-Path $script:EngineRepo '03-INFRA\agent-universal-layer\leak-scan\leak_scan.py'
$script:Patterns = Join-Path $script:EngineRepo '03-INFRA\agent-universal-layer\leak-scan\leak_patterns.yaml'
$script:Denylist = Join-Path $Vault '03-INFRA\agent-universal-layer\sanitize\deny.txt'

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $script:Python = @($py.Source, '-3') }
else {
  $python = Get-Command python -ErrorAction SilentlyContinue
  if (-not $python) { Stop-EnginePush 'Python 3 is not available' }
  $script:Python = @($python.Source)
}

if (-not (Test-Path -LiteralPath (Join-Path $script:EngineRepo '.git'))) { Stop-EnginePush "engine repository not found ($script:EngineRepo)" }
if (-not (Test-Path -LiteralPath $script:LeakScan)) { Stop-EnginePush "leak scanner missing ($script:LeakScan)" }
if (-not (Test-Path -LiteralPath $script:Patterns)) { Stop-EnginePush "public leak patterns missing ($script:Patterns)" }
if (-not (Test-Path -LiteralPath $script:Denylist)) { Stop-EnginePush "private denylist missing ($script:Denylist)" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Stop-EnginePush 'git is not available' }

$realUrl = ((Invoke-Git @('config', '--get', 'remote.origin.url')).Output -join '').Trim()
if (-not $realUrl -or $realUrl.StartsWith('PUSH-DISABLED')) { Stop-EnginePush 'remote.origin.url is missing or disabled' }
$authorEmail = ((Invoke-Git @('config', '--get', 'user.email')).Output -join '').Trim()
if ($authorEmail -notmatch '^[^@\s]+@[^@\s]+$') { Stop-EnginePush 'git user.email is missing or invalid' }
$signingEnabled = ((Invoke-Git @('config', '--bool', 'commit.gpgsign') -AllowFailure).Output -join '').Trim()
if ($signingEnabled -ne 'true') { Stop-EnginePush 'commit.gpgsign is not enabled' }
$signingKey = ((Invoke-Git @('config', '--get', 'user.signingkey') -AllowFailure).Output -join '').Trim()
if (-not $signingKey) { Stop-EnginePush 'user.signingkey is not configured' }

$fetch = Invoke-Git @('fetch', 'origin') -AllowFailure
if ($fetch.Code -ne 0) { Stop-EnginePush 'fetch origin failed, no push attempted' }

if ($BranchOrAction -eq 'tag') {
  if (-not $TagName) { Stop-EnginePush 'tag mode requires a tag name' }
  if ((Invoke-Git @('rev-parse', '-q', '--verify', "refs/tags/$TagName") -AllowFailure).Code -ne 0) { Stop-EnginePush "local tag not found: $TagName" }
  if ((Invoke-Git @('verify-tag', $TagName) -AllowFailure).Code -ne 0) { Stop-EnginePush "tag signature verification failed: $TagName" }
  $target = ((Invoke-Git @('rev-list', '-n1', $TagName)).Output -join '').Trim()
  if ((Invoke-Git @('verify-commit', $target) -AllowFailure).Code -ne 0) { Stop-EnginePush "tag target commit signature verification failed: $target" }
  if ((Invoke-Git @('merge-base', '--is-ancestor', $target, 'origin/main') -AllowFailure).Code -ne 0) { Stop-EnginePush "tag target is not on origin/main: $target" }
  $messageFile = [IO.Path]::GetTempFileName()
  try {
    ((Invoke-Git @('tag', '-l', '--format=%(contents)', $TagName)).Output -join "`n") | Set-Content -LiteralPath $messageFile -Encoding utf8
    if ((Invoke-LeakScan @('--message', $messageFile)) -ne 0) { Stop-EnginePush "tag message leak check failed: $TagName" }
  } finally {
    Remove-Item -LiteralPath $messageFile -Force -ErrorAction SilentlyContinue
  }
  if ((Invoke-Git @('push', $realUrl, "refs/tags/${TagName}:refs/tags/${TagName}") -AllowFailure).Code -ne 0) { Stop-EnginePush 'tag push was rejected; never force a public tag' }
  Write-Host "engine-push: push tag $TagName OK ($($target.Substring(0, 7)))"
  exit 0
}

$branch = if ($BranchOrAction) { $BranchOrAction } else { ((Invoke-Git @('rev-parse', '--abbrev-ref', 'HEAD')).Output -join '').Trim() }
if (-not $branch -or $branch -eq 'HEAD') { Stop-EnginePush 'cannot determine branch from a detached HEAD' }

if ((Invoke-Git @('rev-parse', '--verify', '-q', "origin/$branch") -AllowFailure).Code -eq 0) {
  $range = "origin/$branch..$branch"
} else {
  $baseResult = Invoke-Git @('merge-base', $branch, 'origin/main') -AllowFailure
  $base = ($baseResult.Output -join '').Trim()
  $range = if (($baseResult.Code -eq 0) -and $base) { "$base..$branch" } else { $branch }
}

$introduced = Invoke-Git @('rev-list', '--reverse', $range) -AllowFailure
if ($introduced.Code -ne 0) { Stop-EnginePush "cannot enumerate introduced commits in $range" }
foreach ($commit in @($introduced.Output)) {
  $oid = ([string]$commit).Trim()
  if (-not $oid) { continue }
  if ((Invoke-Git @('verify-commit', $oid) -AllowFailure).Code -ne 0) {
    Stop-EnginePush "commit signature verification failed: $oid"
  }
}

if ((Invoke-LeakScan @('--commit-range', $range)) -ne 0) { Stop-EnginePush "push blocked by leak scan in $range" }
if ((Invoke-Git @('push', $realUrl, "${branch}:${branch}") -AllowFailure).Code -ne 0) { Stop-EnginePush 'push rejected; fetch and resolve without force' }
Write-Host "engine-push: push $branch OK ($(((Invoke-Git @('rev-parse', '--short', $branch)).Output -join '').Trim()))"
