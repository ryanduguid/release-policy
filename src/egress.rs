use crate::{Result, common::*, py};
use fancy_regex::Regex;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

pub const STRUCTURAL_PATTERNS: [(&str, &str); 10] = [
    ("windows user path", r#"[A-Za-z]:\\+Users\\+[^\\\s"']+"#),
    ("unix home path", r"/(?:home|Users)/[A-Za-z0-9._-]+/"),
    ("private ipv4", r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    ("private ipv4", r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    (
        "private ipv4",
        r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b",
    ),
    (
        "localhost url",
        r"\bhttps?://(?:localhost|127\.0\.0\.1)(?::\d+)?\b",
    ),
    (
        "internal hostname",
        r"\b[A-Za-z0-9-]+\.(?:internal|local|lan|corp|intranet)\b",
    ),
    ("object store uri", r"\b(?:s3|gs|azure)://[A-Za-z0-9._-]+"),
    (
        "dotenv reference",
        r"(?<![\w.-])\.env(?:\.[A-Za-z0-9_-]+)?\b",
    ),
    ("unc share", r"\\\\[A-Za-z0-9._-]+\\[A-Za-z0-9._$-]+"),
];
const SUFFIXES: &[&str] = &[
    "cfg", "cff", "csv", "html", "ini", "json", "jsonl", "md", "py", "rst", "sh", "toml", "ts",
    "tsv", "txt", "xml", "yaml", "yml",
];

#[derive(Debug, Clone, PartialEq)]
pub struct Finding {
    pub layer: char,
    pub path: String,
    pub line: usize,
    pub label: String,
    pub excerpt: String,
}
impl Finding {
    pub fn render(&self) -> String {
        let where_ = format!(
            "{}:{}: [{}] {}",
            self.path, self.line, self.layer, self.label
        );
        if self.excerpt.is_empty() {
            where_
        } else {
            format!("{where_}: {}", self.excerpt)
        }
    }
}
#[derive(Debug, Clone)]
pub struct Term {
    pub line: usize,
    pub text: String,
}

pub fn load_terms(path: &Path) -> Result<Vec<Term>> {
    let text = py::read_text(path).map_err(|e| {
        format!(
            "cannot read the terms file {}: {}",
            path.display(),
            read_error(e, path)
        )
    })?;
    let terms = py::splitlines(&text)
        .into_iter()
        .enumerate()
        .filter_map(|(i, line)| {
            let s = py::strip(line);
            if s.is_empty() || s.starts_with('#') {
                None
            } else {
                Some(Term {
                    line: i + 1,
                    text: s.into(),
                })
            }
        })
        .collect::<Vec<_>>();
    if terms.is_empty() {
        return Err(format!("the terms file {} lists no terms", path.display()));
    }
    Ok(terms)
}

/// Every file under the supplied paths, in pathlib's order: deduplicated and
/// sorted as `Path` objects compare, which on Windows ignores case.
pub fn candidate_files(paths: &[PathBuf]) -> Result<Vec<PathBuf>> {
    fn walk(path: &Path, out: &mut Vec<PathBuf>, ancestors: &mut BTreeSet<PathBuf>) -> Result<()> {
        // Track ancestry to avoid cycles while collecting candidate paths.
        let identity = absolute(path)?;
        if !ancestors.insert(identity.clone()) {
            return Ok(());
        }
        let entries = std::fs::read_dir(path).map_err(|e| io_error(e, path))?;
        for entry in entries {
            let child = entry.map_err(|e| io_error(e, path))?.path();
            if child.is_file() {
                out.push(child);
            } else if child.is_dir() && !symlink(&child) {
                walk(&child, out, ancestors)?;
            }
        }
        ancestors.remove(&identity);
        Ok(())
    }
    let mut found = Vec::new();
    for supplied in paths {
        // Path() drops '.' components and repeated separators.
        let path = py::PyPath::new(&supplied.to_string_lossy()).fs();
        if !path.exists() {
            return Err(format!(
                "publication candidate {} does not exist",
                path.display()
            ));
        }
        if path.is_file() {
            found.push(path);
        } else {
            walk(&path, &mut found, &mut BTreeSet::new())?;
        }
    }
    // set() keeps the first spelling of a path; sorted() orders by parts.
    let mut seen = std::collections::HashSet::new();
    let mut unique = found
        .into_iter()
        .filter(|p| seen.insert(py::path_normcase(p)))
        .collect::<Vec<_>>();
    unique.sort_by_cached_key(|p| py::path_sort_key(p));
    Ok(unique)
}

fn glob(pattern: &str, path: &str) -> bool {
    let mut expression = String::from(r"\A");
    let chars = pattern.chars().collect::<Vec<_>>();
    let mut i = 0;
    while i < chars.len() {
        match chars[i] {
            '*' => expression.push_str("(?s:.*)"),
            '?' => expression.push_str("(?s:.)"),
            '[' => {
                let mut j = i + 1;
                if j < chars.len() && chars[j] == '!' {
                    j += 1;
                }
                if j < chars.len() && chars[j] == ']' {
                    j += 1;
                }
                while j < chars.len() && chars[j] != ']' {
                    j += 1;
                }
                if j == chars.len() {
                    expression.push_str(r"\[");
                } else {
                    let body = chars[i + 1..j].iter().collect::<String>();
                    let body = if let Some(rest) = body.strip_prefix('!') {
                        format!("^{rest}")
                    } else if body.starts_with('^') {
                        format!(r"\{body}")
                    } else {
                        body
                    };
                    expression.push('[');
                    expression.push_str(&body);
                    expression.push(']');
                    i = j;
                }
            }
            c => expression.push_str(&regex::escape(&c.to_string())),
        }
        i += 1;
    }
    expression.push_str(r"\z");
    Regex::new(&expression).is_ok_and(|r| r.is_match(path).unwrap_or(false))
}

pub fn partition_excluded(
    files: &[PathBuf],
    patterns: &[String],
) -> Result<(Vec<PathBuf>, Vec<PathBuf>)> {
    let mut kept = Vec::new();
    let mut excluded = Vec::new();
    let mut unused = patterns.iter().collect::<BTreeSet<_>>();
    for path in files {
        let posix = py::as_posix(path);
        let matched = patterns
            .iter()
            .filter(|p| glob(p, &posix))
            .collect::<Vec<_>>();
        if matched.is_empty() {
            kept.push(path.clone());
        } else {
            for p in matched {
                unused.remove(p);
            }
            excluded.push(path.clone());
        }
    }
    if !unused.is_empty() {
        return Err(format!(
            "these --exclude pattern(s) matched no file in the candidate: {}",
            unused
                .into_iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    Ok((kept, excluded))
}

fn excerpt(line: &str, start: usize, end: usize) -> String {
    let chars = line.chars().collect::<Vec<_>>();
    let left = start.saturating_sub(24);
    let right = (end + 24).min(chars.len());
    format!(
        "{}{}{}",
        if left > 0 { "..." } else { "" },
        py::strip(&chars[left..right].iter().collect::<String>()),
        if right < chars.len() { "..." } else { "" }
    )
}

fn compile_patterns<'a>(patterns: &[(&'a str, &str)]) -> Result<Vec<(&'a str, Regex)>> {
    let mut compiled = Vec::new();
    for (label, pattern) in patterns {
        // Rust regex Unicode tables differ from Python's pinned release for
        // \d, \s and \w. Use the copied CPython tables in those classes.
        let pattern = pattern
            .replace(r"\d", &py::D)
            .replace(r"\s", &py::class_body(crate::pyunicode::SPACE))
            .replace(r"\w", &py::class_body(crate::pyunicode::WORD));
        let regex = Regex::new(&format!("(?i){pattern}")).map_err(|e| {
            format!(
                "structural pattern {} does not compile: {e}",
                py::repr_str(label)
            )
        })?;
        compiled.push((*label, regex));
    }
    Ok(compiled)
}

pub fn scan_text(
    text: &str,
    path: &str,
    terms: &[Term],
    terms_label: &str,
    patterns: &[(&str, &str)],
) -> Result<Vec<Finding>> {
    static DEFAULT: std::sync::LazyLock<Result<Vec<(&str, Regex)>>> =
        std::sync::LazyLock::new(|| compile_patterns(&STRUCTURAL_PATTERNS));
    let dynamic;
    let compiled = if patterns == STRUCTURAL_PATTERNS {
        DEFAULT.as_ref().map_err(Clone::clone)?
    } else {
        dynamic = compile_patterns(patterns)?;
        &dynamic
    };
    let lowered_terms = terms
        .iter()
        .map(|t| (t, t.text.to_lowercase()))
        .collect::<Vec<_>>();
    let mut findings = Vec::new();
    for (i, line) in py::splitlines(text).into_iter().enumerate() {
        let lower = line.to_lowercase();
        for (term, lowered) in &lowered_terms {
            if lower.contains(lowered) {
                findings.push(Finding {
                    layer: 'A',
                    path: path.into(),
                    line: i + 1,
                    label: format!(
                        "configured term at {terms_label}:{} (match redacted)",
                        term.line
                    ),
                    excerpt: String::new(),
                });
            }
        }
        for (label, regex) in compiled {
            if let Some(m) = regex.find(line).map_err(|e| e.to_string())? {
                findings.push(Finding {
                    layer: 'B',
                    path: path.into(),
                    line: i + 1,
                    label: (*label).into(),
                    excerpt: excerpt(
                        line,
                        line[..m.start()].chars().count(),
                        line[..m.end()].chars().count(),
                    ),
                });
            }
        }
        if let Some(offset) = lower.find("x-internal-") {
            let marker = lower[..offset].chars().count();
            findings.push(Finding {
                layer: 'C',
                path: path.into(),
                line: i + 1,
                label: "reserved namespace 'x-internal-'".into(),
                excerpt: excerpt(line, marker, marker + 11),
            });
        }
    }
    Ok(findings)
}

#[derive(Default)]
pub struct CheckedCandidate {
    pub findings: Vec<Finding>,
    pub unscanned: Vec<PathBuf>,
    pub excluded: Vec<PathBuf>,
}

fn read_error(error: py::ReadError, path: &Path) -> String {
    match error {
        py::ReadError::Os(e) => io_error(e, path),
        py::ReadError::Decode(e) => e.to_string(),
    }
}

pub fn check_candidate(
    paths: &[PathBuf],
    terms: &[Term],
    exclude: &[String],
    terms_label: &str,
) -> Result<CheckedCandidate> {
    let (scanned, excluded) = partition_excluded(&candidate_files(paths)?, exclude)?;
    let mut checked = CheckedCandidate {
        excluded,
        ..Default::default()
    };
    for path in scanned {
        let text = if SUFFIXES.contains(
            &path
                .extension()
                .unwrap_or_default()
                .to_string_lossy()
                .to_lowercase()
                .as_str(),
        ) {
            match py::read_text(&path) {
                Ok(s) => Some(s),
                Err(py::ReadError::Decode(_)) => None,
                Err(e) => {
                    return Err(format!(
                        "cannot read the publication candidate {}: {}",
                        path.display(),
                        read_error(e, &path)
                    ));
                }
            }
        } else {
            None
        };
        if let Some(text) = text {
            checked.findings.extend(scan_text(
                &text,
                &py::as_posix(&path),
                terms,
                terms_label,
                &STRUCTURAL_PATTERNS,
            )?);
        } else {
            checked.unscanned.push(path);
        }
    }
    Ok(checked)
}
