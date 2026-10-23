"""The environment preflight that runs before any paid cell."""

import sys
from importlib import metadata

from agentic_trust.runner import cell, preflight


def fake_versions(monkeypatch, **overrides):
    real = metadata.version

    def version(name):
        key = name.lower().replace("-", "_")
        if key in overrides:
            if overrides[key] is None:
                raise metadata.PackageNotFoundError(name)
            return overrides[key]
        return real(name)

    monkeypatch.setattr(preflight.metadata, "version", version)


def test_this_environment_passes():
    errors, _ = preflight.problems()
    assert errors == []


def test_old_brotli_is_an_error(monkeypatch):
    fake_versions(monkeypatch, brotli="1.1.0")
    errors, _ = preflight.problems()
    assert any("brotli 1.1.0 installed" in e and ">=1.2.0" in e for e in errors)


def test_wrong_anthropic_or_missing_httpx2_is_an_error(monkeypatch):
    fake_versions(monkeypatch, anthropic="2.0.0", httpx2=None)
    errors, _ = preflight.problems()
    assert any(e.startswith("anthropic 2.0.0 installed") for e in errors)
    assert any(e.startswith("httpx2 is required") for e in errors)


def test_running_outside_a_venv_is_a_warning(monkeypatch):
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    errors, warnings = preflight.problems()
    assert errors == [] and any("conda env 'base'" in w for w in warnings)


def test_cell_stops_before_anything_else_when_preflight_fails(monkeypatch, capsys):
    fake_versions(monkeypatch, brotli="1.1.0")
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(AssertionError("asked")))
    assert cell.main(["SEC-DEC-1", "P1", "--yes"]) == 1
    out = capsys.readouterr().out
    assert "preflight ERROR: brotli 1.1.0" in out and "Nothing was sent" in out
    assert "tracing" not in out  # stopped before reading keys or configuring anything
