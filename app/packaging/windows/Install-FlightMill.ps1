#requires -Version 5.1
<#
Install the exported Flight Mill package for the current Windows user.
Dot-sourcing this file defines helpers only; it performs no installation.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-PackagePath {
    param([string]$Root, [string]$RelativePath)
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or
        [IO.Path]::IsPathRooted($RelativePath) -or
        $RelativePath -match '[:*?"<>|]') {
        throw "Invalid package path: $RelativePath"
    }
    $parts = $RelativePath.Replace('\', '/').Split('/')
    foreach ($part in $parts) {
        if ([string]::IsNullOrEmpty($part) -or $part -in @('.', '..') -or
            $part -match '[. ]$' -or
            $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
            throw "Invalid package path: $RelativePath"
        }
    }
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $candidate = [IO.Path]::GetFullPath([IO.Path]::Combine($base, ($parts -join '\')))
    if (-not $candidate.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Package path escapes its folder: $RelativePath"
    }
    $current = $base
    foreach ($part in @('') + $parts) {
        if ($part) { $current = [IO.Path]::Combine($current, $part) }
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Package paths must not contain links or junctions: $current"
            }
        }
    }
    return $candidate
}

function Assert-TargetPlatform {
    param([int]$BuildNumber, [string]$NativeArchitecture, [bool]$Is64BitProcess)
    if ($BuildNumber -lt 22000 -or $NativeArchitecture -ne 'AMD64' -or -not $Is64BitProcess) {
        throw 'This package requires Windows 11 on an Intel or AMD 64-bit computer, using 64-bit Windows PowerShell. ARM and 32-bit Windows are not supported.'
    }
}

function Get-RegistryValue {
    param([Microsoft.Win32.RegistryHive]$Hive,
          [Microsoft.Win32.RegistryView]$View, [string]$Path, [string]$Name)
    $base = $null
    $key = $null
    try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, $View)
        $key = $base.OpenSubKey($Path)
        if ($null -ne $key) { return $key.GetValue($Name, $null) }
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
        if ($null -ne $base) { $base.Dispose() }
    }
    return $null
}

function Get-DotNetRelease {
    $releases = foreach ($view in @('Registry64', 'Registry32')) {
        $value = Get-RegistryValue -Hive LocalMachine -View $view `
            -Path 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -Name Release
        if ($null -ne $value) { [int]$value }
    }
    if (@($releases).Count -eq 0) { return 0 }
    return [int](($releases | Measure-Object -Maximum).Maximum)
}

function Test-WebViewVersion {
    param($Value)
    $parsed = $null
    return ($null -ne $Value -and [Version]::TryParse([string]$Value, [ref]$parsed) -and
            $parsed -gt [Version]'0.0.0.0')
}

function Get-WebViewVersions {
    $path = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    foreach ($hive in @('CurrentUser', 'LocalMachine')) {
        foreach ($view in @('Registry64', 'Registry32')) {
            $value = Get-RegistryValue -Hive $hive -View $view -Path $path -Name pv
            if (Test-WebViewVersion $value) {
                [PSCustomObject]@{ hive = $hive; view = $view; version = [string]$value }
            }
        }
    }
}

function Assert-PackageManifest {
    param([string]$Root, $Manifest)
    if ([string]$Manifest.package_id -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$' -or
        [string]$Manifest.package_id -match '[. ]$' -or
        [string]::IsNullOrWhiteSpace([string]$Manifest.app_version) -or
        [string]$Manifest.architecture -notin @('x64', 'AMD64') -or
        @($Manifest.files).Count -eq 0) {
        throw 'The package manifest has an invalid identity, architecture, or file list.'
    }
    $seen = @{}
    foreach ($entry in $Manifest.files) {
        $relative = [string]$entry.path
        $normalized = $relative.Replace('\', '/')
        $path = Resolve-PackagePath -Root $Root -RelativePath $relative
        if ($normalized -eq 'manifest.json' -or $seen.ContainsKey($normalized)) {
            throw "Duplicate or self-referential manifest entry: $relative"
        }
        $seen[$normalized] = $true
        $size = 0L
        if ([string]$entry.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
            -not [long]::TryParse([string]$entry.bytes, [ref]$size) -or $size -lt 0) {
            throw "Invalid package checksum or size: $relative"
        }
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Package file is missing. Extract the complete ZIP first: $relative"
        }
        if ((Get-Item -LiteralPath $path).Length -ne $size -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne [string]$entry.sha256) {
            throw "Package verification failed: $relative. Extract a fresh copy of the complete ZIP."
        }
    }
    if (-not $seen.ContainsKey('FlightMill/FlightMill.exe')) {
        throw 'The manifest does not contain FlightMill/FlightMill.exe.'
    }
}

function Assert-MicrosoftSignature {
    param($Signature)
    if ($null -eq $Signature -or [string]$Signature.Status -ne 'Valid' -or
        $null -eq $Signature.SignerCertificate -or
        $Signature.SignerCertificate.GetNameInfo(
            [Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false
        ) -ne 'Microsoft Corporation') {
        throw 'The downloaded prerequisite does not have a valid Microsoft Corporation signature. Nothing was executed.'
    }
}

function Assert-SelfCheckReport {
    param($Report, [string]$PackageId)
    if ($Report.ok -isnot [bool] -or -not $Report.ok -or
        $Report.runtime.frozen -isnot [bool] -or -not $Report.runtime.frozen -or
        [string]$Report.build_info.package_id -cne $PackageId -or
        $null -eq $Report.errors -or @($Report.errors).Count -ne 0) {
        throw 'The bundled application did not confirm a successful frozen-runtime check for this exact package. No shortcut was created.'
    }
}

function ConvertTo-WindowsArgument {
    param([string]$Value)
    # CommandLineToArgvW escaping; handles spaces, quotes, and trailing backslashes.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-CheckedProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$LogPrefix,
          [int]$TimeoutSeconds = 120)
    $argumentText = ($Arguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '
    $process = Start-Process -FilePath $Executable -ArgumentList $argumentText `
        -PassThru -WindowStyle Hidden -RedirectStandardOutput ($LogPrefix + '.stdout.txt') `
        -RedirectStandardError ($LogPrefix + '.stderr.txt')
    # Cache the native handle before waiting (Windows PowerShell 5.1 can otherwise
    # lose ExitCode for a short-lived process returned by Start-Process).
    $null = $process.Handle
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        throw "The prerequisite or self-check is still running (process $($process.Id)). Review $LogPrefix logs before trying again."
    }
    $process.WaitForExit()
    return $process.ExitCode
}

function Install-MissingWebView {
    param([string]$LogDirectory)
    $versions = @(Get-WebViewVersions)
    if ($versions.Count -gt 0) { return $versions }
    Write-Host 'Microsoft Edge WebView2 Runtime is missing. Downloading its Microsoft installer...'
    $download = Join-Path $LogDirectory 'MicrosoftEdgeWebview2Setup.exe'
    # This Microsoft-owned stable URL is documented in Microsoft's deployment sample.
    $uri = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $uri -OutFile $download -UseBasicParsing -MaximumRedirection 5
    Assert-MicrosoftSignature (Get-AuthenticodeSignature -LiteralPath $download)
    $hash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant()
    @{ url = $uri; sha256 = $hash; checked_utc = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $LogDirectory 'webview2-download.json') -Encoding UTF8
    Write-Host 'Installing Microsoft Edge WebView2 Runtime for the current user...'
    $code = Invoke-CheckedProcess -Executable $download -Arguments @('/silent', '/install') `
        -LogPrefix (Join-Path $LogDirectory 'webview2-install') -TimeoutSeconds 1200
    if ($code -ne 0) { throw "WebView2 installation returned $code. See the installation logs; an organization policy may require IT assistance." }
    $versions = @(Get-WebViewVersions)
    if ($versions.Count -eq 0) { throw 'WebView2 installation finished but its Runtime registration is still missing. Restart Windows if requested, then rerun this installer.' }
    return $versions
}

function New-FlightMillShortcut {
    param([string]$ShortcutPath, [string]$Executable, [string]$OutputDirectory)
    $shell = New-Object -ComObject WScript.Shell
    try {
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        $arguments = '--output-dir ' + (ConvertTo-WindowsArgument $OutputDirectory)
        if (Test-Path -LiteralPath $ShortcutPath) {
            if ($shortcut.TargetPath -eq $Executable -and $shortcut.Arguments -eq $arguments) {
                return
            }
            throw "An existing shortcut will not be overwritten: $ShortcutPath"
        }
        $shortcut.TargetPath = $Executable
        $shortcut.Arguments = $arguments
        $shortcut.WorkingDirectory = Split-Path -Parent $Executable
        $shortcut.IconLocation = $Executable + ',0'
        $shortcut.Description = 'Flight Mill acquisition workspace'
        $shortcut.Save()
    }
    finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
}

function Invoke-FlightMillInstall {
    param([string]$PackageRoot)
    $build = Get-RegistryValue -Hive LocalMachine -View Registry64 `
        -Path 'SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name CurrentBuildNumber
    $architecture = $env:PROCESSOR_ARCHITEW6432
    if ([string]::IsNullOrWhiteSpace($architecture)) { $architecture = $env:PROCESSOR_ARCHITECTURE }
    Assert-TargetPlatform -BuildNumber ([int]$build) -NativeArchitecture $architecture `
        -Is64BitProcess ([IntPtr]::Size -eq 8)
    $manifestPath = Join-Path $PackageRoot 'manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host 'Verifying the exported Flight Mill package...'
    Assert-PackageManifest -Root $PackageRoot -Manifest $manifest

    $logsRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'FlightMill\InstallLogs'
    $logDirectory = Join-Path $logsRoot ((Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    [void][IO.Directory]::CreateDirectory($logDirectory)
    $receiptPath = Join-Path $logDirectory 'install-receipt.json'
    $receipt = [ordered]@{ package_id = $manifest.package_id; app_version = $manifest.app_version
        started_utc = [DateTime]::UtcNow.ToString('o'); status = 'started'; install_path = $null
        manifest_sha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() }
    Write-Host "Installation logs: $logDirectory"
    try {
        $dotnet = Get-DotNetRelease
        $receipt['dotnet_framework_release'] = $dotnet
        if ($dotnet -lt 528040) {
            throw '.NET Framework 4.8 or later is missing or damaged. Windows 11 includes this component. Use Windows Update / Windows component repair with your administrator, then rerun setup. Installing a separate .NET Desktop Runtime does not replace it.'
        }
        $receipt['webview2'] = @(Install-MissingWebView -LogDirectory $logDirectory)
        $programsRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\FlightMill'
        [void][IO.Directory]::CreateDirectory($programsRoot)
        $destination = Resolve-PackagePath -Root $programsRoot -RelativePath $manifest.package_id
        if (Test-Path -LiteralPath $destination) {
            $installedManifest = Join-Path $destination 'manifest.json'
            if (-not (Test-Path -LiteralPath $installedManifest -PathType Leaf) -or
                (Get-FileHash -LiteralPath $installedManifest -Algorithm SHA256).Hash -ne $receipt.manifest_sha256) {
                throw "An incomplete or different installation already occupies $destination. Existing files are preserved. Ask for help with that folder before trying again."
            }
            Assert-PackageManifest -Root $destination -Manifest $manifest
            Write-Host 'Rechecking the identical installed package; existing files are preserved.'
        }
        else {
            [void][IO.Directory]::CreateDirectory($destination)
            Write-Host "Installing Flight Mill to $destination"
            foreach ($entry in $manifest.files) {
                $source = Resolve-PackagePath -Root $PackageRoot -RelativePath $entry.path
                $target = Resolve-PackagePath -Root $destination -RelativePath $entry.path
                [void][IO.Directory]::CreateDirectory((Split-Path -Parent $target))
                Copy-Item -LiteralPath $source -Destination $target
            }
            Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $destination 'manifest.json')
            Assert-PackageManifest -Root $destination -Manifest $manifest
        }
        $receipt['install_path'] = $destination
        $exe = Join-Path $destination 'FlightMill\FlightMill.exe'
        $report = Join-Path $logDirectory 'self-check.json'
        Write-Host 'Checking the bundled Python, app dependencies, native libraries, and renderer...'
        $code = Invoke-CheckedProcess -Executable $exe -Arguments @('--self-check', '--report', $report) `
            -LogPrefix (Join-Path $logDirectory 'self-check')
        if ($code -ne 0 -or -not (Test-Path -LiteralPath $report -PathType Leaf)) {
            throw "Flight Mill's dependency check failed (exit $code). See $logDirectory. No shortcut was created."
        }
        $selfCheck = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
        Assert-SelfCheckReport -Report $selfCheck -PackageId $manifest.package_id
        $receipt['self_check'] = $selfCheck
        $documents = [Environment]::GetFolderPath('MyDocuments')
        if ([string]::IsNullOrWhiteSpace($documents)) { throw 'Windows did not provide a Documents folder for this user.' }
        $output = Join-Path $documents 'FlightMill\Trials'
        [void][IO.Directory]::CreateDirectory($output)
        $shortcutName = 'Flight Mill (' + $manifest.package_id + ').lnk'
        $desktop = [Environment]::GetFolderPath('DesktopDirectory')
        $startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Flight Mill'
        [void][IO.Directory]::CreateDirectory($startMenu)
        $links = @((Join-Path $desktop $shortcutName), (Join-Path $startMenu $shortcutName))
        foreach ($link in $links) {
            New-FlightMillShortcut -ShortcutPath $link -Executable $exe -OutputDirectory $output
        }
        $receipt['shortcuts'] = $links
        $receipt['trial_output_directory'] = $output
        $receipt['status'] = 'installed; operator acceptance pending'
        Write-Host ''
        Write-Host 'Flight Mill installed successfully.' -ForegroundColor Green
        Write-Host "Open the new Desktop or Start menu shortcut: Flight Mill ($($manifest.package_id))"
        Write-Host "Trials will be stored in: $output"
        Write-Host 'The installer did not open the app or connect to a device.'
    }
    catch {
        $receipt['status'] = 'failed'
        $receipt['error'] = $_.Exception.Message
        throw
    }
    finally {
        $receipt['finished_utc'] = [DateTime]::UtcNow.ToString('o')
        $receipt | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
        Write-Host "Installation receipt: $receiptPath"
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-FlightMillInstall -PackageRoot $PSScriptRoot }
    catch {
        Write-Host ''
        Write-Host ('Installation stopped: ' + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
}
