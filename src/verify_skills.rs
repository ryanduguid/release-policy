use crate::{Result, common::*, py};
use std::path::{Path, PathBuf};

pub const MODE: &str = "subcontractor-accounting-v1";

pub fn require_supported_mode(mode: &str) -> Result<()> {
    if mode != MODE {
        return Err(format!(
            "unsupported skills verification mode: {}",
            py::repr_str(mode)
        ));
    }
    Ok(())
}

pub fn guard_skill_release(mode: &str, tag: &str) -> Result<()> {
    require_supported_mode(mode)?;
    if tag == "v0.1.0" {
        return Err("v0.1.0 is frozen and must never be rebuilt or replaced".into());
    }
    Ok(())
}

pub fn resolve_safe_relative(root: &Path, supplied: &str, label: &str) -> Result<PathBuf> {
    if supplied.is_empty()
        || supplied.contains('\\')
        || full("[A-Za-z]:/.*", supplied)
        || supplied
            .split('/')
            .any(|part| matches!(part, "" | "." | ".."))
    {
        return Err(format!("{label} must be a safe relative POSIX path"));
    }
    let mut cursor = std::fs::canonicalize(root).map_err(|e| io_error(e, root))?;
    for part in supplied.split('/') {
        cursor.push(part);
        let meta = match std::fs::symlink_metadata(&cursor) {
            Ok(meta) => meta,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(format!("{label} is missing: {supplied}"));
            }
            Err(e) => return Err(io_error(e, &cursor)),
        };
        if meta.file_type().is_symlink() {
            return Err(format!("{label} contains a symlink component: {supplied}"));
        }
    }
    Ok(cursor)
}

pub fn tracked_mode(root: &Path, relative: &str) -> Result<String> {
    let out = run(
        root,
        "git",
        &["ls-files", "--stage", "--error-unmatch", "--", relative],
    )?;
    let text = py::decode_utf8(&out.stdout).map_err(|e| e.to_string())?;
    let lines = py::splitlines(&text);
    if !out.status.success() || lines.len() != 1 {
        return Err(format!(
            "required path is not one tracked Git entry: {relative}"
        ));
    }
    Ok(py::split_whitespace(lines[0])
        .first()
        .copied()
        .unwrap_or("")
        .to_owned())
}

pub fn require_tracked_regular_file(root: &Path, supplied: &str, label: &str) -> Result<PathBuf> {
    let candidate = resolve_safe_relative(root, supplied, label)?;
    if !candidate.is_file() {
        return Err(format!("{label} is not a regular file: {supplied}"));
    }
    let mode = tracked_mode(root, supplied)?;
    if mode != "100644" && mode != "100755" {
        return Err(format!("{label} has forbidden Git mode {mode}: {supplied}"));
    }
    Ok(candidate)
}

pub fn require_tracked_test_files(root: &Path) -> Result<Vec<PathBuf>> {
    let tests_root = resolve_safe_relative(root, "tests", "tests directory")?;
    if !tests_root.is_dir() {
        return Err("tests must be a real directory".into());
    }
    let out = checked(root, "git", &["ls-files", "-z", "--", "tests"])?;
    let text = py::decode_utf8(&out.stdout).map_err(|e| e.to_string())?;
    let mut tests = Vec::new();
    for name in text.split('\0').filter(|s| !s.is_empty()) {
        let file = name.rsplit('/').next().unwrap();
        if file.starts_with("test") && file.ends_with(".py") {
            tests.push(require_tracked_regular_file(root, name, "test file")?);
        }
    }
    if tests.is_empty() {
        return Err("tests must contain a tracked regular test*.py file".into());
    }
    Ok(tests)
}

pub fn read_canonical_version(path: &Path) -> Result<String> {
    let raw = read_bytes(path)?;
    let text = std::str::from_utf8(&raw).map_err(|_| "version file must be UTF-8")?;
    if text.starts_with('\u{feff}') {
        return Err("version file must not contain a BOM".into());
    }
    let version = text
        .strip_suffix("\r\n")
        .or_else(|| text.strip_suffix('\n'))
        .unwrap_or(text);
    if !full(SEMVER, version) {
        return Err("version file must contain one canonical MAJOR.MINOR.PATCH line".into());
    }
    Ok(version.to_owned())
}

/// The absolute path of the first `python` on PATH, as `sys.executable` names
/// the interpreter; plain `python` when none is found.
pub fn python_on_path() -> String {
    let name = if cfg!(windows) {
        "python.exe"
    } else {
        "python"
    };
    std::env::var_os("PATH")
        .and_then(|path| {
            std::env::split_paths(&path)
                .map(|dir| dir.join(name))
                .find(|candidate| candidate.is_file())
        })
        .map_or("python".into(), |p| p.to_string_lossy().into_owned())
}

pub fn verification_commands(python: &str) -> Vec<Vec<String>> {
    [
        vec![
            python,
            "-m",
            "pip",
            "install",
            "--isolated",
            "--disable-pip-version-check",
            "--no-input",
            "--no-deps",
            "--only-binary",
            ":all:",
            "--requirement",
            "requirements-test.txt",
        ],
        vec![
            python, "-B", "-m", "unittest", "discover", "-s", "tests", "-v",
        ],
        vec![python, "scripts/validate_validation.py"],
        vec![python, "tests/verify_skills_cli.py"],
    ]
    .into_iter()
    .map(|args| args.into_iter().map(str::to_owned).collect())
    .collect()
}

pub fn verify_with(
    root: &Path,
    mode: &str,
    version_file: &str,
    python: &str,
    mut runner: impl FnMut(&[String], &Path) -> Result<()>,
) -> Result<()> {
    require_supported_mode(mode)?;
    let version = require_tracked_regular_file(root, version_file, "version file")?;
    for (file, label) in [
        ("requirements-test.txt", "requirements file"),
        ("scripts/validate_validation.py", "validation program"),
        ("tests/verify_skills_cli.py", "Skills CLI program"),
    ] {
        require_tracked_regular_file(root, file, label)?;
    }
    require_tracked_test_files(root)?;
    read_canonical_version(&version)?;
    for command in verification_commands(python) {
        runner(&command, root)?;
    }
    Ok(())
}
