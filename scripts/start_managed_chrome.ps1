param(
    [ValidatePattern('^[A-Za-z0-9_.-]{1,64}$')]
    [string]$ProfileId = 'authorized',

    [ValidateRange(1, 65535)]
    [int]$Port = 9222,

    [string]$StartUrl = 'about:blank'
)

$chromeCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
    (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe')
)
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
    $StartUrl
)
Start-Process -FilePath $chromePath -ArgumentList $arguments

Write-Output "Managed Chrome started with profile '$ProfileId'."
Write-Output "DevTools address: 127.0.0.1:$Port"
Write-Output 'Log in manually, keep this Chrome open, then use profile_mode=attach.'
