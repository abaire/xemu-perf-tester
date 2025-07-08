from __future__ import annotations

import pytest

from xemu_perf_tester.util.xemu import XemuVersion

_0_8_54 = "xemu-0.8.54-master-0c2a6178192043db8b30c4d61a96129b442102f5"
_0_8_92 = "xemu-0.8.92-master-9d5cf0926aa6f8eb2221e63a2e92bd86b02afae0"
_DEV_BUILD = (
    "xemu-0.8.82-62-g47ec547e53-fix_2301_allocate_minimum_inline_vertex_buffer-47ec547e53e660a41638955aee5e7e1b10dad329"
)
_PR_BUILD = "xemu-0.8.92-4-gfe356d27a4- -fe356d27a428c676846d20bab7f6fd21a210bbb1"


def test_no_version():
    with pytest.raises(ValueError, match="Invalid xemu version string '"):
        XemuVersion("")


def test_release_version():
    sut = XemuVersion(_0_8_92)

    assert sut.is_release
    assert sut.xemu_major == 0
    assert sut.xemu_minor == 8
    assert sut.xemu_patch == 92


def test_dev_version():
    sut = XemuVersion(_DEV_BUILD)

    assert not sut.is_release
    assert sut.xemu_major == 0
    assert sut.xemu_minor == 8
    assert sut.xemu_patch == 82
    assert sut.build == 62
    assert sut.branch == "fix_2301_allocate_minimum_inline_vertex_buffer"


def test_pr_version():
    sut = XemuVersion(_PR_BUILD)

    assert not sut.is_release
    assert sut.xemu_major == 0
    assert sut.xemu_minor == 8
    assert sut.xemu_patch == 92
    assert sut.build == 4
    assert sut.branch == ""


def test_compare_release_release():
    a = XemuVersion(_0_8_54)
    b = XemuVersion(_0_8_92)

    assert a.compare(b) < 0
    assert a.compare(a) == 0
    assert b.compare(a) > 0


def test_compare_release_dev():
    a = XemuVersion(_0_8_54)
    b = XemuVersion(_DEV_BUILD)

    assert a.compare(b) < 0
    assert b.compare(b) == 0
    assert b.compare(a) > 0


def test_compare_release_pr():
    a = XemuVersion(_0_8_92)
    b = XemuVersion(_PR_BUILD)

    assert a.compare(b) == 0
    assert b.compare(a) == 0
