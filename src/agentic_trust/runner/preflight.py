"""Check the Python environment before any paid run.

Catches the failure seen in the first smoke attempt: anthropic's HTTP stack
(httpx2) picking up an old brotli library from a shared environment (Anaconda
base) and crashing on the first response. The pins live in pyproject.toml; this
reads them back from the installed package's metadata, so there is one source.

    python -m agentic_trust.runner.preflight
"""

from __future__ import annotations

import os
import sys
from importlib import metadata

from packaging.requirements import Requirement

PACKAGE = "agentic-trust"
# Exact pins checked here; brotli is checked against its minimum instead.
CHECKED = ("anthropic", "httpx2", "httpcore2", "httpx", "httpcore", "langsmith", "brotli", "brotlicffi")


def problems() -> tuple[list[str], list[str]]:
    """(errors, warnings). Errors mean a run should not start."""
    errors, warnings = [], []
    if sys.prefix == sys.base_prefix:
        where = f"conda env {os.environ['CONDA_DEFAULT_ENV']!r}" if os.environ.get("CONDA_DEFAULT_ENV") else sys.prefix
        warnings.append(f"not running in a virtual environment ({where}); use a clean venv (see README)")

    try:
        reqs = [Requirement(r) for r in metadata.requires(PACKAGE) or []]
    except metadata.PackageNotFoundError:
        return [f"{PACKAGE} is not installed; run `pip install -e .` in your venv"], warnings

    for req in reqs:
        name = req.name.lower()
        if name not in CHECKED or (req.marker and not req.marker.evaluate({"extra": ""})):
            continue
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            errors.append(f"{name} is required ({req.specifier}) but not installed")
            continue
        if not req.specifier.contains(installed, prereleases=True):
            errors.append(f"{name} {installed} installed, but this project needs {name}{req.specifier}")
    return errors, warnings


def check(out=print) -> bool:
    errors, warnings = problems()
    for w in warnings:
        out(f"preflight warning: {w}")
    for e in errors:
        out(f"preflight ERROR: {e}")
    if errors:
        out("Fix the environment before running (README: Setup). Nothing was sent to any API.")
    return not errors


def main() -> int:
    ok = check()
    if ok:
        print("preflight OK: pinned HTTP stack and brotli versions match pyproject.toml")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
