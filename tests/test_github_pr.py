from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
import requests

from xemu_perf_tester.util import xemu as xemu_module
from xemu_perf_tester.util.github import CiBuildInfo, CiBuildNotFoundError, fetch_ci_build_info, is_ci_build_url
from xemu_perf_tester.util.xemu import find_ci_artifact

_PR_URL = "https://github.com/xemu-project/xemu/pull/3088"
_RUN_URL = "https://github.com/xemu-project/xemu/actions/runs/1234"
_API = "https://api.github.com/repos/xemu-project/xemu"
_HEAD_SHA = "ffce6589c806fab529e2063b5d818e82d03f68da"


class _FakeResponse:
    def __init__(self, data: Any, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self) -> Any:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}"
            raise requests.exceptions.HTTPError(msg)


def _run(run_id: int, name: str = "CI", status: str = "completed", conclusion: str | None = "success", created="1"):
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "created_at": f"2026-10-0{created}T00:00:00Z",
        "html_url": f"https://github.com/xemu-project/xemu/actions/runs/{run_id}",
        "head_sha": _HEAD_SHA,
        "head_branch": "fix_1852",
        "pull_requests": [],
    }


def _artifact(name: str, artifact_id: int = 1, *, expired: bool = False) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "name": name,
        "expired": expired,
        "archive_download_url": f"{_API}/actions/artifacts/{artifact_id}/zip",
    }


def _make_get(routes: dict[str, Any]):
    def fake_get(url: str, **_kwargs):
        base = url.split("?per_page")[0].split("&per_page")[0]
        if base not in routes:
            return _FakeResponse({}, 404)
        return _FakeResponse(routes[base])

    return fake_get


def _pr_routes(runs: list[dict[str, Any]], artifacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    routes: dict[str, Any] = {
        f"{_API}/pulls/3088": {
            "head": {"sha": _HEAD_SHA},
            "html_url": _PR_URL,
            "title": "Fix 1852",
        },
        f"{_API}/actions/runs?head_sha={_HEAD_SHA}": {"total_count": len(runs), "workflow_runs": runs},
    }
    for run in runs:
        routes[f"{_API}/actions/runs/{run['id']}/artifacts"] = {
            "total_count": len(artifacts or []),
            "artifacts": artifacts or [],
        }
    return routes


def test_is_ci_build_url():
    assert is_ci_build_url(_PR_URL)
    assert is_ci_build_url(_RUN_URL)
    assert not is_ci_build_url("v0.8.92")
    assert not is_ci_build_url("latest")


def test_pr_success_picks_newest_successful_ci_run():
    runs = [
        _run(1, created="1"),
        _run(2, name="Other", created="3"),
        _run(3, created="2"),
        _run(4, conclusion="failure", created="4"),
    ]
    with mock.patch("requests.get", side_effect=_make_get(_pr_routes(runs, [_artifact("xemu-ubuntu-x86_64-release")]))):
        info = fetch_ci_build_info(_PR_URL)

    assert info.run_id == 3
    assert info.pr_number == 3088
    assert info.pr_title == "Fix 1852"
    assert info.head_sha == _HEAD_SHA
    assert info.head_ref == "fix_1852"
    assert [a["name"] for a in info.artifacts] == ["xemu-ubuntu-x86_64-release"]


def test_pr_in_progress():
    with (
        mock.patch("requests.get", side_effect=_make_get(_pr_routes([_run(1, status="in_progress", conclusion=None)]))),
        pytest.raises(CiBuildNotFoundError, match="still in_progress"),
    ):
        fetch_ci_build_info(_PR_URL)


def test_pr_failed():
    with (
        mock.patch("requests.get", side_effect=_make_get(_pr_routes([_run(1, conclusion="failure")]))),
        pytest.raises(CiBuildNotFoundError, match="concluded 'failure'"),
    ):
        fetch_ci_build_info(_PR_URL)


def test_pr_no_ci_run():
    with (
        mock.patch("requests.get", side_effect=_make_get(_pr_routes([_run(1, name="Lint")]))),
        pytest.raises(CiBuildNotFoundError, match="No 'CI' workflow runs"),
    ):
        fetch_ci_build_info(_PR_URL)


def test_pr_not_found():
    with (
        mock.patch("requests.get", side_effect=_make_get({})),
        pytest.raises(CiBuildNotFoundError, match="not found"),
    ):
        fetch_ci_build_info(_PR_URL)


def test_action_run_url():
    run = _run(1234)
    run["pull_requests"] = [{"number": 3088}]
    routes = {
        f"{_API}/actions/runs/1234": run,
        f"{_API}/actions/runs/1234/artifacts": {
            "total_count": 1,
            "artifacts": [_artifact("xemu-macos-universal-release")],
        },
    }
    with mock.patch("requests.get", side_effect=_make_get(routes)):
        info = fetch_ci_build_info(_RUN_URL)

    assert info.run_id == 1234
    assert info.pr_number == 3088
    assert info.pr_url == _PR_URL


def _build_info(artifacts: list[dict[str, Any]]) -> CiBuildInfo:
    return CiBuildInfo(
        owner="xemu-project",
        repo="xemu",
        run_id=1,
        run_url="https://github.com/xemu-project/xemu/actions/runs/1",
        head_sha=_HEAD_SHA,
        head_ref="fix",
        artifacts=artifacts,
    )


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", "xemu-windows-x86_64-release"),
        ("Windows", "ARM64", "xemu-windows-arm64-release"),
        ("Linux", "x86_64", "xemu-ubuntu-x86_64-release"),
        ("Linux", "aarch64", "xemu-ubuntu-aarch64-release"),
        ("Darwin", "arm64", "xemu-macos-universal-release"),
    ],
)
def test_find_ci_artifact_for_platform(system: str, machine: str, expected: str):
    artifacts = [
        _artifact("xemu-windows-x86_64-debug", 1),
        _artifact("xemu-windows-x86_64-release-pdb", 2),
        _artifact("xemu-windows-x86_64-release", 3),
        _artifact("xemu-windows-arm64-release", 4),
        _artifact("xemu-ubuntu-x86_64-release", 5),
        _artifact("xemu-ubuntu-aarch64-release", 6),
        _artifact("xemu-macos-arm64-release", 7),
        _artifact("xemu-macos-universal-release", 8),
    ]
    with (
        mock.patch.object(xemu_module.platform, "system", return_value=system),
        mock.patch.object(xemu_module.platform, "machine", return_value=machine),
    ):
        assert find_ci_artifact(_build_info(artifacts))["name"] == expected


def test_find_ci_artifact_legacy_windows_name():
    with (
        mock.patch.object(xemu_module.platform, "system", return_value="Windows"),
        mock.patch.object(xemu_module.platform, "machine", return_value="AMD64"),
    ):
        assert (
            find_ci_artifact(_build_info([_artifact("xemu-win-x86_64-release")]))["name"] == "xemu-win-x86_64-release"
        )


def test_find_ci_artifact_expired():
    with (
        mock.patch.object(xemu_module.platform, "system", return_value="Linux"),
        mock.patch.object(xemu_module.platform, "machine", return_value="x86_64"),
        pytest.raises(CiBuildNotFoundError, match="expired"),
    ):
        find_ci_artifact(_build_info([_artifact("xemu-ubuntu-x86_64-release", expired=True)]))


def test_find_ci_artifact_missing_lists_available():
    with (
        mock.patch.object(xemu_module.platform, "system", return_value="Linux"),
        mock.patch.object(xemu_module.platform, "machine", return_value="x86_64"),
        pytest.raises(CiBuildNotFoundError, match="xemu-macos-universal-release"),
    ):
        find_ci_artifact(_build_info([_artifact("xemu-macos-universal-release")]))


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("xemu-macos-universal-release.zip", True),
        ("xemu-0.8.138-9-g7850f6a9f4-macos-universal-unsigned.zip", True),
        ("xemu.app/Contents/Resources/foo.zip", False),
        ("xemu-ubuntu-x86_64-release.tgz", False),
    ],
)
def test_is_macos_nested_zip(filename: str, *, expected: bool):
    assert xemu_module._is_macos_nested_zip(filename) == expected  # noqa: SLF001 Private member accessed
