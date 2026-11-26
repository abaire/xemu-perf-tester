from __future__ import annotations

import json

import pytest

from xemu_perf_tester.compare import aggregate, compare, main, render_markdown

_CONTROL = "PfifoContention::Control"
_LONG = "PfifoContention::LongPushbuffer"


def _record(name: str, p95: float, *, load: int = 100, missed_vblanks: int = 0, **extra) -> dict:
    return {
        "name": name,
        "mode": "sustained",
        "load": load,
        "frames": 240,
        "aborted": False,
        "missed_vblanks": missed_vblanks,
        "fence_timeouts": 0,
        "busy_us": {"p50": p95 - 1000, "p95": p95},
        "resets": {"max": 0},
        **extra,
    }


def _results(cells: list[tuple[str, bool, list[list[dict]]]], *, hardware: dict | None = None) -> dict:
    """Builds a results file from (renderer, vsync, [records of each repeat]) entries."""
    ret: dict = {"xemu_version": "xemu-test", "cells": []}
    for renderer, vsync, repeats in cells:
        for repeat, records in enumerate(repeats):
            ret["cells"].append({"renderer": renderer, "vsync": vsync, "repeat": repeat, "results": records})
    if hardware is not None:
        ret["hardware_baseline"] = {"tests": hardware}
    return ret


_HARDWARE = {_CONTROL: {"load": 100, "busy_p50_us": 9000, "busy_p95_us": 10000}}


def test_aggregate_groups_repeats_by_cell():
    results = _results(
        [
            ("GL", True, [[_record(_CONTROL, 12000)], [_record(_CONTROL, 14000, missed_vblanks=2)]]),
            ("GL", False, [[_record(_CONTROL, 20000)]]),
        ]
    )
    # A single-mode record has no busy_us and is ignored.
    results["cells"][0]["results"].append({"name": "Other::Test", "iterations": 10})

    cells = aggregate(results)

    assert set(cells) == {("GL", True), ("GL", False)}
    assert cells[("GL", True)].launches == 2
    assert cells[("GL", True)].failed_launches == 0
    control = cells[("GL", True)].tests[_CONTROL]
    assert control.busy_p95_us == [12000, 14000]
    assert control.median_p95_us == 13000
    assert control.spread_p95_us == 2000
    assert control.missed_vblanks == 2
    assert list(cells[("GL", True)].tests) == [_CONTROL]


def test_compare_against_hardware_only():
    candidate = _results([("GL", True, [[_record(_CONTROL, 15000)]])], hardware=_HARDWARE)

    (cell,) = compare(candidate)
    (row,) = cell["rows"]

    assert row["vs_hw"] == pytest.approx(1.5)
    assert row["delta_p95_us"] is None
    assert row["significant"] is None
    assert row["notes"] == []


def test_flags_load_that_differs_from_hardware():
    candidate = _results([("GL", True, [[_record(_CONTROL, 15000, load=50)]])], hardware=_HARDWARE)

    (row,) = compare(candidate)[0]["rows"]

    assert row["notes"] == ["load differs from HW (100)"]


def test_significance_uses_both_spreads():
    baseline = _results([("GL", True, [[_record(_CONTROL, 20000)], [_record(_CONTROL, 20400)]])])
    # Medians differ by 600, spreads sum to 400 + 100.
    improved = _results([("GL", True, [[_record(_CONTROL, 19600)], [_record(_CONTROL, 19700)]])])
    # Medians differ by 400, which is within the combined spread of 400 + 100.
    noisy = _results([("GL", True, [[_record(_CONTROL, 19800)], [_record(_CONTROL, 19900)]])])

    (row,) = compare(improved, baseline)[0]["rows"]
    assert row["delta_p95_us"] == pytest.approx(-550)
    assert row["delta_p95_pct"] == pytest.approx(-550 / 20200 * 100)
    assert row["significant"] is True

    (row,) = compare(noisy, baseline)[0]["rows"]
    assert row["significant"] is False


def test_single_repeat_has_no_significance():
    baseline = _results([("GL", True, [[_record(_CONTROL, 20000)]])])
    candidate = _results([("GL", True, [[_record(_CONTROL, 10000)]])])

    (row,) = compare(candidate, baseline)[0]["rows"]

    assert row["delta_p95_us"] == -10000
    assert row["significant"] is None


def test_flags_load_that_differs_from_baseline():
    baseline = _results([("GL", True, [[_record(_CONTROL, 20000, load=200)]])])
    candidate = _results([("GL", True, [[_record(_CONTROL, 20000)]])])

    (row,) = compare(candidate, baseline)[0]["rows"]

    assert "load differs from baseline" in row["notes"]


def test_cells_sorted_with_vsync_on_first():
    candidate = _results(
        [
            ("VK", False, [[_record(_CONTROL, 1)]]),
            ("GL", False, [[_record(_CONTROL, 1)]]),
            ("VK", True, [[_record(_CONTROL, 1)]]),
            ("GL", True, [[_record(_CONTROL, 1), _record(_LONG, 1)]]),
        ]
    )

    report = compare(candidate)

    assert [(cell["renderer"], cell["vsync"]) for cell in report] == [
        ("GL", True),
        ("GL", False),
        ("VK", True),
        ("VK", False),
    ]
    assert [row["test"] for row in report[0]["rows"]] == [_CONTROL, _LONG]


def test_markdown_includes_delta_columns_only_with_baseline():
    candidate = _results([("GL", True, [[_record(_CONTROL, 15000)]])], hardware=_HARDWARE)

    alone = render_markdown(compare(candidate), candidate, None)
    assert "### GL, vsync on" in alone
    assert "| PfifoContention::Control | 100 | 1 | 14.00 | 15.00 | 0.00 | 1.50 | 0 |  |" in alone
    assert "p95 change" not in alone
    assert alone.isascii()

    against_self = render_markdown(compare(candidate, candidate), candidate, candidate)
    assert "| +0.00 | +0.0 | n/a |" in against_self


def test_main_writes_json(tmp_path):
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(_results([("GL", True, [[_record(_CONTROL, 15000)]])], hardware=_HARDWARE)))
    output_path = tmp_path / "report.json"

    assert main([str(candidate_path), "--format", "json", "--output", str(output_path)]) == 0

    report = json.loads(output_path.read_text())
    assert report["candidate"] == "xemu-test"
    assert report["cells"][0]["rows"][0]["vs_hw"] == pytest.approx(1.5)


def test_notes_failed_launches():
    candidate = _results([("GL", True, [[_record(_CONTROL, 15000)], []])])
    candidate["cells"][1]["error"] = "Failed to execute emulator"

    (cell,) = compare(candidate)

    assert cell["launches"] == 2
    assert cell["failed_launches"] == 1
    assert cell["notes"] == ["1 of 2 candidate launches failed"]
    assert "**1 of 2 candidate launches failed**" in render_markdown([cell], candidate, None)


def test_cell_where_every_launch_failed_is_reported():
    candidate = _results([("GL", True, [[]])])
    candidate["cells"][0]["error"] = "Failed to execute emulator"

    (cell,) = compare(candidate)

    assert cell["notes"] == ["1 of 1 candidate launches failed"]
    assert cell["rows"] == []


def test_reports_tests_and_cells_missing_from_candidate():
    baseline = _results(
        [
            ("GL", True, [[_record(_CONTROL, 20000), _record(_LONG, 20000)]]),
            ("VK", True, [[_record(_CONTROL, 20000)]]),
        ]
    )
    candidate = _results([("GL", True, [[_record(_CONTROL, 19000)]])])

    gl, vk = compare(candidate, baseline)

    long_row = gl["rows"][1]
    assert long_row["test"] == _LONG
    assert long_row["busy_p95_us"] is None
    assert long_row["notes"] == ["no candidate results"]
    assert vk["notes"] == ["no candidate launches"]
    assert vk["rows"][0]["notes"] == ["no candidate results"]

    text = render_markdown([gl, vk], candidate, baseline)
    assert f"| {_LONG} |  | 0 |  |  |  |  |  |  | n/a |  | no candidate results |" in text


def test_reports_tests_missing_from_baseline():
    baseline = _results([("GL", True, [[_record(_CONTROL, 20000)]])])
    candidate = _results([("GL", True, [[_record(_CONTROL, 19000), _record(_LONG, 19000)]])])

    (cell,) = compare(candidate, baseline)

    assert cell["rows"][1]["notes"] == ["no baseline results"]


def test_notes_baseline_problems_and_hardware_status():
    baseline = _results([("GL", True, [[_record(_CONTROL, 20000, aborted=True, fence_timeouts=1)]])])
    candidate = _results(
        [("GL", True, [[_record(_CONTROL, 19000)]])],
        hardware={_CONTROL: {"status": "unreachable", "busy_p95_us": 10000}},
    )

    (row,) = compare(candidate, baseline)[0]["rows"]

    # The hardware has no load, so it can't be said to differ.
    assert row["notes"] == [
        "HW calibration status 'unreachable'",
        "baseline fence timeouts",
        "baseline aborted in 1 repeat(s)",
    ]


def test_warns_about_different_machines_and_isos():
    baseline = _results([("GL", True, [[_record(_CONTROL, 20000)]])])
    baseline.update({"machine_token": "a", "iso": "a.iso"})
    candidate = _results([("GL", True, [[_record(_CONTROL, 19000)]])])
    candidate.update({"machine_token": "b", "iso": "b.iso"})

    text = render_markdown(compare(candidate, baseline), candidate, baseline)

    assert "**Warning:** The candidate and baseline were run on different machines." in text
    assert "**Warning:** The candidate and baseline were run with different ISOs (`b.iso`, `a.iso`)." in text
    assert text.isascii()


def test_main_rejects_single_mode_results(tmp_path, capsys):
    path = tmp_path / "single.json"
    path.write_text(json.dumps({"xemu_version": "xemu-test", "results": []}))

    assert main([str(path)]) == 1
    assert "is not the results of a sustained run" in capsys.readouterr().err


def test_main_writes_markdown_with_baseline(tmp_path):
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(_results([("GL", True, [[_record(_CONTROL, 15000)]])])))
    output_path = tmp_path / "report.md"

    assert main([str(results_path), str(results_path), "--output", str(output_path)]) == 0

    assert "| +0.00 | +0.0 | n/a |" in output_path.read_text(encoding="utf-8")
