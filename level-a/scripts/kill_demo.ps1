<#
.SYNOPSIS
The kill beat (spec "Demo script", 1:00), for Windows PowerShell.

.DESCRIPTION
The worker dies between asking ElevenLabs for the call to Corner Mart and
saving the answer, the phone rings once, the claim lands while the worker is
down, and the restarted worker adopts the attempt from the evidence instead of
dialling again. Same steps as scripts/kill_demo.sh:

1. python scripts/demo.py kill-arm: arms the after_dial_request failpoint for
   corner-mart through the API, releases Corner Mart and (unless -NoRelease)
   Queen St Grocer's held reply, whose claim names Corner Mart. It refuses
   while another worker is alive: stop the supervisor first.
2. Runs the worker in the foreground. When it dials Corner Mart the failpoint
   kills it with exit code 137.
3. Counts the kill as the supervisor would (python -m levela count-kill:
   runs.kills + 1 and "worker died with exit 137 (n)" on the wall), prints a
   banner and waits for Enter while the phone rings and the teammate answers
   "received 25".
4. Restarts the worker in the foreground. Ctrl+C stops it; then restart the
   supervisor.

Settings (API_BASE_URL, ADMIN_TOKEN, ATLAS_URI, the phones) are read from
level-a\.env by the Python side. $env:PYTHON picks the interpreter
(default: python).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\kill_demo.ps1 -RecallId RCL-2026-020
#>
param(
    [string]$RecallId = "",
    [string]$Api = "",
    [switch]$NoRelease,
    [int]$Workers = 4
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

function Show-Banner([string[]]$Lines) {
    $rule = "=" * 66
    Write-Host ""
    Write-Host $rule
    foreach ($line in $Lines) { Write-Host "  $line" }
    Write-Host $rule
    Write-Host ""
}

$demoArgs = @("scripts/demo.py")
if ($Api) { $demoArgs += @("--api", $Api) }
if ($RecallId) { $demoArgs += @("--recall-id", $RecallId) }
$demoArgs += "kill-arm"
if ($NoRelease) { $demoArgs += "--no-release" }

$workerArgs = @("-m", "levela", "worker", "--workers", "$Workers")
$countArgs = @("-m", "levela", "count-kill", "--exit-code", "137")
if ($RecallId) {
    $workerArgs += @("--recall", $RecallId)
    $countArgs += @("--recall", $RecallId)
}

& $Python @demoArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Show-Banner @("Starting the worker in the foreground.",
              "It dies the moment it has asked for the call to Corner Mart.")
& $Python @workerArgs
$code = $LASTEXITCODE

if ($code -ne 137) {
    Show-Banner @("The worker exited with code $code, not 137: the failpoint did not fire.",
                  "Check the wall (python scripts/demo.py status) and the failpoint (python -m levela status).")
    if ($code -eq 0) { $code = 1 }
    exit $code
}

# The supervisor is stopped for this beat, so count the kill the way it would:
# the wall's "Kills survived" and the restarted worker's "restart n" line must
# include the kill the room just watched. A failure here must not stop the beat.
& $Python @countArgs
if ($LASTEXITCODE -ne 0) { Write-Host "Could not count the kill on the wall (see above); carrying on." }

Show-Banner @("WORKER KILLED (exit 137) by failpoint after_dial_request.",
              "The call to Corner Mart was requested and never saved.",
              "Let the phone ring; the teammate answers `"received 25, all on the shelf`".",
              "Watch the wall: the claim lands while the worker is down.",
              "Press Enter to restart the worker.")
[void](Read-Host)

Show-Banner @("Restarting the worker: it reads what is due and what is in doubt,",
              "adopts the attempt from the evidence and does not dial again.",
              "Ctrl+C stops it; then restart the supervisor (python scripts/supervisor.py).")
& $Python @workerArgs
exit $LASTEXITCODE
