from __future__ import annotations

import shlex
import sys

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from xemu_perf_tester.util import xemu
from xemu_perf_tester.util.xemu import generate_xemu_toml


def _generate(tmp_path, *, use_vulkan: bool, vsync: bool) -> dict:
    toml_path = tmp_path / "xemu.toml"
    generate_xemu_toml(
        str(toml_path),
        bootrom_path="/inputs/mcpx.bin",
        flashrom_path="/inputs/bios.bin",
        eeprom_path="/inputs/eeprom.bin",
        hdd_path="/cache/hdd.img",
        use_vulkan=use_vulkan,
        vsync=vsync,
    )
    with open(toml_path, "rb") as infile:
        return tomllib.load(infile)


@pytest.mark.parametrize(("use_vulkan", "renderer"), [(False, "OPENGL"), (True, "VULKAN")])
@pytest.mark.parametrize("vsync", [True, False])
def test_pins_host_settings(tmp_path, use_vulkan, renderer, vsync):
    config = _generate(tmp_path, use_vulkan=use_vulkan, vsync=vsync)

    display = config["display"]
    assert display["renderer"] == renderer
    assert display["quality"]["surface_scale"] == 1
    assert display["window"] == {"fullscreen_on_startup": False, "startup_size": "1280x960", "vsync": vsync}
    assert display["ui"] == {"show_menubar": False, "show_notifications": False, "fit": "scale"}
    assert config["perf"]["cache_shaders"] is False


def test_keeps_system_files(tmp_path):
    config = _generate(tmp_path, use_vulkan=False, vsync=True)

    assert config["sys"]["mem_limit"] == "64"
    assert config["sys"]["files"]["hdd_path"] == "/cache/hdd.img"
    assert config["general"]["show_welcome"] is False


def test_emulator_command_passes_its_config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(xemu.platform, "system", lambda: "Linux")
    xemu_path = str(tmp_path / "xemu-dir" / "xemu")

    command, config_path = xemu.build_emulator_command(xemu_path)

    assert config_path == str(tmp_path / "xemu-dir" / "xemu.toml")
    assert shlex.split(command.replace("{ISO}", "test.iso")) == [
        xemu_path,
        "-config_path",
        config_path,
        "-dvd_path",
        "test.iso",
    ]
