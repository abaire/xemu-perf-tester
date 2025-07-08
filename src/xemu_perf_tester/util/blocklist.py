from __future__ import annotations

import json
import os
from typing import Any

import semver

from xemu_perf_tester.util.xemu import XemuVersion

# ruff: noqa: PLR2004 Magic value used in comparison

_OPERATORS = {
    "<": lambda a, b: a.compare(b) < 0,
    "<=": lambda a, b: a.compare(b) <= 0,
    ">": lambda a, b: a.compare(b) > 0,
    ">=": lambda a, b: a.compare(b) >= 0,
    "==": lambda a, b: a.compare(b) == 0,
    "!=": lambda a, b: a.compare(b) != 0,
}


class BlockList:
    """Manages a block list to selectively disable tests."""

    class Rule:
        def __init__(self, definition: dict[str, Any]):
            self.conditions: list[str] = definition["conditions"]
            self.block_list: list[str] = definition["skipped"]

        def is_applicable(self, xemu_version: XemuVersion) -> bool:
            def parse_version(item: str) -> semver.Version:
                return xemu_version.semver if item.lower() == "$version" else semver.Version.parse(item)

            for condition in self.conditions:
                cleaned: str = condition.replace(" ", "")
                for key in sorted(_OPERATORS, key=len, reverse=True):
                    elements = cleaned.split(key)
                    if len(elements) != 2:
                        continue

                    versions = [parse_version(item) for item in elements]
                    if _OPERATORS[key](*versions):
                        return True
                    break

            return False

    def __init__(
        self,
        xemu_version: str,
        *,
        block_list_file: str | None = None,
        block_list_rules: list[dict[str, Any]] | None = None,
    ):
        self._xemu_version = XemuVersion(xemu_version)
        self._rules: list[BlockList.Rule] = []

        if block_list_file and os.path.isfile(block_list_file):
            with open(block_list_file, "rb") as infile:
                content = json.load(infile)

            self._rules = _parse_rules(content["rules"])
        elif block_list_rules:
            self._rules = _parse_rules(block_list_rules)

        self._rules = [rule for rule in self._rules if rule.is_applicable(self._xemu_version)]

    @property
    def disallowed_tests(self) -> list[str]:
        ret = []
        for rule in self._rules:
            ret.extend(rule.block_list)
        return ret


def _parse_rules(rules: list[dict[str, Any]]):
    return [BlockList.Rule(item) for item in rules]
