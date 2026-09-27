# Install the /godmode skill for Claude Code on Windows (PowerShell).
#
#   .\install.ps1                  # global: $HOME\.claude\skills\godmode (every project)
#   .\install.ps1 -Project         # this project only: .\.claude\skills\godmode
#   .\install.ps1 -Uninstall
#
# Remote one-liner:
#   irm https://raw.githubusercontent.com/robenn044/claude_godmode/main/install.ps1 | iex
param(
  [switch]$Project,
  [string]$Dir = "",
  [string]$Ref = "main",
  [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$Repo = "robenn044/claude_godmode"

if ($Dir) { $TargetRoot = $Dir }
elseif ($Project) { $TargetRoot = Join-Path (Get-Location) ".claude\skills" }
else { $TargetRoot = Join-Path $HOME ".claude\skills" }
$Dest = Join-Path $TargetRoot "godmode"

if ($Uninstall) {
  if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
  Write-Host "Removed $Dest"; return
}

$Tmp = $null
$Local = if ($PSScriptRoot) { Join-Path $PSScriptRoot "skills\godmode" } else { "" }
if ($Local -and (Test-Path (Join-Path $Local "SKILL.md"))) {
  $Src = $Local
} else {
  $Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("godmode-" + [guid]::NewGuid())
  New-Item -ItemType Directory -Path $Tmp | Out-Null
  $Zip = Join-Path $Tmp "src.zip"
  Write-Host "Downloading $Repo@$Ref ..."
  Invoke-WebRequest -Uri "https://codeload.github.com/$Repo/zip/$Ref" -OutFile $Zip -UseBasicParsing
  Expand-Archive -Path $Zip -DestinationPath $Tmp
  $Root = Get-ChildItem $Tmp -Directory | Select-Object -First 1
  $Src = Join-Path $Root.FullName "skills\godmode"
}

New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
Copy-Item -Recurse $Src $Dest
if ($Tmp) { Remove-Item -Recurse -Force $Tmp }
Write-Host "Installed /godmode -> $Dest"

$Py = $null
foreach ($c in @("python", "py", "python3")) { if (Get-Command $c -ErrorAction SilentlyContinue) { $Py = $c; break } }
if (-not $Py) { Write-Warning "Python 3.8+ not found. Install it from https://python.org (the swarm engine needs it)." }
if ($Py) { & $Py (Join-Path $Dest "scripts\godmode_engine.py") preflight }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
  Write-Host "NOTE: 'claude' is not on PATH. Fine inside Claude Desktop; for the terminal: npm install -g @anthropic-ai/claude-code"
}
Write-Host ""
Write-Host "Done. Restart Claude Code (or open a new Claude Desktop session), then try:  /godmode 3 What is 17 * 23?"
