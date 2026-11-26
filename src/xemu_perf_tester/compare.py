"""Compares sustained benchmark results against the hardware baseline and, optionally, against another xemu build."""

# ruff: noqa: T201 `print` found

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any

CellKey = tuple[str, bool]


@dataclass
class RepeatStats:
    """One test's measurements in one host configuration, gathered across repeated launches."""

    loads: set[int] = field(default_factory=set)
    busy_p50_us: list[float] = field(default_factory=list)
    busy_p95_us: list[float] = field(default_factory=list)
    missed_vblanks: int = 0
    fence_timeouts: int = 0
    max_resets: int = 0
    aborted: int = 0

    @property
    def repeats(self) -> int:
        return len(self.busy_p95_us)

    @property
    def median_p50_us(self) -> float:
        return statistics.median(self.busy_p50_us)

    @property
    def median_p95_us(self) -> float:
        return statistics.median(self.busy_p95_us)

    @property
    def spread_p95_us(self) -> float:
        """The range of busy p95 across repeats."""
        return max(self.busy_p95_us) - min(self.busy_p95_us)


@dataclass
class CellStats:
    """Every test's measurements in one host configuration, and how many of its launches failed."""

    launches: int = 0
    failed_launches: int = 0
    tests: dict[str, RepeatStats] = field(default_factory=dict)


def is_sustained_results(results: dict[str, Any]) -> bool:
    """Returns whether `results` is the output of a sustained run, rather than of a single-mode run."""
    return isinstance(results.get("cells"), list)


def aggregate(results: dict[str, Any]) -> dict[CellKey, CellStats]:
    """Groups a sustained results file's records by (renderer, vsync) and test name.

    Records without sustained measurements are ignored.
    """
    ret: dict[CellKey, CellStats] = {}
    for cell in results.get("cells", []):
        key = (cell["renderer"], bool(cell["vsync"]))
        stats = ret.setdefault(key, CellStats())
        stats.launches += 1
        if cell.get("error"):
            stats.failed_launches += 1
        for record in cell.get("results", []):
            busy = record.get("busy_us")
            if not busy or not record.get("frames"):
                continue
            entry = stats.tests.setdefault(record["name"], RepeatStats())
            if "load" in record:
                entry.loads.add(record["load"])
            entry.busy_p50_us.append(busy["p50"])
            entry.busy_p95_us.append(busy["p95"])
            entry.missed_vblanks += record.get("missed_vblanks", 0)
            entry.fence_timeouts += record.get("fence_timeouts", 0)
            entry.max_resets = max(entry.max_resets, record.get("resets", {}).get("max", 0))
            if record.get("aborted"):
                entry.aborted += 1
    return ret


def _single_load(loads: set[int]) -> int | None:
    return next(iter(loads)) if len(loads) == 1 else None


def _empty_row(name: str) -> dict[str, Any]:
    return {
        "test": name,
        "load": None,
        "repeats": 0,
        "busy_p50_us": None,
        "busy_p95_us": None,
        "spread_p95_us": None,
        "missed_vblanks": None,
        "fence_timeouts": None,
        "max_resets": None,
        "vs_hw": None,
        "delta_p95_us": None,
        "delta_p95_pct": None,
        "significant": None,
    }


def compare_test(
    name: str,
    candidate: RepeatStats | None,
    baseline: RepeatStats | None,
    hardware: dict[str, Any] | None,
) -> dict[str, Any]:
    """Returns one report row for a test in one host configuration.

    `candidate` is None for a test that only the baseline has results for.
    """
    notes: list[str] = []
    row = _empty_row(name)

    if hardware and hardware.get("status") not in (None, "ok"):
        notes.append(f"HW calibration status '{hardware['status']}'")

    if baseline is not None:
        if baseline.fence_timeouts:
            notes.append("baseline fence timeouts")
        if baseline.aborted:
            notes.append(f"baseline aborted in {baseline.aborted} repeat(s)")

    if candidate is None:
        notes.insert(0, "no candidate results")
        row["notes"] = notes
        return row

    load = _single_load(candidate.loads)
    if len(candidate.loads) > 1:
        notes.append("load varies between repeats")

    row.update(
        {
            "load": load,
            "repeats": candidate.repeats,
            "busy_p50_us": candidate.median_p50_us,
            "busy_p95_us": candidate.median_p95_us,
            "spread_p95_us": candidate.spread_p95_us,
            "missed_vblanks": candidate.missed_vblanks,
            "fence_timeouts": candidate.fence_timeouts,
            "max_resets": candidate.max_resets,
        }
    )

    if candidate.fence_timeouts:
        notes.append("fence timeouts")
    if candidate.aborted:
        notes.append(f"aborted in {candidate.aborted} repeat(s)")

    if hardware and hardware.get("busy_p95_us"):
        row["vs_hw"] = candidate.median_p95_us / hardware["busy_p95_us"]
        hardware_load = hardware.get("load")
        if load is not None and hardware_load is not None and hardware_load != load:
            notes.append(f"load differs from HW ({hardware_load})")

    if baseline is not None and baseline.repeats:
        delta = candidate.median_p95_us - baseline.median_p95_us
        row["delta_p95_us"] = delta
        row["delta_p95_pct"] = 100.0 * delta / baseline.median_p95_us if baseline.median_p95_us else None
        if candidate.repeats > 1 and baseline.repeats > 1:
            row["significant"] = abs(delta) > candidate.spread_p95_us + baseline.spread_p95_us
        if candidate.loads != baseline.loads:
            notes.append("load differs from baseline")

    row["notes"] = notes
    return row


def _launch_notes(cell: CellStats | None, label: str) -> list[str]:
    if cell is None:
        return [f"no {label} launches"]
    if cell.failed_launches:
        return [f"{cell.failed_launches} of {cell.launches} {label} launches failed"]
    return []


def compare(candidate: dict[str, Any], baseline: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Returns the report: one entry per (renderer, vsync) host configuration in either file, each with its rows.

    A test that only one of the files has results for still gets a row, so that a failed launch or test is visible.
    """
    hardware_tests = (candidate.get("hardware_baseline") or {}).get("tests", {})
    candidate_cells = aggregate(candidate)
    baseline_cells = aggregate(baseline) if baseline else {}

    report = []
    for key in sorted(set(candidate_cells) | set(baseline_cells), key=lambda k: (k[0], not k[1])):
        renderer, vsync = key
        candidate_cell = candidate_cells.get(key)
        baseline_cell = baseline_cells.get(key)
        candidate_tests = candidate_cell.tests if candidate_cell else {}
        baseline_tests = baseline_cell.tests if baseline_cell else {}

        notes = _launch_notes(candidate_cell, "candidate")
        if baseline:
            notes.extend(_launch_notes(baseline_cell, "baseline"))

        rows = []
        for name in sorted(set(candidate_tests) | set(baseline_tests)):
            row = compare_test(name, candidate_tests.get(name), baseline_tests.get(name), hardware_tests.get(name))
            if baseline and baseline_cell and name in candidate_tests and name not in baseline_tests:
                row["notes"].append("no baseline results")
            rows.append(row)

        report.append(
            {
                "renderer": renderer,
                "vsync": vsync,
                "launches": candidate_cell.launches if candidate_cell else 0,
                "failed_launches": candidate_cell.failed_launches if candidate_cell else 0,
                "notes": notes,
                "rows": rows,
            }
        )
    return report


def file_warnings(candidate: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    """Returns warnings about differences between the files that make their results hard to compare."""
    if not baseline:
        return []

    ret = []
    if candidate.get("machine_token") != baseline.get("machine_token"):
        ret.append("The candidate and baseline were run on different machines.")
    if candidate.get("iso") != baseline.get("iso"):
        ret.append(
            f"The candidate and baseline were run with different ISOs (`{candidate.get('iso')}`, "
            f"`{baseline.get('iso')}`)."
        )
    return ret


def _ms(value_us: float | None) -> str:
    return "" if value_us is None else f"{value_us / 1000:.2f}"


def _blank_if_none(value: Any) -> str:
    return "" if value is None else str(value)


def render_markdown(report: list[dict[str, Any]], candidate: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    lines = [f"Candidate: `{candidate.get('xemu_version', 'unknown')}`"]
    if baseline:
        lines.append(f"Baseline: `{baseline.get('xemu_version', 'unknown')}`")
    if not (candidate.get("hardware_baseline") or {}).get("tests"):
        lines.append("No hardware baseline available.")
    lines.extend(f"**Warning:** {warning}" for warning in file_warnings(candidate, baseline))
    lines.append("")
    lines.append("Times are busy time in ms: the median over repeats of each launch's p50 and p95.")
    lines.append("`vs HW` is busy p95 divided by the hardware's; 1.00 or less meets the hardware.")
    lines.append("Missed vblanks are the total over all repeats.")

    for cell in report:
        lines.extend(["", f"### {cell['renderer']}, vsync {'on' if cell['vsync'] else 'off'}", ""])
        if cell["notes"]:
            lines.extend(["**" + "; ".join(cell["notes"]) + "**", ""])

        header = "| Test | Load | Repeats | p50 | p95 | p95 spread | vs HW |"
        divider = "|---|---:|---:|---:|---:|---:|---:|"
        if baseline:
            header += " p95 change | Change % | Significant |"
            divider += "---:|---:|:---:|"
        header += " Missed vblanks | Notes |"
        divider += "---:|---|"
        lines.extend([header, divider])

        for row in cell["rows"]:
            vs_hw = "" if row["vs_hw"] is None else f"{row['vs_hw']:.2f}"
            line = (
                f"| {row['test']} | {_blank_if_none(row['load'])} | {row['repeats']} | {_ms(row['busy_p50_us'])} "
                f"| {_ms(row['busy_p95_us'])} | {_ms(row['spread_p95_us'])} | {vs_hw} |"
            )
            if baseline:
                delta = "" if row["delta_p95_us"] is None else f"{row['delta_p95_us'] / 1000:+.2f}"
                pct = "" if row["delta_p95_pct"] is None else f"{row['delta_p95_pct']:+.1f}"
                significant = {True: "yes", False: "no", None: "n/a"}[row["significant"]]
                line += f" {delta} | {pct} | {significant} |"
            line += f" {_blank_if_none(row['missed_vblanks'])} | {'; '.join(row['notes'])} |"
            lines.append(line)

    return "\n".join(lines) + "\n"


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as infile:
        return json.load(infile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reports sustained xemu-perf-run results against the hardware baseline and, optionally, against "
        "the results of another xemu build. A delta is significant only when it is larger than the spread of both "
        "builds' repeats combined, which needs at least 2 repeats on each side."
    )
    parser.add_argument("candidate", help="Sustained results JSON file to report on.")
    parser.add_argument("baseline", nargs="?", help="Sustained results JSON file to compare the candidate against.")
    parser.add_argument("--format", choices=["md", "json"], default="md", help="Output format.")
    parser.add_argument("--output", "-o", help="File to write the report to, instead of stdout.")
    args = parser.parse_args(argv)

    candidate = _load(args.candidate)
    baseline = _load(args.baseline) if args.baseline else None
    for path, results in ((args.candidate, candidate), (args.baseline, baseline)):
        if results is not None and not is_sustained_results(results):
            print(f"'{path}' is not the results of a sustained run", file=sys.stderr)
            return 1

    report = compare(candidate, baseline)

    if args.format == "json":
        text = json.dumps(
            {
                "candidate": candidate.get("xemu_version"),
                "baseline": baseline.get("xemu_version") if baseline else None,
                "warnings": file_warnings(candidate, baseline),
                "cells": report,
            },
            indent=2,
        )
    else:
        text = render_markdown(report, candidate, baseline)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as outfile:
            outfile.write(text)
    else:
        print(text, end="")
    return 0


def execute() -> None:
    sys.exit(main())


if __name__ == "__main__":
    execute()
