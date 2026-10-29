use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{LazyLock, Mutex, MutexGuard, PoisonError};

use regex::Regex;
use sha2::{Digest, Sha256};

use crate::{Result, py};

pub const SEMVER: &str = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)";
pub const SHA: &str = "[0-9a-f]{40}";
pub const REPOSITORY: &str = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+";

pub fn full(pattern: &str, value: &str) -> bool {
    Regex::new(&format!("\\A(?:{pattern})\\z"))
        .unwrap()
        .is_match(value)
}

pub fn sha256(raw: &[u8]) -> String {
    Sha256::digest(raw)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

pub fn run(root: &Path, program: &str, args: &[&str]) -> Result<Output> {
    Command::new(program)
        .args(args)
        .current_dir(root)
        .output()
        .map_err(|e| py::oserror_str(&e, program, py::OsStyle::Native))
}

pub fn checked(root: &Path, program: &str, args: &[&str]) -> Result<Output> {
    let out = run(root, program, args)?;
    if !out.status.success() {
        let command = std::iter::once(program)
            .chain(args.iter().copied())
            .map(py::repr_str)
            .collect::<Vec<_>>()
            .join(", ");
        return Err(format!(
            "Command '[{command}]' returned non-zero exit status {}.",
            out.status.code().unwrap_or(1)
        ));
    }
    Ok(out)
}

pub fn git(root: &Path, args: &[&str]) -> Result<String> {
    let out = checked(root, "git", args)?;
    Ok(py::universal_newlines(
        &py::decode_utf8(&out.stdout).map_err(|e| e.to_string())?,
    ))
}

pub fn absolute(path: &Path) -> Result<PathBuf> {
    // Python resolve(strict=False) also resolves the existing prefix of missing paths.
    Ok(py::resolve(path))
}

pub fn io_error(error: std::io::Error, path: &Path) -> String {
    py::oserror_str(&error, &path.to_string_lossy(), py::OsStyle::Errno)
}

pub fn read_bytes(path: &Path) -> Result<Vec<u8>> {
    std::fs::read(path).map_err(|e| io_error(e, path))
}

pub fn write_bytes(path: &Path, data: &[u8]) -> Result<()> {
    std::fs::write(path, data).map_err(|e| io_error(e, path))
}

pub fn text_bytes(text: &str) -> Vec<u8> {
    if cfg!(windows) {
        text.replace('\n', "\r\n").into_bytes()
    } else {
        text.as_bytes().to_vec()
    }
}

static UNBUFFERED: LazyLock<bool> = LazyLock::new(python_unbuffered);
static STDOUT: LazyLock<Mutex<Box<dyn Write + Send>>> =
    LazyLock::new(|| Mutex::new(python_stdout(*UNBUFFERED)));
/// A write to stderr failed. Buffered, its bytes stayed pending in Python's
/// stderr and failed again at exit.
static STDERR_FAILED: AtomicBool = AtomicBool::new(false);
/// A terminal write that clap failed (`usage_failed`), still pending at exit.
static STDOUT_PENDING: Mutex<Option<std::io::Error>> = Mutex::new(None);

/// `PYTHONUNBUFFERED` as CPython reads it: set and not an integer equal to 0.
fn python_unbuffered() -> bool {
    std::env::var("PYTHONUNBUFFERED").is_ok_and(|value| {
        !value.is_empty()
            && value
                .trim_start_matches([' ', '\t', '\n', '\x0b', '\x0c', '\r'])
                .parse::<i32>()
                != Ok(0)
    })
}

/// stdout as the scripts had it: written straight to the handle with
/// `PYTHONUNBUFFERED`, line-buffered on a terminal, and otherwise held in a
/// 128 KiB buffer (CPython 3.15's `io.DEFAULT_BUFFER_SIZE`) until it fills or
/// the process exits.
fn python_stdout(unbuffered: bool) -> Box<dyn Write + Send> {
    use std::io::IsTerminal;
    let stdout = std::io::stdout();
    let terminal = stdout.is_terminal();
    if unbuffered && terminal {
        // Rust's stdout keeps a Windows console's UTF-16 writes; a raw handle would not.
        return Box::new(Unbuffered(stdout));
    }
    if terminal {
        return Box::new(std::io::LineWriter::new(stdout));
    }
    if unbuffered {
        #[cfg(unix)]
        let handle = std::os::fd::AsFd::as_fd(&stdout)
            .try_clone_to_owned()
            .map(std::fs::File::from);
        #[cfg(windows)]
        let handle = std::os::windows::io::AsHandle::as_handle(&stdout)
            .try_clone_to_owned()
            .map(std::fs::File::from);
        if let Ok(file) = handle {
            return Box::new(file);
        }
    }
    Box::new(std::io::BufWriter::with_capacity(128 * 1024, stdout))
}

/// Flushes each write, so it reaches the handle before the write returns.
struct Unbuffered(std::io::Stdout);

impl Write for Unbuffered {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let written = self.0.write(buf)?;
        self.0.flush()?;
        Ok(written)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.0.flush()
    }
}

fn stdout() -> MutexGuard<'static, Box<dyn Write + Send>> {
    STDOUT.lock().unwrap_or_else(PoisonError::into_inner)
}

fn write_stderr(raw: &[u8]) -> std::io::Result<()> {
    std::io::stderr()
        .lock()
        .write_all(raw)
        .inspect_err(|_| STDERR_FAILED.store(true, Ordering::SeqCst))
}

/// `(type(exc).__name__, str(exc))` for a failed write to a standard stream:
/// `OSError: [Errno 22] Invalid argument` for a closed pipe on Windows,
/// `BrokenPipeError: [Errno 32] Broken pipe` elsewhere.
fn stream_error(error: &std::io::Error) -> (&'static str, String) {
    let mut text = error.to_string();
    if let Some(at) = text.rfind(" (os error ") {
        text.truncate(at);
    }
    match error.raw_os_error() {
        Some(code) if cfg!(windows) => crt_error(code),
        Some(32) => ("BrokenPipeError", format!("[Errno 32] {text}")),
        Some(code) => ("OSError", format!("[Errno {code}] {text}")),
        None => ("OSError", text),
    }
}

/// The exception for a Windows error code. CPython wrote through the C
/// runtime's `_write`, which reports ERROR_ACCESS_DENIED as EBADF and maps the
/// rest as `__acrt_errno_from_os_error` does (UCRT misc/errno.cpp), worded
/// from `_sys_errlist`. A pipe with no reader (ERROR_NO_DATA) is EINVAL.
fn crt_error(code: i32) -> (&'static str, String) {
    let (errno, kind, message) = match code {
        5 | 6 | 114 | 130 => (9, "OSError", "Bad file descriptor"),
        2 | 3 | 15 | 18 | 53 | 67 | 161 | 206 => {
            (2, "FileNotFoundError", "No such file or directory")
        }
        4 => (24, "OSError", "Too many open files"),
        7..=9 | 1816 => (12, "OSError", "Not enough space"),
        10 => (7, "OSError", "Arg list too long"),
        11 | 188..=202 => (8, "OSError", "Exec format error"),
        16 | 19..=36 | 65 | 82 | 83 | 108 | 158 | 167 => {
            (13, "PermissionError", "Permission denied")
        }
        17 => (18, "OSError", "Improper link"),
        80 | 183 => (17, "FileExistsError", "File exists"),
        89 | 164 | 215 => (11, "BlockingIOError", "Resource temporarily unavailable"),
        109 => (32, "BrokenPipeError", "Broken pipe"),
        112 => (28, "OSError", "No space left on device"),
        128 | 129 => (10, "ChildProcessError", "No child processes"),
        145 => (41, "OSError", "Directory not empty"),
        1113 => (42, "OSError", "Illegal byte sequence"),
        _ => (22, "OSError", "Invalid argument"),
    };
    (kind, format!("[Errno {errno}] {message}"))
}

/// `print` to stdout, or to stderr when `error`. No script caught the OSError
/// a failed print raised: it ends the process as an uncaught exception does
/// (docs/rust-cli.md).
pub fn print(text: &str, error: bool) {
    let raw = text_bytes(text);
    let written = if error {
        write_stderr(&raw)
    } else {
        stdout().write_all(&raw)
    };
    if let Err(failure) = written {
        let message = format!("release-policy: {}\n", stream_error(&failure).1);
        let _ = write_stderr(&text_bytes(&message));
        std::process::exit(shutdown(1));
    }
}

/// argparse's help or usage message: argparse ignored its own write errors.
pub fn print_usage(text: &str, error: bool) {
    let raw = text_bytes(text);
    let _ = if error {
        write_stderr(&raw)
    } else {
        stdout().write_all(&raw)
    };
}

/// A help or usage message clap could not write to a terminal. argparse
/// ignored the error, but with buffering its bytes stayed pending and failed
/// again at exit.
pub fn usage_failed(error: std::io::Error, stderr: bool) {
    if stderr {
        STDERR_FAILED.store(true, Ordering::SeqCst);
    } else if !*UNBUFFERED {
        *STDOUT_PENDING
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = Some(error);
    }
}

/// Interpreter shutdown: flush stdout, then stderr. Output still pending in
/// either fails there with exit status 120.
pub fn shutdown(code: i32) -> i32 {
    let mut code = code;
    let pending = STDOUT_PENDING
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .take();
    if let Err(error) = pending.map_or_else(|| stdout().flush(), Err) {
        let (kind, message) = stream_error(&error);
        let text = format!("Exception ignored while flushing sys.stdout:\n{kind}: {message}\n");
        let _ = write_stderr(&text_bytes(&text));
        code = 120;
    }
    if STDERR_FAILED.load(Ordering::SeqCst) && !*UNBUFFERED {
        code = 120;
    }
    code
}

pub fn symlink(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink())
}

pub fn repr_list<'a>(items: impl IntoIterator<Item = &'a str>) -> String {
    format!(
        "[{}]",
        items
            .into_iter()
            .map(py::repr_str)
            .collect::<Vec<_>>()
            .join(", ")
    )
}

/// Python JSON separators, escaping and float rendering.
pub fn dumps(
    value: &serde_json::Value,
    pretty: bool,
    sorted: bool,
    ascii: bool,
    compact: bool,
) -> String {
    fn quote(s: &str, ascii: bool) -> String {
        let mut out = String::from("\"");
        for c in s.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\x08' => out.push_str("\\b"),
                '\x0c' => out.push_str("\\f"),
                c if c < ' ' || (ascii && !c.is_ascii()) => {
                    use std::fmt::Write;
                    for u in c.encode_utf16(&mut [0; 2]) {
                        let _ = write!(out, "\\u{u:04x}");
                    }
                }
                c => out.push(c),
            }
        }
        out.push('"');
        out
    }
    fn render(
        v: &serde_json::Value,
        depth: usize,
        pretty: bool,
        sorted: bool,
        ascii: bool,
        compact: bool,
    ) -> String {
        use serde_json::Value;
        let sep = if compact { ":" } else { ": " };
        let comma = if compact || pretty { "," } else { ", " };
        match v {
            Value::Null => "null".into(),
            Value::Bool(b) => b.to_string(),
            Value::String(s) => quote(s, ascii),
            Value::Number(n) => {
                let s = n.to_string();
                if s.contains(['.', 'e', 'E']) {
                    py::float_repr(s.parse().unwrap_or(f64::INFINITY))
                } else {
                    s
                }
            }
            Value::Array(a) => {
                if a.is_empty() {
                    return "[]".into();
                }
                let parts = a
                    .iter()
                    .map(|x| render(x, depth + 1, pretty, sorted, ascii, compact))
                    .collect::<Vec<_>>();
                if pretty {
                    format!(
                        "[\n{}{}\n{}]",
                        "  ".repeat(depth + 1),
                        parts.join(&format!(",\n{}", "  ".repeat(depth + 1))),
                        "  ".repeat(depth)
                    )
                } else {
                    format!("[{}]", parts.join(comma))
                }
            }
            Value::Object(m) => {
                if m.is_empty() {
                    return "{}".into();
                }
                let mut pairs = m.iter().collect::<Vec<_>>();
                if sorted {
                    pairs.sort_by(|a, b| a.0.cmp(b.0));
                }
                let parts = pairs
                    .into_iter()
                    .map(|(k, v)| {
                        format!(
                            "{}{sep}{}",
                            quote(k, ascii),
                            render(v, depth + 1, pretty, sorted, ascii, compact)
                        )
                    })
                    .collect::<Vec<_>>();
                if pretty {
                    format!(
                        "{{\n{}{}\n{}}}",
                        "  ".repeat(depth + 1),
                        parts.join(&format!(",\n{}", "  ".repeat(depth + 1))),
                        "  ".repeat(depth)
                    )
                } else {
                    format!("{{{}}}", parts.join(comma))
                }
            }
        }
    }
    render(value, 0, pretty, sorted, ascii, compact)
}

pub fn read_utf8(path: &Path, label: &str) -> Result<String> {
    let raw = read_bytes(path)?;
    if raw.starts_with(b"\xef\xbb\xbf") {
        return Err(format!("{label} must not contain a UTF-8 BOM"));
    }
    String::from_utf8(raw).map_err(|_| format!("{label} must be UTF-8"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn windows_errors_read_as_the_c_runtime_reported_them() {
        assert_eq!(
            crt_error(232),
            ("OSError", "[Errno 22] Invalid argument".into())
        );
        assert_eq!(
            crt_error(5),
            ("OSError", "[Errno 9] Bad file descriptor".into())
        );
        assert_eq!(
            crt_error(33),
            ("PermissionError", "[Errno 13] Permission denied".into())
        );
        assert_eq!(
            crt_error(39),
            ("OSError", "[Errno 22] Invalid argument".into())
        );
        assert_eq!(
            crt_error(109),
            ("BrokenPipeError", "[Errno 32] Broken pipe".into())
        );
        assert_eq!(
            crt_error(112),
            ("OSError", "[Errno 28] No space left on device".into())
        );
        assert_eq!(
            crt_error(1816),
            ("OSError", "[Errno 12] Not enough space".into())
        );
    }

    #[test]
    fn a_failed_terminal_usage_message_fails_again_at_exit() {
        usage_failed(std::io::Error::from_raw_os_error(232), false);
        assert_eq!(shutdown(2), if *UNBUFFERED { 2 } else { 120 });
    }

    #[test]
    fn run_reports_spawn_failures() {
        let error = run(&std::env::temp_dir(), "release-policy-no-such-program", &[]).unwrap_err();
        assert!(!error.is_empty());
    }

    #[test]
    fn checked_reports_non_zero_exits_like_python() {
        let error =
            checked(&std::env::temp_dir(), "git", &["--release-policy-nonsense"]).unwrap_err();
        let repr = std::iter::once("git")
            .chain(["--release-policy-nonsense"])
            .map(py::repr_str)
            .collect::<Vec<_>>()
            .join(", ");
        assert!(
            error.starts_with(&format!(
                "Command '[{repr}]' returned non-zero exit status "
            )),
            "{error}"
        );
    }

    #[test]
    fn dumps_renders_python_separators_escapes_and_orderings() {
        let specials = "q\u{22}\u{5c}\u{8}\u{c}\u{a}\u{d}\u{9}\u{1}\u{e9}";
        let rendered =
            "q\u{5c}\u{22}\u{5c}\u{5c}\u{5c}b\u{5c}f\u{5c}n\u{5c}r\u{5c}t\u{5c}u0001\u{e9}";
        let rendered_ascii =
            "q\u{5c}\u{22}\u{5c}\u{5c}\u{5c}b\u{5c}f\u{5c}n\u{5c}r\u{5c}t\u{5c}u0001\u{5c}u00e9";
        let value = json!({
            "b": [1, 2.0, specials, [], {}],
            "a": 1,
        });
        assert_eq!(
            dumps(&value, false, false, false, false),
            format!(
                "{{\u{22}b\u{22}: [1, 2.0, \u{22}{rendered}\u{22}, [], {{}}], \u{22}a\u{22}: 1}}"
            )
        );
        assert_eq!(
            dumps(&value, false, false, false, true),
            format!("{{\u{22}b\u{22}:[1,2.0,\u{22}{rendered}\u{22},[],{{}}],\u{22}a\u{22}:1}}")
        );
        assert_eq!(
            dumps(&value, false, true, false, false),
            format!(
                "{{\u{22}a\u{22}: 1, \u{22}b\u{22}: [1, 2.0, \u{22}{rendered}\u{22}, [], {{}}]}}"
            )
        );
        assert_eq!(
            dumps(&value, false, false, true, false),
            format!(
                "{{\u{22}b\u{22}: [1, 2.0, \u{22}{rendered_ascii}\u{22}, [], {{}}], \u{22}a\u{22}: 1}}"
            )
        );
    }

    #[test]
    fn dumps_indents_nested_containers_like_python() {
        let value = json!({"a": [1, 2]});
        assert_eq!(
            dumps(&value, true, false, false, false),
            "{\n  \"a\": [\n    1,\n    2\n  ]\n}"
        );
    }

    #[test]
    fn dumps_writes_null_and_booleans_as_json_literals() {
        let value = json!([null, true, false]);
        assert_eq!(
            dumps(&value, false, false, false, false),
            "[null, true, false]"
        );
    }
}
