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
import datetime
import itertools
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
from dataclasses import dataclass
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
from xemu_perf_tester.util.github import (
    CiBuildInfo,
    CiBuildNotFoundError,
    download_artifact,
    fetch_ci_build_info,
    fetch_github_release_info,
    is_ci_build_url,
)
from xemu_perf_tester.util.hdd import retrieve_files, setup_xemu_hdd_image
from xemu_perf_tester.util.perf_tester_config import XemuPerfTesterConfigManager
from xemu_perf_tester.util.xemu import (
    XemuVersion,
    build_emulator_command,
    copy_xemu_inputs,
    download_xemu,
    download_xemu_ci_build,
    ensure_cache_path,
    ensure_path,
    ensure_results_path,
    generate_xemu_toml,
    pinned_host_settings,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

logger = logging.getLogger(__name__)

_MODIFIED_TESTER_ISO = "updated_tester_iso.iso"

_SUBMISSION_CLIENT_ID = "Ov23liIyQD02yI5OZZCV"
_SUBMISSION_REPO = "abaire/xemu-perf-tester_results"

_RUN_MODE_SINGLE = "single"
_RUN_MODE_SUSTAINED = "sustained"
_RENDERERS = ("GL", "VK")

# Settings implied by each --matrix preset. Flags given explicitly take precedence.
_MATRIX_PRESETS: dict[str, dict[str, Any]] = {
    "contention": {
        "run_mode": _RUN_MODE_SUSTAINED,
        "just_suites": ["PfifoContention"],
        "renderers": list(_RENDERERS),
        "vsync": [True, False],
    },
}

# Logged by xemu when the configured renderer isn't available in the build, before it switches to another one.
_RENDERER_UNAVAILABLE_MESSAGE = "Configured renderer unavailable"

_DEFAULT_REPEAT = {_RUN_MODE_SINGLE: 1, _RUN_MODE_SUSTAINED: 3}
# Sustained runs get a shorter timeout than the test runner's default, so that one hung launch doesn't stall a matrix.
_DEFAULT_SUSTAINED_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class RunSettings:
    """How the benchmark is run: the mode, the host configurations to run it under and how often to repeat it."""

    run_mode: str = _RUN_MODE_SINGLE
    renderers: tuple[str, ...] = ("GL",)
    vsync: tuple[bool, ...] = (True,)
    repeat: int = 1
    just_suites: tuple[str, ...] | None = None
    warmup_frames: int | None = None
    measure_frames: int | None = None
    timeout_seconds: int | None = None

    @property
    def cells(self) -> list[tuple[str, bool]]:
        """Returns every (renderer, vsync) host configuration to run."""
        return list(itertools.product(self.renderers, self.vsync))


def _parse_vsync(value: str) -> bool:
    if value == "on":
        return True
    if value == "off":
        return False
    msg = f"Invalid vsync value '{value}', expected 'on' or 'off'"
    raise ValueError(msg)


def resolve_run_settings(args: argparse.Namespace) -> RunSettings:
    """Combines the --matrix preset, if any, with the explicitly given flags.

    Raises ValueError if the combination is invalid.
    """
    preset: dict[str, Any] = _MATRIX_PRESETS[args.matrix] if args.matrix else {}

    run_mode = args.run_mode or preset.get("run_mode", _RUN_MODE_SINGLE)

    if args.renderers:
        renderers = [renderer.upper() for renderer in args.renderers]
    elif args.use_vulkan:
        renderers = ["VK"]
    else:
        renderers = preset.get("renderers", ["GL"])
    for renderer in renderers:
        if renderer not in _RENDERERS:
            msg = f"Invalid renderer '{renderer}', expected one of {', '.join(_RENDERERS)}"
            raise ValueError(msg)

    vsync = [_parse_vsync(value) for value in args.vsync] if args.vsync else preset.get("vsync", [True])

    repeat = args.repeat if args.repeat is not None else _DEFAULT_REPEAT[run_mode]
    if repeat < 1:
        msg = "--repeat must be at least 1"
        raise ValueError(msg)

    just_suites = args.just_suites or preset.get("just_suites")

    timeout_seconds = args.timeout_seconds
    if timeout_seconds is None and run_mode == _RUN_MODE_SUSTAINED:
        timeout_seconds = _DEFAULT_SUSTAINED_TIMEOUT_SECONDS
    if timeout_seconds is not None and timeout_seconds < 1:
        msg = "--timeout-seconds must be at least 1"
        raise ValueError(msg)

    if args.warmup_frames is not None and args.warmup_frames < 0:
        msg = "--warmup-frames must not be negative"
        raise ValueError(msg)
    if args.measure_frames is not None and args.measure_frames < 1:
        msg = "--measure-frames must be at least 1"
        raise ValueError(msg)

    vsync = list(dict.fromkeys(vsync))
    if args.upload_results:
        # Uploaded results must be comparable with each other, so only the standard configuration may be uploaded.
        if run_mode != _RUN_MODE_SINGLE:
            msg = "--upload-results is only supported in single run mode"
            raise ValueError(msg)
        if vsync != [True]:
            msg = "--upload-results is only supported with vsync on"
            raise ValueError(msg)
        if repeat != 1:
            msg = "--upload-results is only supported with --repeat 1"
            raise ValueError(msg)
        if args.warmup_frames is not None or args.measure_frames is not None:
            msg = "--upload-results is not supported with --warmup-frames or --measure-frames"
            raise ValueError(msg)

    return RunSettings(
        run_mode=run_mode,
        renderers=tuple(dict.fromkeys(renderers)),
        vsync=tuple(vsync),
        repeat=repeat,
        just_suites=tuple(just_suites) if just_suites else None,
        warmup_frames=args.warmup_frames,
        measure_frames=args.measure_frames,
        timeout_seconds=timeout_seconds,
    )


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


def _configure_iso(config: Config, block_list: BlockList, settings: RunSettings) -> str | None:
    """Repacks the tester ISO with the runner's settings and returns its path."""
    manager = XemuPerfTesterConfigManager(config)
    iso_path = os.path.join(config.ensure_data_dir(), _MODIFIED_TESTER_ISO)
    if not manager.repack_iso_fresh(
        iso_path,
        tests_to_disable=block_list.disallowed_tests,
        run_mode=settings.run_mode,
        warmup_frames=settings.warmup_frames,
        measure_frames=settings.measure_frames,
        suites_to_enable=settings.just_suites,
    ):
        logger.error("FATAL: Failed to repack tester ISO")
        return None

    return iso_path


def _execute_and_collect_output(config: Config, repacked_iso: str) -> tuple[EmulatorOutput | None, bool]:
    """Runs xemu until it exits or times out.

    Returns the parsed emulator output, or None if xemu could not be run, and whether xemu was killed for timing out.
    """
    emulator_command = config.build_emulator_command(repacked_iso)

    stderr = ""
    timed_out = False
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
        return None, False
    except subprocess.TimeoutExpired as err:
        logger.error("xemu was killed after running for %d seconds", config.timeout_seconds)  # noqa: TRY400 Use `logging.exception` instead of `logging.error`
        timed_out = True
        if err.stderr is not None:
            # Windows Python 3.13 returns a string rather than bytes.
            stderr = err.stderr.decode() if isinstance(err.stderr, bytes) else err.stderr
    except subprocess.CalledProcessError as err:
        if err.stderr is not None:
            stderr = err.stderr.decode() if isinstance(err.stderr, bytes) else err.stderr
        logger.error(stderr)  # noqa: TRY400 Use `logging.exception` instead of `logging.error`
        logger.exception(err)  # noqa: TRY401 Redundant exception object included in `logging.exception` call
        raise

    return EmulatorOutput.parse(stdout=[], stderr=stderr.split("\n")), timed_out


def _parse_results_file(results_file: str) -> list[Any]:
    with open(results_file) as infile:
        json_lines: list[str] = []
        for line in infile:
            if json_lines or line == "[\n":
                json_lines.append(line)

            if line == "]\n":
                break

    if len(json_lines) < 3:
        msg = f"No test results found in '{results_file}'"
        raise ValueError(msg)
    if json_lines[-1] != "]\n":
        msg = f"Test results in '{results_file}' are incomplete; xemu probably exited before the tests finished"
        raise ValueError(msg)

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


def _fetch_host_noise_info() -> dict[str, Any]:
    """Returns information about host conditions that can add noise to benchmark results."""
    ret: dict[str, Any] = {}

    try:
        ret["load_average"] = list(psutil.getloadavg())
    except (OSError, AttributeError):
        logger.debug("(non-fatal): Failed to retrieve load average", exc_info=True)

    try:
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if battery is not None:
            ret["power_plugged"] = battery.power_plugged
    except (OSError, RuntimeError):
        logger.debug("(non-fatal): Failed to retrieve power source", exc_info=True)

    governor_path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
    if os.path.isfile(governor_path):
        try:
            with open(governor_path) as infile:
                ret["cpu_governor"] = infile.read().strip()
        except OSError:
            logger.debug("(non-fatal): Failed to read CPU governor", exc_info=True)

    return ret


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
            ret["cpu_cores"] = cpu_cores
    except (SystemError, RuntimeError):
        logger.exception(" (non-fatal): psutil failed to retrieve CPU core count")

    try:
        num_threads = psutil.cpu_count(logical=True)
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


def _token_cache_file():
    return user_config_path("xemu-perf-tester") / "github_token"


def _is_token_valid(token: str) -> bool:
    try:
        response = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
    except requests.exceptions.RequestException:
        logger.exception("Failed to validate cached GitHub token")
        return False
    return response.status_code != 401


def _authenticate_and_cache_token() -> str:
    token = github_auth()
    token_cache_file = _token_cache_file()
    token_cache_file.parent.mkdir(parents=True, exist_ok=True)
    token_cache_file.write_text(token)
    token_cache_file.chmod(0o600)
    return token


def get_gh_cli_token() -> str | None:
    """Returns the token of the locally authenticated GitHub CLI (`gh`), if available."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        result = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("Failed to retrieve token from gh CLI", exc_info=True)
        return None
    if result.returncode != 0:
        logger.debug("gh auth token failed: %s", result.stderr.strip())
        return None
    return result.stdout.strip() or None


def get_artifact_download_token(explicit_token: str | None = None) -> str:
    """Returns a token for downloading CI artifacts.

    Prefers an explicitly provided token, then the GitHub CLI's token, then the result submission token.
    """
    if explicit_token:
        return explicit_token
    token = get_gh_cli_token()
    if token:
        logger.info("Using GitHub CLI credentials to download CI artifacts")
        return token
    return get_github_token(validate=True)


def get_github_token(*, validate: bool = False) -> str:
    """Returns the cached GitHub token used for result submission, authenticating if necessary."""
    token_cache_file = _token_cache_file()
    token = token_cache_file.read_text().strip() if token_cache_file.exists() else None
    if token and (not validate or _is_token_valid(token)):
        return token
    return _authenticate_and_cache_token()


def _build_issue_title(results: dict[str, Any]) -> str:
    xemu_version = results.get("xemu_version", "UNKNOWN")
    dev_build = results.get("dev_build")
    if not dev_build:
        return f"Benchmark result for xemu {xemu_version}"
    if dev_build.get("pr_number"):
        return f"Benchmark result for xemu PR #{dev_build['pr_number']} {xemu_version}"
    return f"Benchmark result for xemu CI run {dev_build.get('run_id')} {xemu_version}"


def _post_issue(token: str, title: str, encoded_payload: str) -> requests.Response:
    return requests.post(
        f"https://api.github.com/repos/{_SUBMISSION_REPO}/issues",
        json={
            "title": title,
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

    title = _build_issue_title(results)
    token = get_github_token()
    response = _post_issue(token, title, encoded_payload)
    if response.status_code == 401:
        token = _authenticate_and_cache_token()
        response = _post_issue(token, title, encoded_payload)

    if response.status_code == 201:
        print("Successfully submitted benchmark results.")
    else:
        print(f"Failed to submit. Status: {response.status_code}\n{response.text}")


def _build_dev_build_info(ci_build: CiBuildInfo, xemu_version: str) -> dict[str, Any]:
    ret = ci_build.to_dict()
    try:
        version = XemuVersion(xemu_version)
        ret["base_version"] = f"{version.xemu_major}.{version.xemu_minor}.{version.xemu_patch}"
    except ValueError:
        logger.warning("Failed to determine base version from '%s'", xemu_version)
        ret["base_version"] = None
    return ret


def _build_common_metadata(
    iso_path: str,
    machine_token: str,
    just_suites: Collection[str] | None,
    xemu_version: str,
    xemu_tag: str | None,
    ci_build: CiBuildInfo | None,
) -> dict[str, Any]:
    """Returns the metadata that is the same for every launch in a run."""
    ret: dict[str, Any] = {
        "iso": os.path.basename(iso_path),
        "suite_allowlist": list(just_suites) if just_suites else None,
        "xemu_version": xemu_version,
        "machine_info": _fetch_machine_info(),
        "machine_token": machine_token,
    }

    if xemu_tag:
        ret["xemu_tag"] = xemu_tag

    if ci_build:
        ret["dev_build"] = _build_dev_build_info(ci_build, xemu_version)

    return ret


def _process_results(
    output_directory: str,
    output_file_suffix: str,
    metadata: dict[str, Any],
    test_results: list[Any],
    *,
    renderer: str,
    vsync: bool,
    upload_results: bool = False,
):
    """Writes or uploads the results of a single-mode launch."""
    results = dict(metadata)
    results["renderer"] = renderer
    if not vsync:
        results["vsync"] = False
    results["results"] = test_results

    if upload_results:
        submit_results(results)
        return

    os.makedirs(output_directory, exist_ok=True)
    output_file = os.path.join(output_directory, f"{metadata['machine_token']}-{renderer}{output_file_suffix}.json")
    with open(output_file, "w") as outfile:
        json.dump(results, outfile, indent=2)


def build_hardware_baseline(calibration: dict[str, Any] | None) -> dict[str, Any] | None:
    """Returns the hardware measurements from a calibration.json, keyed by fully qualified test name."""
    if not calibration:
        return None

    tests: dict[str, Any] = {}
    for result in calibration.get("results", []):
        name = result.get("name")
        if not name:
            continue
        tests[name] = {
            key: result[key] for key in ("status", "load", "busy_p50_us", "busy_p95_us", "max_resets") if key in result
        }

    return {
        "hardware": calibration.get("hardware"),
        "calibration": calibration.get("calibration"),
        "tests": tests,
    }


def _sustained_output_file(output_directory: str, machine_token: str) -> str:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(output_directory, f"{machine_token}-sustained-{timestamp}.json")


def run(
    iso_path: str,
    work_path: str,
    inputs_path: str,
    results_path: str,
    xemu_path: str,
    reset_hdd: Callable[[str], None],
    hdd_path: str,
    machine_token: str,
    settings: RunSettings,
    block_list_file: str | None = None,
    xemu_tag: str | None = None,
    ci_build: CiBuildInfo | None = None,
    *,
    no_bundle: bool = False,
    upload_results: bool = False,
) -> int:
    """Runs the benchmark once per repeat of every host configuration in `settings`, each in a fresh xemu launch.

    `reset_hdd` is called with `hdd_path` before every launch to recreate the hard drive image.
    """
    # On Windows the first call starts sampling and returns zeros, so it's called here, well before the first launch.
    with contextlib.suppress(OSError, AttributeError):
        psutil.getloadavg()

    emulator_command, portable_mode_config_path = build_emulator_command(xemu_path, no_bundle=no_bundle)
    if not emulator_command:
        return 1

    def write_xemu_toml(renderer: str, *, vsync: bool) -> None:
        generate_xemu_toml(
            portable_mode_config_path,
            bootrom_path=os.path.join(inputs_path, "mcpx.bin"),
            flashrom_path=os.path.join(inputs_path, "bios.bin"),
            eeprom_path=os.path.join(inputs_path, "eeprom.bin"),
            hdd_path=hdd_path,
            use_vulkan=renderer == "VK",
            vsync=vsync,
        )

    first_renderer, first_vsync = settings.cells[0]
    write_xemu_toml(first_renderer, vsync=first_vsync)
    output_directory, xemu_version = _determine_xemu_info(results_path, emulator_command=emulator_command)

    config = Config(
        work_dir=work_path,
        output_dir=results_path,
        emulator_command=emulator_command,
        iso_path=iso_path,
        xbox_artifact_path=r"c:\xemu-perf-tests",
        suite_allowlist=list(settings.just_suites) if settings.just_suites else None,
        # The test runner treats 0 as its own default timeout.
        timeout_seconds=settings.timeout_seconds or 0,
    )

    block_list = BlockList(xemu_version, block_list_file=block_list_file)
    repacked_iso = _configure_iso(config, block_list, settings)
    if not repacked_iso:
        return 1

    sustained = settings.run_mode == _RUN_MODE_SUSTAINED
    metadata = _build_common_metadata(iso_path, machine_token, settings.just_suites, xemu_version, xemu_tag, ci_build)
    cells: list[dict[str, Any]] = []
    succeeded = 0
    failures = 0

    hardware_baseline: dict[str, Any] | None = None
    sustained_output_file = ""
    if sustained:
        hardware_baseline = build_hardware_baseline(XemuPerfTesterConfigManager(config).extract_calibration())
        if not hardware_baseline:
            logger.warning("The tester ISO has no calibration.json, so results can't be compared to hardware")
        sustained_output_file = _sustained_output_file(output_directory, machine_token)

    def write_sustained_results() -> None:
        """Writes every launch so far, so that the results survive if a later launch hangs or the run is stopped."""
        results = dict(metadata)
        results["run_mode"] = settings.run_mode
        results["repeat"] = settings.repeat
        results["hardware_baseline"] = hardware_baseline
        results["cells"] = cells

        os.makedirs(output_directory, exist_ok=True)
        # Written to a temporary file first so that stopping the run mid-write doesn't lose the earlier launches.
        temp_file = f"{sustained_output_file}.tmp"
        with open(temp_file, "w") as outfile:
            json.dump(results, outfile, indent=2)
        os.replace(temp_file, sustained_output_file)

    unavailable_renderers: set[str] = set()
    for renderer, vsync in settings.cells:
        for repeat in range(settings.repeat):
            if renderer in unavailable_renderers:
                logger.error(
                    "Skipping %s, vsync %s, repeat %d: xemu can't use it",
                    renderer,
                    "on" if vsync else "off",
                    repeat + 1,
                )
                failures += 1
                if sustained:
                    cells.append(
                        {
                            "renderer": renderer,
                            "vsync": vsync,
                            "repeat": repeat,
                            "error": f"Skipped: xemu can't use the {renderer} renderer",
                            "results": [],
                        }
                    )
                    write_sustained_results()
                continue
            logger.info(
                "Running %s, vsync %s, repeat %d of %d", renderer, "on" if vsync else "off", repeat + 1, settings.repeat
            )
            write_xemu_toml(renderer, vsync=vsync)
            reset_hdd(hdd_path)

            cell: dict[str, Any] = {
                "renderer": renderer,
                "vsync": vsync,
                "repeat": repeat,
                "host_settings": pinned_host_settings(use_vulkan=renderer == "VK", vsync=vsync),
                # Sampled before the launch, so that it measures the host rather than xemu.
                "host_noise": _fetch_host_noise_info(),
            }
            emulator_output, timed_out = _execute_and_collect_output(config, repacked_iso)
            if timed_out:
                cell["timed_out"] = True
            test_results: list[Any] | None = None
            if emulator_output:
                cell["xemu_machine_info"] = emulator_output.machine_info
                if emulator_output.failure_info:
                    cell["failure_info"] = emulator_output.failure_info
                if _RENDERER_UNAVAILABLE_MESSAGE in emulator_output.failure_info:
                    # xemu ran with a different renderer, so the results don't belong to this one.
                    logger.error("xemu can't use the %s renderer in this build", renderer)
                    unavailable_renderers.add(renderer)
                    cell["error"] = f"xemu can't use the {renderer} renderer and switched to another"
                    emulator_output = None
            if emulator_output:
                try:
                    with tempfile.TemporaryDirectory() as temp_path:
                        retrieve_files(hdd_path, temp_path, "c", "xemu-perf-tests/results.txt")
                        test_results = _parse_results_file(os.path.join(temp_path, "xemu-perf-tests", "results.txt"))
                except Exception as err:
                    logger.exception("Failed to retrieve results")
                    cell["error"] = str(err)
            elif "error" not in cell:
                cell["error"] = "Failed to execute emulator"

            if test_results is None:
                failures += 1
            else:
                succeeded += 1

            if sustained:
                cell["results"] = test_results or []
                cells.append(cell)
                write_sustained_results()
            elif emulator_output and test_results is not None:
                launch_metadata = dict(metadata)
                launch_metadata["machine_info"] = {**metadata["machine_info"], **cell["host_noise"]}
                launch_metadata["xemu_machine_info"] = (
                    emulator_output.machine_info + "\n" + emulator_output.failure_info
                )
                suffix = ("" if vsync else "-novsync") + (f"-r{repeat}" if settings.repeat > 1 else "")
                _process_results(
                    output_directory,
                    suffix,
                    launch_metadata,
                    test_results,
                    renderer=renderer,
                    vsync=vsync,
                    upload_results=upload_results,
                )

    if sustained:
        if not succeeded:
            logger.error("No launch succeeded")
            return 201
        logger.info("Wrote %s", sustained_output_file)

    if failures:
        logger.error("%d of %d launches failed", failures, len(settings.cells) * settings.repeat)
        return 201

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


def build_arg_parser() -> argparse.ArgumentParser:
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
        help="Release tag to use when downloading xemu from GitHub. May also be the URL of a xemu pull request or "
        "GitHub Actions run, in which case the CI build artifact for this platform is used.",
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
        "--run-mode",
        choices=[_RUN_MODE_SINGLE, _RUN_MODE_SUSTAINED],
        help="How the tester runs each test: a single profiled frame, or a sustained run of 60 FPS frames. "
        f"Defaults to {_RUN_MODE_SINGLE}.",
    )
    parser.add_argument(
        "--matrix",
        choices=sorted(_MATRIX_PRESETS),
        help="Runs a preset benchmark matrix. 'contention' runs the PfifoContention suite in sustained mode with "
        "each renderer, with vsync both on and off. Flags given explicitly override the preset.",
    )
    parser.add_argument(
        "--renderers",
        nargs="+",
        metavar="{GL,VK}",
        help="Renderers to run with, each in its own launches. Overrides --use-vulkan.",
    )
    parser.add_argument(
        "--vsync",
        nargs="+",
        choices=["on", "off"],
        help="xemu vsync settings to run with, each in its own launches. Defaults to on.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        help="Number of separate xemu launches for each renderer and vsync setting. "
        f"Defaults to {_DEFAULT_REPEAT[_RUN_MODE_SINGLE]} in single mode and "
        f"{_DEFAULT_REPEAT[_RUN_MODE_SUSTAINED]} in sustained mode.",
    )
    parser.add_argument(
        "--warmup-frames", type=int, help="Overrides the number of unrecorded frames before each sustained test."
    )
    parser.add_argument(
        "--measure-frames", type=int, help="Overrides the number of recorded frames in each sustained test."
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        help="Maximum time each xemu launch may run before it is killed. Defaults to "
        f"{_DEFAULT_SUSTAINED_TIMEOUT_SECONDS} in sustained mode.",
    )
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

    parser.add_argument(
        "--github-token",
        help="Github API token used for PR/action artifact fetching. Defaults to the token of the GitHub CLI (gh) "
        "if it is installed and authenticated, otherwise the token used to submit results.",
    )
    parser.add_argument(
        "--ci-workflow",
        default="CI",
        help="Name of the GitHub Actions workflow that produces xemu build artifacts for PR/action URLs.",
    )

    return parser


def entrypoint():
    parser = build_arg_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level)

    try:
        settings = resolve_run_settings(args)
    except ValueError as err:
        parser.error(str(err))

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

    ci_build: CiBuildInfo | None = None
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
    elif is_ci_build_url(args.xemu_tag):
        github_token = get_artifact_download_token(args.github_token)
        auth_header = {"Authorization": f"Bearer {github_token}"}
        try:
            ci_build = fetch_ci_build_info(args.xemu_tag, auth_header, workflow_name=args.ci_workflow)
            xemu = download_xemu_ci_build(cache_path, ci_build, auth_header)
        except CiBuildNotFoundError as err:
            logger.error("%s", err)  # noqa: TRY400 Use `logging.exception` instead of `logging.error`
            return 1
    else:
        xemu = download_xemu(cache_path, args.xemu_tag)

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
            reset_hdd=lambda hdd_path: _setup_minimal_hdd(hdd_path, iso),
            hdd_path=hdd,
            machine_token=machine_token,
            settings=settings,
            no_bundle=args.no_bundle,
            block_list_file=block_list_file,
            xemu_tag=args.xemu_tag,
            ci_build=ci_build,
            upload_results=args.upload_results,
        )

    if args.temp_path:
        return _copy_inputs_and_run(ensure_path(args.temp_path))

    with tempfile.TemporaryDirectory() as temp_path:
        return _copy_inputs_and_run(ensure_path(temp_path))


if __name__ == "__main__":
    sys.exit(entrypoint())
