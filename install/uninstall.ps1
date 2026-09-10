<#
.SYNOPSIS
  Remove the AI Development Orchestrator skill on Windows.

.DESCRIPTION
  Removes the skills-directory entry, or the marked AGENTS.md pointer block.
  Your configuration is left alone; to remove that too, run:
      dev-orchestra config reset --scope global --delete

.EXAMPLE
  .\install\uninstall.ps1
  .\install\uninstall.ps1 -Project C:\code\my-app
  .\install\uninstall.ps1 -Codex
#>
[CmdletBinding()]
param(
    [switch]$Codex,
    [string]$Project
)

$ErrorActionPreference = 'Stop'
$SkillName = 'dev-orchestra'
$root = Split-Path -Parent $PSScriptRoot

if (-not $Codex) {
    if ($Project) {
        $dest = Join-Path $Project ".claude/skills/$SkillName"
    }
    else {
        $base = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
        $dest = Join-Path $base "skills/$SkillName"
    }
    if (Test-Path -LiteralPath $dest) {
        Remove-Item -LiteralPath $dest -Recurse -Force -Confirm:$false
        Write-Host "Removed $dest"
    }
    else {
        Write-Host "Nothing installed at $dest"
    }
}
else {
    if ($Project) {
        $agentsFile = Join-Path $Project 'AGENTS.md'
    }
    else {
        $base = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
        $agentsFile = Join-Path $base 'AGENTS.md'
    }
    $begin = "<!-- BEGIN $SkillName -->"
    $end = "<!-- END $SkillName -->"

    if (-not (Test-Path -LiteralPath $agentsFile)) {
        Write-Host "No pointer block found in $agentsFile"
    }
    else {
        $kept = New-Object System.Collections.Generic.List[string]
        $skip = $false
        $found = $false
        foreach ($line in @(Get-Content -LiteralPath $agentsFile)) {
            if ($line -match [regex]::Escape($begin)) { $skip = $true; $found = $true }
            if (-not $skip) { $kept.Add($line) }
            if ($line -match [regex]::Escape($end)) { $skip = $false }
        }
        if ($found) {
            Set-Content -LiteralPath $agentsFile -Value $kept -Encoding utf8
            Write-Host "Removed the pointer block from $agentsFile"
        }
        else {
            Write-Host "No pointer block found in $agentsFile"
        }
    }
}

Write-Host "The checkout at $root was left in place."
