// Copyright 2026 Ryan Duguid
// SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

//! `release-policy init`: write the release caller workflow a repository
//! needs, offline, pinned to a released policy commit. It refuses inputs
//! the release jobs would refuse, with the Rust ports of their parsers.

use crate::{Result, common::*, py, release, required_checks};
use std::path::Path;

/// The release whose workflows a new caller runs, and the commit its tag
/// marks. Move both, with the goldens in tests/data/init, after a release.
pub const POLICY_RELEASE: &str = "v0.4.0";
pub const POLICY_COMMIT: &str = "ddb7c18953f5abef38ba164a0ae48dc7c6a0e1f3";
const CALLER: &str = ".github/workflows/release.yml";
const STEM: &str = "[a-z0-9]+(?:-[a-z0-9]+)*";
const BINARY: &str = "[a-z0-9]+(?:[-_][a-z0-9]+)*";
const PATH: &str = "[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*";

pub struct Options {
    pub family: Option<String>,
    pub required_checks: Vec<String>,
    pub artifact_stem: Option<String>,
    pub version_file: Option<String>,
    pub force: bool,
}

struct Family {
    name: &'static str,
    workflow: &'static str,
    kind: &'static str,
    guide: &'static str,
    /// What the owner still has to do for this family; `{docs}` is the guides' URL.
    left: &'static [&'static str],
}

const FAMILIES: [Family; 3] = [
    Family {
        name: "python",
        workflow: "release-python.yml",
        kind: "a Python package release",
        guide: "python-consumers.md",
        left: &[
            "Commit uv.lock, with a dev extra providing pytest, build and each build requirement.",
            "PyPI is optional: register this repository as a Trusted Publisher on PyPI and add the publish job from {docs}/pypi-publishing.md",
        ],
    },
    Family {
        name: "binary",
        workflow: "release-binary.yml",
        kind: "a Rust binary release",
        guide: "binary-consumers.md",
        left: &[
            "Set publish = false and the VERSION file's version in Cargo.toml; commit Cargo.lock, LICENSE, README.md and a rust-toolchain.toml pinning an exact version.",
        ],
    },
    Family {
        name: "archive",
        workflow: "release-archive.yml",
        kind: "a source archive release",
        guide: "archive-consumers.md",
        left: &[
            "Keep VERSION to one MAJOR.MINOR.PATCH line and tests runnable as 'python -B -m unittest discover -s tests -v'.",
        ],
    },
];

/// Write the caller and return the report of what is left for the owner.
pub fn init(root: &Path, options: &Options) -> Result<String> {
    let args = ["rev-parse", "--is-inside-work-tree", "--show-prefix"];
    let out = run(root, "git", &args)?;
    if !out.status.success() {
        return Err("run init inside a Git work tree".into());
    }
    // Inside .git or a bare repository the prefix is empty too, but Git
    // reports that it is not in a work tree.
    if out.stdout.trim_ascii_end() != b"true" {
        return Err("run init at the repository's top level".into());
    }
    let name = match &options.family {
        Some(name) => name.as_str(),
        None => detect(root)?,
    };
    let family = FAMILIES
        .iter()
        .find(|f| f.name == name)
        .ok_or_else(|| format!("unknown family: {}", py::repr_str(name)))?;
    if options.version_file.is_some() && family.name != "python" {
        return Err("--version-file applies only to the python family".into());
    }
    if options.artifact_stem.is_some() && family.name == "python" {
        return Err("--artifact-stem does not apply to the python family, which takes its name from pyproject.toml".into());
    }
    let checks = checked_required_checks(root, &options.required_checks)?;
    let stem = options.artifact_stem.as_deref();
    let inputs = match family.name {
        "python" => python_inputs(root, options.version_file.as_deref())?,
        "binary" => binary_inputs(root, stem)?,
        _ => vec![("artifact-stem", archive_stem(root, stem)?)],
    };
    write_caller(root, &caller(family, &inputs, &checks), options.force)?;
    Ok(report(family))
}

/// The parsed TOML file at the root, or `None` when there is none.
fn manifest(root: &Path, file: &str) -> Result<Option<toml::Table>> {
    let path = root.join(file);
    if !path.is_file() {
        return Ok(None);
    }
    read_utf8(&path, file)?
        .parse::<toml::Table>()
        .map(Some)
        .map_err(|_| format!("{file} is not valid TOML"))
}

fn has_table(root: &Path, file: &str, table: &str) -> Result<bool> {
    Ok(manifest(root, file)?.is_some_and(|doc| doc.get(table).is_some_and(toml::Value::is_table)))
}

fn detect(root: &Path) -> Result<&'static str> {
    let python = has_table(root, "pyproject.toml", "project")?;
    let binary = has_table(root, "Cargo.toml", "package")?;
    match (python, binary) {
        (true, true) => Err(
            "found both pyproject.toml [project] and Cargo.toml [package]; pass --family".into(),
        ),
        (true, false) => Ok("python"),
        (false, true) => Ok("binary"),
        _ if root.join("VERSION").is_file() => Ok("archive"),
        _ => Err("found no pyproject.toml with [project], Cargo.toml with [package] or VERSION file; pass --family".into()),
    }
}

/// The release gate's parser, then checks it cannot make: the label must be
/// writable on one YAML line and reach the gate as written, and its workflow
/// must be in this repository and not be the caller init writes.
fn checked_required_checks(root: &Path, given: &[String]) -> Result<Vec<String>> {
    required_checks::parse_required_checks(&given.join("\n"))?
        .iter()
        .map(|check| {
            let label = check.label();
            // YAML cannot hold U+FFFE, U+FFFF or most control characters.
            if label
                .chars()
                .any(|c| c.is_control() || matches!(c, '\u{fffe}' | '\u{ffff}'))
            {
                return Err(format!(
                    "required check {} holds a character a workflow cannot hold",
                    py::repr_str(&label)
                ));
            }
            if label.contains("${{") {
                return Err(format!(
                    "required check {} holds '${{{{', which GitHub would evaluate; name the job as GitHub shows it",
                    py::repr_str(&label)
                ));
            }
            if names_caller(root, &check.workflow)? {
                return Err(format!(
                    "required check {} names the release workflow init writes",
                    py::repr_str(&label)
                ));
            }
            if !root.join(&check.workflow).is_file() {
                return Err(format!(
                    "required check {} names a workflow file that is not here",
                    py::repr_str(&label)
                ));
            }
            Ok(label)
        })
        .collect()
}

/// Whether `relative` is the existing caller under any name the file system
/// accepts for it, such as another letter case on a case-insensitive file
/// system or a hard link. Unix compares device and inode, as realpath keeps
/// the given case in a casefolded Linux directory.
#[cfg(unix)]
fn names_caller(root: &Path, relative: &str) -> Result<bool> {
    use std::os::unix::fs::MetadataExt;
    Ok(same_file(root, relative, |path| {
        std::fs::metadata(path).map(|m| (m.dev(), m.ino()))
    }))
}

/// Windows compares the canonical path, which is the on-disk name, then the
/// file IDs, which also find a hard link. Where both names exist but either
/// cannot be identified, init refuses, as it could not rule out the caller.
#[cfg(windows)]
fn names_caller(root: &Path, relative: &str) -> Result<bool> {
    if same_file(root, relative, std::fs::canonicalize::<std::path::PathBuf>) {
        return Ok(true);
    }
    let caller = open_for_id(&root.join(CALLER));
    let path = open_for_id(&root.join(relative));
    let missing = |file: &std::io::Result<std::fs::File>| {
        file.as_ref()
            .is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound)
    };
    if missing(&caller) || missing(&path) {
        return Ok(false);
    }
    let unknown = |e: std::io::Error| {
        format!(
            "cannot tell whether {} is the release workflow init writes: {e}",
            py::repr_str(relative)
        )
    };
    same_id(
        [&caller.map_err(unknown)?, &path.map_err(unknown)?],
        file_id,
        file_index,
    )
    .map_err(unknown)
}

fn same_file<T: PartialEq>(
    root: &Path,
    relative: &str,
    id: impl Fn(std::path::PathBuf) -> std::io::Result<T>,
) -> bool {
    id(root.join(CALLER))
        .is_ok_and(|caller| id(root.join(relative)).is_ok_and(|path| path == caller))
}

/// A handle that reads only attributes, as `std::fs::metadata` opens one, so
/// a file whose data the user may not read, or a directory, still opens.
#[cfg(windows)]
fn open_for_id(path: &Path) -> std::io::Result<std::fs::File> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::FILE_FLAG_BACKUP_SEMANTICS;
    std::fs::OpenOptions::new()
        .access_mode(0)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS)
        .open(path)
}

/// Whether 2 open files are one, by volume and file ID: the 128-bit ID where
/// both give it, as ReFS can repeat the 64-bit index, else the 64-bit index,
/// for a file system without the 128-bit ID. Both use the same kind, so a
/// failure on one file alone cannot make the pair look different.
#[cfg(windows)]
fn same_id<W: PartialEq, N: PartialEq>(
    [one, other]: [&std::fs::File; 2],
    wide: impl Fn(&std::fs::File) -> std::io::Result<W>,
    narrow: impl Fn(&std::fs::File) -> std::io::Result<N>,
) -> std::io::Result<bool> {
    if let (Ok(one), Ok(other)) = (wide(one), wide(other)) {
        return Ok(one == other);
    }
    Ok(narrow(one)? == narrow(other)?)
}

/// The volume and 128-bit file ID.
#[cfg(windows)]
fn file_id(file: &std::fs::File) -> std::io::Result<(u64, [u8; 16])> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ID_INFO, FileIdInfo, GetFileInformationByHandleEx,
    };
    let mut info = FILE_ID_INFO::default();
    // SAFETY: the handle stays open for the call, and the buffer is a
    // FILE_ID_INFO of the size passed.
    // nosemgrep: rust.lang.security.unsafe-usage.unsafe-usage
    let read = unsafe {
        GetFileInformationByHandleEx(
            file.as_raw_handle(),
            FileIdInfo,
            (&raw mut info).cast(),
            size_of::<FILE_ID_INFO>() as u32,
        )
    };
    (read != 0)
        .then_some((info.VolumeSerialNumber, info.FileId.Identifier))
        .ok_or_else(std::io::Error::last_os_error)
}

/// The volume and 64-bit file index.
#[cfg(windows)]
fn file_index(file: &std::fs::File) -> std::io::Result<(u32, u32, u32)> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, GetFileInformationByHandle,
    };
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    // SAFETY: the handle stays open for the call, and the buffer is a
    // BY_HANDLE_FILE_INFORMATION.
    // nosemgrep: rust.lang.security.unsafe-usage.unsafe-usage
    let read = unsafe { GetFileInformationByHandle(file.as_raw_handle(), &raw mut info) };
    (read != 0)
        .then_some((
            info.dwVolumeSerialNumber,
            info.nFileIndexHigh,
            info.nFileIndexLow,
        ))
        .ok_or_else(std::io::Error::last_os_error)
}

type Inputs = Vec<(&'static str, String)>;

fn python_inputs(root: &Path, version_file: Option<&str>) -> Result<Inputs> {
    let Some(file) = version_file else {
        release::derive_metadata(root, "pyproject", "pyproject.toml").map_err(|e| {
            if e.starts_with("pyproject parser requires") {
                format!("{e}; for a dynamic version pass --version-file")
            } else {
                e
            }
        })?;
        return Ok(Vec::new());
    };
    if !full(PATH, file) {
        return Err(format!(
            "version-file must be a relative path of letters, digits, '.', '_', '-' and '/': {}",
            py::repr_str(file)
        ));
    }
    if names_caller(root, file)? {
        return Err(format!(
            "version-file {} names the release workflow init writes",
            py::repr_str(file)
        ));
    }
    release::derive_metadata(root, "python-literal", file)?;
    Ok(vec![
        ("version-parser", "python-literal".into()),
        ("version-file", file.into()),
    ])
}

/// The stem defaults to the package name with `-` for `_`; a package name
/// that differs from the stem is passed as the binary.
fn binary_inputs(root: &Path, stem: Option<&str>) -> Result<Inputs> {
    let cargo = manifest(root, "Cargo.toml")?.ok_or("Cargo.toml is missing")?;
    let Some(name) = cargo
        .get("package")
        .and_then(|package| package.get("name"))
        .and_then(toml::Value::as_str)
    else {
        return Err("Cargo.toml must state a literal [package] name; workspace inheritance is not supported".into());
    };
    let stem = checked_stem(stem.map_or_else(|| name.replace('_', "-"), str::to_owned))?;
    if stem == name {
        return Ok(vec![("artifact-stem", stem)]);
    }
    if !full(BINARY, name) {
        return Err(format!(
            "Cargo.toml [package] name is not a binary name the release accepts: {}",
            py::repr_str(name)
        ));
    }
    Ok(vec![("artifact-stem", stem), ("binary", name.into())])
}

/// The stem defaults to the repository directory's name.
fn archive_stem(root: &Path, stem: Option<&str>) -> Result<String> {
    checked_stem(match stem {
        Some(stem) => stem.to_owned(),
        None => absolute(root)?
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default(),
    })
}

fn checked_stem(stem: String) -> Result<String> {
    if !full(STEM, &stem) {
        return Err(format!(
            "artifact-stem must be lower-case and hyphenated: {}",
            py::repr_str(&stem)
        ));
    }
    Ok(stem)
}

fn caller(family: &Family, inputs: &Inputs, checks: &[String]) -> String {
    let mut text = format!(
        "name: Release\non:\n  push:\n    tags: [\"v*\"]\npermissions:\n  contents: read\n\
         jobs:\n  release:\n    permissions:\n      actions: read\n      attestations: write\n      \
         contents: write\n      id-token: write\n    \
         uses: ryanduguid/release-policy/.github/workflows/{}@{POLICY_COMMIT} # {POLICY_RELEASE}\n    \
         with:\n",
        family.workflow
    );
    // Quoted, so YAML reads a stem such as 2048, 1e3 or null as text. The
    // STEM, BINARY and PATH admit no '"' or '\', so no value needs an escape.
    for (key, value) in inputs {
        text += &format!("      {key}: \"{value}\"\n");
    }
    text += "      required-checks: |\n";
    for check in checks {
        text += &format!("        {check}\n");
    }
    text
}

/// Create the caller, replacing an existing file only when forced, and never
/// through a symbolic or hard link.
fn write_caller(root: &Path, text: &str, force: bool) -> Result<()> {
    let mut path = root.to_path_buf();
    let mut relative = Vec::new();
    for part in CALLER.split('/') {
        path.push(part);
        relative.push(part);
        if symlink(&path) {
            return Err(format!(
                "{} must not be a symbolic link",
                relative.join("/")
            ));
        }
    }
    if path.exists() {
        if !force {
            return Err(format!(
                "{CALLER} already exists; pass --force to replace it"
            ));
        }
        // Remove the name, so a hard link's other names keep their bytes.
        std::fs::remove_file(&path).map_err(|e| io_error(e, &path))?;
    }
    let directory = path.parent().unwrap_or(root);
    std::fs::create_dir_all(directory).map_err(|e| io_error(e, directory))?;
    write_bytes(&path, text.as_bytes())
}

fn report(family: &Family) -> String {
    let docs = format!("https://github.com/ryanduguid/release-policy/blob/{POLICY_RELEASE}/docs");
    let mut text = format!(
        "Wrote {CALLER}: {} through {} at release-policy {POLICY_RELEASE} ({POLICY_COMMIT}).\n\n\
         Left for you:\n\
         - Commit the workflow to main: each release reads it from main's head.\n\
         - Protect release tags with an active tag ruleset on refs/tags/v*: update and deletion rules, no bypass actors.\n",
        family.kind, family.workflow
    );
    for line in family.left {
        text += &format!("- {}\n", line.replace("{docs}", &docs));
    }
    text += &format!(
        "- To release: RELEASE_NOTES.md starting '# vX.Y.Z', the required checks green on main's head, then an annotated tag vX.Y.Z pushed there.\n\
         No secret is needed: the release runs on the workflow's own token.\n\
         Guide: {docs}/{}\n\
         Prerequisites: {docs}/consumer-prerequisites.md\n",
        family.guide
    );
    text
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    use std::fs::File;

    /// A caller, a hard link to it and a copy of it, each opened as init
    /// opens a name to identify it.
    fn files() -> (tempfile::TempDir, [File; 3]) {
        let dir = tempfile::tempdir().unwrap();
        let path = |name: &str| dir.path().join(name);
        std::fs::write(path("release.yml"), "name: Mine\n").unwrap();
        std::fs::hard_link(path("release.yml"), path("linked.yml")).unwrap();
        std::fs::write(path("copy.yml"), "name: Mine\n").unwrap();
        let open = |name: &str| open_for_id(&path(name)).unwrap();
        let files = [open("release.yml"), open("linked.yml"), open("copy.yml")];
        (dir, files)
    }

    /// What a volume without the 128-bit file ID returns for it.
    fn unsupported(_: &File) -> std::io::Result<()> {
        Err(std::io::ErrorKind::Unsupported.into())
    }

    #[test]
    fn a_hard_link_is_found_by_the_64_bit_index_where_the_128_bit_id_fails() {
        let (_dir, [caller, linked, copy]) = files();
        assert!(same_id([&caller, &linked], unsupported, file_index).unwrap());
        assert!(!same_id([&caller, &copy], unsupported, file_index).unwrap());
    }

    /// A 128-bit ID for one name and a 64-bit index for the other would
    /// never be equal, so a failure on either name moves both to the index.
    #[test]
    fn a_128_bit_id_failing_on_one_name_alone_moves_both_to_the_index() {
        let (_dir, [caller, linked, _]) = files();
        let calls = std::cell::Cell::new(0);
        let first_only = |file: &File| {
            calls.set(calls.get() + 1);
            if calls.get() == 1 {
                file_id(file)
            } else {
                Err(std::io::ErrorKind::Unsupported.into())
            }
        };
        assert!(same_id([&caller, &linked], first_only, file_index).unwrap());
    }

    /// With neither ID, init cannot rule out the caller, so it refuses.
    #[test]
    fn a_name_without_either_id_is_an_error_not_another_file() {
        let (_dir, [caller, _, copy]) = files();
        assert!(same_id([&caller, &copy], unsupported, unsupported).is_err());
    }

    #[test]
    fn the_128_bit_id_finds_a_hard_link_where_the_volume_gives_it() {
        let (_dir, [caller, linked, copy]) = files();
        assert!(same_id([&caller, &linked], file_id, unsupported).unwrap());
        assert!(!same_id([&caller, &copy], file_id, unsupported).unwrap());
    }
}
