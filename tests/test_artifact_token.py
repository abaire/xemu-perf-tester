from __future__ import annotations

import subprocess
from unittest import mock

from xemu_perf_tester import runner


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_explicit_token_wins():
    with (
        mock.patch.object(runner, "get_gh_cli_token") as gh_token,
        mock.patch.object(runner, "get_github_token") as cached,
    ):
        assert runner.get_artifact_download_token("explicit") == "explicit"
    gh_token.assert_not_called()
    cached.assert_not_called()


def test_gh_cli_token_used_when_available():
    with (
        mock.patch.object(runner.shutil, "which", return_value="/usr/bin/gh"),
        mock.patch.object(runner.subprocess, "run", return_value=_completed(0, "gho_abc\n")),
        mock.patch.object(runner, "get_github_token") as cached,
    ):
        assert runner.get_artifact_download_token(None) == "gho_abc"
    cached.assert_not_called()


def test_falls_back_when_gh_missing():
    with (
        mock.patch.object(runner.shutil, "which", return_value=None),
        mock.patch.object(runner, "get_github_token", return_value="cached") as cached,
    ):
        assert runner.get_artifact_download_token(None) == "cached"
    cached.assert_called_once_with(validate=True)


def test_falls_back_when_gh_not_authenticated():
    with (
        mock.patch.object(runner.shutil, "which", return_value="/usr/bin/gh"),
        mock.patch.object(runner.subprocess, "run", return_value=_completed(1, stderr="not logged in")),
        mock.patch.object(runner, "get_github_token", return_value="cached"),
    ):
        assert runner.get_artifact_download_token(None) == "cached"


def test_falls_back_when_gh_errors():
    with (
        mock.patch.object(runner.shutil, "which", return_value="/usr/bin/gh"),
        mock.patch.object(runner.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 10)),
        mock.patch.object(runner, "get_github_token", return_value="cached"),
    ):
        assert runner.get_artifact_download_token(None) == "cached"
