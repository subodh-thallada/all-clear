<#
.SYNOPSIS
Start the two mock distributor order systems (V2 section 7) and keep them running.

.DESCRIPTION
Distributor North and Distributor East, one process each, so each has its own
version switch and its own faults. Same behaviour as scripts/start_order_systems.sh.

The API reaches them at ORDER_NORTH_URL and ORDER_EAST_URL (default
http://127.0.0.1:8101 and :8102): change those in .env if you change the
ports. Each order system checks X-Admin-Token against ADMIN_TOKEN from .env, the same
token the API sends when the operator presses "Switch order system to v2". Ctrl+C
stops both; if one dies, the other is stopped too. $env:PYTHON picks the
interpreter (default: python).

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\start_order_systems.ps1
#>
param(
    [string]$Scenario = "recall_long",
    [string]$BindHost = "127.0.0.1",
    [int]$NorthPort = 8101,
    [int]$EastPort = 8102,
    [int]$ReadySeconds = 30
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
# An order system bound to every interface still answers on the loopback address.
$ProbeHost = if ($BindHost -eq "0.0.0.0") { "127.0.0.1" } else { $BindHost }

function Start-Order system([string]$Name, [int]$Port) {
    $orderSystemArgs = @("-m", "levela.mock.order_system", "--name", $Name, "--port", "$Port",
                    "--host", $BindHost, "--scenario", $Scenario)
    Start-Process -FilePath $Python -ArgumentList $orderSystemArgs -NoNewWindow -PassThru
}

# Waits until the order system serves its OpenAPI document (the repairer reads it too).
function Wait-Ready([string]$Name, [int]$Port) {
    $base = "http://${ProbeHost}:$Port"
    $deadline = (Get-Date).AddSeconds($ReadySeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            [void](Invoke-WebRequest -Uri "$base/openapi.json" -UseBasicParsing -TimeoutSec 2)
            Write-Host "order system $Name is up at $base"
            return
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "order system $Name did not answer $base/openapi.json within $ReadySeconds s"
}

$order systems = @()
try {
    $order systems += Start-Order system "north" $NorthPort
    $order systems += Start-Order system "east" $EastPort
    Wait-Ready "north" $NorthPort
    Wait-Ready "east" $EastPort
    Write-Host "Both order systems are up (scenario $Scenario). Ctrl+C stops both."
    while (-not ($order systems | Where-Object { $_.HasExited })) {
        Start-Sleep -Seconds 1
    }
    Write-Host "An order system stopped; stopping the other."
    exit 1
} finally {
    foreach ($p in $order systems) {
        if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
}
