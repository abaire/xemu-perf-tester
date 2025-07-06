from __future__ import annotations

import os

from pyfatx import Fatx


def setup_xemu_hdd_image(image_path: str, dashboard_xbe_path: str):
    """Sets up a new hard drive image suitable for use with xemu."""

    Fatx.create(image_path)

    fatx = Fatx(image_path, drive="c")
    with open(dashboard_xbe_path, "rb") as dashboard_xbe:
        fatx.write("/xboxdash.xbe", dashboard_xbe.read())


def retrieve_files(hdd_image_path: str, output_path: str, drive: str = "c", *files):
    """Retrieves files from the given HDD image, copying them to the given output_path."""

    if not os.path.exists(output_path):
        os.makedirs(output_path, exist_ok=True)
    if not os.path.isdir(output_path):
        msg = f"Output path '{output_path}' exists and is not a directory!"
        raise ValueError(msg)

    fatx = Fatx(hdd_image_path, drive=drive)

    for file_path in files:
        # file_info = fatx.get_attr(file_path)

        output_file = os.path.join(output_path, file_path)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)

        with open(output_file, "wb") as outfile:
            outfile.write(fatx.read(file_path))
