<#
.SYNOPSIS
  Install the AI Development Orchestrator skill on Windows.

.DESCRIPTION
  Claude Code: links (or copies) this checkout into the skills directory.
  Codex CLI:   appends a marked pointer block to AGENTS.md.
  Antigravity: links (or copies) this checkout into ~/.gemini/config/plugins,
               or <project>/.agents/plugins with -Project. -Gemini is the
               same switch. Restart Antigravity afterwards.

  SKILL.md is never duplicated - the Codex install points at it.

  Symlink creation on Windows needs Developer Mode or an elevated shell; the
  installer falls back to a copy automatically when it cannot link. The
  Antigravity install makes a junction first, which needs neither.

.EXAMPLE
  .\install\install.ps1
  .\install\install.ps1 -Copy
  .\install\install.ps1 -Project C:\code\my-app
  .\install\install.ps1 -Codex
  .\install\install.ps1 -Antigravity
  .\install\install.ps1 -Antigravity -Project C:\code\my-app
#>
[CmdletBinding()]
param(
    [switch]$Codex,
    [Alias('Gemini')]
    [switch]$Antigravity,
    [switch]$Copy,
    [string]$Project
)

$ErrorActionPreference = 'Stop'
$SkillName = 'dev-orchestra'
$root = Split-Path -Parent $PSScriptRoot

if ($Codex -and $Antigravity) {
    [Console]::Error.WriteLine('-Antigravity and -Codex cannot be combined')
    exit 2
}

# What a copy install carries: the plugin payload, not .git, tests or CI.
$Payload = @('plugin.json', 'skills', '.claude-plugin', '.codex-plugin', 'README.md', 'LICENSE', 'references', 'scripts', 'bin', 'agents', 'examples')

# Written into every copy the installer makes, so that a later run can tell a
# directory it made from one it did not.
$Sentinel = '.dev-orchestra-install'

# The project's .git/info/exclude gets each entry below its own comment, and
# the uninstaller removes an entry only when its comment is right above it.
$ExcludeMarker = '# added by dev-orchestra install --antigravity'
$ExcludeEntry = "/.agents/plugins/$SkillName"
$ClaudeExcludeMarker = '# added by dev-orchestra install --claude'
$ClaudeExcludeEntry = "/.claude/skills/$SkillName"

# The pointer block tells the host how to run the CLI, so it has to name an
# interpreter this machine actually has. Same order as bin/dev-orchestra.ps1:
# a `python3` on PATH here is usually the Store's alias, which opens the
# Microsoft Store rather than running anything. A name that runs a Python older
# than 3.11 is passed over, the same as the wrapper does.
function Test-Python([string]$Path) {
    try {
        & $Path -c 'import sys; sys.exit(sys.version_info < (3, 11))' *> $null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

$PythonCmd = 'python'
foreach ($candidate in @('python', 'py', 'python3')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found -and (Test-Python $found.Source)) {
        $PythonCmd = $candidate
        break
    }
}

function Test-Utf8Bom {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    return ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
}

# AGENTS.md and .git/info/exclude are read and written as UTF-8 without a
# BOM, whichever PowerShell runs this: Windows PowerShell's Get-Content reads
# them in the ANSI code page, and its `-Encoding utf8` writes a BOM, which
# together garble every non-ASCII line. A file that is not UTF-8 is not
# rewritten at all.
function Read-TextLines {
    # The lines of $Path, each with the line ending it has, so that what is
    # written back keeps them; the last may have none. $null when the file is
    # not UTF-8. A BOM is dropped.
    param([string]$Path)

    $strict = New-Object System.Text.UTF8Encoding($false, $true)
    try { $text = [System.IO.File]::ReadAllText($Path, $strict) }
    catch [System.Text.DecoderFallbackException] { return $null }
    $lines = [regex]::Split($text, '(?<=\n)')
    if ($lines[-1] -eq '') { $lines = @($lines | Select-Object -SkipLast 1) }
    # The comma keeps an empty or one-line array an array.
    return , [string[]]$lines
}

function Get-LineText {
    # A line from Read-TextLines without its line ending.
    param([string]$Line)

    return $Line.TrimEnd([char[]]"`r`n")
}

function Write-TextLines {
    # $Lines as Read-TextLines returned them, then $Added, each ending the way
    # the file's lines already do. UTF-8 without a BOM.
    param([string]$Path, [string[]]$Lines, [string[]]$Added)

    $newline = [Environment]::NewLine
    foreach ($line in $Lines) {
        if ($line.EndsWith("`r`n")) { $newline = "`r`n"; break }
        if ($line.EndsWith("`n")) { $newline = "`n"; break }
    }
    $builder = New-Object System.Text.StringBuilder
    foreach ($line in $Lines) { [void]$builder.Append($line) }
    if ($Added) {
        # A last line without a line ending would otherwise run into the first added one.
        if ($builder.Length -gt 0 -and -not $Lines[-1].EndsWith("`n")) { [void]$builder.Append($newline) }
        foreach ($line in $Added) { [void]$builder.Append($line).Append($newline) }
    }
    [System.IO.File]::WriteAllText($Path, $builder.ToString(), (New-Object System.Text.UTF8Encoding($false)))
}

function Add-GitExcludeLines {
    # Append $Lines to <project>/.git/info/exclude unless $Entry is already a
    # line there, with a marker or without one. No BOM: git would read it as
    # part of the first pattern.
    param([string]$Entry, [string[]]$Lines)

    $gitDir = Join-Path $Project '.git'
    if (-not (Test-Path -LiteralPath $gitDir -PathType Container)) { return }
    # A full path: the .NET call below does not follow Set-Location.
    $infoDir = (New-Item -ItemType Directory -Force -Path (Join-Path $gitDir 'info')).FullName
    $excludeFile = Join-Path $infoDir 'exclude'
    $existing = [string[]]@()
    if (Test-Path -LiteralPath $excludeFile) {
        $existing = Read-TextLines $excludeFile
        if ($null -eq $existing) {
            Write-Warning "$excludeFile is not UTF-8; add $Entry to it by hand to keep the install out of git status."
            return
        }
    }
    if (@($existing | ForEach-Object { Get-LineText $_ }) -notcontains $Entry) {
        Write-TextLines $excludeFile $existing $Lines
        Write-Host "Excluded $Entry via .git/info/exclude (local only)"
    }
    elseif (Test-Utf8Bom $excludeFile) {
        # An earlier installer wrote the entry with a BOM, which hides it from git.
        Write-TextLines $excludeFile $existing
    }
}

function Add-ProjectGitExclude {
    # A per-project install drops a directory (usually a link to this git
    # checkout) inside someone else's repository. Left alone, `git add -A`
    # there fails with "does not have a commit checked out". Exclude it
    # locally, which touches neither their .gitignore nor their history.
    if (-not $Project) { return }
    Add-GitExcludeLines -Entry $ClaudeExcludeEntry -Lines @($ClaudeExcludeMarker, $ClaudeExcludeEntry)
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

function Copy-Payload {
    param([string]$Destination)

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($item in $Payload) {
        $source = Join-Path $root $item
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination $Destination -Recurse -Force
        }
    }
}

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

function Remove-OwnedDestination {
    # Clear the way for an install at $Destination, or stop. A link is removed
    # only when it resolves to this checkout, and never recursively; a
    # directory only when this installer wrote it (the sentinel is there, or
    # it is an older Claude copy, and it is not a clone or the checkout
    # itself); anything else is left where it is. Returns $true when
    # something was removed.
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
            'Remove it by hand if it is no longer wanted, then re-run.'
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

    # The checkout itself, reached by its own path or through a link above it:
    # removing it would remove the checkout, and the skill is already there.
    if ($item.PSIsContainer -and ((Resolve-RealPath $Destination) -ieq (Resolve-RealPath $root))) {
        Stop-Refused @(
            "$Destination is this checkout itself; replacing it would delete the checkout."
            'Nothing to install: it is already in place. To link or copy it, run the installer from a checkout somewhere else.'
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
        'Remove it by hand if it is no longer wanted, then re-run.'
    ) + @(Get-FullCopyNote $Destination))
}

function Get-AutoloadEntries {
    # Entries at the checkout root that Antigravity would load along with the
    # skill. Kept in step with ANTIGRAVITY_AUTOLOAD in scripts/orchestrator/hosts.py.
    $found = New-Object System.Collections.Generic.List[string]
    foreach ($entry in @('hooks.json', 'mcp_config.json', 'plugins.json', 'rules')) {
        if (Get-Item -LiteralPath (Join-Path $root $entry) -Force -ErrorAction SilentlyContinue) {
            $found.Add($entry)
        }
    }
    $agentsDir = Join-Path $root 'agents'
    if (Test-Path -LiteralPath $agentsDir -PathType Container) {
        foreach ($file in @(Get-ChildItem -LiteralPath $agentsDir -Filter '*.md' -Force)) {
            $found.Add('agents/' + $file.Name)
        }
    }
    return $found.ToArray()
}

function Add-MarkedGitExclude {
    if (-not $Project) { return }
    Add-GitExcludeLines -Entry $ExcludeEntry -Lines @($ExcludeMarker, $ExcludeEntry)
}

function Copy-ForAntigravity {
    param([string]$Destination)

    Copy-Payload -Destination $Destination
    # Antigravity would load an agents/*.md as an agent definition; the payload
    # needs only the rest of agents/.
    $agentsDir = Join-Path $Destination 'agents'
    if (Test-Path -LiteralPath $agentsDir -PathType Container) {
        foreach ($file in @(Get-ChildItem -LiteralPath $agentsDir -Filter '*.md' -Force)) {
            Remove-Item -LiteralPath $file.FullName -Force -Confirm:$false
        }
    }
    Set-Content -LiteralPath (Join-Path $Destination $Sentinel) -Value "Installed by install/install.ps1 from $root" -Encoding utf8
    Write-Host "Copied the plugin to $Destination"
    Write-Host 'Re-run this installer after `git pull` to upgrade.'
}

function Install-AntigravityPlugin {
    if ($Project) {
        $pluginsDir = Join-Path $Project '.agents/plugins'
    }
    else {
        $pluginsDir = Join-Path $HOME '.gemini/config/plugins'
    }
    # First, so that a fresh home or a project path that does not exist yet
    # works, and before anything below resolves a path. The full path, because
    # the .NET calls below do not follow Set-Location.
    $pluginsDir = (New-Item -ItemType Directory -Force -Path $pluginsDir).FullName
    $dest = Join-Path $pluginsDir $SkillName

    # A link exposes the whole working tree, and Antigravity loads more than
    # skills/ from a plugin root. A copy carries only the payload, without
    # agents/*.md. Checked before anything is removed, so that a refused run
    # leaves the existing install in place.
    if (-not $Copy) {
        $loaded = @(Get-AutoloadEntries)
        if ($loaded.Count -gt 0) {
            $lines = @('Refusing to link: Antigravity would also load these from the checkout:')
            foreach ($entry in $loaded) { $lines += "    $entry" }
            $lines += 'Remove them, re-run with -Copy, or install from a clean worktree.'
            Stop-Refused $lines
        }
    }

    if (Remove-OwnedDestination -Destination $dest) {
        Write-Host "Replaced the existing install at $dest"
    }

    $linked = $false
    if (-not $Copy) {
        # A junction needs neither Developer Mode nor elevation, but cannot
        # point at a network path; a symlink can, when it is allowed.
        # Junctions are Windows only: elsewhere PowerShell answers the request
        # without making a link.
        $kinds = @('SymbolicLink')
        if ($env:OS -eq 'Windows_NT') { $kinds = @('Junction', 'SymbolicLink') }
        $failures = @()
        foreach ($kind in $kinds) {
            try {
                New-Item -ItemType $kind -Path $dest -Target $root -ErrorAction Stop | Out-Null
                $made = Get-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue
                if (-not ($made -and ($made.Attributes -band [System.IO.FileAttributes]::ReparsePoint))) {
                    throw 'no link was made'
                }
                $mechanism = 'symlink'
                if ($kind -eq 'Junction') { $mechanism = 'junction' }
                Write-Host "Linked ($mechanism) $dest -> $root"
                Write-Host "``git pull`` in the checkout now upgrades the plugin in place."
                $linked = $true
                break
            }
            catch {
                $failures += ($kind + ': ' + $_.Exception.Message)
                # A failed attempt must not leave an empty directory behind
                # for the next one to trip over.
                $left = Get-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue
                if ($left -and $left.PSIsContainer -and -not ($left.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -and -not (Get-ChildItem -LiteralPath $dest -Force)) {
                    Remove-Item -LiteralPath $dest -Force -Confirm:$false
                }
            }
        }
        if (-not $linked) {
            Write-Warning ('Could not create a junction or a symlink (' + ($failures -join '; ') + '). Copying instead.')
        }
    }
    if (-not $linked) {
        Copy-ForAntigravity -Destination $dest
    }

    # Only now: a failed install leaves the exclude file as it was.
    Add-MarkedGitExclude
    Write-Host 'Restart Antigravity: a newly installed plugin directory is only discovered on startup.'
}

function Install-ClaudeSkill {
    if ($Project) {
        $skillsDir = Join-Path $Project '.claude/skills'
    }
    else {
        $base = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
        $skillsDir = Join-Path $base 'skills'
    }
    # The full path, because the .NET calls below do not follow Set-Location.
    $skillsDir = (New-Item -ItemType Directory -Force -Path $skillsDir).FullName
    $dest = Join-Path $skillsDir $SkillName

    if (Remove-OwnedDestination -Destination $dest) {
        Write-Host "Replaced the existing install at $dest"
    }

    $linked = $false
    if (-not $Copy) {
        try {
            New-Item -ItemType SymbolicLink -Path $dest -Target $root -ErrorAction Stop | Out-Null
            Write-Host "Linked $dest -> $root"
            Write-Host "``git pull`` in the checkout now upgrades the skill in place."
            $linked = $true
        }
        catch {
            Write-Warning 'Could not create a symlink (Developer Mode off?). Copying instead.'
        }
    }

    if (-not $linked) {
        Copy-Payload -Destination $dest
        Set-Content -LiteralPath (Join-Path $dest $Sentinel) -Value "Installed by install/install.ps1 from $root" -Encoding utf8
        Write-Host "Copied the skill to $dest"
        Write-Host 'Re-run this installer after `git pull` to upgrade.'
    }

    # Only now: a failed install leaves the exclude file as it was.
    Add-ProjectGitExclude
}

function Install-CodexPointer {
    if ($Project) {
        $agentsFile = Join-Path $Project 'AGENTS.md'
    }
    else {
        $base = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
        $agentsFile = Join-Path $base 'AGENTS.md'
    }
    # The full path, because the .NET calls below do not follow Set-Location.
    $agentsDir = (New-Item -ItemType Directory -Force -Path (Split-Path -Parent $agentsFile)).FullName
    $agentsFile = Join-Path $agentsDir (Split-Path -Leaf $agentsFile)

    $begin = "<!-- BEGIN $SkillName -->"
    $end = "<!-- END $SkillName -->"

    $lines = [string[]]@()
    if (Test-Path -LiteralPath $agentsFile) {
        $lines = Read-TextLines $agentsFile
        if ($null -eq $lines) {
            Stop-Refused @(
                "$agentsFile is not UTF-8; it was left as it is."
                'Save it as UTF-8, then re-run.'
            )
        }
    }

    # Idempotent: strip any previous block before appending the current one.
    # The lines kept are written back as they were read, line endings included.
    $kept = New-Object System.Collections.Generic.List[string]
    $skip = $false
    foreach ($line in $lines) {
        if ($line.Contains($begin)) { $skip = $true }
        if (-not $skip) { $kept.Add($line) }
        if ($line.Contains($end)) { $skip = $false }
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
        "    $PythonCmd $root/scripts/dev_orchestra.py <command>"
        ''
        'That file is the single source of truth; do not rely on a copy of it.'
        $end
    )

    Write-TextLines $agentsFile $kept.ToArray() $block
    Write-Host "Added the pointer block to $agentsFile"
}

if ($Codex) { Install-CodexPointer } elseif ($Antigravity) { Install-AntigravityPlugin } else { Install-ClaudeSkill }

Write-Host ''
Write-Host 'Verify with:'
Write-Host "    $root\bin\dev-orchestra.ps1 doctor"
Write-Host "    $root\bin\dev-orchestra.ps1 config setup --preset standard"
