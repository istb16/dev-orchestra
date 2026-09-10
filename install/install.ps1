<#
.SYNOPSIS
  Install the AI Development Orchestrator skill on Windows.

.DESCRIPTION
  Claude Code: links (or copies) this checkout into the skills directory.
  Codex CLI:   appends a marked pointer block to AGENTS.md.

  SKILL.md is never duplicated - the Codex install points at it.

  Symlink creation on Windows needs Developer Mode or an elevated shell; the
  installer falls back to a copy automatically when it cannot link.

.EXAMPLE
  .\install\install.ps1
  .\install\install.ps1 -Copy
  .\install\install.ps1 -Project C:\code\my-app
  .\install\install.ps1 -Codex
#>
[CmdletBinding()]
param(
    [switch]$Codex,
    [switch]$Copy,
    [string]$Project
)

$ErrorActionPreference = 'Stop'
$SkillName = 'dev-orchestra'
$root = Split-Path -Parent $PSScriptRoot

function Add-ProjectGitExclude {
    # A per-project install drops a directory (usually a link to this git
    # checkout) inside someone else's repository. Left alone, `git add -A`
    # there fails with "does not have a commit checked out". Exclude it
    # locally, which touches neither their .gitignore nor their history.
    param([string]$Destination)

    if (-not $Project) { return }
    $gitDir = Join-Path $Project '.git'
    if (-not (Test-Path -LiteralPath $gitDir -PathType Container)) { return }

    $entry = '/' + ($Destination.Substring($Project.Length).TrimStart('\', '/') -replace '\\', '/')
    $infoDir = Join-Path $gitDir 'info'
    New-Item -ItemType Directory -Force -Path $infoDir | Out-Null
    $excludeFile = Join-Path $infoDir 'exclude'

    $existing = if (Test-Path -LiteralPath $excludeFile) { @(Get-Content -LiteralPath $excludeFile) } else { @() }
    if ($existing -notcontains $entry) {
        Set-Content -LiteralPath $excludeFile -Value ($existing + $entry) -Encoding utf8
        Write-Host "Excluded $entry via .git/info/exclude (local only)"
    }
}

function Install-ClaudeSkill {
    if ($Project) {
        $skillsDir = Join-Path $Project '.claude/skills'
    }
    else {
        $base = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
        $skillsDir = Join-Path $base 'skills'
    }
    New-Item -ItemType Directory -Force -Path $skillsDir | Out-Null
    $dest = Join-Path $skillsDir $SkillName

    if (Test-Path -LiteralPath $dest) {
        Write-Host "Replacing existing install at $dest"
        Remove-Item -LiteralPath $dest -Recurse -Force -Confirm:$false
    }

    Add-ProjectGitExclude -Destination $dest

    if (-not $Copy) {
        try {
            New-Item -ItemType SymbolicLink -Path $dest -Target $root -ErrorAction Stop | Out-Null
            Write-Host "Linked $dest -> $root"
            Write-Host "``git pull`` in the checkout now upgrades the skill in place."
            return
        }
        catch {
            Write-Warning 'Could not create a symlink (Developer Mode off?). Copying instead.'
        }
    }

    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    foreach ($item in 'skills', '.claude-plugin', '.codex-plugin', 'README.md', 'LICENSE', 'references', 'scripts', 'bin', 'agents', 'examples') {
        $source = Join-Path $root $item
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination $dest -Recurse -Force
        }
    }
    Write-Host "Copied the skill to $dest"
    Write-Host 'Re-run this installer after `git pull` to upgrade.'
}

function Install-CodexPointer {
    if ($Project) {
        $agentsFile = Join-Path $Project 'AGENTS.md'
    }
    else {
        $base = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
        $agentsFile = Join-Path $base 'AGENTS.md'
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $agentsFile) | Out-Null

    $begin = "<!-- BEGIN $SkillName -->"
    $end = "<!-- END $SkillName -->"

    $lines = if (Test-Path -LiteralPath $agentsFile) { @(Get-Content -LiteralPath $agentsFile) } else { @() }

    # Idempotent: strip any previous block before appending the current one.
    $kept = New-Object System.Collections.Generic.List[string]
    $skip = $false
    foreach ($line in $lines) {
        if ($line -match [regex]::Escape($begin)) { $skip = $true }
        if (-not $skip) { $kept.Add($line) }
        if ($line -match [regex]::Escape($end)) { $skip = $false }
    }

    $block = @(
        $begin
        '## AI Development Orchestrator'
        ''
        'When the request is to implement a feature or issue, investigate and fix a bug,'
        'run a multi-model code review, orchestrate development across AI CLIs, or change'
        'the development agent configuration, read and follow:'
        ''
        "    $root/skills/dev-orchestra/SKILL.md"
        ''
        'Its helper CLI is:'
        ''
        "    python $root/scripts/dev_orchestra.py <command>"
        ''
        'That file is the single source of truth; do not rely on a copy of it.'
        $end
    )

    Set-Content -LiteralPath $agentsFile -Value ($kept + $block) -Encoding utf8
    Write-Host "Added the pointer block to $agentsFile"
}

if ($Codex) { Install-CodexPointer } else { Install-ClaudeSkill }

Write-Host ''
Write-Host 'Verify with:'
Write-Host "    $root\bin\dev-orchestra.ps1 doctor"
