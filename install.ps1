# One-step setup for Windows 10 and 11. Paste this into PowerShell:
#
#   irm https://raw.githubusercontent.com/Linking-Experiences-with-Opportunities/reelforge/main/install.ps1 | iex
#
# Installs everything reelforge needs (with winget), then starts the local
# helper that marketing.pathion.dev talks to, and starts it again at every
# sign-in. Safe to run again: it updates what is there. No administrator
# rights are needed, except that Windows may ask once while Tesseract installs.
#
#   $env:REELFORGE_SRC = 'C:\path\to\checkout'   use an existing checkout (never pulled)
#   $env:REELFORGE_REF = 'branch'                install a branch other than main
#   $env:REELFORGE_ORIGINS = 'url url'           extra pages allowed to use the helper
#   $env:REELFORGE_SKIP_WHISPER = '1'            skip speech transcription (about 2 GB)

& {
  $ErrorActionPreference = 'Continue'
  $ProgressPreference = 'SilentlyContinue'

  $Repo = 'https://github.com/Linking-Experiences-with-Opportunities/reelforge.git'
  $HomeDir = Join-Path $HOME '.reelforge'
  $OwnSrc = [bool]$env:REELFORGE_SRC
  $Src = if ($OwnSrc) { $env:REELFORGE_SRC } else { Join-Path $HomeDir 'src' }
  $Ref = if ($env:REELFORGE_REF) { $env:REELFORGE_REF } else { 'main' }
  $Port = 47821
  $Log = Join-Path $HomeDir 'helper.log'
  $Runner = Join-Path $HomeDir 'run-helper.ps1'
  $Startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'Reelforge helper.lnk'
  $ToolBin = Join-Path $HOME '.local\bin'
  $Tesseract = Join-Path $env:ProgramFiles 'Tesseract-OCR'

  function Say($text) { Write-Host ''; Write-Host $text -ForegroundColor Cyan }
  function Fail($text) { Write-Host ''; Write-Host $text -ForegroundColor Red; throw 'reelforge setup stopped' }
  # A PowerShell single-quoted literal: paths like C:\Users\O'Brien go into the runner script.
  function Lit($text) { "'" + ($text -replace "'", "''") + "'" }
  function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }
  function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$ToolBin;$machine;$user;$Tesseract"
  }
  function Winget-Install($id) {
    winget install --exact --id $id --silent --disable-interactivity `
      --accept-package-agreements --accept-source-agreements --source winget | Out-Host
    return $LASTEXITCODE -eq 0
  }

  New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
  Refresh-Path

  # ---- tools ---------------------------------------------------------------
  if (-not (Have 'winget')) {
    Fail 'This needs winget (App Installer). Install "App Installer" from the Microsoft Store, then run this again.'
  }
  Say 'Installing the video tools (ffmpeg, yt-dlp, git, uv)'
  $tools = [ordered]@{ ffmpeg = 'Gyan.FFmpeg'; 'yt-dlp' = 'yt-dlp.yt-dlp'; git = 'Git.Git'; uv = 'astral-sh.uv' }
  foreach ($name in $tools.Keys) {
    if (-not (Have $name)) {
      if (-not (Winget-Install $tools[$name])) { Fail "Could not install $name. Send this window's text to Max." }
      Refresh-Path
    }
  }
  if (-not (Have 'tesseract')) {
    Say 'Installing Tesseract (reads text in reels). Windows may ask you to allow it.'
    if (-not (Winget-Install 'UB-Mannheim.TesseractOCR')) { Write-Host 'Tesseract did not install; reelforge works without it.' }
    Refresh-Path
  }
  foreach ($name in 'ffmpeg', 'ffprobe', 'git', 'uv') {
    if (-not (Have $name)) { Fail "$name is installed but not found yet. Close PowerShell, open it again, and run this again." }
  }

  # ---- reelforge -----------------------------------------------------------
  if (-not $OwnSrc) {
    Say 'Getting reelforge'
    if (Test-Path (Join-Path $Src '.git')) {
      git -C $Src fetch --quiet origin $Ref
      git -C $Src checkout --quiet -B $Ref "origin/$Ref"
    } else {
      git clone --quiet --branch $Ref $Repo $Src
    }
    if ($LASTEXITCODE -ne 0) { Fail 'Could not download reelforge. Check the internet connection and run this again.' }
  }
  if (-not (Test-Path (Join-Path $Src 'pyproject.toml'))) { Fail "No reelforge checkout at $Src" }

  # A running helper holds reelforge.exe open, which stops uv from replacing it.
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like '*run-helper.ps1*' -or $_.CommandLine -match 'reelforge(\.exe)?"?\s+serve') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 1

  uv tool install --quiet --force --python 3.12 -e $Src
  if ($LASTEXITCODE -ne 0) { Fail "reelforge did not install. Send this window's text to Max." }

  if (-not $env:REELFORGE_SKIP_WHISPER -and -not (Have 'whisper')) {
    Say 'Installing speech transcription (large download, a few minutes)'
    uv tool install --quiet --python 3.12 openai-whisper
    if ($LASTEXITCODE -ne 0) { Write-Host 'Transcription did not install; reelforge works without it.' }
  }

  # ---- the helper ----------------------------------------------------------
  $origins = @('https://marketing.pathion.dev') + @(($env:REELFORGE_ORIGINS -split '\s+') | Where-Object { $_ })
  $originArgs = ($origins | ForEach-Object { "'--allow-origin', $(Lit $_)" }) -join ', '
  $pull = ''
  if (-not $OwnSrc) {
    # Editable install: a pull is the whole update. Never let it block startup.
    $pull = @"
  `$before = git -C $(Lit $Src) rev-parse HEAD 2>`$null
  git -C $(Lit $Src) fetch --quiet origin $(Lit $Ref) 2>`$null
  if (`$LASTEXITCODE -eq 0) { git -C $(Lit $Src) merge --quiet --ff-only $(Lit "origin/$Ref") 2>`$null | Out-Null }
  if (`$before -ne (git -C $(Lit $Src) rev-parse HEAD 2>`$null)) { uv tool install --quiet --force --python 3.12 -e $(Lit $Src) 2>`$null | Out-Null }
"@
  }
  $runnerText = @"
# Written by install.ps1. Started at sign-in from the Startup folder, without a window.
# Runs the helper, and starts it again whenever it stops (the page's Update button stops it).
`$ErrorActionPreference = 'Continue'
while (`$true) {
  `$env:Path = $(Lit "$ToolBin;") + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User') + $(Lit ";$Tesseract")
$pull
  `$serveArgs = @('serve', '--port', '$Port', $originArgs)
  & reelforge @serveArgs *>> $(Lit $Log)
  Start-Sleep -Seconds 3
}
"@
  Set-Content -Path $Runner -Value $runnerText -Encoding UTF8

  # Start at sign-in. conhost --headless keeps it out of sight, even where Windows
  # Terminal is the default console (it ignores -WindowStyle Hidden).
  $command = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`""
  $shell = New-Object -ComObject WScript.Shell
  $link = $shell.CreateShortcut($Startup)
  $link.TargetPath = Join-Path $env:SystemRoot 'System32\conhost.exe'
  $link.Arguments = $command
  $link.WorkingDirectory = $HomeDir
  $link.WindowStyle = 7
  $link.Description = 'Reelforge helper for marketing.pathion.dev'
  $link.Save()

  Say 'Starting the helper'
  Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\conhost.exe') -ArgumentList $command -WorkingDirectory $HomeDir
  foreach ($i in 1..40) {
    try {
      $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
      if ($health.ok) {
        Say 'Done. Go back to marketing.pathion.dev, open Tools, and click Connect.'
        return
      }
    } catch { }
    Start-Sleep -Seconds 1
  }
  Fail "The helper did not start. Send $Log to Max."
}
