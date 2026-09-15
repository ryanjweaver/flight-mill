[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Port,
    [string]$Environment = "supermini_hw747_v002",
    [string]$ExpectedSha256 = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$firmwareBinary = Join-Path $repositoryRoot "firmware\.pio\build\$Environment\firmware.bin"
if (-not (Test-Path -LiteralPath $firmwareBinary -PathType Leaf)) {
    throw "Firmware binary is missing. Run scripts\build_firmware.ps1 first."
}

$hashBefore = (Get-FileHash -LiteralPath $firmwareBinary -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ExpectedSha256 -and $hashBefore -ne $ExpectedSha256.ToLowerInvariant()) {
    throw "Firmware SHA-256 mismatch: expected $ExpectedSha256, found $hashBefore."
}
Write-Output "FIRMWARE_BIN_SHA256_BEFORE_UPLOAD=$hashBefore"

$availableLetter = @("P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z") |
    Where-Object { -not (Test-Path "${_}:\") } |
    Select-Object -First 1
if (-not $availableLetter) {
    throw "No unused drive letter is available for the short-path firmware upload."
}

$drive = "${availableLetter}:"
$oldCoreDirectory = $env:PLATFORMIO_CORE_DIR
$oldTelemetrySetting = $env:PLATFORMIO_SETTING_ENABLE_TELEMETRY
$oldJobSetting = $env:PLATFORMIO_RUN_JOBS
$locationPushed = $false

try {
    & subst.exe $drive $repositoryRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create temporary upload mapping $drive"
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
    & $platformIo run -e $Environment --target upload --upload-port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "PlatformIO firmware upload failed with exit code $LASTEXITCODE."
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

$hashAfter = (Get-FileHash -LiteralPath $firmwareBinary -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hashAfter -ne $hashBefore) {
    throw "Firmware binary changed during upload: before $hashBefore, after $hashAfter."
}
Write-Output "FIRMWARE_BIN_SHA256_AFTER_UPLOAD=$hashAfter"
