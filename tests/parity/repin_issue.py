# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: Apache-2.0

"""Differential parity harness: scripts/repin_issue.py vs `release-policy repin-issue`."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "repin_issue.py"
WORKFLOW = ".github/workflows/attribution-policy.yml"
ACTION = ".github/actions/no-ai-attribution/action.yml"
BASELINE = "tests/test_repository_baseline.sh"
TITLE = "Repin the attribution action"
BOT = "github-actions[bot]"
PIN_PREFIX = "uses: ryanduguid/release-policy/.github/actions/no-ai-attribution@"

FAKE_GH = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
stdin_data = sys.stdin.read()
scenario = json.loads(Path(os.environ["FAKE_GH_SCENARIO"]).read_text(encoding="utf-8"))
log_path = Path(scenario["log"])
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"args": args, "stdin": stdin_data}) + "\\n")

for idx, rule in enumerate(scenario["rules"]):
    if rule["args"] == args:
        if "sequence" in rule:
            state_path = Path(str(log_path) + ".state.json")
            counts = (
                json.loads(state_path.read_text(encoding="utf-8"))
                if state_path.exists()
                else {}
            )
            count = counts.get(str(idx), 0)
            counts[str(idx)] = count + 1
            state_path.write_text(json.dumps(counts), encoding="utf-8")
            rule = rule["sequence"][min(count, len(rule["sequence"]) - 1)]
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("code", 0))

sys.stderr.write(f"fake gh: unexpected args {args!r}\\n")
sys.exit(99)
"""


def find_binary(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("RELEASE_POLICY_BIN")
    if env:
        return Path(env).resolve()
    exe = "release-policy.exe" if os.name == "nt" else "release-policy"
    for profile in ("release", "debug"):
        candidate = ROOT / "target" / profile / exe
        if candidate.exists():
            return candidate
    raise FileNotFoundError("pass --binary or set RELEASE_POLICY_BIN")


def find_fake_gh_exe(binary: Path) -> Path:
    candidates = [
        binary.with_name("fake-gh.exe"),
        ROOT / "target" / "release" / "fake-gh.exe",
        ROOT / "target" / "debug" / "fake-gh.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("build fake-gh.exe (`cargo build --bin fake-gh`) or use POSIX")


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return proc.stdout.strip()


def write_file(cwd: Path, rel: str, data: bytes | str) -> None:
    path = cwd / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_bytes(data.encode("utf-8"))
    else:
        path.write_bytes(data)


def make_repo(parent: Path, name: str, pin_mode: str = "stale") -> dict[str, str]:
    repo = parent / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for k, v in (
        ("user.name", "Fixture"),
        ("user.email", "fixture@example.invalid"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
        ("tag.gpgSign", "false"),
    ):
        git(repo, "config", k, v)
    write_file(repo, ACTION, "name: action v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    first = git(repo, "rev-parse", "HEAD")

    write_file(repo, ACTION, "name: action v2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "action v2")
    second = git(repo, "rev-parse", "HEAD")

    pin_sha = second if pin_mode == "current" else first
    wf = (
        "name: Attribution\non:\n  pull_request_target:\njobs:\n  check:\n"
        "    steps:\n"
        "      - uses: actions/checkout@1111111111111111111111111111111111111111 # v4.2.1\n"
        f"      - {PIN_PREFIX}{pin_sha}\n"
    )
    if pin_mode == "crlf":
        wf = wf.replace("\n", "\r\n")
    norm = wf.replace("\r\n", "\n").replace(
        "1111111111111111111111111111111111111111 # v4.2.1",
        "0000000000000000000000000000000000000000 # v4",
    )
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    base = f'CANONICAL_SHA256 = {{\n    "{WORKFLOW}": "{digest}",\n}}\n'
    if pin_mode == "crlf":
        base = base.replace("\n", "\r\n")
    write_file(repo, WORKFLOW, wf)
    write_file(repo, BASELINE, base)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pin workflow")
    pinned = git(repo, "rev-parse", "HEAD")

    return {"root": str(repo), "first": first, "second": second, "head": pinned}


def run_cmd(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def install_fake_gh(directory: Path, binary: Path) -> tuple[Path, Path]:
    bin_dir = directory / "bin"
    bin_dir.mkdir(exist_ok=True)
    if os.name == "nt":
        shutil.copy2(find_fake_gh_exe(binary), bin_dir / "gh.exe")
    else:
        gh_path = bin_dir / "gh"
        gh_path.write_text(FAKE_GH, encoding="utf-8", newline="\n")
        gh_path.chmod(gh_path.stat().st_mode | stat.S_IXUSR)
    scenario = directory / "scenario.json"
    return bin_dir, scenario


def py_cmd(args: list[str]) -> list[str]:
    runner = (
        "import pathlib, sys; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "import repin_issue; "
        "raise SystemExit(repin_issue.main(sys.argv[1:], root=pathlib.Path.cwd()))"
    )
    return [sys.executable, "-c", runner, *args]


def compare_prepare(binary: Path, tmp: Path, pin_mode: str, label: str) -> None:
    py_dir = tmp / f"{label}_py"
    rs_dir = tmp / f"{label}_rs"
    py_dir.mkdir()
    rs_dir.mkdir()
    # Copy the exact git repo bytes so both sides start from identical commits & SHAs.
    info = make_repo(py_dir, "repo", pin_mode=pin_mode)
    shutil.copytree(py_dir / "repo", rs_dir / "repo")
    env = dict(os.environ)
    py_patch = py_dir / "out.patch"
    rs_patch = rs_dir / "out.patch"
    args_py = ["prepare", "--target", info["head"], "--patch", str(py_patch)]
    args_rs = ["repin-issue", "prepare", "--target", info["head"], "--patch", str(rs_patch)]
    py_res = run_cmd(py_cmd(args_py), Path(info["root"]), env)
    rs_res = run_cmd([str(binary), *args_rs], rs_dir / "repo", env)

    expected_stdout = py_res.stdout.replace(str(py_patch), str(rs_patch))
    assert py_res.returncode == rs_res.returncode == 0, (py_res, rs_res)
    assert expected_stdout == rs_res.stdout, (expected_stdout, rs_res.stdout)
    assert py_res.stderr == rs_res.stderr == "", (py_res.stderr, rs_res.stderr)
    assert py_patch.exists() == rs_patch.exists(), (py_patch.exists(), rs_patch.exists())
    if py_patch.exists():
        assert py_patch.read_bytes() == rs_patch.read_bytes()
    for rel in (WORKFLOW, BASELINE):
        assert (Path(info["root"]) / rel).read_bytes() == (rs_dir / "repo" / rel).read_bytes()


def compare_publish_scenario(
    binary: Path,
    tmp: Path,
    name: str,
    rules: list[dict],
    patch_bytes: bytes = b"--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
    stale_reason: str | None = None,
) -> None:
    case_dir = tmp / name
    case_dir.mkdir()
    info = make_repo(case_dir, "repo", pin_mode="current")
    repo = Path(info["root"])
    patch = case_dir / "repin.patch"
    patch.write_bytes(patch_bytes)

    target = info["head"]
    current = info["head"]
    if stale_reason == "action":
        write_file(repo, ACTION, "name: moved\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "move action")
        current = git(repo, "rev-parse", "HEAD")
    elif stale_reason == "workflow":
        write_file(repo, WORKFLOW, "name: moved\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "move workflow")
        current = git(repo, "rev-parse", "HEAD")
    elif stale_reason == "baseline":
        write_file(repo, BASELINE, "changed\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "move baseline")
        current = git(repo, "rev-parse", "HEAD")
    elif stale_reason == "non_ancestor":
        target = info["second"]
        current = info["first"]

    bin_dir, scenario = install_fake_gh(case_dir, binary)
    if any(
        isinstance(item.get("body"), str) and item["body"] == "__CURRENT_BODY__"
        for r in rules
        if "stdout" in r
        for item in (json.loads(r["stdout"]) if r["stdout"].startswith("[") else [])
        if isinstance(item, dict)
    ):
        sys.path.insert(0, str(ROOT / "scripts"))
        import repin_issue as py_repin  # noqa: E402

        real_body = py_repin.issue_body(target, patch_bytes.decode("utf-8"))
        rules = [
            {**r, "stdout": r["stdout"].replace('"__CURRENT_BODY__"', json.dumps(real_body))}
            if "stdout" in r
            else r
            for r in rules
        ]
    results = []
    for tool_name, cmd in (
        (
            "py",
            py_cmd(
                [
                    "publish",
                    "--repository",
                    "o/r",
                    "--target",
                    target,
                    "--patch",
                    str(patch),
                    "--current",
                    current,
                ]
            ),
        ),
        (
            "rs",
            [
                str(binary),
                "repin-issue",
                "publish",
                "--repository",
                "o/r",
                "--target",
                target,
                "--patch",
                str(patch),
                "--current",
                current,
            ],
        ),
    ):
        log_path = case_dir / f"{tool_name}.calls.jsonl"
        scenario.write_text(
            json.dumps({"rules": rules, "log": str(log_path)}), encoding="utf-8"
        )
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_GH_SCENARIO"] = str(scenario)
        proc = run_cmd(cmd, repo, env)
        calls = (
            [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            if log_path.exists()
            else []
        )
        results.append((proc.returncode, proc.stdout, proc.stderr, calls))

    py_out, rs_out = results
    assert py_out == rs_out, f"{name}: mismatch\n  py={py_out!r}\n  rs={rs_out!r}"


def compare_error_case(
    binary: Path, cwd: Path, args: list[str], env: dict[str, str]
) -> None:
    py_res = run_cmd(py_cmd(args), cwd, env)
    rs_res = run_cmd([str(binary), "repin-issue", *args], cwd, env)
    assert py_res.returncode == rs_res.returncode, (args, py_res, rs_res)
    assert py_res.returncode != 0
    assert rs_res.stdout == ""
    assert rs_res.stderr.strip(), (args, rs_res)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=None)
    args = parser.parse_args()
    binary = find_binary(args.binary)

    compared = 0
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)

        # 1-3: prepare stale, current, CRLF + third-party pin
        for mode in ("stale", "current", "crlf"):
            compare_prepare(binary, tmp, mode, f"prep_{mode}")
            compared += 1

        # 4-7: publish stale main guards
        for reason in ("action", "workflow", "baseline", "non_ancestor"):
            compare_publish_scenario(binary, tmp, f"stale_{reason}", [], stale_reason=reason)
            compared += 1

        # 8-12: publish open/skip/noop/update/pagination/CRLF
        target_repo = make_repo(tmp, "body_ref", pin_mode="current")
        target = target_repo["head"]
        patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
        compare_publish_scenario(
            binary,
            tmp,
            "pub_open",
            [
                {
                    "args": ["api", "repos/o/r/issues?state=open&per_page=100&page=1"],
                    "stdout": "[]",
                },
                {
                    "args": ["api", "--method", "POST", "repos/o/r/issues", "--input", "-"],
                    "stdout": '{"number": 12}',
                },
            ],
        )
        compared += 1

        compare_publish_scenario(
            binary,
            tmp,
            "pub_human",
            [
                {
                    "args": ["api", "repos/o/r/issues?state=open&per_page=100&page=1"],
                    "stdout": json.dumps(
                        [{"number": 9, "title": TITLE, "user": {"login": "octocat"}}]
                    ),
                }
            ],
        )
        compared += 1

        compare_publish_scenario(
            binary,
            tmp,
            "pub_noop",
            [
                {
                    "args": ["api", "repos/o/r/issues?state=open&per_page=100&page=1"],
                    "stdout": json.dumps(
                        [
                            {
                                "number": 12,
                                "title": TITLE,
                                "body": "__CURRENT_BODY__",
                                "user": {"login": BOT},
                            }
                        ]
                    ),
                }
            ],
        )
        compared += 1

        compare_publish_scenario(
            binary,
            tmp,
            "pub_update_crlf",
            [
                {
                    "args": ["api", "repos/o/r/issues?state=open&per_page=100&page=1"],
                    "stdout": json.dumps(
                        [{"number": 12, "title": TITLE, "body": "old", "user": {"login": BOT}}]
                    ),
                },
                {
                    "args": ["api", "--method", "PATCH", "repos/o/r/issues/12", "--input", "-"],
                    "stdout": "{}",
                },
            ],
            patch_bytes=patch.replace("\n", "\r\n").encode("utf-8"),
        )
        compared += 1

        page1 = [{"number": n, "title": "Other"} for n in range(1, 101)]
        compare_publish_scenario(
            binary,
            tmp,
            "pub_paginate",
            [
                {
                    "args": ["api", "repos/o/r/issues?state=open&per_page=100&page=1"],
                    "stdout": json.dumps(page1),
                },
                {
                    "args": ["api", "repos/o/r/issues?state=open&per_page=100&page=2"],
                    "stdout": json.dumps(
                        [
                            {
                                "number": 200,
                                "title": TITLE,
                                "pull_request": {},
                                "user": {"login": BOT},
                            },
                            {
                                "number": 201,
                                "title": TITLE,
                                "body": "old",
                                "user": {"login": BOT},
                            },
                        ]
                    ),
                },
                {
                    "args": ["api", "--method", "PATCH", "repos/o/r/issues/201", "--input", "-"],
                    "stdout": "{}",
                },
            ],
        )
        compared += 1

        # 13-22: error & CLI exit parity cases
        err_repo = Path(target_repo["root"])
        err_patch = tmp / "err.patch"
        err_patch.write_text(patch, encoding="utf-8")
        fence_patch = tmp / "fence.patch"
        fence_patch.write_text("bad ````\n", encoding="utf-8")
        bin_patch = tmp / "bin.patch"
        bin_patch.write_bytes(b"\xff\xfe")

        bin_dir, scenario = install_fake_gh(tmp, binary)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_GH_SCENARIO"] = str(scenario)

        def pub_cmd(repo_name: str, patch_path: Path) -> list[str]:
            return [
                "publish",
                "--repository",
                repo_name,
                "--target",
                target,
                "--patch",
                str(patch_path),
                "--current",
                target,
            ]

        api_url = "repos/o/r/issues?state=open&per_page=100&page=1"
        err_cases: list[tuple[list[str], list[dict] | None]] = [
            (["prepare", "--target", "bad", "--patch", str(err_patch)], None),
            (["prepare", "--target", "0" * 40, "--patch", str(err_patch)], None),
            (pub_cmd("bad", err_patch), None),
            (pub_cmd("o/r", fence_patch), None),
            (pub_cmd("o/r", tmp / "missing.patch"), None),
            (pub_cmd("o/r", bin_patch), None),
            (pub_cmd("o/r", err_patch), [{"args": ["api", api_url], "code": 1, "stderr": "api down"}]),
            (pub_cmd("o/r", err_patch), [{"args": ["api", api_url], "stdout": "{}"}]),
            (["unknown-cmd"], None),
            (["prepare"], None),
        ]
        for idx, (cmd_args, rules) in enumerate(err_cases):
            log_p = tmp / f"err_{idx}.log"
            payload = json.dumps({"rules": rules or [], "log": str(log_p)})
            scenario.write_text(payload, encoding="utf-8")
            compare_error_case(binary, err_repo, cmd_args, env)
            compared += 1

    print(f"repin-issue parity: {compared} inputs compared, 0 unexplained differences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
