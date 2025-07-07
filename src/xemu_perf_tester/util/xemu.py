from __future__ import annotations

# ruff: noqa: S101 Use of `assert` detected
import logging
import os
import platform
import shutil
import tomllib
import zipfile

from xemu_perf_tester.util.github import download_artifact, fetch_github_release_info

logger = logging.getLogger(__name__)


def _macos_extract_app(archive_file: str, target_app_bundle: str) -> None:
    """Extracts the xemu.app bundle from the given archive and renames it."""
    app_bundle_directory = os.path.dirname(target_app_bundle)

    try:
        with zipfile.ZipFile(archive_file, "r") as zip_ref:
            os.makedirs(app_bundle_directory, exist_ok=True)

            for file_info in zip_ref.infolist():
                if file_info.filename.startswith("xemu.app/") and not file_info.is_dir():
                    zip_ref.extract(file_info, app_bundle_directory)

            if not os.path.isfile(os.path.join(app_bundle_directory, "xemu.app", "Contents", "MacOS", "xemu")):
                msg = f"xemu archive was downloaded at '{archive_file}' but app bundle could not be extracted"
                raise ValueError(msg)

    except FileNotFoundError:
        logger.exception("Archive not found when extracting xemu app bundle")
        raise
    except zipfile.BadZipFile:
        logger.exception("Invalid zip archive when extracting xemu app bundle")
        raise


def _windows_extract_app(archive_file: str, target_executable: str) -> None:
    """Extracts xemu.exe from the given archive."""

    try:
        with zipfile.ZipFile(archive_file, "r") as zip_ref:
            for file_info in zip_ref.infolist():
                if file_info.filename == "xemu.exe":
                    target_dir = os.path.dirname(target_executable)
                    zip_ref.extract(file_info, target_dir)
                    if os.path.basename(target_executable) != "xemu.exe":
                        os.rename(os.path.join(target_dir, "xemu.exe"), target_executable)
                    return

    except FileNotFoundError:
        logger.exception("Archive not found when extracting xemu.exe")
        raise
    except zipfile.BadZipFile:
        logger.exception("Invalid zip archive when extracting xemu.exe")
        raise


def download_xemu(output_dir: str, tag: str = "latest") -> str | None:
    logger.info("Fetching info on xemu at release tag %s...", tag)
    release_info = fetch_github_release_info("https://api.github.com/repos/xemu-project/xemu", tag)
    if not release_info:
        return None

    release_tag = release_info.get("tag_name")
    if not release_tag:
        logger.error("Failed to retrieve release tag from GitHub.")
        return None

    system = platform.system()
    if system == "Linux":
        # xemu-v0.8.15-x86_64.AppImage
        def check_asset(asset_name: str) -> bool:
            if not asset_name.startswith("xemu-v") or "-dbg-" in asset_name:
                return False
            return asset_name.endswith(".AppImage") and platform.machine() in asset_name
    elif system == "Darwin":
        # xemu-macos-universal-release.zip
        def check_asset(asset_name: str) -> bool:
            return asset_name == "xemu-macos-universal-release.zip"
    elif system == "Windows":
        # xemu-win-x86_64-release.zip
        def check_asset(asset_name: str) -> bool:
            if not asset_name.startswith("xemu-win-") or not asset_name.endswith("release.zip"):
                return False
            platform_name = platform.machine()
            if platform_name == "AMD64":
                platform_name = "x86_64"
            return platform_name.lower() in asset_name
    else:
        msg = f"System '{system} not supported"
        raise NotImplementedError(msg)

    asset_name = ""
    download_url = ""
    for asset in release_info.get("assets", []):
        asset_name = asset.get("name", "")
        if not check_asset(asset_name):
            continue
        download_url = asset.get("browser_download_url", "")
        break

    if not download_url:
        logger.error("Failed to fetch download URL for latest nxdk_pgraph_tests release")
        return None

    if system == "Linux":
        target_file = os.path.join(output_dir, asset_name)
        artifact_path_override = None
    elif system == "Darwin":
        target_file = os.path.join(output_dir, f"xemu-macos-{release_tag}", "xemu.app")
        artifact_path_override = f"{target_file}.zip"
    elif system == "Windows":
        target_file = os.path.join(output_dir, "xemu.exe")
        artifact_path_override = f"{target_file}.zip"
    else:
        msg = f"System '{system} not supported"
        raise NotImplementedError(msg)

    logger.debug("Xemu %s %s", target_file, download_url)

    tag_info_file_path = os.path.join(output_dir, "xemu-tag.info")

    if not release_tag or not os.path.isfile(tag_info_file_path):
        force_download = True
    else:
        with open(tag_info_file_path) as tag_info_file:
            cached_tag = tag_info_file.readline()
            force_download = cached_tag != release_tag

    was_downloaded = download_artifact(target_file, download_url, artifact_path_override, force_download=force_download)

    if was_downloaded:
        if system == "Linux":
            os.chmod(target_file, 0o700)
        elif system == "Darwin":
            assert artifact_path_override
            _macos_extract_app(artifact_path_override, target_file)
        elif system == "Windows":
            assert artifact_path_override
            _windows_extract_app(artifact_path_override, target_file)

        with open(tag_info_file_path, "w") as tag_info_file:
            tag_info_file.write(release_tag)

    return target_file


def generate_xemu_toml(
    file_path: str,
    bootrom_path: str,
    flashrom_path: str,
    eeprom_path: str,
    hdd_path: str,
    *,
    use_vulkan: bool = False,
) -> None:
    content = [
        "[general]",
        "show_welcome = false",
        "skip_boot_anim = true",
        "",
        "[general.updates]",
        "check = false",
        "",
        "[net]",
        "enable = true",
        "",
        "[sys]",
        "mem_limit = '64'",
        "",
        "[sys.files]",
        f"bootrom_path = '{bootrom_path}'",
        f"flashrom_path = '{flashrom_path}'",
        f"eeprom_path = '{eeprom_path}'",
        f"hdd_path = '{hdd_path}'",
    ]

    if use_vulkan:
        content.extend(["", "[display]", "renderer = 'VULKAN'"])

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w") as outfile:
        outfile.write("\n".join(content))


def _build_macos_xemu_binary_paths(xemu_app_bundle_path: str) -> tuple[str, str]:
    contents_path = os.path.join(xemu_app_bundle_path, "Contents")
    library_path = ":".join(
        [
            os.path.join(contents_path, "Libraries", platform.uname().machine),
            os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", ""),
        ]
    )
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = library_path

    xemu_binary = os.path.join(contents_path, "MacOS", "xemu")
    os.chmod(xemu_binary, 0o700)
    return xemu_binary, os.path.join(contents_path, "Resources")


def build_emulator_command(xemu_path: str, *, no_bundle: bool = False) -> tuple[str, str]:
    portable_mode_config_path = os.path.dirname(xemu_path)

    system = platform.system()
    if system == "Darwin":
        if not no_bundle:
            xemu_path, portable_mode_config_path = _build_macos_xemu_binary_paths(xemu_path)
    elif system == "Linux":
        if xemu_path.endswith("AppImage"):
            # AppImages need to have the xemu.toml file within their home dir.
            portable_mode_config_path = os.path.join(f"{xemu_path}.home", ".local", "share", "xemu", "xemu")
    elif system == "Windows":
        pass
    else:
        msg = f"Platform {system} not supported."
        raise NotImplementedError(msg)

    return xemu_path + " -dvd_path {ISO}", os.path.join(portable_mode_config_path, "xemu.toml")


def ensure_path(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(path, exist_ok=True)
    return path


def ensure_cache_path(cache_path: str) -> str:
    if not cache_path:
        msg = "cache_path may not be empty"
        raise ValueError(msg)
    return ensure_path(cache_path)


def ensure_results_path(results_path: str) -> str:
    if not results_path:
        msg = "results_path may not be empty"
        raise ValueError(msg)
    return ensure_path(results_path)


def copy_xemu_inputs(toml_path: str, destination_directory: str):
    """Copies the various required input files from an existing xemu.toml manifest to the given directory."""

    toml_path = os.path.abspath(os.path.expanduser(toml_path))
    if not os.path.isfile(toml_path):
        msg = f"No xemu toml file found at '{toml_path}'"
        raise ValueError(msg)

    os.makedirs(destination_directory, exist_ok=True)

    with open(toml_path, "rb") as infile:
        data = tomllib.load(infile)

    files = data["sys"]["files"]

    mcpx_path = files["bootrom_path"]
    shutil.copy2(mcpx_path, os.path.join(destination_directory, "mcpx.bin"))

    bios_path = files["flashrom_path"]
    shutil.copy2(bios_path, os.path.join(destination_directory, "bios.bin"))
