from __future__ import annotations

# ruff: noqa: FURB122 [*] Use of `f.write` in a for loop
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import mergedeep
import requests

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECS = 15
_HTTP_NOT_FOUND = 404
_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def fetch_github_release_info(
    api_url: str, tag: str = "latest", additional_headers: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Fetches information about a specific release tag."""
    full_url = f"{api_url}/releases/latest" if not tag or tag == "latest" else f"{api_url}/releases?per_page=60"

    def fetch_and_filter(url: str):
        headers = _API_HEADERS.copy()
        if additional_headers:
            mergedeep.merge(headers, additional_headers)

        try:
            response = requests.get(url, headers=headers, timeout=_FETCH_TIMEOUT_SECS)
            response.raise_for_status()
            release_info = response.json()

        except requests.exceptions.RequestException:
            logger.exception("Failed to retrieve information from %s", url)
            return None

        if isinstance(release_info, list):
            release_info = _filter_release_info_by_tag(release_info, tag)
        if release_info:
            return release_info

        if not response.links:
            return None

        next_link = response.links.get("next", {}).get("url")
        if not next_link:
            return None
        if "per_page=60" not in next_link:
            next_link = next_link + "&per_page=60"
        return fetch_and_filter(next_link)

    return fetch_and_filter(full_url)


class CiBuildNotFoundError(RuntimeError):
    """Raised when a usable CI build cannot be found for a PR or action run URL."""


@dataclass
class CiBuildInfo:
    """Describes a GitHub Actions workflow run that produced xemu build artifacts."""

    owner: str
    repo: str
    run_id: int
    run_url: str
    head_sha: str
    head_ref: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    pr_number: int | None = None
    pr_url: str | None = None
    pr_title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": f"{self.owner}/{self.repo}",
            "run_id": self.run_id,
            "run_url": self.run_url,
            "head_sha": self.head_sha,
            "head_ref": self.head_ref,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "pr_title": self.pr_title,
        }


def is_ci_build_url(url: str) -> bool:
    """Returns True if the given string is a GitHub PR or action run URL."""
    return bool(_parse_pr_url(url) or _parse_action_run_url(url))


def _get_json(url: str, headers: dict[str, Any], description: str) -> Any:
    try:
        response = requests.get(url, headers=headers, timeout=_FETCH_TIMEOUT_SECS)
    except requests.exceptions.RequestException as err:
        msg = f"Failed to retrieve {description} from {url}: {err}"
        raise CiBuildNotFoundError(msg) from err

    if response.status_code == _HTTP_NOT_FOUND:
        msg = f"{description} not found at {url}"
        raise CiBuildNotFoundError(msg)
    try:
        response.raise_for_status()
    except requests.exceptions.RequestException as err:
        msg = f"Failed to retrieve {description} from {url}: {err}"
        raise CiBuildNotFoundError(msg) from err
    return response.json()


def _get_paginated(url: str, headers: dict[str, Any], key: str, description: str) -> list[dict[str, Any]]:
    """Fetches all pages of a GitHub list API that returns {key: [...]}."""
    ret: list[dict[str, Any]] = []
    separator = "&" if "?" in url else "?"
    page = 1
    while True:
        data = _get_json(f"{url}{separator}per_page=100&page={page}", headers, description)
        items = data.get(key, [])
        ret.extend(items)
        if len(items) < 100 or len(ret) >= data.get("total_count", 0):  # noqa: PLR2004 Magic value
            return ret
        page += 1


def _select_workflow_run(
    runs: list[dict[str, Any]], workflow_name: str, description: str, head_sha: str
) -> dict[str, Any]:
    candidates = [run for run in runs if run.get("name") == workflow_name]
    if not candidates:
        available = sorted({run.get("name", "<unnamed>") for run in runs})
        msg = (
            f"No '{workflow_name}' workflow runs found for {description} at {head_sha}. "
            f"Available workflows: {available or 'none'}"
        )
        raise CiBuildNotFoundError(msg)

    candidates.sort(key=lambda run: run.get("created_at", ""), reverse=True)
    for run in candidates:
        if run.get("status") == "completed" and run.get("conclusion") == "success":
            return run

    newest = candidates[0]
    if newest.get("status") != "completed":
        msg = (
            f"The '{workflow_name}' workflow for {description} is still {newest.get('status')} "
            f"({newest.get('html_url')}). Try again once it has completed."
        )
        raise CiBuildNotFoundError(msg)

    msg = (
        f"No successful '{workflow_name}' workflow run for {description} at {head_sha}. "
        f"Latest run concluded '{newest.get('conclusion')}' ({newest.get('html_url')})"
    )
    raise CiBuildNotFoundError(msg)


def fetch_ci_build_info(
    action_or_pr_url: str, additional_headers: dict[str, Any] | None = None, workflow_name: str = "CI"
) -> CiBuildInfo:
    """Resolves a PR or action run URL to the workflow run containing its build artifacts.

    Raises CiBuildNotFoundError if no usable build exists.
    """
    headers = _API_HEADERS.copy()
    if additional_headers:
        mergedeep.merge(headers, additional_headers)

    pr_number: int | None = None
    pr_url: str | None = None
    pr_title: str | None = None

    parsed_pr = _parse_pr_url(action_or_pr_url)
    if parsed_pr:
        owner, repo, pr_number_str = parsed_pr
        pr_number = int(pr_number_str)
        api_root = f"https://api.github.com/repos/{owner}/{repo}"
        description = f"{owner}/{repo}#{pr_number}"

        pr_info = _get_json(f"{api_root}/pulls/{pr_number}", headers, f"PR {description}")
        head_sha = pr_info.get("head", {}).get("sha")
        if not head_sha:
            msg = f"PR {description} has no head SHA"
            raise CiBuildNotFoundError(msg)
        pr_url = pr_info.get("html_url", action_or_pr_url)
        pr_title = pr_info.get("title")

        runs = _get_paginated(
            f"{api_root}/actions/runs?head_sha={head_sha}", headers, "workflow_runs", f"workflow runs for {description}"
        )
        run_info = _select_workflow_run(runs, workflow_name, description, head_sha)
    else:
        parsed_run = _parse_action_run_url(action_or_pr_url)
        if not parsed_run:
            msg = f"'{action_or_pr_url}' is not a GitHub PR or action run URL"
            raise CiBuildNotFoundError(msg)
        owner, repo, run_id_str = parsed_run
        api_root = f"https://api.github.com/repos/{owner}/{repo}"
        run_info = _get_json(f"{api_root}/actions/runs/{run_id_str}", headers, f"action run {run_id_str}")
        if run_info.get("status") != "completed" or run_info.get("conclusion") != "success":
            msg = (
                f"Action run {action_or_pr_url} is not a successful completed run "
                f"(status: {run_info.get('status')}, conclusion: {run_info.get('conclusion')})"
            )
            raise CiBuildNotFoundError(msg)
        associated_prs = run_info.get("pull_requests") or []
        if len(associated_prs) == 1:
            pr_number = associated_prs[0].get("number")
            if pr_number:
                pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"

    run_id = run_info["id"]
    artifacts = _get_paginated(
        f"{api_root}/actions/runs/{run_id}/artifacts", headers, "artifacts", f"artifacts for run {run_id}"
    )

    return CiBuildInfo(
        owner=owner,
        repo=repo,
        run_id=run_id,
        run_url=run_info.get("html_url", ""),
        head_sha=run_info.get("head_sha", ""),
        head_ref=run_info.get("head_branch") or "",
        artifacts=artifacts,
        pr_number=pr_number,
        pr_url=pr_url,
        pr_title=pr_title,
    )


def download_artifact(
    target_path: str,
    download_url: str,
    artifact_path_override: str | None = None,
    additional_headers: dict[str, Any] | None = None,
    *,
    force_download: bool = False,
) -> bool:
    """Downloads an artifact from the given URL, if it does not already exist. Returns True if download was needed."""
    if os.path.exists(target_path) and not force_download:
        return False

    if artifact_path_override and os.path.exists(artifact_path_override) and not force_download:
        return True

    if not download_url.startswith("https://"):
        logger.error("Download URL '%s' has unexpected scheme", download_url)
        msg = f"Bad download_url '{download_url} - non HTTPS scheme"
        raise ValueError(msg)

    logger.debug("Downloading %s from %s", target_path, download_url)
    if artifact_path_override:
        target_path = artifact_path_override
        logger.debug(
            "> downloading artifact %s containing %s",
            artifact_path_override,
            target_path,
        )
    os.makedirs(os.path.dirname(target_path), exist_ok=True)

    headers = {
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if additional_headers:
        mergedeep.merge(headers, additional_headers)

    try:
        with requests.get(download_url, headers=headers, stream=True, timeout=90) as r:
            r.raise_for_status()
            with open(target_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
    except requests.exceptions.RequestException:
        logger.exception("Failed to download artifact %s from '%s'", target_path, download_url)
        return False

    return True


def _filter_release_info_by_tag(release_infos: list[dict[str, Any]], tag: str) -> dict[str, Any] | None:
    for info in release_infos:
        logger.debug("Release info: %s", info.get("tag_name", "<<NO TAG NAME>>"))
        if info.get("tag_name") == tag:
            return info
    return None


def _parse_action_run_url(action_run_url: str) -> tuple[str, str, str] | None:
    """Parses a GitHub Actions run URL to extract owner, repo, and run_id."""
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/actions/runs/(\d+)", action_run_url)
    if match:
        return match.group(1), match.group(2), match.group(3)
    return None


def _parse_pr_url(pr_url: str) -> tuple[str, str, str] | None:
    """Parses a GitHub PR URL to extract owner, repo, and pr_number."""
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if match:
        return match.group(1), match.group(2), match.group(3)
    return None
