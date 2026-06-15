"""Small helpers for loading the miniQMT bundled xtquant package."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

from .config import XTQUANT_SITE_PACKAGES


def ensure_xtquant_path(site_packages: str | Path | None = None) -> Path:
    path = Path(site_packages or XTQUANT_SITE_PACKAGES)
    if path.exists():
        text = str(path)
        if text not in sys.path:
            sys.path.append(text)
        package_dir = path / "xtquant"
        if package_dir.exists() and hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(package_dir))
            except OSError:
                pass
    return path


def xtquant_compatibility(site_packages: str | Path | None = None) -> dict[str, Any]:
    path = Path(site_packages or XTQUANT_SITE_PACKAGES)
    package_dir = path / "xtquant"
    current_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    native_modules = sorted(p.name for p in package_dir.glob("IPythonApiClient.cp*-win_amd64.pyd"))
    return {
        "python_version": sys.version.split()[0],
        "expected_native_tag": current_tag,
        "native_modules": native_modules,
        "matching_native_module": any(f".{current_tag}-" in name for name in native_modules),
    }


def import_xtdata(site_packages: str | Path | None = None) -> Any:
    ensure_xtquant_path(site_packages)
    return importlib.import_module("xtquant.xtdata")


def import_xttrader_modules(site_packages: str | Path | None = None) -> tuple[Any, Any]:
    ensure_xtquant_path(site_packages)
    xttrader = importlib.import_module("xtquant.xttrader")
    xttype = importlib.import_module("xtquant.xttype")
    return xttrader, xttype
