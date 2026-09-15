#requires -Version 5.1
# Pure/mocked installer regressions: no downloads, installers, app, UI, or USB.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Install-FlightMill.ps1')
$script:passed = 0
function Assert-True {
    param([bool]$Condition, [string]$Name)
    if (-not $Condition) { throw "FAIL: $Name" }
    $script:passed += 1
}
function Assert-Throws {
    param([scriptblock]$Action, [string]$Name)
    $threw = $false
    try { & $Action } catch { $threw = $true }
    Assert-True $threw $Name
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('FlightMillInstallerTests_' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory((Join-Path $testRoot 'FlightMill'))
try {
    $fakeExe = Join-Path $testRoot 'FlightMill\FlightMill.exe'
    [IO.File]::WriteAllText($fakeExe, 'test file; never executable')
    $manifest = [PSCustomObject]@{ package_id = 'test-20260915'; app_version = '0.2.0.dev0';
        target_os = 'Windows 11'; architecture = 'x64'; files = @([PSCustomObject]@{
            path = 'FlightMill/FlightMill.exe'; bytes = (Get-Item -LiteralPath $fakeExe).Length;
            sha256 = (Get-FileHash -LiteralPath $fakeExe -Algorithm SHA256).Hash }) }
    Assert-PackageManifest -Root $testRoot -Manifest $manifest
    Assert-True $true 'valid manifest accepted'
    foreach ($path in @('../outside', '..\outside', '/outside', 'C:\outside', 'a:b',
                        'a//b', 'a/./b', 'a/../b', 'a./b', 'a /b', 'NUL.txt', 'a/COM1',
                        'a*', 'a?', 'a"', 'a|', 'a<', 'a>')) {
        Assert-Throws { Resolve-PackagePath $testRoot $path } "reject path $path"
    }
    $resolved = Resolve-PackagePath $testRoot 'FlightMill/FlightMill.exe'
    Assert-True ($resolved -eq $fakeExe) 'normal path resolves under package'
    $manifest.files[0].sha256 = ('0' * 64)
    Assert-Throws { Assert-PackageManifest $testRoot $manifest } 'checksum tampering rejected'
    $manifest.files[0].sha256 = (Get-FileHash -LiteralPath $fakeExe -Algorithm SHA256).Hash
    $manifest.files[0].bytes += 1
    Assert-Throws { Assert-PackageManifest $testRoot $manifest } 'size mismatch rejected'
    $manifest.files[0].bytes -= 1
    $manifest.files += $manifest.files[0]
    Assert-Throws { Assert-PackageManifest $testRoot $manifest } 'duplicate entry rejected'
    $manifest.files = @($manifest.files[0])
    $manifest.package_id = '..'
    Assert-Throws { Assert-PackageManifest $testRoot $manifest } 'unsafe installation name rejected'
    $manifest.package_id = 'test-20260915'
    $manifest.architecture = 'ARM64'
    Assert-Throws { Assert-PackageManifest $testRoot $manifest } 'wrong package architecture rejected'
    $manifest.architecture = 'x64'
    $manifest.files[0].path = 'FlightMill/missing.exe'
    Assert-Throws { Assert-PackageManifest $testRoot $manifest } 'missing package file rejected'
    $manifest.files[0].path = 'FlightMill/FlightMill.exe'

    Assert-TargetPlatform 22000 'AMD64' $true
    Assert-True $true 'Windows 11 AMD64 accepted'
    Assert-Throws { Assert-TargetPlatform 19045 'AMD64' $true } 'Windows 10 rejected'
    Assert-Throws { Assert-TargetPlatform 26200 'ARM64' $true } 'ARM64 rejected'
    Assert-Throws { Assert-TargetPlatform 26200 'AMD64' $false } '32-bit shell rejected'
    foreach ($version in @($null, '', '0.0.0.0', 'nonsense')) {
        Assert-True (-not (Test-WebViewVersion $version)) 'invalid WebView2 version rejected'
    }
    Assert-True (Test-WebViewVersion '152.0.4191.66') 'WebView2 version accepted'
    Assert-True ((ConvertTo-WindowsArgument 'C:\Folder with spaces\') -eq '"C:\Folder with spaces\\"') 'trailing backslash escaped'
    Assert-True ((ConvertTo-WindowsArgument 'a"b') -eq '"a\"b"') 'embedded quote escaped'
    Assert-True ((ConvertTo-WindowsArgument '') -eq '""') 'empty argument quoted'

    $certificate = [PSCustomObject]@{}
    $certificate | Add-Member ScriptMethod GetNameInfo { param($Type, $Issuer) 'Microsoft Corporation' }
    Assert-MicrosoftSignature ([PSCustomObject]@{Status = 'Valid'; SignerCertificate = $certificate})
    Assert-True $true 'valid Microsoft signature accepted'
    Assert-Throws { Assert-MicrosoftSignature ([PSCustomObject]@{Status = 'NotSigned'; SignerCertificate = $certificate}) } 'unsigned download rejected'
    $foreign = [PSCustomObject]@{}
    $foreign | Add-Member ScriptMethod GetNameInfo { param($Type, $Issuer) 'Another Publisher' }
    Assert-Throws { Assert-MicrosoftSignature ([PSCustomObject]@{Status = 'Valid'; SignerCertificate = $foreign}) } 'other publisher rejected'

    $report = [PSCustomObject]@{ ok = $true; runtime = [PSCustomObject]@{ frozen = $true };
        build_info = [PSCustomObject]@{ package_id = 'test-20260915' }; errors = @() }
    Assert-SelfCheckReport $report 'test-20260915'
    Assert-True $true 'successful frozen self-check for exact package accepted'
    $report.ok = $false
    Assert-Throws { Assert-SelfCheckReport $report 'test-20260915' } 'failed self-check rejected'
    $report.ok = 'true'
    Assert-Throws { Assert-SelfCheckReport $report 'test-20260915' } 'nonboolean self-check success rejected'
    $report.ok = $true
    $report.runtime.frozen = $false
    Assert-Throws { Assert-SelfCheckReport $report 'test-20260915' } 'unfrozen self-check rejected'
    $report.runtime.frozen = $true
    Assert-Throws { Assert-SelfCheckReport $report 'another-package' } 'different self-check package rejected'
    $report.errors = @('dependency missing')
    Assert-Throws { Assert-SelfCheckReport $report 'test-20260915' } 'self-check errors rejected'
    $report.errors = $null
    Assert-Throws { Assert-SelfCheckReport $report 'test-20260915' } 'missing errors list rejected'

    # Fake registry reads ensure all required hives and registry views are considered.
    $script:reads = @()
    function Get-RegistryValue {
        param($Hive, $View, $Path, $Name)
        $script:reads += "$Hive/$View/$Name"
        if ($Hive -eq 'CurrentUser' -and $View -eq 'Registry32' -and $Name -eq 'pv') { return '152.0.4191.66' }
        return $null
    }
    $versions = @(Get-WebViewVersions)
    Assert-True ($versions.Count -eq 1) 'per-user runtime in registry32 is detected'
    Assert-True ($script:reads.Count -eq 4) 'both hives and both registry views checked'
    function Invoke-WebRequest { throw 'unexpected network access' }
    function Invoke-CheckedProcess { throw 'unexpected process execution' }
    $versions = @(Install-MissingWebView -LogDirectory $testRoot)
    Assert-True ($versions.Count -eq 1) 'installed runtime skips download and execution'
    function Get-RegistryValue { param($Hive, $View, $Path, $Name) return $null }
    Assert-True ((Get-DotNetRelease) -eq 0) 'missing framework detected'
    function Get-RegistryValue {
        param($Hive, $View, $Path, $Name)
        if ($View -eq 'Registry64') { return 533320 }
        return 528040
    }
    Assert-True ((Get-DotNetRelease) -eq 533320) 'newer framework detected without downgrade'

    # Exercise the complete missing-runtime branch with fake download, signature,
    # process, and registry adapters. The downloaded test text is never executed.
    function Reset-WebViewMocks {
        $script:runtimeRegistered = $false
        $script:registerAfterInstall = $true
        $script:installerExit = 0
        $script:signatureStatus = 'Valid'
        $script:failDownload = $false
        $script:networkCalls = 0
        $script:signatureCalls = 0
        $script:installCalls = 0
    }
    function Get-WebViewVersions {
        if ($script:runtimeRegistered) {
            [PSCustomObject]@{ hive = 'CurrentUser'; view = 'Registry64'; version = '152.0.4191.66' }
        }
    }
    function Invoke-WebRequest {
        param($Uri, $OutFile, [switch]$UseBasicParsing, $MaximumRedirection)
        $script:networkCalls += 1
        if ($Uri -ne 'https://go.microsoft.com/fwlink/p/?LinkId=2124703') { throw 'Unexpected installer URL' }
        if ($script:failDownload) { throw 'Simulated offline target' }
        [IO.File]::WriteAllText($OutFile, 'fake Microsoft download for unit test; never execute')
    }
    function Get-AuthenticodeSignature {
        param($LiteralPath)
        $script:signatureCalls += 1
        return [PSCustomObject]@{ Status = $script:signatureStatus; SignerCertificate = $certificate }
    }
    function Invoke-CheckedProcess {
        param($Executable, $Arguments, $LogPrefix, $TimeoutSeconds)
        $script:installCalls += 1
        if (($Arguments -join ' ') -ne '/silent /install') { throw 'Unexpected installer arguments' }
        if ($script:registerAfterInstall -and $script:installerExit -eq 0) { $script:runtimeRegistered = $true }
        return $script:installerExit
    }
    Reset-WebViewMocks
    $versions = @(Install-MissingWebView -LogDirectory $testRoot)
    Assert-True ($versions.Count -eq 1 -and $script:runtimeRegistered) 'missing runtime installed and registration rechecked'
    Assert-True ($script:networkCalls -eq 1 -and $script:signatureCalls -eq 1 -and $script:installCalls -eq 1) 'one download, signature check, and install action'
    Assert-True (Test-Path -LiteralPath (Join-Path $testRoot 'webview2-download.json')) 'verified download receipt written'
    Reset-WebViewMocks
    $script:installerExit = 1603
    Assert-Throws { Install-MissingWebView -LogDirectory $testRoot } 'failed WebView2 installation rejected'
    Assert-True ($script:installCalls -eq 1) 'failed installer is not retried automatically'
    Reset-WebViewMocks
    $script:registerAfterInstall = $false
    Assert-Throws { Install-MissingWebView -LogDirectory $testRoot } 'successful installer without registration rejected'
    Assert-True ($script:installCalls -eq 1) 'missing registration does not trigger repeated installs'
    Reset-WebViewMocks
    $script:signatureStatus = 'HashMismatch'
    Assert-Throws { Install-MissingWebView -LogDirectory $testRoot } 'bad downloaded signature stops installation'
    Assert-True ($script:installCalls -eq 0) 'invalid signature never executes downloaded file'
    Reset-WebViewMocks
    $script:failDownload = $true
    Assert-Throws { Install-MissingWebView -LogDirectory $testRoot } 'download failure stops installation'
    Assert-True ($script:signatureCalls -eq 0 -and $script:installCalls -eq 0) 'failed download never verifies or executes a file'
    Write-Host "$script:passed installer helper tests passed. No installer, app, UI, or device was run."
}
finally {
    $resolvedRoot = [IO.Path]::GetFullPath($testRoot)
    $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolvedRoot.StartsWith($tempParent, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedRoot) -notmatch '^FlightMillInstallerTests_[0-9a-f]{32}$') {
        throw 'Refusing to clean an unexpected test directory.'
    }
    Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
}
