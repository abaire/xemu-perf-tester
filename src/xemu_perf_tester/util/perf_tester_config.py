# SPDX-FileCopyrightText: 2025-present Erik Abair <erik.abair@bearbrains.work>
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any

import mergedeep
from python_xiso_repacker import ensure_extract_xiso, extract_file, replace_file

if TYPE_CHECKING:
    from collections.abc import Collection

    from nxdk_pgraph_test_runner.config import Config

logger = logging.getLogger(__name__)


class XemuPerfTesterConfigManager:
    def __init__(self, runner_config: Config, iso_path: str | None = None) -> None:
        self._runner_config = runner_config
        self._data_dir = self._runner_config.ensure_data_dir()

        self.iso_path = iso_path if iso_path else self._runner_config.iso_path
        if not os.path.isfile(self.iso_path):
            msg = f"Invalid path to ISO file '{self.iso_path}"
            raise ValueError(msg)

    def repack_iso_fresh(
        self,
        output_path: str,
        tests_to_disable: list[str] | None = None,
    ) -> bool:
        """Repacks the xemu_perf_tests iso with FTP enabled."""
        tester_config = self.extract_pgraph_tester_config()
        if not tester_config:
            tester_config = {"settings": {}}

        if not self.iso_path:
            logger.error("No ISO file set")
            return False

        mergedeep.merge(
            tester_config,
            {
                "settings": {
                    "disable_autorun": False,
                    "enable_autorun_immediately": True,
                    "enable_shutdown_on_completion": True,
                    "reboot_or_shutdown_delay": 100,
                    "output_directory_path": self._runner_config.xbox_artifact_path,
                    "skip_tests_by_default": False,
                }
            },
        )

        tester_config.pop("test_suites", None)
        if tests_to_disable:
            skip_config = create_skip_config(tests_to_disable)
            logger.debug("Repacking with additional skipped tests %s", skip_config)
            mergedeep.merge(tester_config, skip_config)

        return self._repack_config(tester_config, output_path)

    def extract_pgraph_tester_config(self) -> dict[str, Any] | None:
        """Extracts an existing JSON config from the xemu_perf_tests ISO."""
        logger.info("Extracting config from %s", self.iso_path)

        extract_xiso = ensure_extract_xiso(None)
        if not extract_xiso:
            logger.error("extract-xiso is unavailable")
            return None

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, "config.json")
            if not extract_file(self.iso_path, "xemu_perf_tests_config.json", output_file, extract_xiso):
                logger.error("Failed to extract JSON config")
                return None

            return _parse_config_file(output_file)

    def repack_with_additional_tests_disabled(self, names_to_disable: Collection[str]) -> bool:
        """Repacks the ISO with the given fully qualified test names disabled."""
        tester_config = self.extract_pgraph_tester_config()
        if not tester_config:
            msg = "Failed to extract existing xemu_perf_tests config."
            raise ValueError(msg)

        skip_config = create_skip_config(names_to_disable)
        logger.debug("Repacking with additional skipped tests %s", skip_config)
        mergedeep.merge(tester_config, skip_config)

        return self._repack_config(tester_config, output_path=self.iso_path)

    def repack_with_only_tests(self, names_to_enable: Collection[str]) -> bool:
        """Repacks the ISO with the given fully qualified test name."""
        tester_config = self.extract_pgraph_tester_config()
        if not tester_config:
            msg = "Failed to extract existing xemu_perf_tests config."
            raise ValueError(msg)

        if "settings" in tester_config:
            tester_config["settings"]["skip_tests_by_default"] = True

        test_suites: dict[str, Any] = {}
        for fq_name in names_to_enable:
            suite, name = fq_name.split("::")

            if suite not in test_suites:
                test_suites[suite] = {}

            test_suites[suite][name] = {"skipped": False}

        tester_config["test_suites"] = test_suites

        return self._repack_config(tester_config, output_path=self.iso_path)

    def repack_with_only_test_suites(self, suites_to_enable: Collection[str]) -> bool:
        """Repacks the ISO with the given fully qualified test name."""
        tester_config = self.extract_pgraph_tester_config()
        if not tester_config:
            msg = "Failed to extract existing test config."
            raise ValueError(msg)

        if "settings" in tester_config:
            tester_config["settings"]["skip_tests_by_default"] = True
        tester_config["test_suites"] = {suite: {"skipped": False} for suite in suites_to_enable}

        return self._repack_config(tester_config, output_path=self.iso_path)

    def _repack_config(self, tester_config: dict[str, Any], output_path: str) -> bool:
        """Repacks the source ISO with the given xemu_perf_tests config data."""

        extract_xiso = ensure_extract_xiso(None)
        if not extract_xiso:
            logger.error("extract-xiso is unavailable")
            return False

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "updated-config.json")
            with open(config_path, "w", encoding="utf-8") as outfile:
                logger.debug("Repacking with config content\n%s\n", tester_config)
                json.dump(tester_config, outfile, ensure_ascii=True, indent=2, sort_keys=True)

            if not replace_file(self.iso_path, output_path, "xemu_perf_tests_config.json", config_path, extract_xiso):
                logger.error("Failed to repack ISO")
                return False

        return True


def create_skip_config(names_to_disable: Collection[str]) -> dict[str, Any]:
    """Creates a 'test_suites' object with the given tests skipped."""

    ret: dict[str, Any] = {}
    skip_config = {"skipped": True}

    for fq_name in names_to_disable:
        elements = fq_name.split("::")
        if len(elements) > 1 and elements[1]:
            suite, test = elements

            if suite not in ret:
                ret[suite] = {}
            ret[suite][test] = skip_config
        else:
            suite = elements[0]
            if suite not in ret:
                ret[suite] = skip_config

    return {"test_suites": ret}


def _parse_config_file(file_path: str) -> dict[str, Any] | None:
    with open(file_path, "rb") as infile:
        return json.load(infile)
