"""Prepare only the pinned engine's two hash-verified source build exceptions."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.request import Request, build_opener

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from pr_review import PR_AGENT_SHA, NoRedirect, ReviewError, require

EXCEPTIONS = {"giteapy": "1.0.8", "html2text": "2024.2.26"}


def prepare_sources(engine: Path) -> None:
    revision = subprocess.run(["git", "-C", str(engine), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(engine), "status", "--porcelain",
                            "--untracked-files=no", "--", "pr_agent", "uv.lock"],
                           capture_output=True, text=True, check=True).stdout
    require(revision == PR_AGENT_SHA and not dirty, "engine_pin_mismatch")
    lock = tomllib.loads((engine / "uv.lock").read_text(encoding="utf-8"))
    packages = [p for p in lock["package"] if p["source"].get("registry") and not p.get("wheels")]
    require(len(packages) == 2 and {p["name"]: p["version"] for p in packages} == EXCEPTIONS,
            "unexpected_source_dependency")
    requirements = []
    for package in packages:
        source = package["sdist"]
        filename = f"{package['name']}-{package['version']}.tar.gz"
        require(package["source"] == {"registry": "https://pypi.org/simple"}
                and source["url"].startswith("https://files.pythonhosted.org/")
                and source["url"].rsplit("/", 1)[-1] == filename
                and type(source["size"]) is int and 0 < source["size"] <= 1_000_000,
                "untrusted_archive_source")
        with build_opener(NoRedirect()).open(Request(source["url"]), timeout=60) as response:
            raw = response.read(source["size"] + 1)
        require(len(raw) == source["size"]
                and "sha256:" + hashlib.sha256(raw).hexdigest() == source["hash"],
                "archive_checksum_mismatch")
        archive = engine / filename
        archive.write_bytes(raw)
        requirements.append(f"{archive.resolve().as_posix()} --hash={source['hash']}")
    (engine / "pr-review-source-exceptions.txt").write_text(
        "\n".join(requirements) + "\n", encoding="utf-8")


def verify_packages(engine: Path) -> None:
    expected = {}
    for line in (engine / "pr-review-runtime-requirements.txt").read_text(encoding="utf-8").splitlines():
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        pins = list(requirement.specifier)
        require(len(pins) == 1 and pins[0].operator == "==" and requirement.url is None,
                "unlocked_runtime_requirement")
        canonical_name = canonicalize_name(requirement.name)
        require(canonical_name not in expected, "duplicate_runtime_requirement")
        expected[canonical_name] = pins[0].version
    distributions = list(importlib.metadata.distributions())
    actual = {canonicalize_name(item.metadata["Name"]): item.version for item in distributions}
    require(len(actual) == len(distributions) and actual == expected, "runtime_lock_metadata_mismatch")
    for name, version in EXCEPTIONS.items():
        distribution = importlib.metadata.distribution(name)
        require(distribution.metadata["Name"].lower().replace("_", "-") == name
                and distribution.version == version, "source_package_metadata_mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify"))
    parser.add_argument("engine", type=Path)
    args = parser.parse_args(argv)
    try:
        require(not any(os.environ.get(name) for name in
                        ("OPENROUTER_API_KEY", "NOUS_API_KEY", "GH_TOKEN", "GITHUB_TOKEN")),
                "installer_credentials_refused")
        if args.command == "prepare":
            prepare_sources(args.engine)
        else:
            verify_packages(args.engine)
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, ReviewError) else "engine_install_failed",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
