<#
.SYNOPSIS
  Windows wrapper for the ai-orchestrator CLI.
.DESCRIPTION
  Locates the repository root relative to this script and runs the Python entry
  point with whatever Python 3 interpreter is available.
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $root 'scripts/ai_orchestrator.py'

$python = $null
foreach ($candidate in @('python3', 'python', 'py')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $python = $found.Source; break }
}
if (-not $python) {
    Write-Error 'ai-orchestrator: Python 3.9+ is required but was not found on PATH'
    exit 127
}

& $python $entry @Args
exit $LASTEXITCODE
