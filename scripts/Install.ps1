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
if ((Read-Host "Install Research Agent Hub, Local Deep Research, GPT Researcher and PaperQA2 in $RahRoot? Internet access and space for three app environments are required. [y/N]") -notmatch '^y(es)?$') { exit 0 }
$Pin = (Get-Content python-version.txt -Raw).Trim()
& .\uv.exe --no-config python install $Pin
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$RahPython = & .\uv.exe --no-config python find --managed-python $Pin
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $RahPython install_release.py $RahRoot (Join-Path $PSScriptRoot 'uv.exe')
exit $LASTEXITCODE
