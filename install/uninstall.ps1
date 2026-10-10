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
$ClaudeExcludeMarker = '# added by dev-orchestra install --claude'
$ClaudeExcludeEntry = "/.claude/skills/$SkillName"

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
    # and its CLI are there, and every entry in it, at any depth and hidden
    # or not, is at the same path in this checkout's payload, so removing it
    # loses nothing the checkout does not have. Python's bytecode caches are
    # left out (see Test-BytecodeCache).
    param([string]$Path)

    foreach ($relative in @("skills/$SkillName/SKILL.md", 'scripts/orchestrator/__init__.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Path $relative) -PathType Leaf)) { return $false }
    }
    try { $entries = @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop) } catch { return $false }
    foreach ($entry in $entries) {
        if ($Payload -notcontains $entry.Name) { return $false }
        if (-not (Test-CheckoutHas $entry (Join-Path $root $entry.Name))) { return $false }
    }
    return $true
}

function Test-CheckoutHas {
    # Whether $Entry, and everything under it, is at $Twin in the checkout:
    # a directory as a directory, a file as a file. The installer never wrote
    # a link or junction into a copy, so one inside it, which is not
    # followed, fails the check, and so does what cannot be read.
    param($Entry, [string]$Twin)

    if ($Entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $false }
    if (-not $Entry.PSIsContainer) { return [System.IO.File]::Exists($Twin) }
    if ($Entry.Name -eq '__pycache__') { return (Test-BytecodeCache $Entry) }
    if (-not [System.IO.Directory]::Exists($Twin)) { return $false }
    try { $children = @(Get-ChildItem -LiteralPath $Entry.FullName -Force -ErrorAction Stop) } catch { return $false }
    foreach ($child in $children) {
        if (-not (Test-CheckoutHas $child (Join-Path $Twin $child.Name))) { return $false }
    }
    return $true
}

function Test-BytecodeCache {
    # Whether the __pycache__ directory $Entry holds only what Python wrote
    # there while the CLI ran from the copy: .pyc files, which Python makes
    # again. Such a directory needs no twin in the checkout. Anything else in
    # it -- another file, a link, a junction, a directory -- fails the check.
    param($Entry)

    try { $children = @(Get-ChildItem -LiteralPath $Entry.FullName -Force -ErrorAction Stop) } catch { return $false }
    foreach ($child in $children) {
        if ($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $false }
        if ($child.PSIsContainer -or $child.Name -notlike '*.pyc') { return $false }
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
    "    Remove-Item -LiteralPath '$([System.Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent($Path))' -Recurse -Force"
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
                "    [System.IO.Directory]::Delete('$([System.Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent($Destination))', `$false)"
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
            "    [System.IO.Directory]::Delete('$([System.Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent($Destination))', `$false)"
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

# AGENTS.md and .git/info/exclude are read and written as UTF-8, whichever
# PowerShell runs this: Windows PowerShell's Get-Content reads them in the
# ANSI code page, and its `-Encoding utf8` writes a BOM, which together
# garble every non-ASCII line. A file that is not UTF-8 is not rewritten at
# all.
function Read-TextFile {
    # The text of $Path as Lines, each with the line ending it has, so that
    # what is written back keeps them (the last may have none), and Bom, true
    # when it starts with a UTF-8 BOM. $null when the file is not UTF-8, a
    # UTF-16 or UTF-32 one included. Decoded here, not by ReadAllText, which
    # decodes by any BOM it finds and replaces what it cannot read.
    param([string]$Path)

    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $offset = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { $offset = 3 }
    $strict = New-Object System.Text.UTF8Encoding($false, $true)
    try { $text = $strict.GetString($bytes, $offset, $bytes.Length - $offset) }
    catch [System.Text.DecoderFallbackException] { return $null }
    # NUL is valid UTF-8, but a text file has none: it is UTF-16 or UTF-32
    # without a BOM.
    if ($text.Contains([string][char]0)) { return $null }
    $lines = [regex]::Split($text, '(?<=\n)')
    if ($lines[-1] -eq '') { $lines = @($lines | Select-Object -SkipLast 1) }
    return @{ Lines = [string[]]$lines; Bom = ($offset -eq 3) }
}

function Get-LineText {
    # A line from Read-TextFile without its line ending.
    param([string]$Line)

    return $Line.TrimEnd([char[]]"`r`n")
}

function Get-KeptLines {
    # $Kept, what is left of $Lines once a block was taken out, ending the
    # way $Lines did: when the last line had no line ending, the line now
    # last loses its own.
    param([string[]]$Lines, [string[]]$Kept)

    if ($Kept.Count -gt 0 -and $Lines.Count -gt 0 -and -not $Lines[-1].EndsWith("`n")) {
        $Kept[-1] = $Kept[-1] -replace '\r?\n$', ''
    }
    return , $Kept
}

function Write-TextLines {
    # $Lines as Read-TextFile gave them, then $Added, each ending the way the
    # file's lines already do, in UTF-8 with a BOM only when $Bom is set.
    # After a last line without a line ending, the added lines start on a
    # line of their own; with $KeepOpenEnd the last of them then gets none
    # either, so that the file still ends the way it did.
    param([string]$Path, [string[]]$Lines, [string[]]$Added, [switch]$Bom, [switch]$KeepOpenEnd)

    $newline = [Environment]::NewLine
    foreach ($line in $Lines) {
        if ($line.EndsWith("`r`n")) { $newline = "`r`n"; break }
        if ($line.EndsWith("`n")) { $newline = "`n"; break }
    }
    $builder = New-Object System.Text.StringBuilder
    foreach ($line in $Lines) { [void]$builder.Append($line) }
    if ($Added) {
        $open = $Lines.Count -gt 0 -and -not $Lines[-1].EndsWith("`n")
        # A last line without a line ending would otherwise run into the first added one.
        if ($open) { [void]$builder.Append($newline) }
        for ($i = 0; $i -lt $Added.Count; $i++) {
            [void]$builder.Append($Added[$i])
            if ($i -lt $Added.Count - 1 -or -not ($open -and $KeepOpenEnd)) { [void]$builder.Append($newline) }
        }
    }
    $encoding = New-Object System.Text.UTF8Encoding([bool]$Bom)
    # WriteAllText writes no BOM for an empty text even when asked: add it.
    $bytes = $encoding.GetPreamble() + $encoding.GetBytes($builder.ToString())
    [System.IO.File]::WriteAllBytes($Path, [byte[]]$bytes)
}

function Remove-MarkedGitExclude {
    # Drop $Entry, which the installer added, and its $Marker. An entry
    # without the marker right above it was there before and stays.
    param([string]$Entry, [string]$Marker)

    if (-not $Project) { return }
    $excludeFile = Join-Path $Project '.git/info/exclude'
    if (-not (Test-Path -LiteralPath $excludeFile -PathType Leaf)) { return }
    # A full path: the .NET call below does not follow Set-Location.
    $excludeFile = (Get-Item -LiteralPath $excludeFile -Force).FullName

    $read = Read-TextFile $excludeFile
    if ($null -eq $read) {
        Write-Warning "$excludeFile is not UTF-8; remove $Entry from it by hand if it is no longer wanted."
        return
    }
    $lines = $read.Lines
    $kept = New-Object System.Collections.Generic.List[string]
    $removed = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ((Get-LineText $lines[$i]) -eq $Marker -and ($i + 1) -lt $lines.Count -and (Get-LineText $lines[$i + 1]) -eq $Entry) {
            $removed = $true
            $i++
            continue
        }
        $kept.Add($lines[$i])
    }
    if ($removed) {
        Write-TextLines $excludeFile $kept.ToArray()
        Write-Host "Removed $Entry from .git/info/exclude"
    }
    # The entry without a marker: what an installer before the marker wrote,
    # or the user's own line. Either way it is not removed, only named.
    if (@($kept | ForEach-Object { Get-LineText $_ }) -contains $Entry) {
        Write-Host "Left $Entry in .git/info/exclude: the installer did not mark it as its own."
        Write-Host 'An older installer may have added it; remove the line by hand if it is no longer wanted.'
    }
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
    Remove-MarkedGitExclude -Entry $ExcludeEntry -Marker $ExcludeMarker
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
    Remove-MarkedGitExclude -Entry $ClaudeExcludeEntry -Marker $ClaudeExcludeMarker
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
        # A full path: the .NET calls below do not follow Set-Location.
        $agentsFile = (Get-Item -LiteralPath $agentsFile -Force).FullName
        $read = Read-TextFile $agentsFile
        if ($null -eq $read) {
            Stop-Refused @(
                "$agentsFile is not UTF-8; it was left as it is."
                'Save it as UTF-8, then re-run.'
            )
        }
        # The lines kept are written back as they were read, line endings and
        # a BOM included, and a file without a final line ending stays without one.
        $lines = $read.Lines
        $kept = New-Object System.Collections.Generic.List[string]
        $skip = $false
        $found = $false
        foreach ($line in $lines) {
            if ($line.Contains($begin)) { $skip = $true; $found = $true }
            if (-not $skip) { $kept.Add($line) }
            if ($line.Contains($end)) { $skip = $false }
        }
        if ($skip) {
            # A BEGIN with no END after it: what follows may be the user's own
            # text, so nothing is taken out.
            Stop-Refused @(
                "$agentsFile has $begin with no $end after it; it was left as it is."
                'Remove the unfinished block by hand, then re-run.'
            )
        }
        if ($found) {
            Write-TextLines $agentsFile (Get-KeptLines $lines ($kept.ToArray())) -Bom:($read.Bom)
            Write-Host "Removed the pointer block from $agentsFile"
        }
        else {
            Write-Host "No pointer block found in $agentsFile"
        }
    }
}

Write-Host "The checkout at $root was left in place."
