use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

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

pub fn print(text: &str, error: bool) {
    let raw = text_bytes(text);
    if error {
        let _ = std::io::stderr().lock().write_all(&raw);
    } else {
        let _ = std::io::stdout().lock().write_all(&raw);
    }
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
