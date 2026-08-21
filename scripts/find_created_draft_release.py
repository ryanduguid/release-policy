"""Find the exact draft identified by ``gh release create`` output."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
import subprocess
import time
from urllib.parse import urlparse


class ReleaseLookupError(RuntimeError):
    """Raised when the created release cannot be safely identified."""


class ReleaseNotVisibleError(ReleaseLookupError):
    """Raised while the exact created release is not yet visible."""


def _is_transient_listing_error(error: Exception) -> bool:
    if isinstance(error, json.JSONDecodeError):
        return True
    if not isinstance(error, subprocess.CalledProcessError):
        return False

    output = "\n".join(
        str(value)
        for value in (error.stdout, error.stderr)
        if value is not None
    ).casefold()
    transient_statuses = (
        "http 408",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    )
    transient_messages = (
        "connection reset",
        "network is unreachable",
        "temporarily unavailable",
        "temporary failure",
        "timed out",
        "timeout",
    )
    return any(status in output for status in transient_statuses) or any(
        message in output for message in transient_messages
    )


def _release_id_in_url(created_url: str) -> str | None:
    path_parts = [part for part in urlparse(created_url).path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[-2] == "releases" and path_parts[-1].isdigit():
        return path_parts[-1]
    return None


def select_created_draft_release_id(
    releases: Sequence[Mapping[str, object]],
    created_url: str,
) -> str:
    """Return the ID of the exact eligible draft identified by create output."""

    normalised_url = created_url.strip()
    if not normalised_url:
        raise ReleaseLookupError("gh release create did not return a release URL")

    created_id = _release_id_in_url(normalised_url)
    matches = [
        release
        for release in releases
        if (
            str(release.get("id", "")) == created_id
            if created_id is not None
            else normalised_url
            in {str(release.get("url", "")), str(release.get("html_url", ""))}
        )
    ]
    if not matches:
        raise ReleaseNotVisibleError(
            "created release is not visible in the paginated release listing"
        )
    if len(matches) != 1:
        raise ReleaseLookupError("created release URL matched more than one release")

    release = matches[0]
    if release.get("draft") is not True or release.get("prerelease") is not False:
        raise ReleaseLookupError("created release is not an eligible draft")

    release_id = str(release.get("id", ""))
    if not release_id.isdigit():
        raise ReleaseLookupError("created release did not contain a numeric ID")
    return release_id


def find_created_draft_release_id(
    list_releases: Callable[[], Sequence[Mapping[str, object]]],
    created_url: str,
    *,
    expected_tag: str | None = None,
    attempts: int,
    delay_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Retry listings until the exact draft created by GitHub appears."""

    if attempts < 1:
        raise ValueError("attempts must be at least one")

    last_error: Exception | None = None
    tracked_id: str | None = None
    for attempt in range(attempts):
        try:
            releases = list_releases()
            if tracked_id is None:
                tracked_id = select_created_draft_release_id(releases, created_url)

            matches = [
                release
                for release in releases
                if str(release.get("id", "")) == tracked_id
            ]
            if not matches:
                raise ReleaseNotVisibleError(
                    "created release ID is not visible in the paginated release listing"
                )
            if len(matches) != 1:
                raise ReleaseLookupError("created release ID matched more than one release")

            release = matches[0]
            if release.get("draft") is not True or release.get("prerelease") is not False:
                raise ReleaseLookupError("created release is not an eligible draft")
            if expected_tag is not None:
                observed_tag = str(release.get("tag_name", ""))
                if observed_tag != expected_tag:
                    if observed_tag.startswith("untagged-"):
                        raise ReleaseNotVisibleError(
                            "created release is visible but its requested tag has not settled"
                        )
                    raise ReleaseLookupError(
                        "created release has an unexpected tag: "
                        f"{observed_tag!r} != {expected_tag!r}"
                    )
            return tracked_id
        except ReleaseNotVisibleError as error:
            last_error = error
        except (json.JSONDecodeError, subprocess.CalledProcessError) as error:
            if not _is_transient_listing_error(error):
                raise
            last_error = error
        if attempt < attempts - 1:
            sleep(delay_seconds)

    raise ReleaseLookupError(
        "could not identify the draft created by gh release create "
        f"after {attempts} attempts: {last_error}"
    )


def _list_paginated_releases(repository: str) -> list[Mapping[str, object]]:
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
            f"repos/{repository}/releases?per_page=100",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise ReleaseLookupError("GitHub returned an invalid paginated release response")
    if not all(isinstance(release, dict) for page in pages for release in page):
        raise ReleaseLookupError("GitHub returned a non-object release entry")
    return [release for page in pages for release in page]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find the exact draft identified by gh release create output."
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--created-url", required=True)
    parser.add_argument("--expected-tag", required=True)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--delay-seconds", type=float, default=5)
    arguments = parser.parse_args()

    release_id = find_created_draft_release_id(
        lambda: _list_paginated_releases(arguments.repository),
        arguments.created_url,
        expected_tag=arguments.expected_tag,
        attempts=arguments.attempts,
        delay_seconds=arguments.delay_seconds,
    )
    print(release_id)


if __name__ == "__main__":
    main()
