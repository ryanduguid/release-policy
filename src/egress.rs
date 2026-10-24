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

/// Python's `\b` before a word character: the start, or a non-word character.
macro_rules! pre {
    () => {
        r"(?:\A|(?P<pre>(?-i:[^\w])))"
    };
}
/// Python's `\b` after a word character: the end, or a non-word character.
macro_rules! post {
    () => {
        r"(?:\z|(?P<post>(?-i:[^\w])))"
    };
}

/// STRUCTURAL_PATTERNS rewritten for the `regex` crate, which scans a line in
/// linear time with no backtracking limit; fancy-regex gave up on a long line.
/// `regex` has no lookaround and its `\b` uses other Unicode tables, so
/// Python's `\b` and the dotenv lookbehind consume the neighbouring character
/// in a group named `pre*` or `post*`, which the reported span leaves out.
/// Leftmost-first matching then picks the match Python's backtracking picks.
const SCAN_PATTERNS: [&str; 10] = [
    STRUCTURAL_PATTERNS[0].1,
    STRUCTURAL_PATTERNS[1].1,
    concat!(pre!(), r"(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}", post!()),
    concat!(pre!(), r"192\.168\.\d{1,3}\.\d{1,3}", post!()),
    concat!(
        pre!(),
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}",
        post!()
    ),
    concat!(
        pre!(),
        r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?",
        post!()
    ),
    // A leading hyphen is not a word character, so its \b needs one before it.
    concat!(
        r"(?:",
        pre!(),
        r"[A-Za-z0-9]|(?P<pre_hyphen>(?-i:[\w]))-)",
        r"[A-Za-z0-9-]*\.(?:internal|local|lan|corp|intranet)",
        post!()
    ),
    concat!(pre!(), r"(?:s3|gs|azure)://[A-Za-z0-9._-]+"),
    // Python ignores case in a class with no cased character, as here. The
    // suffix is tried longest first, as Python's greedy `+` backtracks; a
    // suffix ending in a hyphen needs a word character after it.
    concat!(
        r"(?:\A|(?P<pre>(?-i:[^\w.-])))\.env",
        r"(?:\.[A-Za-z0-9_-]*(?:[A-Za-z0-9_]",
        post!(),
        r"|-(?P<post_hyphen>(?-i:[\w])))|(?:\z|(?P<post_bare>(?-i:[^\w]))))"
    ),
    STRUCTURAL_PATTERNS[9].1,
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

/// A compiled structural pattern: a SCAN_PATTERNS rewrite, or a caller's own
/// pattern on fancy-regex, which has lookaround but a backtracking limit.
enum Pattern {
    Scan(regex::Regex),
    Custom(Regex),
}

impl Pattern {
    /// The byte span of the first match, as `re.search` finds it.
    fn search(&self, line: &str) -> Result<Option<std::ops::Range<usize>>> {
        match self {
            Pattern::Custom(regex) => Ok(regex
                .find(line)
                .map_err(|e| e.to_string())?
                .map(|m| m.range())),
            Pattern::Scan(regex) => Ok(regex.captures(line).map(|caps| {
                let mut span = caps.get_match().range();
                for (name, group) in regex.capture_names().zip(caps.iter()) {
                    match (name, group) {
                        (Some(n), Some(g)) if n.starts_with("pre") => span.start = g.end(),
                        (Some(n), Some(g)) if n.starts_with("post") => span.end = g.start(),
                        _ => {}
                    }
                }
                span
            })),
        }
    }
}

fn compile_patterns<'a>(
    patterns: &[(&'a str, &str)],
    scan: bool,
) -> Result<Vec<(&'a str, Pattern)>> {
    let mut compiled = Vec::new();
    for (label, pattern) in patterns {
        // Rust regex Unicode tables differ from Python's pinned release for
        // \d, \s and \w. Use the copied CPython tables in those classes.
        let pattern = format!(
            "(?i){}",
            pattern
                .replace(r"\d", &py::D)
                .replace(r"\s", &py::class_body(crate::pyunicode::SPACE))
                .replace(r"\w", &py::class_body(crate::pyunicode::WORD))
        );
        let regex = if scan {
            regex::Regex::new(&pattern)
                .map(Pattern::Scan)
                .map_err(|e| e.to_string())
        } else {
            Regex::new(&pattern)
                .map(Pattern::Custom)
                .map_err(|e| e.to_string())
        };
        let regex = regex.map_err(|e| {
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
    static DEFAULT: std::sync::LazyLock<Result<Vec<(&str, Pattern)>>> =
        std::sync::LazyLock::new(|| {
            let scan = STRUCTURAL_PATTERNS
                .iter()
                .zip(SCAN_PATTERNS)
                .map(|(&(label, _), pattern)| (label, pattern))
                .collect::<Vec<_>>();
            compile_patterns(&scan, true)
        });
    let dynamic;
    let compiled = if patterns == STRUCTURAL_PATTERNS {
        DEFAULT.as_ref().map_err(Clone::clone)?
    } else {
        dynamic = compile_patterns(patterns, false)?;
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
            if let Some(span) = regex.search(line)? {
                findings.push(Finding {
                    layer: 'B',
                    path: path.into(),
                    line: i + 1,
                    label: (*label).into(),
                    excerpt: excerpt(
                        line,
                        line[..span.start].chars().count(),
                        line[..span.end].chars().count(),
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn glob_matches_fnmatch_classes_and_literals() {
        assert!(glob("a*c", "abc"));
        assert!(!glob("a*c", "abd"));
        assert!(glob("a?c", "abc"));
        assert!(!glob("a?c", "ac"));
        assert!(glob("a[bc]d", "abd"));
        assert!(!glob("a[bc]d", "aed"));
        assert!(glob("a[!bc]d", "aed"));
        assert!(!glob("a[!bc]d", "abd"));
        assert!(glob("a[^bc]d", "a^d"));
        assert!(glob("a[^bc]d", "abd"));
        assert!(glob("a[b", "a[b"));
        // A ']' first in a class, or just after its '!', is a member.
        assert!(glob("a[]]d", "a]d"));
        assert!(glob("a[!]]d", "abd"));
        assert!(!glob("a[!]]d", "a]d"));
    }

    #[test]
    fn scan_text_compiles_patterns_other_than_the_structural_set() {
        let findings = scan_text(
            "first\nticket ABC-123 here\n",
            "notes.md",
            &[],
            "terms",
            &[("ticket id", r"\b[A-Z]+-\d+\b")],
        )
        .unwrap();
        assert_eq!(
            findings,
            [Finding {
                layer: 'B',
                path: "notes.md".into(),
                line: 2,
                label: "ticket id".into(),
                excerpt: "ticket ABC-123 here".into(),
            }]
        );
    }

    #[test]
    fn candidate_files_reports_unreadable_roots() {
        let missing = std::env::temp_dir().join("release-policy-egress-no-such-root");
        assert!(candidate_files(&[missing]).is_err());
    }

    /// Python's findings, from CPython 3.15.0, where a span or boundary is subtle.
    const BOUNDARY_CASES: [(&str, &[&str]); 13] = [
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\u{b2}-.internal ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] internal hostname: ...~~~~~~~~~~~~~~~~~~~~~~~\u{b2}-.internal ~~~~~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~box.internal\u{301} ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] internal hostname: ...~~~~~~~~~~~~~~~~~~~~~~~~box.internal\u{301} ~~~~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\u{b2}box.internal ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~10.0.0.1\u{b2} ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\u{345}.env ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] dotenv reference: ...~~~~~~~~~~~~~~~~~~~~~~~\u{345}.env ~~~~~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~.env.foo-bar ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] dotenv reference: ...~~~~~~~~~~~~~~~~~~~~~~~~.env.foo-bar ~~~~~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~.env.foo- ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] dotenv reference: ...~~~~~~~~~~~~~~~~~~~~~~~~.env.foo- ~~~~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~.env.foo-\u{b2} ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] dotenv reference: ...~~~~~~~~~~~~~~~~~~~~~~~~.env.foo-\u{b2} ~~~~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~.env.---\u{b2} ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] dotenv reference: ...~~~~~~~~~~~~~~~~~~~~~~~~.env.---\u{b2} ~~~~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~.env.---! ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] dotenv reference: ...~~~~~~~~~~~~~~~~~~~~~~~~.env.---! ~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~.env. ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] dotenv reference: ...~~~~~~~~~~~~~~~~~~~~~~~~.env. ~~~~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~http://localhost:80x ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] localhost url: ...~~~~~~~~~~~~~~~~~~~~~~~~http://localhost:80x ~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
        (
            "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~http://localhost:80. ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
            &[
                "f.md:1: [B] localhost url: ...~~~~~~~~~~~~~~~~~~~~~~~~http://localhost:80. ~~~~~~~~~~~~~~~~~~~~~~...",
            ],
        ),
    ];

    #[test]
    fn spans_and_boundaries_match_python() {
        for (text, expected) in BOUNDARY_CASES {
            let found = scan_text(text, "f.md", &[], "t", &STRUCTURAL_PATTERNS).unwrap();
            let found = found.iter().map(Finding::render).collect::<Vec<_>>();
            assert_eq!(found, expected, "{text:?}");
        }
    }

    /// A caller's pattern keeps Python's syntax and its own group names.
    #[test]
    fn custom_patterns_keep_python_syntax_and_spans() {
        let cases: [(&str, &str, &[&str]); 4] = [
            (
                "abcdefghijklmnopqrstuvwxyzfoobarABCDEFGHIJKLMNOPQRSTUVWXYZ",
                "(?P<prefix>foo)bar",
                &[
                    "f.md:1: [B] custom: ...cdefghijklmnopqrstuvwxyzfoobarABCDEFGHIJKLMNOPQRSTUVWX...",
                ],
            ),
            ("foo", "(?<!x)foo", &["f.md:1: [B] custom: foo"]),
            ("xfoo", "(?<!x)foo", &[]),
            (
                "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~.env.local",
                STRUCTURAL_PATTERNS[8].1,
                &["f.md:1: [B] custom: ...~~~~~~~~~~~~~~~~~~~~~~~~.env.local"],
            ),
        ];
        for (text, pattern, expected) in cases {
            let found = scan_text(text, "f.md", &[], "t", &[("custom", pattern)]).unwrap();
            let found = found.iter().map(Finding::render).collect::<Vec<_>>();
            assert_eq!(found, expected, "{pattern}");
        }
        let (pad, run) = ("~".repeat(30), "x".repeat(200));
        let found = scan_text(
            &format!("{pad}{run}{pad}"),
            "f.md",
            &[],
            "t",
            &[("custom", "(?P<post>x{100})(?P<pre>x{100})")],
        )
        .unwrap();
        let context = &pad[..24];
        assert_eq!(
            found.iter().map(Finding::render).collect::<Vec<_>>(),
            [format!("f.md:1: [B] custom: ...{context}{run}{context}...")]
        );
    }
}
