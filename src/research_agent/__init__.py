"""Deep Research Agent package metadata."""

import re
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    _metadata_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    _package_version = (
        tomllib.loads(_metadata_file.read_text(encoding="utf-8"))["project"]["version"]
        if _metadata_file.exists()
        else version("deep-research-agent")
    )
except PackageNotFoundError:  # source tree before installation
    _package_version = "0.4.0rc1"

__version__ = re.sub(r"rc(\d+)$", r"-rc.\1", _package_version)
