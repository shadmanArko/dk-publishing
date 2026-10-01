"""Rules import-linter cannot express. Each one fails the build when broken."""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "dk_publishing"
PACKAGE = "dk_publishing"

PLATFORM_NAMES = re.compile(
    r"\b(instagram|facebook|threads|linkedin|tiktok|youtube|reddit|twitter)\b", re.IGNORECASE
)
LLM_SDKS = {"anthropic", "openai", "google.generativeai", "litellm"}


def _py_files(sub: str = "") -> Iterator[Path]:
    yield from sorted((SRC / sub).rglob("*.py"))


def _imports(path: Path) -> Iterator[str]:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def _is_stdlib(module: str) -> bool:
    return module.split(".")[0] in sys.stdlib_module_names


def test_domain_imports_only_stdlib_and_itself() -> None:
    bad = [
        (p.name, m)
        for p in _py_files("domain")
        for m in _imports(p)
        if not (_is_stdlib(m) or m.startswith(f"{PACKAGE}.domain"))
    ]
    assert not bad, f"domain must stay pure: {bad}"


def test_application_imports_only_stdlib_domain_and_itself() -> None:
    allowed = (f"{PACKAGE}.domain", f"{PACKAGE}.application")
    bad = [
        (p.name, m)
        for p in _py_files("application")
        for m in _imports(p)
        if not (_is_stdlib(m) or m.startswith(allowed))
    ]
    assert not bad, f"application may depend only on domain: {bad}"


def test_platform_names_appear_only_in_platform_adapters() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for p in _py_files()
        if "adapters/platforms" not in p.as_posix() and PLATFORM_NAMES.search(p.read_text())
    ]
    assert not offenders, f"platform names leaked outside adapters/platforms: {offenders}"


def test_only_secrets_adapter_imports_cryptography() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for p in _py_files()
        if "adapters/secrets" not in p.as_posix()
        and any(m.split(".")[0] == "cryptography" for m in _imports(p))
    ]
    assert not offenders, f"cryptography is the vault's alone: {offenders}"


def test_no_llm_sdk_is_imported() -> None:
    """Nothing between approval and publish calls a model; the package never imports one."""
    offenders = [
        (str(p.relative_to(SRC)), m)
        for p in _py_files()
        for m in _imports(p)
        if any(m == sdk or m.startswith(f"{sdk}.") for sdk in LLM_SDKS)
    ]
    assert not offenders, offenders
