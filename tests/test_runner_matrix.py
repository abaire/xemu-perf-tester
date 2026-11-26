from __future__ import annotations

import pytest

from xemu_perf_tester.runner import RunSettings, build_arg_parser, build_hardware_baseline, resolve_run_settings


def _resolve(*argv: str) -> RunSettings:
    return resolve_run_settings(build_arg_parser().parse_args(list(argv)))


def test_defaults_to_single_gl_run():
    settings = _resolve()

    assert settings == RunSettings(run_mode="single", renderers=("GL",), vsync=(True,), repeat=1)
    assert settings.timeout_seconds is None


def test_use_vulkan_selects_vk():
    assert _resolve("--use-vulkan").renderers == ("VK",)


def test_contention_matrix():
    settings = _resolve("--matrix", "contention")

    assert settings.run_mode == "sustained"
    assert settings.just_suites == ("PfifoContention",)
    assert settings.cells == [("GL", True), ("GL", False), ("VK", True), ("VK", False)]
    assert settings.repeat == 3
    assert settings.timeout_seconds == 1800


def test_explicit_flags_override_matrix():
    settings = _resolve(
        "--matrix",
        "contention",
        "--renderers",
        "vk",
        "--vsync",
        "off",
        "--repeat",
        "2",
        "--just-suites",
        "Other",
        "--timeout-seconds",
        "60",
        "--warmup-frames",
        "5",
        "--measure-frames",
        "10",
    )

    assert settings.cells == [("VK", False)]
    assert settings.repeat == 2
    assert settings.just_suites == ("Other",)
    assert settings.timeout_seconds == 60
    assert settings.warmup_frames == 5
    assert settings.measure_frames == 10


def test_sustained_without_matrix_keeps_all_suites():
    settings = _resolve("--run-mode", "sustained")

    assert settings.just_suites is None
    assert settings.cells == [("GL", True)]
    assert settings.repeat == 3


def test_use_vulkan_overrides_matrix():
    assert _resolve("--matrix", "contention", "--use-vulkan").renderers == ("VK",)


def test_upload_allowed_with_standard_single_run():
    assert _resolve("--upload-results", "--renderers", "GL", "VK").cells == [("GL", True), ("VK", True)]


def test_duplicate_values_are_removed():
    assert _resolve("--renderers", "GL", "gl", "--vsync", "on", "on").cells == [("GL", True)]


@pytest.mark.parametrize(
    "argv",
    [
        ("--matrix", "contention", "--upload-results"),
        ("--run-mode", "sustained", "-U"),
        ("--renderers", "D3D"),
        ("--repeat", "0"),
        ("--upload-results", "--vsync", "on", "off"),
        ("--upload-results", "--repeat", "2"),
        ("--upload-results", "--measure-frames", "10"),
        ("--timeout-seconds", "0"),
        ("--warmup-frames", "-1"),
        ("--measure-frames", "0"),
    ],
)
def test_rejects_invalid_settings(argv):
    with pytest.raises(ValueError):  # noqa: PT011 `pytest.raises(ValueError)` is too broad
        _resolve(*argv)


def test_hardware_baseline():
    calibration = {
        "hardware": {"devkit_kernel": True},
        "calibration": {"target_busy_p95_us": 15000},
        "loads": {"PfifoContention::Control": 47440},
        "results": [
            {
                "name": "PfifoContention::Control",
                "status": "ok",
                "load": 47440,
                "busy_p50_us": 14381,
                "busy_p95_us": 14978,
                "max_resets": 0,
                "history": [{"load": 1}],
            },
            {"status": "ok"},
        ],
    }

    assert build_hardware_baseline(calibration) == {
        "hardware": {"devkit_kernel": True},
        "calibration": {"target_busy_p95_us": 15000},
        "tests": {
            "PfifoContention::Control": {
                "status": "ok",
                "load": 47440,
                "busy_p50_us": 14381,
                "busy_p95_us": 14978,
                "max_resets": 0,
            }
        },
    }
    assert build_hardware_baseline(None) is None
