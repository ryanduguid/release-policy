//! Fixtures shared by the integration tests: temporary Git checkouts and the
//! binary itself.
#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// A temporary directory under the resolved system temp directory, as
/// pytest's `tmp_path` is. Windows runners spell TEMP with an 8.3 short name
/// (`RUNNER~1`), which the archive builder's resolved-parent check refuses.
pub fn tempdir() -> tempfile::TempDir {
    tempfile::tempdir_in(release_policy::py::resolve(&std::env::temp_dir())).unwrap()
}

/// A temporary Git checkout with a fixed identity and LF line endings.
pub struct Repo {
    pub dir: tempfile::TempDir,
}

impl Repo {
    pub fn new() -> Self {
        Self::init(tempdir())
    }

    /// A checkout inside `parent`, so a test can watch its surroundings.
    pub fn new_in(parent: &Path) -> Self {
        Self::init(tempfile::tempdir_in(parent).unwrap())
    }

    fn init(dir: tempfile::TempDir) -> Self {
        let repo = Repo { dir };
        repo.git(&["init", "-q", "-b", "main"]);
        for (key, value) in [
            ("user.name", "Fixture"),
            ("user.email", "fixture@example.invalid"),
            ("core.autocrlf", "false"),
            ("commit.gpgsign", "false"),
            ("tag.gpgSign", "false"),
            ("maintenance.auto", "false"),
        ] {
            repo.git(&["config", key, value]);
        }
        repo
    }

    pub fn root(&self) -> &Path {
        self.dir.path()
    }

    pub fn path(&self, relative: &str) -> PathBuf {
        self.root().join(relative)
    }

    /// Run git and return its trimmed stdout; a failure panics.
    pub fn git(&self, args: &[&str]) -> String {
        let out = Command::new("git")
            .args(args)
            .current_dir(self.root())
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).unwrap().trim().to_owned()
    }

    pub fn write(&self, relative: &str, contents: impl AsRef<[u8]>) -> PathBuf {
        let path = self.path(relative);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, contents).unwrap();
        path
    }

    /// Stage everything and commit; returns the new HEAD.
    pub fn commit(&self, message: &str) -> String {
        self.git(&["add", "-A"]);
        self.git(&["commit", "-q", "--allow-empty", "-m", message]);
        self.git(&["rev-parse", "HEAD"])
    }

    /// Record an index entry of the given mode without a matching object.
    pub fn set_mode(&self, relative: &str, mode: &str) {
        self.git(&[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("{mode},{},{relative}", "1".repeat(40)),
        ]);
    }
}

/// What one run of the binary printed, with Windows line endings folded.
pub struct Output {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// Run `release-policy` with the given arguments in `cwd`.
pub fn cli(cwd: &Path, args: &[&str], stdin: &str) -> Output {
    cli_env(cwd, args, stdin, &[])
}

pub fn cli_env(cwd: &Path, args: &[&str], stdin: &str, env: &[(&str, &str)]) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_release-policy"))
        .args(args)
        .current_dir(cwd)
        .envs(env.iter().copied())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    std::io::Write::write_all(child.stdin.as_mut().unwrap(), stdin.as_bytes()).unwrap();
    let out = child.wait_with_output().unwrap();
    let text = |raw: Vec<u8>| String::from_utf8(raw).unwrap().replace("\r\n", "\n");
    Output {
        code: out.status.code().unwrap_or(-1),
        stdout: text(out.stdout),
        stderr: text(out.stderr),
    }
}

/// The test double installed as `gh` first on PATH: it answers each call from
/// `rules` (`{"args": [...], "stdout", "stderr", "code", "files"}`) and logs it.
pub struct FakeGh {
    dir: tempfile::TempDir,
}

impl FakeGh {
    pub fn new(rules: serde_json::Value) -> Self {
        Self::program("gh", rules)
    }

    /// The same double installed under another program name.
    pub fn program(program: &str, rules: serde_json::Value) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let name = format!("{program}{}", std::env::consts::EXE_SUFFIX);
        std::fs::create_dir(dir.path().join("bin")).unwrap();
        std::fs::copy(
            env!("CARGO_BIN_EXE_fake-gh"),
            dir.path().join("bin").join(name),
        )
        .unwrap();
        let fake = FakeGh { dir };
        fake.set_rules(rules);
        fake
    }

    /// The directory holding the installed double.
    pub fn bin(&self) -> PathBuf {
        self.dir.path().join("bin")
    }

    pub fn set_rules(&self, rules: serde_json::Value) {
        let log = self.dir.path().join("calls.log");
        let scenario = serde_json::json!({"rules": rules, "log": log});
        std::fs::write(self.dir.path().join("scenario.json"), scenario.to_string()).unwrap();
    }

    /// PATH and scenario variables for a run that should reach this `gh`.
    pub fn env(&self) -> Vec<(String, String)> {
        let path = std::env::join_paths(std::iter::once(self.dir.path().join("bin")).chain(
            std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()),
        ))
        .unwrap();
        vec![
            ("PATH".into(), path.to_string_lossy().into_owned()),
            (
                "FAKE_GH_SCENARIO".into(),
                self.dir
                    .path()
                    .join("scenario.json")
                    .to_string_lossy()
                    .into_owned(),
            ),
        ]
    }

    /// Every call so far, as argument lists.
    pub fn calls(&self) -> Vec<Vec<String>> {
        std::fs::read_to_string(self.dir.path().join("calls.log"))
            .unwrap_or_default()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    pub fn run(&self, cwd: &Path, args: &[&str], stdin: &str) -> Output {
        let env = self.env();
        let env = env
            .iter()
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect::<Vec<_>>();
        cli_env(cwd, args, stdin, &env)
    }
}

/// One fake `gh` answer.
pub fn rule(args: &[&str], stdout: &str, stderr: &str, code: i32) -> serde_json::Value {
    serde_json::json!({"args": args, "stdout": stdout, "stderr": stderr, "code": code})
}

/// True where the platform let us make a symbolic link; Windows needs a privilege.
pub fn symlink(target: &Path, link: &Path) -> bool {
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(target, link).is_ok()
    }
    #[cfg(windows)]
    {
        if target.is_dir() {
            std::os::windows::fs::symlink_dir(target, link).is_ok()
        } else {
            std::os::windows::fs::symlink_file(target, link).is_ok()
        }
    }
}
