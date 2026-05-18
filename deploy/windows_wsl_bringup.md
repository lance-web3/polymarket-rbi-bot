# Windows/WSL Collector Bring-Up

This is the Windows replacement for the old macOS LaunchAgents:

- `deploy/com.polymarket.rbi-bot.quote-collector.plist`
- `deploy/com.polymarket.rbi-bot.quote-collector-nonsports.plist`

The collector still runs inside WSL so paths, Python imports, and JSONL outputs
stay Linux-native. Windows supervises the long-running process with NSSM.

## Current Migration Notes

As of the WSL copy inspected on 2026-05-11:

- `data/quote_collection/run.jsonl` is present at about 2.1 GB.
- `data/quote_collection/nonsports_run.jsonl` is present at about 418 MB.
- the last row in both streams is from 2026-05-04 UTC, so collection is stale.
- `data/scan_shortlist.json` is present.
- `data/scan_shortlist_nonsports.json` is missing, so the nonsports collector
  cannot be restarted against the original corpus until that file is restored
  or deliberately regenerated.

Track A's pinned verdict date was 2026-05-02. Because that date is already in
the past, do not treat new collection as filling the verdict window. New
collection is for the next research cycle.

## Prerequisites

1. WSL distro: `Ubuntu-24.04`
2. WSL project path: `/home/a/polymarket-rbi-bot`
3. `.env` restored locally inside the WSL repo, never via cloud sync.
4. NSSM available on Windows. Either add `nssm.exe` to `PATH` or pass
   `-NssmPath C:\path\to\nssm.exe`.
5. Run PowerShell as Administrator when installing services.

## Validate WSL And Venv

From elevated Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\install_windows_wsl_collectors.ps1
```

The default run creates `.venv`, installs `requirements.txt`, checks the main
watchlist, and validates that config loads without printing secrets. It does
not install services unless `-InstallServices` is passed.

If `.venv` is already good and you only want validation:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\install_windows_wsl_collectors.ps1 -SkipVenv
```

## Install The Main Collector Service

Use this when you only want the main sports stream for now:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\install_windows_wsl_collectors.ps1 `
  -SkipVenv `
  -InstallServices `
  -StartServices `
  -NoNonsports `
  -PromptForServiceCredential
```

`-PromptForServiceCredential` is recommended. NSSM defaults to LocalSystem, but
per-user WSL distros usually need the service to log on as the Windows account
that owns the distro.

The service name is:

```text
PolymarketRbiBot-QuoteCollector
```

It runs:

```bash
/home/a/polymarket-rbi-bot/.venv/bin/python -m deploy.collect_quotes \
  --watchlist data/scan_shortlist.json \
  --interval-seconds 30 \
  --use-clob-order-books \
  --output data/quote_collection/run.jsonl
```

## Restore Or Regenerate Nonsports

Best option: restore the original `data/scan_shortlist_nonsports.json` from the
Mac backup so the 2026-04-27 corpus definition stays intact.

If you accept a fresh 2026-05-11-era nonsports corpus, regenerate and install:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\install_windows_wsl_collectors.ps1 `
  -SkipVenv `
  -InstallServices `
  -StartServices `
  -RegenerateNonsportsWatchlist `
  -PromptForServiceCredential
```

The nonsports service name is:

```text
PolymarketRbiBot-QuoteCollector-Nonsports
```

## Health Checks

From WSL:

```bash
cd /home/a/polymarket-rbi-bot
source .venv/bin/activate
python -m deploy.collector_health --stream main
python -m deploy.collector_health --stream nonsports
```

If the nonsports watchlist is still missing, the nonsports health check is
expected to fail or be skipped.

## Manual Fallback

If NSSM is not installed yet, keep collection running manually in a Windows
Terminal WSL tab:

```bash
cd /home/a/polymarket-rbi-bot
source .venv/bin/activate
python -m deploy.collect_quotes \
  --watchlist data/scan_shortlist.json \
  --interval-seconds 30 \
  --use-clob-order-books \
  --output data/quote_collection/run.jsonl
```

This does not survive reboots, but it is enough to restart data collection
while NSSM is being set up.
