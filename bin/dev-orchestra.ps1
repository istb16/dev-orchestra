<#
.SYNOPSIS
  Windows wrapper for the dev-orchestra CLI.
.DESCRIPTION
  Locates the repository root relative to this script and runs the Python entry
  point with the first Python 3.11+ interpreter found on PATH.
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $root 'scripts/dev_orchestra.py'

# `python` before `python3` here, which is the opposite of the POSIX
# wrapper: on Windows a `python3` on PATH is usually the Store's app execution
# alias, which opens the Microsoft Store instead of running anything. `py` is
# the launcher a python.org install ships.
# A name that runs an older Python is passed over rather than run: the next one
# may well be a newer one.
function Test-Python([string]$Path) {
    try {
        & $Path -c 'import sys; sys.exit(sys.version_info < (3, 11))' *> $null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

$python = $null
foreach ($candidate in @('python', 'py', 'python3')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found -and (Test-Python $found.Source)) { $python = $found.Source; break }
}
if (-not $python) {
    Write-Error 'dev-orchestra: no Python 3.11+ was found on PATH'
    exit 127
}

& $python $entry @Args
exit $LASTEXITCODE
