# Refresh cash bids from this machine and push.
#
# This is THE runner - GitHub Actions no longer runs on a schedule. NEW Co-op
# and Landus block GitHub's datacenter IP ranges (403 / 429) but answer a home
# connection normally, and GitHub's cron proved unreliable besides (1 of 10
# slots fired). The workflow remains as a manual fallback from the Actions tab
# for when this PC is off for days.
#
# A manual cloud run can still race a scheduled one here; bids.json is
# generated rather than authored, so a collision is settled by keeping
# whichever build is newer.
#
# Registered by scripts/install-task.ps1. Run it by hand any time - double-click
# scripts\run-now.cmd, or:
#     powershell -ExecutionPolicy Bypass -File scripts\update-bids.ps1
#
# A hand-run and the scheduled run can be started at the same moment, so a
# machine-wide mutex lets only one through. The other exits immediately instead
# of both scraping and then fighting over the same commit.

param([switch]$Verbose)

$ErrorActionPreference = 'Stop'

$repo   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$log    = Join-Path $repo '.build\update-bids.log'

New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

# Global\ so it is shared across sessions - the scheduled task and a console
# window are different sessions. Released automatically if the process dies.
$mutex = New-Object System.Threading.Mutex($false, 'Global\GrainMapUpdateBids')
if (-not $mutex.WaitOne(0)) {
    $msg = 'another run is already in progress - exiting'
    Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) -Encoding utf8
    if ($Verbose) { Write-Host $msg -ForegroundColor Yellow }
    exit 0
}

function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
    if ($Verbose) { Write-Host $line }
}

# Keep the log from growing without bound.
if ((Test-Path $log) -and (Get-Item $log).Length -gt 512KB) {
    Get-Content $log -Tail 2000 | Set-Content "$log.tmp" -Encoding utf8
    Move-Item "$log.tmp" $log -Force
}

Set-Location $repo
Write-Log "--- run start ---"

try {
    if (-not (Test-Path $python)) { throw "no venv at $python" }

    # Catch up to origin first so the build starts from the newest bids.json.
    # --autostash sets aside any work in progress and puts it back afterwards,
    # so editing anything else in this repo cannot freeze the bids. An earlier
    # version refused to run on a dirty tree and silently stopped refreshing
    # for exactly that reason.
    git fetch origin main --quiet
    git checkout --quiet -- docs/bids.json 2>$null

    git rebase --autostash --quiet origin/main
    if ($LASTEXITCODE -ne 0) {
        git rebase --abort 2>$null
        throw "rebase onto origin/main failed - resolve by hand, work in progress is untouched"
    }

    if ($Verbose) {
        # Show the per-source lines live when a person is watching.
        & $python scrapers\build_bids.py | Tee-Object -Variable buildOut
        $buildOut | Where-Object { $_ -match ':\s+\d+ locations' -or $_ -match 'wrote ' } |
            ForEach-Object { Add-Content -Path $log -Value ("    " + $_) -Encoding utf8 }
    } else {
        & $python scrapers\build_bids.py
    }
    if ($LASTEXITCODE -ne 0) { throw "build_bids.py failed (exit $LASTEXITCODE)" }

    # Record any source that did not refresh, so the log shows WHICH co-op
    # failed rather than just that the run finished.
    $report = & $python scrapers\source_status.py --log
    if ($report) { $report -split "`n" | ForEach-Object { if ($_.Trim()) { Write-Log $_.TrimEnd() } } }

    & $python scrapers\build_bids.py --validate docs\bids.json
    if ($LASTEXITCODE -ne 0) { throw "validation failed (exit $LASTEXITCODE)" }

    git diff --quiet -- docs/bids.json
    if ($LASTEXITCODE -eq 0) { Write-Log "no bid changes"; exit 0 }

    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm')
    git add docs/bids.json
    git commit --quiet -m "Update cash bids ($stamp UTC, farm PC)"

    # Same race the cloud run handles, from the other side. Replay our commit
    # on top of whatever arrived: bids.json is generated, so on a conflict the
    # build we just made wins ("theirs" is the commit being replayed during a
    # rebase, i.e. ours).
    #
    # Deliberately NOT `git reset --hard`, which an earlier version used - that
    # would throw away any uncommitted work sitting in this repo.
    foreach ($attempt in 1..3) {
        git push --quiet origin main 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Log "pushed"; exit 0 }

        Write-Log "push rejected (attempt $attempt) - replaying onto origin"
        git fetch origin main --quiet
        git rebase --autostash -X theirs --quiet origin/main
        if ($LASTEXITCODE -ne 0) {
            git rebase --abort 2>$null
            throw "could not replay onto origin/main - resolve by hand"
        }
    }

    throw "could not push after 3 attempts"
}
catch {
    Write-Log "ERROR: $_"
    exit 1
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
