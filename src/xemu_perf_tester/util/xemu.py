from __future__ import annotations

# ruff: noqa: S101 Use of `assert` detected
import logging
import os
import platform
import re
import shutil
import sys
import tarfile
import zipfile
from typing import Any

import semver

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from xemu_perf_tester.util.github import (
    CiBuildInfo,
    CiBuildNotFoundError,
    download_artifact,
    fetch_github_release_info,
    is_ci_build_url,
)

logger = logging.getLogger(__name__)

_GITHASHSTRING = r"[a-f0-9]+"
_XEMU_VERSION_CAPTURE = r"(\d+)\.(\d+)\.(\d+)"
# xemu-0.8.92-master-<githash>
# xemu-0.8.53-master-5685a6290cfbf7b022ec5e58a8ffb09f664c04e8
# xemu-0.8.134-fc9980d2962cbec656253106ea2e121fab1e68d4
_XEMU_RELEASE_VERSION_RE = re.compile(r"xemu-" + _XEMU_VERSION_CAPTURE + r"(?:-master)?-" + _GITHASHSTRING + "$")
# xemu-0.8.92-<build>-g<shorthash>-<branch_name>-<githash>
# xemu-0.8.53-4-g90cfbf-fix_something-90cfbf022ec5e58a8ffb09f664
_XEMU_DEV_VERSION_RE = re.compile(r"xemu-" + _XEMU_VERSION_CAPTURE + r"-(\d+)-g[^-]+-([^-]+)-" + _GITHASHSTRING + "$")

# CI builds of PRs do not include a branch name.
# xemu-0.8.138-9-g7850f6a9f4-7850f6a9f49c44e364ec2488491419e57dc4b6d6
_XEMU_DEV_NO_BRANCH_VERSION_RE = re.compile(
    r"xemu-" + _XEMU_VERSION_CAPTURE + r"-(\d+)-g[a-f0-9]+-" + _GITHASHSTRING + "$"
)

_XEMU_FORK_PR_RE = re.compile(r"xemu-" + _XEMU_VERSION_CAPTURE + r"-\s+-.*")

_XEMU_API_URL = "https://api.github.com/repos/xemu-project/xemu"


class XemuVersion:
    """xemu version string parser"""

    def __init__(self, version_str: str):
        self.raw = version_str

        match = _XEMU_RELEASE_VERSION_RE.match(version_str)
        if match:
            self.xemu_major = int(match.group(1))
            self.xemu_minor = int(match.group(2))
            self.xemu_patch = int(match.group(3))
            self.build = None
            self.branch = None
        else:
            match = _XEMU_DEV_VERSION_RE.match(version_str)
            if match:
                self.xemu_major = int(match.group(1))
                self.xemu_minor = int(match.group(2))
                self.xemu_patch = int(match.group(3))
                self.build = int(match.group(4))
                self.branch = match.group(5).strip()
            elif match := _XEMU_DEV_NO_BRANCH_VERSION_RE.match(version_str):
                self.xemu_major = int(match.group(1))
                self.xemu_minor = int(match.group(2))
                self.xemu_patch = int(match.group(3))
                self.build = int(match.group(4))
                self.branch = ""
            else:
                match = _XEMU_FORK_PR_RE.match(version_str)
                if match:
                    self.xemu_major = int(match.group(1))
                    self.xemu_minor = int(match.group(2))
                    self.xemu_patch = int(match.group(3))
                    self.build = 0
                    self.branch = "Unofficial"
                else:
                    msg = f"Invalid xemu version string '{version_str}'"
                    raise ValueError(msg)

        self.semver = semver.Version(self.xemu_major, self.xemu_minor, self.xemu_patch, build=self.build)

    @property
    def is_release(self) -> bool:
        return self.build is None

    def compare(self, other: XemuVersion) -> int:
        return self.semver.compare(other.semver)


def _ubuntu_extract_app(archive_file: str, target_binary: str) -> None:
    output_directory = os.path.dirname(target_binary)
    os.makedirs(output_directory, exist_ok=True)

    try:
        nested_archive = ""
        with zipfile.ZipFile(archive_file, "r") as zip_ref:
            # Newer CI artifacts contain the AppImage directly.
            for file_info in zip_ref.infolist():
                if "/" not in file_info.filename and file_info.filename.endswith(".AppImage"):
                    with zip_ref.open(file_info) as infile, open(target_binary, "wb") as outfile:
                        shutil.copyfileobj(infile, outfile)
                    os.chmod(target_binary, 0o700)
                    return

            for file_info in zip_ref.infolist():
                if (
                    file_info.filename.startswith("xemu")
                    and not file_info.is_dir()
                    and file_info.filename.endswith(".tgz")
                ):
                    nested_archive = os.path.join(output_directory, file_info.filename)
                    zip_ref.extract(file_info, output_directory)
                    break

        if not nested_archive:
            msg = f"xemu archive was downloaded at '{archive_file}' but artifact tgz could not be extracted"
            raise ValueError(msg)

        with tarfile.open(nested_archive, "r:gz") as tar:
            for tar_info in tar.getmembers():
                if tar_info.name.endswith(".AppImage"):
                    file_obj = tar.extractfile(tar_info)
                    assert file_obj

                    with open(target_binary, "wb") as outfile:
                        outfile.write(file_obj.read())
                    os.chmod(target_binary, 0o700)
                    break

    except FileNotFoundError:
        logger.exception("Archive not found when extracting xemu app bundle")
        raise
    except zipfile.BadZipFile:
        logger.exception("Invalid zip archive when extracting xemu app bundle")
        raise


_MACOS_CI_ARTIFACT_NESTED_ZIP_NAME = "xemu-macos-universal-release.zip"


def _is_macos_nested_zip(filename: str) -> bool:
    """Returns True if the given archive member is a nested zip containing the xemu.app bundle.

    CI artifacts have contained both "xemu-macos-universal-release.zip" and
    "xemu-<version>-macos-universal-unsigned.zip".
    """
    if filename == _MACOS_CI_ARTIFACT_NESTED_ZIP_NAME:
        return True
    return "/" not in filename and filename.startswith("xemu-") and "-macos-" in filename and filename.endswith(".zip")


def _macos_extract_app(archive_file: str, target_app_bundle: str) -> None:
    """Extracts the xemu.app bundle from the given archive and renames it."""
    app_bundle_directory = os.path.dirname(target_app_bundle)

    try:
        nested_archive_file = []

        def extract_app_bundle(zip_filename):
            with zipfile.ZipFile(zip_filename, "r") as zip_ref:
                os.makedirs(app_bundle_directory, exist_ok=True)

                for file_info in zip_ref.infolist():
                    if file_info.filename.startswith("xemu.app/") and not file_info.is_dir():
                        zip_ref.extract(file_info, app_bundle_directory)
                    elif _is_macos_nested_zip(file_info.filename):
                        zip_ref.extract(file_info, app_bundle_directory)
                        nested_archive_file.append(os.path.join(app_bundle_directory, file_info.filename))
                        break

        extract_app_bundle(archive_file)
        if nested_archive_file:
            extract_app_bundle(nested_archive_file[0])

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


def _download_xemu_release(output_dir: str, tag: str = "latest") -> str | None:
    logger.info("Fetching info on xemu at release tag %s...", tag)
    release_info = fetch_github_release_info(_XEMU_API_URL, tag)
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
            if not asset_name.startswith("xemu-") or "-dbg-" in asset_name:
                return False
            return asset_name.endswith(".AppImage") and platform.machine() in asset_name
    elif system == "Darwin":
        # xemu-macos-universal-release.zip
        def check_asset(asset_name: str) -> bool:
            if not asset_name.startswith("xemu-") or "-dbg-" in asset_name:
                return False
            return "macos-universal" in asset_name and asset_name.endswith(".zip")
    elif system == "Windows":
        # xemu-win-x86_64-release.zip
        def check_asset(asset_name: str) -> bool:
            if not asset_name.startswith("xemu-win-") or not asset_name.endswith("release.zip"):
                return False

            # xemu-win-release.zip for very old versions
            if asset_name == "xemu-win-release.zip":
                return True

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
        logger.error("Failed to fetch download URL for latest xemu release")
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


def _host_machine_name() -> str:
    platform_name = platform.machine().lower()
    if platform_name == "amd64":
        return "x86_64"
    if platform_name == "arm64" and platform.system() != "Darwin":
        return "aarch64"
    return platform_name


def _ci_artifact_names_for_host() -> list[str]:
    """Returns the CI artifact names usable on this host, in order of preference."""
    system = platform.system()
    machine = _host_machine_name()
    if system == "Linux":
        return [f"xemu-ubuntu-{machine}-release"]
    if system == "Darwin":
        return ["xemu-macos-universal-release", f"xemu-macos-{machine}-release"]
    if system == "Windows":
        windows_machine = "arm64" if machine in {"aarch64", "arm64"} else machine
        return [f"xemu-windows-{windows_machine}-release", f"xemu-win-{machine}-release"]

    msg = f"System '{system} not supported"
    raise NotImplementedError(msg)


def find_ci_artifact(build_info: CiBuildInfo) -> dict[str, Any]:
    """Returns the artifact from the given build that should be used on this host.

    Raises CiBuildNotFoundError if there is no usable artifact.
    """
    artifacts_by_name = {artifact.get("name", ""): artifact for artifact in build_info.artifacts}
    for name in _ci_artifact_names_for_host():
        artifact = artifacts_by_name.get(name)
        if not artifact:
            continue
        if artifact.get("expired"):
            msg = f"Artifact '{name}' from {build_info.run_url} has expired. Re-run the workflow to regenerate it."
            raise CiBuildNotFoundError(msg)
        return artifact

    msg = (
        f"No artifact for this platform ({', '.join(_ci_artifact_names_for_host())}) in {build_info.run_url}. "
        f"Available artifacts: {sorted(artifacts_by_name)}"
    )
    raise CiBuildNotFoundError(msg)


def download_xemu_ci_build(output_dir: str, build_info: CiBuildInfo, auth_header: dict[str, Any]) -> str | None:
    """Downloads and extracts the xemu CI build for this host, returning the path to the binary/app bundle."""
    artifact = find_ci_artifact(build_info)
    download_url = artifact.get("archive_download_url", "")
    if not download_url:
        logger.error("Artifact '%s' has no download URL", artifact.get("name"))
        return None

    # Each artifact gets its own directory so CI builds never clobber cached releases or each other.
    build_dir = os.path.join(output_dir, "ci", f"{artifact.get('name')}-{artifact['id']}")

    system = platform.system()
    if system == "Linux":
        target_file = os.path.join(build_dir, f"{artifact.get('name')}.AppImage")
    elif system == "Darwin":
        target_file = os.path.join(build_dir, "xemu.app")
    elif system == "Windows":
        target_file = os.path.join(build_dir, "xemu.exe")
    else:
        msg = f"System '{system} not supported"
        raise NotImplementedError(msg)
    artifact_path_override = os.path.join(build_dir, "artifact.zip")

    if os.path.exists(target_file):
        logger.info("Using cached CI build %s", target_file)
        return target_file

    logger.info("Downloading %s from %s...", artifact.get("name"), build_info.run_url)
    if not download_artifact(
        target_file, download_url, artifact_path_override, additional_headers=auth_header, force_download=True
    ):
        return None

    if system == "Linux":
        _ubuntu_extract_app(artifact_path_override, target_file)
    elif system == "Darwin":
        _macos_extract_app(artifact_path_override, target_file)
    elif system == "Windows":
        _windows_extract_app(artifact_path_override, target_file)

    return target_file


def download_xemu(output_dir: str, tag: str = "latest") -> str | None:
    """Downloads an official xemu release."""
    if is_ci_build_url(tag):
        msg = f"'{tag}' is a CI build URL; use fetch_ci_build_info and download_xemu_ci_build"
        raise ValueError(msg)
    return _download_xemu_release(output_dir, tag)


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
    if os.path.isdir(toml_path):
        toml_path = os.path.join(toml_path, "xemu.toml")
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
