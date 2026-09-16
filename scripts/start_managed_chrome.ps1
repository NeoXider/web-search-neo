param(
    [ValidatePattern('^[A-Za-z0-9_.-]{1,64}$')]
    [string]$ProfileId = 'authorized',

    [ValidateRange(1, 65535)]
    [int]$Port = 9222,

    [ValidateSet('visible', 'headless')]
    [string]$WindowMode = 'visible',

    [string]$StartUrl = 'about:blank'
)

# Windows PowerShell 5.1 joins an -ArgumentList array with bare spaces, so an
# argument such as a profile path under "C:\Users\John Doe" arrives split in two.
# Quote each argument the way CommandLineToArgvW parses it and pass one string.
function ConvertTo-ProcessArgument([string]$Value) {
    if ($Value -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

$chromeCandidates = foreach ($root in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    # ProgramFiles(x86) is absent on 32-bit Windows, and Join-Path rejects $null.
    if (-not [string]::IsNullOrWhiteSpace($root)) {
        Join-Path $root 'Google\Chrome\Application\chrome.exe'
    }
}
$chromePath = $chromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $chromePath) {
    throw 'Google Chrome executable was not found.'
}

$profileRoot = Join-Path $env:LOCALAPPDATA 'WebSearchNeo\profiles'
$profilePath = Join-Path $profileRoot $ProfileId
New-Item -ItemType Directory -Force -Path $profilePath | Out-Null

$arguments = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=$profilePath",
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding'
)
if ($WindowMode -eq 'headless') {
    $arguments += '--headless=new'
    $arguments += '--window-size=1440,900'
}
$arguments += $StartUrl
$argumentLine = ($arguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '

if ($WindowMode -eq 'headless') {
    Start-Process -FilePath $chromePath -ArgumentList $argumentLine -WindowStyle Hidden
} else {
    Start-Process -FilePath $chromePath -ArgumentList $argumentLine
}

Write-Output "Managed Chrome started with profile '$ProfileId'."
Write-Output "DevTools address: 127.0.0.1:$Port"
Write-Output "Window mode: $WindowMode"
if ($WindowMode -eq 'visible') {
    Write-Output 'Log in manually, keep this Chrome open, then use profile_mode=attach.'
} else {
    Write-Output 'The managed Chrome is running without a visible window; use profile_mode=attach.'
}
