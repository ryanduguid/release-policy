// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: Apache-2.0

//! Package, check and validate the per-target archives of a Rust binary release.

use crate::py::{self, OsStyle, PyPath};
use crate::pyjson::{self, Json};
use crate::{Result, common::*};
use crc32fast::Hasher;
use fancy_regex::Regex as FancyRegex;
use flate2::read::{DeflateDecoder, MultiGzDecoder};
use flate2::write::DeflateEncoder;
use flate2::{Compression, GzBuilder};
use regex::Regex;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::time::{Duration, Instant};

pub const MAX_MEMBERS: usize = 12;
pub const MAX_MEMBER_BYTES: usize = 512 * 1024 * 1024;
pub const MAX_ARCHIVE_BYTES: usize = MAX_MEMBER_BYTES;
pub const MAX_DECOMPRESSED_BYTES: usize = MAX_MEMBER_BYTES + 64 * 1024;
const MAX_DIGEST_BYTES: u64 = 1024;
const ZIP_END: &[u8; 4] = b"PK\x05\x06";
const ZIP64_LOCATOR: &[u8; 4] = b"PK\x06\x07";
const ZIP_END_SEARCH: usize = 22 + 0xFFFF;
const MAX_ZIP_DIRECTORY_BYTES: usize = 64 * 1024;

pub const DOCUMENTS: [&str; 3] = ["LICENSE", "NOTICE", "README.md"];
pub const REQUIRED_DOCUMENTS: [&str; 2] = ["LICENSE", "README.md"];

pub const WINDOWS_SYSTEM_DLLS: [&str; 22] = [
    "advapi32.dll",
    "bcrypt.dll",
    "bcryptprimitives.dll",
    "crypt32.dll",
    "dbghelp.dll",
    "iphlpapi.dll",
    "kernel32.dll",
    "mswsock.dll",
    "ncrypt.dll",
    "ntdll.dll",
    "ole32.dll",
    "oleaut32.dll",
    "psapi.dll",
    "rpcrt4.dll",
    "secur32.dll",
    "shell32.dll",
    "shlwapi.dll",
    "user32.dll",
    "userenv.dll",
    "version.dll",
    "winhttp.dll",
    "ws2_32.dll",
];

const MACHO_MAGIC_64: &[u8; 4] = b"\xcf\xfa\xed\xfe";
const MACHO_EXECUTE: u32 = 0x2;
const MACHO_LOAD_DYLINKER: u32 = 0xE;
const MACHO_MALFORMED: &str = "malformed Mach-O load commands";
const RUNNER_DIRECTORIES: [&str; 3] = ["GITHUB_WORKSPACE", "RUNNER_TEMP", "CARGO_TARGET_DIR"];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Target {
    pub triple: &'static str,
    pub suffix: &'static str,
    pub executable_suffix: &'static str,
    pub object_format: &'static str,
    pub arch: &'static str,
    pub role: &'static str,
}

pub const TARGETS: [Target; 6] = [
    Target {
        triple: "x86_64-unknown-linux-musl",
        suffix: ".tar.gz",
        executable_suffix: "",
        object_format: "elf64-x86-64",
        arch: "x86_64",
        role: "linux",
    },
    Target {
        triple: "aarch64-unknown-linux-musl",
        suffix: ".tar.gz",
        executable_suffix: "",
        object_format: "elf64-littleaarch64",
        arch: "aarch64",
        role: "linux-arm64",
    },
    Target {
        triple: "x86_64-apple-darwin",
        suffix: ".tar.gz",
        executable_suffix: "",
        object_format: "Mach-O 64-bit x86-64",
        arch: "x86_64",
        role: "macos",
    },
    Target {
        triple: "aarch64-apple-darwin",
        suffix: ".tar.gz",
        executable_suffix: "",
        object_format: "Mach-O arm64",
        arch: "aarch64",
        role: "macos-arm64",
    },
    Target {
        triple: "x86_64-pc-windows-msvc",
        suffix: ".zip",
        executable_suffix: ".exe",
        object_format: "COFF-x86-64",
        arch: "x86_64",
        role: "windows",
    },
    Target {
        triple: "aarch64-pc-windows-msvc",
        suffix: ".zip",
        executable_suffix: ".exe",
        object_format: "COFF-ARM64",
        arch: "aarch64",
        role: "windows-arm64",
    },
];

pub fn handoff_spec() -> Vec<(String, String)> {
    let mut out = vec![
        ("source-zip".into(), ".zip".into()),
        ("source-tar".into(), ".tar.gz".into()),
    ];
    for t in TARGETS {
        out.push((
            format!("{}-archive", t.role),
            format!("-{}{}", t.triple, t.suffix),
        ));
    }
    out.push(("source-sbom".into(), ".spdx.json".into()));
    for t in TARGETS {
        out.push((
            format!("{}-sbom", t.role),
            format!("-{}.spdx.json", t.triple),
        ));
    }
    out
}

static STEM_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z").unwrap());
static BINARY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[a-z0-9]+(?:[-_][a-z0-9]+)*\z").unwrap());
static VERSION_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\z").unwrap()
});
static DATA_FILE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*\z").unwrap()
});
static TOOLCHAIN_CHANNEL_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[1-9][0-9]*\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\z").unwrap());
static DIGEST_LINE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A(?P<digest>[0-9a-f]{64}) [ *](?P<name>[^\r\n]+)\r?\n\z").unwrap()
});
static SHA256_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A[0-9a-f]{64}\z").unwrap());
static WINDOWS_RUNTIME_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\A(?:vcruntime|msvcp|msvcr|ucrtbase|api-ms-win-crt-).*\z").unwrap()
});
static DLL_NAME_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\A[A-Za-z0-9][A-Za-z0-9_.-]*\.dll\z").unwrap());
static API_SET_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\A(?:api|ext)-ms-win-[a-z0-9-]+\.dll\z").unwrap());
static MACOS_SYSTEM_LIBRARY_RE: LazyLock<FancyRegex> = LazyLock::new(|| {
    FancyRegex::new(
        r"\A(?:/usr/lib/[A-Za-z0-9_+-][A-Za-z0-9_.+-]*\.dylib|/System/Library/Frameworks/([A-Za-z0-9_]+)\.framework/Versions/[A-Z0-9]+/\1)\z",
    )
    .unwrap()
});
static NEEDED_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"NeededLibraries \[\n(?P<names>(?:[^\]\n]*\n)*?)\]").unwrap());

fn target_for(triple: &str) -> Result<Target> {
    TARGETS
        .iter()
        .find(|t| t.triple == triple)
        .copied()
        .ok_or_else(|| format!("unsupported target: {triple}"))
}

fn checked<'a>(pattern: &Regex, value: &'a str, label: &str) -> Result<&'a str> {
    if !pattern.is_match(value) {
        return Err(format!("{label} is not valid: {}", py::repr_str(value)));
    }
    Ok(value)
}

pub fn asset_name(stem: &str, version: &str, triple: &str) -> Result<String> {
    checked(&STEM_RE, stem, "artifact-stem")?;
    checked(&VERSION_RE, version, "version")?;
    let target = target_for(triple)?;
    Ok(format!("{stem}-{version}-{triple}{}", target.suffix))
}

pub fn binaries(binary: &str) -> Result<Vec<String>> {
    let mut names = Vec::new();
    let mut seen = HashSet::new();
    for part in binary.split_whitespace() {
        let name = checked(&BINARY_RE, part, "binary")?.to_owned();
        if !seen.insert(name.clone()) {
            return Err(format!(
                "binary must name one or more distinct targets: {}",
                py::repr_str(binary)
            ));
        }
        names.push(name);
    }
    if names.is_empty() {
        return Err(format!(
            "binary must name one or more distinct targets: {}",
            py::repr_str(binary)
        ));
    }
    Ok(names)
}

pub fn data_names(data_files: &str) -> Result<Vec<(String, String)>> {
    let mut ordered = Vec::new();
    let mut seen: HashSet<String> = DOCUMENTS.iter().map(|s| (*s).to_owned()).collect();
    for line in py::splitlines(data_files) {
        let entry = py::strip(line);
        if entry.is_empty() {
            continue;
        }
        let valid = checked(&DATA_FILE_RE, entry, "data file")?;
        let name = valid.rsplit('/').next().unwrap_or(valid).to_owned();
        if !seen.insert(name.clone()) {
            return Err(format!(
                "data file {entry} would ship as {name}, a name already taken"
            ));
        }
        ordered.push((name, entry.to_owned()));
    }
    Ok(ordered)
}

pub fn member_names(
    binary: &str,
    triple: &str,
    documents: &[&str],
    data: &[&str],
) -> Result<Vec<String>> {
    let suffix = target_for(triple)?.executable_suffix;
    let mut names: Vec<String> = binaries(binary)?
        .into_iter()
        .map(|b| format!("{b}{suffix}"))
        .collect();
    let mut extras: Vec<String> = data
        .iter()
        .chain(documents.iter())
        .map(|s| (*s).to_owned())
        .collect();
    extras.sort();
    names.extend(extras);
    let unique: HashSet<&str> = names.iter().map(String::as_str).collect();
    if unique.len() != names.len() || names.len() > MAX_MEMBERS {
        let refs: Vec<&str> = names.iter().map(String::as_str).collect();
        return Err(format!(
            "the archive cannot hold {}: at most {MAX_MEMBERS} distinct members",
            repr_list(refs)
        ));
    }
    Ok(names)
}

fn read_toml(path: &Path, label: &str) -> Result<toml::Value> {
    let raw = read_bytes(path)?;
    let text = py::decode_utf8(&raw).map_err(|e| format!("{label}: {e}"))?;
    toml::from_str::<toml::Value>(&text).map_err(|e| format!("{label}: {e}"))
}

fn package_meta<'a>(document: &'a toml::Value, mdisp: &str) -> Result<(&'a str, &'a str)> {
    let Some(package) = document.get("package").and_then(toml::Value::as_table) else {
        return Err(format!("{mdisp} has no [package] table"));
    };
    let Some(name) = package.get("name").and_then(toml::Value::as_str) else {
        return Err(format!(
            "{mdisp} must state a literal package name and version; workspace inheritance is not supported"
        ));
    };
    let Some(version) = package.get("version").and_then(toml::Value::as_str) else {
        return Err(format!(
            "{mdisp} must state a literal package name and version; workspace inheritance is not supported"
        ));
    };
    if package.get("publish").and_then(toml::Value::as_bool) != Some(false) {
        return Err(format!(
            "{mdisp} must set publish = false: binaries ship on GitHub Releases only"
        ));
    }
    Ok((name, version))
}

fn declared_bins<'a>(document: &'a toml::Value, name: &'a str) -> HashSet<&'a str> {
    let mut declared = HashSet::from([name]);
    if let Some(bins) = document.get("bin").and_then(toml::Value::as_array) {
        for item in bins {
            if let Some(bin_name) = item
                .as_table()
                .and_then(|t| t.get("name"))
                .and_then(toml::Value::as_str)
            {
                declared.insert(bin_name);
            }
        }
    }
    declared
}

pub fn cargo_package(manifest: &Path, release_version: &str, binary: &str) -> Result<String> {
    let mdisp = manifest.display();
    if symlink(manifest) {
        return Err(format!("{mdisp} must not be a symbolic link"));
    }
    let document = read_toml(
        manifest,
        &format!("{mdisp} is not a readable Cargo manifest"),
    )?;
    let (name, version) = package_meta(&document, &mdisp.to_string())?;
    checked(&VERSION_RE, version, &format!("{mdisp} package version"))?;
    if version != release_version {
        return Err(format!(
            "Cargo.toml version {version} != release version {release_version}"
        ));
    }
    let declared = declared_bins(&document, name);
    for each in binaries(binary)? {
        if !declared.contains(each.as_str()) {
            return Err(format!(
                "{each} is neither the package {name} nor a declared [[bin]]"
            ));
        }
    }
    Ok(name.to_owned())
}

pub fn find_documents(source: &Path, root: &Path) -> Result<BTreeMap<String, PathBuf>> {
    let mut found = BTreeMap::new();
    for name in DOCUMENTS {
        for directory in [source, root] {
            let candidate = directory.join(name);
            if candidate.is_file() && !symlink(&candidate) {
                found.insert(name.to_owned(), candidate);
                break;
            }
        }
    }
    let missing: Vec<&str> = REQUIRED_DOCUMENTS
        .iter()
        .copied()
        .filter(|d| !found.contains_key(*d))
        .collect();
    if !missing.is_empty() {
        return Err(format!("missing release documents: {}", missing.join(", ")));
    }
    Ok(found)
}

/// Resolve a path cleanly even when trailing components do not yet exist (`Path.resolve(strict=False)`).
pub fn resolve_path(path: &Path) -> PathBuf {
    resolve_with_depth(path, 0)
}

fn resolve_with_depth(path: &Path, depth: u8) -> PathBuf {
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(path))
            .unwrap_or_else(|_| path.to_path_buf())
    };
    let mut resolved = PathBuf::new();
    let mut unresolved_tail = false;
    for comp in abs.components() {
        match comp {
            Component::Prefix(_) | Component::RootDir => resolved.push(comp.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                resolved.pop();
            }
            Component::Normal(seg) => {
                resolved.push(seg);
                if unresolved_tail {
                    continue;
                }
                if let Ok(canon) = std::fs::canonicalize(&resolved) {
                    resolved = strip_verbatim(canon);
                } else if depth < 40
                    && let Ok(target) = std::fs::read_link(&resolved)
                {
                    let base = resolved.parent().unwrap_or(&resolved);
                    let next = if target.is_absolute() {
                        target
                    } else {
                        base.join(target)
                    };
                    resolved = resolve_with_depth(&next, depth + 1);
                } else {
                    unresolved_tail = true;
                }
            }
        }
    }
    resolved
}

fn strip_verbatim(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{rest}"))
    } else if let Some(rest) = text.strip_prefix(r"\\?\") {
        PathBuf::from(rest)
    } else {
        path
    }
}

fn path_equals(a: &Path, b: &Path) -> bool {
    py::path_normcase(a) == py::path_normcase(b)
}

fn is_relative_to(path: &Path, base: &Path) -> bool {
    let path_str = py::path_normcase(path);
    let base_str = py::path_normcase(base);
    let p_norm = Path::new(&path_str);
    let b_norm = Path::new(&base_str);
    p_norm.starts_with(b_norm)
}

pub fn find_data_files(source: &Path, data_files: &str) -> Result<BTreeMap<String, PathBuf>> {
    let mut found = BTreeMap::new();
    let resolved_source = resolve_path(source);
    for (name, entry) in data_names(data_files)? {
        let mut path = source.to_path_buf();
        let mut expected = resolved_source.clone();
        for part in entry.split('/') {
            path.push(part);
            expected.push(part);
        }
        if !path_equals(&resolve_path(&path), &expected) || !path.is_file() {
            return Err(format!(
                "data file {entry} is not a regular file in {}",
                source.display()
            ));
        }
        found.insert(name, path);
    }
    Ok(found)
}

fn ustar_header(name: &str, size: usize, mode: u32, epoch: u64) -> Result<[u8; 512]> {
    let name_bytes = name.as_bytes();
    let (short_name, prefix) = if name_bytes.len() <= 100 {
        (name_bytes, &[][..])
    } else if let Some(pos) = name_bytes.iter().rposition(|&b| b == b'/')
        && pos <= 155
        && name_bytes.len() - pos - 1 <= 100
    {
        (&name_bytes[pos + 1..], &name_bytes[..pos])
    } else {
        return Err(format!("USTAR member name too long: {name}"));
    };
    let mut header = [0u8; 512];
    header[..short_name.len()].copy_from_slice(short_name);
    write_octal(&mut header[100..108], mode as u64, 7);
    write_octal(&mut header[108..116], 0, 7);
    write_octal(&mut header[116..124], 0, 7);
    write_octal(&mut header[124..136], size as u64, 11);
    write_octal(&mut header[136..148], epoch, 11);
    header[148..156].fill(b' ');
    header[156] = b'0';
    header[257..263].copy_from_slice(b"ustar\0");
    header[263..265].copy_from_slice(b"00");
    header[345..345 + prefix.len()].copy_from_slice(prefix);
    let checksum: u32 = header.iter().map(|&b| u32::from(b)).sum();
    let cksum = format!("{checksum:06o}\0 ");
    header[148..156].copy_from_slice(cksum.as_bytes());
    Ok(header)
}

fn write_octal(slot: &mut [u8], value: u64, digits: usize) {
    let formatted = format!("{value:0digits$o}");
    let bytes = formatted.as_bytes();
    let start = bytes.len().saturating_sub(digits);
    slot[..digits].copy_from_slice(&bytes[start..]);
    slot[digits] = 0;
}

fn create_exclusive(path: &Path) -> Result<File> {
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| py::oserror_str(&e, &path.to_string_lossy(), OsStyle::Native))
}

fn write_tar_gz(output: &Path, members: &[(String, Vec<u8>, u32)], epoch: u64) -> Result<()> {
    let mut tar = Vec::new();
    for (name, data, mode) in members {
        tar.extend_from_slice(&ustar_header(name, data.len(), *mode, epoch)?);
        tar.extend_from_slice(data);
        let pad = (512 - (data.len() % 512)) % 512;
        tar.resize(tar.len() + pad, 0);
    }
    tar.resize(tar.len() + 1024, 0);

    let file = create_exclusive(output)?;
    let mut encoder = GzBuilder::new()
        .mtime(0)
        .write(file, Compression::default());
    encoder
        .write_all(&tar)
        .map_err(|e| py::oserror_str(&e, &output.to_string_lossy(), OsStyle::Native))?;
    encoder
        .finish()
        .map_err(|e| py::oserror_str(&e, &output.to_string_lossy(), OsStyle::Native))?;
    Ok(())
}

fn dos_datetime(epoch: u64) -> (u16, u16) {
    let clamped = epoch.max(315_532_800);
    let days = (clamped / 86_400) as i64;
    let secs = clamped % 86_400;
    let hour = (secs / 3600) as u16;
    let minute = ((secs % 3600) / 60) as u16;
    let second = (secs % 60) as u16;
    // Civil date from Unix days (Howard Hinnant).
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = (yoe as i64) + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u16;
    let month = (if mp < 10 { mp + 3 } else { mp - 9 }) as u16;
    let year = (y + if month <= 2 { 1 } else { 0 }) as u16;
    let dos_time = (hour << 11) | (minute << 5) | (second / 2);
    let dos_date = ((year.saturating_sub(1980)) << 9) | (month << 5) | day;
    (dos_time, dos_date)
}

fn write_zip_local(
    buf: &mut Vec<u8>,
    name: &[u8],
    compressed: &[u8],
    uncomp_len: u32,
    crc: u32,
    dos: (u16, u16),
) {
    let flags: u16 = if name.is_ascii() { 0 } else { 0x800 };
    buf.extend_from_slice(b"PK\x03\x04");
    buf.extend_from_slice(&20u16.to_le_bytes());
    buf.extend_from_slice(&flags.to_le_bytes());
    buf.extend_from_slice(&8u16.to_le_bytes());
    buf.extend_from_slice(&dos.0.to_le_bytes());
    buf.extend_from_slice(&dos.1.to_le_bytes());
    buf.extend_from_slice(&crc.to_le_bytes());
    buf.extend_from_slice(&(compressed.len() as u32).to_le_bytes());
    buf.extend_from_slice(&uncomp_len.to_le_bytes());
    buf.extend_from_slice(&(name.len() as u16).to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(name);
    buf.extend_from_slice(compressed);
}

#[allow(clippy::too_many_arguments)]
fn write_zip_central(
    central: &mut Vec<u8>,
    name: &[u8],
    comp_len: u32,
    uncomp_len: u32,
    crc: u32,
    mode: u32,
    local_offset: u32,
    dos: (u16, u16),
) {
    let flags: u16 = if name.is_ascii() { 0 } else { 0x800 };
    let ext_attr: u32 = (0o100000 | mode) << 16;
    central.extend_from_slice(b"PK\x01\x02");
    central.extend_from_slice(&((3u16 << 8) | 20u16).to_le_bytes());
    central.extend_from_slice(&20u16.to_le_bytes());
    central.extend_from_slice(&flags.to_le_bytes());
    central.extend_from_slice(&8u16.to_le_bytes());
    central.extend_from_slice(&dos.0.to_le_bytes());
    central.extend_from_slice(&dos.1.to_le_bytes());
    central.extend_from_slice(&crc.to_le_bytes());
    central.extend_from_slice(&comp_len.to_le_bytes());
    central.extend_from_slice(&uncomp_len.to_le_bytes());
    central.extend_from_slice(&(name.len() as u16).to_le_bytes());
    central.extend_from_slice(&[0u8; 8]);
    central.extend_from_slice(&ext_attr.to_le_bytes());
    central.extend_from_slice(&local_offset.to_le_bytes());
    central.extend_from_slice(name);
}

fn write_zip_eocd(buf: &mut Vec<u8>, central: &[u8], count: u16) {
    let cd_offset = buf.len() as u32;
    let cd_size = central.len() as u32;
    buf.extend_from_slice(central);
    buf.extend_from_slice(ZIP_END);
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.extend_from_slice(&count.to_le_bytes());
    buf.extend_from_slice(&count.to_le_bytes());
    buf.extend_from_slice(&cd_size.to_le_bytes());
    buf.extend_from_slice(&cd_offset.to_le_bytes());
    buf.extend_from_slice(&0u16.to_le_bytes());
}

fn write_zip(output: &Path, members: &[(String, Vec<u8>, u32)], epoch: u64) -> Result<()> {
    let dos = dos_datetime(epoch);
    let mut out = create_exclusive(output)?;
    let mut buf = Vec::new();
    let mut central = Vec::new();
    for (name, data, mode) in members {
        let local_offset = buf.len() as u32;
        let mut hasher = Hasher::new();
        hasher.update(data);
        let crc = hasher.finalize();
        let mut encoder = DeflateEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(data).map_err(|e| e.to_string())?;
        let compressed = encoder.finish().map_err(|e| e.to_string())?;
        let uncomp_len = data.len() as u32;
        write_zip_local(&mut buf, name.as_bytes(), &compressed, uncomp_len, crc, dos);
        write_zip_central(
            &mut central,
            name.as_bytes(),
            compressed.len() as u32,
            uncomp_len,
            crc,
            *mode,
            local_offset,
            dos,
        );
    }
    write_zip_eocd(&mut buf, &central, members.len() as u16);
    out.write_all(&buf)
        .map_err(|e| py::oserror_str(&e, &output.to_string_lossy(), OsStyle::Native))
}

#[allow(clippy::too_many_arguments)]
pub fn package(
    executables: &Path,
    binary: &str,
    triple: &str,
    stem: &str,
    version: &str,
    source: &Path,
    root: &Path,
    epoch: u64,
    output_dir: &Path,
    data_files: &str,
) -> Result<PathBuf> {
    let target = target_for(triple)?;
    let output = output_dir.join(asset_name(stem, version, triple)?);
    let docs = find_documents(source, root)?;
    let data = find_data_files(source, data_files)?;
    let mut files = BTreeMap::new();
    for (k, v) in docs {
        files.insert(k, v);
    }
    let doc_keys: Vec<String> = files.keys().cloned().collect();
    let doc_refs: Vec<&str> = doc_keys.iter().map(String::as_str).collect();
    let data_keys: Vec<String> = data.keys().cloned().collect();
    let data_refs: Vec<&str> = data_keys.iter().map(String::as_str).collect();
    for (k, v) in data {
        files.insert(k, v);
    }
    let names = member_names(binary, triple, &doc_refs, &data_refs)?;
    let count = binaries(binary)?.len();
    let mut members = Vec::with_capacity(names.len());
    for name in &names[..count] {
        members.push((name.clone(), read_bytes(&executables.join(name))?, 0o755));
    }
    for name in &names[count..] {
        members.push((name.clone(), read_bytes(&files[name])?, 0o644));
    }
    std::fs::create_dir_all(output_dir)
        .map_err(|e| py::oserror_str(&e, &output_dir.to_string_lossy(), OsStyle::Native))?;
    if target.suffix == ".zip" {
        write_zip(&output, &members, epoch)?;
    } else {
        write_tar_gz(&output, &members, epoch)?;
    }
    Ok(output)
}

fn admit(members: &BTreeMap<String, (Vec<u8>, u32)>, name: &str, size: usize) -> Result<()> {
    if members.contains_key(name) {
        return Err(format!("duplicate member {name}"));
    }
    let existing: usize = members.values().map(|(d, _)| d.len()).sum();
    let Some(total) = existing.checked_add(size) else {
        return Err(format!(
            "{name} exceeds the archive's member count or size limit"
        ));
    };
    if members.len() >= MAX_MEMBERS || size > MAX_MEMBER_BYTES || total > MAX_DECOMPRESSED_BYTES {
        return Err(format!(
            "{name} exceeds the archive's member count or size limit"
        ));
    }
    Ok(())
}

fn archive_filename(archive: &Path) -> String {
    archive
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| archive.to_string_lossy().into_owned())
}

fn u16_le(slice: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([slice[off], slice[off + 1]])
}

fn u32_le(slice: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([slice[off], slice[off + 1], slice[off + 2], slice[off + 3]])
}

fn zip_directory_is_small(archive: &Path, archive_name: &str) -> Result<(u16, u32, u32, u64)> {
    let size = std::fs::metadata(archive)
        .map_err(|e| py::oserror_str(&e, &archive.to_string_lossy(), OsStyle::Native))?
        .len();
    let mut handle = File::open(archive)
        .map_err(|e| py::oserror_str(&e, &archive.to_string_lossy(), OsStyle::Native))?;
    let start = size.saturating_sub((ZIP_END_SEARCH + 20) as u64);
    handle
        .seek(SeekFrom::Start(start))
        .map_err(|e| e.to_string())?;
    let mut tail = Vec::new();
    handle.read_to_end(&mut tail).map_err(|e| e.to_string())?;

    let end = tail
        .windows(4)
        .rposition(|w| w == ZIP_END)
        .filter(|&pos| tail.len() - pos >= 22)
        .ok_or_else(|| format!("{archive_name} has no ZIP end of central directory record"))?;

    if end >= 20 && &tail[end - 20..end - 16] == ZIP64_LOCATOR {
        return Err(format!(
            "{archive_name} is a ZIP64 archive; release archives are not"
        ));
    }
    let eocd = &tail[end..end + 22];
    let disk_num = u16_le(eocd, 4);
    let cd_disk = u16_le(eocd, 6);
    let on_disk = u16_le(eocd, 8);
    let total = u16_le(eocd, 10);
    let directory = u32_le(eocd, 12);
    let cd_offset = u32_le(eocd, 16);
    let comment_len = u16_le(eocd, 20) as usize;
    if on_disk != total
        || total as usize > MAX_MEMBERS
        || directory as usize > MAX_ZIP_DIRECTORY_BYTES
    {
        return Err(format!(
            "{archive_name} lists {total} members in a {directory}-byte directory"
        ));
    }
    if disk_num != 0 || cd_disk != 0 || end + 22 + comment_len > tail.len() {
        return Err(format!(
            "{archive_name}: malformed ZIP end of central directory"
        ));
    }
    Ok((total, directory, cd_offset, size))
}

struct ZipCdEntry {
    name: String,
    method: u16,
    crc: u32,
    comp_size: u32,
    uncomp_size: u32,
    ext_attr: u32,
    local_off: u32,
}

fn check_zip_cd_flags(
    extra: &[u8],
    comp_size: u32,
    uncomp_size: u32,
    local_off: u32,
    flag: u16,
    name: &str,
    archive_name: &str,
) -> Result<()> {
    if has_zip64_extra(extra)
        || comp_size == u32::MAX
        || uncomp_size == u32::MAX
        || local_off == u32::MAX
    {
        return Err(format!(
            "{archive_name} is a ZIP64 archive; release archives are not"
        ));
    }
    if flag & 1 != 0 {
        return Err(format!("{name}: encrypted ZIP entries are unsupported"));
    }
    Ok(())
}

fn parse_zip_cd_entry(cd: &[u8], pos: usize, archive_name: &str) -> Result<(ZipCdEntry, usize)> {
    if pos + 46 > cd.len() || &cd[pos..pos + 4] != b"PK\x01\x02" {
        return Err(format!(
            "{archive_name}: malformed ZIP central directory entry"
        ));
    }
    let flag = u16_le(cd, pos + 8);
    let name_len = u16_le(cd, pos + 28) as usize;
    let extra_len = u16_le(cd, pos + 30) as usize;
    let end = pos + 46 + name_len + extra_len + u16_le(cd, pos + 32) as usize;
    if end > cd.len() {
        return Err(format!(
            "{archive_name}: truncated ZIP central directory entry"
        ));
    }
    let name_raw = &cd[pos + 46..pos + 46 + name_len];
    let name = if flag & 0x800 != 0 {
        py::decode_utf8(name_raw).map_err(|e| e.to_string())?
    } else {
        String::from_utf8_lossy(name_raw).into_owned()
    };
    let comp_size = u32_le(cd, pos + 20);
    let uncomp_size = u32_le(cd, pos + 24);
    let local_off = u32_le(cd, pos + 42);
    let extra = &cd[pos + 46 + name_len..pos + 46 + name_len + extra_len];
    check_zip_cd_flags(
        extra,
        comp_size,
        uncomp_size,
        local_off,
        flag,
        &name,
        archive_name,
    )?;
    let entry = ZipCdEntry {
        name,
        method: u16_le(cd, pos + 10),
        crc: u32_le(cd, pos + 16),
        comp_size,
        uncomp_size,
        ext_attr: u32_le(cd, pos + 38),
        local_off,
    };
    Ok((entry, end))
}

fn read_zip_central_dir(archive: &Path, archive_name: &str) -> Result<(File, u16, Vec<u8>, u64)> {
    let (total, cd_size, cd_offset, file_size) = zip_directory_is_small(archive, archive_name)?;
    if u64::from(cd_offset) + u64::from(cd_size) > file_size {
        return Err(format!(
            "{archive_name}: malformed ZIP central directory offset"
        ));
    }
    let mut file = File::open(archive)
        .map_err(|e| py::oserror_str(&e, &archive.to_string_lossy(), OsStyle::Native))?;
    file.seek(SeekFrom::Start(u64::from(cd_offset)))
        .map_err(|e| e.to_string())?;
    let mut cd = vec![0u8; cd_size as usize];
    file.read_exact(&mut cd)
        .map_err(|_| format!("{archive_name}: truncated ZIP central directory"))?;
    Ok((file, total, cd, file_size))
}

fn read_zip_members(archive: &Path) -> Result<BTreeMap<String, (Vec<u8>, u32)>> {
    let archive_name = archive_filename(archive);
    let (mut file, total, cd, file_size) = read_zip_central_dir(archive, &archive_name)?;
    let mut pos = 0usize;
    let mut members = BTreeMap::new();
    let mut remaining_decompressed = MAX_DECOMPRESSED_BYTES;
    for _ in 0..total {
        let (entry, next_pos) = parse_zip_cd_entry(&cd, pos, &archive_name)?;
        pos = next_pos;
        let mode = (entry.ext_attr >> 16) & 0o177_777;
        let kind = mode & 0o170_000;
        if entry.name.ends_with('/') || (kind != 0 && kind != 0o100_000) {
            return Err(format!("{} is not a regular file", entry.name));
        }
        admit(&members, &entry.name, entry.uncomp_size as usize)?;
        let data = extract_zip_entry(
            &mut file,
            &archive_name,
            &entry,
            file_size,
            remaining_decompressed,
        )?;
        remaining_decompressed =
            remaining_decompressed
                .checked_sub(data.len())
                .ok_or_else(|| {
                    format!("{archive_name}: the archive decompresses beyond the size limit")
                })?;
        members.insert(entry.name, (data, mode & 0o7777));
    }
    if pos != cd.len() {
        return Err(format!(
            "{archive_name}: trailing data in ZIP central directory"
        ));
    }
    Ok(members)
}

fn has_zip64_extra(mut extra: &[u8]) -> bool {
    while extra.len() >= 4 {
        let tag = u16_le(extra, 0);
        let len = u16_le(extra, 2) as usize;
        if tag == 0x0001 {
            return true;
        }
        if 4 + len > extra.len() {
            break;
        }
        extra = &extra[4 + len..];
    }
    false
}

fn decode_zip_payload(raw: Vec<u8>, entry: &ZipCdEntry, budget: usize) -> Result<Vec<u8>> {
    match entry.method {
        0 => {
            if raw.len() != entry.uncomp_size as usize || raw.len() > budget {
                return Err(format!("{}: uncompressed size mismatch", entry.name));
            }
            Ok(raw)
        }
        8 => {
            let cap = (entry.uncomp_size as usize).min(budget);
            let mut decoder = DeflateDecoder::new(&raw[..]);
            let mut out = Vec::new();
            (&mut decoder)
                .take((cap + 1) as u64)
                .read_to_end(&mut out)
                .map_err(|e| format!("{}: deflate decode failed: {e}", entry.name))?;
            if out.len() > cap || out.len() != entry.uncomp_size as usize {
                return Err(format!("{}: uncompressed size mismatch", entry.name));
            }
            Ok(out)
        }
        other => Err(format!(
            "{}: unsupported ZIP compression method {other}",
            entry.name
        )),
    }
}

fn read_zip_payload_offset(
    file: &mut File,
    archive_name: &str,
    name: &str,
    local_off: u64,
    file_size: u64,
) -> Result<u64> {
    if local_off.checked_add(30).is_none_or(|end| end > file_size) {
        return Err(format!(
            "{archive_name}: truncated ZIP local header for {name}"
        ));
    }
    file.seek(SeekFrom::Start(local_off))
        .map_err(|e| e.to_string())?;
    let mut local = [0u8; 30];
    file.read_exact(&mut local)
        .map_err(|_| format!("{archive_name}: truncated ZIP local header for {name}"))?;
    if &local[0..4] != b"PK\x03\x04" {
        return Err(format!(
            "Bad magic number for file header in {archive_name}"
        ));
    }
    let skip = u64::from(u16_le(&local, 26)) + u64::from(u16_le(&local, 28));
    Ok(local_off + 30 + skip)
}

fn extract_zip_entry(
    file: &mut File,
    archive_name: &str,
    entry: &ZipCdEntry,
    file_size: u64,
    budget: usize,
) -> Result<Vec<u8>> {
    let payload_off = read_zip_payload_offset(
        file,
        archive_name,
        &entry.name,
        u64::from(entry.local_off),
        file_size,
    )?;
    let comp = u64::from(entry.comp_size);
    if comp > MAX_ARCHIVE_BYTES as u64
        || payload_off
            .checked_add(comp)
            .is_none_or(|end| end > file_size)
    {
        return Err(format!(
            "{archive_name}: truncated ZIP payload for {}",
            entry.name
        ));
    }
    if entry.method == 0
        && (entry.comp_size != entry.uncomp_size || (entry.uncomp_size as usize) > budget)
    {
        return Err(format!("{}: uncompressed size mismatch", entry.name));
    }
    file.seek(SeekFrom::Start(payload_off))
        .map_err(|e| e.to_string())?;
    let mut raw = vec![0u8; entry.comp_size as usize];
    file.read_exact(&mut raw)
        .map_err(|_| format!("{archive_name}: truncated ZIP payload for {}", entry.name))?;
    let data = decode_zip_payload(raw, entry, budget)?;
    let mut hasher = Hasher::new();
    hasher.update(&data);
    if hasher.finalize() != entry.crc {
        return Err(format!("Bad CRC-32 for file '{}'", entry.name));
    }
    Ok(data)
}

struct BoundedReader<R> {
    inner: R,
    left: usize,
}

impl<R: Read> Read for BoundedReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let want = buf.len().min(self.left + 1);
        let n = self.inner.read(&mut buf[..want])?;
        if n > self.left {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "the archive decompresses beyond the size limit",
            ));
        }
        self.left -= n;
        Ok(n)
    }
}

fn read_tar_exact(reader: &mut dyn Read, buf: &mut [u8]) -> Result<bool> {
    let mut filled = 0;
    while filled < buf.len() {
        match reader.read(&mut buf[filled..]) {
            Ok(0) => {
                if filled == 0 {
                    return Ok(false);
                }
                return Err("truncated tar archive".into());
            }
            Ok(n) => filled += n,
            Err(e) => return Err(e.to_string()),
        }
    }
    Ok(true)
}

fn parse_tar_octal(field: &[u8]) -> Result<u64> {
    let trimmed = field
        .iter()
        .copied()
        .take_while(|&b| b != 0)
        .collect::<Vec<_>>();
    let text = std::str::from_utf8(&trimmed)
        .map_err(|_| "invalid tar octal header".to_owned())?
        .trim();
    if text.is_empty() {
        return Ok(0);
    }
    u64::from_str_radix(text, 8).map_err(|_| format!("invalid tar octal header: {text:?}"))
}

fn tar_header_str(field: &[u8]) -> Result<String> {
    let end = field.iter().position(|&b| b == 0).unwrap_or(field.len());
    py::decode_utf8(&field[..end]).map_err(|e| e.to_string())
}

fn valid_tar_checksum(block: &[u8; 512]) -> Result<bool> {
    let expected = parse_tar_octal(&block[148..156])?;
    let mut unsigned: i64 = 0;
    let mut signed: i64 = 0;
    for (idx, &b) in block.iter().enumerate() {
        if (148..156).contains(&idx) {
            unsigned += 0x20;
            signed += 0x20;
        } else {
            unsigned += i64::from(b);
            signed += i64::from(b as i8);
        }
    }
    Ok(expected as i64 == unsigned || expected as i64 == signed)
}

fn skip_pax_header(stream: &mut dyn Read, size: usize) -> Result<()> {
    let mut left = size
        .checked_add(511)
        .map(|n| (n / 512) * 512)
        .ok_or_else(|| "truncated PAX header in tar archive".to_owned())?;
    let mut trash = [0u8; 4096];
    while left > 0 {
        let chunk = left.min(trash.len());
        if !read_tar_exact(stream, &mut trash[..chunk])? {
            return Err("truncated PAX header in tar archive".into());
        }
        left -= chunk;
    }
    Ok(())
}

fn read_tar_member_data(stream: &mut dyn Read, name: &str, size: usize) -> Result<Vec<u8>> {
    let mut data = Vec::with_capacity(size.min(64 * 1024));
    let read = stream
        .take(size as u64)
        .read_to_end(&mut data)
        .map_err(|e| e.to_string())?;
    if read != size {
        return Err(format!("truncated tar member {name}"));
    }
    let pad = (512 - (size % 512)) % 512;
    if pad > 0 {
        let mut zeros = [0u8; 512];
        if !read_tar_exact(stream, &mut zeros[..pad])? {
            return Err(format!("truncated tar padding for {name}"));
        }
    }
    Ok(data)
}

fn parse_tar_entry_meta(block: &[u8; 512]) -> Result<(u8, String, usize, u32)> {
    let typeflag = block[156];
    let base = tar_header_str(&block[0..100])?;
    let prefix = tar_header_str(&block[345..500])?;
    let raw_name = if prefix.is_empty() {
        base
    } else {
        format!("{prefix}/{base}")
    };
    let name = if typeflag == b'5' {
        raw_name.trim_end_matches('/').to_owned()
    } else {
        raw_name
    };
    let size_u64 = parse_tar_octal(&block[124..136])?;
    let size = usize::try_from(size_u64)
        .map_err(|_| format!("{name} exceeds the archive's member count or size limit"))?;
    let mode = (parse_tar_octal(&block[100..108])? as u32) & 0o7777;
    Ok((typeflag, name, size, mode))
}

fn read_tar_gz_members(archive: &Path) -> Result<BTreeMap<String, (Vec<u8>, u32)>> {
    let file = File::open(archive)
        .map_err(|e| py::oserror_str(&e, &archive.to_string_lossy(), OsStyle::Native))?;
    let gz = MultiGzDecoder::new(BufReader::new(file));
    let mut stream = BufReader::new(BoundedReader {
        inner: gz,
        left: MAX_DECOMPRESSED_BYTES,
    });
    let mut members = BTreeMap::new();
    let mut saw_pax = false;
    let mut block = [0u8; 512];
    while read_tar_exact(&mut stream, &mut block)? {
        if block.iter().all(|&b| b == 0) {
            let mut sink = [0u8; 4096];
            while stream.read(&mut sink).map_err(|e| e.to_string())? > 0 {}
            break;
        }
        if !valid_tar_checksum(&block)? {
            return Err("invalid tar header checksum".into());
        }
        let (typeflag, name, size, mode) = parse_tar_entry_meta(&block)?;
        if matches!(typeflag, b'x' | b'X' | b'g') {
            skip_pax_header(&mut stream, size)?;
            saw_pax = true;
            continue;
        }
        if saw_pax {
            return Err(format!(
                "{name} uses PAX headers; release archives are USTAR"
            ));
        }
        if typeflag != b'0' && typeflag != 0 {
            return Err(format!("{name} is not a regular file"));
        }
        admit(&members, &name, size)?;
        let data = read_tar_member_data(&mut stream, &name, size)?;
        members.insert(name, (data, mode));
    }
    Ok(members)
}

fn archive_members(archive: &Path, target: Target) -> Result<BTreeMap<String, (Vec<u8>, u32)>> {
    let archive_name = archive_filename(archive);
    let size = std::fs::metadata(archive)
        .map_err(|e| py::oserror_str(&e, &archive.to_string_lossy(), OsStyle::Native))?
        .len();
    if size > MAX_ARCHIVE_BYTES as u64 {
        return Err(format!("{archive_name} exceeds the archive size limit"));
    }
    if target.suffix == ".zip" {
        read_zip_members(archive)
    } else {
        read_tar_gz_members(archive)
    }
}

fn create_dir_exclusive(dir: &Path) -> Result<()> {
    if dir.symlink_metadata().is_ok() {
        let e = io::Error::from(io::ErrorKind::AlreadyExists);
        return Err(py::oserror_str(&e, &dir.to_string_lossy(), OsStyle::Native));
    }
    if let Some(parent) = dir.parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent)
            .map_err(|e| py::oserror_str(&e, &parent.to_string_lossy(), OsStyle::Native))?;
    }
    std::fs::create_dir(dir)
        .map_err(|e| py::oserror_str(&e, &dir.to_string_lossy(), OsStyle::Native))
}

#[cfg(unix)]
fn set_file_mode(path: &Path, mode: u32) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode))
        .map_err(|e| py::oserror_str(&e, &path.to_string_lossy(), OsStyle::Native))
}

#[cfg(not(unix))]
fn set_file_mode(_path: &Path, _mode: u32) -> Result<()> {
    Ok(())
}

fn check_verify_members(
    members: &BTreeMap<String, (Vec<u8>, u32)>,
    exec_names: &[String],
    data: &[String],
    doc_set: &BTreeSet<&str>,
    expected: &[String],
    target: Target,
) -> Result<()> {
    for name in members.keys() {
        let single = !name.is_empty()
            && !name.contains('/')
            && !name.contains('\\')
            && name != "."
            && name != "..";
        if !single {
            return Err(format!("unsafe member name {}", py::repr_str(name)));
        }
    }
    for (kind, names) in [("executable", exec_names), ("data file", data)] {
        for name in names {
            if !members.contains_key(name.as_str()) {
                return Err(format!("the {kind} {name} is missing"));
            }
        }
    }
    let known_docs: HashSet<&str> = DOCUMENTS.into_iter().collect();
    if doc_set.iter().any(|d| !known_docs.contains(d))
        || REQUIRED_DOCUMENTS.iter().any(|r| !doc_set.contains(r))
    {
        let actual_refs: Vec<&str> = members.keys().map(String::as_str).collect();
        let expected_refs: Vec<&str> = expected.iter().map(String::as_str).collect();
        return Err(format!(
            "members are {}, expected {}",
            repr_list(actual_refs),
            repr_list(expected_refs)
        ));
    }
    for name in exec_names {
        if target.executable_suffix.is_empty() && members[name].1 & 0o100 == 0 {
            return Err(format!("{name} is not executable"));
        }
    }
    Ok(())
}

fn extract_members(
    dir: &Path,
    expected: &[String],
    members: &BTreeMap<String, (Vec<u8>, u32)>,
) -> Result<()> {
    create_dir_exclusive(dir)?;
    for name in expected {
        let dest = dir.join(name);
        write_bytes(&dest, &members[name].0)?;
        let mode = if members[name].1 == 0 {
            0o644
        } else {
            members[name].1
        };
        set_file_mode(&dest, mode)?;
    }
    Ok(())
}

pub fn verify(
    archive: &Path,
    binary: &str,
    triple: &str,
    extract_dir: Option<&Path>,
    data_files: &str,
) -> Result<Vec<String>> {
    let target = target_for(triple)?;
    let members = archive_members(archive, target)?;
    let exec_names: Vec<String> = binaries(binary)?
        .into_iter()
        .map(|b| format!("{b}{}", target.executable_suffix))
        .collect();
    let exec_set: HashSet<&str> = exec_names.iter().map(String::as_str).collect();
    let data: Vec<String> = data_names(data_files)?
        .into_iter()
        .map(|(n, _)| n)
        .collect();
    let data_set: HashSet<&str> = data.iter().map(String::as_str).collect();
    let doc_set: BTreeSet<&str> = members
        .keys()
        .map(String::as_str)
        .filter(|n| !exec_set.contains(n) && !data_set.contains(n))
        .collect();
    let documents: Vec<&str> = doc_set.iter().copied().collect();
    let data_refs: Vec<&str> = data.iter().map(String::as_str).collect();
    let expected = member_names(binary, triple, &documents, &data_refs)?;
    check_verify_members(&members, &exec_names, &data, &doc_set, &expected, target)?;
    if let Some(dir) = extract_dir {
        extract_members(dir, &expected, &members)?;
    }
    Ok(expected)
}

fn wait_smoke_child(
    child: &mut std::process::Child,
    executable: &Path,
    timeout: Duration,
) -> Result<std::process::ExitStatus> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(status) => return Ok(status),
            None if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "Command '{}' timed out after {} seconds",
                    executable.display(),
                    timeout.as_secs()
                ));
            }
            None => std::thread::sleep(Duration::from_millis(10)),
        }
    }
}

pub fn smoke_with_timeout(executable: &Path, version: &str, timeout: Duration) -> Result<String> {
    let mut child = Command::new(executable)
        .arg("--version")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| py::oserror_str(&e, &executable.to_string_lossy(), OsStyle::Native))?;
    let mut stdout_pipe = child.stdout.take().expect("piped stdout");
    let mut stderr_pipe = child.stderr.take().expect("piped stderr");
    let out_handle = std::thread::spawn(move || -> io::Result<Vec<u8>> {
        let mut buf = Vec::new();
        stdout_pipe.read_to_end(&mut buf)?;
        Ok(buf)
    });
    let err_handle = std::thread::spawn(move || -> io::Result<Vec<u8>> {
        let mut buf = Vec::new();
        stderr_pipe.read_to_end(&mut buf)?;
        Ok(buf)
    });
    let status = match wait_smoke_child(&mut child, executable, timeout) {
        Ok(status) => status,
        Err(err) => {
            let _ = out_handle.join();
            let _ = err_handle.join();
            return Err(err);
        }
    };
    let stdout_raw = out_handle.join().unwrap().map_err(|e| e.to_string())?;
    let _ = err_handle.join();
    let stdout = py::decode_utf8(&stdout_raw).map_err(|e| e.to_string())?;
    let first = py::splitlines(&stdout).into_iter().next().unwrap_or("");
    let code = status.code().unwrap_or(1);
    let expected_suffix = format!(" {version}");
    if code != 0 || !first.ends_with(&expected_suffix) {
        let name = archive_filename(executable);
        return Err(format!(
            "{name} --version exited {code} with {}; expected a first line ending in ' {version}'",
            py::repr_str(first)
        ));
    }
    Ok(first.to_owned())
}

pub fn smoke(executable: &Path, version: &str) -> Result<String> {
    smoke_with_timeout(executable, version, Duration::from_secs(60))
}

pub fn needed_libraries(readobj_output: &str) -> Result<Vec<String>> {
    let normalised = readobj_output.replace("\r\n", "\n");
    let Some(caps) = NEEDED_RE.captures(&normalised) else {
        return Err("llvm-readobj printed no NeededLibraries list".into());
    };
    let names = py::splitlines(&caps["names"])
        .into_iter()
        .map(|l| py::strip(l).to_owned())
        .filter(|l| !l.is_empty())
        .collect();
    Ok(names)
}

fn macho_load_commands(executable: &[u8], target: Target) -> Result<(u32, usize)> {
    if executable.get(..4) != Some(MACHO_MAGIC_64) {
        return Err("not a thin 64-bit little-endian Mach-O file".into());
    }
    if executable.len() < 32 {
        return Err(MACHO_MALFORMED.into());
    }
    let cputype = u32_le(executable, 4);
    let filetype = u32_le(executable, 12);
    let count = u32_le(executable, 16);
    let size = u32_le(executable, 20) as usize;
    let (expected, name) = if target.arch == "aarch64" {
        (0x0100_000C, "arm64")
    } else {
        (0x0100_0007, "x86_64")
    };
    if cputype != expected {
        return Err(format!("not an {name} Mach-O executable"));
    }
    if filetype != MACHO_EXECUTE {
        return Err("not a Mach-O executable file type".into());
    }
    let end = 32usize
        .checked_add(size)
        .filter(|&end| end <= executable.len())
        .ok_or_else(|| MACHO_MALFORMED.to_owned())?;
    Ok((count, end))
}

fn macho_path(executable: &[u8], offset: usize, length: usize) -> Result<String> {
    let name_at = if length >= 12 {
        u32_le(executable, offset + 8) as usize
    } else {
        0
    };
    if !(12..length).contains(&name_at) {
        return Err(MACHO_MALFORMED.into());
    }
    let slice = &executable[offset + name_at..offset + length];
    let Some(nul) = slice.iter().position(|&b| b == 0) else {
        return Err(MACHO_MALFORMED.into());
    };
    py::decode_utf8(&slice[..nul]).map_err(|_| MACHO_MALFORMED.to_owned())
}

fn scan_macho_commands(executable: &[u8], count: u32, end: usize) -> Result<Vec<String>> {
    let mut offset = 32usize;
    let mut needed = Vec::new();
    for _ in 0..count {
        if offset + 8 > end {
            return Err(MACHO_MALFORMED.into());
        }
        let command = u32_le(executable, offset);
        let length = u32_le(executable, offset + 4) as usize;
        if length < 8 || offset + length > end {
            return Err(MACHO_MALFORMED.into());
        }
        if command == 0x8000_001C {
            return Err("the executable adds a run-path search path".into());
        }
        if command == 0x27 {
            return Err("the executable sets dyld environment variables".into());
        }
        if matches!(
            command,
            0xC | 0x8000_0018 | 0x8000_001F | 0x20 | 0x8000_0023
        ) {
            needed.push(macho_path(executable, offset, length)?);
        } else if command == MACHO_LOAD_DYLINKER {
            let text = macho_path(executable, offset, length)?;
            if text != "/usr/lib/dyld" {
                return Err(format!(
                    "the executable names a dynamic linker other than /usr/lib/dyld: {text}"
                ));
            }
        }
        offset += length;
    }
    if offset != end {
        return Err(MACHO_MALFORMED.into());
    }
    Ok(needed)
}

pub fn macho_linkage(executable: &[u8], triple: &str) -> Result<Vec<String>> {
    let target = target_for(triple)?;
    if !target.object_format.starts_with("Mach-O") {
        return Err(format!(
            "only a macOS executable's linkage is read from it, not {triple}'s"
        ));
    }
    let (count, end) = macho_load_commands(executable, target)?;
    let needed = scan_macho_commands(executable, count, end)?;
    let foreign: Vec<&str> = needed
        .iter()
        .map(String::as_str)
        .filter(|name| !matches!(MACOS_SYSTEM_LIBRARY_RE.is_match(name), Ok(true)))
        .collect();
    if !foreign.is_empty() {
        return Err(format!(
            "loads libraries macOS does not ship: {}",
            repr_list(foreign)
        ));
    }
    Ok(needed)
}

fn check_windows_dlls(needed: &[String]) -> Result<()> {
    let runtime: Vec<&str> = needed
        .iter()
        .map(String::as_str)
        .filter(|name| WINDOWS_RUNTIME_RE.is_match(name))
        .collect();
    if !runtime.is_empty() {
        return Err(format!("imports C runtime DLLs: {}", repr_list(runtime)));
    }
    let unsafe_dlls: Vec<&str> = needed
        .iter()
        .map(String::as_str)
        .filter(|name| !DLL_NAME_RE.is_match(name))
        .collect();
    if !unsafe_dlls.is_empty() {
        return Err(format!(
            "imports DLLs by path or with an unexpected name: {}",
            repr_list(unsafe_dlls)
        ));
    }
    let allowed: HashSet<&str> = WINDOWS_SYSTEM_DLLS.into_iter().collect();
    let foreign: Vec<&str> = needed
        .iter()
        .map(String::as_str)
        .filter(|name| {
            !API_SET_RE.is_match(name) && !allowed.contains(name.to_lowercase().as_str())
        })
        .collect();
    if !foreign.is_empty() {
        return Err(format!(
            "imports DLLs outside the pinned Windows system set: {}",
            repr_list(foreign)
        ));
    }
    Ok(())
}

pub fn linkage(readobj_output: &str, triple: &str) -> Result<Vec<String>> {
    let target = target_for(triple)?;
    if target.object_format.starts_with("Mach-O") {
        return Err("a macOS executable's linkage is read from its load commands".into());
    }
    if readobj_output.matches("Format: ").count() != 1
        || readobj_output.matches("NeededLibraries [").count() > 1
    {
        return Err("llvm-readobj's output describes more than one object".into());
    }
    let lines: HashSet<&str> = py::splitlines(readobj_output)
        .into_iter()
        .map(py::strip)
        .collect();
    let expected_format = format!("Format: {}", target.object_format);
    let expected_arch = format!("Arch: {}", target.arch);
    if !lines.contains(expected_format.as_str()) || !lines.contains(expected_arch.as_str()) {
        return Err(format!(
            "not an {} {} executable",
            target.arch, target.object_format
        ));
    }
    let needed = needed_libraries(readobj_output)?;
    if target.object_format.starts_with("elf") {
        if !needed.is_empty() || readobj_output.contains("PT_INTERP") {
            let refs: Vec<&str> = needed.iter().map(String::as_str).collect();
            return Err(format!(
                "not a static executable: needs {} or an interpreter",
                repr_list(refs)
            ));
        }
        return Ok(needed);
    }
    check_windows_dlls(&needed)?;
    Ok(needed)
}

fn read_toolchain_channel(directory: &Path) -> Result<Option<String>> {
    let legacy = directory.join("rust-toolchain");
    let current = directory.join("rust-toolchain.toml");
    if legacy.exists() || symlink(&legacy) {
        return Err(format!(
            "{}: use rust-toolchain.toml with an exact channel",
            legacy.display()
        ));
    }
    if symlink(&current) || (current.exists() && !current.is_file()) {
        return Err(format!("{} must be a regular file", current.display()));
    }
    if !current.is_file() {
        return Ok(None);
    }
    let document = read_toml(
        &current,
        &format!("{} is not readable TOML", current.display()),
    )?;
    let channel = document
        .get("toolchain")
        .and_then(toml::Value::as_table)
        .and_then(|t| t.get("channel"))
        .and_then(toml::Value::as_str);
    let Some(channel) = channel.filter(|c| TOOLCHAIN_CHANNEL_RE.is_match(c)) else {
        return Err(format!(
            "{} must pin an exact MAJOR.MINOR.PATCH channel",
            current.display()
        ));
    };
    Ok(Some(channel.to_owned()))
}

pub fn toolchain(source: &Path, root: &Path) -> Result<String> {
    let source = resolve_path(source);
    let root = resolve_path(root);
    if source != root && !is_relative_to(&source, &root) {
        return Err(format!(
            "{} is not inside {}",
            source.display(),
            root.display()
        ));
    }
    let mut cur = Some(source.as_path());
    while let Some(directory) = cur {
        if let Some(channel) = read_toolchain_channel(directory)? {
            return Ok(channel);
        }
        if path_equals(directory, &root) {
            break;
        }
        cur = directory.parent();
    }
    Err(format!(
        "no rust-toolchain.toml between {} and {}",
        source.display(),
        root.display()
    ))
}

fn read_dir_regular_entries(
    dir: &Path,
    expected_names: &[&str],
) -> Result<BTreeMap<String, PathBuf>> {
    let expected: BTreeSet<&str> = expected_names.iter().copied().collect();
    let mut entries = BTreeMap::new();
    let read_dir = std::fs::read_dir(dir)
        .map_err(|e| py::oserror_str(&e, &dir.to_string_lossy(), OsStyle::Native))?;
    for entry_res in read_dir {
        let entry = entry_res.map_err(|e| e.to_string())?;
        let fname = entry.file_name().to_string_lossy().into_owned();
        entries.insert(fname, entry.path());
    }
    let actual: BTreeSet<&str> = entries.keys().map(String::as_str).collect();
    if actual != expected {
        return Err(format!(
            "artefact holds {}, expected {}",
            repr_list(actual.iter().copied()),
            repr_list(expected.iter().copied())
        ));
    }
    for (entry_name, entry_path) in &entries {
        if symlink(entry_path) || !entry_path.is_file() {
            return Err(format!("{entry_name} is not a regular file"));
        }
    }
    Ok(entries)
}

fn read_incoming_digest(digest_file: &Path, archive: &Path, name: &str) -> Result<String> {
    if std::fs::metadata(digest_file)
        .map_err(|e| e.to_string())?
        .len()
        > MAX_DIGEST_BYTES
    {
        return Err(format!(
            "{name}.sha256 is not one sha256sum line for {name}"
        ));
    }
    if std::fs::metadata(archive).map_err(|e| e.to_string())?.len() > MAX_ARCHIVE_BYTES as u64 {
        return Err(format!("{name} exceeds the archive size limit"));
    }
    let line_bytes = read_bytes(digest_file)?;
    let line = String::from_utf8_lossy(&line_bytes);
    let Some(caps) = DIGEST_LINE_RE
        .captures(&line)
        .filter(|c| &c["name"] == name)
    else {
        return Err(format!(
            "{name}.sha256 is not one sha256sum line for {name}"
        ));
    };
    Ok(caps["digest"].to_owned())
}

struct ReceivedSpec<'a> {
    name: &'a str,
    expected_digest: &'a str,
    binary: &'a str,
    triple: &'a str,
    extract_dir: &'a Path,
    data_files: &'a str,
    tagged: &'a BTreeMap<String, PathBuf>,
}

fn verify_admitted_archive(
    archive: &Path,
    admitted: &Path,
    dst: File,
    spec: &ReceivedSpec<'_>,
) -> Result<()> {
    let mut dst = dst;
    let mut src = File::open(archive)
        .map_err(|e| py::oserror_str(&e, &archive.to_string_lossy(), OsStyle::Native))?;
    io::copy(&mut src, &mut dst).map_err(|e| e.to_string())?;
    drop(dst);

    if file_sha256(admitted)? != spec.expected_digest {
        return Err(format!("{} does not match its recorded digest", spec.name));
    }
    let names = verify(
        admitted,
        spec.binary,
        spec.triple,
        Some(spec.extract_dir),
        spec.data_files,
    )?;
    let exec_count = binaries(spec.binary)?.len();
    let shipped: BTreeSet<&str> = names[exec_count..].iter().map(String::as_str).collect();
    let tagged_names: BTreeSet<&str> = spec.tagged.keys().map(String::as_str).collect();
    if shipped != tagged_names {
        return Err(format!(
            "{} ships {}, the tag has {}",
            spec.name,
            repr_list(shipped.iter().copied()),
            repr_list(tagged_names.iter().copied())
        ));
    }
    for document in shipped {
        if read_bytes(&spec.extract_dir.join(document))? != read_bytes(&spec.tagged[document])? {
            return Err(format!(
                "{}'s {document} differs from the tagged {document}",
                spec.name
            ));
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub fn receive(
    incoming: &Path,
    stem: &str,
    version: &str,
    binary: &str,
    triple: &str,
    dist: &Path,
    extract_dir: &Path,
    source: &Path,
    root: &Path,
    data_files: &str,
) -> Result<PathBuf> {
    let name = asset_name(stem, version, triple)?;
    let digest_name = format!("{name}.sha256");
    let entries = read_dir_regular_entries(incoming, &[&name, &digest_name])?;
    let archive = &entries[&name];
    let expected_digest = read_incoming_digest(&entries[&digest_name], archive, &name)?;

    let mut tagged = find_documents(source, root)?;
    for (k, v) in find_data_files(source, data_files)? {
        tagged.insert(k, v);
    }
    std::fs::create_dir_all(dist)
        .map_err(|e| py::oserror_str(&e, &dist.to_string_lossy(), OsStyle::Native))?;
    let admitted = dist.join(&name);
    let dst = create_exclusive(&admitted)?;
    let spec = ReceivedSpec {
        name: &name,
        expected_digest: &expected_digest,
        binary,
        triple,
        extract_dir,
        data_files,
        tagged: &tagged,
    };
    if let Err(err) = verify_admitted_archive(archive, &admitted, dst, &spec) {
        let _ = std::fs::remove_file(&admitted);
        return Err(err);
    }
    Ok(admitted)
}

pub fn sbom_lists(sbom: &Path, package_name: &str) -> Result<bool> {
    let raw = read_bytes(sbom)?;
    let document = pyjson::loads_bytes(&raw).map_err(|e| e.0)?;
    let Some(packages) = document.get("packages").and_then(Json::as_array) else {
        return Ok(false);
    };
    Ok(packages
        .iter()
        .any(|item| item.get("name").and_then(Json::as_str) == Some(package_name)))
}

pub fn handoff_names(stem: &str, version: &str) -> Result<Vec<(String, String)>> {
    let stem = checked(&STEM_RE, stem, "artifact-stem")?;
    let version = checked(&VERSION_RE, version, "version")?;
    let base = format!("{stem}-{version}");
    Ok(handoff_spec()
        .into_iter()
        .map(|(role, suffix)| (role, format!("{base}{suffix}")))
        .collect())
}

fn file_sha256(path: &Path) -> Result<String> {
    use sha2::{Digest, Sha256};
    let mut file = File::open(path)
        .map_err(|e| py::oserror_str(&e, &path.to_string_lossy(), OsStyle::Native))?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

pub fn digests(dist: &Path, stem: &str, version: &str) -> Result<Vec<String>> {
    let mut out = Vec::new();
    for (role, name) in handoff_names(stem, version)? {
        let hex = file_sha256(&dist.join(&name))?;
        checked(&SHA256_RE, &hex, &format!("{name} digest"))?;
        out.push(format!("{role}={hex}"));
    }
    Ok(out)
}

fn parse_expected_digests(
    digest_outputs: &[String],
    names: &[(String, String)],
) -> Result<HashMap<String, String>> {
    let mut expected = HashMap::new();
    for item in digest_outputs {
        let (role, digest) = item.split_once('=').unwrap_or((item.as_str(), ""));
        if expected.contains_key(role) {
            return Err(format!("the {role} digest output is repeated"));
        }
        checked(&SHA256_RE, digest, &format!("{role} digest"))?;
        expected.insert(role.to_owned(), digest.to_owned());
    }
    let expected_keys: BTreeSet<&str> = expected.keys().map(String::as_str).collect();
    let required_roles: BTreeSet<&str> = names.iter().map(|(r, _)| r.as_str()).collect();
    if expected_keys != required_roles {
        return Err(format!(
            "digest outputs are {}, expected {}",
            repr_list(expected_keys.iter().copied()),
            repr_list(required_roles.iter().copied())
        ));
    }
    Ok(expected)
}

pub fn accept(
    incoming: &Path,
    dist: &Path,
    stem: &str,
    version: &str,
    digest_outputs: &[String],
) -> Result<Vec<PathBuf>> {
    let names = handoff_names(stem, version)?;
    let expected = parse_expected_digests(digest_outputs, &names)?;
    let expected_names: Vec<&str> = names.iter().map(|(_, n)| n.as_str()).collect();
    let entries = read_dir_regular_entries(incoming, &expected_names)?;

    let mut kept = Vec::with_capacity(names.len());
    for (role, name) in &names {
        let own = dist.join(name);
        let received = &entries[name];
        let want = &expected[role];
        let check_path = if role.ends_with("-sbom") {
            let mut src = File::open(received)
                .map_err(|e| py::oserror_str(&e, &received.to_string_lossy(), OsStyle::Native))?;
            let mut dst = create_exclusive(&own)?;
            io::copy(&mut src, &mut dst).map_err(|e| e.to_string())?;
            &own
        } else {
            if file_sha256(&own)? != *want {
                return Err(format!(
                    "{name} from the admit job differs from this job's own"
                ));
            }
            received
        };
        if file_sha256(check_path)? != *want {
            return Err(format!("{name} does not match the admit job's digest"));
        }
        kept.push(own);
    }
    Ok(kept)
}

fn python_tempdir() -> PathBuf {
    for var in ["TMPDIR", "TEMP", "TMP"] {
        if let Some(val) = std::env::var_os(var)
            && !val.is_empty()
        {
            let p = PathBuf::from(val);
            if p.is_dir() {
                return p;
            }
        }
    }
    std::env::temp_dir()
}

pub fn trusted_bases() -> Vec<PathBuf> {
    let mut bases = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        bases.push(resolve_path(&cwd));
    }
    bases.push(resolve_path(&python_tempdir()));
    for var in RUNNER_DIRECTORIES {
        if let Some(val) = std::env::var_os(var)
            && !val.is_empty()
        {
            bases.push(resolve_path(&PathBuf::from(val)));
        }
    }
    bases
}

pub fn confined_path(raw: &str) -> Result<PathBuf> {
    let py_path = PyPath::new(raw);
    let fs_path = py_path.fs();
    if symlink(&fs_path) {
        return Err(format!("{py_path} must not be a symbolic link"));
    }
    let resolved = resolve_path(&fs_path);
    for base in trusted_bases() {
        if is_relative_to(&resolved, &base) {
            return Ok(resolved);
        }
    }
    Err(format!(
        "{py_path} is outside the working, workspace and temporary directories"
    ))
}
