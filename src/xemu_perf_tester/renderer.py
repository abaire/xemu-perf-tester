#!/usr/bin/env python3

# ruff: noqa: T201 `print` found

from __future__ import annotations

import argparse
import glob
import importlib.resources as pkg_resources
import json
import logging
import os
import sys
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import altair as alt
import pandas as pd
from jinja2 import Environment, FileSystemLoader

if TYPE_CHECKING:
    from collections.abc import Collection

logger = logging.getLogger(__name__)


class IndexedResults:
    def __init__(self, flat_results: list[dict[str, Any]]):
        self.flat_results = flat_results
        self.results_by_xemu_version = defaultdict(list)
        self.results_by_os_system = defaultdict(list)
        self.results_by_renderer = defaultdict(list)
        self.results_by_test_version = defaultdict(list)
        self.results_by_gpu_renderer = defaultdict(list)
        self.results_by_gpu_vendor = defaultdict(list)
        self.results_by_cpu_manufacturer = defaultdict(list)

        self.flattened_for_altair = []
        for result in flat_results:
            machine_info = result["machine_info"]

            self.results_by_xemu_version[result["xemu_version"]].append(result)
            self.results_by_os_system[machine_info["os_system"]].append(result)
            self.results_by_renderer[result["renderer"]].append(result)
            self.results_by_test_version[result["iso"]].append(result)
            self.results_by_gpu_renderer[result["gpu_renderer"]].append(result)
            self.results_by_gpu_vendor[result["gpu_vendor"]].append(result)
            self.results_by_cpu_manufacturer[machine_info["cpu_manufacturer"]].append(result)

            for test_result in result.get("results", []):
                self.flattened_for_altair.append(
                    {
                        "test_suite": test_result["name"].split("::")[0] if "::" in test_result["name"] else "N/A",
                        "test_name": test_result["name"],
                        "average_us": test_result["average_us"],
                        "xemu_version": result["xemu_version"],
                        "renderer": result["renderer"],
                        "iso": result["iso"],
                        "os_system": machine_info["os_system"],
                        "os_release": machine_info["os_release"],
                        "os_version": machine_info["os_version"],
                        "os_machine_type": machine_info["os_machine_type"],
                        "cpu_manufacturer": machine_info["cpu_manufacturer"],
                        "gpu_vendor": result["gpu_vendor"],
                        "gpu_renderer": result["gpu_renderer"],
                        "gpu_gl_version": result["gpu_gl_version"],
                        "gpu_glsl_version": result["gpu_glsl_version"],
                    }
                )

        self.dataframe = pd.DataFrame(self.flattened_for_altair)

    def _create_chart(
        self, df: pd.DataFrame, title: str, x_field: str, color_field: str, tooltip_fields: list[str]
    ) -> alt.Chart:
        df["average_us"] = pd.to_numeric(df["average_us"], errors="coerce")
        df = df.dropna(subset=["average_us"])

        if df.empty:
            logger.warning("No data for chart: %s (x_field: %s, color_field: %s)", title, x_field, color_field)
            return (
                alt.Chart(pd.DataFrame({"text": ["No data to display."]}))
                .mark_text(text="text")
                .properties(title=title)
            )

        base_tooltip = [
            alt.Tooltip("test_name:N", title="Test"),
            alt.Tooltip("average_us:Q", title="Avg Latency (us)", format=".2f"),
        ]

        dynamic_tooltip = []
        for field in tooltip_fields:
            if field != "test_name" and field != "average_us" and field in df.columns:
                field_type = "N"
                if df[field].dtype in ["int64", "float64"]:
                    field_type = "Q"
                dynamic_tooltip.append(alt.Tooltip(f"{field}:{field_type}", title=field.replace("_", " ").title()))

        final_tooltips_map = {t.shorthand.split(":")[0]: t for t in base_tooltip + dynamic_tooltip}
        final_tooltips = list(final_tooltips_map.values())

        return (
            alt.Chart(df)
            .mark_point()
            .encode(
                x=alt.X(f"{x_field}:N", title=x_field.replace("_", " ").title()),
                y=alt.Y("average(average_us):Q", title="Average Latency (us)"),
                color=alt.Color(f"{color_field}:N", title=color_field.replace("_", " ").title()),
                tooltip=final_tooltips,
            )
            .properties(title=title)
            .interactive()
        )

    def render(self, output_path: str):
        template_dir_path = pkg_resources.files("xemu_perf_tester") / "templates"
        env = Environment(loader=FileSystemLoader(str(template_dir_path)), autoescape=True)
        template = env.get_template("report_template.html.jinja2")

        charts_data = []
        chart_id_counter = 0

        if not self.dataframe.empty:
            avg_latency_by_cpu_manufacturer = (
                self.dataframe.groupby("cpu_manufacturer")["average_us"].mean().reset_index()
            )
            chart_id_counter += 1
            chart_id = f"chart-{chart_id_counter}"
            overall_chart = (
                alt.Chart(avg_latency_by_cpu_manufacturer)
                .mark_bar()
                .encode(
                    x=alt.X("cpu_manufacturer:N", title="Machine (CPU Manufacturer)"),
                    y=alt.Y("average_us:Q", title="Overall Avg Latency (us)"),
                    tooltip=[
                        alt.Tooltip("cpu_manufacturer:N", title="Machine"),
                        alt.Tooltip("average_us:Q", title="Overall Avg Latency (us)", format=".2f"),
                    ],
                )
                .properties(title="Overall Average Latency by Machine (CPU Manufacturer)")
                .interactive()
            )
            charts_data.append(
                {
                    "id": chart_id,
                    "title": "Overall Average Latency by Machine (CPU Manufacturer)",
                    "json_spec": overall_chart.to_dict(),
                }
            )

        unique_test_names: Collection[Any] = self.dataframe["test_name"].unique() if not self.dataframe.empty else []

        comparison_schemes = {
            "by_xemu_version": {
                "x_field": "xemu_version",
                "color_field": "xemu_version",
                "tooltip_extras": ["cpu_manufacturer", "renderer", "os_system", "gpu_vendor", "gpu_renderer"],
            },
            "by_os_system": {
                "x_field": "os_system",
                "color_field": "os_system",
                "tooltip_extras": ["cpu_manufacturer", "xemu_version", "renderer", "gpu_vendor", "gpu_renderer"],
            },
            "by_renderer": {
                "x_field": "renderer",
                "color_field": "renderer",
                "tooltip_extras": ["cpu_manufacturer", "xemu_version", "os_system", "gpu_vendor", "gpu_renderer"],
            },
            "by_gpu_renderer": {
                "x_field": "gpu_renderer",
                "color_field": "gpu_renderer",
                "tooltip_extras": ["cpu_manufacturer", "xemu_version", "os_system", "renderer"],
            },
            "by_gpu_vendor": {
                "x_field": "gpu_vendor",
                "color_field": "gpu_vendor",
                "tooltip_extras": ["cpu_manufacturer", "xemu_version", "os_system", "renderer", "gpu_renderer"],
            },
            "by_cpu_manufacturer": {
                "x_field": "cpu_manufacturer",
                "color_field": "cpu_manufacturer",
                "tooltip_extras": ["xemu_version", "renderer", "os_system", "gpu_vendor", "gpu_renderer"],
            },
        }

        for test_name in unique_test_names:
            logger.debug("Processing charts for test: '%s'", test_name)
            test_df = self.dataframe[self.dataframe["test_name"] == test_name].copy()  # Work on a copy

            for scheme_config in comparison_schemes.values():
                x_field: str = str(scheme_config["x_field"])
                color_field: str = str(scheme_config["color_field"])
                tooltip_extras = scheme_config["tooltip_extras"]

                if not test_df.empty:
                    chart_id_counter += 1
                    chart_id = f"chart-{chart_id_counter}"
                    chart_title = f"'{test_name}' by {x_field.replace('_', ' ').title()}"
                    effective_tooltip_fields = [f for f in tooltip_extras if f in test_df.columns]

                    chart = self._create_chart(
                        test_df,
                        chart_title,
                        x_field=x_field,
                        color_field=color_field,
                        tooltip_fields=effective_tooltip_fields,
                    )
                    charts_data.append({"id": chart_id, "title": chart_title, "json_spec": chart.to_dict()})

        html_output = template.render(
            title="xemu perf tester results",
            total_results=len(self.flat_results),
            xemu_versions=sorted(self.results_by_xemu_version.keys()),
            os_systems=sorted(self.results_by_os_system.keys()),
            renderers=sorted(self.results_by_renderer.keys()),
            test_versions=sorted(self.results_by_test_version.keys()),
            gpus=sorted(self.results_by_gpu_renderer.keys()),
            gpu_vendors=sorted(self.results_by_gpu_vendor.keys()),
            cpu_manufacturers=sorted(self.results_by_cpu_manufacturer.keys()),
            charts=charts_data,
        )

        with open(output_path, "w") as f:
            f.write(html_output)
        logger.debug("Generated HTML report: %s", output_path)


def _expand_gpu_info(result: dict[str, Any]):
    result["gpu_vendor"] = None
    result["gpu_renderer"] = None
    result["gpu_gl_version"] = None
    result["gpu_glsl_version"] = None

    for line in result["xemu_machine_info"].splitlines():
        key, value = line.split(": ", maxsplit=1)
        if key == "GL_VENDOR":
            result["gpu_vendor"] = value
        elif key == "GL_RENDERER":
            result["gpu_renderer"] = value
        elif key == "GL_VERSION":
            result["gpu_gl_version"] = value
        elif key == "GL_SHADING_LANGUAGE_VERSION":
            result["gpu_glsl_version"] = value


def load_results(results_dir: str) -> list[dict[str, Any]]:
    results_dir = os.path.abspath(os.path.expanduser(results_dir))

    results = []
    for result_file in glob.glob("**/*.json", root_dir=results_dir, recursive=True):
        with open(os.path.join(results_dir, result_file), "rb") as infile:
            result = json.load(infile)
            _expand_gpu_info(result)
            results.append(result)

    return results


def entrypoint():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--verbose",
        "-v",
        help="Enables verbose logging information",
        action="store_true",
    )
    parser.add_argument("--output", "-o", default="results.html", help="Output HTML file")
    parser.add_argument(
        "results",
        help="Path to the root of the results to process.",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level)

    if not os.path.isdir(args.results):
        logger.error("Results directory '%s' does not exist", args.results)
        return 1

    results = IndexedResults(load_results(args.results))

    results.render(args.output)

    return 0


if __name__ == "__main__":
    sys.exit(entrypoint())
