"""Loader: import the hyphenated guard module so tests can exercise its
internal fast-path gate directly. Hyphens are not importable identifiers."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_cfv", Path(__file__).resolve().parent / "check-fabricated-verification.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

has_any_claim = _mod._has_any_claim
