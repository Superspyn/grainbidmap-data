# Refresh cash bids from this machine and push.
#
# Why this exists: NEW Co-op and Landus block GitHub's datacenter IP ranges.
# The scheduled cloud run gets 403 / 429 from them and marks both stale, so
# 116 of the 329 pins never refresh there. Both answer normally from a home
# connection, so this task covers what the cloud run cannot.
#
# The cloud run still matters - it keeps the other 18 sources current when
# this PC is off. The two runs are offset (cloud on :00/:30, this on :15/:45)
# so they rarely collide, and bids.json is generated rather than authored, so
# a collision is settled by keeping whichever build is newer.
#
# Registered by scripts/install-task.ps1. Run it by hand any time:
#     powershell -ExecutionPolicy Bypass -File scripts\update-bids.ps1

$ErrorActionPreference = 'Stop'

$repo   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$log    = Join-Path $repo '.build\update-bids.log'

New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
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

    # Catch up to origin first, so we build on top of the newest bids.json and
    # our commit fast-forwards. A half-built bids.json is disposable - anything
    # else uncommitted is real work, so stop rather than touch it.
    git fetch origin main --quiet
    git checkout --quiet -- docs/bids.json 2>$null

    # Untracked files cannot block a rebase, so they do not block a run -
    # otherwise a stray scratch file would silently freeze the bids for days.
    $dirty = git status --porcelain --untracked-files=no
    if ($dirty) {
        Write-Log "tracked files have uncommitted changes - skipping this run:"
        $dirty | ForEach-Object { Write-Log "    $_" }
        exit 0
    }

    git rebase --quiet origin/main
    if ($LASTEXITCODE -ne 0) { git rebase --abort 2>$null; throw "rebase onto origin/main failed" }

    & $python scrapers\build_bids.py
    if ($LASTEXITCODE -ne 0) { throw "build_bids.py failed (exit $LASTEXITCODE)" }

    & $python scrapers\build_bids.py --validate docs\bids.json
    if ($LASTEXITCODE -ne 0) { throw "validation failed (exit $LASTEXITCODE)" }

    git diff --quiet -- docs/bids.json
    if ($LASTEXITCODE -eq 0) { Write-Log "no bid changes"; exit 0 }

    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm')
    git add docs/bids.json
    git commit --quiet -m "Update cash bids ($stamp UTC, farm PC)"

    # Same race the cloud run handles, from the other side: the newer build
    # already carries everything the older one did, so newer wins outright.
    $ours = Join-Path $env:TEMP 'bids-ours.json'
    Copy-Item docs\bids.json $ours -Force

    foreach ($attempt in 1..3) {
        git push --quiet origin main 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Log "pushed"; exit 0 }

        Write-Log "push rejected (attempt $attempt) - comparing against origin"
        git fetch origin main --quiet
        git reset --hard --quiet origin/main

        $cmp = 'import json, sys; f = lambda p: json.load(open(p, encoding="utf-8"))["generated_at"]; sys.exit(0 if f(sys.argv[1]) > f(sys.argv[2]) else 1)'
        & $python -c $cmp $ours docs\bids.json
        if ($LASTEXITCODE -ne 0) { Write-Log "origin already has a newer build - nothing to do"; exit 0 }

        Copy-Item $ours docs\bids.json -Force
        git add docs/bids.json
        git commit --quiet -m "Update cash bids ($stamp UTC, farm PC)"
    }

    throw "could not push after 3 attempts"
}
catch {
    Write-Log "ERROR: $_"
    exit 1
}
