//! The publication egress check. Ported from tests/test_egress.py.
mod support;

use release_policy::egress;
use std::path::{Path, PathBuf};
use support::{Output, cli};

/// A publication candidate in a temporary directory; commands run inside it.
struct Candidate {
    dir: tempfile::TempDir,
}

impl Candidate {
    fn new(files: &[(&str, &str)]) -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("c")).unwrap();
        let candidate = Candidate { dir };
        for (name, text) in files {
            candidate.write(name, text);
        }
        candidate
    }

    /// The candidate root; the terms file lives beside it, outside the candidate.
    fn root(&self) -> PathBuf {
        self.dir.path().join("c")
    }

    fn write(&self, name: &str, text: impl AsRef<[u8]>) -> PathBuf {
        let path = self.root().join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, text).unwrap();
        path
    }

    fn terms(&self, text: &str) -> String {
        let path = self.dir.path().join("terms.txt");
        std::fs::write(&path, text).unwrap();
        path.to_string_lossy().into_owned()
    }

    fn run(&self, args: &[&str]) -> Output {
        let mut all = vec!["check-egress"];
        all.extend(args);
        cli(&self.root(), &all, "")
    }

    fn here(&self) -> String {
        self.root().to_string_lossy().into_owned()
    }
}

// LayerATests

#[test]
fn a_configured_term_is_a_finding_and_the_term_is_not_printed() {
    let c = Candidate::new(&[("README.md", "Prepared for Northwind Holdings in June.\n")]);
    let terms = c.terms("# policy\nNorthwind Holdings\n");
    let readme = c.root().join("README.md").to_string_lossy().into_owned();
    let out = c.run(&[&readme, "--terms", &terms]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("[A] configured term at "));
    assert!(out.stdout.contains("terms.txt:2 (match redacted)"));
    assert!(out.stdout.contains("README.md:1"));
    assert!(!out.stdout.contains("Northwind"));
    assert!(!out.stdout.contains("Prepared for"));
    assert!(out.stdout.contains("do not publish"));
}

#[test]
fn a_term_is_found_whatever_its_case() {
    let c = Candidate::new(&[("notes.md", "NORTHWIND holdings pty ltd\n")]);
    let terms = c.terms("Northwind Holdings\n");
    let out = c.run(&[&c.here(), "--terms", &terms]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("[A]"));
}

#[test]
fn a_near_miss_is_not_a_finding() {
    let c = Candidate::new(&[("notes.md", "Northwinds are common on this coast.\n")]);
    let terms = c.terms("Northwind Holdings\n");
    let out = c.run(&[&c.here(), "--terms", &terms]);
    assert_eq!(out.code, 0);
    assert!(out.stdout.contains("RESULT: clean"));
}

#[test]
fn comments_and_blank_lines_in_the_terms_file_are_not_terms() {
    let c = Candidate::new(&[("notes.md", "nothing to see\n")]);
    let terms = c.terms("# a comment\n\n  \nAcme\n");
    let loaded = egress::load_terms(Path::new(&terms)).unwrap();
    assert_eq!(
        loaded
            .iter()
            .map(|t| (t.line, t.text.as_str()))
            .collect::<Vec<_>>(),
        [(4, "Acme")]
    );
    assert_eq!(c.run(&[&c.here(), "--terms", &terms]).code, 0);
}

#[test]
fn no_part_of_the_matched_line_reaches_the_output() {
    let line = format!(
        "{} Northwind Holdings {}",
        "filler ".repeat(40),
        "trailing ".repeat(40)
    );
    let c = Candidate::new(&[("notes.md", &format!("{line}\n"))]);
    let terms = c.terms("Northwind Holdings\n");
    let out = c.run(&[&c.here(), "--terms", &terms]);
    let printed = out.stdout.lines().find(|row| row.contains("[A]")).unwrap();
    assert!(printed.len() < line.len());
    for fragment in ["filler", "trailing", "Northwind"] {
        assert!(!printed.contains(fragment));
    }
}

// LayerBTests

#[test]
fn structural_shapes_are_findings_by_default() {
    for text in [
        r"see C:\Users\ryan\Documents\notes.md",
        "see /home/ryan/notes.md for the working copy",
        "the runner answered on 10.1.2.3 overnight",
        "the printer sits at 192.168.0.14",
        "open http://localhost:8080 to see it",
        "resolved through reports.internal all week",
        "staged into s3://client-exports-2026 first",
        "copy the key out of .env before running",
        r"mapped from \\fileserver\payroll last quarter",
    ] {
        let c = Candidate::new(&[("notes.md", &format!("{text}\n"))]);
        let out = c.run(&[&c.here(), "--no-terms"]);
        assert_eq!(out.code, 1, "{text}: {}", out.stdout);
        assert!(out.stdout.contains("[B]"));
    }
}

#[test]
fn ordinary_prose_does_not_trip_the_structural_layer() {
    let text = "The service is published at https://example.com/reports.\n\
                Version 10.1.2 shipped on Tuesday and 192.168 is not in it.\n\
                The environment is documented in the deployment guide.\n\
                Local development happens on the developer's own machine.\n";
    let c = Candidate::new(&[("notes.md", text)]);
    let out = c.run(&[&c.here(), "--no-terms"]);
    assert_eq!(out.code, 0, "{}", out.stdout);
}

#[test]
fn allow_structural_reports_without_failing() {
    let c = Candidate::new(&[("notes.md", "open http://localhost:8080 to see it\n")]);
    let out = c.run(&[&c.here(), "--no-terms", "--allow-structural"]);
    assert_eq!(out.code, 0);
    assert!(out.stdout.contains("[B]"));
    assert!(out.stdout.contains("waved through"));
}

#[test]
fn allow_structural_does_not_wave_through_the_other_layers() {
    let c = Candidate::new(&[("notes.md", "x-internal-ledger: 4\n")]);
    let out = c.run(&[&c.here(), "--no-terms", "--allow-structural"]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("[C]"));
}

// LayerCTests

#[test]
fn the_reserved_namespace_is_a_finding() {
    let c = Candidate::new(&[("meta.yaml", "x-internal-cost-centre: 1182\n")]);
    let out = c.run(&[&c.here(), "--no-terms"]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("[C] reserved namespace"));
}

#[test]
fn a_similar_key_that_is_not_the_namespace_is_not_a_finding() {
    let c = Candidate::new(&[("meta.yaml", "x_internal_note: fine\nexternal-id: 4\n")]);
    assert_eq!(c.run(&[&c.here(), "--no-terms"]).code, 0);
}

// ExitStatusTests

#[test]
fn a_clean_candidate_exits_0() {
    let c = Candidate::new(&[("README.md", "Nothing private here.\n")]);
    let terms = c.terms("Northwind Holdings\n");
    let out = c.run(&[&c.here(), "--terms", &terms]);
    assert_eq!(out.code, 0);
    assert!(out.stdout.contains("RESULT: clean"));
}

#[test]
fn a_missing_terms_file_exits_2_not_1() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    let absent = c.root().join("absent.txt").to_string_lossy().into_owned();
    let out = c.run(&[&c.here(), "--terms", &absent]);
    assert_eq!(out.code, 2);
    assert!(out.stderr.contains("EGRESS CHECK ERROR"));
    assert!(out.stdout.contains("unchecked, not clean"));
}

#[test]
fn an_empty_terms_file_exits_2() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    let terms = c.terms("# everything was deleted\n");
    let out = c.run(&[&c.here(), "--terms", &terms]);
    assert_eq!(out.code, 2);
    assert!(out.stderr.contains("lists no terms"));
}

#[test]
fn a_missing_candidate_exits_2() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    let absent = c.root().join("absent").to_string_lossy().into_owned();
    let out = c.run(&[&absent, "--no-terms"]);
    assert_eq!(out.code, 2);
    assert!(out.stderr.contains("does not exist"));
}

#[test]
fn running_without_a_terms_decision_exits_2() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    let out = c.run(&[&c.here()]);
    assert_eq!(out.code, 2);
    assert!(out.stderr.contains("layer A needs a terms file"));
}

#[test]
fn contradictory_terms_arguments_exit_2() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    let terms = c.terms("Acme\n");
    let out = c.run(&[&c.here(), "--terms", &terms, "--no-terms"]);
    assert_eq!(out.code, 2);
    assert!(out.stderr.contains("contradict"));
}

#[test]
fn a_pattern_that_does_not_compile_exits_2() {
    let error =
        egress::scan_text("text", "x.md", &[], "the terms file", &[("broken", "(")]).unwrap_err();
    assert!(
        error.contains("structural pattern 'broken' does not compile"),
        "{error}"
    );
}

#[test]
fn the_three_statuses_are_distinct() {
    let c = Candidate::new(&[("README.md", "clean\n"), ("bad.md", "x-internal-a\n")]);
    let clean = c.root().join("README.md").to_string_lossy().into_owned();
    let codes = [
        c.run(&[&clean, "--no-terms"]).code,
        c.run(&[&c.here(), "--no-terms"]).code,
        c.run(&[&c.here()]).code,
    ];
    assert_eq!(codes, [0, 1, 2]);
}

// CandidateTests

#[test]
fn a_directory_is_walked_and_a_file_is_taken_as_given() {
    let c = Candidate::new(&[("a.md", "one\n"), ("nested/b.md", "two\n")]);
    let walked = egress::candidate_files(&[c.root()]).unwrap();
    let names = walked
        .iter()
        .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    assert_eq!(names, ["a.md", "b.md"]);
    assert_eq!(
        egress::candidate_files(&[c.root().join("a.md")]).unwrap(),
        [c.root().join("a.md")]
    );
}

#[test]
fn a_file_that_cannot_be_read_as_text_is_not_cleared() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    c.write("logo.png", b"\x89PNG\r\n\x1a\n binary");
    let out = c.run(&[&c.here(), "--no-terms"]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("not scanned as text: "));
    assert!(out.stdout.contains("logo.png"));
    assert!(
        out.stdout
            .contains("unchecked, so this candidate is not cleared")
    );
}

#[test]
fn undecodable_text_is_reported_rather_than_crashing() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    c.write("broken.md", b"\xff\xfe not utf-8");
    let out = c.run(&[&c.here(), "--no-terms"]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("broken.md"));
}

#[test]
fn allow_unscanned_clears_them_once_a_person_has_looked() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    c.write("logo.png", b"\x89PNG\r\n\x1a\n binary");
    let out = c.run(&[&c.here(), "--no-terms", "--allow-unscanned"]);
    assert_eq!(out.code, 0);
    assert!(out.stdout.contains("not scanned as text: "));
    assert!(out.stdout.contains("waved through by --allow-unscanned"));
}

#[test]
fn allow_unscanned_does_not_wave_through_a_finding() {
    let c = Candidate::new(&[("notes.md", "x-internal-ledger: 4\n")]);
    c.write("logo.png", b"\x89PNG binary");
    let out = c.run(&[&c.here(), "--no-terms", "--allow-unscanned"]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("[C]"));
}

// EntryPointTests

#[test]
fn an_unreadable_candidate_exits_2_rather_than_reporting_it_clean() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    let path = c.root().join("README.md");
    // Hold the file so the check cannot read it.
    #[cfg(windows)]
    let _lock = {
        use std::os::windows::fs::OpenOptionsExt;
        std::fs::OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(&path)
            .unwrap()
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o0)).unwrap();
        if std::fs::read(&path).is_ok() {
            return; // Running as root reads anything.
        }
    }
    let out = c.run(&[&c.here(), "--no-terms"]);
    assert_eq!(out.code, 2);
    assert!(out.stderr.contains("cannot read the publication candidate"));
    assert!(out.stdout.contains("unchecked, not clean"));
}

#[test]
fn the_script_runs_as_a_command() {
    let c = Candidate::new(&[("meta.yaml", "x-internal-cost-centre: 1182\n")]);
    let out = c.run(&[&c.here(), "--no-terms"]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("[C] reserved namespace"));
}

// ExclusionTests

#[test]
fn an_excluded_file_is_printed_and_not_scanned() {
    let c = Candidate::new(&[("docs/guide.md", "the reserved prefix is x-internal-\n")]);
    let out = c.run(&["docs", "--no-terms", "--exclude", "docs/guide.md"]);
    assert_eq!(out.code, 0, "{}", out.stdout);
    assert!(
        out.stdout
            .contains("excluded by --exclude, not checked: docs/guide.md")
    );
    assert!(!out.stdout.contains("[C]"));
}

#[test]
fn an_exclusion_does_not_cover_its_neighbours() {
    let c = Candidate::new(&[
        ("docs/guide.md", "the reserved prefix is x-internal-\n"),
        ("docs/other.md", "x-internal-ledger: 4\n"),
    ]);
    let out = c.run(&["docs", "--no-terms", "--exclude", "docs/guide.md"]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("docs/other.md"));
    assert!(out.stdout.contains("[C]"));
}

#[test]
fn an_exclusion_is_the_path_not_the_file_name() {
    let c = Candidate::new(&[
        ("docs/guide.md", "the reserved prefix is x-internal-\n"),
        ("docs/nested/guide.md", "x-internal-ledger: 4\n"),
    ]);
    let out = c.run(&["docs", "--no-terms", "--exclude", "docs/guide.md"]);
    assert_eq!(out.code, 1);
    assert!(out.stdout.contains("docs/nested/guide.md"));
    assert!(out.stdout.contains("[C]"));
}

#[test]
fn a_pattern_that_matches_nothing_exits_2() {
    let c = Candidate::new(&[("README.md", "clean\n")]);
    let out = c.run(&[".", "--no-terms", "--exclude", "gone.md"]);
    assert_eq!(out.code, 2);
    assert!(
        out.stderr
            .contains("matched no file in the candidate: gone.md")
    );
    assert!(out.stdout.contains("unchecked, not clean"));
}

#[test]
fn a_glob_excludes_a_family_of_files() {
    let c = Candidate::new(&[
        ("docs/a.md", "x-internal-one\n"),
        ("docs/b.md", "x-internal-two\n"),
    ]);
    let out = c.run(&["docs", "--no-terms", "--exclude", "docs/*.md"]);
    assert_eq!(out.code, 0, "{}", out.stdout);
    assert_eq!(out.stdout.matches("excluded by --exclude").count(), 2);
}

#[test]
fn this_repository_can_check_its_own_published_prose() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let out = cli(
        root,
        &[
            "check-egress",
            "README.md",
            "RELEASE_NOTES.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "AGENTS.md",
            "docs",
            "--no-terms",
            "--allow-structural",
            "--exclude",
            "docs/egress-check.md",
        ],
        "",
    );
    assert_eq!(out.code, 0, "{}{}", out.stdout, out.stderr);
    assert!(
        out.stdout
            .contains("excluded by --exclude, not checked: docs/egress-check.md")
    );
}

// PrivateAddressTests

#[test]
fn each_private_range_is_a_finding() {
    for address in [
        "10.1.2.3",
        "172.16.0.1",
        "172.24.8.9",
        "172.31.255.254",
        "192.168.0.14",
        "127.0.0.1",
    ] {
        let c = Candidate::new(&[("notes.md", &format!("host {address} here\n"))]);
        let out = c.run(&[".", "--no-terms"]);
        assert_eq!(out.code, 1, "{address}: {}", out.stdout);
        assert!(out.stdout.contains("[B] private ipv4"));
    }
}

#[test]
fn public_addresses_either_side_of_the_range_are_not_findings() {
    let c = Candidate::new(&[(
        "notes.md",
        "edge hosts 172.15.0.1 and 172.32.0.1 are public\n",
    )]);
    let out = c.run(&[".", "--no-terms"]);
    assert_eq!(out.code, 0, "{}", out.stdout);
}

// LineScanTests

#[test]
fn a_million_character_line_is_scanned() {
    let padding = "x".repeat(1_000_000);
    for (label, text, expected) in [
        ("clean", format!("{padding}\n"), 0),
        ("finding", format!("{padding} host 10.1.2.3\n"), 1),
    ] {
        let c = Candidate::new(&[("notes.md", &text)]);
        let out = c.run(&[".", "--no-terms"]);
        assert_eq!(out.code, expected, "{label}: {}{}", out.stdout, out.stderr);
    }
}

#[test]
fn a_word_boundary_follows_the_unicode_word_class() {
    // A combining mark is not a word character; a superscript digit is.
    for (text, expected) in [
        ("box.internal\u{301}\n", 1),
        ("\u{301}box.internal\n", 1),
        ("\u{b2}box.internal\n", 0),
        ("10.0.0.1\u{b2}\n", 0),
        ("\u{345}.env\n", 1),
    ] {
        let c = Candidate::new(&[("notes.md", text)]);
        let out = c.run(&[".", "--no-terms"]);
        assert_eq!(out.code, expected, "{text:?}: {}{}", out.stdout, out.stderr);
    }
}
