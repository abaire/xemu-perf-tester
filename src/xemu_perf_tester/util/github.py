from __future__ import annotations

# ruff: noqa: TRY300 Consider moving this statement to an `else` block
import logging
import os
import re
from typing import Any

import mergedeep
import requests

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECS = 15
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


def _fetch_pr_action_info(
    owner: str, repo: str, pr_number: str, additional_headers: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    headers = _API_HEADERS.copy()
    if additional_headers:
        mergedeep.merge(headers, additional_headers)

    pr_api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    try:
        response = requests.get(pr_api_url, headers=headers, timeout=_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
        pr_info = response.json()
        head_sha = pr_info.get("head", {}).get("sha")
        if not head_sha:
            return None

    except requests.exceptions.RequestException:
        logger.exception("Failed to retrieve PR information from %s", pr_api_url)
        return None

    runs_api_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs?head_sha={head_sha}"
    try:
        response = requests.get(runs_api_url, headers=headers, timeout=_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
        runs_info = response.json()
        if not runs_info.get("workflow_runs"):
            logger.error("No workflow runs found for %s/%s#%s SHA %s", owner, repo, pr_number, head_sha)
            return None

        return runs_info["workflow_runs"][0]

    except requests.exceptions.RequestException:
        logger.exception("Failed to retrieve workflow runs for %s/%s#%s SHA %s", owner, repo, pr_number, head_sha)
        return None


def fetch_github_ci_artifact_info(
    action_or_pr_url: str, additional_headers: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Fetches information about artifacts associated with an action or PR URL."""

    parsed_url = _parse_pr_url(action_or_pr_url)
    if parsed_url:
        run_info = _fetch_pr_action_info(*parsed_url, additional_headers=additional_headers)
        if not run_info:
            logger.error("Failed to fetch PR action information from %s", action_or_pr_url)
            return None

        action_or_pr_url = run_info.get("html_url", "")
        if not action_or_pr_url:
            msg = f"Missing expected 'html_url' member from action info fetched from {action_or_pr_url}: {run_info}"
            raise ValueError(msg)

    parsed_url = _parse_action_run_url(action_or_pr_url)
    if not parsed_url:
        return None
    owner, repo, run_id = parsed_url
    api_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts"

    headers = _API_HEADERS.copy()
    if additional_headers:
        mergedeep.merge(headers, additional_headers)

    try:
        response = requests.get(api_url, headers=headers, timeout=_FETCH_TIMEOUT_SECS)
        response.raise_for_status()
        return response.json()

    except requests.exceptions.RequestException:
        logger.exception("Failed to retrieve artifact information from %s", api_url)
        return None


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
