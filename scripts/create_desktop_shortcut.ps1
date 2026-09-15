param([string]$PythonPath, [string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $PythonPath) { $PythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Python is missing. Select the configured Python 3.12 environment with -PythonPath.'
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$pythonWindowed = Join-Path (Split-Path $PythonPath -Parent) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowed -PathType Leaf)) { throw 'pythonw.exe is missing from the selected environment.' }
$env:PYTHONPATH = (Join-Path $projectRoot 'app/src') + ';' + $projectRoot
& $PythonPath -c 'from flightmill.desktop.bootstrap import preflight; preflight()'
if ($LASTEXITCODE -ne 0) { throw 'Launch preflight failed. Select the configured pinned environment.' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectRoot 'data/simulations' }
$shortcutPath = Join-Path $projectRoot 'Flight Mill.lnk'
if (Test-Path -LiteralPath $shortcutPath) { throw 'A local shortcut already exists; inspect it before creating another.' }
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonWindowed
$shortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'launch_desktop.pyw') + '" --output-dir "' + $OutputDirectory + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = 'Flight Mill — this source checkout; configured workstation only'
$shortcut.IconLocation = (Join-Path $projectRoot 'app/packaging/flightmill.ico')
$shortcut.Save()
Write-Output $shortcutPath
