from __future__ import annotations

from nxdk_pgraph_test_runner import Config

from xemu_perf_tester.util.perf_tester_config import XemuPerfTesterConfigManager, create_skip_config


def _repack(tmp_path, monkeypatch, **kwargs) -> dict:
    iso_path = tmp_path / "tester.iso"
    iso_path.write_bytes(b"")
    config = Config(work_dir=str(tmp_path), iso_path=str(iso_path), xbox_artifact_path=r"c:\out")
    manager = XemuPerfTesterConfigManager(config)

    repacked: dict = {}

    def fake_repack(tester_config, _output_path):
        repacked.update(tester_config)
        return True

    monkeypatch.setattr(
        manager,
        "extract_pgraph_tester_config",
        lambda: {"settings": {"run_mode": "calibrate"}, "test_suites": {"Old": {"skipped": False}}},
    )
    monkeypatch.setattr(manager, "_repack_config", fake_repack)

    assert manager.repack_iso_fresh(str(tmp_path / "out.iso"), **kwargs)
    return repacked


def test_sets_run_mode_and_replaces_test_filter(tmp_path, monkeypatch):
    config = _repack(tmp_path, monkeypatch)

    assert config["settings"]["run_mode"] == "single"
    assert config["settings"]["skip_tests_by_default"] is False
    assert config["settings"]["output_directory_path"] == r"c:\out"
    assert "warmup_frames" not in config["settings"]
    assert "test_suites" not in config


def test_sustained_frame_overrides(tmp_path, monkeypatch):
    config = _repack(tmp_path, monkeypatch, run_mode="sustained", warmup_frames=5, measure_frames=10)

    assert config["settings"]["run_mode"] == "sustained"
    assert config["settings"]["warmup_frames"] == 5
    assert config["settings"]["measure_frames"] == 10


def test_suite_allowlist_keeps_block_list(tmp_path, monkeypatch):
    config = _repack(
        tmp_path,
        monkeypatch,
        suites_to_enable=["PfifoContention", "Other"],
        tests_to_disable=["PfifoContention::TinyKicks", "Other::"],
    )

    assert config["settings"]["skip_tests_by_default"] is True
    assert config["test_suites"] == {
        "PfifoContention": {"skipped": False, "TinyKicks": {"skipped": True}},
        "Other": {"skipped": True},
    }


def test_create_skip_config_with_suite_and_test_of_that_suite():
    expected = {"test_suites": {"Suite": {"skipped": True, "Test": {"skipped": True}}}}

    assert create_skip_config(["Suite::", "Suite::Test"]) == expected
    assert create_skip_config(["Suite::Test", "Suite"]) == expected
