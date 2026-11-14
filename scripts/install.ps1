# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Install one release that release-binary.yml published, on Windows x86_64 or arm64:
#
#   .\install.ps1 OWNER/REPO TAG [DIRECTORY] [-ModifyPath]
#
# It downloads SHA256SUMS, the Windows archive and the provenance bundle from
# the release, then installs nothing unless the GitHub CLI verifies SHA256SUMS
# and the archive against that bundle, as signed by release-policy's
# release-binary.yml for OWNER/REPO at TAG on a GitHub-hosted runner, and the
# archive matches its line in SHA256SUMS. The archive's files go into
# DIRECTORY, by default %LOCALAPPDATA%\Programs\<stem>. With -ModifyPath it
# then adds DIRECTORY to the end of the user's own Path, unless that Path
# already holds it; the machine Path is never read or changed. Windows
# PowerShell 5.1 and PowerShell 7 both run it.
param([string]$Repository, [string]$Tag, [string]$Directory, [switch]$ModifyPath)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Signer = 'ryanduguid/release-policy/.github/workflows/release-binary.yml'
$Name = '\A[A-Za-z0-9][A-Za-z0-9_.+-]*\z'

function Deny-Install([string]$Message) {
    throw "install.ps1: $Message"
}

function Test-Argument {
    if (-not $Repository -or -not $Tag) {
        Deny-Install 'usage: install.ps1 OWNER/REPO TAG [DIRECTORY] [-ModifyPath]'
    }
    if ($ModifyPath -and $Directory.Contains(';')) {
        Deny-Install "-ModifyPath cannot add a directory holding ';' to Path: $Directory"
    }
    if ($Repository -cnotmatch '\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\z') {
        Deny-Install "OWNER/REPO must be one owner and one repository: $Repository"
    }
    if ($Tag -cnotmatch '\A(?:[A-Za-z0-9_.+-]+/)*v[0-9][A-Za-z0-9_.+-]*\z' -or $Tag.Contains('..')) {
        Deny-Install "the tag must end in v and the version, such as v1.2.3 or tool/v1.2.3: $Tag"
    }
    if ($env:OS -ne 'Windows_NT') {
        Deny-Install 'no Windows here: use install.sh on Linux and macOS'
    }
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $script:Target = switch ($arch) {
        'AMD64' { 'x86_64-pc-windows-msvc' }
        'ARM64' { 'aarch64-pc-windows-msvc' }
        default { Deny-Install "no build for Windows ${arch}: releases ship Windows x86_64 and arm64 only" }
    }
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Deny-Install 'the GitHub CLI (gh) is required to verify build provenance: https://cli.github.com/'
    }
}

function Get-Asset([string]$File) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$base/$File" -OutFile (Join-Path $work $File)
    } catch {
        Deny-Install "could not download $File from ${base}: $($_.Exception.Message)"
    }
}

# Returns the digest and name on the one SHA256SUMS line for this target.
function Read-Sum {
    $lines = @(Get-Content -LiteralPath (Join-Path $work 'SHA256SUMS') |
        Where-Object { $_.EndsWith($suffix, [StringComparison]::Ordinal) })
    if ($lines.Count -ne 1) {
        Deny-Install "SHA256SUMS lists $($lines.Count) archives for $Target, not 1"
    }
    $line = [regex]::Match($lines[0], '\A([0-9a-f]{64})  (\S+)\z')
    $archive = $line.Groups[2].Value
    if (-not $line.Success -or -not $archive.EndsWith($suffix, [StringComparison]::Ordinal) -or
        $archive.Substring(0, $archive.Length - $suffix.Length) -cnotmatch $Name) {
        Deny-Install "not a release archive line in SHA256SUMS: $($lines[0])"
    }
    return $line.Groups[1].Value, $archive
}

function Test-Provenance([string]$Archive, [string]$Expected) {
    foreach ($subject in 'SHA256SUMS', $Archive) {
        # A gh that never runs the CLI must not inherit an earlier success.
        $global:LASTEXITCODE = 1
        & gh attestation verify (Join-Path $work $subject) --bundle (Join-Path $work "$stem-$version.intoto.jsonl") `
            --repo $Repository --signer-workflow $Signer --source-ref "refs/tags/$Tag" `
            --deny-self-hosted-runners | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Deny-Install "$subject failed provenance verification for $Repository $Tag"
        }
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $work $Archive)).Hash.ToLowerInvariant()
    if ($actual -cne $Expected) {
        Deny-Install "$Archive does not match SHA256SUMS"
    }
}

# Extracts the archive and installs its files, each a plain top-level file.
function Install-Archive([string]$Archive, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead((Join-Path $work $Archive))
    try {
        $members = @($zip.Entries | ForEach-Object { $_.FullName })
    } finally {
        $zip.Dispose()
    }
    if ($members.Count -eq 0) {
        Deny-Install "$Archive is empty"
    }
    foreach ($member in $members) {
        if ($member -cnotmatch $Name) {
            Deny-Install "$Archive holds an unexpected member: $member"
        }
    }
    $files = Join-Path $work 'files'
    Expand-Archive -LiteralPath (Join-Path $work $Archive) -DestinationPath $files
    foreach ($member in $members) {
        if (Test-Path -LiteralPath (Join-Path $Destination $member) -PathType Container) {
            Deny-Install "$(Join-Path $Destination $member) is a directory"
        }
    }
    # Copy every file into a new directory beside the install, then move each
    # into place, so no name an earlier occupant chose is written.
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $stage = Join-Path $Destination ('.install.' + [IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Path $stage | Out-Null
    try {
        foreach ($member in $members) {
            Copy-Item -LiteralPath (Join-Path $files $member) -Destination (Join-Path $stage $member)
        }
        foreach ($member in $members) {
            Move-Item -LiteralPath (Join-Path $stage $member) -Destination (Join-Path $Destination $member) -Force
        }
    } finally {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Adds Folder to the end of the user's Path in HKCU:\Environment unless an
# entry already names it, keeping the value's REG_EXPAND_SZ type and its
# entries as written, %VARIABLES% included, then tells running programs.
function Add-UserPath([string]$Folder) {
    $current = [string](Get-Item -LiteralPath 'HKCU:\Environment').GetValue(
        'Path', '', 'DoNotExpandEnvironmentNames')
    $entries = @($current -split ';' | Where-Object { $_ })
    $same = @($entries | Where-Object {
        [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -eq $Folder.TrimEnd('\') })
    if ($same.Count -gt 0) {
        Write-Output "Your user Path already holds $Folder; it is unchanged."
        return
    }
    try {
        Set-ItemProperty -LiteralPath 'HKCU:\Environment' -Name Path -Type ExpandString `
            -Value (($entries + $Folder) -join ';')
    } catch {
        Deny-Install "installed into $Folder, but could not change your user Path, which is unchanged: $($_.Exception.Message)"
    }
    Write-Output "Added $Folder to the end of your user Path (HKCU:\Environment, value Path)."
    try {
        # WM_SETTINGCHANGE to every top-level window, as setting a variable
        # in System Properties does, so a terminal opened now sees the change.
        Add-Type -Namespace ReleasePolicyInstall -Name User32 -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode)]
public static extern IntPtr SendMessageTimeout(IntPtr window, uint message, UIntPtr wParam,
    string lParam, uint flags, uint timeout, out UIntPtr result);
'@
        $result = [UIntPtr]::Zero
        [void][ReleasePolicyInstall.User32]::SendMessageTimeout(
            [IntPtr]0xFFFF, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result)
        Write-Output 'Open a new terminal to use it.'
    } catch {
        Write-Output 'Sign out and in again to use it everywhere; running programs keep the old Path.'
    }
    Write-Output "To undo, remove $Folder from Path in 'Edit environment variables for your account' (rundll32 sysdm.cpl,EditEnvironmentVariables)."
}

if ($args.Count -gt 0) {
    Deny-Install 'usage: install.ps1 OWNER/REPO TAG [DIRECTORY] [-ModifyPath]'
}
Test-Argument
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$version = ($Tag -split '/')[-1].Substring(1)
$suffix = "-$version-$Target.zip"
$base = "https://github.com/$Repository/releases/download/$Tag"
$work = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $work | Out-Null
try {
    Get-Asset -File 'SHA256SUMS'
    $expected, $archive = Read-Sum
    $stem = $archive.Substring(0, $archive.Length - $suffix.Length)
    if (-not $Directory) {
        $Directory = Join-Path (Join-Path $env:LOCALAPPDATA 'Programs') $stem
    }
    Get-Asset -File $archive
    Get-Asset -File "$stem-$version.intoto.jsonl"
    Test-Provenance -Archive $archive -Expected $expected
    Install-Archive -Archive $archive -Destination $Directory
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Output "Installed $archive into $Directory, verified against SHA256SUMS and its provenance."
if ($ModifyPath) {
    Add-UserPath -Folder (Resolve-Path -LiteralPath $Directory).ProviderPath
} elseif (($env:PATH -split ';') -notcontains $Directory) {
    Write-Output "Add $Directory to PATH to run it."
}
