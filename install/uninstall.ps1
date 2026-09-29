<#
.SYNOPSIS
  Remove the AI Development Orchestrator skill on Windows.

.DESCRIPTION
  Removes the skills-directory entry, the marked AGENTS.md pointer block, or
  with -Antigravity (or -Gemini) the plugin directory the installer made.
  Your configuration is left alone; to remove that too, run:
      dev-orchestra config reset --scope global --delete

.EXAMPLE
  .\install\uninstall.ps1
  .\install\uninstall.ps1 -Project C:\code\my-app
  .\install\uninstall.ps1 -Codex
  .\install\uninstall.ps1 -Antigravity
  .\install\uninstall.ps1 -Antigravity -Project C:\code\my-app
#>
[CmdletBinding()]
param(
    [switch]$Codex,
    [Alias('Gemini')]
    [switch]$Antigravity,
    [string]$Project
)

$ErrorActionPreference = 'Stop'
$SkillName = 'dev-orchestra'
$root = Split-Path -Parent $PSScriptRoot

if ($Codex -and $Antigravity) {
    [Console]::Error.WriteLine('-Antigravity and -Codex cannot be combined')
    exit 2
}

# The same names install.ps1 writes.
$Sentinel = '.dev-orchestra-install'
$ExcludeMarker = '# added by dev-orchestra install --antigravity'
$ExcludeEntry = "/.agents/plugins/$SkillName"

function Get-LinkTarget {
    # Where a junction or symlink points, as a plain path. Windows PowerShell
    # returns an array and may prefix a junction's target with \\?\. Reading
    # it does not need the target to exist.
    param($Item)

    $target = [string]@($Item.Target)[0]
    foreach ($prefix in '\\?\', '\??\') {
        if ($target.StartsWith($prefix)) { $target = $target.Substring($prefix.Length) }
    }
    return $target.TrimEnd('\', '/')
}

function Stop-Refused {
    param([string[]]$Lines)

    foreach ($line in $Lines) { [Console]::Error.WriteLine($line) }
    exit 1
}

function Remove-OwnedDestination {
    # Remove the install at $Destination, or stop. A link is removed only when
    # it resolves to this checkout, and never recursively; a directory only
    # when the installer wrote it (the sentinel is there and it is not a
    # clone); anything else is left where it is. Returns $true when something
    # was removed.
    param([string]$Destination)

    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        # Get-Item can miss a dangling link; the attributes cannot.
        try { $attributes = [System.IO.File]::GetAttributes($Destination) } catch { return $false }
        if ($attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            Stop-Refused @(
                "$Destination is a link to a path that cannot be read, not to this checkout; the installer did not make it."
                'Remove it by hand if it is no longer wanted:'
                "    [System.IO.Directory]::Delete('$($Destination -replace "'", "''")', `$false)"
            )
        }
        Stop-Refused @(
            "$Destination exists and the installer did not write it; it was left in place."
            'Remove it by hand if it is no longer wanted.'
        )
    }

    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        $target = Get-LinkTarget $item
        if ($target -and ($target -ieq $root.TrimEnd('\', '/'))) {
            [System.IO.Directory]::Delete($Destination, $false)
            return $true
        }
        if (-not $target) { $target = 'a path that cannot be read' }
        Stop-Refused @(
            "$Destination is a link to $target, not to this checkout; the installer did not make it."
            'Remove it by hand if it is no longer wanted:'
            "    [System.IO.Directory]::Delete('$($Destination -replace "'", "''")', `$false)"
        )
    }

    $hasSentinel = Test-Path -LiteralPath (Join-Path $Destination $Sentinel) -PathType Leaf
    $gitEntry = Get-Item -LiteralPath (Join-Path $Destination '.git') -Force -ErrorAction SilentlyContinue
    if ($item.PSIsContainer -and $hasSentinel -and ($null -eq $gitEntry)) {
        Remove-Item -LiteralPath $Destination -Recurse -Force -Confirm:$false
        return $true
    }
    Stop-Refused @(
        "$Destination exists and the installer did not write it; it was left in place."
        'Remove it by hand if it is no longer wanted.'
    )
}

function Remove-MarkedGitExclude {
    # Drop the entry the installer added, and its marker. An entry without the
    # marker right above it was there before and stays.
    if (-not $Project) { return }
    $excludeFile = Join-Path $Project '.git/info/exclude'
    if (-not (Test-Path -LiteralPath $excludeFile -PathType Leaf)) { return }
    # A full path: the .NET call below does not follow Set-Location.
    $excludeFile = (Get-Item -LiteralPath $excludeFile -Force).FullName

    $lines = @(Get-Content -LiteralPath $excludeFile)
    $kept = New-Object System.Collections.Generic.List[string]
    $removed = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -eq $ExcludeMarker -and ($i + 1) -lt $lines.Count -and $lines[$i + 1] -eq $ExcludeEntry) {
            $removed = $true
            $i++
            continue
        }
        $kept.Add($lines[$i])
    }
    if (-not $removed) { return }
    if ($kept.Count -gt 0) {
        [System.IO.File]::WriteAllLines($excludeFile, $kept.ToArray(), (New-Object System.Text.UTF8Encoding($false)))
    }
    else {
        Clear-Content -LiteralPath $excludeFile
    }
    Write-Host "Removed $ExcludeEntry from .git/info/exclude"
}

if ($Antigravity) {
    if ($Project) {
        $pluginsDir = Join-Path $Project '.agents/plugins'
    }
    else {
        $pluginsDir = Join-Path $HOME '.gemini/config/plugins'
    }
    # The full path, because the .NET calls above do not follow Set-Location.
    $resolved = Get-Item -LiteralPath $pluginsDir -Force -ErrorAction SilentlyContinue
    if ($resolved) { $pluginsDir = $resolved.FullName }
    $dest = Join-Path $pluginsDir $SkillName

    if (Remove-OwnedDestination -Destination $dest) {
        Write-Host "Removed $dest"
    }
    else {
        Write-Host "Nothing installed at $dest"
    }
    Remove-MarkedGitExclude
    Write-Host 'Restart Antigravity so that it stops loading the plugin.'
}
elseif (-not $Codex) {
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
