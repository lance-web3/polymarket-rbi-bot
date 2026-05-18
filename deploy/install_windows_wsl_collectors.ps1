<# 
.SYNOPSIS
Sets up the Polymarket quote collectors on Windows with WSL doing the Python work.

.DESCRIPTION
This script prepares the WSL virtualenv, validates the collector inputs, and can
install NSSM services that survive Windows reboots. It mirrors the old macOS
LaunchAgents, but the services run `wsl.exe` and execute the project from inside
Ubuntu.

Run from an elevated Windows PowerShell prompt.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\deploy\install_windows_wsl_collectors.ps1 -InstallServices -StartServices

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\deploy\install_windows_wsl_collectors.ps1 -SkipVenv -InstallServices -NoNonsports
#>

[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$WslUser = "a",
    [string]$ProjectDir = "/home/a/polymarket-rbi-bot",
    [string]$NssmPath = "nssm",
    [string]$ServicePrefix = "PolymarketRbiBot",
    [int]$IntervalSeconds = 30,
    [switch]$SkipVenv,
    [switch]$InstallServices,
    [switch]$StartServices,
    [switch]$NoNonsports,
    [switch]$RefreshMainWatchlist,
    [switch]$RegenerateNonsportsWatchlist,
    [switch]$PromptForServiceCredential
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$WslExe = Join-Path $env:WINDIR "System32\wsl.exe"
$ProgramDataDir = Join-Path $env:ProgramData "polymarket-rbi-bot"
$LogDir = Join-Path $ProgramDataDir "logs"

function Write-Step {
    param([string]$Message)
    Write-Host "[polymarket-rbi-bot] $Message"
}

function Invoke-Checked {
    param(
        [string]$Description,
        [scriptblock]$Script
    )
    Write-Step $Description
    & $Script
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Wsl {
    param([string[]]$Arguments)
    & $WslExe @("-d", $Distro, "-u", $WslUser, "--cd", $ProjectDir, "--") @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE`: $($Arguments -join ' ')"
    }
}

function Invoke-WslShell {
    param([string]$Command)
    & $WslExe -d $Distro -u $WslUser --cd $ProjectDir -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "WSL shell command failed with exit code $LASTEXITCODE`: $Command"
    }
}

function Test-WslPath {
    param([string]$Path)
    & $WslExe -d $Distro -u $WslUser -- test -e $Path | Out-Null
    return $LASTEXITCODE -eq 0
}

function Resolve-Nssm {
    $cmd = Get-Command $NssmPath -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        (Join-Path $PSScriptRoot "nssm.exe"),
        (Join-Path $PSScriptRoot "bin\nssm.exe"),
        "C:\nssm\nssm.exe",
        "C:\Program Files\nssm\nssm.exe",
        "C:\Program Files (x86)\nssm\nssm.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    throw "NSSM was not found. Install NSSM or pass -NssmPath C:\path\to\nssm.exe."
}

function Test-ServiceInstalled {
    param([string]$Name)
    return $null -ne (Get-Service -Name $Name -ErrorAction SilentlyContinue)
}

function Set-NssmValue {
    param(
        [string]$Nssm,
        [string]$ServiceName,
        [string]$Key,
        [string[]]$Values
    )
    & $Nssm set $ServiceName $Key @Values | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "nssm set $ServiceName $Key failed with exit code $LASTEXITCODE"
    }
}

function Install-CollectorService {
    param(
        [string]$Nssm,
        [string]$Name,
        [string]$DisplayName,
        [string]$Watchlist,
        [string]$Output,
        [string]$Stdout,
        [string]$Stderr,
        [System.Management.Automation.PSCredential]$Credential
    )

    $appParameters = @(
        "-d", $Distro,
        "-u", $WslUser,
        "--cd", $ProjectDir,
        "--",
        "$ProjectDir/.venv/bin/python",
        "-m", "deploy.collect_quotes",
        "--watchlist", $Watchlist,
        "--interval-seconds", "$IntervalSeconds",
        "--use-clob-order-books",
        "--output", $Output
    ) -join " "

    if (Test-ServiceInstalled $Name) {
        Write-Step "Updating existing NSSM service $Name"
        Set-NssmValue $Nssm $Name "Application" @($WslExe)
    }
    else {
        Write-Step "Installing NSSM service $Name"
        & $Nssm install $Name $WslExe | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "nssm install $Name failed with exit code $LASTEXITCODE"
        }
    }

    Set-NssmValue $Nssm $Name "AppDirectory" @((Split-Path $WslExe))
    Set-NssmValue $Nssm $Name "AppParameters" @($appParameters)
    Set-NssmValue $Nssm $Name "DisplayName" @($DisplayName)
    Set-NssmValue $Nssm $Name "Description" @("Runs the Polymarket RBI Bot quote collector inside WSL.")
    Set-NssmValue $Nssm $Name "Start" @("SERVICE_AUTO_START")
    Set-NssmValue $Nssm $Name "AppStdout" @($Stdout)
    Set-NssmValue $Nssm $Name "AppStderr" @($Stderr)
    Set-NssmValue $Nssm $Name "AppRotateFiles" @("1")
    Set-NssmValue $Nssm $Name "AppRotateOnline" @("1")
    Set-NssmValue $Nssm $Name "AppRotateBytes" @("10485760")
    Set-NssmValue $Nssm $Name "AppThrottle" @("30000")
    Set-NssmValue $Nssm $Name "AppExit" @("Default", "Restart")

    if ($Credential) {
        $plainPassword = $Credential.GetNetworkCredential().Password
        Set-NssmValue $Nssm $Name "ObjectName" @($Credential.UserName, $plainPassword)
    }
}

if (-not (Test-Path $WslExe)) {
    throw "wsl.exe was not found at $WslExe"
}

$distroNames = (& $WslExe -l -q) | ForEach-Object { $_.Trim([char]0).Trim() } | Where-Object { $_ }
if ($LASTEXITCODE -ne 0) {
    throw "Unable to list WSL distributions."
}
if ($distroNames -notcontains $Distro) {
    throw "WSL distro '$Distro' was not found. Installed distros: $($distroNames -join ', ')"
}

Invoke-Checked "Validating WSL project directory" {
    & $WslExe -d $Distro -u $WslUser -- test -d $ProjectDir
}

Invoke-WslShell "mkdir -p data/quote_collection"

if (-not $SkipVenv) {
    Invoke-WslShell "python3 -m venv .venv"
    Invoke-Wsl @(".venv/bin/python", "-m", "pip", "install", "--upgrade", "pip")
    Invoke-Wsl @(".venv/bin/python", "-m", "pip", "install", "-r", "requirements.txt")
}

if ($RefreshMainWatchlist) {
    Invoke-WslShell ".venv/bin/python -m deploy.scan_markets --limit 200 --top 50 > data/scan_shortlist.json"
}

$mainWatchlist = "$ProjectDir/data/scan_shortlist.json"
$nonsportsWatchlist = "$ProjectDir/data/scan_shortlist_nonsports.json"

if (-not (Test-WslPath $mainWatchlist)) {
    throw "Missing data/scan_shortlist.json. Restore it from backup or run with -RefreshMainWatchlist."
}

$installNonsports = -not $NoNonsports
if ($installNonsports -and -not (Test-WslPath $nonsportsWatchlist)) {
    if ($RegenerateNonsportsWatchlist) {
        Invoke-Wsl @(".venv/bin/python", "-m", "deploy.discover_nonsports_corpus", "--apply", "--top-per-family", "8", "--min-liquidity", "20000")
    }
    else {
        Write-Warning "Missing data/scan_shortlist_nonsports.json. Nonsports service will be skipped. Restore the original file or rerun with -RegenerateNonsportsWatchlist to build a fresh 2026-05-11-era corpus."
        $installNonsports = $false
    }
}

$validationCode = "from polymarket_rbi_bot.config import BotConfig; c=BotConfig.from_env(); print(f'host={c.host} gamma={c.gamma_host} chain_id={c.chain_id} has_l2_auth={c.has_l2_auth}')"
Invoke-Wsl @(".venv/bin/python", "-c", $validationCode)

if (-not $InstallServices) {
    Write-Step "WSL bring-up validation complete. Rerun with -InstallServices to install NSSM services."
    exit 0
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$nssm = Resolve-Nssm
$credential = $null

if ($PromptForServiceCredential) {
    $credential = Get-Credential -Message "Enter the Windows account that owns the WSL distro '$Distro'."
}
else {
    Write-Warning "NSSM defaults to LocalSystem. Per-user WSL distros usually need the service Log On account set to your Windows user. Rerun with -PromptForServiceCredential or set it in services.msc/NSSM before starting."
}

$services = @()
$mainService = "$ServicePrefix-QuoteCollector"
Install-CollectorService `
    -Nssm $nssm `
    -Name $mainService `
    -DisplayName "Polymarket RBI Bot Quote Collector" `
    -Watchlist "data/scan_shortlist.json" `
    -Output "data/quote_collection/run.jsonl" `
    -Stdout (Join-Path $LogDir "collector.out.log") `
    -Stderr (Join-Path $LogDir "collector.err.log") `
    -Credential $credential
$services += $mainService

if ($installNonsports) {
    $nonsportsService = "$ServicePrefix-QuoteCollector-Nonsports"
    Install-CollectorService `
        -Nssm $nssm `
        -Name $nonsportsService `
        -DisplayName "Polymarket RBI Bot Quote Collector - Nonsports" `
        -Watchlist "data/scan_shortlist_nonsports.json" `
        -Output "data/quote_collection/nonsports_run.jsonl" `
        -Stdout (Join-Path $LogDir "nonsports.out.log") `
        -Stderr (Join-Path $LogDir "nonsports.err.log") `
        -Credential $credential
    $services += $nonsportsService
}

if ($StartServices) {
    foreach ($service in $services) {
        Write-Step "Starting $service"
        & $nssm start $service | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "nssm start $service failed with exit code $LASTEXITCODE"
        }
    }
}

Write-Step "Installed services: $($services -join ', ')"
Write-Step "Check health from WSL with: python -m deploy.collector_health --stream main"
if ($installNonsports) {
    Write-Step "Check nonsports health with: python -m deploy.collector_health --stream nonsports"
}
