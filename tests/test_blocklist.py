from __future__ import annotations

import json

from xemu_perf_tester.util.blocklist import BlockList

_0_8_54 = "xemu-0.8.54-master-0c2a6178192043db8b30c4d61a96129b442102f5"
_0_8_92 = "xemu-0.8.92-master-9d5cf0926aa6f8eb2221e63a2e92bd86b02afae0"
_DEV_BUILD = (
    "xemu-0.8.82-62-g47ec547e53-fix_2301_allocate_minimum_inline_vertex_buffer-47ec547e53e660a41638955aee5e7e1b10dad329"
)
_PR_BUILD = "xemu-0.8.92-4-gfe356d27a4- -fe356d27a428c676846d20bab7f6fd21a210bbb1"

TEST_RULES = [
    {
        "conditions": ["$version <= 0.8.54"],
        "skipped": [
            "Test::CrashesOnBuildsLessThan0.8.55",
            "Another::Test",
        ],
    },
    {
        "conditions": [
            "$version == 0.8.50",
            "$version < 0.8.40",
            "$version == 0.8.42",
            "$version >= 999.9999.0",
            "$version > 99.9999.0",
        ],
        "skipped": [
            "Test::Foo",
        ],
    },
]


def test_no_rules():
    sut = BlockList(_0_8_92)

    assert not sut.disallowed_tests


def test_no_applicable_rules():
    sut = BlockList(_0_8_92, block_list_rules=TEST_RULES)

    assert not sut.disallowed_tests


def test_applicable_rule():
    sut = BlockList(_0_8_54, block_list_rules=TEST_RULES)

    assert set(sut.disallowed_tests) == {"Test::CrashesOnBuildsLessThan0.8.55", "Another::Test"}


def test_no_applicable_rules_from_file(tmp_path):
    block_list_file_path = tmp_path / "block_list.json"
    with open(block_list_file_path, "w") as outfile:
        json.dump({"rules": TEST_RULES}, outfile)
    sut = BlockList(_DEV_BUILD, block_list_file=str(block_list_file_path))

    assert not sut.disallowed_tests


def test_applicable_rules_from_file(tmp_path):
    block_list_file_path = tmp_path / "block_list.json"
    with open(block_list_file_path, "w") as outfile:
        json.dump({"rules": TEST_RULES}, outfile)
    sut = BlockList(_0_8_54, block_list_file=str(block_list_file_path))

    assert set(sut.disallowed_tests) == {"Test::CrashesOnBuildsLessThan0.8.55", "Another::Test"}
