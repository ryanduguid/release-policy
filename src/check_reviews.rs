//! Check a consumer's review index, verdict files and known-unknowns register.
//!
//! The rules are those in docs/review-verdicts.md. The check reads the consumer
//! checkout and its Git history only; it reaches no network and writes nothing.
//! It stops at the first failure, like verify-skills, so a maintainer fixes one
//! named defect at a time. With --base it also holds the ledgers to their
//! append-only rule against that revision.

use crate::common::{SHA, repr_list, run, sha256};
use crate::pyjson::{self, Dump, Json};
use crate::{Result, git_revision, py, verify_skills};
use regex::Regex;
use std::path::Path;
use std::sync::LazyLock;

pub const INDEX_PATH: &str = "reviews/index.json";
pub const KNOWN_UNKNOWNS_PATH: &str = "docs/known-unknowns.md";
const VERDICTS: [&str; 3] = ["ACCEPT", "FIX", "REJECT"];
const CONFIDENCES: [&str; 3] = ["HIGH", "LOW", "MEDIUM"];
const RELATIONSHIPS: [&str; 2] = ["author", "independent"];
const ENTRY_KEYS: [&str; 10] = [
    "confidence",
    "date",
    "id",
    "path",
    "reviewer",
    "scope",
    "sha256",
    "subject",
    "supersedes",
    "verdict",
];
const SUBJECT_HEADING: &str = "## Subject";
const HEADLINE_HEADING: &str = "## 1. Headline verdict";
const CITATION_HEADING: &str = "## 2. Citation audit";
const FINDINGS_HEADING: &str = "## 3. Findings";
const QUESTIONS_HEADING: &str = "## 4. Open questions";
const CHANGES_HEADING: &str = "## 5. Required changes";
const METHOD_HEADING: &str = "## 6. Method";
const ATTESTATION_HEADING: &str = "## 7. Attestation";
const ATTESTATION_SENTENCE: &str =
    "I read the subject at the commit above and the materials named in section 6.";
const SUBJECT_ROWS: [&str; 9] = [
    "Repository",
    "Commit",
    "Release",
    "Scope",
    "Reviewer",
    "Relationship",
    "Verdict",
    "Confidence",
    "Date",
];
const FINDING_FIELDS: [&str; 2] = ["Why it matters", "Remedy"];
const QUESTION_FIELDS: [&str; 2] = ["Why it matters", "Resolution path"];
const CHANGE_FIELDS: [&str; 2] = ["Change", "Re-review"];
const KNOWN_UNKNOWN_LINES: [&str; 5] = [
    "Raised",
    "Affects",
    "Why it matters",
    "Resolution path",
    "Status",
];

/// Python's `\S` in a `str` pattern.
fn non_space() -> String {
    let s = &*py::S;
    format!("[^{}]", &s[1..s.len() - 1])
}

fn anchored(pattern: &str) -> Regex {
    Regex::new(&format!(r"\A(?:{pattern})\z")).unwrap()
}

static ID: LazyLock<Regex> =
    LazyLock::new(|| anchored("[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*"));
static HEX_64: LazyLock<Regex> = LazyLock::new(|| anchored("[0-9a-f]{64}"));
static FULL_SHA: LazyLock<Regex> = LazyLock::new(|| anchored(SHA));
static REPOSITORY: LazyLock<Regex> = LazyLock::new(|| anchored(crate::common::REPOSITORY));
static RELEASE: LazyLock<Regex> = LazyLock::new(|| {
    anchored(
        r"(?:[a-z0-9]+(?:-[a-z0-9]+)*/)?v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
    )
});
// An issuer or Act token, then underscore-joined tokens with no whitespace:
// ITAA_1936_s_109E, SGAA_1992_s_18C(1)(c)(i), ITAA_1997_Subdiv_328-D,
// APES_110_para_R112.1, TPB_PG_01-2023. The convention for each family is
// in the schema document; the grammar only keeps the identifier unambiguous.
static SCOPE: LazyLock<Regex> = LazyLock::new(|| anchored(r"[A-Z][A-Z0-9]*(?:_[A-Za-z0-9.()-]+)+"));
static FINDING: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"\A(CRITICAL|WARNING|NOTE)\. {}", non_space())).unwrap());
static DEFECT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"\ADefect: {}", non_space())).unwrap());
static TABLE_ROW: LazyLock<Regex> = LazyLock::new(|| anchored(r"\|(.*)\|"));
static DASHES: LazyLock<Regex> = LazyLock::new(|| anchored("-+"));
static KNOWN_UNKNOWN_HEADING: LazyLock<Regex> =
    LazyLock::new(|| anchored(r"## (KU-[0-9]{3}): (.+\?)"));
static RESOLVED: LazyLock<Regex> = LazyLock::new(|| {
    anchored(&format!(
        "resolved ([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}) by ({}.*)",
        non_space()
    ))
});

#[derive(Clone, Debug)]
pub struct Review {
    pub id: String,
    pub path: String,
    pub sha256: String,
    pub repository: String,
    pub commit: String,
    pub release: Option<String>,
    pub scope: Vec<String>,
    pub name: String,
    pub credential: String,
    pub relationship: String,
    pub verdict: String,
    pub confidence: String,
    pub date: String,
}

#[derive(Clone, Debug)]
pub struct KnownUnknown {
    pub question: String,
    pub values: Vec<String>,
    pub resolved: Option<(String, String)>,
}

impl KnownUnknown {
    pub fn open(&self) -> bool {
        self.resolved.is_none()
    }
}

/// Decode a document the way the baseline digest does: UTF-8, no BOM, LF.
pub fn canonical_text(raw: &[u8], label: &str) -> Result<String> {
    if raw.starts_with(b"\xef\xbb\xbf") {
        return Err(format!("{label} must not contain a UTF-8 BOM"));
    }
    let text = std::str::from_utf8(raw).map_err(|_| format!("{label} must be valid UTF-8"))?;
    if text.replace("\r\n", "").contains('\r') {
        return Err(format!("{label} contains a lone carriage return"));
    }
    Ok(text.replace("\r\n", "\n"))
}

fn mapping<'a>(value: &'a Json, label: &str, keys: &[&str]) -> Result<&'a Json> {
    let exact = match value {
        Json::Object(pairs) => {
            pairs.len() == keys.len() && keys.iter().all(|k| value.get(k).is_some())
        }
        _ => false,
    };
    if !exact {
        let mut sorted = keys.to_vec();
        sorted.sort_unstable();
        return Err(format!(
            "{label} must contain exactly {}",
            repr_list(sorted)
        ));
    }
    Ok(value)
}

fn field<'a>(value: &'a Json, key: &str) -> &'a Json {
    value.get(key).expect("a key the shape check required")
}

fn text<'a>(value: &'a Json, label: &str) -> Result<&'a str> {
    match value {
        Json::Str(s) if !s.is_empty() => Ok(s),
        _ => Err(format!("{label} must be a non-empty string")),
    }
}

fn choice<'a>(value: &'a Json, label: &str, allowed: &[&str]) -> Result<&'a str> {
    let result = text(value, label)?;
    if !allowed.contains(&result) {
        return Err(format!(
            "{label} must be one of {}",
            repr_list(allowed.iter().copied())
        ));
    }
    Ok(result)
}

fn pattern<'a>(value: &'a Json, label: &str, pattern: &Regex, form: &str) -> Result<&'a str> {
    let result = text(value, label)?;
    if !pattern.is_match(result) {
        return Err(format!("{label} must be {form}"));
    }
    Ok(result)
}

pub fn date(value: &str, label: &str) -> Result<String> {
    if value.is_empty() {
        return Err(format!("{label} must be a non-empty string"));
    }
    if py::Date::fromisoformat(value).is_none() || py::char_len(value) != 10 {
        return Err(format!("{label} must be an ISO 8601 calendar date"));
    }
    Ok(value.to_owned())
}

pub fn require_not_after(value: &str, bound: &str, label: &str) -> Result<()> {
    // Both are YYYY-MM-DD, so string order is calendar order.
    if value > bound {
        return Err(format!(
            "{label} {value} is later than the checked commit date {bound}"
        ));
    }
    Ok(())
}

pub fn parse_index(document: &Json) -> Result<Vec<Review>> {
    let root = mapping(document, "review index", &["reviews", "schema"])?;
    if !field(root, "schema").py_eq(&Json::Int(1.into())) {
        return Err("review index schema must be 1".into());
    }
    let Json::Array(entries) = field(root, "reviews") else {
        return Err("review index reviews must be a list".into());
    };
    let mut reviews = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for (position, raw_entry) in entries.iter().enumerate() {
        let label = format!("review[{position}]");
        let entry = mapping(raw_entry, &label, &ENTRY_KEYS)?;
        let review_id = pattern(
            field(entry, "id"),
            &format!("{label}.id"),
            &ID,
            "YYYY-MM-DD-hyphenated-words",
        )?
        .to_owned();
        date(&review_id[..10], &format!("{label}.id date prefix"))?;
        if seen.contains(&review_id) {
            return Err(format!("duplicate review id: {review_id}"));
        }
        let label = format!("review {review_id}");
        let path = text(field(entry, "path"), &format!("{label} path"))?.to_owned();
        if path != format!("reviews/{review_id}.md") {
            return Err(format!("{label} path must be reviews/{review_id}.md"));
        }
        let digest = pattern(
            field(entry, "sha256"),
            &format!("{label} sha256"),
            &HEX_64,
            "64 lower-case hex digits",
        )?
        .to_owned();
        let subject = mapping(
            field(entry, "subject"),
            &format!("{label} subject"),
            &["commit", "release", "repository"],
        )?;
        let repository = pattern(
            field(subject, "repository"),
            &format!("{label} subject.repository"),
            &REPOSITORY,
            "owner/name",
        )?
        .to_owned();
        let commit = pattern(
            field(subject, "commit"),
            &format!("{label} subject.commit"),
            &FULL_SHA,
            "a full lower-case commit SHA",
        )?
        .to_owned();
        let release = match field(subject, "release") {
            Json::Null => None,
            release => Some(
                pattern(
                    release,
                    &format!("{label} subject.release"),
                    &RELEASE,
                    "a release tag such as v1.2.3 or prefix/v1.2.3",
                )?
                .to_owned(),
            ),
        };
        let scope = match field(entry, "scope") {
            Json::Array(items) if !items.is_empty() => items,
            _ => return Err(format!("{label} scope must be a non-empty list")),
        };
        let provisions = scope
            .iter()
            .enumerate()
            .map(|(index, item)| {
                pattern(
                    item,
                    &format!("{label} scope[{index}]"),
                    &SCOPE,
                    "an identifier such as ITAA_1936_s_109E or APES_110_para_R112.1",
                )
                .map(str::to_owned)
            })
            .collect::<Result<Vec<_>>>()?;
        let unique = provisions.iter().collect::<std::collections::HashSet<_>>();
        if unique.len() != provisions.len() {
            return Err(format!("{label} scope repeats an identifier"));
        }
        let reviewer = mapping(
            field(entry, "reviewer"),
            &format!("{label} reviewer"),
            &["credential", "name", "relationship"],
        )?;
        let name = text(field(reviewer, "name"), &format!("{label} reviewer.name"))?.to_owned();
        let credential = text(
            field(reviewer, "credential"),
            &format!("{label} reviewer.credential"),
        )?
        .to_owned();
        let relationship = choice(
            field(reviewer, "relationship"),
            &format!("{label} reviewer.relationship"),
            &RELATIONSHIPS,
        )?
        .to_owned();
        let verdict = choice(
            field(entry, "verdict"),
            &format!("{label} verdict"),
            &VERDICTS,
        )?
        .to_owned();
        let confidence = choice(
            field(entry, "confidence"),
            &format!("{label} confidence"),
            &CONFIDENCES,
        )?
        .to_owned();
        let signed = date(
            text(field(entry, "date"), &format!("{label} date"))?,
            &format!("{label} date"),
        )?;
        let supersedes = field(entry, "supersedes");
        if *supersedes != Json::Null
            && !seen
                .iter()
                .any(|earlier| supersedes.as_str() == Some(earlier))
        {
            return Err(format!(
                "{label} supersedes must name an earlier listed review"
            ));
        }
        seen.push(review_id.clone());
        reviews.push(Review {
            id: review_id,
            path,
            sha256: digest,
            repository,
            commit,
            release,
            scope: provisions,
            name,
            credential,
            relationship,
            verdict,
            confidence,
            date: signed,
        });
    }
    let mut sorted = seen.clone();
    sorted.sort();
    if seen != sorted {
        return Err("review index must be sorted by id".into());
    }
    Ok(reviews)
}

pub fn load_index_document(raw: &[u8], label: &str) -> Result<Json> {
    if raw.starts_with(b"\xef\xbb\xbf") {
        return Err(format!("{label} must not contain a UTF-8 BOM"));
    }
    let invalid = || format!("{label} must be valid UTF-8 JSON");
    let text = std::str::from_utf8(raw).map_err(|_| invalid())?;
    let document = pyjson::loads(text).map_err(|_| invalid())?;
    let canonical = Dump {
        indent: Some(2),
        sort_keys: true,
        ensure_ascii: true,
    };
    if raw != format!("{}\n", pyjson::dumps(&document, canonical)).as_bytes() {
        return Err(format!("{label} must use canonical sorted JSON"));
    }
    Ok(document)
}

/// `git` output read as Python's text mode reads it.
fn git(root: &Path, args: &[&str]) -> Result<(bool, String)> {
    let out = run(root, "git", args)?;
    let stdout = py::decode_utf8(&out.stdout).map_err(|e| e.to_string())?;
    Ok((out.status.success(), py::universal_newlines(&stdout)))
}

pub fn head_date(root: &Path) -> Result<String> {
    let (ok, stdout) = git(root, &["log", "-1", "--format=%cI"])?;
    if !ok {
        return Err("the consumer checkout has no commit to date the ledgers by".into());
    }
    Ok(py::prefix_chars(&stdout, 10).to_owned())
}

pub fn require_subject_commit(root: &Path, review: &Review) -> Result<()> {
    let label = format!("verdict {} subject", review.id);
    if !git(
        root,
        &["cat-file", "-e", &format!("{}^{{commit}}", review.commit)],
    )?
    .0
    {
        return Err(format!(
            "{label}.commit is not present in this checkout; fetch the full history"
        ));
    }
    if !git(
        root,
        &["merge-base", "--is-ancestor", &review.commit, "HEAD"],
    )?
    .0
    {
        return Err(format!(
            "{label}.commit is not an ancestor of the checked-out commit"
        ));
    }
    let Some(release) = &review.release else {
        return Ok(());
    };
    let (ok, stdout) = git(
        root,
        &[
            "rev-parse",
            "--verify",
            "--quiet",
            &format!("refs/tags/{release}^{{commit}}"),
        ],
    )?;
    if !ok {
        return Err(format!(
            "{label}.release tag {release} is not present in this checkout; fetch tags"
        ));
    }
    if py::strip(&stdout) != review.commit {
        return Err(format!(
            "{label}.release tag {release} points at another commit"
        ));
    }
    Ok(())
}

fn section<'a>(lines: &[&'a str], heading: &str) -> Vec<&'a str> {
    let start = lines.iter().position(|l| *l == heading).unwrap() + 1;
    lines[start..]
        .iter()
        .take_while(|l| !l.starts_with("## "))
        .copied()
        .collect()
}

/// Group `- ` list items with their two-space continuation lines (no blank lines).
fn items(lines: &[&str], label: &str) -> Result<Vec<(String, Vec<String>)>> {
    let mut items: Vec<(String, Vec<String>)> = Vec::new();
    for line in lines {
        if let Some(head) = line.strip_prefix("- ") {
            items.push((head.to_owned(), Vec::new()));
        } else if line.starts_with("  ") && !items.is_empty() {
            items.last_mut().unwrap().1.push(py::strip(line).to_owned());
        } else {
            return Err(format!("{label} has a line outside any list item: {line}"));
        }
    }
    Ok(items)
}

/// Read `Name: text` lines, letting a line without a name continue the last one.
fn fields(
    continuation: &[String],
    names: &[&str],
    label: &str,
) -> Result<std::collections::HashMap<String, String>> {
    let mut fields = std::collections::HashMap::new();
    let mut current: Option<&str> = None;
    for line in continuation {
        if let Some(name) = names
            .iter()
            .find(|name| line.starts_with(&format!("{name}: ")))
        {
            current = Some(name);
            let rest = &line[name.len() + 2..];
            fields.insert((*name).to_owned(), py::strip(rest).to_owned());
        } else if let Some(name) = current {
            let joined = format!("{} {line}", fields[name]);
            fields.insert(name.to_owned(), py::strip(&joined).to_owned());
        } else {
            return Err(format!(
                "{label} has text before its first named line: {line}"
            ));
        }
    }
    for name in names {
        if fields.get(*name).is_none_or(String::is_empty) {
            return Err(format!("{label} needs a non-empty '{name}:' line"));
        }
    }
    Ok(fields)
}

/// A list section holds items or the single word `none`.
fn list_section(lines: &[&str], heading: &str, label: &str) -> Result<Vec<(String, Vec<String>)>> {
    let content = section(lines, heading)
        .into_iter()
        .filter(|l| !py::strip(l).is_empty())
        .collect::<Vec<_>>();
    if content == ["none"] {
        return Ok(Vec::new());
    }
    if content.is_empty() {
        return Err(format!("{label} {heading} must list items or say none"));
    }
    items(&content, &format!("{label} {heading}"))
}

fn table_rows(lines: &[&str]) -> Vec<Vec<String>> {
    let mut rows = Vec::new();
    for line in lines {
        let Some(captures) = TABLE_ROW.captures(py::strip(line)) else {
            continue;
        };
        let cells = captures[1]
            .split('|')
            .map(|c| py::strip(c).to_owned())
            .collect::<Vec<_>>();
        if cells.iter().all(|c| DASHES.is_match(c)) {
            continue;
        }
        rows.push(cells);
    }
    rows
}

fn check_subject(lines: &[&str], review: &Review, label: &str) -> Result<()> {
    let rows = table_rows(&section(lines, SUBJECT_HEADING));
    if rows.first().is_none_or(|r| r != &["Field", "Value"]) {
        return Err(format!("{label} Subject must be a Field | Value table"));
    }
    if rows.iter().any(|r| r.len() != 2) {
        return Err(format!("{label} Subject rows must have two cells"));
    }
    if rows[1..].iter().map(|r| r[0].as_str()).ne(SUBJECT_ROWS) {
        return Err(format!(
            "{label} Subject rows must be exactly {} in order",
            repr_list(SUBJECT_ROWS)
        ));
    }
    let expected = [
        review.repository.clone(),
        review.commit.clone(),
        review.release.clone().unwrap_or("none".into()),
        review.scope.join(", "),
        format!("{}, {}", review.name, review.credential),
        review.relationship.clone(),
        review.verdict.clone(),
        review.confidence.clone(),
        review.date.clone(),
    ];
    let differing = SUBJECT_ROWS
        .iter()
        .zip(&expected)
        .zip(&rows[1..])
        .filter(|((_, value), row)| **value != row[1])
        .map(|((field, _), _)| *field)
        .collect::<Vec<_>>();
    if !differing.is_empty() {
        return Err(format!(
            "{label} Subject rows differ from the index: {}",
            repr_list(differing)
        ));
    }
    Ok(())
}

fn check_citations(lines: &[&str], label: &str) -> Result<()> {
    let rows = table_rows(&section(lines, CITATION_HEADING));
    if rows
        .first()
        .is_none_or(|r| r != &["Claimed", "Correct", "Correction"])
    {
        return Err(format!(
            "{label} citation audit must open with the Claimed table"
        ));
    }
    let mut audited = 0;
    for row in &rows[1..] {
        if row.len() != 3 || row[0].is_empty() || !matches!(row[1].as_str(), "yes" | "no") {
            return Err(format!(
                "{label} citation row must be claimed | yes or no | correction"
            ));
        }
        if row[1] == "no" && row[2].is_empty() {
            return Err(format!(
                "{label} citation row marked no needs a correction: {}",
                row[0]
            ));
        }
        audited += 1;
    }
    if audited == 0 {
        return Err(format!("{label} citation audit has no rows"));
    }
    Ok(())
}

/// Check one verdict file and return its open questions, for the register check.
pub fn check_verdict(root: &Path, review: &Review) -> Result<Vec<String>> {
    let label = format!("verdict {}", review.id);
    let path = verify_skills::require_tracked_regular_file(root, &review.path, &label)?;
    let text = canonical_text(&crate::common::read_bytes(&path)?, &label)?;
    if sha256(text.as_bytes()) != review.sha256 {
        return Err(format!("{label} content does not match its listed sha256"));
    }
    let lines = text.split('\n').collect::<Vec<_>>();
    if lines[0] != format!("# Review verdict {}", review.id) {
        return Err(format!(
            "{label} must open with '# Review verdict {}'",
            review.id
        ));
    }
    let mut expected = vec![
        SUBJECT_HEADING,
        HEADLINE_HEADING,
        CITATION_HEADING,
        FINDINGS_HEADING,
        QUESTIONS_HEADING,
    ];
    if review.verdict == "FIX" {
        expected.push(CHANGES_HEADING);
    }
    expected.extend([METHOD_HEADING, ATTESTATION_HEADING]);
    let headings = lines
        .iter()
        .filter(|l| l.starts_with("## "))
        .copied()
        .collect::<Vec<_>>();
    if headings != expected {
        return Err(format!(
            "{label} sections must be exactly {}",
            repr_list(expected)
        ));
    }

    check_subject(&lines, review, &label)?;

    let headline = section(&lines, HEADLINE_HEADING)
        .into_iter()
        .filter(|l| !py::strip(l).is_empty())
        .collect::<Vec<_>>();
    let opens = headline.first().is_some_and(|first| {
        fancy_regex::Regex::new(&format!(r"\A{}{}", review.verdict, py::word_boundary()))
            .unwrap()
            .is_match(first)
            .unwrap_or(false)
    });
    if !opens {
        return Err(format!(
            "{label} headline must open with {}",
            review.verdict
        ));
    }
    if review.relationship == "author" && !headline.join(" ").contains("self-review") {
        return Err(format!(
            "{label} is an author verdict and its headline must say self-review"
        ));
    }

    check_citations(&lines, &label)?;

    let mut critical = 0;
    for (head, continuation) in list_section(&lines, FINDINGS_HEADING, &label)? {
        let Some(captures) = FINDING.captures(&head) else {
            return Err(format!(
                "{label} finding must open with CRITICAL., WARNING. or NOTE."
            ));
        };
        fields(
            &continuation,
            &FINDING_FIELDS,
            &format!("{label} finding '{head}'"),
        )?;
        if &captures[1] == "CRITICAL" {
            critical += 1;
        }
    }
    if review.verdict == "REJECT" && critical == 0 {
        return Err(format!("{label} is REJECT and needs a CRITICAL finding"));
    }
    if review.verdict == "ACCEPT" && critical > 0 {
        return Err(format!("{label} is ACCEPT and lists a CRITICAL finding"));
    }

    let mut questions = Vec::new();
    for (head, continuation) in list_section(&lines, QUESTIONS_HEADING, &label)? {
        if !head.ends_with('?') {
            return Err(format!(
                "{label} open question must end with a question mark: {head}"
            ));
        }
        fields(
            &continuation,
            &QUESTION_FIELDS,
            &format!("{label} question '{head}'"),
        )?;
        questions.push(head);
    }

    if review.verdict == "FIX" {
        let changes = list_section(&lines, CHANGES_HEADING, &label)?;
        if changes.is_empty() {
            return Err(format!(
                "{label} is FIX and must list at least one required change"
            ));
        }
        for (head, continuation) in changes {
            if !DEFECT.is_match(&head) {
                return Err(format!(
                    "{label} required change must open with 'Defect: ': {head}"
                ));
            }
            let found = fields(
                &continuation,
                &CHANGE_FIELDS,
                &format!("{label} change '{head}'"),
            )?;
            if !matches!(found["Re-review"].as_str(), "yes" | "no") {
                return Err(format!(
                    "{label} change '{head}' Re-review must be yes or no"
                ));
            }
        }
    }

    if section(&lines, METHOD_HEADING)
        .iter()
        .all(|l| py::strip(l).is_empty())
    {
        return Err(format!(
            "{label} Method must describe what was read and how"
        ));
    }

    let attestation = section(&lines, ATTESTATION_HEADING)
        .into_iter()
        .map(py::strip)
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>();
    if !attestation.join(" ").contains(ATTESTATION_SENTENCE) {
        return Err(format!(
            "{label} attestation must contain the fixed sentence"
        ));
    }
    if !attestation.contains(&format!("Name: {}", review.name).as_str())
        || !attestation.contains(&format!("Date: {}", review.date).as_str())
    {
        return Err(format!(
            "{label} attestation must be signed with the index name and date"
        ));
    }
    Ok(questions)
}

pub fn parse_known_unknowns(text: &str) -> Result<Vec<(String, KnownUnknown)>> {
    let mut entries: Vec<(String, String, Vec<String>)> = Vec::new();
    for line in text.split('\n') {
        if line.starts_with("## ") {
            let Some(captures) = KNOWN_UNKNOWN_HEADING.captures(line) else {
                return Err(format!(
                    "known-unknowns heading must be '## KU-NNN: question?': {line}"
                ));
            };
            if entries.iter().any(|(id, _, _)| id == &captures[1]) {
                return Err(format!("duplicate known-unknown id: {}", &captures[1]));
            }
            entries.push((captures[1].to_owned(), captures[2].to_owned(), Vec::new()));
        } else if let Some((entry_id, _, items)) = entries.last_mut() {
            if let Some(item) = line.strip_prefix("- ") {
                items.push(item.to_owned());
            } else if line.starts_with("  ") && !items.is_empty() {
                let last = items.last_mut().unwrap();
                last.push(' ');
                last.push_str(py::strip(line));
            } else if !py::strip(line).is_empty() {
                return Err(format!("{entry_id} has an unexpected line: {line}"));
            }
        }
    }

    let mut register: Vec<(String, KnownUnknown)> = Vec::new();
    for (entry_id, question, items) in entries {
        if items.len() != KNOWN_UNKNOWN_LINES.len() {
            return Err(format!(
                "{entry_id} must have exactly the lines {}",
                repr_list(KNOWN_UNKNOWN_LINES)
            ));
        }
        let mut values = Vec::new();
        for (name, item) in KNOWN_UNKNOWN_LINES.iter().zip(&items) {
            let prefix = format!("{name}: ");
            let value = item.strip_prefix(&prefix).map(py::strip);
            let Some(value) = value.filter(|v| !v.is_empty()) else {
                return Err(format!("{entry_id} line must be '{prefix}<text>': {item}"));
            };
            values.push(value.to_owned());
        }
        date(&values[0], &format!("{entry_id} Raised"))?;
        let mut resolved = None;
        if values[4] != "open" {
            let Some(captures) = RESOLVED.captures(&values[4]) else {
                return Err(format!(
                    "{entry_id} Status must be 'open' or 'resolved YYYY-MM-DD by <source>'"
                ));
            };
            resolved = Some((
                date(&captures[1], &format!("{entry_id} resolution date"))?,
                captures[2].to_owned(),
            ));
        }
        let entry = KnownUnknown {
            question,
            values,
            resolved,
        };
        // A repeated id was refused above, so this only appends.
        register.push((entry_id, entry));
    }
    Ok(register)
}

pub fn check_known_unknowns(
    register: &[(String, KnownUnknown)],
    verdict_ids: &[String],
    bound: &str,
) -> Result<()> {
    for (entry_id, entry) in register {
        require_not_after(&entry.values[0], bound, &format!("{entry_id} Raised"))?;
        let Some((resolved_on, source)) = &entry.resolved else {
            continue;
        };
        require_not_after(resolved_on, bound, &format!("{entry_id} resolution date"))?;
        if ID.is_match(source) && !verdict_ids.contains(source) {
            return Err(format!(
                "{entry_id} is resolved by an unlisted verdict: {source}"
            ));
        }
    }
    Ok(())
}

/// Index a document parse_index has already accepted by review id.
fn entries_by_id(document: &Json) -> Vec<(&str, &Json)> {
    let Some(Json::Array(entries)) = document.get("reviews") else {
        unreachable!("parse_index accepted the document")
    };
    entries
        .iter()
        .map(|e| (e.get("id").and_then(Json::as_str).unwrap(), e))
        .collect()
}

fn base_document(root: &Path, base: &str, path: &str) -> Result<Option<Vec<u8>>> {
    let out = run(root, "git", &["show", &format!("{base}:{path}")])?;
    Ok(out.status.success().then_some(out.stdout))
}

pub fn require_append_only(
    root: &Path,
    base: &str,
    current_index: &Json,
    current_register: &[(String, KnownUnknown)],
) -> Result<()> {
    require_append_only_with(
        root,
        base,
        current_index,
        current_register,
        &mut |commit, path| base_document(root, commit, path),
    )
}

/// Reads one ledger path at one commit; None where it does not exist there.
pub type LedgerReader<'a> = dyn FnMut(&str, &str) -> Result<Option<Vec<u8>>> + 'a;

/// `require_append_only`, with the reader of a ledger at a commit supplied.
pub fn require_append_only_with(
    root: &Path,
    base: &str,
    current_index: &Json,
    current_register: &[(String, KnownUnknown)],
    read: &mut LedgerReader,
) -> Result<()> {
    let commit = git_revision::resolve_commit(root, base)
        .map_err(|_| format!("base revision is not a commit in this checkout: {base}"))?;
    if let Some(raw_index) = read(&commit, INDEX_PATH)? {
        let base_index = load_index_document(&raw_index, &format!("base {base} review index"))?;
        parse_index(&base_index)?;
        let listed = entries_by_id(current_index);
        for (review_id, entry) in entries_by_id(&base_index) {
            let unchanged = listed
                .iter()
                .find(|(id, _)| *id == review_id)
                .is_some_and(|(_, current)| current.py_eq(entry));
            if !unchanged {
                return Err(format!(
                    "review {review_id} was listed at {base} and must not change or disappear"
                ));
            }
        }
    }
    let Some(raw_register) = read(&commit, KNOWN_UNKNOWNS_PATH)? else {
        return Ok(());
    };
    let base_register = parse_known_unknowns(&canonical_text(
        &raw_register,
        &format!("base {base} register"),
    )?)?;
    for (entry_id, before) in &base_register {
        let Some((_, after)) = current_register.iter().find(|(id, _)| id == entry_id) else {
            return Err(format!(
                "{entry_id} was registered at {base} and must not disappear"
            ));
        };
        if after.question != before.question || after.values[..4] != before.values[..4] {
            return Err(format!(
                "{entry_id} may change only its Status line after {base}"
            ));
        }
        if after.values[4] != before.values[4] && !(before.open() && !after.open()) {
            return Err(format!(
                "{entry_id} Status may only move from open to resolved"
            ));
        }
    }
    Ok(())
}

pub fn check_consumer(root: &Path, base: Option<&str>) -> Result<String> {
    let bound = head_date(root)?;
    let path = verify_skills::require_tracked_regular_file(root, INDEX_PATH, "review index")?;
    let document = load_index_document(&crate::common::read_bytes(&path)?, "review index")?;
    let reviews = parse_index(&document)?;
    let mut questions = Vec::new();
    for review in &reviews {
        require_not_after(&review.date, &bound, &format!("review {} date", review.id))?;
        for question in check_verdict(root, review)? {
            questions.push((review.id.clone(), question));
        }
        require_subject_commit(root, review)?;
    }
    let label = "known-unknowns register";
    let path = verify_skills::require_tracked_regular_file(root, KNOWN_UNKNOWNS_PATH, label)?;
    let register =
        parse_known_unknowns(&canonical_text(&crate::common::read_bytes(&path)?, label)?)?;
    let ids = reviews.iter().map(|r| r.id.clone()).collect::<Vec<_>>();
    check_known_unknowns(&register, &ids, &bound)?;
    for (review_id, question) in &questions {
        if !register.iter().any(|(_, e)| &e.question == question) {
            return Err(format!(
                "verdict {review_id} open question is not in {KNOWN_UNKNOWNS_PATH}: {question}"
            ));
        }
    }
    if let Some(base) = base {
        require_append_only(root, base, &document, &register)?;
    }
    let open = register.iter().filter(|(_, e)| e.open()).count();
    Ok(format!(
        "reviews valid: {} verdicts, {} known unknowns ({open} open)",
        reviews.len(),
        register.len()
    ))
}
