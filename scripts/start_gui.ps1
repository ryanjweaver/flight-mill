param(
    [switch]$Browser,
    [int]$Port = 0,
    [string]$OutputDirectory,
    [string]$PythonPath
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $PythonPath) { $PythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Project Python environment is missing. See docs/SIMULATION_QUICK_START.md.'
}
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectRoot 'data/simulations' }
$env:PYTHONPATH = (Join-Path $projectRoot 'app/src') + ';' + $projectRoot
$launchArguments = @('-m', 'flightmill', '--output-dir', $OutputDirectory)
if ($Port -gt 0) { $launchArguments += @('--port', $Port) }
if ($Browser) { $launchArguments += '--browser' }
& $pythonPath @launchArguments
exit $LASTEXITCODE
