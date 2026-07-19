[CmdletBinding()]
param(
  [Parameter(Position = 0)] [string]$Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($Path) -or $Path -in @('-h', '--help')) {
  Write-Output 'Uso: agent-open-folder <percorso-assoluto-cartella>'
  Write-Output 'Apre una cartella locale nel file manager predefinito.'
  exit $(if ($Path) { 0 } else { 2 })
}

if (-not [System.IO.Path]::IsPathRooted($Path)) {
  Write-Error 'Errore: serve un percorso assoluto di una cartella esistente.'
  exit 2
}

try {
  $Folder = [System.IO.Path]::GetFullPath($Path)
} catch {
  Write-Error 'Errore: il percorso della cartella non è valido.'
  exit 2
}

if (-not (Test-Path -LiteralPath $Folder -PathType Container)) {
  Write-Error 'Errore: il percorso non indica una cartella esistente.'
  exit 2
}

Start-Process -FilePath 'explorer.exe' -ArgumentList @($Folder)
Write-Output "Cartella aperta: $Folder"
