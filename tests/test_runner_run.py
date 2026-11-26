from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from xemu_perf_tester import runner
from xemu_perf_tester.runner import RunSettings, _parse_results_file

_RECORD = '  {\n    "name": "PfifoContention::Control",\n    "busy_us": {"p50": 1, "p95": 2}\n  },\n'


class _FakeConfigManager:
    def __init__(self, _config):
        pass

    def extract_calibration(self):
        return None


@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    """Replaces everything run() launches or reads with fakes, and returns the launches that are made.

    Each launch is described by the dict in `launches` with the same index: `fail` makes xemu fail to start,
    `machine_info` sets xemu's reported machine info.
    """
    launches: list[dict] = []
    calls: list[int] = []
    results_dir = tmp_path / "results"

    monkeypatch.setattr(runner, "build_emulator_command", lambda *_a, **_k: ("xemu", str(tmp_path / "xemu.toml")))
    monkeypatch.setattr(runner, "_determine_xemu_info", lambda *_a, **_k: (str(results_dir), "xemu-test"))
    monkeypatch.setattr(runner, "BlockList", lambda *_a, **_k: SimpleNamespace(disallowed_tests=[]))
    monkeypatch.setattr(runner, "_configure_iso", lambda *_a: "repacked.iso")
    monkeypatch.setattr(runner, "XemuPerfTesterConfigManager", _FakeConfigManager)
    monkeypatch.setattr(runner, "_fetch_machine_info", lambda: {"cpu": "test"})
    monkeypatch.setattr(runner, "_fetch_host_noise_info", lambda: {"load_average": [len(calls)]})

    def execute(_config, _iso):
        launch = launches[len(calls)]
        calls.append(len(calls))
        if launch.get("fail"):
            return None, False
        return SimpleNamespace(machine_info=launch["machine_info"], failure_info=launch.get("failure_info", "")), False

    def retrieve(_hdd, output_path, _drive, file_path):
        os.makedirs(os.path.join(output_path, os.path.dirname(file_path)))
        with open(os.path.join(output_path, file_path), "w") as outfile:
            outfile.write("[\n" + _RECORD + "]\n")

    monkeypatch.setattr(runner, "_execute_and_collect_output", execute)
    monkeypatch.setattr(runner, "retrieve_files", retrieve)

    def run(settings: RunSettings) -> int:
        return runner.run(
            iso_path="tester.iso",
            work_path=str(tmp_path),
            inputs_path=str(tmp_path / "inputs"),
            results_path=str(results_dir),
            xemu_path="xemu",
            reset_hdd=lambda _hdd: None,
            hdd_path=str(tmp_path / "hdd.img"),
            machine_token="machine",
            settings=settings,
        )

    return SimpleNamespace(launches=launches, run=run, results_dir=results_dir)


def test_sustained_records_every_launch_with_its_own_machine_info(fake_run):
    fake_run.launches.extend([{"machine_info": "GL info"}, {"fail": True}])

    ret = fake_run.run(RunSettings(run_mode="sustained", renderers=("GL", "VK"), repeat=1))

    assert ret == 201
    (output_file,) = fake_run.results_dir.iterdir()
    results = json.loads(output_file.read_text())
    assert results["xemu_version"] == "xemu-test"
    assert results["machine_info"] == {"cpu": "test"}
    assert "xemu_machine_info" not in results
    gl, vk = results["cells"]
    assert gl["renderer"] == "GL"
    assert gl["xemu_machine_info"] == "GL info"
    assert gl["host_noise"] == {"load_average": [0]}
    assert gl["results"][0]["name"] == "PfifoContention::Control"
    assert vk["renderer"] == "VK"
    assert vk["error"] == "Failed to execute emulator"
    assert vk["host_noise"] == {"load_average": [1]}
    assert vk["results"] == []


def test_single_mode_writes_each_launch_with_its_own_machine_info(fake_run):
    fake_run.launches.extend([{"machine_info": "GL info"}, {"machine_info": "VK info"}])

    assert fake_run.run(RunSettings(renderers=("GL", "VK"))) == 0

    gl = json.loads((fake_run.results_dir / "machine-GL.json").read_text())
    vk = json.loads((fake_run.results_dir / "machine-VK.json").read_text())
    assert gl["xemu_machine_info"] == "GL info\n"
    assert vk["xemu_machine_info"] == "VK info\n"
    assert gl["machine_info"] == {"cpu": "test", "load_average": [0]}
    assert vk["machine_info"] == {"cpu": "test", "load_average": [1]}
    assert vk["renderer"] == "VK"


def test_renderer_that_xemu_switches_away_from_is_not_recorded(fake_run):
    fallback = "Warning: Configured renderer unavailable. Switching to OpenGL."
    fake_run.launches.extend(
        [
            {"machine_info": "GL info", "failure_info": fallback},
            {"machine_info": "GL info"},
            {"machine_info": "GL info"},
        ]
    )

    ret = fake_run.run(RunSettings(run_mode="sustained", renderers=("VK", "GL"), repeat=2))

    assert ret == 201
    (output_file,) = fake_run.results_dir.iterdir()
    cells = json.loads(output_file.read_text())["cells"]
    assert [(cell["renderer"], cell["repeat"]) for cell in cells] == [("VK", 0), ("VK", 1), ("GL", 0), ("GL", 1)]
    assert cells[0]["error"] == "xemu can't use the VK renderer and switched to another"
    assert cells[0]["results"] == []
    assert cells[1]["error"] == "Skipped: xemu can't use the VK renderer"
    assert "error" not in cells[2]
    assert "error" not in cells[3]


def test_parse_results_file(tmp_path):
    path = tmp_path / "results.txt"
    path.write_text("log line\n[\n" + _RECORD + "]\n")

    assert _parse_results_file(str(path)) == [{"name": "PfifoContention::Control", "busy_us": {"p50": 1, "p95": 2}}]


def test_parse_results_file_rejects_truncated_results(tmp_path):
    path = tmp_path / "results.txt"
    path.write_text("[\n" + _RECORD + _RECORD)

    with pytest.raises(ValueError, match="incomplete"):
        _parse_results_file(str(path))
