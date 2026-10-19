//! Consumers re-pin only the families a release-policy change moves.
//! Ported from tests/test_pin_families.py.
mod support;

use release_policy::common::git;
use release_policy::pin_families::{self, FAMILIES, USAGE};
use std::collections::BTreeSet;
use std::path::Path;
use support::{Repo, cli};

const FILES: [(&str, &str); 13] = [
    (
        ".github/workflows/release-python.yml",
        "run: . policy/scripts/gates.sh\n",
    ),
    (
        ".github/workflows/release-archive.yml",
        "run: . policy/scripts/gates.sh\nuses: ./.github/workflows/publish-archives.yml\n",
    ),
    (
        ".github/workflows/publish-archives.yml",
        "run: policy/scripts/publish_archives.sh\n",
    ),
    (
        ".github/workflows/release-skills.yml",
        "uses: ./.github/workflows/publish-archives.yml\nuses: ./.github/workflows/verify-skills.yml\n",
    ),
    (
        ".github/workflows/verify-skills.yml",
        "run: python policy/scripts/verify_skills.py verify\n",
    ),
    (
        ".github/workflows/attribution-policy.yml",
        "uses: ./.github/actions/no-ai-attribution\n",
    ),
    (
        ".github/actions/no-ai-attribution/action.yml",
        "runs: scan\n",
    ),
    (
        "scripts/gates.sh",
        "\"$PYTHON\" \"$GATES_DIR/python_release.py\" tag\n",
    ),
    ("scripts/python_release.py", "print('release')\n"),
    (
        "scripts/publish_archives.sh",
        "# see release-python.yml\n. \"$(dirname \"$0\")/publish_common.sh\"\n",
    ),
    ("scripts/publish_common.sh", "true\n"),
    ("scripts/verify_skills.py", "print('verified')\n"),
    ("docs/guide.md", "prose\n"),
];

fn file(name: &str) -> &'static str {
    FILES.iter().find(|(n, _)| *n == name).unwrap().1
}

fn fixture() -> (Repo, String) {
    let repo = Repo::new();
    for (name, text) in FILES {
        repo.write(name, text);
    }
    let head = repo.commit("fixture");
    (repo, head)
}

fn set(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

/// The families a commit of `files` on top of HEAD moves.
fn moved(repo: &Repo, files: &[(&str, &str)]) -> BTreeSet<String> {
    let before = repo.git(&["rev-parse", "HEAD"]);
    for (name, text) in files {
        repo.write(name, text);
    }
    let after = repo.commit("change");
    pin_families::changed(&before, &after, repo.root())
        .unwrap()
        .into_iter()
        .filter(|(_, paths)| !paths.is_empty())
        .map(|(family, _)| family)
        .collect()
}

/// This checkout, whose real workflows the last checks read.
fn checkout() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
}

#[test]
fn families_follow_workflows_actions_and_scripts() {
    let (repo, head) = fixture();
    let inputs = |entry| pin_families::inputs(entry, &head, repo.root()).unwrap();
    assert_eq!(
        inputs(".github/workflows/release-python.yml"),
        set(&[
            ".github/workflows/release-python.yml",
            "scripts/gates.sh",
            "scripts/python_release.py"
        ])
    );
    // The comment naming release-python.yml does not tie the archive family to it.
    assert_eq!(
        inputs(".github/workflows/release-archive.yml"),
        set(&[
            ".github/workflows/release-archive.yml",
            ".github/workflows/publish-archives.yml",
            "scripts/gates.sh",
            "scripts/python_release.py",
            "scripts/publish_archives.sh",
            "scripts/publish_common.sh",
        ])
    );
    assert_eq!(
        inputs(".github/workflows/verify-skills.yml"),
        set(&[
            ".github/workflows/verify-skills.yml",
            "scripts/verify_skills.py"
        ])
    );
}

#[test]
fn option_revisions_fail_without_a_keep_pin_verdict() {
    let (repo, _) = fixture();
    for revision in ["--help", "--format=%ct", "missing", "HEAD^{tree}"] {
        assert!(
            pin_families::changed(revision, "HEAD", repo.root()).is_err(),
            "{revision}"
        );
        assert!(
            pin_families::inputs(FAMILIES[0].1, revision, repo.root()).is_err(),
            "{revision}"
        );
    }
    let out = cli(repo.root(), &["pin-families", "--help", "--help"], "");
    assert_ne!(out.code, 0);
    assert!(!out.stdout.contains("keep the pin"));
}

#[test]
fn ref_is_resolved_before_tree_and_diff_operations() {
    let (repo, before) = fixture();
    repo.write("scripts/python_release.py", "print('new')\n");
    let after = repo.commit("new");
    repo.git(&["update-ref", "refs/heads/moving", &before]);
    let mut observed: Vec<Vec<String>> = Vec::new();
    let result = pin_families::changed_with("moving", &after, repo.root(), &mut |args| {
        observed.push(args.iter().map(|a| (*a).to_owned()).collect());
        // Moving the ref mid-run must not change what is compared.
        repo.git(&["update-ref", "refs/heads/moving", &after]);
        git(repo.root(), args)
    })
    .unwrap();
    assert!(!result[0].1.is_empty());
    assert!(observed.iter().flatten().all(|a| !a.contains("moving")));
}

#[test]
fn verifier_changes_move_both_skill_families() {
    let workflow = format!(
        "{}name: updated verifier\n",
        file(".github/workflows/verify-skills.yml")
    );
    for change in [
        (".github/workflows/verify-skills.yml", workflow.as_str()),
        (
            "scripts/verify_skills.py",
            "print('verification changed')\n",
        ),
    ] {
        let (repo, _) = fixture();
        assert_eq!(
            moved(&repo, &[change]),
            set(&["release-skills", "verify-skills"])
        );
    }
}

#[test]
fn imported_policy_helpers_move_the_families_that_execute_them() {
    let (repo, _) = fixture();
    repo.write(
        "scripts/python_release.py",
        "from git_revision import resolve_commit\n",
    );
    repo.write(
        "scripts/git_revision.py",
        "def resolve_commit(): return 'first'\n",
    );
    repo.commit("helpers");
    assert_eq!(
        moved(
            &repo,
            &[(
                "scripts/git_revision.py",
                "def resolve_commit(): return 'second'\n"
            )]
        ),
        set(&["release-python", "release-archive"])
    );
}

#[test]
fn release_only_changes_preserve_the_verifier_pin() {
    let (repo, _) = fixture();
    let path = ".github/workflows/release-skills.yml";
    let text = format!("{}name: updated release\n", file(path));
    assert_eq!(moved(&repo, &[(path, &text)]), set(&["release-skills"]));
}

#[test]
fn real_skill_families_share_the_verifier_inputs() {
    let verifier = pin_families::inputs(FAMILIES[3].1, "HEAD", checkout()).unwrap();
    let release = pin_families::inputs(FAMILIES[2].1, "HEAD", checkout()).unwrap();
    assert_eq!(
        verifier,
        set(&[
            ".github/workflows/verify-skills.yml",
            "scripts/verify_skills.py"
        ])
    );
    assert!(verifier.is_subset(&release) && verifier != release);
}

#[test]
fn only_the_moved_families_are_named() {
    let (repo, _) = fixture();
    assert_eq!(
        moved(
            &repo,
            &[(
                ".github/actions/no-ai-attribution/action.yml",
                "runs: scan2\n"
            )]
        ),
        set(&["attribution"])
    );
    // A script two steps from the workflow still moves its family.
    assert_eq!(
        moved(
            &repo,
            &[("scripts/python_release.py", "print('changed')\n")]
        ),
        set(&["release-python", "release-archive"])
    );
    assert_eq!(
        moved(&repo, &[("scripts/publish_common.sh", "false\n")]),
        set(&["release-archive", "release-skills"])
    );
    assert_eq!(moved(&repo, &[("docs/guide.md", "more prose\n")]), set(&[]));
}

#[test]
fn a_family_absent_from_both_commits_moves_nothing() {
    let (repo, _) = fixture();
    repo.git(&["rm", "-q", ".github/workflows/release-skills.yml"]);
    repo.git(&["commit", "-q", "-m", "retire the skills family"]);
    assert_eq!(moved(&repo, &[("docs/guide.md", "more prose\n")]), set(&[]));
}

#[test]
fn the_real_release_python_family_is_the_audited_set() {
    let found =
        pin_families::inputs(".github/workflows/release-python.yml", "HEAD", checkout()).unwrap();
    for path in [
        ".github/workflows/release-python.yml",
        "scripts/gates.sh",
        "scripts/required_checks.py",
        "scripts/python_release.py",
        "scripts/publish_python.sh",
        "scripts/publish_common.sh",
    ] {
        assert!(found.contains(path), "{path}: {found:?}");
    }
    assert!(!found.contains(".github/workflows/attribution-policy.yml"));
}

#[test]
fn prints_one_verdict_per_family() {
    let moved = vec![
        ("release-python".to_owned(), vec![]),
        (
            "attribution".to_owned(),
            vec![".github/actions/no-ai-attribution/action.yml".to_owned()],
        ),
    ];
    assert_eq!(
        pin_families::report(&moved),
        "release-python: keep the pin\nattribution: re-pin (.github/actions/no-ai-attribution/action.yml)\n"
    );
    let (repo, _) = fixture();
    let out = cli(repo.root(), &["pin-families", "old"], "");
    assert_eq!(out.code, 2);
    assert_eq!(out.stderr, format!("{USAGE}\n"));
}

#[test]
fn runs_as_a_command() {
    let out = cli(checkout(), &["pin-families", "HEAD", "HEAD"], "");
    assert_eq!(out.code, 0);
    let expected: String = FAMILIES
        .iter()
        .map(|(family, _)| format!("{family}: keep the pin\n"))
        .collect();
    assert_eq!(out.stdout, expected);
}
