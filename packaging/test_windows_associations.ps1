# Run only on a disposable Windows runner: this installs and removes the app
# and writes per-user file associations.
param(
    [Parameter(Mandatory = $true)][string]$Installer
)
$ErrorActionPreference = 'Stop'
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$installDir = Join-Path $env:RUNNER_TEMP ("Boardviewer association test " + [guid]::NewGuid().ToString('N'))
$progId = 'ThermeteryBoardviewer.Board'
$extensionKey = 'Software\Classes\.fz'
$progKey = "Software\Classes\$progId"

function Read-Value([Microsoft.Win32.RegistryKey]$Root, [string]$Path, [string]$Name) {
    $key = $Root.OpenSubKey($Path)
    if ($null -eq $key) { return $null }
    try { return $key.GetValue($Name, $null, 'DoNotExpandEnvironmentNames') }
    finally { $key.Dispose() }
}

# Refuse to touch an existing application or .fz association, even on CI.
$existingDefault = Read-Value ([Microsoft.Win32.Registry]::ClassesRoot) '.fz' ''
$existingProgId = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($progKey)
$choice = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.fz\UserChoice')
try {
    if ($existingDefault -or $null -ne $existingProgId -or $null -ne $choice) {
        throw 'Runner already has an .fz default or Boardviewer registration; refusing to alter it.'
    }
} finally {
    if ($null -ne $existingProgId) { $existingProgId.Dispose() }
    if ($null -ne $choice) { $choice.Dispose() }
}

$uninstaller = Join-Path $installDir 'unins000.exe'
try {
    $setup = Start-Process -FilePath $installerPath -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS', "/DIR=`"$installDir`""
    )
    if ($setup.ExitCode -ne 0) { throw "Installer exited with code $($setup.ExitCode)." }
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) { throw 'Uninstaller not found.' }

    $actualDefault = Read-Value ([Microsoft.Win32.Registry]::CurrentUser) $extensionKey ''
    if ($actualDefault -ne $progId) { throw "Unexpected .fz default: $actualDefault" }
    $openWith = Read-Value ([Microsoft.Win32.Registry]::CurrentUser) "$extensionKey\OpenWithProgids" $progId
    if ($null -eq $openWith) { throw 'The .fz Open With entry is missing.' }
    $command = Read-Value ([Microsoft.Win32.Registry]::CurrentUser) "$progKey\shell\open\command" ''
    $expected = '"' + (Join-Path $installDir 'ThermeteryBoardviewer.exe') + '" "%1"'
    if ($command -cne $expected) { throw "Unexpected open command: $command (expected $expected)" }
    Write-Host 'Verified .fz default, Open With entry, and quoted open command.'
} finally {
    if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
        $uninstall = Start-Process -FilePath $uninstaller -Wait -PassThru -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
        if ($uninstall.ExitCode -ne 0) { throw "Uninstaller exited with code $($uninstall.ExitCode)." }
    }
}

if ($null -ne (Read-Value ([Microsoft.Win32.Registry]::CurrentUser) $extensionKey '')) {
    throw 'Uninstall left the .fz default behind.'
}
if ($null -ne (Read-Value ([Microsoft.Win32.Registry]::CurrentUser) "$extensionKey\OpenWithProgids" $progId)) {
    throw 'Uninstall left the .fz Open With entry behind.'
}
$remaining = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($progKey)
if ($null -ne $remaining) { $remaining.Dispose(); throw 'Uninstall left the Boardviewer ProgID behind.' }
Write-Host 'Verified association cleanup after uninstall.'
