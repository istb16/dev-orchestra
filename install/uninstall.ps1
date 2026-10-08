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

# The same names install.ps1 writes, and what its copy carries.
$Sentinel = '.dev-orchestra-install'
$Payload = @('plugin.json', 'skills', '.claude-plugin', '.codex-plugin', 'README.md', 'LICENSE', 'references', 'scripts', 'bin', 'agents', 'examples')
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

function Resolve-RealPath {
    # $Path with every link and junction along it followed, so that two
    # names for one directory compare equal. What does not exist is kept as
    # it is written.
    param([string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    # A bound, in case two links point at each other.
    for ($hop = 0; $hop -lt 40; $hop++) {
        $followed = $false
        $prefix = $full
        while ($prefix) {
            $item = Get-Item -LiteralPath $prefix -Force -ErrorAction SilentlyContinue
            if ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                $target = Get-LinkTarget $item
                if ($target) {
                    if (-not [System.IO.Path]::IsPathRooted($target)) {
                        $target = [System.IO.Path]::Combine([System.IO.Path]::GetDirectoryName($prefix), $target)
                    }
                    $full = [System.IO.Path]::GetFullPath($target + $full.Substring($prefix.Length))
                    $followed = $true
                }
                break
            }
            $prefix = [System.IO.Path]::GetDirectoryName($prefix)
        }
        if (-not $followed) { break }
    }
    return $full.TrimEnd('\', '/')
}

function Test-UnmarkedCopy {
    # A Claude copy made before the installer wrote the sentinel: the skill
    # and its CLI are there, and nothing at the top that a copy does not
    # carry, so removing it loses nothing the checkout does not have.
    param([string]$Path)

    foreach ($relative in @("skills/$SkillName/SKILL.md", 'scripts/orchestrator/__init__.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Path $relative) -PathType Leaf)) { return $false }
    }
    foreach ($entry in @(Get-ChildItem -LiteralPath $Path -Force)) {
        if ($Payload -notcontains $entry.Name) { return $false }
    }
    return $true
}

function Get-FullCopyNote {
    # A full copy of a checkout, .git included, where the Claude install goes:
    # what install.sh left under Git Bash, whose `ln -s` copies, before it
    # checked for a link. It cannot be told from a clone, so it is explained,
    # never removed.
    param([string]$Path)

    if ($Antigravity) { return }
    if (-not (Get-Item -LiteralPath (Join-Path $Path '.git') -Force -ErrorAction SilentlyContinue)) { return }
    if (-not (Test-Path -LiteralPath (Join-Path $Path "skills/$SkillName/SKILL.md") -PathType Leaf)) { return }
    'If it is a full copy of a checkout, .git included, that an earlier install.sh'
    'made under Git Bash, not a clone you work in, remove it once you have checked'
    'it holds nothing of yours:'
    "    Remove-Item -LiteralPath '$($Path -replace "'", "''")' -Recurse -Force"
}

function Stop-Refused {
    param([string[]]$Lines)

    foreach ($line in $Lines) { [Console]::Error.WriteLine($line) }
    exit 1
}

function Test-ReparsePoint {
    # A link or junction, dangling or not.
    param([string]$Path)

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($item) { return [bool]($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) }
    # Get-Item can miss a dangling link; the attributes cannot. Remove-OwnedDestination
    # reads them too before it gets here, deliberately: it needs three answers.
    try { return [bool]([System.IO.File]::GetAttributes($Path) -band [System.IO.FileAttributes]::ReparsePoint) }
    catch { return $false }
}

function Remove-Link {
    # Remove a link itself, never what it points at. A directory link or
    # junction goes through Directory.Delete without recursion; a file symlink
    # has no Directory attribute and is removed like a file.
    param([string]$Path)

    $attributes = [System.IO.File]::GetAttributes($Path)
    if ($attributes -band [System.IO.FileAttributes]::Directory) {
        [System.IO.Directory]::Delete($Path, $false)
    }
    else {
        # Not Remove-Item: the provider can fail to find a dangling file link.
        [System.IO.File]::Delete($Path)
    }
}

function Remove-OwnedDestination {
    # Remove the install at $Destination, or stop. A link is removed only when
    # it resolves to this checkout, and never recursively; a directory only
    # when the installer wrote it (the sentinel is there, or it is an older
    # Claude copy, and it is not a clone or the checkout itself); anything
    # else is left where it is. Returns $true when something was removed.
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

    if (Test-ReparsePoint $Destination) {
        $target = Get-LinkTarget $item
        # Both sides with every link followed, so that a link made through a
        # junction to the checkout, or a checkout run through one, still
        # counts as this checkout. Following the link itself, rather than
        # its target text, also places a relative target correctly.
        if ($target -and ((Resolve-RealPath $Destination) -ieq (Resolve-RealPath $root))) {
            Remove-Link $Destination
            return $true
        }
        if (-not $target) { $target = 'a path that cannot be read' }
        Stop-Refused @(
            "$Destination is a link to $target, not to this checkout; the installer did not make it."
            'Remove it by hand if it is no longer wanted:'
            "    [System.IO.Directory]::Delete('$($Destination -replace "'", "''")', `$false)"
        )
    }

    # The checkout itself, reached by its own path or through a link above it.
    if ($item.PSIsContainer -and ((Resolve-RealPath $Destination) -ieq (Resolve-RealPath $root))) {
        Stop-Refused @(
            "$Destination is this checkout itself; removing it would delete the checkout."
            'The installer did not put it there. Delete the checkout by hand if it is no longer wanted.'
        )
    }

    $gitEntry = Get-Item -LiteralPath (Join-Path $Destination '.git') -Force -ErrorAction SilentlyContinue
    if ($item.PSIsContainer -and ($null -eq $gitEntry)) {
        $hasSentinel = Test-Path -LiteralPath (Join-Path $Destination $Sentinel) -PathType Leaf
        if ($hasSentinel -or (-not $Antigravity -and (Test-UnmarkedCopy $Destination))) {
            Remove-Item -LiteralPath $Destination -Recurse -Force -Confirm:$false
            return $true
        }
    }
    Stop-Refused (@(
        "$Destination exists and the installer did not write it; it was left in place."
        'Remove it by hand if it is no longer wanted.'
    ) + @(Get-FullCopyNote $Destination))
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
        $skillsDir = Join-Path $Project '.claude/skills'
    }
    else {
        $base = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
        $skillsDir = Join-Path $base 'skills'
    }
    # The full path, because the .NET calls above do not follow Set-Location.
    # Missing, nothing is installed and no .NET call is made.
    $resolved = Get-Item -LiteralPath $skillsDir -Force -ErrorAction SilentlyContinue
    if ($resolved) { $skillsDir = $resolved.FullName }
    $dest = Join-Path $skillsDir $SkillName

    if ($resolved -and (Remove-OwnedDestination -Destination $dest)) {
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
