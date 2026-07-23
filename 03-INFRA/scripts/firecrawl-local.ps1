[CmdletBinding()]
param(
  [Parameter(Position = 0)] [string]$Command,
  [Parameter(ValueFromRemainingArguments = $true)] [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$ApiUrl = if ($env:FIRECRAWL_API_URL) { $env:FIRECRAWL_API_URL.TrimEnd('/') } else { 'http://127.0.0.1:33002' }
$ApiKey = if ($env:FIRECRAWL_API_KEY) { $env:FIRECRAWL_API_KEY } else { 'local-self-hosted' }

function Show-Usage {
  @'
Usage:
  firecrawl-local status
  firecrawl-local scrape <url> [--format markdown,links] [--json] [-o file]
  firecrawl-local search <query> [--limit n] [--sources web,news,images] [--scrape] [--scrape-formats markdown] [--json] [-o file]

Defaults:
  FIRECRAWL_API_URL=http://127.0.0.1:33002
  FIRECRAWL_API_KEY=local-self-hosted
  search --limit=20
'@
}

function Get-Headers {
  return @{ Authorization = "Bearer $ApiKey" }
}

function Invoke-FirecrawlJson([string]$Endpoint, [hashtable]$Payload) {
  $body = $Payload | ConvertTo-Json -Depth 20 -Compress
  return Invoke-RestMethod -Uri ($ApiUrl + $Endpoint) -Method Post -Headers (Get-Headers) -ContentType 'application/json' -Body $body
}

function Write-Result($Value, [string]$OutputPath, [switch]$AsJson) {
  if ($AsJson -or $Value -isnot [string]) {
    $text = if ($Value -is [string]) { $Value } else { $Value | ConvertTo-Json -Depth 30 }
  } else {
    $text = [string]$Value
  }
  if ($OutputPath) {
    Set-Content -LiteralPath $OutputPath -Value $text -Encoding utf8
  } else {
    Write-Output $text
  }
}

if ([string]::IsNullOrWhiteSpace($Command) -or $Command -in @('-h', '--help')) {
  Show-Usage
  exit 0
}

switch ($Command) {
  'status' {
    $code = 0
    try {
      $response = Invoke-WebRequest -Uri ($ApiUrl + '/') -Method Get -MaximumRedirection 0 -TimeoutSec 5
      $code = [int]$response.StatusCode
    } catch {
      if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } else { $code = 0 }
    }
    Write-Output 'firecrawl-local'
    Write-Output "  url: $ApiUrl"
    Write-Output "  auth: $(if ($env:FIRECRAWL_API_KEY) { 'env FIRECRAWL_API_KEY' } else { 'dummy local-self-hosted' })"
    Write-Output "  root_http: $code"
    if ($code -in @(200, 302)) { Write-Output '  status: ok'; exit 0 }
    Write-Output '  status: not reachable'
    exit 1
  }
  'scrape' {
    $url = $null; $formats = 'markdown'; $output = $null; $asJson = $false
    for ($i = 0; $i -lt $Arguments.Count; $i++) {
      switch ($Arguments[$i]) {
        '--format' { $i++; $formats = $Arguments[$i] }
        '-f' { $i++; $formats = $Arguments[$i] }
        '--json' { $asJson = $true }
        '--output' { $i++; $output = $Arguments[$i] }
        '-o' { $i++; $output = $Arguments[$i] }
        '-h' { Show-Usage; exit 0 }
        '--help' { Show-Usage; exit 0 }
        { $_ -like '--*' } { Write-Error "Unsupported scrape option: $($_)"; exit 2 }
        default { if (-not $url) { $url = $Arguments[$i] } else { Write-Error "Unexpected arg: $($_)"; exit 2 } }
      }
    }
    if (-not $url) { Write-Error 'Missing URL'; exit 2 }
    $payload = @{ url = $url; formats = @($formats -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
    $response = Invoke-FirecrawlJson '/v2/scrape' $payload
    if ($asJson -or $formats.Contains(',')) { Write-Result $response $output -AsJson; exit 0 }
    $property = $response.data.PSObject.Properties[$formats]
    $value = if ($property) { $property.Value } else { $null }
    Write-Result $(if ($value) { $value } else { $response }) $output -AsJson:$false
    exit 0
  }
  'search' {
    $queryParts = @(); $limit = 20; $sources = $null; $scrape = $false; $scrapeFormats = 'markdown'; $output = $null; $asJson = $false
    for ($i = 0; $i -lt $Arguments.Count; $i++) {
      switch ($Arguments[$i]) {
        '--limit' { $i++; $limit = [int]$Arguments[$i] }
        '--sources' { $i++; $sources = $Arguments[$i] }
        '--scrape' { $scrape = $true }
        '--scrape-formats' { $i++; $scrapeFormats = $Arguments[$i] }
        '--json' { $asJson = $true }
        '--output' { $i++; $output = $Arguments[$i] }
        '-o' { $i++; $output = $Arguments[$i] }
        '-h' { Show-Usage; exit 0 }
        '--help' { Show-Usage; exit 0 }
        { $_ -like '--*' } { Write-Error "Unsupported search option: $($_)"; exit 2 }
        default { $queryParts += $Arguments[$i] }
      }
    }
    $query = $queryParts -join ' '
    if (-not $query) { Write-Error 'Missing query'; exit 2 }
    $payload = @{ query = $query; limit = $limit }
    if ($sources) { $payload.sources = @($sources -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
    if ($scrape) { $payload.scrapeOptions = @{ formats = @($scrapeFormats -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) } }
    $response = Invoke-FirecrawlJson '/v2/search' $payload
    if ($asJson) { Write-Result $response $output -AsJson; exit 0 }
    $rows = @($response.data.web | ForEach-Object {
      $title = if ($_.title) { $_.title } else { 'Untitled' }
      $urlValue = if ($_.url) { $_.url } else { '' }
      $line = '- ' + $title + "`n  " + $urlValue
      if ($_.description) { $line += "`n  " + $_.description }
      $line
    })
    Write-Result $(if ($rows) { $rows -join "`n" } else { $response | ConvertTo-Json -Depth 30 }) $output -AsJson:$false
    exit 0
  }
  default { Write-Error "Unknown command: $Command"; Show-Usage; exit 2 }
}
