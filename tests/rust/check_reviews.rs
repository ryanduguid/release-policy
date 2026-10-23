//! A consumer's review index, verdict files and known-unknowns register.
//! Ported from tests/test_check_reviews.py.
mod support;

use release_policy::check_reviews::{self as reviews, check_consumer};
use serde_json::{Value, json};
use support::{Repo, cli};

const REVIEW_ID: &str = "2026-01-15-example-review";
const LATER_ID: &str = "2026-01-16-second-review";
const SIGNED: &str = "2026-01-15";
const NOTE: &str = "- NOTE. Minor wording.\n  Why it matters: readers may misread it.\n  Remedy: disclaim in the documentation.";
const WARNING: &str = "- WARNING. A defensible reading is undisclosed.\n  Why it matters: another reading exists.\n  Remedy: disclaim in the documentation.";
const CRITICAL: &str = "- CRITICAL. Wrong output for a lawful input.\n  Why it matters: money.\n  Remedy: refuse with an error.";
const QUESTION: &str = "- Does the export label the receipt date?\n  Why it matters: a sent date can read as on time.\n  Resolution path: a real header row.";
const CHANGE: &str =
    "- Defect: no year label on the output.\n  Change: add an income_year field.\n  Re-review: no";
const KNOWN_UNKNOWNS: &str = "# Known unknowns

Preamble prose that the parser ignores.

## KU-001: Does the export label the receipt date?

- Raised: 2026-01-10
- Affects: the due-date comparison
- Why it matters: a payment on the date sent can read as on time
  when the fund received it later.
- Resolution path: a header row from a real export
- Status: open

## KU-002: Which May figure fixes the rate?

- Raised: 2026-01-10
- Affects: the rate table
- Why it matters: the wrong month changes every repayment.
- Resolution path: the source series for the year
- Status: resolved 2026-01-12 by RBA F5 series, May 2024 figure
";

#[derive(Default)]
struct Render {
    questions: Option<String>,
    changes: Option<String>,
    headline: Option<String>,
}

/// The verdict file for an index entry, or None when the entry is too
/// malformed to render (the test then renders the valid entry instead).
fn render_verdict(entry: &Value, findings: &str, overrides: &Render) -> Option<String> {
    let subject = entry.get("subject")?.as_object()?;
    let reviewer = entry.get("reviewer")?.as_object()?;
    let scope = entry.get("scope")?.as_array()?;
    let text = |v: &Value| match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        other => other.to_string(),
    };
    let field = |v: Option<&Value>| v.map(text);
    let verdict = text(entry.get("verdict")?);
    let release = match subject.get("release")? {
        Value::Null => "none".to_owned(),
        Value::String(s) if s.is_empty() => "none".to_owned(),
        other => text(other),
    };
    let rows = [
        ("Repository", field(subject.get("repository"))?),
        ("Commit", field(subject.get("commit"))?),
        ("Release", release),
        (
            "Scope",
            scope.iter().map(text).collect::<Vec<_>>().join(", "),
        ),
        (
            "Reviewer",
            format!(
                "{}, {}",
                field(reviewer.get("name"))?,
                field(reviewer.get("credential"))?
            ),
        ),
        ("Relationship", field(reviewer.get("relationship"))?),
        ("Verdict", verdict.clone()),
        ("Confidence", field(entry.get("confidence"))?),
        ("Date", field(entry.get("date"))?),
    ];
    let headline = overrides.headline.clone().unwrap_or_else(|| {
        let mut line = format!("{verdict}. The encoding matches the scope.");
        if reviewer.get("relationship") == Some(&json!("author")) {
            line.push_str(" This is a self-review by the maintainer.");
        }
        line
    });
    let mut lines = vec![
        format!("# Review verdict {}", text(entry.get("id")?)),
        String::new(),
        "## Subject".into(),
        String::new(),
        "| Field | Value |".into(),
        "| --- | --- |".into(),
    ];
    lines.extend(rows.iter().map(|(k, v)| format!("| {k} | {v} |")));
    for line in [
        "",
        "## 1. Headline verdict",
        "",
        &headline,
        "",
        "## 2. Citation audit",
        "",
        "| Claimed | Correct | Correction |",
        "| --- | --- | --- |",
        "| ITAA 1997 s 328-180 | yes | |",
        "| ITAA 1997 s 328-180(2) | no | The rule is in s 328-180(3). |",
        "",
        "Provisions the subject should cite and does not:",
        "",
        "- none",
        "",
        "## 3. Findings",
        "",
        findings,
        "",
        "## 4. Open questions",
        "",
        overrides.questions.as_deref().unwrap_or(QUESTION),
        "",
    ] {
        lines.push(line.into());
    }
    if verdict == "FIX" {
        for line in [
            "## 5. Required changes",
            "",
            overrides.changes.as_deref().unwrap_or(CHANGE),
            "",
        ] {
            lines.push(line.into());
        }
    }
    for line in [
        "## 6. Method",
        "",
        "Read the brief and the source at the commit above. Two hours. No tools.",
        "",
        "## 7. Attestation",
        "",
        "I read the subject at the commit above and the materials named in",
        "section 6. The verdict is my own professional judgement.",
        "",
    ] {
        lines.push(line.into());
    }
    lines.push(format!("Name: {}", field(reviewer.get("name"))?));
    lines.push(format!("Date: {}", field(entry.get("date"))?));
    lines.push(String::new());
    Some(lines.join("\n"))
}

fn sha256(text: &str) -> String {
    release_policy::common::sha256(text.as_bytes())
}

/// A consumer checkout, mirroring the Python fixture's knobs.
struct Consumer {
    repo: Repo,
    head: String,
    verdict: &'static str,
    findings: String,
    entry_overrides: Value,
    render: Render,
    text_edits: Vec<(String, String)>,
    known_unknowns: String,
    extra_entries: Vec<Value>,
}

impl Consumer {
    fn new() -> Self {
        let repo = Repo::new();
        repo.write("README.md", "# consumer\n");
        let head = repo.commit("seed");
        repo.git(&["tag", "v1.2.3"]);
        Consumer {
            repo,
            head,
            verdict: "FIX",
            findings: WARNING.into(),
            entry_overrides: json!({}),
            render: Render::default(),
            text_edits: Vec::new(),
            known_unknowns: KNOWN_UNKNOWNS.into(),
            extra_entries: Vec::new(),
        }
    }

    fn entry(&self, id: &str, verdict: &str) -> Value {
        json!({
            "confidence": "MEDIUM",
            "date": SIGNED,
            "id": id,
            "path": format!("reviews/{id}.md"),
            "reviewer": {"credential": "Registered Tax Agent", "name": "Example Reviewer", "relationship": "independent"},
            "scope": ["ITAA_1997_s_328-180", "ITAA_1997_Subdiv_328-D"],
            "sha256": "0".repeat(64),
            "subject": {"commit": self.head, "release": "v1.2.3", "repository": "ryanduguid/example"},
            "supersedes": null,
            "verdict": verdict,
        })
    }

    /// Write the verdict file for `entry` and return the entry with its digest.
    fn listed(&self, entry: &Value, text: &str) -> Value {
        let mut text = text.to_owned();
        for (old, new) in &self.text_edits {
            text = text.replace(old, new);
        }
        self.repo
            .write(entry["path"].as_str().unwrap_or("reviews/x.md"), &text);
        let mut listed = entry.clone();
        listed["sha256"] = json!(sha256(&text));
        listed
    }

    fn install_with(&self, index: Option<&[u8]>) {
        let entry = self.entry(REVIEW_ID, self.verdict);
        let mut overridden = entry.clone();
        for (key, value) in self.entry_overrides.as_object().unwrap() {
            overridden[key] = value.clone();
        }
        let text = render_verdict(&overridden, &self.findings, &self.render)
            .or_else(|| render_verdict(&entry, &self.findings, &self.render))
            .unwrap();
        let mut listed = self.listed(&overridden, &text);
        if let Some(sha) = self.entry_overrides.get("sha256") {
            listed["sha256"] = sha.clone();
        }
        let mut entries = vec![listed];
        entries.extend(self.extra_entries.iter().cloned());
        let document = json!({"reviews": entries, "schema": 1});
        let canonical = format!("{}\n", python_dumps(&document));
        self.repo
            .write("reviews/index.json", index.unwrap_or(canonical.as_bytes()));
        self.repo
            .write("docs/known-unknowns.md", &self.known_unknowns);
        self.repo.git(&["add", "reviews", "docs"]);
    }

    fn install(&self) {
        self.install_with(None)
    }

    fn check(&self) -> Result<String, String> {
        check_consumer(self.repo.root(), None)
    }

    fn refused(&self, fragment: &str) {
        self.refused_at(fragment, None)
    }

    fn refused_at(&self, fragment: &str, base: Option<&str>) {
        let error = check_consumer(self.repo.root(), base).unwrap_err();
        assert!(error.contains(fragment), "{error:?} lacks {fragment:?}");
    }
}

/// `json.dumps(value, indent=2, sort_keys=True)`.
fn python_dumps(value: &Value) -> String {
    let parsed = release_policy::pyjson::loads(&value.to_string()).unwrap();
    release_policy::pyjson::dumps(
        &parsed,
        release_policy::pyjson::Dump {
            indent: Some(2),
            sort_keys: true,
            ensure_ascii: true,
        },
    )
}

#[test]
fn accepts_a_complete_consumer_and_reports_counts() {
    let c = Consumer::new();
    c.install();
    assert_eq!(
        c.check().unwrap(),
        "reviews valid: 1 verdicts, 2 known unknowns (1 open)"
    );
    let root = c.repo.root().to_string_lossy().into_owned();
    let out = cli(c.repo.root(), &["check-reviews", "--root", &root], "");
    assert_eq!(out.code, 0);
    assert!(out.stdout.contains("reviews valid"));
}

#[test]
fn main_reports_the_first_failure_and_exits_one() {
    let c = Consumer::new();
    c.install_with(Some(b"{}\n"));
    let root = c.repo.root().to_string_lossy().into_owned();
    let out = cli(c.repo.root(), &["check-reviews", "--root", &root], "");
    assert_eq!(out.code, 1);
    assert!(
        out.stderr
            .contains("check_reviews: review index must contain exactly")
    );
}

#[test]
fn the_script_runs_as_a_command() {
    let c = Consumer::new();
    c.install();
    let out = cli(c.repo.root(), &["check_reviews"], "");
    assert_eq!(out.code, 0);
    assert!(out.stdout.contains("reviews valid: 1 verdicts"));
}

#[test]
fn a_checkout_without_commits_cannot_date_the_ledgers() {
    let empty = Repo::new();
    let error = check_consumer(empty.root(), None).unwrap_err();
    assert!(error.contains("no commit to date"));
}

#[test]
fn crlf_verdicts_digest_as_lf_and_other_encodings_are_refused() {
    let c = Consumer::new();
    c.install();
    let path = c.repo.path(&format!("reviews/{REVIEW_ID}.md"));
    let lf = std::fs::read(&path).unwrap();
    std::fs::write(
        &path,
        String::from_utf8(lf.clone()).unwrap().replace('\n', "\r\n"),
    )
    .unwrap();
    c.check().unwrap();
    let text = String::from_utf8(lf.clone()).unwrap();
    for (raw, fragment) in [
        (
            [b"\xef\xbb\xbf".as_slice(), &lf].concat(),
            "must not contain a UTF-8 BOM",
        ),
        ([lf.as_slice(), b"\xff"].concat(), "must be valid UTF-8"),
        (
            text.replace("Subject", "Sub\rject").into_bytes(),
            "lone carriage return",
        ),
        (
            [lf.as_slice(), b"\n"].concat(),
            "does not match its listed sha256",
        ),
    ] {
        std::fs::write(&path, raw).unwrap();
        c.refused(fragment);
    }
}

#[test]
fn index_must_be_tracked_canonical_json() {
    let c = Consumer::new();
    for (raw, fragment) in [
        (
            b"\xef\xbb\xbf{}\n".as_slice(),
            "must not contain a UTF-8 BOM",
        ),
        (b"{\n", "must be valid UTF-8 JSON"),
        (br#"{"schema": 1, "reviews": []}"#, "canonical sorted JSON"),
        (
            b"{\n  \"reviews\": [],\n  \"schema\": 2\n}\n",
            "schema must be 1",
        ),
        (
            b"{\n  \"reviews\": {},\n  \"schema\": 1\n}\n",
            "reviews must be a list",
        ),
        (
            b"{\n  \"reviews\": [\n    1\n  ],\n  \"schema\": 1\n}\n",
            "must contain exactly",
        ),
    ] {
        c.install_with(Some(raw));
        c.refused(fragment);
    }
    c.install();
    c.repo
        .git(&["rm", "--cached", "--quiet", "reviews/index.json"]);
    c.refused("not one tracked Git entry");
}

#[test]
fn entry_fields_are_validated() {
    let mut c = Consumer::new();
    let head = c.head.clone();
    let cases = [
        (
            json!({"id": "Example"}),
            "id must be YYYY-MM-DD-hyphenated-words".to_owned(),
        ),
        (json!({"id": ""}), "id must be a non-empty string".into()),
        (
            json!({"id": "2026-02-30-example"}),
            "id date prefix must be an ISO 8601 calendar date".into(),
        ),
        (
            json!({"path": "reviews/other.md"}),
            format!("path must be reviews/{REVIEW_ID}.md"),
        ),
        (
            json!({"sha256": "0".repeat(63)}),
            "sha256 must be 64 lower-case hex digits".into(),
        ),
        (
            json!({"subject": {"commit": head}}),
            "subject must contain exactly".into(),
        ),
        (
            json!({"subject": {"commit": head, "release": null, "repository": "nested/a/b"}}),
            "subject.repository must be owner/name".into(),
        ),
        (
            json!({"subject": {"commit": "abc", "release": null, "repository": "o/n"}}),
            "subject.commit must be a full lower-case commit SHA".into(),
        ),
        (
            json!({"subject": {"commit": head, "release": "1.2.3", "repository": "o/n"}}),
            "subject.release must be a release tag".into(),
        ),
        (
            json!({"scope": []}),
            "scope must be a non-empty list".into(),
        ),
        (
            json!({"scope": "ITAA_1997_s_1"}),
            "scope must be a non-empty list".into(),
        ),
        (
            json!({"scope": ["ITAA 1997 s 1"]}),
            "scope[0] must be an identifier".into(),
        ),
        (
            json!({"scope": ["ITAA_1997_s_1", "ITAA_1997_s_1"]}),
            "scope repeats an identifier".into(),
        ),
        (
            json!({"reviewer": {"name": "A"}}),
            "reviewer must contain exactly".into(),
        ),
        (
            json!({"reviewer": {"credential": "CPA", "name": "", "relationship": "author"}}),
            "reviewer.name must be a non-empty string".into(),
        ),
        (
            json!({"reviewer": {"credential": "", "name": "A", "relationship": "author"}}),
            "reviewer.credential must be a non-empty string".into(),
        ),
        (
            json!({"reviewer": {"credential": "CPA", "name": "A", "relationship": "tool"}}),
            "reviewer.relationship must be one of".into(),
        ),
        (json!({"verdict": "MAYBE"}), "verdict must be one of".into()),
        (
            json!({"confidence": "SURE"}),
            "confidence must be one of".into(),
        ),
        (
            json!({"date": "15 January 2026"}),
            "date must be an ISO 8601 calendar date".into(),
        ),
        (
            json!({"date": "20260115"}),
            "date must be an ISO 8601 calendar date".into(),
        ),
        (
            json!({"date": "2999-01-01"}),
            "date 2999-01-01 is later than the checked commit date".into(),
        ),
        (
            json!({"supersedes": "2026-01-01-unknown"}),
            "supersedes must name an earlier listed review".into(),
        ),
    ];
    for (overrides, fragment) in cases {
        c.entry_overrides = overrides;
        c.install();
        c.refused(&fragment);
    }
}

#[test]
fn scope_grammar_admits_statutes_standards_and_nested_provisions() {
    let mut c = Consumer::new();
    c.entry_overrides = json!({"scope": [
        "SGAA_1992_s_18C(1)(c)(i)", "APES_110_para_R112.1", "TPB_PG_01-2023", "TAA_1953_s_284-75",
    ]});
    c.install();
    c.check().unwrap();
}

#[test]
fn subject_commit_and_release_are_resolved_in_git() {
    let mut c = Consumer::new();
    let orphan = c.repo.git(&["commit-tree", "HEAD^{tree}", "-m", "orphan"]);
    c.repo.git(&["tag", "elsewhere/v2.0.0", &orphan]);
    let head = c.head.clone();
    for (subject, fragment) in [
        (
            json!({"commit": "1".repeat(40), "release": null, "repository": "o/n"}),
            "not present in this checkout",
        ),
        (
            json!({"commit": orphan, "release": null, "repository": "o/n"}),
            "not an ancestor of the checked-out",
        ),
        (
            json!({"commit": head, "release": "v9.9.9", "repository": "o/n"}),
            "is not present in this checkout; fetch tags",
        ),
        (
            json!({"commit": head, "release": "elsewhere/v2.0.0", "repository": "o/n"}),
            "points at another commit",
        ),
    ] {
        c.entry_overrides = json!({"subject": subject});
        c.install();
        c.refused(fragment);
    }
    c.entry_overrides = json!({"subject": {"commit": head, "release": null, "repository": "o/n"}});
    c.install();
    c.check().unwrap();
}

fn later_entry(c: &Consumer) -> Value {
    let mut later = c.entry(LATER_ID, "ACCEPT");
    later["supersedes"] = json!(REVIEW_ID);
    let text = render_verdict(&later, NOTE, &Render::default()).unwrap();
    c.listed(&later, &text)
}

#[test]
fn duplicate_unsorted_and_superseding_entries() {
    let mut c = Consumer::new();
    let later = later_entry(&c);
    c.extra_entries = vec![later.clone()];
    c.install();
    assert!(c.check().unwrap().starts_with("reviews valid: 2 "));

    let mut duplicate = later.clone();
    duplicate["id"] = json!(REVIEW_ID);
    duplicate["path"] = json!(format!("reviews/{REVIEW_ID}.md"));
    c.extra_entries = vec![duplicate];
    c.install();
    c.refused("duplicate review id");

    let mut earlier = later;
    earlier["id"] = json!("2026-01-01-earlier");
    earlier["path"] = json!("reviews/2026-01-01-earlier.md");
    earlier["supersedes"] = Value::Null;
    c.extra_entries = vec![earlier];
    c.install();
    c.refused("must be sorted by id");
}

#[test]
fn verdict_file_structure_is_enforced() {
    let mut c = Consumer::new();
    let date_rows = format!("| Date | 2026-01-01 |\n| Date | {SIGNED} |");
    let cases = [
        (
            format!("# Review verdict {REVIEW_ID}"),
            "# Verdict".to_owned(),
            "must open with",
        ),
        (
            "## 6. Method".into(),
            "## Method".into(),
            "sections must be exactly",
        ),
        (
            "## 7. Attestation".into(),
            "## 7. Attestation\n\n## 8. Extra".into(),
            "sections must be exactly",
        ),
        (
            "| Field | Value |".into(),
            "| Name | Value |".into(),
            "Subject must be a Field",
        ),
        (
            "| Verdict | FIX |".into(),
            "| Verdict | FIX | extra |".into(),
            "Subject rows must have two cells",
        ),
        (
            "| Confidence | MEDIUM |".into(),
            "| Confidence | HIGH |".into(),
            "Subject rows differ from the index: ['Confidence']",
        ),
        (
            format!("| Date | {SIGNED} |"),
            date_rows,
            "Subject rows must be exactly",
        ),
        (
            "FIX. The encoding".into(),
            "ACCEPT. The encoding".into(),
            "headline must open with FIX",
        ),
        (
            "FIX. The encoding matches the scope.".into(),
            String::new(),
            "headline must open with FIX",
        ),
        (
            "| Claimed | Correct | Correction |".into(),
            "| Section | Correct | Note |".into(),
            "must open with the Claimed table",
        ),
        (
            "| yes | |".into(),
            "| maybe | |".into(),
            "citation row must be claimed",
        ),
        (
            "| no | The rule is in s 328-180(3). |".into(),
            "| no | |".into(),
            "marked no needs a correction",
        ),
        (
            "- WARNING. A defensible".into(),
            "- Soft. A defensible".into(),
            "finding must open with",
        ),
        (
            "  Remedy: disclaim in the documentation.".into(),
            "  Remedy:".into(),
            "needs a non-empty 'Remedy:' line",
        ),
        (
            "  Why it matters: another reading exists.".into(),
            "  stray sentence".into(),
            "text before its first named line",
        ),
        (
            "- Does the export label the receipt date?".into(),
            "- Does the export label the receipt date".into(),
            "must end with a question mark",
        ),
        (
            "  Resolution path: a real header row.".into(),
            String::new(),
            "needs a non-empty 'Resolution path:' line",
        ),
        (
            "- Does the export label the receipt date?".into(),
            "- Is the rate ever rounded?".into(),
            "open question is not in docs/known-unknowns.md: Is the rate ever rounded",
        ),
        (
            "- Defect: no year label on the output.".into(),
            "- No year label on the output.".into(),
            "required change must open with 'Defect: '",
        ),
        (
            "- Defect: no year label on the output.".into(),
            "- Defect: no year label on the output.\nstray".into(),
            "line outside any list item",
        ),
        (
            "  Re-review: no".into(),
            "  Re-review: later".into(),
            "Re-review must be yes or no",
        ),
        (
            "Read the brief and the source at the commit above. Two hours. No tools.".into(),
            String::new(),
            "Method must describe",
        ),
        (
            "I read the subject at the commit above".into(),
            "I skimmed the subject at the commit above".into(),
            "must contain the fixed sentence",
        ),
        (
            "Name: Example Reviewer".into(),
            "Name: Someone Else".into(),
            "signed with the index name and date",
        ),
    ];
    for (old, new, fragment) in cases {
        c.text_edits = vec![(old, new)];
        c.install();
        c.refused(fragment);
    }
}

#[test]
fn citation_audit_needs_rows() {
    let mut c = Consumer::new();
    c.text_edits = vec![
        ("| ITAA 1997 s 328-180 | yes | |\n".into(), String::new()),
        (
            "| ITAA 1997 s 328-180(2) | no | The rule is in s 328-180(3). |\n".into(),
            String::new(),
        ),
    ];
    c.install();
    c.refused("citation audit has no rows");
}

#[test]
fn list_sections_take_items_or_none() {
    let mut c = Consumer::new();
    c.verdict = "ACCEPT";
    c.findings = "none".into();
    c.render.questions = Some("none".into());
    c.install();
    c.check().unwrap();
    c.render.questions = Some(String::new());
    c.install();
    c.refused("Open questions must list items or say none");
    c.verdict = "FIX";
    c.findings = WARNING.into();
    c.render.questions = None;
    c.render.changes = Some("none".into());
    c.install();
    c.refused("is FIX and must list at least one required change");
}

#[test]
fn fix_verdict_needs_section_five_and_others_must_not_have_it() {
    let mut c = Consumer::new();
    c.verdict = "ACCEPT";
    c.findings = NOTE.into();
    c.text_edits = vec![(
        "## 6. Method".into(),
        "## 5. Required changes\n\n- x\n\n## 6. Method".into(),
    )];
    c.install();
    c.refused("sections must be exactly");
    c.verdict = "FIX";
    c.findings = WARNING.into();
    c.text_edits = vec![("## 5. Required changes".into(), "## 5. Changes".into())];
    c.install();
    c.refused("sections must be exactly");
}

#[test]
fn severity_rules_follow_the_verdict() {
    let mut c = Consumer::new();
    for (verdict, findings, fragment) in [
        ("REJECT", WARNING, "needs a CRITICAL finding"),
        ("ACCEPT", CRITICAL, "lists a CRITICAL finding"),
        ("REJECT", CRITICAL, ""),
        ("ACCEPT", NOTE, ""),
        ("FIX", CRITICAL, ""),
    ] {
        c.verdict = verdict;
        c.findings = findings.into();
        c.install();
        if fragment.is_empty() {
            c.check().unwrap();
        } else {
            c.refused(fragment);
        }
    }
}

#[test]
fn author_verdicts_must_say_self_review() {
    let mut c = Consumer::new();
    c.entry_overrides = json!({"reviewer": {"credential": "CPA", "name": "Example Reviewer", "relationship": "author"}});
    c.install();
    c.check().unwrap();
    c.render.headline = Some("FIX. The encoding matches the scope.".into());
    c.install();
    c.refused("author verdict and its headline must say self-review");
}

#[test]
fn known_unknowns_register_is_validated() {
    let mut c = Consumer::new();
    let entry = KNOWN_UNKNOWNS.split("## KU-002").next().unwrap().to_owned();
    let tail = entry.splitn(3, "\n\n").nth(2).unwrap().to_owned();
    let cases = [
        (
            "## KU-1: Short?\n\n- Raised: 2026-01-10\n".to_owned(),
            "heading must be",
        ),
        ("## KU-003: No question mark\n".into(), "heading must be"),
        (format!("{entry}{tail}"), "duplicate known-unknown id"),
        (
            format!("{entry}## KU-003: Q?\n\nstray prose\n"),
            "has an unexpected line",
        ),
        (
            format!("{entry}## KU-003: Q?\n\n- Raised: 2026-01-10\n"),
            "must have exactly the lines",
        ),
        (
            entry.replace("- Affects: the due-date comparison", "- Affect: x"),
            "line must be 'Affects: <text>'",
        ),
        (
            entry.replace("- Affects: the due-date comparison", "- Affects: "),
            "line must be 'Affects: <text>'",
        ),
        (
            entry.replace("- Raised: 2026-01-10", "- Raised: yesterday"),
            "Raised must be an ISO 8601",
        ),
        (
            entry.replace("- Raised: 2026-01-10", "- Raised: 2999-01-01"),
            "Raised 2999-01-01 is later than",
        ),
        (
            entry.replace("- Status: open", "- Status: closed"),
            "Status must be 'open' or",
        ),
        (
            entry.replace("- Status: open", "- Status: resolved 2026-13-01 by x"),
            "resolution date must be",
        ),
        (
            entry.replace("- Status: open", "- Status: resolved 2999-01-01 by x"),
            "resolution date 2999-01-01 is later",
        ),
        (
            entry.replace(
                "- Status: open",
                "- Status: resolved 2026-01-12 by 2026-01-01-missing",
            ),
            "resolved by an unlisted verdict",
        ),
    ];
    for (text, fragment) in cases {
        c.known_unknowns = text;
        c.install();
        c.refused(fragment);
    }
}

#[test]
fn known_unknowns_may_resolve_by_a_listed_verdict_or_a_citation() {
    let mut c = Consumer::new();
    c.known_unknowns = KNOWN_UNKNOWNS.replace(
        "- Status: open",
        &format!("- Status: resolved 2026-01-15 by {REVIEW_ID}"),
    );
    c.install();
    assert_eq!(
        c.check().unwrap(),
        "reviews valid: 1 verdicts, 2 known unknowns (0 open)"
    );
    c.known_unknowns = "# Known unknowns\n\nNo entries yet.\n".into();
    c.render.questions = Some("none".into());
    c.install();
    assert!(c.check().unwrap().ends_with("0 known unknowns (0 open)"));
    c.repo
        .write("docs/known-unknowns.md", b"\xef\xbb\xbf# Known unknowns\n");
    c.refused("register must not contain a UTF-8 BOM");
}

#[test]
fn base_revision_holds_the_ledgers_append_only() {
    let mut c = Consumer::new();
    let later = later_entry(&c);
    c.extra_entries = vec![later.clone()];
    c.install();
    c.repo.git(&["commit", "--quiet", "-m", "ledgers"]);

    c.refused_at("base revision is not a commit", Some("no-such-ref"));
    for base in ["--help", "--format=%ct", "HEAD^{tree}"] {
        c.refused_at("base revision is not a commit", Some(base));
    }
    let head = c.head.clone();
    check_consumer(c.repo.root(), Some(&head)).unwrap();
    check_consumer(c.repo.root(), Some("HEAD")).unwrap();

    c.extra_entries = vec![];
    c.install();
    c.refused_at(
        &format!("review {LATER_ID} was listed at HEAD and must not change or disappear"),
        Some("HEAD"),
    );

    c.extra_entries = vec![later];
    c.entry_overrides = json!({"confidence": "HIGH"});
    c.install();
    c.refused_at(
        &format!("review {REVIEW_ID} was listed at HEAD"),
        Some("HEAD"),
    );
    c.entry_overrides = json!({});

    for (text, fragment) in [
        (
            KNOWN_UNKNOWNS.split("## KU-002").next().unwrap().to_owned(),
            "KU-002 was registered at HEAD and must not disappear",
        ),
        (
            KNOWN_UNKNOWNS.replace("- Affects: the rate table", "- Affects: the rates"),
            "KU-002 may change only its Status line",
        ),
        (
            KNOWN_UNKNOWNS.replace(
                "- Status: resolved 2026-01-12 by RBA F5 series, May 2024 figure",
                "- Status: open",
            ),
            "KU-002 Status may only move from open to resolved",
        ),
        (
            KNOWN_UNKNOWNS.replace(
                "- Status: open",
                "- Status: resolved 2026-01-14 by ATO guidance",
            ),
            "",
        ),
    ] {
        c.known_unknowns = text;
        c.install();
        if fragment.is_empty() {
            check_consumer(c.repo.root(), Some("HEAD")).unwrap();
        } else {
            c.refused_at(fragment, Some("HEAD"));
        }
    }
}

#[test]
fn base_revision_without_a_register_checks_only_the_index() {
    let c = Consumer::new();
    c.install();
    c.repo
        .git(&["rm", "--cached", "--quiet", "docs/known-unknowns.md"]);
    c.repo.git(&["commit", "--quiet", "-m", "index only"]);
    c.install();
    check_consumer(c.repo.root(), Some("HEAD")).unwrap();
}

#[test]
fn both_historical_ledgers_use_the_same_resolved_commit() {
    let c = Consumer::new();
    c.install();
    c.repo.git(&["commit", "--quiet", "-m", "ledgers"]);
    let base = c.repo.git(&["rev-parse", "HEAD"]);
    c.repo.git(&["update-ref", "refs/heads/moving", &base]);
    let index = reviews::load_index_document(
        &std::fs::read(c.repo.path("reviews/index.json")).unwrap(),
        "index",
    )
    .unwrap();
    let register = reviews::parse_known_unknowns(
        &std::fs::read_to_string(c.repo.path("docs/known-unknowns.md")).unwrap(),
    )
    .unwrap();
    let mut observed = Vec::new();
    reviews::require_append_only_with(
        c.repo.root(),
        "moving",
        &index,
        &register,
        &mut |commit, path| {
            observed.push(commit.to_owned());
            let _ = std::process::Command::new("git")
                .args(["update-ref", "-d", "refs/heads/moving"])
                .current_dir(c.repo.root())
                .status();
            let out = std::process::Command::new("git")
                .args(["show", &format!("{commit}:{path}")])
                .current_dir(c.repo.root())
                .output()
                .unwrap();
            Ok(out.status.success().then_some(out.stdout))
        },
    )
    .unwrap();
    assert_eq!(observed, [base.clone(), base]);
}

#[test]
fn a_required_change_needs_its_change_line() {
    let mut c = Consumer::new();
    c.text_edits = vec![(
        "  Change: add an income_year field.\n".into(),
        String::new(),
    )];
    c.install();
    c.refused("change 'Defect: no year label on the output.' needs a non-empty 'Change:' line");
}

/// The current ledgers, read from the working tree as `check_consumer` reads them.
fn current_ledgers(
    c: &Consumer,
) -> (
    release_policy::pyjson::Json,
    Vec<(String, reviews::KnownUnknown)>,
) {
    let index = reviews::load_index_document(
        &std::fs::read(c.repo.path(reviews::INDEX_PATH)).unwrap(),
        "index",
    )
    .unwrap();
    let register = reviews::parse_known_unknowns(
        &std::fs::read_to_string(c.repo.path(reviews::KNOWN_UNKNOWNS_PATH)).unwrap(),
    )
    .unwrap();
    (index, register)
}

#[test]
fn a_base_register_must_be_canonical_text() {
    let c = Consumer::new();
    c.install();
    let (index, register) = current_ledgers(&c);
    let error = reviews::require_append_only_with(
        c.repo.root(),
        "HEAD",
        &index,
        &register,
        &mut |_, path| {
            Ok((path == reviews::KNOWN_UNKNOWNS_PATH)
                .then(|| [b"\xef\xbb\xbf".as_slice(), KNOWN_UNKNOWNS.as_bytes()].concat()))
        },
    )
    .unwrap_err();
    assert_eq!(error, "base HEAD register must not contain a UTF-8 BOM");
}

#[test]
#[should_panic(expected = "parse_index accepted the document")]
fn the_append_only_check_needs_an_index_parse_index_accepted() {
    let c = Consumer::new();
    c.install();
    let (_, register) = current_ledgers(&c);
    let unchecked = release_policy::pyjson::loads("{}").unwrap();
    let raw_index = std::fs::read(c.repo.path(reviews::INDEX_PATH)).unwrap();
    let _ = reviews::require_append_only_with(
        c.repo.root(),
        "HEAD",
        &unchecked,
        &register,
        &mut |_, path| Ok((path == reviews::INDEX_PATH).then(|| raw_index.clone())),
    );
}

#[cfg(unix)]
#[test]
fn undecodable_git_output_fails_each_subject_check_closed() {
    use std::os::unix::fs::PermissionsExt;
    let c = Consumer::new();
    c.install();
    let real = std::env::split_paths(&std::env::var_os("PATH").unwrap())
        .map(|dir| dir.join("git"))
        .find(|candidate| candidate.is_file())
        .unwrap();
    // A git that answers the call named in FAKE_GIT_UNDECODABLE with a byte
    // UTF-8 cannot decode, and passes every other call to the real git.
    let bin = tempfile::tempdir().unwrap();
    let script = bin.path().join("git");
    std::fs::write(
        &script,
        format!(
            "#!/bin/sh\ncase \"$*\" in\n  \"$FAKE_GIT_UNDECODABLE\"*) printf '\\377'; exit 0 ;;\nesac\nexec '{}' \"$@\"\n",
            real.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let path = std::env::join_paths(
        std::iter::once(bin.path().to_path_buf())
            .chain(std::env::split_paths(&std::env::var_os("PATH").unwrap())),
    )
    .unwrap();
    let path = path.to_string_lossy();
    let run = |call: &str| {
        support::cli_env(
            c.repo.root(),
            &["check-reviews"],
            "",
            &[("PATH", &path), ("FAKE_GIT_UNDECODABLE", call)],
        )
    };
    let passing = run("no such call");
    assert_eq!(passing.code, 0, "{}", passing.stderr);
    for call in [
        "cat-file -e",
        "merge-base --is-ancestor",
        "rev-parse --verify --quiet",
    ] {
        let out = run(call);
        assert_eq!(out.code, 1, "{call}");
        assert_eq!(
            out.stderr,
            "check_reviews: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte\n",
            "{call}"
        );
    }
}
