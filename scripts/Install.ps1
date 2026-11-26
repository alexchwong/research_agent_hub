param([switch]$BootstrapOnly)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'This installer targets Windows x64.'
}
foreach ($Entry in Get-Content sha256.txt) {
    $Hash, $Member = $Entry -split '  ', 2
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $Member).Hash -ne $Hash) {
        throw 'Bundle integrity check failed. Download the ZIP again.'
    }
}
if ((Get-Content host.txt -Raw).Trim() -ne 'windows-x64') { throw 'Download the Windows x64 ZIP.' }
$RahRoot = Join-Path $env:LOCALAPPDATA 'Research Agent Hub'
$Prompt = "Install Research Agent Hub, Local Deep Research, GPT Researcher and PaperQA2 in $RahRoot? Internet access and space for three app environments are required. [y/N]"
if ([Console]::IsInputRedirected) {
    Write-Host $Prompt
    $Reply = [Console]::ReadLine()
} else {
    $Reply = Read-Host $Prompt
}
if ($Reply -notmatch '^y(es)?$') { exit 0 }
$Pin = (Get-Content python-version.txt -Raw).Trim()
& .\uv.exe --no-config python install $Pin
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$RahPython = & .\uv.exe --no-config python find --managed-python $Pin
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$InstallArgs = @('install_release.py', $RahRoot, (Join-Path $PSScriptRoot 'uv.exe'))
if ($BootstrapOnly) { $InstallArgs += '--bootstrap-only' }
& $RahPython @InstallArgs
exit $LASTEXITCODE
