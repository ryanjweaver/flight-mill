[CmdletBinding()]
param(
    [string]$Environment = "supermini_hw747_v002",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$availableLetter = @("P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z") |
    Where-Object { -not (Test-Path "${_}:\") } |
    Select-Object -First 1
if (-not $availableLetter) {
    throw "No unused drive letter is available for the short-path firmware build."
}

$drive = "${availableLetter}:"
$oldCoreDirectory = $env:PLATFORMIO_CORE_DIR
$oldTelemetrySetting = $env:PLATFORMIO_SETTING_ENABLE_TELEMETRY
$oldJobSetting = $env:PLATFORMIO_RUN_JOBS
$locationPushed = $false

try {
    & subst.exe $drive $repositoryRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create temporary build mapping $drive"
    }

    $env:PLATFORMIO_CORE_DIR = "$drive\.pio-core"
    $env:PLATFORMIO_SETTING_ENABLE_TELEMETRY = "No"
    $env:PLATFORMIO_RUN_JOBS = "1"

    $localPlatformIo = "$drive\.venv\Scripts\pio.exe"
    if (Test-Path -LiteralPath $localPlatformIo) {
        $platformIo = $localPlatformIo
    } else {
        $platformIoCommand = Get-Command pio -ErrorAction SilentlyContinue
        if (-not $platformIoCommand) {
            throw "PlatformIO is missing. Install it in .venv or make pio available on PATH."
        }
        $platformIo = $platformIoCommand.Source
    }

    Push-Location "$drive\firmware"
    $locationPushed = $true
    if ($Clean) {
        & $platformIo run -e $Environment --target clean
        if ($LASTEXITCODE -ne 0) {
            throw "PlatformIO clean failed with exit code $LASTEXITCODE."
        }
    }
    & $platformIo run -e $Environment
    if ($LASTEXITCODE -ne 0) {
        throw "PlatformIO firmware build failed with exit code $LASTEXITCODE."
    }
} finally {
    if ($locationPushed) {
        Pop-Location
    }
    & subst.exe $drive /D | Out-Null
    $env:PLATFORMIO_CORE_DIR = $oldCoreDirectory
    $env:PLATFORMIO_SETTING_ENABLE_TELEMETRY = $oldTelemetrySetting
    $env:PLATFORMIO_RUN_JOBS = $oldJobSetting
}
