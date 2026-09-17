from __future__ import annotations

import io
import runpy
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import required_checks

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "required_checks.py"

REPO = "ryanduguid/example"
SHA = "a" * 40
OTHER_SHA = "b" * 40
CI = ".github/workflows/ci.yml"
BOUNDARIES = ".github/workflows/boundaries.yml"
LIST = f"{CI}: lint\n{CI}: payday-super-checker / test (3.12)\n{BOUNDARIES}: boundaries\n"


def run(run_id: int, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": run_id,
        "path": CI,
        "head_sha": SHA,
        "event": "push",
        "head_branch": "main",
        "status": "completed",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
    }
    base.update(overrides)
    return base


def job(job_id: int, name: str, conclusion: str = "success", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": job_id,
        "name": name,
        "head_sha": SHA,
        "status": "completed",
        "conclusion": conclusion,
    }
    base.update(overrides)
    return base


class FakeGitHub:
    """Serves fabricated runs and jobs and records every endpoint requested."""

    def __init__(
        self,
        runs: list[dict[str, object]],
        jobs: dict[int, list[dict[str, object]]],
        page_size: int = 100,
    ) -> None:
        self.runs = runs
        self.jobs = jobs
        self.page_size = page_size
        self.requests: list[str] = []
        # Set to a page number to simulate a run appearing mid-pagination.
        self.insert_before_page: int | None = None
        # Whether that shift settles, so a re-read succeeds.
        self.settle = True

    def __call__(self, endpoint: str) -> object:
        self.requests.append(endpoint)
        page = int(endpoint.rsplit("page=", 1)[1])
        start = (page - 1) * self.page_size
        if "/actions/runs?" in endpoint:
            runs = self.runs
            if self.insert_before_page is not None and page >= self.insert_before_page:
                # A run started while the pages were being read, shifting the
                # rest down by one, exactly as offset pagination allows.
                runs = [run(9999)] + runs
                self.insert_before_page = None if self.settle else self.insert_before_page
            return {
                "workflow_runs": runs[start : start + self.page_size],
                "total_count": len(runs),
            }
        run_id = int(endpoint.split("/actions/runs/")[1].split("/")[0])
        found = self.jobs[run_id]
        return {"jobs": found[start : start + self.page_size], "total_count": len(found)}


def complete_success() -> FakeGitHub:
    return FakeGitHub(
        [run(10), run(11, path=BOUNDARIES)],
        {
            10: [job(1, "lint"), job(2, "payday-super-checker / test (3.12)")],
            11: [job(3, "boundaries")],
        },
    )


class ParseTests(unittest.TestCase):
    def test_parses_paths_and_job_names_and_ignores_comments(self) -> None:
        checks = required_checks.parse_required_checks(f"# comment\n\n{LIST}")
        self.assertEqual(
            [check.label for check in checks],
            [f"{CI}: lint", f"{CI}: payday-super-checker / test (3.12)", f"{BOUNDARIES}: boundaries"],
        )

    def test_rejects_malformed_lines(self) -> None:
        for text in (
            "lint",
            f"{CI}:lint",
            f"{CI}: ",
            "docs/ci.yml: lint",
            f"{CI}: lint\n{CI}: lint\n",
            "",
            "# only a comment\n",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                required_checks.parse_required_checks(text)


class TrustedRunTests(unittest.TestCase):
    def test_keeps_only_push_and_dispatch_runs_of_this_repository_at_the_exact_commit(self) -> None:
        """These runs are real and readable; the policy is what excludes them."""
        runs = [
            run(1),
            run(2, event="workflow_dispatch"),
            run(3, head_sha=OTHER_SHA),
            run(4, event="pull_request"),
            run(5, head_branch="feature"),
            run(6, repository={"full_name": "someone/else"}),
            run(7, head_repository={"full_name": "fork/example"}),
            run(12, repository="someone/else"),
        ]
        github = FakeGitHub(runs, {})
        self.assertEqual(
            required_checks.trusted_runs(github, REPO, SHA),
            {CI: (required_checks.Run(2, "completed"), required_checks.Run(1, "completed"))},
        )

    def test_a_run_that_cannot_be_read_refuses_the_listing(self) -> None:
        """Dropping it would leave the listing looking complete without it.

        The entry it hides could be the run whose mandatory check failed, so
        an unreadable entry is a refusal rather than an exclusion.
        """
        for broken, expected in (
            ("not a run", "no numeric id"),
            ({"id": None}, "no numeric id"),
            (run(9, id="9"), "no numeric id"),
            (run(8, path=None), "no readable path or id"),
            (run(8, path=123), "no readable path or id"),
        ):
            with self.subTest(broken=broken):
                github = FakeGitHub([run(1), broken], {})  # type: ignore[list-item]
                with self.assertRaisesRegex(RuntimeError, expected):
                    required_checks.trusted_runs(github, REPO, SHA)

    def test_repository_names_compare_case_insensitively(self) -> None:
        github = FakeGitHub([run(1, repository={"full_name": "RyanDuguid/Example"})], {})
        self.assertEqual(
            required_checks.trusted_runs(github, REPO, SHA),
            {CI: (required_checks.Run(1, "completed"),)},
        )

    def test_every_run_of_a_workflow_is_kept_and_ordered_for_readable_messages(self) -> None:
        """No run is discarded: the check has to hold in all of them, in any listing order."""
        for order in ([run(9), run(3)], [run(3), run(9)]):
            with self.subTest(order=[entry["id"] for entry in order]):
                github = FakeGitHub(order, {})
                self.assertEqual(
                    required_checks.trusted_runs(github, REPO, SHA),
                    {CI: (required_checks.Run(9, "completed"), required_checks.Run(3, "completed"))},
                )

    def test_pagination_follows_full_pages_and_stops_on_a_short_one(self) -> None:
        """A run that must agree can sit on any page, so every page has to be read."""
        github = FakeGitHub([run(index) for index in range(1, 202)], {})
        found = required_checks.trusted_runs(github, REPO, SHA)[CI]
        self.assertEqual(len(found), 201)
        self.assertEqual(found[0], required_checks.Run(201, "completed"))
        self.assertEqual(
            [request.rsplit("page=", 1)[1] for request in github.requests], ["1", "2", "3"]
        )

    def test_a_listing_that_shifts_mid_read_is_read_again(self) -> None:
        """A run inserted while paginating pushes another off the page boundary."""
        github = FakeGitHub([run(index) for index in range(1, 201)], {})
        github.insert_before_page = 2
        found = required_checks.trusted_runs(github, REPO, SHA)[CI]
        self.assertEqual(len(found), 200)
        self.assertEqual({entry.run_id for entry in found}, set(range(1, 201)))

    def test_a_listing_that_keeps_shifting_fails_closed(self) -> None:
        """Never judge an incomplete set: a skipped run could be the failing one."""
        github = FakeGitHub([run(index) for index in range(1, 201)], {})
        github.insert_before_page = 2
        github.settle = False
        with self.assertRaisesRegex(RuntimeError, "the listing changed while it was read"):
            required_checks.trusted_runs(github, REPO, SHA)

    def test_a_repeated_entry_is_detected_even_within_one_page(self) -> None:
        github = FakeGitHub([run(1), run(1)], {})
        with self.assertRaisesRegex(RuntimeError, "the listing changed while it was read"):
            required_checks.trusted_runs(github, REPO, SHA)

    def test_a_count_that_disagrees_with_the_entries_fails_closed(self) -> None:
        def short(_endpoint: str) -> object:
            return {"workflow_runs": [run(1)], "total_count": 2}

        with self.assertRaisesRegex(RuntimeError, "the listing changed while it was read"):
            required_checks.trusted_runs(short, REPO, SHA)

    def test_a_malformed_listing_is_an_error_not_an_empty_result(self) -> None:
        for payload in ({"workflow_runs": "x"}, [], {}):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                required_checks.trusted_runs(lambda _endpoint: payload, REPO, SHA)


class RunJobTests(unittest.TestCase):
    def test_keeps_the_newest_job_per_name_for_the_exact_commit(self) -> None:
        github = FakeGitHub(
            [],
            {
                10: [
                    job(1, "lint", "failure"),
                    job(2, "lint"),
                    job(3, "other", head_sha=OTHER_SHA),
                    job(6, "quiet", status=None, conclusion=None),
                    job(0, "lint", "cancelled"),
                ]
            },
        )
        jobs = required_checks.run_jobs(github, REPO, 10, SHA)
        self.assertEqual(
            jobs,
            {
                "lint": required_checks.Job(10, 2, "completed", "success"),
                "quiet": required_checks.Job(10, 6, "", ""),
            },
        )

    def test_a_job_that_cannot_be_read_refuses_the_listing(self) -> None:
        """The name it lacks could be the mandatory check that failed."""
        for broken, expected in (
            ({"id": 5}, "no readable name or id"),
            (job(4, "no id", id=None), "no numeric id"),
            (job(4, 7), "no readable name or id"),
        ):
            with self.subTest(broken=broken):
                github = FakeGitHub([], {10: [job(1, "lint"), broken]})
                with self.assertRaisesRegex(RuntimeError, expected):
                    required_checks.run_jobs(github, REPO, 10, SHA)

    def test_the_gh_wrapper_returns_json_and_fails_on_errors(self) -> None:
        ok = subprocess.CompletedProcess([], 0, stdout='{"jobs": []}', stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok):
            self.assertEqual(required_checks._gh_json("x"), {"jobs": []})
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 404")
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                required_checks._gh_json("x")
        silent = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with mock.patch.object(subprocess, "run", return_value=silent):
            with self.assertRaisesRegex(RuntimeError, "gh api failed"):
                required_checks._gh_json("x")
        garbled = subprocess.CompletedProcess([], 0, stdout="not json", stderr="")
        with mock.patch.object(subprocess, "run", return_value=garbled):
            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                required_checks._gh_json("x")


class EvaluateTests(unittest.TestCase):
    def check(self, github: FakeGitHub, text: str = LIST, **options: object) -> required_checks.Verdict:
        return required_checks.check(
            required_checks.parse_required_checks(text),
            repository=REPO,
            commit=SHA,
            fetch_json=github,
            **options,  # type: ignore[arg-type]
        )

    def test_complete_success_for_the_exact_commit_passes(self) -> None:
        verdict = self.check(complete_success())
        self.assertTrue(verdict.ok)
        self.assertEqual(len(verdict.passed), 3)
        self.assertIn(f"{CI}: lint: run 10 job 1", verdict.passed)

    def test_a_missing_check_fails(self) -> None:
        github = FakeGitHub([run(10)], {10: [job(1, "lint")]})
        verdict = self.check(github)
        self.assertFalse(verdict.ok)
        self.assertEqual(len(verdict.failed), 2)
        self.assertIn("completed without reporting this check", verdict.failed[0])
        self.assertIn("no push or workflow_dispatch run", verdict.failed[1])

    def test_a_pending_check_fails_closed_when_the_wait_ends(self) -> None:
        github = complete_success()
        github.jobs[10][0] = job(1, "lint", conclusion=None, status="in_progress")
        verdict = self.check(github, wait_seconds=0)
        self.assertEqual(len(verdict.pending), 1)
        self.assertIn("is in_progress", verdict.pending[0])
        self.assertFalse(verdict.ok)

    def test_failed_cancelled_and_skipped_conclusions_are_not_success(self) -> None:
        for conclusion in ("failure", "cancelled", "skipped", "neutral", "timed_out"):
            with self.subTest(conclusion=conclusion):
                github = complete_success()
                github.jobs[10][1] = job(2, "payday-super-checker / test (3.12)", conclusion)
                verdict = self.check(github)
                self.assertEqual(len(verdict.failed), 1)
                self.assertIn(f"concluded {conclusion!r}", verdict.failed[0])

    def test_a_success_for_the_wrong_commit_does_not_count(self) -> None:
        github = complete_success()
        github.runs[0] = run(10, head_sha=OTHER_SHA)
        self.assertEqual(len(self.check(github).failed), 2)
        self.assertIn("no push or workflow_dispatch run", self.check(github).failed[0])
        github = complete_success()
        github.jobs[10][0] = job(1, "lint", head_sha=OTHER_SHA)
        self.assertEqual(len(self.check(github).failed), 1)

    def test_a_success_under_the_wrong_workflow_identity_does_not_count(self) -> None:
        github = complete_success()
        github.runs[1] = run(11, path=".github/workflows/impostor.yml")
        verdict = self.check(github)
        self.assertEqual(len(verdict.failed), 1)
        self.assertIn(f"{BOUNDARIES}: boundaries", verdict.failed[0])
        github = complete_success()
        github.runs[1] = run(11, path=BOUNDARIES, event="pull_request")
        self.assertEqual(len(self.check(github).failed), 1)

    def test_a_failing_run_refuses_even_though_another_run_succeeded(self) -> None:
        """Run 10 passed both CI checks; run 12 cancelled one and never reported the other."""
        github = complete_success()
        github.runs.append(run(12, event="workflow_dispatch"))
        github.jobs[12] = [job(9, "lint", "cancelled")]
        verdict = self.check(github)
        self.assertEqual(len(verdict.failed), 2)
        self.assertIn("run 12 job 9 concluded 'cancelled'", verdict.failed[0])
        self.assertIn("run 12 completed without reporting this check", verdict.failed[1])

    def test_a_failing_rerun_of_an_older_run_refuses(self) -> None:
        """A re-run keeps its run id and adds an attempt, so it is never the highest id.

        Run 10 was re-run after run 12 succeeded, and its latest attempt failed.
        Any rule that picked one run by recency would authorise on run 12.
        """
        github = complete_success()
        github.runs.append(run(12, event="workflow_dispatch"))
        github.jobs[12] = [job(20, "lint"), job(21, "payday-super-checker / test (3.12)")]
        github.jobs[10] = [job(1, "lint", "failure"), job(2, "payday-super-checker / test (3.12)")]
        verdict = self.check(github)
        self.assertEqual(len(verdict.failed), 1)
        self.assertIn("run 10 job 1 concluded 'failure'", verdict.failed[0])
        self.assertFalse(verdict.ok)

    def test_a_newer_run_that_never_reported_the_check_refuses_an_older_success(self) -> None:
        """The bypass: run 12 was cancelled before the job existed, run 10 had passed it.

        Judging the check from whichever run happens to contain it lets that
        older success authorise the release, which is exactly what naming the
        newest run is supposed to prevent.
        """
        github = complete_success()
        github.runs.append(run(12, conclusion="cancelled"))
        github.jobs[12] = [job(9, "payday-super-checker / test (3.12)")]
        verdict = self.check(github)
        self.assertEqual(len(verdict.failed), 1)
        self.assertIn("run 12 completed without reporting this check", verdict.failed[0])
        self.assertIn(f"{CI}: lint", verdict.failed[0])

    def test_a_newer_run_still_in_progress_is_pending_not_missing(self) -> None:
        """A job GitHub has not created yet is not a refusal, and must be waited for."""
        github = complete_success()
        github.runs.append(run(12, status="in_progress"))
        github.jobs[12] = []
        verdict = self.check(github, wait_seconds=0)
        self.assertEqual(verdict.failed, ())
        self.assertEqual(len(verdict.pending), 2)
        self.assertIn("run 12 is in_progress and has not reported this check yet", verdict.pending[0])

    def test_a_pending_run_holds_the_check_even_though_another_run_passed(self) -> None:
        """Run 10 passed, so only the unfinished run 12 keeps the check waiting."""
        github = complete_success()
        github.runs.append(run(12, status="queued"))
        github.jobs[12] = []
        verdict = self.check(github, wait_seconds=0)
        self.assertEqual(verdict.failed, ())
        self.assertEqual(len(verdict.pending), 2)
        self.assertEqual(len(verdict.passed), 1)

    def test_a_workflow_with_no_trusted_run_at_all_fails(self) -> None:
        github = FakeGitHub([run(10)], {10: [job(1, "lint")]})
        verdict = self.check(github, text=f"{BOUNDARIES}: boundaries\n")
        self.assertEqual(len(verdict.failed), 1)
        self.assertIn("no push or workflow_dispatch run", verdict.failed[0])

    def test_only_the_named_skipped_checks_matter(self) -> None:
        """A path-skipped sibling engine never blocks a release that did not name it."""
        github = complete_success()
        github.jobs[10].append(job(7, "the-wip-tally / test (3.12)", "skipped"))
        self.assertTrue(self.check(github).ok)

    def test_a_bounded_wait_polls_then_accepts_a_late_success(self) -> None:
        github = complete_success()
        github.jobs[10][0] = job(1, "lint", conclusion=None, status="queued")
        ticks = iter([0.0, 0.0, 30.0, 30.0, 60.0])
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 2:
                github.jobs[10][0] = job(1, "lint")

        verdict = self.check(
            github, wait_seconds=120, poll_seconds=30, clock=lambda: next(ticks), sleep=sleep
        )
        self.assertTrue(verdict.ok)
        self.assertEqual(sleeps, [30.0, 30.0])

    def test_a_bounded_wait_times_out_and_fails_closed(self) -> None:
        github = complete_success()
        github.jobs[10][0] = job(1, "lint", conclusion=None, status="queued")
        ticks = iter([0.0, 0.0, 0.0, 50.0, 50.0, 70.0])
        sleeps: list[float] = []
        verdict = self.check(
            github, wait_seconds=60, poll_seconds=30, clock=lambda: next(ticks), sleep=sleeps.append
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(sleeps, [30.0, 10.0])
        self.assertEqual(len(verdict.pending), 1)

    def test_an_api_error_propagates_instead_of_passing(self) -> None:
        def broken(_endpoint: str) -> object:
            raise RuntimeError("HTTP 500")

        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            self.check(broken)  # type: ignore[arg-type]


class ConsumerGuideTests(unittest.TestCase):
    """The caller must grant what the gate needs, because a called workflow cannot.

    A reusable workflow's `permissions:` block is a ceiling it may lower, never a
    grant it may raise. The `actions: read` the release workflows declare for the
    gate therefore does nothing unless the calling job grants it too, and a
    caller that omits it stops at the gate instead of releasing.
    """

    DOCS = Path(__file__).resolve().parents[1] / "docs"
    CALLS_RELEASE = "uses: ryanduguid/release-policy/.github/workflows/release-"

    def caller_grants(self, guide: str) -> list[tuple[int, bool]]:
        """For each documented release caller, whether its job grants Actions read.

        Walks back from the `uses:` line to that job's `permissions:` block, so
        the answer comes from the example a consumer would copy.
        """
        lines = (self.DOCS / guide).read_text(encoding="utf-8").splitlines()
        results = []
        for index, line in enumerate(lines):
            if self.CALLS_RELEASE not in line:
                continue
            granted = False
            for previous in reversed(lines[:index]):
                stripped = previous.strip()
                if stripped == "permissions:":
                    break
                if stripped.endswith(":") and not stripped.startswith("-") and ": " not in stripped:
                    break  # reached the job header without meeting a permissions block
                if stripped == "actions: read":
                    granted = True
            results.append((index + 1, granted))
        return results

    def guides_with_release_callers(self) -> list[str]:
        """Every guide that shows a release call, not a hand-listed few.

        The PyPI guide was missed when `required-checks` became mandatory
        precisely because the check named its guides instead of finding them.
        """
        found = [
            path.name
            for path in sorted(self.DOCS.glob("*.md"))
            if self.CALLS_RELEASE in path.read_text(encoding="utf-8")
        ]
        self.assertIn("python-consumers.md", found)
        self.assertIn("pypi-publishing.md", found)
        return found

    def test_every_documented_release_caller_grants_actions_read(self) -> None:
        for guide in self.guides_with_release_callers():
            callers = self.caller_grants(guide)
            with self.subTest(guide=guide):
                self.assertTrue(callers, "no release caller example found")
            for line_number, granted in callers:
                with self.subTest(guide=guide, line=line_number):
                    self.assertTrue(granted, "this caller example omits actions: read")

    def test_every_documented_release_caller_supplies_required_checks(self) -> None:
        """The input is mandatory, so an example without it is a call GitHub refuses."""
        for guide in self.guides_with_release_callers():
            lines = (self.DOCS / guide).read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if self.CALLS_RELEASE not in line:
                    continue
                indent = len(line) - len(line.lstrip())
                block = []
                for following in lines[index + 1 :]:
                    if following.strip() and (len(following) - len(following.lstrip())) < indent:
                        break
                    block.append(following)
                with self.subTest(guide=guide, line=index + 1):
                    self.assertIn("required-checks:", "\n".join(block))

    def test_the_verification_caller_needs_no_actions_read(self) -> None:
        """verify-skills.yml runs no API-backed gate, so it keeps the narrower grant."""
        text = (self.DOCS / "skill-consumers.md").read_text(encoding="utf-8")
        start = text.index("shared-conformance:")
        block = text[start : text.index("uses: ryanduguid/release-policy", start)]
        self.assertIn("contents: read", block)
        self.assertNotIn("actions: read", block)

    def test_the_prerequisites_explain_why_the_caller_must_grant_it(self) -> None:
        text = (self.DOCS / "consumer-prerequisites.md").read_text(encoding="utf-8")
        self.assertIn("actions: read", text)
        self.assertIn("cannot hold a permission its caller did not grant", text)


class MainTests(unittest.TestCase):
    def main(self, argv: list[str], text: str = LIST, **options: object) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = required_checks.main(argv, stdin=text, **options)  # type: ignore[arg-type]
        return code, out.getvalue(), err.getvalue()

    def test_passes_and_reports_every_check(self) -> None:
        code, out, err = self.main(
            ["--repository", REPO, "--commit", SHA], fetch_json=complete_success()
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.count("ok   "), 3)
        self.assertIn("required checks passed: 3", out)

    def test_rejects_bad_arguments_before_any_request(self) -> None:
        github = complete_success()
        for argv in (
            ["--repository", "bad", "--commit", SHA],
            ["--repository", REPO, "--commit", "abc"],
            ["--repository", REPO, "--commit", SHA, "--wait-seconds", "9999"],
            ["--repository", REPO, "--commit", SHA, "--poll-seconds", "0"],
        ):
            with self.subTest(argv=argv):
                code, _, err = self.main(argv, fetch_json=github)
                self.assertEqual(code, 1)
                self.assertIn("FAIL", err)
        self.assertEqual(github.requests, [])

    def test_reports_parse_and_api_errors_as_failures(self) -> None:
        code, _, err = self.main(["--repository", REPO, "--commit", SHA], text="")
        self.assertEqual(code, 1)
        self.assertIn("fails closed", err)

        def broken(_endpoint: str) -> object:
            raise RuntimeError("HTTP 500")

        code, _, err = self.main(["--repository", REPO, "--commit", SHA], fetch_json=broken)
        self.assertEqual((code, err), (1, "FAIL HTTP 500\n"))

    def test_reports_pending_and_failed_checks(self) -> None:
        github = complete_success()
        github.jobs[10][0] = job(1, "lint", conclusion=None, status="queued")
        github.jobs[11][0] = job(3, "boundaries", "failure")
        code, out, err = self.main(
            ["--repository", REPO, "--commit", SHA, "--wait-seconds", "0"], fetch_json=github
        )
        self.assertEqual(code, 1)
        self.assertEqual(out.count("ok   "), 1)
        self.assertIn("is queued after the 0s wait", err)
        self.assertIn("concluded 'failure'", err)

    def test_runs_as_a_script_and_exits_with_its_verdict(self) -> None:
        argv = ["required_checks.py", "--repository", "bad", "--commit", SHA]
        with mock.patch.object(sys, "argv", argv), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(raised.exception.code, 1)

    def test_reads_the_list_from_stdin_by_default(self) -> None:
        with mock.patch.object(required_checks.sys, "stdin", io.StringIO(LIST)):
            code, out, _ = self.main(["--repository", REPO, "--commit", SHA], text=None, fetch_json=complete_success())  # type: ignore[arg-type]
        self.assertEqual(code, 0)
        self.assertIn("required checks passed", out)


if __name__ == "__main__":
    unittest.main()
