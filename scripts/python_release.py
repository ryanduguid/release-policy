"""Closed metadata parsing and exact candidate inventory for Python releases."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tomllib
from typing import Any, Sequence


_CANONICAL_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_STEM = re.compile(r"[A-Za-z0-9.]+(?:_[A-Za-z0-9.]+)*\Z")
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_ASSET_BYTES = 512 * 1024 * 1024
_MANIFEST = "release-manifest.json"
_CHECKSUMS = "SHA256SUMS"
_POLICY_REPOSITORY = "ryanduguid/release-policy"
_POLICY_WORKFLOW = ".github/workflows/release-python.yml"


@dataclass(frozen=True)
class ReleaseMetadata:
    """Normalised package metadata used by the release gates."""

    name: str
    stem: str
    version: str


@dataclass(frozen=True)
class CandidateAsset:
    """One exact payload file in the candidate bundle."""

    name: str
    media_type: str
    sha256: str
    size: int


@dataclass(frozen=True)
class CandidateInventory:
    """The release identity bound to an immutable Actions artefact."""

    stem: str
    version: str
    tag: str
    repository: str
    commit: str
    policy_sha: str
    run_id: int
    run_attempt: int
    assets: tuple[CandidateAsset, ...]

    def as_manifest(self) -> dict[str, object]:
        return {
            "assets": [
                {
                    "media_type": asset.media_type,
                    "name": asset.name,
                    "sha256": asset.sha256,
                    "size": asset.size,
                }
                for asset in self.assets
            ],
            "package": {"stem": self.stem, "version": self.version},
            "policy": {
                "repository": _POLICY_REPOSITORY,
                "sha": self.policy_sha,
                "workflow": _POLICY_WORKFLOW,
            },
            "run": {"attempt": self.run_attempt, "id": self.run_id},
            "schema": 1,
            "source": {
                "commit": self.commit,
                "ref": f"refs/tags/{self.tag}",
                "repository": self.repository,
            },
        }


def _safe_relative_path(supplied: str) -> PurePosixPath:
    if "\\" in supplied:
        raise ValueError("version-file must use a safe relative POSIX path")
    relative = PurePosixPath(supplied)
    if (
        not supplied
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("version-file must be a safe relative POSIX path")
    return relative


def _tracked_regular_file(root: Path, supplied: str) -> Path:
    root = root.resolve()
    relative = _safe_relative_path(supplied)
    lexical = root.joinpath(*relative.parts)

    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"version-file must not traverse a symbolic link: {supplied}")

    try:
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError(f"version-file is missing: {supplied}") from error
    if resolved == root or root not in resolved.parents or not resolved.is_file():
        raise ValueError("version-file must be a regular file inside the checkout")

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative.as_posix()],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    if tracked.returncode != 0:
        raise ValueError(f"version-file must be tracked by Git: {supplied}")
    if resolved.stat().st_size > _MAX_METADATA_BYTES:
        raise ValueError("version-file exceeds the 1 MiB metadata limit")
    return resolved


def _read_utf8(path: Path, *, label: str) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{label} must not contain a UTF-8 BOM")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} must be UTF-8") from error


def _project_name(root: Path) -> str:
    project_file = _tracked_regular_file(root, "pyproject.toml")
    try:
        document = tomllib.loads(_read_utf8(project_file, label="pyproject.toml"))
        name = document["project"]["name"]
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("pyproject.toml must declare [project].name") from error
    if not isinstance(name, str) or not name.strip():
        raise ValueError("pyproject.toml [project].name must be a non-empty string")
    return name


def _version_from_pyproject(path: Path) -> str:
    try:
        document = tomllib.loads(_read_utf8(path, label="version-file"))
        version = document["project"]["version"]
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("pyproject parser requires [project].version") from error
    if not isinstance(version, str):
        raise ValueError("pyproject [project].version must be a string")
    return version


def _literal_assignment_value(node: ast.stmt) -> tuple[bool, object | None]:
    if isinstance(node, ast.Assign):
        targets = node.targets
        value = node.value
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
        value = node.value
    else:
        return False, None

    matches = [
        target
        for target in targets
        if isinstance(target, ast.Name) and target.id == "__version__"
    ]
    if not matches:
        return False, None
    if len(targets) != 1 or value is None:
        raise ValueError("__version__ must be one direct literal string assignment")
    try:
        return True, ast.literal_eval(value)
    except (ValueError, TypeError) as error:
        raise ValueError("__version__ must be one direct literal string assignment") from error


def _version_from_python_literal(path: Path) -> str:
    text = _read_utf8(path, label="version-file")
    try:
        module = ast.parse(text, filename=path.name, mode="exec")
    except SyntaxError as error:
        raise ValueError("version-file must be valid Python source") from error

    values: list[object] = []
    for node in module.body:
        matched, value = _literal_assignment_value(node)
        if matched:
            values.append(value)
    if len(values) != 1:
        raise ValueError("version-file must contain exactly one __version__ assignment")
    if not isinstance(values[0], str):
        raise ValueError("__version__ must be one direct literal string assignment")
    return values[0]


def derive_metadata(
    root: Path,
    *,
    version_parser: str,
    version_file: str,
) -> ReleaseMetadata:
    """Read package name and version through one of two closed parsers."""

    parsers = {
        "pyproject": _version_from_pyproject,
        "python-literal": _version_from_python_literal,
    }
    parser = parsers.get(version_parser)
    if parser is None:
        raise ValueError("version-parser must be 'pyproject' or 'python-literal'")

    root = root.resolve()
    name = _project_name(root)
    path = _tracked_regular_file(root, version_file)
    version = parser(path)
    if _CANONICAL_VERSION.fullmatch(version) is None:
        raise ValueError("version must be canonical MAJOR.MINOR.PATCH")

    stem = re.sub(r"[^A-Za-z0-9.]+", "_", name)
    if _STEM.fullmatch(stem) is None:
        raise ValueError("project name does not produce a safe wheel stem")
    return ReleaseMetadata(name=name, stem=stem, version=version)


def _validate_context(
    *,
    stem: str,
    version: str,
    tag: str,
    repository: str,
    commit: str,
    policy_sha: str,
    run_id: int,
    run_attempt: int,
) -> None:
    if _STEM.fullmatch(stem) is None:
        raise ValueError("candidate stem is not safe")
    if _CANONICAL_VERSION.fullmatch(version) is None or tag != f"v{version}":
        raise ValueError("candidate tag and canonical version do not match")
    if _REPOSITORY.fullmatch(repository) is None:
        raise ValueError("candidate repository must be owner/name")
    if _FULL_SHA.fullmatch(commit) is None:
        raise ValueError("candidate source commit must be a full lower-case SHA")
    if _FULL_SHA.fullmatch(policy_sha) is None:
        raise ValueError("candidate policy SHA must be a full lower-case SHA")
    if run_id < 1 or run_attempt < 1:
        raise ValueError("candidate run id and attempt must be positive")


def _payload_contract(stem: str, version: str) -> tuple[tuple[str, str], ...]:
    base = f"{stem}-{version}"
    return (
        (f"{base}-py3-none-any.whl", "application/zip"),
        (f"{base}.tar.gz", "application/gzip"),
        (f"{base}.spdx.json", "application/spdx+json"),
    )


def _regular_asset(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"candidate asset must be a regular file: {path.name}")
    size = path.stat().st_size
    if size < 1 or size > _MAX_ASSET_BYTES:
        raise ValueError(f"candidate asset has an invalid size: {path.name}")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _manifest_bytes(inventory: CandidateInventory) -> bytes:
    return (json.dumps(inventory.as_manifest(), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _exact_directory_files(directory: Path) -> set[str]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("candidate directory must be a regular directory")
    entries = list(directory.iterdir())
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise ValueError(f"candidate asset must be a regular file: {entry.name}")
    return {entry.name for entry in entries}


def _candidate_directory(directory: Path) -> Path:
    lexical = directory if directory.is_absolute() else Path.cwd() / directory
    if lexical.is_symlink() or not lexical.is_dir():
        raise ValueError("candidate directory must be a regular directory")
    return lexical


def write_candidate_inventory(
    directory: Path,
    *,
    stem: str,
    version: str,
    tag: str,
    repository: str,
    commit: str,
    policy_sha: str,
    run_id: int,
    run_attempt: int,
) -> CandidateInventory:
    """Write a manifest and checksum file around exactly three prebuilt payloads."""

    _validate_context(
        stem=stem,
        version=version,
        tag=tag,
        repository=repository,
        commit=commit,
        policy_sha=policy_sha,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    directory = _candidate_directory(directory)
    contract = _payload_contract(stem, version)
    expected = {name for name, _ in contract}
    actual = _exact_directory_files(directory)
    for control in (_MANIFEST, _CHECKSUMS):
        if control in actual:
            raise FileExistsError(f"refusing to overwrite existing control file: {control}")
    if actual != expected:
        raise ValueError(
            f"candidate payload inventory mismatch: expected {sorted(expected)}, found {sorted(actual)}"
        )

    assets: list[CandidateAsset] = []
    for name, media_type in contract:
        path = directory / name
        _regular_asset(path)
        assets.append(
            CandidateAsset(
                name=name,
                media_type=media_type,
                sha256=_digest(path),
                size=path.stat().st_size,
            )
        )
    inventory = CandidateInventory(
        stem=stem,
        version=version,
        tag=tag,
        repository=repository,
        commit=commit,
        policy_sha=policy_sha,
        run_id=run_id,
        run_attempt=run_attempt,
        assets=tuple(assets),
    )

    manifest_path = directory / _MANIFEST
    manifest_path.write_bytes(_manifest_bytes(inventory))
    checksum_names = sorted((*expected, _MANIFEST))
    checksum_text = "".join(
        f"{_digest(directory / name)}  {name}\n" for name in checksum_names
    )
    (directory / _CHECKSUMS).write_text(checksum_text, encoding="utf-8", newline="\n")
    return inventory


def _expect_mapping(value: Any, *, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"candidate manifest {label} has an invalid shape")
    return value


def _inventory_from_manifest(document: Any) -> CandidateInventory:
    root = _expect_mapping(
        document,
        label="root",
        keys={"assets", "package", "policy", "run", "schema", "source"},
    )
    if root["schema"] != 1:
        raise ValueError("candidate manifest schema is unsupported")
    package = _expect_mapping(root["package"], label="package", keys={"stem", "version"})
    policy = _expect_mapping(
        root["policy"], label="policy", keys={"repository", "sha", "workflow"}
    )
    run = _expect_mapping(root["run"], label="run", keys={"attempt", "id"})
    source = _expect_mapping(
        root["source"], label="source", keys={"commit", "ref", "repository"}
    )
    if policy["repository"] != _POLICY_REPOSITORY or policy["workflow"] != _POLICY_WORKFLOW:
        raise ValueError("candidate manifest policy signer is not release-python.yml")
    if not isinstance(source["ref"], str) or not source["ref"].startswith("refs/tags/v"):
        raise ValueError("candidate manifest source ref is invalid")

    raw_assets = root["assets"]
    if not isinstance(raw_assets, list):
        raise ValueError("candidate manifest assets must be a list")
    assets: list[CandidateAsset] = []
    for raw_asset in raw_assets:
        asset = _expect_mapping(
            raw_asset,
            label="asset",
            keys={"media_type", "name", "sha256", "size"},
        )
        if not all(isinstance(asset[key], str) for key in ("media_type", "name", "sha256")):
            raise ValueError("candidate manifest asset text fields are invalid")
        if not isinstance(asset["size"], int):
            raise ValueError("candidate manifest asset size is invalid")
        assets.append(
            CandidateAsset(
                name=asset["name"],
                media_type=asset["media_type"],
                sha256=asset["sha256"],
                size=asset["size"],
            )
        )

    try:
        tag = str(source["ref"])[len("refs/tags/") :]
        inventory = CandidateInventory(
            stem=str(package["stem"]),
            version=str(package["version"]),
            tag=tag,
            repository=str(source["repository"]),
            commit=str(source["commit"]),
            policy_sha=str(policy["sha"]),
            run_id=int(run["id"]),
            run_attempt=int(run["attempt"]),
            assets=tuple(assets),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("candidate manifest scalar fields are invalid") from error
    _validate_context(
        stem=inventory.stem,
        version=inventory.version,
        tag=inventory.tag,
        repository=inventory.repository,
        commit=inventory.commit,
        policy_sha=inventory.policy_sha,
        run_id=inventory.run_id,
        run_attempt=inventory.run_attempt,
    )
    return inventory


def verify_candidate_inventory(
    directory: Path,
    *,
    expected_stem: str,
    expected_version: str,
    expected_tag: str,
    expected_repository: str,
    expected_commit: str,
    expected_policy_sha: str,
    expected_run_id: int,
    expected_run_attempt: int,
) -> CandidateInventory:
    """Fail closed unless candidate data and the expected workflow context agree."""

    directory = _candidate_directory(directory)
    expected_payload = {name for name, _ in _payload_contract(expected_stem, expected_version)}
    expected_files = {*expected_payload, _MANIFEST, _CHECKSUMS}
    actual_files = _exact_directory_files(directory)
    if actual_files != expected_files:
        raise ValueError(
            f"candidate inventory mismatch: expected {sorted(expected_files)}, found {sorted(actual_files)}"
        )

    manifest_path = directory / _MANIFEST
    if manifest_path.stat().st_size > _MAX_METADATA_BYTES:
        raise ValueError("candidate manifest exceeds the 1 MiB limit")
    try:
        document = json.loads(_read_utf8(manifest_path, label=_MANIFEST))
    except json.JSONDecodeError as error:
        raise ValueError("candidate manifest is not valid JSON") from error
    inventory = _inventory_from_manifest(document)
    if manifest_path.read_bytes() != _manifest_bytes(inventory):
        raise ValueError("candidate manifest is not canonical JSON")

    comparisons = (
        (inventory.stem, expected_stem, "package stem"),
        (inventory.version, expected_version, "package version"),
        (inventory.tag, expected_tag, "source tag"),
        (inventory.repository, expected_repository, "source repository"),
        (inventory.commit, expected_commit, "source commit"),
        (inventory.policy_sha, expected_policy_sha, "policy SHA"),
        (inventory.run_id, expected_run_id, "run id"),
        (inventory.run_attempt, expected_run_attempt, "run attempt"),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise ValueError(f"candidate {label} mismatch: {actual!r} != {expected!r}")

    expected_contract = dict(_payload_contract(expected_stem, expected_version))
    if {asset.name for asset in inventory.assets} != set(expected_contract):
        raise ValueError("candidate manifest payload inventory is invalid")
    if len(inventory.assets) != len(expected_contract):
        raise ValueError("candidate manifest contains duplicate assets")
    for asset in inventory.assets:
        path = directory / asset.name
        _regular_asset(path)
        if asset.media_type != expected_contract[asset.name]:
            raise ValueError(f"candidate media type mismatch: {asset.name}")
        if asset.size != path.stat().st_size:
            raise ValueError(f"candidate size mismatch: {asset.name}")
        if asset.sha256 != _digest(path):
            raise ValueError(f"candidate digest mismatch: {asset.name}")

    checksum_names = sorted((*expected_payload, _MANIFEST))
    expected_checksums = "".join(
        f"{_digest(directory / name)}  {name}\n" for name in checksum_names
    )
    actual_checksums = _read_utf8(directory / _CHECKSUMS, label=_CHECKSUMS)
    if actual_checksums != expected_checksums:
        raise ValueError("candidate checksum inventory or digest mismatch")
    return inventory


def _metadata_command(arguments: argparse.Namespace) -> int:
    metadata = derive_metadata(
        Path(arguments.root),
        version_parser=arguments.version_parser,
        version_file=arguments.version_file,
    )
    if arguments.format == "lines":
        print(metadata.stem)
        print(metadata.version)
    else:
        print(
            json.dumps(
                {"name": metadata.name, "stem": metadata.stem, "version": metadata.version},
                sort_keys=True,
            )
        )
    return 0


def _candidate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--stem", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--policy-sha", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)


def _create_candidate_command(arguments: argparse.Namespace) -> int:
    write_candidate_inventory(
        arguments.directory,
        stem=arguments.stem,
        version=arguments.version,
        tag=arguments.tag,
        repository=arguments.repository,
        commit=arguments.commit,
        policy_sha=arguments.policy_sha,
        run_id=arguments.run_id,
        run_attempt=arguments.run_attempt,
    )
    return 0


def _verify_candidate_command(arguments: argparse.Namespace) -> int:
    verify_candidate_inventory(
        arguments.directory,
        expected_stem=arguments.stem,
        expected_version=arguments.version,
        expected_tag=arguments.tag,
        expected_repository=arguments.repository,
        expected_commit=arguments.commit,
        expected_policy_sha=arguments.policy_sha,
        expected_run_id=arguments.run_id,
        expected_run_attempt=arguments.run_attempt,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    metadata = commands.add_parser("metadata", help="read closed package metadata")
    metadata.add_argument("--root", default=".")
    metadata.add_argument(
        "--version-parser", choices=("pyproject", "python-literal"), required=True
    )
    metadata.add_argument("--version-file", required=True)
    metadata.add_argument("--format", choices=("json", "lines"), default="json")
    metadata.set_defaults(handler=_metadata_command)

    create = commands.add_parser("create-candidate", help="write exact candidate controls")
    _candidate_arguments(create)
    create.set_defaults(handler=_create_candidate_command)

    verify = commands.add_parser("verify-candidate", help="verify exact candidate controls")
    _candidate_arguments(verify)
    verify.set_defaults(handler=_verify_candidate_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
