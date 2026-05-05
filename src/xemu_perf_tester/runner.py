#!/usr/bin/env python3

# ruff: noqa: PLC0415 `import` should be at the top-level of a file
# ruff: noqa: T201 `print` found
# ruff: noqa: S105 Possible hardcoded password
# ruff: noqa: PLR2004 Magic value used in comparison
# ruff: noqa: BLE001 Do not catch blind exception
# ruff: noqa: RUF012 Mutable class attributes should be annotated with `typing.ClassVar`
# ruff: noqa: S607 Starting a process with a partial executable path

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
from shutil import SameFileError
from time import sleep
from typing import TYPE_CHECKING, Any

import cpuinfo
import machineid
import psutil
import requests
from nxdk_pgraph_test_runner import Config
from nxdk_pgraph_test_runner.emulator_output import EmulatorOutput
from platformdirs import user_config_path
from python_xiso_repacker import ensure_extract_xiso, extract_file

from xemu_perf_tester.util.blocklist import BlockList
from xemu_perf_tester.util.github import download_artifact, fetch_github_release_info
from xemu_perf_tester.util.hdd import retrieve_files, setup_xemu_hdd_image
from xemu_perf_tester.util.perf_tester_config import XemuPerfTesterConfigManager
from xemu_perf_tester.util.xemu import (
    build_emulator_command,
    copy_xemu_inputs,
    download_xemu,
    ensure_cache_path,
    ensure_path,
    ensure_results_path,
    generate_xemu_toml,
)

if TYPE_CHECKING:
    from collections.abc import Collection

logger = logging.getLogger(__name__)

_MODIFIED_TESTER_ISO = "updated_tester_iso.iso"

_SUBMISSION_CLIENT_ID = "Ov23liIyQD02yI5OZZCV"
_SUBMISSION_REPO = "abaire/xemu-perf-tester_results"


def _download_tester_iso(output_dir: str, tag: str = "latest", github_api_token: str | None = None) -> str | None:
    logger.info("Fetching info on xemu-perf-tests ISO at release tag %s...", tag)

    auth_header = {"Authorization": f"token {github_api_token}"} if github_api_token else None
    release_info = fetch_github_release_info(
        "https://api.github.com/repos/abaire/xemu-perf-tests", tag, additional_headers=auth_header
    )
    if not release_info:
        return None

    release_tag = release_info.get("tag_name")
    if not release_tag:
        logger.error("Failed to retrieve release tag from GitHub.")
        return None

    download_url = ""
    for asset in release_info.get("assets", []):
        if not asset.get("name", "").endswith(".iso"):
            continue
        download_url = asset.get("browser_download_url", "")
        break

    if not download_url:
        logger.error("Failed to fetch download URL for latest xemu-perf-tests release")
        return None

    target_file = os.path.join(output_dir, f"xemu-perf-tests-{release_tag}.iso")
    download_artifact(target_file, download_url, additional_headers=auth_header)

    return target_file


def _determine_xemu_info(results_path: str, emulator_command: str) -> tuple[str, str]:
    """Returns the output directory and xemu version."""
    command = Config(emulator_command=emulator_command).build_emulator_command("__this_file_does_not_exist")
    stderr: str | None
    try:
        logger.debug("Fetching xemu info '%s'", command)
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=1, env=os.environ.copy())
        stderr = result.stderr
    except subprocess.TimeoutExpired as err:
        # Windows Python 3.13 returns a string rather than bytes.
        stderr = err.stderr.decode() if isinstance(err.stderr, bytes) else err.stderr

        # Give tne GL subsystem time to settle after the hard kill. Prevents deadlock in get_output_directory.
        sleep(0.5)
    except subprocess.CalledProcessError as err:
        stderr = err.stderr.decode() if isinstance(err.stderr, bytes) else err.stderr
        logger.error(stderr)  # noqa: TRY400 Use `logging.exception` instead of `logging.error`
        logger.exception(err)  # noqa: TRY401 Redundant exception object included in `logging.exception` call
        raise

    if stderr is None:
        stderr = ""
    emulator_output = EmulatorOutput.parse(stdout=[], stderr=stderr.split("\n"))

    return os.path.join(
        results_path,
        emulator_output.emulator_version,
    ), emulator_output.emulator_version


def _configure_iso(config: Config, block_list: BlockList) -> str | None:
    manager = XemuPerfTesterConfigManager(config)
    iso_path = os.path.join(config.ensure_data_dir(), _MODIFIED_TESTER_ISO)
    if not manager.repack_iso_fresh(iso_path, tests_to_disable=block_list.disallowed_tests):
        return None

    return iso_path


def _execute_and_collect_output(config: Config, block_list: BlockList) -> EmulatorOutput | None:
    repacked_iso = _configure_iso(config, block_list)
    if not repacked_iso:
        logger.error("FATAL: Failed to repack tester ISO")
        return 1

    emulator_command = config.build_emulator_command(repacked_iso)
    manager = XemuPerfTesterConfigManager(config, repacked_iso)

    if config.suite_allowlist and not manager.repack_with_only_test_suites(set(config.suite_allowlist)):
        logger.error("FATAL: Failed to repack with allowlist suites %s", config.suite_allowlist)
        return None

    stderr = ""
    try:
        result = subprocess.run(
            emulator_command,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
            env=os.environ.copy(),
        )
        stderr = result.stderr
    except FileNotFoundError:
        logger.exception("Failed to execute emulator")
        return None
    except subprocess.TimeoutExpired as err:
        if err.stderr is not None:
            # Windows Python 3.13 returns a string rather than bytes.
            stderr = err.stderr.decode() if isinstance(err.stderr, bytes) else err.stderr
    except subprocess.CalledProcessError as err:
        if err.stderr is not None:
            stderr = err.stderr.decode() if isinstance(err.stderr, bytes) else err.stderr
        logger.error(stderr)  # noqa: TRY400 Use `logging.exception` instead of `logging.error`
        logger.exception(err)  # noqa: TRY401 Redundant exception object included in `logging.exception` call
        raise

    return EmulatorOutput.parse(stdout=[], stderr=stderr.split("\n"))


def _parse_results_file(results_file: str) -> dict[str, Any]:
    with open(results_file) as infile:
        json_lines: list[str] = []
        for line in infile:
            if json_lines or line == "[\n":
                json_lines.append(line)

            if line == "]\n":
                break

    # Remove the trailing comma from the last test result.
    json_lines[-2] = json_lines[-2].rstrip()[:-1]
    return json.loads("".join(json_lines))


def _get_display_refresh_rate() -> float | None:
    """Attempts to return the primary display refresh rate in Hz.

    Uses only stdlib ctypes on macOS and Windows. Falls back to parsing
    ``xrandr`` output on Linux (X11 only; returns None on Wayland).
    Returns None if the rate cannot be determined.
    """
    system = platform.system()

    if system == "Darwin":
        try:
            import ctypes
            import ctypes.util

            cg_lib = ctypes.util.find_library("CoreGraphics")
            if cg_lib is None:
                return None
            cg = ctypes.cdll.LoadLibrary(cg_lib)
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
            cg.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
            cg.CGDisplayModeGetRefreshRate.restype = ctypes.c_double
            cg.CGDisplayModeGetRefreshRate.argtypes = [ctypes.c_void_p]
            cg.CGDisplayModeRelease.argtypes = [ctypes.c_void_p]

            display_id = cg.CGMainDisplayID()
            mode = cg.CGDisplayCopyDisplayMode(display_id)
            if mode:
                rate = cg.CGDisplayModeGetRefreshRate(mode)
                cg.CGDisplayModeRelease(mode)
                if rate > 0:
                    return rate
        except Exception:
            logger.debug("(non-fatal): Failed to get display refresh rate on macOS", exc_info=True)

    elif system == "Windows":
        try:
            import ctypes
            import ctypes.wintypes

            enum_current_settings = -1

            class DEVMODEW(ctypes.Structure):
                _fields_ = [
                    ("_pad1", ctypes.c_byte * 68),
                    ("dmSize", ctypes.c_uint16),
                    ("_pad2", ctypes.c_byte * 114),
                    ("dmDisplayFrequency", ctypes.c_uint32),
                    ("_pad3", ctypes.c_byte * 32),
                ]

            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(DEVMODEW)
            windll = getattr(ctypes, "windll", None)
            if windll is None:
                return None
            if windll.user32.EnumDisplaySettingsW(None, enum_current_settings, ctypes.byref(dm)):
                freq = dm.dmDisplayFrequency
                if freq > 1:  # 0 and 1 are sentinel values meaning "default"
                    return float(freq)
        except Exception:
            logger.debug("(non-fatal): Failed to get display refresh rate on Windows", exc_info=True)

    elif system == "Linux":
        try:
            result = subprocess.run(["xrandr"], capture_output=True, text=True, timeout=5, check=False)
            for line in result.stdout.splitlines():
                if "*" not in line:
                    continue
                # Active mode line looks like:   1920x1080     60.00*+  50.00
                for token in line.split():
                    if "*" in token:
                        return float(token.replace("*", "").replace("+", ""))
        except Exception:
            logger.debug("(non-fatal): Failed to get display refresh rate on Linux", exc_info=True)

    return None


def _fetch_machine_info() -> dict[str, Any]:
    cpu_info = cpuinfo.get_cpu_info()
    ret = {
        "cpu_manufacturer": cpu_info.get("brand_raw", "N/A"),
        "cpu_vendor_id": cpu_info.get("vendor_id_raw", "N/A"),
        "cpu_stepping": cpu_info.get("stepping", "N/A"),
        "cpu_model": cpu_info.get("model", "N/A"),
        "cpu_family": cpu_info.get("family", "N/A"),
        "os_system": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "os_machine_type": platform.machine(),
    }

    try:
        cpu_cores = psutil.cpu_count(logical=False)
        if cpu_cores:
            ret["cpu_freq_max"] = cpu_cores
    except (SystemError, RuntimeError):
        logger.exception(" (non-fatal): psutil failed to retrieve CPU core count")

    try:
        num_threads = psutil.cpu_count(logical=False)
        if num_threads:
            ret["hw_threads"] = num_threads
    except (SystemError, RuntimeError):
        logger.exception(" (non-fatal): psutil failed to retrieve CPU HW thread count")

    try:
        cpu_freq = psutil.cpu_freq()
        if cpu_freq:
            ret["cpu_freq_max"] = cpu_freq.max
    except (SystemError, RuntimeError):
        logger.exception(" (non-fatal): psutil failed to retrieve CPU frequency")

    refresh_rate = _get_display_refresh_rate()
    if refresh_rate is not None:
        ret["display_refresh_rate_hz"] = refresh_rate

    if platform.system() == "Darwin":  # macOS
        ret["os_macos_version"] = platform.mac_ver()
    elif platform.system() == "Windows":
        ret["os_win_edition"] = platform.win32_edition()
        ret["os_win_version"] = platform.win32_ver()
    elif platform.system() == "Linux":
        import distro

        ret["os_linux_distro"] = distro.name(pretty=True)
        ret["os_linux_distro_id"] = distro.id()
        ret["os_linux_version"] = distro.version(best=True)

    return ret


def github_auth() -> Any:
    code_resp = requests.post(
        "https://github.com/login/device/code",
        data={
            "client_id": _SUBMISSION_CLIENT_ID,
            "scope": "public_repo",
        },
        headers={"Accept": "application/json"},
        timeout=10,
    ).json()

    print(f"\nTo submit results, please go to: {code_resp['verification_uri']}")
    print(f"Enter the code: {code_resp['user_code']}\n")
    print("Waiting for authorization...", end="", flush=True)

    token_url = "https://github.com/login/oauth/access_token"
    token_payload = {
        "client_id": _SUBMISSION_CLIENT_ID,
        "device_code": code_resp["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }

    token = None
    interval = code_resp.get("interval", 5)

    while not token:
        time.sleep(interval)
        auth_resp = requests.post(
            token_url, data=token_payload, headers={"Accept": "application/json"}, timeout=10
        ).json()

        if "access_token" in auth_resp:
            token = auth_resp["access_token"]
            print(" Authorized!")
        elif auth_resp.get("error") not in ("authorization_pending", "slow_down"):
            sys.exit(f"\nAuth failed: {auth_resp.get('error')}")

    return token


def _post_issue(token: str, results_xemu_version: str, encoded_payload: str) -> requests.Response:
    return requests.post(
        f"https://api.github.com/repos/{_SUBMISSION_REPO}/issues",
        json={
            "title": f"Benchmark result for xemu {results_xemu_version}",
            "body": f"```\n{encoded_payload}\n```",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )


def submit_results(results: dict):
    compact_json = json.dumps(results, separators=(",", ":"))
    compressed = zlib.compress(compact_json.encode(), level=zlib.Z_BEST_COMPRESSION)
    encoded_payload = base64.b64encode(compressed).decode()

    token_cache_file = user_config_path("xemu-perf-tester") / "github_token"
    token = token_cache_file.read_text().strip() if token_cache_file.exists() else None
    xemu_version = results.get("xemu_version", "UNKNOWN")
    if token:
        response = _post_issue(token, xemu_version, encoded_payload)
        if response.status_code == 401:
            token = None
    if not token:
        token = github_auth()
        token_cache_file.parent.mkdir(parents=True, exist_ok=True)
        token_cache_file.write_text(token)
        token_cache_file.chmod(0o600)
        response = _post_issue(token, xemu_version, encoded_payload)

    if response.status_code == 201:
        print("Successfully submitted benchmark results.")
    else:
        print(f"Failed to submit. Status: {response.status_code}\n{response.text}")


def _process_results(
    output_directory: str,
    iso_path: str,
    machine_token: str,
    just_suites: Collection[str] | None,
    results_file: str,
    emulator_output: EmulatorOutput,
    xemu_tag: str | None,
    *,
    use_vulkan: bool,
    upload_results: bool = False,
):
    renderer = "VK" if use_vulkan else "GL"

    results = {
        "iso": os.path.basename(iso_path),
        "suite_allowlist": just_suites,
        "xemu_version": emulator_output.emulator_version,
        "xemu_machine_info": emulator_output.machine_info + "\n" + emulator_output.failure_info,
        "machine_info": _fetch_machine_info(),
        "renderer": renderer,
        "results": _parse_results_file(results_file),
        "machine_token": machine_token,
    }

    if xemu_tag:
        results["xemu_tag"] = xemu_tag

    if upload_results:
        submit_results(results)
        return

    os.makedirs(output_directory, exist_ok=True)
    output_file = os.path.join(output_directory, f"{machine_token}-{renderer}.json")
    with open(output_file, "w") as outfile:
        json.dump(results, outfile, indent=2)


def run(
    iso_path: str,
    work_path: str,
    inputs_path: str,
    results_path: str,
    xemu_path: str,
    hdd_path: str,
    machine_token: str,
    just_suites: Collection[str] | None = None,
    block_list_file: str | None = None,
    xemu_tag: str | None = None,
    *,
    no_bundle: bool = False,
    use_vulkan: bool = False,
    upload_results: bool = False,
) -> int:
    emulator_command, portable_mode_config_path = build_emulator_command(xemu_path, no_bundle=no_bundle)
    if not emulator_command:
        return 1

    generate_xemu_toml(
        portable_mode_config_path,
        bootrom_path=os.path.join(inputs_path, "mcpx.bin"),
        flashrom_path=os.path.join(inputs_path, "bios.bin"),
        eeprom_path=os.path.join(inputs_path, "eeprom.bin"),
        hdd_path=hdd_path,
        use_vulkan=use_vulkan,
    )

    output_directory, xemu_version = _determine_xemu_info(results_path, emulator_command=emulator_command)

    config = Config(
        work_dir=work_path,
        output_dir=results_path,
        emulator_command=emulator_command,
        iso_path=iso_path,
        xbox_artifact_path=r"c:\xemu-perf-tests",
        suite_allowlist=just_suites,
    )

    block_list = BlockList(xemu_version, block_list_file=block_list_file)
    emulator_output = _execute_and_collect_output(config, block_list)
    if not emulator_output:
        return 201

    with tempfile.TemporaryDirectory() as temp_path:
        retrieve_files(hdd_path, temp_path, "c", "xemu-perf-tests/results.txt")

        _process_results(
            output_directory,
            iso_path,
            machine_token,
            just_suites,
            os.path.join(temp_path, "xemu-perf-tests", "results.txt"),
            emulator_output,
            use_vulkan=use_vulkan,
            xemu_tag=xemu_tag,
            upload_results=upload_results,
        )

    return 0


def _copy_files_from_xemu_toml(args):
    toml_path = args.import_install
    if not toml_path:
        msg = "Invalid state: _copy_files_from_xemu_toml called without xemu.toml path argument"
        raise RuntimeError(msg)

    copy_xemu_inputs(toml_path, "inputs")


def _setup_minimal_hdd(hdd: str, iso: str):
    if os.path.isfile(hdd):
        os.unlink(hdd)

    with tempfile.TemporaryDirectory() as temp_path:
        extract_xiso = ensure_extract_xiso(None)
        if not extract_xiso:
            msg = "extract-xiso is unavailable"
            raise NotImplementedError(msg)

        fake_dashboard = os.path.join(temp_path, "xboxdash.xbe")
        if not extract_file(iso, "default.xbe", fake_dashboard, extract_xiso):
            msg = f"Bad tester image '{iso}': no default.xbe"
            raise ValueError(msg)

        setup_xemu_hdd_image(hdd, fake_dashboard)


def entrypoint():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--import-install",
        help="Import settings from an existing xemu install",
        metavar="xemu_toml_path",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        help="Enables verbose logging information",
        action="store_true",
    )
    parser.add_argument("--iso", "-I", help="Path to the xemu-perf-tests.iso xiso file.")
    parser.add_argument(
        "--test-tag",
        metavar="github_release_tag",
        default="latest",
        help="Release tag to use when downloading xemu-perf-tests iso from GitHub.",
    )
    parser.add_argument("--xemu", "-X", help="Path to the xemu executable.")
    parser.add_argument(
        "--xemu-tag",
        metavar="github_release_tag",
        default="latest",
        help="Release tag to use when downloading xemu from GitHub.",
    )
    parser.add_argument(
        "--bios",
        "-B",
        default="inputs/bios.bin",
        help="Path to Xbox BIOS image to use.",
    )
    parser.add_argument(
        "--mcpx",
        "-M",
        default="inputs/mcpx.bin",
        help="Path to Xbox MCPX boot ROM image to use.",
    )
    parser.add_argument("--cache-path", "-C", default="cache", help="Path to persistent cache area.")
    parser.add_argument("--temp-path", help="Temporary path used during execution of tests")
    parser.add_argument(
        "--results-path",
        "-R",
        default="results",
        help="Path to directory into which results should be stored.",
    )
    parser.add_argument(
        "--no-bundle", action="store_true", help="Suppress attempt to set DYLD_FALLBACK_LIBRARY_PATH on macOS."
    )
    parser.add_argument("--use-vulkan", action="store_true", help="Use the Vulkan renderer instead of OpenGL.")
    parser.add_argument("--just-suites", nargs="+", help="Just run the given suites rather than the full test set.")
    parser.add_argument(
        "--block-list-file",
        metavar="block_list_json_file",
        help="Specify a block_list.json file used to restrict the set of tests based on host machine information.",
    )
    parser.add_argument(
        "--upload-results",
        "-U",
        action="store_true",
        help="Automatically uploads results to the results repository.",
    )

    parser.add_argument("--github-token", help="Github API token, only required for PR/action artifact fetching.")

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level)

    cache_path = ensure_cache_path(args.cache_path)
    results_path = ensure_results_path(args.results_path)

    if args.import_install:
        _copy_files_from_xemu_toml(args)
        logger.info("Configuration files copied")
        return 0

    mcpx_path = os.path.abspath(os.path.expanduser(args.mcpx))
    if not os.path.isfile(mcpx_path):
        logger.error("Missing required mcpx.bin file")
        return 1
    bios_path = os.path.abspath(os.path.expanduser(args.bios))
    if not os.path.isfile(bios_path):
        logger.error("Missing required bios.bin file")
        return 1

    iso = (
        os.path.abspath(os.path.expanduser(args.iso))
        if args.iso
        else _download_tester_iso(cache_path, args.test_tag, args.github_token)
    )
    if not iso or not os.path.isfile(iso):
        logger.error("Invalid ISO path '%s'", iso)
        return 1

    if args.xemu:
        xemu = os.path.abspath(os.path.expanduser(args.xemu))
        dev_dir = os.path.join(cache_path, "dev_xemu")
        os.makedirs(dev_dir, exist_ok=True)
        xemu_copy = os.path.join(dev_dir, os.path.basename(xemu))
        if os.path.isdir(xemu):
            shutil.copytree(xemu, xemu_copy, dirs_exist_ok=True)
        else:
            shutil.copy2(xemu, xemu_copy)
        xemu = xemu_copy
    else:
        xemu = download_xemu(cache_path, args.xemu_tag, args.github_token)

    if not xemu:
        logger.error("Failed to download xemu")
        return 1
    if not os.path.exists(xemu):
        logger.error("Invalid xemu path '%s'", xemu)
        return 1

    hdd = os.path.join(cache_path, "hdd.img")
    _setup_minimal_hdd(hdd, iso)

    machine_token = machineid.hashed_id("xemu-perf-tester")

    block_list_file = os.path.abspath(os.path.expanduser(args.block_list_file)) if args.block_list_file else None

    def _copy_inputs_and_run(temp_path: str) -> int:
        inputs_path = os.path.join(temp_path, "inputs")
        os.makedirs(inputs_path, exist_ok=True)
        with contextlib.suppress(SameFileError):
            shutil.copy(mcpx_path, os.path.join(inputs_path, "mcpx.bin"))
        with contextlib.suppress(SameFileError):
            shutil.copy(bios_path, os.path.join(inputs_path, "bios.bin"))
        return run(
            iso_path=iso,
            work_path=temp_path,
            inputs_path=inputs_path,
            results_path=results_path,
            xemu_path=xemu,
            hdd_path=hdd,
            machine_token=machine_token,
            no_bundle=args.no_bundle,
            use_vulkan=args.use_vulkan,
            just_suites=args.just_suites,
            block_list_file=block_list_file,
            xemu_tag=args.xemu_tag,
            upload_results=args.upload_results,
        )

    if args.temp_path:
        return _copy_inputs_and_run(ensure_path(args.temp_path))

    with tempfile.TemporaryDirectory() as temp_path:
        return _copy_inputs_and_run(ensure_path(temp_path))


if __name__ == "__main__":
    sys.exit(entrypoint())
