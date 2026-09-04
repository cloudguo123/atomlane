#!/usr/bin/env python3
"""Fail-closed static frontends for the AtomLane Atom IR.

Supported inputs are intentionally narrow.  Syntax outside the documented
POSIX-lite/package/Make/Compose subset is preserved as an opaque island with a
diagnostic; the compiler never silently drops source semantics.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import ntpath
import os
import re
import secrets
import shlex
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

try:  # Python 3.10 uses the tiny tomli dependency when available.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - fail-closed path
        tomllib = None  # type: ignore[assignment]

from atom_engine import (
    MAX_ATOMS,
    MAX_COMMAND_CHARS,
    MAX_RECURSION,
    MAX_SOURCE_BYTES,
    AtomError,
    _bounded_text,
    _normalize_resource,
    _slug,
)
from platform_adapter import resolve_host_executable, resolve_windows_path_executable
from python_static_effects import infer_python_cli_accesses

MAX_SHELL_SEGMENTS = 128
MAX_PACKAGE_SCRIPTS = 256
MAX_PACKAGE_DEPTH = 8
MAX_MAKE_TARGETS = 1024
SAFE_YAML_PARSER_TIMEOUT_SECONDS = 15


def _strict_read_text(path: Path, label: str) -> str:
    """Read one bounded UTF-8 source without replacement-character recovery."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AtomError(f"cannot read {label}: {exc}") from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise AtomError(f"{label} exceeds the {MAX_SOURCE_BYTES}-byte static parsing limit")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AtomError(f"{label} is not valid UTF-8") from exc


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AtomError(f"duplicate JSON key is not accepted by the static frontend: {key}")
        result[key] = value
    return result


@dataclass
class Fragment:
    roots: list[str] = field(default_factory=list)
    terminals: list[str] = field(default_factory=list)
    atoms: list[str] = field(default_factory=list)


@dataclass
class ShellSegment:
    text: str
    connector_before: str | None
    line: int
    dynamic: bool = False


class Compilation:
    def __init__(self, project: Path) -> None:
        self.project = project.resolve()
        self.atoms: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.native_delegates: list[dict[str, Any]] = []
        self.relaxation_candidates: list[dict[str, Any]] = []
        self.test_suites: list[dict[str, Any]] = []
        self._ids: set[str] = set()

    def snapshot(self, path: Path) -> dict[str, Any]:
        path = path.resolve(strict=False)
        try:
            relative = path.relative_to(self.project).as_posix()
        except ValueError:
            relative = str(path)
        if relative in self.snapshots:
            return self.snapshots[relative]
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise AtomError(f"source snapshot exceeds {MAX_SOURCE_BYTES} bytes: {path}")
        item = {
            "path": relative,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        self.snapshots[relative] = item
        return item

    def diagnostic(
        self,
        code: str,
        message: str,
        *,
        severity: str = "warning",
        source: str | None = None,
        line: int | None = None,
        symbol: str | None = None,
    ) -> None:
        self.diagnostics.append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "source": source,
                "line": line,
                "symbol": symbol,
            }
        )

    def unique_id(self, proposed: str) -> str:
        base = _slug(proposed)
        candidate = base
        suffix = 2
        while candidate in self._ids:
            tail = f"-{suffix}"
            candidate = base[: 160 - len(tail)] + tail
            suffix += 1
        self._ids.add(candidate)
        return candidate

    def emit(self, raw: dict[str, Any]) -> str:
        if len(self.atoms) >= MAX_ATOMS:
            raise AtomError(f"compiled atom graph exceeds the {MAX_ATOMS}-atom limit")
        raw = dict(raw)
        raw["id"] = self.unique_id(raw.get("id", "atom"))
        self.atoms.append(raw)
        return raw["id"]

    def result(self) -> dict[str, Any]:
        return {
            "atoms": self.atoms,
            "diagnostics": self.diagnostics,
            "snapshots": [self.snapshots[key] for key in sorted(self.snapshots)],
            "native_delegates": self.native_delegates,
            "relaxation_candidates": self.relaxation_candidates,
            "test_suites": self.test_suites,
        }


def _source_label(path: Path, project: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(project).as_posix()
    except ValueError:
        return str(path.resolve(strict=False))


def _add_dependencies(compilation: Compilation, atom_ids: list[str], dependencies: list[dict[str, str]]) -> None:
    if not dependencies:
        return
    by_id = {atom["id"]: atom for atom in compilation.atoms}
    for atom_id in atom_ids:
        atom = by_id[atom_id]
        existing = atom.setdefault("dependencies", [])
        for dependency in dependencies:
            if dependency not in existing:
                existing.append(dict(dependency))


def _scan_shell_list(command: str) -> tuple[list[ShellSegment], str | None]:
    """Split a conservative top-level `&&`/`;`/newline list.

    Pipelines, background execution, OR lists, unbalanced syntax, and shell
    control structures are returned as one opaque region.  Parenthesized and
    command-substitution contents are tracked so their connectors are never
    mistaken for top-level separators.
    """
    if len(command) > MAX_COMMAND_CHARS:
        return [], "BUDGET_EXCEEDED"
    segments: list[ShellSegment] = []
    buffer: list[str] = []
    connector_before: str | None = None
    quote: str | None = None
    escaped = False
    dynamic_segment = False
    line = 1
    segment_line = 1
    index = 0

    def flush() -> None:
        nonlocal buffer, connector_before, dynamic_segment, segment_line
        text = "".join(buffer).strip()
        if text:
            segments.append(ShellSegment(text, connector_before, segment_line, dynamic_segment))
            connector_before = None
        buffer = []
        dynamic_segment = False
        segment_line = line

    while index < len(command):
        char = command[index]
        next_char = command[index + 1] if index + 1 < len(command) else ""
        if escaped:
            buffer.append(char)
            escaped = False
            if char == "\n":
                line += 1
            index += 1
            continue
        if char == "\\" and quote != "'":
            buffer.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            elif quote == '"' and (char == "`" or (char == "$" and next_char == "(")):
                # Correctly finding the end of a nested command substitution
                # requires the full shell grammar.  Do not scan across it with
                # the POSIX-lite frontend: an embedded semicolon or control
                # operator must never be mistaken for a top-level separator.
                return [], "UNSUPPORTED_DYNAMIC_SHELL"
            if char == "\n":
                line += 1
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "`" or (char == "$" and next_char == "(") or (char in {"<", ">"} and next_char == "("):
            return [], "UNSUPPORTED_DYNAMIC_SHELL"
        if char in {"(", ")"}:
            # Subshells, function declarations, and arithmetic commands are
            # opaque islands.  Tracking parentheses alone is unsound because
            # their quoting and control-flow rules are context dependent.
            return [], "UNSUPPORTED_CONTROL_STRUCTURE"
        if char == "#" and (index == 0 or command[index - 1].isspace() or command[index - 1] in ";&|"):
            # A shell comment begins only at the start of a word.  Drop the
            # comment body but leave the newline to preserve list sequencing.
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        if char == "|":
            return [], "UNSUPPORTED_PIPELINE_OR_OR_LIST"
        if char == "&" and next_char == "&":
            if not "".join(buffer).strip():
                return [], "INVALID_SHELL_LIST"
            flush()
            connector_before = "success"
            index += 2
            continue
        if char == "&" and not (buffer and buffer[-1] in {">", "<"}):
            return [], "UNSUPPORTED_BACKGROUND_EXECUTION"
        if char == ";":
            if not "".join(buffer).strip():
                return [], "INVALID_SHELL_LIST"
            flush()
            connector_before = "order"
            index += 1
            continue
        if char == "\n":
            if "".join(buffer).strip():
                flush()
                connector_before = "order"
            line += 1
            segment_line = line
            index += 1
            continue
        buffer.append(char)
        if char == "\n":
            line += 1
        index += 1
    if quote or escaped or (connector_before == "success" and not "".join(buffer).strip()):
        return [], "UNBALANCED_SHELL"
    flush()
    if len(segments) > MAX_SHELL_SEGMENTS:
        return [], "BUDGET_EXCEEDED"
    if not segments:
        return [], "EMPTY_COMMAND"
    first_words = []
    for segment in segments:
        try:
            words = shlex.split(segment.text, posix=True)
        except ValueError:
            return [], "UNBALANCED_SHELL"
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", words[0]):
            words.pop(0)
        first_words.append(words[0] if words else "")
    shell_state = {"cd", "export", "unset", "set", "umask", "trap", "source", ".", "exec"}
    control_words = {
        "if", "then", "else", "elif", "fi", "for", "while", "until", "case", "esac",
        "select", "function", "{", "}", "do", "done", "in", "[[", "((", "!", "time",
    }
    if any(word in control_words for word in first_words):
        return [], "UNSUPPORTED_CONTROL_STRUCTURE"
    if any(word in shell_state for word in first_words) and len(segments) > 1:
        return [], "SHELL_STATE_BARRIER"
    if "<<" in command:
        return [], "UNSUPPORTED_HEREDOC"
    return segments, None


def _simple_argv(command: str) -> tuple[list[str] | None, dict[str, str]]:
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None, {}
    if not words:
        return None, {}
    if re.search(r"(?:^|[^\\])[$*?\[\]{}~<>]", command):
        return None, {}
    environment: dict[str, str] = {}
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", words[0]):
        key, value = words.pop(0).split("=", 1)
        environment[key] = value
    return (words or None), environment


def _flag_value(argv: list[str], *names: str) -> list[str]:
    values: list[str] = []
    for index, item in enumerate(argv):
        if item in names and index + 1 < len(argv):
            values.append(argv[index + 1])
        for name in names:
            prefix = name + "="
            if item.startswith(prefix):
                values.append(item[len(prefix):])
    return values


def _native_parallelism(argv: list[str] | None) -> dict[str, Any]:
    """Return a conservative inner-scheduler declaration for one argv.

    An explicit positive worker count is a bounded claim.  A recognized tool
    without a literal bound remains a native scheduler; malformed, dynamic,
    percentage, or unlimited values are never guessed into a numeric budget.
    """
    if not argv:
        return {"kind": "unknown", "tokens": None}
    executable = _executable_name(argv[0])
    pytest_runner = _is_pytest_runner(argv)
    native = {
        "make", "gmake", "ninja", "pytest", "jest", "vitest", "xargs",
        "turbo", "nx", "lerna", "docker-compose",
    }
    if executable == "docker" and len(argv) > 1 and argv[1] == "compose":
        native.add("docker")
    if executable not in native and not pytest_runner:
        return {"kind": "unknown", "tokens": None}

    flags: tuple[str, ...]
    if executable in {"make", "gmake", "ninja"}:
        flags = ("-j", "--jobs")
    elif executable == "pytest" or pytest_runner:
        flags = ("-n", "--numprocesses")
    elif executable in {"jest", "vitest"}:
        flags = ("--maxWorkers", "--max-workers")
    elif executable == "xargs":
        flags = ("-P", "--max-procs")
    elif executable in {"docker", "docker-compose"}:
        flags = ("--parallel",)
    else:
        flags = ("--concurrency", "--parallel", "--max-parallel")

    for index, item in enumerate(argv):
        for flag in flags:
            value: str | None = None
            if item == flag and index + 1 < len(argv):
                value = argv[index + 1]
            elif item.startswith(flag + "="):
                value = item[len(flag) + 1:]
            elif len(flag) == 2 and item.startswith(flag) and item != flag:
                value = item[len(flag):]
            if value is not None and value.isdigit() and int(value) > 0:
                return {"kind": "bounded", "tokens": int(value)}
    return {"kind": "native_scheduler", "tokens": None}


PYTEST_DISTRIBUTIONS = {
    "load",
    "loadfile",
    "loadscope",
    "loadgroup",
    "worksteal",
}
MAX_PYTEST_SCOPE_ENTRIES = 200_000
MAX_PYTEST_RUNNER_BYTES = 512 * 1024 * 1024
PYTEST_CONFIG_NAMES = (
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)
PYTEST_EMPTY_CONFIG = Path(__file__).resolve().parents[1] / "assets" / "empty-pytest.ini"
PYTEST_BASETEMP_NAME_RE = re.compile(
    r"^atomlane-pytest-[0-9a-f]{32}-tmp$",
    re.IGNORECASE,
)
PYTEST_CRITICAL_ENV = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "PYTEST_DEBUG",
)
PYTEST_FORCED_EMPTY_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONOPTIMIZE")
PYTEST_SELECTOR_VALUE_OPTIONS = {
    "-k",
    "-m",
    "-o",
    "--override-ini",
    "-p",
    "-r",
    "-W",
    "--maxfail",
    "--tb",
    "--capture",
    "--show-capture",
    "--color",
    "--code-highlight",
    "--import-mode",
    "--doctest-glob",
    "--ignore",
    "--ignore-glob",
    "--deselect",
    "--durations",
    "--durations-min",
    "--junit-prefix",
    "--junit-suite-name",
    "--verbosity",
    "--assert",
    "--pythonwarnings",
    "--pdbcls",
    "--junitprefix",
    "--doctest-report",
    "--lfnf",
    "--last-failed-no-failures",
    "--log-level",
    "--log-format",
    "--log-date-format",
    "--log-cli-level",
    "--log-cli-format",
    "--log-cli-date-format",
    "--log-file-level",
    "--log-file-format",
    "--log-file-date-format",
    "--log-file-mode",
    "--log-auto-indent",
    "--log-disable",
}
PYTEST_SELECTOR_NO_VALUE_OPTIONS = {
    "-q",
    "--quiet",
    "-v",
    "--verbose",
    "-s",
    "-x",
    "--exitfirst",
    "-l",
    "--showlocals",
    "--no-showlocals",
    "--no-header",
    "--no-summary",
    "--disable-warnings",
    "--full-trace",
    "--strict-config",
    "--strict-markers",
    "--continue-on-collection-errors",
    "--runxfail",
    "--lf",
    "--last-failed",
    "--ff",
    "--failed-first",
    "--nf",
    "--new-first",
    "--cache-clear",
    "--sw",
    "--stepwise",
    "--sw-skip",
    "--stepwise-skip",
    "--trace-config",
    "--doctest-modules",
    "--doctest-continue-on-failure",
}
PYTEST_OWNED_OPTIONS = {
    "--",
    "-c",
    "--config-file",
    "--rootdir",
    "--confcutdir",
    "-o",
    "--override-ini",
    "-n",
    "--numprocesses",
    "--maxprocesses",
    "--dist",
    "-d",
    "-f",
    "--looponfail",
    "--tx",
    "--px",
    "--max-worker-restart",
    "--ramp",
    "--maxschedchunk",
    "--loadscope-reorder",
    "--no-loadscope-reorder",
    "--rsyncdir",
    "--rsyncignore",
    "--basetemp",
    "--junitxml",
    "--junit-xml",
    "--pdb",
    "--trace",
    "-h",
    "--help",
    "--version",
    "-V",
    "--fixtures",
    "--funcargs",
    "--fixtures-per-test",
    "--markers",
    "--cache-show",
    "--debug",
    "--pastebin",
    "--log-file",
    "--collect-only",
    "--collectonly",
    "--co",
    "--collect-in-virtualenv",
    "--pyargs",
    "--lf",
    "--last-failed",
    "--ff",
    "--failed-first",
    "--nf",
    "--new-first",
    "--cache-clear",
    "--sw",
    "--stepwise",
    "--sw-skip",
    "--stepwise-skip",
    "--lfnf",
    "--last-failed-no-failures",
    "--setup-only",
    "--setuponly",
    "--setup-plan",
    "--setupplan",
}


def _executable_name(value: str) -> str:
    """Normalize POSIX and Windows executable names without host guessing."""
    name = ntpath.basename(value.replace("/", "\\")).casefold()
    return name.removesuffix(".exe")


def _is_pytest_runner(argv: list[str]) -> bool:
    executable = _executable_name(argv[0])
    if executable in {"pytest", "py.test"}:
        return True
    if not re.fullmatch(r"(?:python|pythonw|py)(?:[0-9]+(?:\.[0-9]+)*)?", executable):
        return False
    return any(
        argv[index] == "-m" and argv[index + 1].casefold() == "pytest"
        for index in range(len(argv) - 1)
    )


def _is_exact_pytest_runner_prefix(argv: list[str]) -> bool:
    """Accept only a runner prefix; selectors and pytest flags belong in arguments."""
    executable = _executable_name(argv[0])
    if executable in {"pytest", "py.test"}:
        return False
    if not re.fullmatch(r"(?:python|pythonw|py)(?:[0-9]+(?:\.[0-9]+)*)?", executable):
        return False
    prefix = argv[1:-2]
    if argv[-2:] != ["-m", "pytest"]:
        return False
    # Optimization removes ordinary ``assert`` statements and can turn a
    # failing suite into a passing JUnit report.  It is unsafe for evidence.
    allowed_switches = {"-I", "-E", "-B", "-s", "-S", "-u", "-P"}
    if executable == "py" and prefix and re.fullmatch(r"-[23](?:\.\d+)?", prefix[0]):
        prefix = prefix[1:]
    return all(item in allowed_switches for item in prefix)


def _pytest_owned_options(argv: list[str]) -> list[str]:
    found: list[str] = []
    for index, item in enumerate(argv):
        if item.startswith("@"):
            found.append("@response-file")
            continue
        if len(item) > 2 and item.startswith("-") and not item.startswith("--"):
            first = item[1]
            if first in {"c", "n"}:
                found.append(f"-{first}")
                continue
            if first not in {"k", "m", "o", "p", "r", "W"}:
                short_flags = item[1:]
                forbidden = next(
                    (
                        flag
                        for flag in ("c", "n", "d", "f", "h", "V")
                        if flag in short_flags
                    ),
                    None,
                )
                if forbidden is not None:
                    found.append(f"-{forbidden}")
                    continue
                if not set(short_flags) <= {"q", "v", "s", "x", "l"}:
                    found.append("ambiguous short option cluster")
                    continue
        override_value: str | None = None
        if item in {"-o", "--override-ini"} and index + 1 < len(argv):
            override_value = argv[index + 1]
        elif item.startswith("-o") and item != "-o":
            override_value = item[2:]
        elif item.startswith("--override-ini="):
            override_value = item.split("=", 1)[1]
        if override_value is not None:
            found.append("ini override")
            continue
        for option in PYTEST_OWNED_OPTIONS:
            if item == option or item.startswith(option + "="):
                found.append(option)
                break
            if option in {"-c", "-n", "-o"} and item.startswith(option) and item != option:
                found.append(option)
                break
    return found


def _pytest_owned_option(argv: list[str]) -> str | None:
    found = _pytest_owned_options(argv)
    return found[0] if found else None


def _pytest_plugin_control(argv: list[str]) -> str | None:
    """Reject user/config attempts to disable xdist or re-enable shared cache."""
    for index, item in enumerate(argv):
        plugin: str | None = None
        if item == "-p" and index + 1 < len(argv):
            plugin = argv[index + 1]
        elif item.startswith("-p") and item != "-p":
            plugin = item[2:]
        if plugin is None:
            continue
        normalized = plugin.casefold().removeprefix("no:")
        if normalized in {"xdist", "xdist.plugin", "cacheprovider"}:
            return plugin
    return None


def _pytest_environment_plugin_control(value: str) -> str | None:
    """Reject environment-loaded plugins that collide with owned controls."""
    for plugin in value.split(","):
        plugin = plugin.strip()
        normalized = plugin.casefold().removeprefix("no:")
        if normalized in {"xdist", "xdist.plugin", "cacheprovider"}:
            return plugin
    return None


def _validate_pytest_tokens(
    tokens: list[str],
    *,
    label: str,
    reject_all_ini_overrides: bool = False,
) -> None:
    owned = _pytest_owned_option(tokens)
    if owned:
        raise AtomError(
            f"{label} must not supply AtomLane-owned or non-executing option {owned}"
        )
    if reject_all_ini_overrides and any(
        item in {"-o", "--override-ini"}
        or item.startswith("-o") and item != "-o"
        or item.startswith("--override-ini=")
        for item in tokens
    ):
        raise AtomError(f"{label} must not contain an ini override")
    plugin = _pytest_plugin_control(tokens)
    if plugin is not None:
        raise AtomError(
            f"{label} must not alter AtomLane's xdist/cacheprovider plugin controls: {plugin}"
        )


def _parse_ini_pytest_config(path: Path) -> tuple[bool, str | None]:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(_strict_read_text(path, f"pytest config {path}"))
    except configparser.Error as exc:
        raise AtomError(f"pytest config is not parseable: {path}: {exc}") from exc
    if path.suffix == ".cfg":
        if parser.has_section("pytest"):
            raise AtomError(
                f"setup.cfg uses unsupported [pytest]; rename it to [tool:pytest]: {path}"
            )
        section = "tool:pytest"
        valid = parser.has_section(section)
    else:
        section = "pytest"
        valid = parser.has_section(section) or path.name == "pytest.ini"
    # iniconfig/pytest does not inherit ConfigParser's [DEFAULT] values.
    section_values = parser._sections.get(section, {}) if valid else {}
    addopts = section_values.get("addopts")
    return valid, addopts


def _parse_toml_pytest_config(path: Path) -> tuple[bool, str | list[str] | None]:
    if tomllib is None:
        raise AtomError(
            f"cannot safely parse pytest TOML on this Python runtime: {path}; "
            "use pytest.ini or install tomli"
        )
    try:
        payload = tomllib.loads(_strict_read_text(path, f"pytest config {path}"))
    except Exception as exc:
        raise AtomError(f"pytest TOML config is not parseable: {path}: {exc}") from exc
    if path.name in {"pytest.toml", ".pytest.toml"}:
        raise AtomError(
            f"version-specific pytest.toml is not supported by the portable contract: {path}"
        )
    tool = payload.get("tool", {})
    if not isinstance(tool, dict):
        return False, None
    pytest_table = tool.get("pytest", {})
    if not isinstance(pytest_table, dict):
        raise AtomError(f"pyproject [tool.pytest] must be a table: {path}")
    native_keys = set(pytest_table) - {"ini_options"}
    ini_options = pytest_table.get("ini_options")
    if native_keys and ini_options is not None:
        raise AtomError(
            f"pyproject cannot mix [tool.pytest] values and [tool.pytest.ini_options]: {path}"
        )
    if native_keys:
        raise AtomError(
            f"version-specific [tool.pytest] TOML is not supported; use [tool.pytest.ini_options]: {path}"
        )
    if ini_options is None:
        return False, None
    if not isinstance(ini_options, dict):
        raise AtomError(f"pyproject [tool.pytest.ini_options] must be a table: {path}")
    return True, ini_options.get("addopts")


def _pytest_config_addopts(path: Path) -> tuple[bool, list[str]]:
    if path.suffix == ".toml":
        valid, raw = _parse_toml_pytest_config(path)
    else:
        valid, raw = _parse_ini_pytest_config(path)
    if raw is None:
        return valid, []
    if isinstance(raw, str):
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError as exc:
            raise AtomError(f"pytest config addopts is not parseable: {path}") from exc
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        # Pytest's TOML adapter keeps every list member as one argv token.
        tokens = list(raw)
    else:
        raise AtomError(f"pytest config addopts must be a string or string array: {path}")
    _validate_pytest_tokens(
        tokens,
        label=f"pytest config addopts in {path}",
        reject_all_ini_overrides=True,
    )
    return valid, tokens


def _pytest_config_path_values(path: Path) -> dict[str, list[str]]:
    """Read path-bearing pytest ini options with pytest-compatible token shapes."""
    raw_values: dict[str, Any] = {}
    if path.suffix == ".toml":
        if tomllib is None:
            raise AtomError(
                f"cannot safely parse pytest TOML on this Python runtime: {path}; "
                "use pytest.ini or install tomli"
            )
        try:
            payload = tomllib.loads(_strict_read_text(path, f"pytest config {path}"))
        except Exception as exc:
            raise AtomError(f"pytest TOML config is not parseable: {path}: {exc}") from exc
        tool = payload.get("tool", {})
        pytest_table = tool.get("pytest", {}) if isinstance(tool, dict) else {}
        ini_options = (
            pytest_table.get("ini_options", {})
            if isinstance(pytest_table, dict)
            else {}
        )
        if isinstance(ini_options, dict):
            raw_values = ini_options
    else:
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        try:
            parser.read_string(_strict_read_text(path, f"pytest config {path}"))
        except configparser.Error as exc:
            raise AtomError(f"pytest config is not parseable: {path}: {exc}") from exc
        section = "tool:pytest" if path.suffix == ".cfg" else "pytest"
        raw_values = parser._sections.get(section, {})

    parsed: dict[str, list[str]] = {}
    for option in ("testpaths", "pythonpath"):
        raw = raw_values.get(option)
        if raw is None:
            parsed[option] = []
        elif isinstance(raw, str):
            try:
                parsed[option] = shlex.split(raw, posix=True)
            except ValueError as exc:
                raise AtomError(
                    f"pytest config {option} is not parseable: {path}"
                ) from exc
        elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            parsed[option] = list(raw)
        else:
            raise AtomError(
                f"pytest config {option} must be a string or string array: {path}"
            )
    return parsed


def _validate_pytest_config_paths(
    path: Path, project: Path, label: str
) -> dict[str, list[Path]]:
    resolved_values: dict[str, list[Path]] = {"testpaths": [], "pythonpath": []}
    for option, values in _pytest_config_path_values(path).items():
        for value in values:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            lexical = Path(os.path.abspath(candidate))
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(project)
            except ValueError as exc:
                raise AtomError(
                    f"{label} pytest {option} paths must stay inside project_path"
                ) from exc
            if not candidate.exists():
                raise AtomError(
                    f"{label} pytest {option} path must exist when the plan is compiled: "
                    f"{candidate}"
                )
            resolved = candidate.resolve(strict=True)
            if lexical != resolved:
                raise AtomError(
                    f"{label} pytest {option} paths must not contain symbolic-link aliases"
                )
            if option == "pythonpath" and not resolved.is_dir():
                raise AtomError(f"{label} pytest pythonpath entries must be directories")
            resolved_values[option].append(resolved)
    return resolved_values


def _validate_pytest_selector_boundaries(
    project: Path,
    cwd: Path,
    tokens: list[str],
    label: str,
) -> list[Path]:
    """Keep every positional pytest selector inside the immutable project scope."""
    selectors: list[Path] = []
    skip_value = False
    for item in tokens:
        if skip_value:
            skip_value = False
            continue
        if item in PYTEST_SELECTOR_VALUE_OPTIONS:
            skip_value = True
            continue
        if item.startswith("-"):
            continue
        path_text = item.split("::", 1)[0]
        if not path_text:
            raise AtomError(f"{label} contains an empty pytest selector")
        selector = Path(path_text).expanduser()
        if not selector.is_absolute():
            selector = cwd / selector
        if not selector.exists():
            raise AtomError(
                f"{label} test selector must exist when the plan is compiled: {selector}"
            )
        lexical = Path(os.path.abspath(selector))
        strict_resolved = selector.resolve(strict=True)
        try:
            strict_resolved.relative_to(project)
        except ValueError as exc:
            raise AtomError(f"{label} test selectors must stay inside project_path") from exc
        if lexical != strict_resolved:
            raise AtomError(
                f"{label} test selectors must not contain symbolic-link aliases"
            )
        selectors.append(strict_resolved)
    return selectors


def _validate_pytest_collection_boundaries(
    project: Path,
    roots: list[Path],
    label: str,
    discovered_links: set[Path] | None = None,
) -> set[Path]:
    """Reject escaping collection trees and return discoverable conftests."""
    pending = [(root, True) for root in roots]
    visited_directories: set[tuple[str, ...]] = set()
    discovered_conftests: set[Path] = set()
    inspected = 0
    while pending:
        current, is_explicit_root = pending.pop()
        try:
            current_state = current.lstat()
            resolved = current.resolve(strict=True)
        except OSError as exc:
            raise AtomError(
                f"{label} pytest collection scope cannot be inspected: {current}"
            ) from exc
        try:
            resolved.relative_to(project)
        except ValueError as exc:
            raise AtomError(
                f"{label} pytest collection scope must stay inside project_path"
            ) from exc
        if stat.S_ISLNK(current_state.st_mode) or getattr(
            current_state, "st_reparse_tag", 0
        ):
            try:
                resolved_state = resolved.stat()
            except OSError as exc:
                raise AtomError(
                    f"{label} pytest collection link cannot be inspected: {current}"
                ) from exc
        else:
            resolved_state = current_state
        if not stat.S_ISDIR(resolved_state.st_mode):
            continue
        if not is_explicit_root and (
            (resolved / "pyvenv.cfg").is_file()
            or (resolved / "conda-meta" / "history").is_file()
        ):
            # Match pytest's default virtual-environment collection exclusion.
            # An explicitly selected environment remains audited.
            continue
        directory_identity = _pytest_directory_visit_key(resolved, resolved_state)
        if directory_identity in visited_directories:
            continue
        visited_directories.add(directory_identity)
        try:
            with os.scandir(resolved) as entries:
                for entry in entries:
                    inspected += 1
                    if inspected > MAX_PYTEST_SCOPE_ENTRIES:
                        raise AtomError(
                            f"{label} pytest collection scope exceeds the bounded "
                            "static audit; use narrower selectors or testpaths"
                        )
                    entry_path = Path(entry.path)
                    try:
                        entry_state = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise AtomError(
                            f"{label} pytest collection entry cannot be inspected: "
                            f"{entry_path}"
                        ) from exc
                    is_link = stat.S_ISLNK(entry_state.st_mode) or bool(
                        getattr(entry_state, "st_reparse_tag", 0)
                    )
                    if is_link:
                        if discovered_links is not None:
                            discovered_links.add(Path(os.path.abspath(entry_path)))
                        try:
                            target = entry_path.resolve(strict=True)
                            target.relative_to(project)
                        except (OSError, ValueError) as exc:
                            raise AtomError(
                                f"{label} pytest collection link escapes project_path: "
                                f"{entry_path}"
                            ) from exc
                        if target.is_dir():
                            pending.append((entry_path, False))
                        elif entry.name == "conftest.py" and target.is_file():
                            discovered_conftests.add(target)
                    elif stat.S_ISDIR(entry_state.st_mode):
                        pending.append((entry_path, False))
                    elif (
                        entry.name == "conftest.py"
                        and stat.S_ISREG(entry_state.st_mode)
                    ):
                        discovered_conftests.add(entry_path.resolve(strict=True))
        except AtomError:
            raise
        except OSError as exc:
            raise AtomError(
                f"{label} pytest collection directory cannot be inspected: {current}"
            ) from exc
    return discovered_conftests


def _pytest_baseline_source_coverage(
    project: Path,
    cwd: Path,
    tokens: list[str],
    config_path: Path,
    explicit_paths: list[Path],
    label: str,
) -> bool:
    """Require direct collection scopes and conftests inside declared snapshots."""
    selectors = _validate_pytest_selector_boundaries(project, cwd, tokens, label)
    config_paths = _validate_pytest_config_paths(config_path, project, label)
    collection_roots = selectors or config_paths["testpaths"] or [cwd]
    discovered_links: set[Path] = set()
    discovered_conftests = _validate_pytest_collection_boundaries(
        project,
        collection_roots,
        label,
        discovered_links,
    )
    source_roots = [*collection_roots, *config_paths["pythonpath"]]
    explicit = {path.resolve(strict=True) for path in explicit_paths}
    if not explicit or discovered_links:
        return False
    if not discovered_conftests.issubset(explicit):
        return False

    for root in source_roots:
        root = root.resolve(strict=True)
        if root.is_file():
            if root not in explicit:
                return False
            conftest_dir = root.parent
        else:
            if not any(path == root or root in path.parents for path in explicit):
                return False
            conftest_dir = root
        current = conftest_dir
        while True:
            conftest = current / "conftest.py"
            if conftest.is_file() and conftest.resolve(strict=True) not in explicit:
                return False
            if current == project or current.parent == current:
                break
            current = current.parent
    return True


def _pytest_directory_visit_key(path: Path, state: os.stat_result) -> tuple[str, ...]:
    """Use an inode only when the filesystem supplies a meaningful one."""
    inode = int(getattr(state, "st_ino", 0) or 0)
    if inode:
        return ("inode", str(getattr(state, "st_dev", 0)), str(inode))
    return ("path", os.path.normpath(os.path.abspath(os.fspath(path))))


def _resolve_pytest_config(
    project: Path,
    cwd: Path,
    raw_config_path: Any,
    initial_args: list[str],
    label: str,
) -> tuple[Path, Path, list[str], str]:
    """Select one immutable config inside the declared project boundary."""
    try:
        cwd.relative_to(project)
    except ValueError as exc:
        raise AtomError(f"{label} cwd must stay inside project_path") from exc
    _validate_pytest_selector_boundaries(project, cwd, initial_args, label)
    if raw_config_path is not None:
        if (
            not isinstance(raw_config_path, str)
            or not raw_config_path
            or "\x00" in raw_config_path
        ):
            raise AtomError(f"{label} config_path must be a non-empty path")
        unresolved = Path(raw_config_path).expanduser()
        if not unresolved.is_absolute():
            unresolved = project / unresolved
        if unresolved.is_symlink():
            raise AtomError(f"{label} config_path must not be a symbolic link")
        config_path = unresolved.resolve(strict=True)
        try:
            config_path.relative_to(project)
        except ValueError as exc:
            raise AtomError(f"{label} config_path must stay inside project_path") from exc
        if not config_path.is_file():
            raise AtomError(f"{label} config_path is not a file: {config_path}")
        if config_path.suffix not in {".ini", ".cfg", ".toml"}:
            raise AtomError(
                f"{label} config_path must use a pytest-supported .ini, .cfg, or .toml suffix"
            )
        if config_path.name in {"pytest.toml", ".pytest.toml"}:
            raise AtomError(
                f"{label} config_path uses version-specific pytest TOML; use pytest.ini or pyproject [tool.pytest.ini_options]"
            )
        valid, addopts = _pytest_config_addopts(config_path)
        if not valid:
            raise AtomError(f"{label} config_path has no pytest configuration: {config_path}")
        return config_path, config_path.parent, addopts, "pytest_config"

    selector_paths: list[Path] = []
    skip_value = False
    for index, item in enumerate(initial_args):
        if skip_value:
            skip_value = False
            continue
        if item in PYTEST_SELECTOR_VALUE_OPTIONS:
            skip_value = True
            continue
        if item.startswith("-"):
            if (
                "=" not in item
                and item not in PYTEST_SELECTOR_NO_VALUE_OPTIONS
                and not (
                    len(item) > 2
                    and item.startswith(("-k", "-m", "-o", "-p", "-r", "-W"))
                )
                and index + 1 < len(initial_args)
            ):
                next_text = initial_args[index + 1].split("::", 1)[0]
                next_path = Path(next_text).expanduser()
                if not next_path.is_absolute():
                    next_path = cwd / next_path
                if next_path.exists():
                    raise AtomError(
                        f"{label} cannot infer whether {item} consumes a path; "
                        "use --option=value or provide config_path explicitly"
                    )
            continue
        path_text = item.split("::", 1)[0]
        selector = Path(path_text).expanduser()
        if not selector.is_absolute():
            selector = cwd / selector
        if not selector.exists():
            continue
        selector = selector.resolve(strict=True)
        selector_dir = selector if selector.is_dir() else selector.parent
        try:
            selector_dir.relative_to(project)
        except ValueError as exc:
            raise AtomError(f"{label} test selectors must stay inside project_path") from exc
        selector_paths.append(selector_dir)
    if selector_paths:
        try:
            search_base = Path(os.path.commonpath([str(path) for path in selector_paths]))
        except ValueError as exc:
            raise AtomError(f"{label} test selectors do not share one filesystem root") from exc
    else:
        search_base = cwd

    fallback_pyproject: tuple[Path, list[str]] | None = None
    current = search_base
    while True:
        for version_specific_name in ("pytest.toml", ".pytest.toml"):
            if (current / version_specific_name).is_file():
                raise AtomError(
                    f"{label} found version-specific {version_specific_name}; "
                    "use pytest.ini or pyproject [tool.pytest.ini_options]"
                )
        for name in PYTEST_CONFIG_NAMES:
            candidate = current / name
            if not candidate.is_file():
                continue
            if candidate.is_symlink():
                raise AtomError(f"{label} discovered pytest config must not be a symbolic link")
            valid, addopts = _pytest_config_addopts(candidate)
            if valid:
                try:
                    candidate.relative_to(project)
                except ValueError as exc:
                    raise AtomError(
                        f"{label} would inherit pytest config outside project_path; "
                        "expand project_path or provide config_path"
                    ) from exc
                return (
                    candidate.resolve(strict=True),
                    current,
                    addopts,
                    "pytest_config",
                )
            if name == "pyproject.toml" and fallback_pyproject is None:
                fallback_pyproject = (candidate.resolve(strict=True), addopts)
        if current.parent == current:
            break
        current = current.parent
    if fallback_pyproject is not None:
        try:
            fallback_pyproject[0].relative_to(project)
        except ValueError as exc:
            raise AtomError(
                f"{label} would inherit pyproject.toml outside project_path; "
                "expand project_path or provide config_path"
            ) from exc
        return (
            fallback_pyproject[0],
            fallback_pyproject[0].parent,
            fallback_pyproject[1],
            "fallback_pyproject",
        )
    if len(set(selector_paths)) > 1:
        raise AtomError(
            f"{label} has multiple selector roots without a common pytest config; "
            "provide config_path explicitly"
        )
    empty_rootdir: Path | None = None
    current = search_base
    while True:
        if (current / "setup.py").is_file():
            try:
                current.relative_to(project)
            except ValueError as exc:
                raise AtomError(
                    f"{label} would inherit setup.py root outside project_path; "
                    "expand project_path or provide config_path"
                ) from exc
            empty_rootdir = current
            break
        if current.parent == current:
            break
        current = current.parent
    if empty_rootdir is None:
        empty_rootdir = Path(os.path.commonpath([str(cwd), str(search_base)]))
    if not PYTEST_EMPTY_CONFIG.is_file() or PYTEST_EMPTY_CONFIG.is_symlink():
        raise AtomError("AtomLane's bundled empty pytest config is unavailable")
    valid, addopts = _pytest_config_addopts(PYTEST_EMPTY_CONFIG)
    if not valid or addopts:
        raise AtomError("AtomLane's bundled empty pytest config is invalid")
    return (
        PYTEST_EMPTY_CONFIG.resolve(strict=True),
        empty_rootdir,
        [],
        "bundled_empty",
    )


def _bounded_string_array(value: Any, label: str, maximum: int) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or not all(
            isinstance(item, str)
            and "\x00" not in item
            and len(item) <= MAX_COMMAND_CHARS
            for item in value
        )
    ):
        raise AtomError(f"{label} must be an array of at most {maximum} bounded strings")
    return list(value)


def _resolve_entrypoint_cwd(project: Path, raw: Any, label: str) -> Path:
    cwd = Path(raw if raw is not None else project).expanduser()
    if not cwd.is_absolute():
        cwd = project / cwd
    cwd = cwd.resolve(strict=False)
    if not cwd.is_dir():
        raise AtomError(f"{label} cwd does not exist: {cwd}")
    return cwd


def _resolve_test_runner(argv: list[str], effective_os: str, label: str) -> list[str]:
    resolved = list(argv)
    executable = Path(argv[0]).expanduser()
    # Parser tests can target the other OS without pretending its executable is
    # available on this host. Production compilation always takes this branch.
    if effective_os == os.name:
        if executable.is_absolute():
            # Preserve virtual-environment and shim paths. Dereferencing a
            # venv's Python symlink silently switches sys.prefix and loses the
            # environment that owns pytest/xdist.
            candidate = Path(os.path.abspath(os.fspath(executable)))
            if not candidate.is_file():
                raise AtomError(f"{label} runner executable does not exist: {candidate}")
            resolved[0] = str(candidate)
        else:
            candidate = resolve_host_executable(argv[0])
            if not candidate:
                raise AtomError(f"{label} runner executable is unavailable: {argv[0]}")
            resolved[0] = os.path.abspath(candidate)
    if not _is_exact_pytest_runner_prefix(resolved):
        raise AtomError(
            f"{label} runner_argv must be an exact pytest runner prefix; put selectors and pytest flags in arguments"
        )
    return resolved


def _pytest_runner_attestation(path: Path, label: str) -> dict[str, Any]:
    """Hash-bind the resolved interpreter while preserving a venv symlink argv."""
    invocation = Path(os.path.abspath(os.fspath(path)))
    try:
        invocation_state = invocation.lstat()
        resolved = invocation.resolve(strict=True)
        resolved_state = resolved.lstat()
    except OSError as exc:
        raise AtomError(f"{label} runner executable cannot be inspected") from exc
    if (
        stat.S_ISLNK(resolved_state.st_mode)
        or getattr(resolved_state, "st_reparse_tag", 0)
        or not stat.S_ISREG(resolved_state.st_mode)
        or resolved_state.st_ino == 0
        or not 0 < resolved_state.st_size <= MAX_PYTEST_RUNNER_BYTES
    ):
        raise AtomError(
            f"{label} resolved runner must be a regular file of at most "
            f"{MAX_PYTEST_RUNNER_BYTES} bytes"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise AtomError(f"{label} resolved runner cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity != (resolved_state.st_dev, resolved_state.st_ino)
            or opened.st_size != resolved_state.st_size
        ):
            raise AtomError(f"{label} resolved runner changed while being opened")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PYTEST_RUNNER_BYTES:
                raise AtomError(f"{label} resolved runner exceeds the size limit")
            digest.update(chunk)
        settled = os.fstat(descriptor)
        final_path_state = resolved.lstat()
        if (
            (settled.st_dev, settled.st_ino) != identity
            or (final_path_state.st_dev, final_path_state.st_ino) != identity
            or settled.st_size != opened.st_size
            or settled.st_mtime_ns != opened.st_mtime_ns
            or settled.st_ctime_ns != opened.st_ctime_ns
            or invocation.resolve(strict=True) != resolved
            or invocation.lstat().st_mode != invocation_state.st_mode
        ):
            raise AtomError(f"{label} runner changed while it was hashed")
    except OSError as exc:
        raise AtomError(f"{label} resolved runner cannot be read safely") from exc
    finally:
        os.close(descriptor)
    return {
        "schema": "atomlane/pytest-runner-attestation/v1",
        "invocation_path": str(invocation),
        "resolved_path": str(resolved),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "size": opened.st_size,
        "mode": stat.S_IMODE(opened.st_mode),
        "mtime_ns": opened.st_mtime_ns,
        "ctime_ns": opened.st_ctime_ns,
        "sha256": digest.hexdigest(),
    }


def _reject_pytest_module_shadowing(
    cwd: Path,
    python_paths: list[Path],
    label: str,
) -> None:
    """Keep pytest and the owned xdist plugin on the trusted runner path."""
    for root in {cwd.resolve(strict=True), *(path.resolve(strict=True) for path in python_paths)}:
        for module in ("pytest", "xdist"):
            candidates = [root / module, root / f"{module}.py", root / f"{module}.pyc"]
            try:
                candidates.extend(
                    path
                    for path in root.glob(f"{module}.*")
                    if path.suffix.casefold() in {".so", ".pyd", ".dll"}
                )
            except OSError as exc:
                raise AtomError(
                    f"{label} module-shadowing scope cannot be inspected: {root}"
                ) from exc
            if any(path.exists() or path.is_symlink() for path in candidates):
                raise AtomError(
                    f"{label} project paths must not shadow the trusted {module} module"
                )


def _path_identity(path: Path | str, effective_os: str) -> str:
    value = os.fspath(path)
    normalized = (
        ntpath.normpath(value)
        if effective_os == "nt"
        else os.path.normpath(value)
    )
    # macOS volumes commonly normalize Unicode and fold case. Apply the same
    # conservative identity on every POSIX host so a plan proven on a
    # case-sensitive volume cannot become unsafe when moved to macOS.
    return unicodedata.normalize("NFC", normalized).casefold()


def _path_identity_is_equal_or_descendant(
    path: Path | str,
    directory: Path | str,
    effective_os: str,
) -> bool:
    """Compare paths conservatively across case-folding and Unicode aliases."""
    candidate = _path_identity(path, effective_os)
    parent = _path_identity(directory, effective_os)
    if candidate == parent:
        return True
    separator = "\\" if effective_os == "nt" else os.sep
    prefix = parent if parent.endswith(separator) else parent + separator
    return candidate.startswith(prefix)


def _path_physical_anchor_identity(
    path: Path | str,
    effective_os: str,
) -> tuple[int, int, tuple[str, ...]]:
    """Anchor a present or future path to the nearest existing filesystem object."""
    current = Path(os.path.abspath(os.fspath(path)))
    remaining: list[str] = []
    while True:
        try:
            state = current.stat()
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                raise AtomError(f"path has no existing physical ancestor: {path}")
            remaining.insert(0, _path_identity(current.name, effective_os))
            current = parent
            continue
        except OSError as exc:
            raise AtomError(f"path physical identity is unavailable: {path}") from exc
        inode = int(getattr(state, "st_ino", 0) or 0)
        if inode <= 0:
            raise AtomError(f"path physical identity is unavailable: {path}")
        return int(state.st_dev), inode, tuple(remaining)


def _path_physical_lineage(path: Path | str) -> tuple[tuple[int, int], ...]:
    """Return physical identities for each existing object on a lexical ancestry."""
    current = Path(os.path.abspath(os.fspath(path)))
    lineage: list[tuple[int, int]] = []
    while True:
        try:
            state = current.stat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AtomError(f"path physical lineage is unavailable: {path}") from exc
        else:
            inode = int(getattr(state, "st_ino", 0) or 0)
            if inode <= 0:
                raise AtomError(f"path physical lineage is unavailable: {path}")
            identity = (int(state.st_dev), inode)
            if not lineage or lineage[-1] != identity:
                lineage.append(identity)
        parent = current.parent
        if parent == current:
            return tuple(lineage)
        current = parent


def _paths_are_equivalent(
    left: Path | str,
    right: Path | str,
    effective_os: str,
) -> bool:
    """Treat lexical aliases and physically anchored aliases as one path."""
    return (
        _path_identity(left, effective_os) == _path_identity(right, effective_os)
        or _path_physical_anchor_identity(left, effective_os)
        == _path_physical_anchor_identity(right, effective_os)
    )


def _path_is_equal_or_descendant(
    candidate: Path | str,
    directory: Path | str,
    effective_os: str,
) -> bool:
    """Compare an intended path boundary through lexical and physical aliases."""
    if _path_identity_is_equal_or_descendant(
        candidate,
        directory,
        effective_os,
    ):
        return True
    candidate_anchor = _path_physical_anchor_identity(candidate, effective_os)
    directory_anchor = _path_physical_anchor_identity(directory, effective_os)
    if (
        candidate_anchor[:2] == directory_anchor[:2]
        and len(candidate_anchor[2]) >= len(directory_anchor[2])
        and candidate_anchor[2][: len(directory_anchor[2])] == directory_anchor[2]
    ):
        return True
    try:
        directory_state = Path(directory).stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AtomError(
            f"path physical identity is unavailable: {directory}"
        ) from exc
    if not stat.S_ISDIR(directory_state.st_mode):
        return False
    inode = int(getattr(directory_state, "st_ino", 0) or 0)
    if inode <= 0:
        raise AtomError(f"path physical identity is unavailable: {directory}")
    directory_identity = (int(directory_state.st_dev), inode)
    return directory_identity in _path_physical_lineage(candidate)


def _paths_overlap(
    left: Path | str,
    *,
    left_is_directory: bool,
    right: Path | str,
    right_is_directory: bool,
    effective_os: str,
) -> bool:
    """Return whether two intended file/directory paths can name overlapping storage."""
    return (
        _paths_are_equivalent(left, right, effective_os)
        or left_is_directory
        and _path_is_equal_or_descendant(right, left, effective_os)
        or right_is_directory
        and _path_is_equal_or_descendant(left, right, effective_os)
    )


def _pytest_output_overlaps_collection(
    output_path: Path | str,
    collection_root: Path,
    effective_os: str,
    *,
    output_is_directory: bool = False,
) -> bool:
    try:
        root_state = collection_root.stat()
    except OSError as exc:
        raise AtomError(
            f"pytest collection root physical identity is unavailable: {collection_root}"
        ) from exc
    return _paths_overlap(
        output_path,
        left_is_directory=output_is_directory,
        right=collection_root,
        right_is_directory=stat.S_ISDIR(root_state.st_mode),
        effective_os=effective_os,
    )


WINDOWS_RESERVED_COMPONENT_RE = re.compile(
    r"^(?:con|prn|aux|nul|conin\$|conout\$|com[0-9\u00b9\u00b2\u00b3]|"
    r"lpt[0-9\u00b9\u00b2\u00b3])$",
    re.IGNORECASE,
)


def _windows_output_path_spelling_is_unambiguous(value: str) -> bool:
    """Reject Win32 spellings that can alias another output pathname."""
    lexical = value.replace("/", "\\")
    if not lexical or "\x00" in lexical or lexical.startswith(
        ("\\\\?\\", "\\\\.\\", "\\??\\", "\\\\??\\")
    ):
        return False
    windows_path = PureWindowsPath(lexical)
    if windows_path.drive and not windows_path.root:
        return False
    if windows_path.root and not windows_path.drive:
        return False

    def component_is_safe(component: str) -> bool:
        if (
            not component
            or component[-1] in {" ", "."}
            or any(ord(character) < 32 for character in component)
            or any(character in '<>:"|?*' for character in component)
        ):
            return False
        basename = component.split(".", 1)[0].rstrip(" .")
        return WINDOWS_RESERVED_COMPONENT_RE.fullmatch(basename) is None

    if windows_path.drive.startswith("\\\\"):
        unc_parts = windows_path.drive.lstrip("\\").split("\\")
        if len(unc_parts) != 2 or not all(map(component_is_safe, unc_parts)):
            return False
    elif windows_path.drive and not re.fullmatch(r"[A-Za-z]:", windows_path.drive):
        return False
    anchor = windows_path.anchor
    for component in windows_path.parts:
        if component == anchor or component in {".", ".."}:
            continue
        if not component_is_safe(component):
            return False
    return True


def _path_is_within_reserved_pytest_basetemp(path: Path | str) -> bool:
    """Reserve generated base-temp namespaces against report placement."""
    candidate = Path(path)
    return any(
        PYTEST_BASETEMP_NAME_RE.fullmatch(parent.name) is not None
        for parent in (candidate, *candidate.parents)
    )


def _regular_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        state = path.lstat()
    except FileNotFoundError:
        return None
    if (
        stat.S_ISLNK(state.st_mode)
        or getattr(state, "st_reparse_tag", 0)
        or not stat.S_ISREG(state.st_mode)
    ):
        return None
    return state.st_dev, state.st_ino


def _snapshot_test_configuration(
    compilation: Compilation,
    project: Path,
    config_path: Path,
    snapshot_paths: list[str],
    required_paths: list[Path],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[Path] = [config_path, *required_paths]
    explicit_candidates: list[Path] = []
    for raw in snapshot_paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = project / path
        lexical = Path(os.path.abspath(path))
        path = path.resolve(strict=True)
        if lexical != path:
            raise AtomError(
                "test-suite snapshot paths must not contain symbolic-link aliases"
            )
        try:
            path.relative_to(project)
        except ValueError as exc:
            raise AtomError(f"test-suite snapshot escapes project: {path}") from exc
        if not path.is_file():
            raise AtomError(f"test-suite snapshot is not a file: {path}")
        candidates.append(path)
        if path != config_path.resolve(strict=False):
            explicit_candidates.append(path)
    accesses: list[dict[str, str]] = []
    snapshots: list[dict[str, Any]] = []
    for path in sorted(set(candidates)):
        snapshots.append(compilation.snapshot(path))
        accesses.append({"resource": str(path), "mode": "snapshot"})
    explicit_snapshots = [
        compilation.snapshot(path) for path in sorted(set(explicit_candidates))
    ]
    return accesses, snapshots, explicit_snapshots


def compile_test_suite(
    compilation: Compilation,
    entrypoint: dict[str, Any],
    *,
    entry_id: str,
    effective_os: str,
    native_worker_ceiling: int | None,
) -> Fragment:
    """Compile one explicitly trusted pytest suite into its native worker pool."""
    framework = entrypoint.get("framework", "pytest")
    if framework != "pytest":
        raise AtomError(f"entrypoint {entry_id} supports only framework=pytest in this release")
    cwd = _resolve_entrypoint_cwd(
        compilation.project,
        entrypoint.get("cwd", str(compilation.project)),
        f"entrypoint {entry_id}",
    )
    runner_argv = _bounded_string_array(
        entrypoint.get("runner_argv", []),
        f"entrypoint {entry_id} runner_argv",
        32,
    )
    if not runner_argv:
        raise AtomError(f"entrypoint {entry_id} runner_argv must not be empty")
    runner_argv = _resolve_test_runner(
        runner_argv, effective_os, f"entrypoint {entry_id}"
    )
    runner_attestation = (
        _pytest_runner_attestation(
            Path(runner_argv[0]), f"entrypoint {entry_id}"
        )
        if effective_os == os.name
        else {
            "schema": "atomlane/pytest-runner-attestation/cross-platform-static",
            "invocation_path": runner_argv[0],
        }
    )
    arguments = _bounded_string_array(
        entrypoint.get("arguments", []),
        f"entrypoint {entry_id} arguments",
        128,
    )
    _validate_pytest_tokens(arguments, label=f"entrypoint {entry_id}")

    worker_raw = entrypoint.get("worker_count", "auto")
    if worker_raw == "auto":
        worker_count = max(1, min(64, int(native_worker_ceiling or (os.cpu_count() or 1))))
        worker_source = "adaptive_host_budget"
    elif isinstance(worker_raw, int) and not isinstance(worker_raw, bool) and 1 <= worker_raw <= 64:
        worker_count = worker_raw
        worker_source = "explicit"
    else:
        raise AtomError(f"entrypoint {entry_id} worker_count must be auto or an integer from 1 to 64")
    distribution = entrypoint.get("distribution", "worksteal")
    if distribution not in PYTEST_DISTRIBUTIONS:
        raise AtomError(f"entrypoint {entry_id} has unsupported pytest distribution: {distribution}")

    case_count_hint = entrypoint.get("case_count_hint")
    if case_count_hint is not None and (
        isinstance(case_count_hint, bool)
        or not isinstance(case_count_hint, int)
        or not 1 <= case_count_hint <= 10_000_000
    ):
        raise AtomError(f"entrypoint {entry_id} case_count_hint must be a positive integer")
    if (
        worker_source == "adaptive_host_budget"
        and case_count_hint is not None
        and worker_count > case_count_hint
    ):
        worker_count = case_count_hint
        worker_source = "adaptive_host_budget_and_case_hint"
    memory_per_worker = entrypoint.get("estimated_memory_mb_per_worker")
    if memory_per_worker is not None and (
        isinstance(memory_per_worker, bool)
        or not isinstance(memory_per_worker, (int, float))
        or not 1 <= float(memory_per_worker) <= 1_048_576
    ):
        raise AtomError(
            f"entrypoint {entry_id} estimated_memory_mb_per_worker must be 1..1048576"
        )
    duration = entrypoint.get("estimated_duration_seconds")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not 0 < float(duration) <= 86_400
    ):
        raise AtomError(f"entrypoint {entry_id} estimated_duration_seconds must be 0..86400")
    timeout = entrypoint.get("timeout_seconds", 900)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < float(timeout) <= 86_400
    ):
        raise AtomError(f"entrypoint {entry_id} timeout_seconds must be 0..86400")

    declared_accesses = entrypoint.get("declared_accesses", [])
    declared_effects = entrypoint.get("declared_effects", [])
    if not isinstance(declared_accesses, list) or len(declared_accesses) > 256:
        raise AtomError(f"entrypoint {entry_id} declared_accesses must be a bounded array")
    if not isinstance(declared_effects, list) or len(declared_effects) > 128:
        raise AtomError(f"entrypoint {entry_id} declared_effects must be a bounded array")
    complete = entrypoint.get("effects_declared_complete", False)
    if not isinstance(complete, bool):
        raise AtomError(f"entrypoint {entry_id} effects_declared_complete must be boolean")
    independence_declared = entrypoint.get("independence_declared", False)
    if not isinstance(independence_declared, bool):
        raise AtomError(f"entrypoint {entry_id} independence_declared must be boolean")
    baseline_source_closure_declared = entrypoint.get(
        "baseline_source_closure_declared", False
    )
    if not isinstance(baseline_source_closure_declared, bool):
        raise AtomError(
            f"entrypoint {entry_id} baseline_source_closure_declared must be boolean"
        )
    snapshot_paths = _bounded_string_array(
        entrypoint.get("snapshot_paths", []),
        f"entrypoint {entry_id} snapshot_paths",
        256,
    )

    environment = entrypoint.get("env", {})
    if not isinstance(environment, dict) or len(environment) > 128:
        raise AtomError(f"entrypoint {entry_id} env must be a bounded object")
    environment = dict(environment)
    if not all(
        isinstance(key, str)
        and isinstance(value, str)
        and key
        and "=" not in key
        and "\x00" not in key + value
        for key, value in environment.items()
    ):
        raise AtomError(f"entrypoint {entry_id} env must contain valid string entries")
    for canonical in PYTEST_CRITICAL_ENV:
        matching = [
            key
            for key in environment
            if key == canonical
            or effective_os == "nt" and key.casefold() == canonical.casefold()
        ]
        if len(matching) > 1:
            raise AtomError(
                f"entrypoint {entry_id} env contains duplicate {canonical} keys"
            )
        ambient_value = ""
        for key, value in os.environ.items():
            if key == canonical or (
                effective_os == "nt" and key.casefold() == canonical.casefold()
            ):
                ambient_value = value
                break
        selected = environment.pop(matching[0]) if matching else ambient_value
        environment[canonical] = selected
    for canonical in PYTEST_FORCED_EMPTY_ENV:
        matching = [
            key
            for key in environment
            if key == canonical
            or effective_os == "nt" and key.casefold() == canonical.casefold()
        ]
        if len(matching) > 1:
            raise AtomError(
                f"entrypoint {entry_id} env contains duplicate {canonical} keys"
            )
        if matching and environment.pop(matching[0]):
            raise AtomError(
                f"entrypoint {entry_id} {canonical} must be empty for trusted pytest/xdist resolution"
            )
        environment[canonical] = ""
    if environment["PYTEST_DEBUG"]:
        raise AtomError(
            f"entrypoint {entry_id} PYTEST_DEBUG must be empty because it writes implicit debug output"
        )
    try:
        # Pytest itself parses PYTEST_ADDOPTS with POSIX shlex semantics on
        # every supported host. Matching it exactly prevents quoted options
        # from bypassing the worker-budget checks on Windows.
        environment_addopts = shlex.split(environment["PYTEST_ADDOPTS"], posix=True)
    except ValueError as exc:
        raise AtomError(f"entrypoint {entry_id} PYTEST_ADDOPTS is not parseable") from exc
    _validate_pytest_tokens(
        environment_addopts,
        label=f"entrypoint {entry_id} PYTEST_ADDOPTS",
    )
    environment_plugin = _pytest_environment_plugin_control(
        environment["PYTEST_PLUGINS"]
    )
    if environment_plugin is not None:
        raise AtomError(
            f"entrypoint {entry_id} PYTEST_PLUGINS must not alter AtomLane's "
            f"xdist/cacheprovider controls: {environment_plugin}"
        )
    config_path, config_rootdir, config_addopts, config_selection_kind = (
        _resolve_pytest_config(
            compilation.project,
            cwd,
            entrypoint.get("config_path"),
            [*environment_addopts, *arguments],
            f"entrypoint {entry_id}",
        )
    )
    uses_empty_config = config_selection_kind == "bundled_empty"
    validated_config_paths = _validate_pytest_config_paths(
        config_path,
        compilation.project,
        f"entrypoint {entry_id}",
    )
    validated_selectors = _validate_pytest_selector_boundaries(
        compilation.project,
        cwd,
        [*config_addopts, *environment_addopts, *arguments],
        f"entrypoint {entry_id}",
    )
    collection_roots = (
        validated_selectors or validated_config_paths["testpaths"] or [cwd]
    )
    _reject_pytest_module_shadowing(
        cwd,
        validated_config_paths["pythonpath"],
        f"entrypoint {entry_id}",
    )
    _validate_pytest_collection_boundaries(
        compilation.project,
        collection_roots,
        f"entrypoint {entry_id}",
    )
    token = secrets.token_hex(16)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    basetemp = temp_root / f"atomlane-pytest-{token}-tmp"
    junit_raw = entrypoint.get("junit_path")
    if junit_raw is None:
        junit_path = temp_root / f"atomlane-pytest-{token}-junit.xml"
    else:
        if not isinstance(junit_raw, str) or not junit_raw or "\x00" in junit_raw:
            raise AtomError(f"entrypoint {entry_id} junit_path must be a non-empty path")
        if effective_os == "nt" and not _windows_output_path_spelling_is_unambiguous(
            junit_raw
        ):
            raise AtomError(
                f"entrypoint {entry_id} junit_path has an ambiguous or reserved "
                "Windows pathname"
            )
        unresolved_junit = Path(junit_raw).expanduser()
        if not unresolved_junit.is_absolute():
            unresolved_junit = cwd / unresolved_junit
        try:
            unresolved_state = unresolved_junit.lstat()
        except FileNotFoundError:
            unresolved_state = None
        if unresolved_state is not None and (
            stat.S_ISLNK(unresolved_state.st_mode)
            or getattr(unresolved_state, "st_reparse_tag", 0)
            or not stat.S_ISREG(unresolved_state.st_mode)
        ):
            raise AtomError(
                f"entrypoint {entry_id} junit_path must be absent or a non-link regular file"
            )
        if unresolved_state is not None and unresolved_state.st_nlink != 1:
            raise AtomError(
                f"entrypoint {entry_id} junit_path aliases another filesystem entry"
            )
        junit_path = unresolved_junit.resolve(strict=False)
        if not junit_path.parent.is_dir():
            raise AtomError(
                f"entrypoint {entry_id} junit_path parent does not exist: {junit_path.parent}"
            )
    if _path_is_within_reserved_pytest_basetemp(junit_path):
        raise AtomError(
            f"entrypoint {entry_id} junit_path must not use AtomLane's reserved "
            "pytest base-temp namespace"
        )
    _path_physical_anchor_identity(junit_path, effective_os)
    _path_physical_anchor_identity(basetemp, effective_os)
    if _paths_overlap(
        junit_path,
        left_is_directory=False,
        right=basetemp,
        right_is_directory=True,
        effective_os=effective_os,
    ):
        raise AtomError(f"entrypoint {entry_id} junit_path overlaps basetemp")
    for collection_root in collection_roots:
        collection_root = collection_root.resolve(strict=True)
        if _pytest_output_overlaps_collection(
            junit_path,
            collection_root,
            effective_os,
        ):
            raise AtomError(
                f"entrypoint {entry_id} junit_path overlaps the pytest collection scope"
            )
        if _pytest_output_overlaps_collection(
            basetemp,
            collection_root,
            effective_os,
            output_is_directory=True,
        ):
            raise AtomError(
                f"entrypoint {entry_id} basetemp overlaps the pytest collection scope"
            )
    junit_identity = _path_identity(junit_path, effective_os)
    junit_file_identity = _regular_file_identity(junit_path)
    for prior_suite in compilation.test_suites:
        prior_contract = prior_suite.get("selection_contract", {})
        prior_roots = prior_contract.get("collection_roots", [])
        prior_junit = prior_suite.get("junit_path")
        prior_basetemp = prior_suite.get("basetemp_path")
        if any(
            _pytest_output_overlaps_collection(
                output_path,
                Path(prior_root),
                effective_os,
                output_is_directory=output_is_directory,
            )
            for output_path, output_is_directory in (
                (junit_path, False),
                (basetemp, True),
            )
            for prior_root in prior_roots
            if isinstance(prior_root, str)
        ) or (
            (isinstance(prior_junit, str) or isinstance(prior_basetemp, str))
            and any(
                _pytest_output_overlaps_collection(
                    output_path,
                    collection_root.resolve(strict=True),
                    effective_os,
                    output_is_directory=output_is_directory,
                )
                for output_path, output_is_directory in (
                    (prior_junit, False),
                    (prior_basetemp, True),
                )
                if isinstance(output_path, str)
                for collection_root in collection_roots
            )
        ):
            raise AtomError(
                f"entrypoint {entry_id} JUnit output overlaps another suite's "
                "pytest collection scope"
            )
        if isinstance(prior_junit, str) and _paths_are_equivalent(
            junit_path,
            prior_junit,
            effective_os,
        ):
            raise AtomError(
                f"entrypoint {entry_id} junit_path must be unique within the plan"
            )
        if isinstance(prior_basetemp, str) and (
            _paths_overlap(
                junit_path,
                left_is_directory=False,
                right=prior_basetemp,
                right_is_directory=True,
                effective_os=effective_os,
            )
            or isinstance(prior_junit, str)
            and _paths_overlap(
                basetemp,
                left_is_directory=True,
                right=prior_junit,
                right_is_directory=False,
                effective_os=effective_os,
            )
            or _paths_overlap(
                basetemp,
                left_is_directory=True,
                right=prior_basetemp,
                right_is_directory=True,
                effective_os=effective_os,
            )
        ):
            raise AtomError(
                f"entrypoint {entry_id} pytest outputs overlap another suite's outputs"
            )

    argv = [*runner_argv, "-p", "xdist"]
    if worker_count > 1:
        argv.extend(["-n", str(worker_count), "--dist", distribution])
    argv.extend(
        [
            "-c",
            str(config_path),
            f"--confcutdir={compilation.project}",
            *([f"--rootdir={config_rootdir}"] if uses_empty_config else []),
            "-p",
            "no:cacheprovider",
            *(
                [
                    "--maxprocesses",
                    str(worker_count),
                    "--max-worker-restart",
                    "0",
                ]
                if worker_count > 1
                else []
            ),
            f"--basetemp={basetemp}",
            f"--junitxml={junit_path}",
            *arguments,
        ]
    )
    if len(argv) > 256:
        raise AtomError(f"entrypoint {entry_id} expanded pytest argv exceeds 256 entries")

    snapshot_accesses, selection_snapshots, explicit_source_snapshots = (
        _snapshot_test_configuration(
            compilation,
            compilation.project,
            config_path,
            snapshot_paths,
            [path for path in collection_roots if path.is_file()],
        )
    )
    explicit_source_paths = [
        (
            Path(snapshot["path"])
            if Path(snapshot["path"]).is_absolute()
            else compilation.project / snapshot["path"]
        ).resolve(strict=True)
        for snapshot in explicit_source_snapshots
    ]
    baseline_source_coverage = _pytest_baseline_source_coverage(
        compilation.project,
        cwd,
        [*config_addopts, *environment_addopts, *arguments],
        config_path,
        explicit_source_paths,
        f"entrypoint {entry_id}",
    )
    protected_input_identities = {
        _path_identity(access["resource"], effective_os)
        for access in snapshot_accesses
    }
    protected_file_identities = {
        identity
        for access in snapshot_accesses
        if (identity := _regular_file_identity(Path(access["resource"]))) is not None
    }
    if Path(runner_argv[0]).is_absolute():
        protected_input_identities.add(_path_identity(runner_argv[0], effective_os))
        protected_input_identities.add(
            _path_identity(Path(runner_argv[0]).resolve(strict=False), effective_os)
        )
        runner_file_identity = _regular_file_identity(
            Path(runner_argv[0]).resolve(strict=False)
        )
        if runner_file_identity is not None:
            protected_file_identities.add(runner_file_identity)
    for prior_suite in compilation.test_suites:
        prior_junit = prior_suite.get("junit_path")
        if isinstance(prior_junit, str):
            prior_identity = _path_identity(prior_junit, effective_os)
            if prior_identity in protected_input_identities:
                raise AtomError(
                    f"entrypoint {entry_id} snapshot or runner overlaps the JUnit output of another suite"
                )
            prior_junit_file_identity = _regular_file_identity(Path(prior_junit))
            if (
                prior_junit_file_identity is not None
                and prior_junit_file_identity in protected_file_identities
            ):
                raise AtomError(
                    f"entrypoint {entry_id} snapshot or runner aliases the JUnit output of another suite"
                )
        prior_contract = prior_suite.get("selection_contract", {})
        prior_runner = prior_contract.get("runner_argv", [])
        if prior_runner and isinstance(prior_runner[0], str):
            prior_runner_path = Path(prior_runner[0])
            if prior_runner_path.is_absolute():
                protected_input_identities.add(
                    _path_identity(prior_runner_path, effective_os)
                )
                prior_runner_identity = _regular_file_identity(
                    prior_runner_path.resolve(strict=False)
                )
                if prior_runner_identity is not None:
                    protected_file_identities.add(prior_runner_identity)
                protected_input_identities.add(
                    _path_identity(
                        prior_runner_path.resolve(strict=False), effective_os
                    )
                )
        for snapshot in prior_contract.get("source_snapshots", []):
            raw_path = snapshot.get("path") if isinstance(snapshot, dict) else None
            if isinstance(raw_path, str):
                prior_path = Path(raw_path)
                if not prior_path.is_absolute():
                    prior_path = compilation.project / prior_path
                protected_input_identities.add(
                    _path_identity(prior_path.resolve(strict=False), effective_os)
                )
                prior_snapshot_identity = _regular_file_identity(
                    prior_path.resolve(strict=False)
                )
                if prior_snapshot_identity is not None:
                    protected_file_identities.add(prior_snapshot_identity)
    if junit_identity in protected_input_identities:
        raise AtomError(
            f"entrypoint {entry_id} junit_path overlaps a snapshotted input or runner executable"
        )
    if (
        junit_file_identity is not None
        and junit_file_identity in protected_file_identities
    ):
        raise AtomError(
            f"entrypoint {entry_id} junit_path aliases a snapshotted input or runner executable"
        )
    if _path_identity(basetemp, effective_os) in protected_input_identities:
        raise AtomError(
            f"entrypoint {entry_id} basetemp overlaps a snapshotted input or runner executable"
        )
    if _path_identity(basetemp, effective_os) == junit_identity:
        raise AtomError(f"entrypoint {entry_id} junit_path overlaps basetemp")
    selection_contract = {
        "schema": "atomlane/pytest-selection/v1",
        "runner_argv": runner_argv,
        "runner_attestation": runner_attestation,
        "arguments": arguments,
        "cwd": str(cwd),
        "env": environment,
        "source_snapshots": sorted(selection_snapshots, key=lambda item: item["path"]),
        "explicit_source_snapshots": sorted(
            explicit_source_snapshots, key=lambda item: item["path"]
        ),
        "baseline_source_closure_declared": baseline_source_closure_declared,
        "baseline_source_coverage": baseline_source_coverage,
        "config_path": str(config_path),
        "config_rootdir": str(config_rootdir),
        "collection_roots": [
            str(path.resolve(strict=True)) for path in collection_roots
        ],
        "config_addopts": config_addopts,
        "environment_addopts": environment_addopts,
        "uses_bundled_empty_config": uses_empty_config,
        "config_selection_kind": config_selection_kind,
        "config_addopts_policy": "preserved_validated",
    }
    selection_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            selection_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    blockers: list[str] = []
    if not complete:
        blockers.append("INCOMPLETE_TEST_EFFECT_MODEL")
    if worker_count > 1 and not independence_declared:
        blockers.append("TEST_CASE_INDEPENDENCE_NOT_DECLARED")
    claims = [
        {"resource": "worker_slot", "units": 1},
        {"resource": "cpu_core", "units": worker_count},
    ]
    total_memory = (
        float(memory_per_worker) * worker_count
        if memory_per_worker is not None
        else None
    )
    if total_memory is not None:
        claims.append({"resource": "memory_mb", "units": total_memory})
    atom_id = compilation.emit(
        {
            "id": entry_id,
            "operation": {
                "kind": "test",
                "argv": argv,
                "cwd": str(cwd),
                "env": environment,
                "completion": "process_exit",
                "timeout_seconds": float(timeout),
                "internal_parallelism": {
                    "kind": "bounded" if worker_count > 1 else "none",
                    "tokens": worker_count if worker_count > 1 else None,
                },
            },
            "accesses": [
                *snapshot_accesses,
                *declared_accesses,
                {"resource": str(basetemp), "mode": "transaction"},
                {"resource": str(junit_path), "mode": "overwrite"},
            ],
            "effects": declared_effects,
            "claims": claims,
            "profile": "cpu",
            "side_effect": True,
            "semantics": {
                "idempotent": None,
                "retryable": False,
                "deterministic": None,
                "cacheable": False,
                "commutative": False,
                "cancel_safe": None,
                "splittable": False,
                "reorderable": "forbidden",
            },
            "cost": {
                "duration_seconds": float(duration) if duration is not None else None,
                "memory_mb": total_memory,
                "cpu_cores": worker_count,
            },
            "batch": {"key": f"pytest:{entry_id}", "strategy": "native_command"},
            "assurance": {
                "parse": "exact",
                "control": "exact",
                "effects": "complete_declared" if complete else "unknown",
                "codegen": "exact_argv",
                "rank": 1.0 if complete else 0.5,
                "blockers": blockers,
            },
            "provenance": {
                "adapter": "test_suite",
                "source": "task_plan",
                "symbol": entry_id,
                "confidence": 1.0,
            },
        }
    )
    suite = {
        "schema": "atomlane/test-suite/v1",
        "id": entry_id,
        "framework": "pytest",
        "strategy": "native_worker_pool" if worker_count > 1 else "native_serial",
        "atom_id": atom_id,
        "configured_workers": worker_count,
        "worker_count_source": worker_source,
        "worker_evidence": "configured_not_observed",
        "distribution": distribution if worker_count > 1 else None,
        "case_count_hint": case_count_hint,
        "junit_path": str(junit_path),
        "basetemp_path": str(basetemp),
        "effects_declared_complete": complete,
        "independence_declared": independence_declared,
        "selection_contract": selection_contract,
        "selection_fingerprint": selection_fingerprint,
        "explicit_snapshot_count": len(explicit_source_snapshots),
        "baseline_source_closure_declared": baseline_source_closure_declared,
        "baseline_source_coverage": baseline_source_coverage,
        "config_addopts_policy": "preserved_validated",
        "collection_execution_performed": False,
        "native_dependency": "pytest-xdist",
    }
    compilation.test_suites.append(suite)
    compilation.diagnostic(
        "PYTEST_ADDOPTS_VALIDATED",
        f"Preserved and hash-bound {len(config_addopts)} config and {len(environment_addopts)} environment addopts tokens after rejecting control conflicts.",
        source="task_plan",
        symbol=entry_id,
    )
    compilation.diagnostic(
        "PYTEST_XDIST_RUNTIME_REQUIRED",
        "The compiled pytest route requires pytest-xdist in the selected runner environment; AtomLane never installs it automatically.",
        source="task_plan",
        symbol=entry_id,
    )
    if worker_count > 1:
        compilation.native_delegates.append(
            {
                "kind": "pytest_native_worker_pool",
                "atoms": [atom_id],
                "argv": argv,
                "cwd": str(cwd),
                "configured_workers": worker_count,
                "worker_evidence": "configured_not_observed",
                "distribution": distribution,
                "reason": "pytest-xdist owns collection, fixtures, case scheduling, and worker lifecycle.",
            }
        )
        if not independence_declared:
            compilation.diagnostic(
                "TEST_CASE_INDEPENDENCE_NOT_DECLARED",
                "Parallel pytest execution remains blocked until the caller explicitly declares that selected cases are independent under the modeled fixtures and effects.",
                source="task_plan",
                symbol=entry_id,
            )
    return Fragment([atom_id], [atom_id], [atom_id])


def _git_root(cwd: Path) -> Path:
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def _infer_command_atom(
    compilation: Compilation,
    command: str,
    *,
    atom_id: str,
    cwd: Path,
    source: str,
    line: int,
    symbol: str,
    adapter: str,
    dynamic: bool = False,
) -> str:
    argv, environment = _simple_argv(command)
    executable = Path(argv[0]).name.lower() if argv else ""
    lower = command.lower()
    operation_kind = "command"
    profile = "mixed"
    side_effect = False
    accesses: list[dict[str, str]] = []
    effects: list[dict[str, str]] = []
    blockers: list[str] = []
    effects_assurance = "partial"
    parse_assurance = "opaque" if dynamic else "exact"
    codegen = "exact_argv"
    internal_parallelism = _native_parallelism(argv)

    shell_needed = argv is None
    if shell_needed:
        argv = ["/bin/zsh", "-lc", command]
        codegen = "exact_argv"
    if dynamic:
        operation_kind = "opaque"
        effects.append({"domain": "unknown", "key": "host", "mode": "write"})
        side_effect = True
        effects_assurance = "unknown"
        blockers.extend(["UNSUPPORTED_DYNAMIC_SHELL", "UNKNOWN_EFFECT"])
    else:
        redirections = re.findall(r"(?:^|\s)(>>|>|<)\s*([^\s;&|]+)", command)
        for operator, target in redirections:
            if any(marker in target for marker in ("$", "`", "*", "?", "[", "{")):
                blockers.append("DYNAMIC_RESOURCE")
                continue
            accesses.append(
                {
                    "resource": target,
                    "mode": "read" if operator == "<" else "append" if operator == ">>" else "overwrite",
                }
            )
            if operator != "<":
                side_effect = True

        read_tools = {"rg", "grep", "find", "ls", "stat", "cat", "head", "tail", "wc", "pwd", "diff", "cmp"}
        if executable in read_tools:
            operation_kind = "read"
            profile = "io"
            effects_assurance = "complete_static" if accesses else "partial"
        if executable in {"pytest", "vitest", "jest"} or " test" in f" {lower}":
            operation_kind = "test"
            profile = "cpu"
            if internal_parallelism["kind"] == "unknown" and (
                executable in {"pytest", "vitest", "jest"}
                or any(name in lower for name in ("vitest", "jest", "pytest"))
            ):
                internal_parallelism = {"kind": "native_scheduler", "tokens": None}
        if executable in {"cc", "clang", "gcc", "swiftc", "rustc"} or "next build" in lower or "tsc " in lower:
            operation_kind = "build"
            profile = "cpu"
            side_effect = True
        if executable == "latexmk":
            operation_kind = "build"
            profile = "cpu"
            inputs = [item for item in argv[1:] if item.endswith((".tex", ".ltx"))]
            output_dirs = _flag_value(argv, "-output-directory", "--output-directory") or ["."]
            for input_name in inputs:
                accesses.append({"resource": input_name, "mode": "read"})
                stem = Path(input_name).stem
                accesses.append({"resource": f"latex-job:{(cwd / output_dirs[-1]).resolve(strict=False)}:{stem}", "mode": "overwrite"})
            side_effect = True
            effects_assurance = "complete_static" if inputs else "partial"
        if executable == "git":
            subcommand = next((item for item in argv[1:] if not item.startswith("-")), "")
            root = _git_root(cwd)
            if subcommand in {"status", "diff", "log", "show", "rev-parse", "ls-files"}:
                operation_kind = "read"
                effects.append({"domain": "git", "key": str(root), "mode": "read"})
                effects_assurance = "complete_static"
            else:
                operation_kind = "mutation"
                side_effect = True
                effects.append({"domain": "git", "key": str(root), "mode": "transaction"})
                effects_assurance = "complete_static"
        if executable in {"rm", "mv", "cp", "touch", "mkdir", "install", "tee"}:
            operation_kind = "mutation"
            side_effect = True
            paths = [item for item in argv[1:] if not item.startswith("-")]
            mode = "delete" if executable == "rm" else "create" if executable in {"touch", "mkdir"} else "overwrite"
            for path in paths:
                accesses.append({"resource": path, "mode": mode})
            effects_assurance = "complete_static" if paths else "unknown"
        if re.search(r"(?:npm|pnpm|yarn|bun|pip|uv|poetry)\s+(?:install|add|remove|update|sync)(?:\s|$)", lower):
            operation_kind = "mutation"
            side_effect = True
            effects.append({"domain": "dependency-environment", "key": str(cwd), "mode": "transaction"})
            effects_assurance = "complete_static"
        if re.search(r"(?:prisma\s+(?:migrate|db\s+(?:push|seed))|alembic\s+upgrade|manage\.py\s+migrate)", lower):
            operation_kind = "mutation"
            side_effect = True
            effects.append({"domain": "database", "key": "default", "mode": "transaction"})
            effects_assurance = "partial"
        if re.search(r"(?:docker\s+compose|docker-compose)\s+(?:up|down|start|stop|restart|rm)", lower):
            operation_kind = "mutation"
            side_effect = True
            effects.append({"domain": "compose-project", "key": str(cwd), "mode": "transaction"})
            effects_assurance = "partial"

        if argv:
            for output in _flag_value(argv, "-o", "--output", "--report", "--summary", "--output-file"):
                accesses.append({"resource": output, "mode": "create"})
                side_effect = True
            for input_path in _flag_value(argv, "-i", "--input", "--input-file"):
                accesses.append({"resource": input_path, "mode": "read"})
            if any(item == "--fix" for item in argv):
                side_effect = True
                operation_kind = "mutation"
                accesses.append({"resource": str(cwd), "mode": "overwrite"})
        if side_effect and not accesses and not effects:
            effects.append({"domain": "unknown", "key": "host", "mode": "write"})
            effects_assurance = "unknown"
            blockers.append("UNKNOWN_EFFECT")
        elif effects_assurance == "partial":
            blockers.append("PARTIAL_EFFECT_MODEL")

    semantics = {
        "idempotent": True if operation_kind in {"read", "test", "build"} and effects_assurance != "unknown" else None,
        "retryable": True if operation_kind == "read" else None,
        "deterministic": True if operation_kind == "read" else None,
        "cacheable": False,
        "commutative": False,
        "cancel_safe": True if operation_kind == "read" else None,
        "splittable": False if operation_kind in {"opaque", "mutation"} else None,
        "reorderable": "unknown",
    }
    return compilation.emit(
        {
            "id": atom_id,
            "operation": {
                "kind": operation_kind,
                "argv": argv,
                "command": command,
                "cwd": str(cwd),
                "env": environment,
                "completion": "process_exit",
                "internal_parallelism": internal_parallelism,
            },
            "accesses": accesses,
            "effects": effects,
            "profile": profile,
            "side_effect": side_effect,
            "semantics": semantics,
            "assurance": {
                "parse": parse_assurance,
                "control": "exact" if not dynamic else "unknown",
                "effects": effects_assurance,
                "codegen": codegen,
                "rank": 1.0 if parse_assurance == "exact" and effects_assurance in {"complete_static", "complete_declared"} else 0.45,
                "blockers": blockers,
            },
            "provenance": {
                "adapter": adapter,
                "source": source,
                "symbol": symbol,
                "line": line,
                "confidence": 1.0 if not dynamic else 0.4,
            },
            "opaque_reason": "dynamic or unsupported shell syntax" if dynamic else None,
        }
    )


Resolver = Callable[[str, list[dict[str, str]], int], Fragment | None]


def compile_shell(
    compilation: Compilation,
    command: str,
    *,
    prefix: str,
    cwd: Path,
    source: str,
    symbol: str,
    line: int = 1,
    inherited: list[dict[str, str]] | None = None,
    resolver: Resolver | None = None,
) -> Fragment:
    segments, error = _scan_shell_list(command)
    if error:
        compilation.diagnostic(error, "shell region was preserved as one opaque atom", source=source, line=line, symbol=symbol)
        atom_id = _infer_command_atom(
            compilation,
            command,
            atom_id=f"{prefix}-opaque",
            cwd=cwd,
            source=source,
            line=line,
            symbol=symbol,
            adapter="shell",
            dynamic=True,
        )
        _add_dependencies(compilation, [atom_id], inherited or [])
        return Fragment([atom_id], [atom_id], [atom_id])

    all_ids: list[str] = []
    roots: list[str] = []
    previous_terminals: list[str] = []
    for index, segment in enumerate(segments):
        dependency_kind = segment.connector_before or "success"
        dependencies = list(inherited or []) if index == 0 else [
            {"atom": atom_id, "kind": dependency_kind} for atom_id in previous_terminals
        ]
        fragment = resolver(segment.text, dependencies, line + segment.line - 1) if resolver else None
        if fragment is None:
            atom_id = _infer_command_atom(
                compilation,
                segment.text,
                atom_id=f"{prefix}-{index + 1}",
                cwd=cwd,
                source=source,
                line=line + segment.line - 1,
                symbol=symbol,
                adapter="shell",
                dynamic=segment.dynamic,
            )
            _add_dependencies(compilation, [atom_id], dependencies)
            fragment = Fragment([atom_id], [atom_id], [atom_id])
        if index == 0:
            roots.extend(fragment.roots)
        previous_terminals = fragment.terminals
        all_ids.extend(fragment.atoms)
    if len(segments) > 1 and all(segment.connector_before in {None, "success"} for segment in segments):
        compilation.relaxation_candidates.append(
            {
                "kind": "success_chain_diagnostic_fanout",
                "atoms": all_ids,
                "semantic_change": "would replace fail-fast success control with aggregate diagnostics",
                "automatic": False,
            }
        )
    return Fragment(roots, previous_terminals, all_ids)


class PackageScriptCompiler:
    def __init__(self, compilation: Compilation, package_file: Path) -> None:
        self.compilation = compilation
        self.package_file = package_file.resolve()
        self.cwd = self.package_file.parent
        self.source = _source_label(self.package_file, compilation.project)
        compilation.snapshot(self.package_file)
        try:
            payload = json.loads(
                _strict_read_text(self.package_file, self.source),
                object_pairs_hook=_json_object_without_duplicates,
            )
        except json.JSONDecodeError as exc:
            raise AtomError(f"invalid JSON in {self.source}: {exc}") from exc
        if not isinstance(payload, dict):
            raise AtomError(f"{self.source} must contain a JSON object")
        scripts = payload.get("scripts", {})
        if not isinstance(scripts, dict) or len(scripts) > MAX_PACKAGE_SCRIPTS:
            raise AtomError(f"{self.source} scripts must be an object with at most {MAX_PACKAGE_SCRIPTS} entries")
        if not all(
            isinstance(name, str)
            and name
            and "\x00" not in name
            and len(name) <= 512
            and isinstance(command, str)
            and "\x00" not in command
            and len(command) <= MAX_COMMAND_CHARS
            for name, command in scripts.items()
        ):
            raise AtomError(f"every {self.source} script must have a bounded string name and command")
        self.scripts = dict(scripts)
        package_manager = payload.get("packageManager")
        manager_from_manifest = package_manager.split("@", 1)[0] if isinstance(package_manager, str) else None
        lock_managers = [
            manager for manager, filename in (
                ("pnpm", "pnpm-lock.yaml"),
                ("yarn", "yarn.lock"),
                ("bun", "bun.lockb"),
                ("bun", "bun.lock"),
                ("npm", "package-lock.json"),
            )
            if (self.cwd / filename).is_file()
        ]
        self.manager_exact = False
        if manager_from_manifest in {"npm", "pnpm", "yarn", "bun"}:
            self.manager = manager_from_manifest
            self.manager_exact = True
            if lock_managers and any(item != self.manager for item in lock_managers):
                compilation.diagnostic(
                    "PACKAGE_MANAGER_CONFLICT",
                    "packageManager disagrees with one or more lockfiles; manifest value retained for static provenance",
                    source=self.source,
                )
        elif len(set(lock_managers)) == 1:
            self.manager = lock_managers[0]
            self.manager_exact = True
        else:
            self.manager = "npm"
            if len(set(lock_managers)) > 1:
                compilation.diagnostic(
                    "PACKAGE_MANAGER_CONFLICT",
                    "multiple package-manager lockfiles prevent an exact manager inference",
                    source=self.source,
                )
            else:
                compilation.diagnostic(
                    "PACKAGE_MANAGER_UNKNOWN",
                    "no packageManager or unique lockfile was found; npm is display-only and lifecycle expansion is disabled",
                    source=self.source,
                )

    @staticmethod
    def _nested_script(command: str) -> tuple[str, bool, tuple[str, ...]] | None:
        """Recognize only exact package-script invocations.

        The boolean indicates whether npm's documented pre/main/post lifecycle
        applies. Literal arguments after `--` are retained and may be lowered
        only when the target script body is itself one exact simple command.
        """
        try:
            words = shlex.split(command, posix=True)
        except ValueError:
            return None
        if any(token in command for token in ("$", "`", "|", "&", ";", "<", ">")):
            return None
        executable = Path(words[0]).name if words else ""
        if executable == "npm":
            if len(words) >= 2 and words[1] == "test":
                forwarded = words[2:]
                if forwarded and forwarded[0] != "--":
                    return None
                forwarded = forwarded[1:] if forwarded else []
                return "test", True, tuple(forwarded)
            if len(words) >= 3 and words[1] in {"run", "run-script"}:
                forwarded = words[3:]
                if forwarded and forwarded[0] != "--":
                    return None
                forwarded = forwarded[1:] if forwarded else []
                return words[2], True, tuple(forwarded)
        if executable in {"pnpm", "bun"} and len(words) >= 3 and words[1] in {"run", "run-script"}:
            forwarded = words[3:]
            if forwarded[:1] == ["--"]:
                forwarded = forwarded[1:]
            return words[2], False, tuple(forwarded)
        if executable == "yarn" and len(words) >= 3 and words[1] in {"run", "run-script"}:
            forwarded = words[3:]
            if forwarded[:1] == ["--"]:
                forwarded = forwarded[1:]
            return words[2], False, tuple(forwarded)
        return None

    def _can_forward_literal_args(self, script: str, forwarded: tuple[str, ...]) -> bool:
        if not forwarded or script not in self.scripts:
            return True
        command = self.scripts[script]
        segments, error = _scan_shell_list(command)
        if error or len(segments) != 1:
            return False
        argv, _ = _simple_argv(segments[0].text)
        return argv is not None and len(command) + len(shlex.join(forwarded)) + 1 <= MAX_COMMAND_CHARS

    def compile(
        self,
        script: str,
        *,
        inherited: list[dict[str, str]] | None = None,
        stack: tuple[str, ...] = (),
        depth: int = 0,
        lifecycle: bool | None = None,
        forwarded_args: tuple[str, ...] = (),
    ) -> Fragment:
        if depth > MAX_PACKAGE_DEPTH:
            return self._opaque(script, inherited or [], "BUDGET_EXCEEDED", stack)
        if script in stack:
            return self._opaque(script, inherited or [], "CYCLE_COLLAPSED", stack)
        if script not in self.scripts:
            return self._opaque(script, inherited or [], "UNKNOWN_PACKAGE_SCRIPT", stack)
        apply_lifecycle = self.manager_exact and self.manager == "npm" if lifecycle is None else lifecycle
        sequence = [script]
        if apply_lifecycle:
            sequence = [name for name in (f"pre{script}", script, f"post{script}") if name in self.scripts]
        all_ids: list[str] = []
        roots: list[str] = []
        terminals: list[str] = []
        for sequence_index, current in enumerate(sequence):
            deps = list(inherited or []) if sequence_index == 0 else [
                {"atom": atom_id, "kind": "success"} for atom_id in terminals
            ]

            def resolver(segment: str, segment_deps: list[dict[str, str]], line: int) -> Fragment | None:
                nested_call = self._nested_script(segment)
                if nested_call is None:
                    return None
                nested, nested_lifecycle, nested_args = nested_call
                if not self._can_forward_literal_args(nested, nested_args):
                    return None
                return self.compile(
                    nested,
                    inherited=segment_deps,
                    stack=stack + (script,),
                    depth=depth + 1,
                    lifecycle=nested_lifecycle,
                    forwarded_args=nested_args,
                )

            command = self.scripts[current]
            if current == script and forwarded_args:
                command = command + " " + shlex.join(forwarded_args)
            fragment = compile_shell(
                self.compilation,
                command,
                prefix=f"pkg-{_slug(current)}",
                cwd=self.cwd,
                source=self.source,
                symbol=f"scripts.{current}",
                inherited=deps,
                resolver=resolver,
            )
            if sequence_index == 0:
                roots = fragment.roots
            terminals = fragment.terminals
            all_ids.extend(fragment.atoms)
        return Fragment(roots, terminals, all_ids)

    def _opaque(
        self,
        script: str,
        inherited: list[dict[str, str]],
        code: str,
        stack: tuple[str, ...],
    ) -> Fragment:
        self.compilation.diagnostic(
            code,
            "package script expansion was preserved as an opaque native command",
            source=self.source,
            symbol=f"scripts.{script}",
        )
        atom_id = self.compilation.emit(
            {
                "id": f"pkg-{_slug(script)}-opaque",
                "operation": {
                    "kind": "opaque",
                    "argv": [self.manager, "run", script],
                    "cwd": str(self.cwd),
                    "completion": "process_exit",
                    "internal_parallelism": {"kind": "unknown", "tokens": None},
                },
                "dependencies": inherited,
                "side_effect": True,
                "effects": [{"domain": "unknown", "key": "host", "mode": "write"}],
                "semantics": {"splittable": False, "reorderable": "forbidden"},
                "assurance": {
                    "parse": "opaque",
                    "control": "unknown",
                    "effects": "unknown",
                    "codegen": "exact_argv" if self.manager_exact else "opaque",
                    "rank": 0.2,
                    "blockers": list(dict.fromkeys([
                        code,
                        "UNKNOWN_EFFECT",
                        *([] if self.manager_exact else ["PACKAGE_MANAGER_UNKNOWN"]),
                    ])),
                },
                "provenance": {
                    "adapter": "package_json",
                    "source": self.source,
                    "symbol": f"scripts.{script}",
                    "confidence": 1.0 if self.manager_exact else 0.4,
                },
                "opaque_reason": code,
            }
        )
        return Fragment([atom_id], [atom_id], [atom_id])


@dataclass
class MakeRule:
    target: str
    prerequisites: list[str] = field(default_factory=list)
    order_only: list[str] = field(default_factory=list)
    recipes: list[tuple[int, str]] = field(default_factory=list)
    unsafe_codes: list[str] = field(default_factory=list)
    line: int = 1


def _literal_python_recipe_calls(recipe: str, cwd: Path) -> list[tuple[Path, list[str]]]:
    """Find bounded literal Python script calls without expanding Make syntax."""
    calls: list[tuple[Path, list[str]]] = []
    for logical_line in recipe.replace("\\\n", " ").splitlines():
        line = logical_line.strip().lstrip("@-+").strip()
        if not line or any(operator in line for operator in ("&&", "||", "|", ";", "`", "$(shell")):
            continue
        try:
            words = shlex.split(line, posix=True)
        except ValueError:
            continue
        script_index = next(
            (
                index
                for index, word in enumerate(words)
                if word.endswith(".py") and "$" not in word and "{" not in word
            ),
            None,
        )
        if script_index is None:
            continue
        # The interpreter may be literal or a non-executed Make variable such
        # as $(PYTHON); locating the source file never evaluates it.
        script = Path(words[script_index]).expanduser()
        if not script.is_absolute():
            script = cwd / script
        script = script.resolve(strict=False)
        try:
            script.relative_to(cwd.resolve())
        except ValueError:
            continue
        if not script.is_file():
            continue
        arguments = [
            word for word in words[script_index + 1:]
            if "$" not in word and "`" not in word
        ]
        calls.append((script, arguments))
    return calls


class MakefileCompiler:
    def __init__(self, compilation: Compilation, makefile: Path) -> None:
        self.compilation = compilation
        self.makefile = makefile.resolve()
        self.cwd = self.makefile.parent
        self.source = _source_label(self.makefile, compilation.project)
        compilation.snapshot(self.makefile)
        self.text = _strict_read_text(self.makefile, self.source)
        self.rules: dict[str, MakeRule] = {}
        self.phony: set[str] = set()
        self.not_parallel = False
        self.one_shell = False
        self.global_taints: set[str] = set()
        self._cache: dict[str, Fragment] = {}
        self._parse()

    def _parse(self) -> None:
        if "$(shell" in self.text or "${shell" in self.text:
            self.compilation.diagnostic(
                "DYNAMIC_MAKE_EXPANSION",
                "Makefile contains shell expansion; affected recipes remain conservative and are never evaluated",
                source=self.source,
            )
        lines = self.text.splitlines()
        current_targets: list[str] = []
        in_define = False
        index = 0
        while index < len(lines):
            line_number = index + 1
            line = lines[index]
            stripped_line = line.strip()
            if in_define:
                if stripped_line == "endef":
                    in_define = False
                index += 1
                continue
            if stripped_line.startswith("define "):
                in_define = True
                self.global_taints.add("UNSUPPORTED_MAKE_DEFINE")
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_DEFINE",
                    "Make define blocks are not expanded by the static frontend",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            if line.startswith("\t"):
                if current_targets:
                    recipe = line[1:]
                    while recipe.endswith("\\") and index + 1 < len(lines):
                        index += 1
                        recipe += "\n" + lines[index].lstrip("\t")
                    for target in current_targets:
                        self.rules[target].recipes.append((line_number, recipe))
                else:
                    self.global_taints.add("ORPHAN_MAKE_RECIPE")
                    self.compilation.diagnostic(
                        "ORPHAN_MAKE_RECIPE",
                        "recipe text was not attached to an exact explicit target",
                        source=self.source,
                        line=line_number,
                    )
                index += 1
                continue
            current_targets = []
            logical = line
            while logical.rstrip().endswith("\\") and index + 1 < len(lines):
                logical = logical.rstrip()[:-1] + " " + lines[index + 1].strip()
                index += 1
            stripped = logical.strip()
            if not stripped or stripped.startswith("#"):
                index += 1
                continue
            if stripped.startswith(".RECIPEPREFIX"):
                self.global_taints.add("UNSUPPORTED_RECIPE_PREFIX")
                self.compilation.diagnostic(
                    "UNSUPPORTED_RECIPE_PREFIX",
                    "custom Make recipe prefixes are not statically lowered",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            assignment = re.match(
                r"^(?:(?:override|export|private|unexport)\s+)*[A-Za-z0-9_.-]+\s*(?::=|::=|\+=|\?=|!=|=)",
                stripped,
            )
            if assignment:
                if "!=" in assignment.group(0) or "$(shell" in stripped or "${shell" in stripped:
                    self.compilation.diagnostic(
                        "DYNAMIC_MAKE_EXPANSION",
                        "shell-capable Make assignment was recorded but never evaluated",
                        source=self.source,
                        line=line_number,
                    )
                index += 1
                continue
            if stripped.startswith(("include ", "-include ")):
                self.global_taints.add("UNSUPPORTED_MAKE_INCLUDE")
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_INCLUDE",
                    "Make include was not expanded by the bounded static frontend",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            if re.match(r"^(?:ifeq|ifneq|ifdef|ifndef|else|endif)\b", stripped):
                self.global_taints.add("UNSUPPORTED_MAKE_CONDITIONAL")
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_CONDITIONAL",
                    "conditional Make parsing is not evaluated by the static frontend",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            if "::" in logical.split("#", 1)[0] or "&:" in logical.split("#", 1)[0]:
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_RULE",
                    "double-colon or grouped-target rules remain native/opaque",
                    source=self.source,
                    line=line_number,
                )
                self.global_taints.add("UNSUPPORTED_MAKE_RULE")
                index += 1
                continue
            match = re.match(r"^([^:#=]+?)\s*:(?![=])\s*(.*)$", logical)
            if not match:
                self.global_taints.add("UNSUPPORTED_MAKE_SYNTAX")
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_SYNTAX",
                    "unrecognized top-level Make syntax prevents exact graph lowering",
                    source=self.source,
                    line=line_number,
                )
                index += 1
                continue
            targets = match.group(1).split()
            if any("%" in target or "$" in target for target in targets):
                self.compilation.diagnostic(
                    "UNSUPPORTED_MAKE_RULE",
                    "pattern or variable-expanded Make rule remains native/opaque",
                    source=self.source,
                    line=line_number,
                )
                self.global_taints.add("UNSUPPORTED_MAKE_RULE")
                index += 1
                continue
            rule_tail = match.group(2).split("#", 1)[0]
            prerequisite_text, separator, inline_recipe = rule_tail.partition(";")
            prerequisite_text = prerequisite_text.strip()
            normal_text, _, order_text = prerequisite_text.partition("|")
            prerequisites = normal_text.split()
            order_only = order_text.split()
            if targets == [".PHONY"]:
                self.phony.update(prerequisites)
                index += 1
                continue
            if targets == [".NOTPARALLEL"]:
                self.not_parallel = True
                index += 1
                continue
            if targets == [".ONESHELL"]:
                self.one_shell = True
                index += 1
                continue
            if targets == [".SECONDEXPANSION"]:
                self.global_taints.add("UNSUPPORTED_SECONDARY_EXPANSION")
                index += 1
                continue
            current_targets = []
            for target in targets:
                if target.startswith("."):
                    continue
                rule = self.rules.setdefault(target, MakeRule(target=target, line=line_number))
                rule.prerequisites.extend(item for item in prerequisites if item != ".WAIT")
                rule.order_only.extend(order_only)
                if len(targets) > 1:
                    rule.unsafe_codes.append("MULTI_TARGET_MAKE_RULE")
                if ".WAIT" in prerequisites:
                    rule.unsafe_codes.append("MAKE_WAIT_BARRIER")
                if any(any(marker in item for marker in ("$", "%", "*", "?", "[")) for item in prerequisites + order_only):
                    rule.unsafe_codes.append("DYNAMIC_MAKE_PREREQUISITE")
                if separator and inline_recipe.strip():
                    rule.recipes.append((line_number, inline_recipe.strip()))
                current_targets.append(target)
            index += 1
        if len(self.rules) > MAX_MAKE_TARGETS:
            raise AtomError(f"Makefile contains more than {MAX_MAKE_TARGETS} explicit targets")

    def _python_recipe_accesses(self, rule: MakeRule) -> list[dict[str, str]]:
        accesses: list[dict[str, str]] = []
        for line_number, recipe in rule.recipes:
            for script, arguments in _literal_python_recipe_calls(recipe, self.cwd):
                self.compilation.snapshot(script)
                inferred = infer_python_cli_accesses(script, arguments, self.cwd)
                for item in inferred.get("accesses", []):
                    if item not in accesses:
                        accesses.append(item)
                if inferred.get("accesses"):
                    self.compilation.diagnostics.append(
                        {
                            "code": "PYTHON_CLI_STATIC_EFFECTS",
                            "severity": "info",
                            "message": (
                                f"inferred {len(inferred['accesses'])} argparse artifact accesses "
                                "from literal defaults and selected AST sinks"
                            ),
                            "source": _source_label(script, self.compilation.project),
                            "line": line_number,
                            "symbol": rule.target,
                        }
                    )
                for message in inferred.get("diagnostics", []):
                    self.compilation.diagnostic(
                        "PYTHON_CLI_STATIC_ANALYSIS_SKIPPED",
                        message,
                        source=_source_label(script, self.compilation.project),
                        line=line_number,
                        symbol=rule.target,
                    )
        return accesses

    def _repair_prerequisite_dataflow(self, fragments: list[Fragment]) -> None:
        atom_ids = sorted({atom_id for fragment in fragments for atom_id in fragment.atoms})
        if len(atom_ids) < 2:
            return
        by_id = {atom["id"]: atom for atom in self.compilation.atoms if atom["id"] in atom_ids}

        def resource_key(atom: dict[str, Any], resource: str) -> str:
            return _normalize_resource(resource, Path(atom["operation"]["cwd"]))

        writers: dict[str, set[str]] = {}
        readers: dict[str, set[str]] = {}
        for atom_id, atom in by_id.items():
            for access in atom.get("accesses", []):
                key = resource_key(atom, access["resource"])
                if access["mode"] in {"create", "overwrite", "transaction", "delete"}:
                    writers.setdefault(key, set()).add(atom_id)
                elif access["mode"] in {"read", "snapshot"}:
                    readers.setdefault(key, set()).add(atom_id)

        def reaches(start: str, target: str, seen: set[str] | None = None) -> bool:
            if start == target:
                return True
            seen = set() if seen is None else seen
            if start in seen:
                return False
            seen.add(start)
            return any(
                edge["atom"] == target or reaches(edge["atom"], target, seen)
                for edge in by_id[start].get("dependencies", [])
                if edge["atom"] in by_id
            )

        for resource in sorted(set(writers) & set(readers)):
            producer_ids = sorted(writers[resource])
            if len(producer_ids) != 1:
                self.compilation.diagnostics.append(
                    {
                        "code": "AMBIGUOUS_MAKE_DATA_PRODUCER",
                        "severity": "warning",
                        "message": "multiple Make prerequisite atoms write one consumed artifact",
                        "atoms": producer_ids,
                        "resources": [resource],
                    }
                )
                continue
            producer = producer_ids[0]
            for consumer in sorted(readers[resource] - {producer}):
                if reaches(consumer, producer):
                    continue
                if reaches(producer, consumer):
                    self.compilation.diagnostics.append(
                        {
                            "code": "MAKE_DATAFLOW_CONTRADICTION",
                            "severity": "error",
                            "message": "static Python artifact flow contradicts the explicit Make order",
                            "atoms": [producer, consumer],
                            "resources": [resource],
                        }
                    )
                    continue
                by_id[consumer].setdefault("dependencies", []).append(
                    {"atom": producer, "kind": "data"}
                )
                self.compilation.diagnostics.append(
                    {
                        "code": "MAKE_DATAFLOW_REPAIRED",
                        "severity": "warning",
                        "message": "added a data edge omitted by sibling Make prerequisites",
                        "atoms": [producer, consumer],
                        "resources": [resource],
                    }
                )

    def compile(self, target: str, stack: tuple[str, ...] = ()) -> Fragment:
        if target in self._cache:
            return self._cache[target]
        if len(stack) > MAX_RECURSION:
            return self._opaque(target, "BUDGET_EXCEEDED")
        if target in stack:
            return self._opaque(target, "CYCLE_COLLAPSED")
        rule = self.rules.get(target)
        if rule is None:
            return self._opaque(target, "UNKNOWN_MAKE_TARGET")
        unsafe = list(dict.fromkeys([*sorted(self.global_taints), *rule.unsafe_codes]))
        if unsafe:
            return self._opaque(target, unsafe[0])
        normal_fragments = [
            self.compile(item, stack + (target,))
            for item in rule.prerequisites
            if item in self.rules
        ]
        order_fragments = [
            self.compile(item, stack + (target,))
            for item in rule.order_only
            if item in self.rules
        ]
        prerequisite_fragments = [*normal_fragments, *order_fragments]
        self._repair_prerequisite_dataflow(prerequisite_fragments)
        recipe_text = "\n".join(text for _, text in rule.recipes)
        accesses: list[dict[str, str]] = []
        effects: list[dict[str, str]] = []
        blockers = ["PARTIAL_EFFECT_MODEL"]
        accesses.extend(self._python_recipe_accesses(rule))
        for match in re.finditer(r"(?:--output|--report|--summary|-o)(?:=|\s+)['\"]?([^\s'\"\\]+)", recipe_text):
            accesses.append({"resource": match.group(1), "mode": "create"})
        for match in re.finditer(r"(?:--input|-i)(?:=|\s+)['\"]?([^\s'\"\\]+)", recipe_text):
            accesses.append({"resource": match.group(1), "mode": "read"})
        for prerequisite in [*rule.prerequisites, *rule.order_only]:
            if prerequisite not in self.rules:
                accesses.append({"resource": prerequisite, "mode": "read"})
        if rule.recipes and target not in self.phony and target:
            accesses.append({"resource": target, "mode": "create"})
        if re.search(r"(?:timing|benchmark|certificate|formal|prospective|rss)", target, re.IGNORECASE):
            effects.append({"domain": "host", "key": "timing-provenance", "mode": "lease"})
        if not recipe_text:
            recipe_text = f"make target barrier: {target}"
        dependencies = [
            {"atom": atom_id, "kind": "success"}
            for fragment in normal_fragments for atom_id in fragment.terminals
        ] + [
            {"atom": atom_id, "kind": "order"}
            for fragment in order_fragments for atom_id in fragment.terminals
        ]
        atom_id = self.compilation.emit(
            {
                "id": f"make-{_slug(target)}",
                "operation": {
                    "kind": "make_recipe" if rule.recipes else "opaque",
                    "command": recipe_text,
                    "cwd": str(self.cwd),
                    "completion": "process_exit",
                    "internal_parallelism": (
                        {"kind": "bounded", "tokens": 1}
                        if self.not_parallel else
                        {"kind": "native_scheduler", "tokens": None}
                    ),
                },
                "dependencies": dependencies,
                "accesses": accesses,
                "effects": effects,
                "profile": "cpu",
                "side_effect": bool(rule.recipes),
                "semantics": {
                    "idempotent": None,
                    "retryable": None,
                    "deterministic": None,
                    "cacheable": False,
                    "commutative": False,
                    "splittable": False,
                    "reorderable": "forbidden" if self.not_parallel else "proved",
                },
                "assurance": {
                    "parse": "conservative",
                    "control": "exact",
                    "effects": "partial",
                    "codegen": "opaque",
                    "rank": 0.55,
                    "blockers": blockers,
                },
                "provenance": {
                    "adapter": "makefile",
                    "source": self.source,
                    "symbol": target,
                    "line": rule.line,
                    "confidence": 1.0,
                },
                "opaque_reason": None if rule.recipes else "alias target barrier",
            }
        )
        all_ids = [atom_id]
        roots = []
        for fragment in prerequisite_fragments:
            all_ids.extend(fragment.atoms)
            roots.extend(fragment.roots)
        if not roots:
            roots = [atom_id]
        result = Fragment(list(dict.fromkeys(roots)), [atom_id], list(dict.fromkeys(all_ids)))
        self._cache[target] = result
        return result

    def _opaque(self, target: str, code: str) -> Fragment:
        self.compilation.diagnostic(
            code,
            "Make target was preserved as a native opaque delegate",
            source=self.source,
            symbol=target,
        )
        atom_id = self.compilation.emit(
            {
                "id": f"make-{_slug(target)}-opaque",
                "operation": {
                    "kind": "opaque",
                    "argv": ["make", "-f", str(self.makefile), target],
                    "cwd": str(self.cwd),
                    "completion": "process_exit",
                    "internal_parallelism": (
                        {"kind": "bounded", "tokens": 1}
                        if self.not_parallel else
                        {"kind": "native_scheduler", "tokens": None}
                    ),
                },
                "side_effect": True,
                "effects": [{"domain": "unknown", "key": "build-tree", "mode": "write"}],
                "semantics": {"splittable": False, "reorderable": "forbidden"},
                "assurance": {
                    "parse": "opaque",
                    "control": "unknown",
                    "effects": "unknown",
                    "codegen": "native_delegate",
                    "rank": 0.2,
                    "blockers": [code, "UNKNOWN_EFFECT"],
                },
                "provenance": {
                    "adapter": "makefile",
                    "source": self.source,
                    "symbol": target,
                    "confidence": 1.0,
                },
                "opaque_reason": code,
            }
        )
        return Fragment([atom_id], [atom_id], [atom_id])

    def delegate(self, target: str) -> None:
        self.compilation.native_delegates.append(
            {
                "kind": "make_native_graph",
                "argv": ["make", "-f", str(self.makefile), target],
                "cwd": str(self.cwd),
                "source": self.source,
                "symbol": target,
                "internal_parallelism": (
                    {"kind": "bounded", "tokens": 1}
                    if self.not_parallel else
                    {"kind": "native_scheduler", "tokens": None}
                ),
                "reason": "Make owns recipe shell semantics, implicit rules, and jobserver coordination.",
            }
        )


_RUBY_COMPOSE_PARSER = r'''
require "yaml"
require "json"

def string_array(value, label)
  return [] if value.nil?
  raise "#{label} must be an array" unless value.is_a?(Array)
  raise "#{label} is too large" if value.length > 128
  value.each do |item|
    raise "#{label} entries must be strings" unless item.is_a?(String)
  end
  value
end

def interpolation?(value)
  case value
  when String
    value.include?("${")
  when Array
    value.any? { |item| interpolation?(item) }
  when Hash
    value.any? { |key, item| interpolation?(key) || interpolation?(item) }
  else
    false
  end
end

def normalize_port(value, label)
  return value if value.is_a?(String)
  raise "#{label} long syntax must be a mapping" unless value.is_a?(Hash)
  target = value["target"]
  published = value["published"]
  protocol = value["protocol"]
  host_ip = value["host_ip"]
  raise "#{label}.target must be a string or integer" unless target.is_a?(String) || target.is_a?(Integer)
  unless published.nil? || published.is_a?(String) || published.is_a?(Integer)
    raise "#{label}.published must be a string or integer"
  end
  raise "#{label}.protocol must be a string" unless protocol.nil? || protocol.is_a?(String)
  raise "#{label}.host_ip must be a string" unless host_ip.nil? || host_ip.is_a?(String)
  {
    "target" => target,
    "published" => published,
    "protocol" => protocol || "tcp",
    "host_ip" => host_ip
  }
end

def normalize_mount(value, label)
  return value if value.is_a?(String)
  raise "#{label} long syntax must be a mapping" unless value.is_a?(Hash)
  out = {}
  ["type", "source", "target"].each do |key|
    item = value[key]
    raise "#{label}.#{key} must be a string" unless item.nil? || item.is_a?(String)
    out[key] = item unless item.nil?
  end
  read_only = value["read_only"]
  raise "#{label}.read_only must be boolean" unless read_only.nil? || read_only == true || read_only == false
  out["read_only"] = read_only unless read_only.nil?
  out
end

def reject_duplicate_yaml_keys(node, path = "$")
  case node
  when Psych::Nodes::Stream, Psych::Nodes::Document, Psych::Nodes::Sequence
    node.children.each_with_index do |child, index|
      reject_duplicate_yaml_keys(child, "#{path}[#{index}]")
    end
  when Psych::Nodes::Mapping
    seen = {}
    node.children.each_slice(2) do |key_node, value_node|
      raise "non-scalar YAML mapping key at #{path}" unless key_node.is_a?(Psych::Nodes::Scalar)
      key = key_node.value
      raise "duplicate YAML key #{key} at #{path}" if seen[key]
      seen[key] = true
      reject_duplicate_yaml_keys(value_node, "#{path}.#{key}")
    end
  end
end

data = STDIN.read
reject_duplicate_yaml_keys(Psych.parse_stream(data))
doc = YAML.safe_load(
  data,
  permitted_classes: [],
  permitted_symbols: [],
  aliases: false
) || {}
raise "top-level YAML must be a mapping" unless doc.is_a?(Hash)
raise "top-level name must be a string" unless doc["name"].nil? || doc["name"].is_a?(String)
raise "Compose include is not supported by the bounded frontend" if doc.key?("include")
services = doc["services"] || {}
raise "services must be a mapping" unless services.is_a?(Hash)
raise "too many services" if services.length > 256
out = {}
services.each do |name, raw|
  raise "service names must be strings" unless name.is_a?(String)
  raise "service #{name} must be a mapping" unless raw.is_a?(Hash)
  raise "service #{name} extends is not supported by the bounded frontend" if raw.key?("extends")
  deps = {}
  depends = raw["depends_on"] || {}
  if depends.is_a?(Array)
    raise "service #{name} has too many dependencies" if depends.length > 128
    depends.each do |item|
      raise "service #{name} dependency names must be strings" unless item.is_a?(String)
      deps[item] = {"condition" => "service_started", "required" => true, "restart" => false}
    end
  elsif depends.is_a?(Hash)
    raise "service #{name} has too many dependencies" if depends.length > 128
    depends.each do |dep, value|
      raise "service #{name} dependency names must be strings" unless dep.is_a?(String)
      raise "service #{name} dependency #{dep} must be a mapping" unless value.nil? || value.is_a?(Hash)
      condition = value.is_a?(Hash) ? value["condition"] : nil
      required = value.is_a?(Hash) ? value["required"] : nil
      restart = value.is_a?(Hash) ? value["restart"] : nil
      raise "dependency condition must be a string" unless condition.nil? || condition.is_a?(String)
      raise "dependency required must be boolean" unless required.nil? || required == true || required == false
      raise "dependency restart must be boolean" unless restart.nil? || restart == true || restart == false
      deps[dep] = {
        "condition" => condition || "service_started",
        "required" => required.nil? ? true : required,
        "restart" => restart.nil? ? false : restart
      }
    end
  else
    raise "service #{name} depends_on must be an array or mapping"
  end
  ports = raw["ports"] || []
  raise "service #{name} ports must be an array" unless ports.is_a?(Array)
  raise "service #{name} has too many ports" if ports.length > 128
  mounts = raw["volumes"] || []
  raise "service #{name} volumes must be an array" unless mounts.is_a?(Array)
  raise "service #{name} has too many volumes" if mounts.length > 128
  healthcheck = raw["healthcheck"]
  unless healthcheck.nil? || healthcheck.is_a?(Hash)
    raise "service #{name} healthcheck must be a mapping"
  end
  implicit_dependencies = []
  string_array(raw["links"], "service #{name} links").each do |item|
    implicit_dependencies << item.split(":", 2)[0]
  end
  string_array(raw["volumes_from"], "service #{name} volumes_from").each do |item|
    implicit_dependencies << item.split(":", 2)[0] unless item.start_with?("container:")
  end
  ["ipc", "pid", "network_mode"].each do |field|
    value = raw[field]
    unless value.nil? || value.is_a?(String)
      raise "service #{name} #{field} must be a string"
    end
    implicit_dependencies << value.split(":", 2)[1] if value && value.start_with?("service:")
  end
  out[name] = {
    "depends_on" => deps,
    "implicit_dependencies" => implicit_dependencies.compact.uniq,
    "profiles" => string_array(raw["profiles"], "service #{name} profiles"),
    "ports" => ports.each_with_index.map { |item, index| normalize_port(item, "service #{name} port #{index}") },
    "volumes" => mounts.each_with_index.map { |item, index| normalize_mount(item, "service #{name} volume #{index}") },
    "devices" => string_array(raw["devices"], "service #{name} devices"),
    "container_name" => raw["container_name"].is_a?(String) ? raw["container_name"] : nil,
    "has_healthcheck" => healthcheck.is_a?(Hash) && healthcheck["disable"] != true,
    "restart" => raw["restart"].is_a?(String) ? raw["restart"] : nil,
    "has_interpolation" => interpolation?(raw)
  }
  unless raw["container_name"].nil? || raw["container_name"].is_a?(String)
    raise "service #{name} container_name must be a string"
  end
end
STDOUT.write(JSON.generate({
  "name" => doc["name"],
  "name_interpolation" => interpolation?(doc["name"]),
  "services" => out
}))
'''


def _safe_ruby_environment(ruby: str) -> dict[str, str]:
    """Build a minimal host-native environment without Ruby/Gem injection hooks."""

    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if not system_root:
            raise AtomError("safe Compose YAML parser requires the Windows system root")
        environment = {
            "PATH": ";".join(
                item
                for item in (
                    ntpath.dirname(ruby),
                    ntpath.join(system_root, "System32"),
                )
                if item
            ),
            "SystemRoot": system_root,
            "WINDIR": system_root,
        }
        for name in ("TEMP", "TMP"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        return environment
    return {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def _parse_compose_with_safe_yaml(path: Path) -> dict[str, Any]:
    system_ruby = Path("/usr/bin/ruby")
    ruby = str(system_ruby) if system_ruby.is_file() else resolve_host_executable("ruby")
    if not ruby:
        raise AtomError("a standards-compliant safe YAML parser is unavailable")
    source = _strict_read_text(path, str(path))
    try:
        completed = subprocess.run(
            [ruby, "--disable-gems", "-e", _RUBY_COMPOSE_PARSER],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=SAFE_YAML_PARSER_TIMEOUT_SECONDS,
            env=_safe_ruby_environment(ruby),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AtomError(f"safe Compose YAML parser failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[0] if completed.stderr.strip() else "unknown parser error"
        raise AtomError(f"safe Compose YAML parser rejected the file: {detail[:500]}")
    if len(completed.stdout) > MAX_SOURCE_BYTES:
        raise AtomError("sanitized Compose parser output exceeded its bound")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AtomError(f"safe Compose parser returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), dict):
        raise AtomError("safe Compose parser returned an invalid structure")
    return payload


class ComposeCompiler:
    def __init__(self, compilation: Compilation, compose_file: Path) -> None:
        self.compilation = compilation
        self.compose_file = compose_file.resolve()
        self.cwd = self.compose_file.parent
        self.source = _source_label(self.compose_file, compilation.project)
        compilation.snapshot(self.compose_file)
        self.payload = _parse_compose_with_safe_yaml(self.compose_file)
        self.services: dict[str, dict[str, Any]] = self.payload["services"]
        for service, raw in self.services.items():
            if not isinstance(raw, dict) or not isinstance(raw.get("depends_on", {}), dict):
                raise AtomError(f"normalized Compose service is invalid: {service}")
            for dependency in raw.get("implicit_dependencies", []):
                raw["depends_on"].setdefault(
                    dependency,
                    {"condition": "service_started", "required": True, "restart": False},
                )
        self.project_name = self.payload.get("name") or self.cwd.name
        self.project_name_dynamic = bool(self.payload.get("name_interpolation"))

    @staticmethod
    def _condition(raw: Any) -> str:
        if not isinstance(raw, dict):
            raise AtomError("normalized Compose dependency must be a mapping")
        condition = raw.get("condition", "service_started")
        if condition not in {
            "service_started", "service_healthy", "service_completed_successfully",
        }:
            raise AtomError(f"unsupported Compose dependency condition: {condition}")
        return condition

    def compile(self, selected: list[str], active_profiles: set[str]) -> Fragment:
        selected = list(dict.fromkeys(selected))
        unknown = sorted(set(selected) - set(self.services))
        if unknown:
            raise AtomError(f"unknown Compose services: {unknown}")
        closure: set[str] = set()

        def include(service: str) -> None:
            if service in closure:
                return
            raw = self.services[service]
            profiles = set(raw.get("profiles") or [])
            if profiles and not (profiles & active_profiles) and service not in selected:
                raise AtomError(
                    f"Compose dependency {service} is excluded by the active profile set; "
                    "refusing to lower an incomplete service graph"
                )
            closure.add(service)
            for dependency, dependency_spec in raw.get("depends_on", {}).items():
                self._condition(dependency_spec)
                if dependency not in self.services:
                    raise AtomError(f"Compose service {service} has unknown dependency: {dependency}")
                include(dependency)

        for service in selected:
            include(service)

        visiting: set[str] = set()
        visited: set[str] = set()

        def check_cycle(service: str) -> None:
            if service in visiting:
                raise AtomError(f"Compose dependency graph contains a cycle involving {service}")
            if service in visited:
                return
            visiting.add(service)
            for dependency in self.services[service].get("depends_on", {}):
                if dependency in closure:
                    check_cycle(dependency)
            visiting.remove(service)
            visited.add(service)

        for service in sorted(closure):
            check_cycle(service)

        atom_by_service: dict[str, str] = {}
        for service in sorted(closure):
            raw = self.services[service]
            dependency_conditions = {
                consumer: self._condition(spec)
                for consumer in closure
                for spec in [self.services[consumer].get("depends_on", {}).get(service)]
                if spec is not None
            }
            if "service_healthy" in dependency_conditions.values() and not raw.get("has_healthcheck"):
                raise AtomError(
                    f"Compose service {service} is required to be healthy, but no enabled healthcheck was declared"
                )
            completion = (
                "successful_service_exit"
                if "service_completed_successfully" in dependency_conditions.values()
                else "healthy"
                if raw.get("has_healthcheck") or "service_healthy" in dependency_conditions.values()
                else "ready"
            )
            effects: list[dict[str, str]] = [
                {"domain": "compose-service", "key": f"{self.project_name}:{service}", "mode": "lease"}
            ]
            if raw.get("container_name"):
                effects.append({"domain": "container-name", "key": raw["container_name"], "mode": "lease"})
            for port in raw.get("ports", []):
                host = _compose_host_port(port)
                if host:
                    effects.append({"domain": "host-port", "key": host, "mode": "lease"})
            for device in raw.get("devices", []):
                effects.append({"domain": "device", "key": device.split(":", 1)[0], "mode": "lease"})
            dynamic = self.project_name_dynamic or bool(raw.get("has_interpolation"))
            atom_by_service[service] = self.compilation.emit(
                {
                    "id": f"compose-{_slug(service)}",
                    "operation": {
                        "kind": "compose_service",
                        "argv": ["docker", "compose", "-f", str(self.compose_file), "up", service],
                        "cwd": str(self.cwd),
                        "completion": completion,
                        "internal_parallelism": {"kind": "native_scheduler", "tokens": None},
                    },
                    "effects": effects,
                    "side_effect": True,
                    "profile": "mixed",
                    "semantics": {
                        "idempotent": True,
                        "retryable": None,
                        "deterministic": None,
                        "cacheable": False,
                        "commutative": False,
                        "cancel_safe": None,
                        "splittable": False,
                        "reorderable": "proved",
                    },
                    "batch": {"key": f"compose:{self.project_name}", "strategy": "compose_services"},
                    "assurance": {
                        "parse": "conservative" if dynamic else "exact",
                        "control": "partial" if dynamic else "exact",
                        "effects": "partial",
                        "codegen": "native_delegate",
                        "rank": 0.45 if dynamic else 0.8,
                        "blockers": (
                            ["DYNAMIC_COMPOSE_VALUE", "COMPOSE_RUNTIME_EFFECTS_PARTIAL"]
                            if dynamic else
                            ["COMPOSE_RUNTIME_EFFECTS_PARTIAL"]
                        ),
                    },
                    "provenance": {
                        "adapter": "compose",
                        "source": self.source,
                        "symbol": service,
                        "confidence": 0.6 if dynamic else 1.0,
                    },
                }
            )

        by_id = {atom["id"]: atom for atom in self.compilation.atoms}
        for service, atom_id in atom_by_service.items():
            dependencies = []
            for dependency, dependency_spec in self.services[service].get("depends_on", {}).items():
                condition = self._condition(dependency_spec)
                kind = {
                    "service_healthy": "after_healthy",
                    "service_completed_successfully": "after_completion",
                    "service_started": "after_ready",
                }[condition]
                dependencies.append({"atom": atom_by_service[dependency], "kind": kind})
            by_id[atom_id]["dependencies"] = dependencies

        delegate_argv = ["docker", "compose", "-f", str(self.compose_file)]
        for profile in sorted(active_profiles):
            delegate_argv.extend(["--profile", profile])
        delegate_argv.extend(["up", "--wait", *selected])
        self.compilation.native_delegates.append(
            {
                "kind": "compose_native_graph",
                "argv": delegate_argv,
                "cwd": str(self.cwd),
                "source": self.source,
                "services": selected,
                "profiles": sorted(active_profiles),
                "internal_parallelism": {"kind": "native_scheduler", "tokens": None},
                "reason": "Compose owns service lifecycle, health conditions, project locking, and native parallelism.",
            }
        )
        roots = [
            atom_by_service[service]
            for service in closure
            if not by_id[atom_by_service[service]].get("dependencies")
        ]
        dependents = {
            dependency["atom"]
            for atom_id in atom_by_service.values()
            for dependency in by_id[atom_id].get("dependencies", [])
        }
        terminals = [atom_id for atom_id in atom_by_service.values() if atom_id not in dependents]
        return Fragment(sorted(roots), sorted(terminals), sorted(atom_by_service.values()))


def _compose_host_port(value: Any) -> str | None:
    """Return a canonical fixed host-port lease, or None for dynamic publish."""
    if isinstance(value, dict):
        published = value.get("published")
        if published is None:
            return None
        candidate = str(published)
        protocol = str(value.get("protocol") or "tcp").lower()
        host = str(value.get("host_ip") or "0.0.0.0")
    elif isinstance(value, str):
        text, _, protocol_suffix = value.partition("/")
        protocol = (protocol_suffix or "tcp").lower()
        if ":" not in text:
            # CONTAINER[/PROTO] requests an implementation-selected host port.
            return None
        if text.startswith("[") and "]" in text:
            closing = text.index("]")
            host = text[: closing + 1]
            remainder = text[closing + 1:]
            if not remainder.startswith(":"):
                return None
            pieces = remainder[1:].split(":")
            if len(pieces) != 2:
                return None
            candidate = pieces[0]
        else:
            pieces = text.rsplit(":", 2)
            if len(pieces) == 2:
                host, candidate = "0.0.0.0", pieces[0]
            elif len(pieces) == 3:
                host, candidate = pieces[0] or "0.0.0.0", pieces[1]
            else:
                return None
    else:
        return None
    numeric = candidate.split("-", 1)
    if not numeric or not all(item.isdigit() for item in numeric):
        return None
    return f"{protocol}://{host}:{candidate}"


def compile_entrypoints(
    project: Path,
    raw_entrypoints: Any,
    *,
    target_os: str | None = None,
    native_worker_ceiling: int | None = None,
) -> dict[str, Any]:
    """Compile host entrypoints, with an explicit OS seam for parser tests.

    Production callers omit ``target_os`` and are always bound to ``os.name``;
    the override exists so portable parser contracts can be regression-tested
    on Windows without weakening the native-Windows frontend gate.
    """
    effective_os = os.name if target_os is None else target_os
    if effective_os not in {"nt", "posix"}:
        raise AtomError("target_os must be 'nt' or 'posix'")
    if raw_entrypoints is None:
        raw_entrypoints = []
    if not isinstance(raw_entrypoints, list) or len(raw_entrypoints) > 64:
        raise AtomError("entrypoints must be an array with at most 64 entries")
    compilation = Compilation(project)
    roots: list[str] = []
    terminals: list[str] = []
    for index, entrypoint in enumerate(raw_entrypoints):
        if not isinstance(entrypoint, dict):
            raise AtomError(f"entrypoint {index} must be an object")
        adapter = entrypoint.get("adapter")
        entry_id = _slug(entrypoint.get("id", f"entry-{index}"))
        if effective_os == "nt" and adapter in {
            "shell",
            "package_script",
            "make_target",
            "compose_services",
        }:
            raise AtomError(
                f"entrypoint {entry_id} adapter {adapter} has POSIX shell semantics; "
                "Windows Preview requires an explicit argv atom or powershell_file"
            )
        if adapter == "shell":
            cwd_raw = entrypoint.get("cwd", str(project))
            cwd = Path(cwd_raw).expanduser()
            if not cwd.is_absolute():
                cwd = project / cwd
            cwd = cwd.resolve(strict=False)
            if not cwd.is_dir():
                raise AtomError(f"entrypoint {entry_id} cwd does not exist: {cwd}")
            command = _bounded_text(entrypoint.get("command"), f"entrypoint {entry_id} command")
            fragment = compile_shell(
                compilation,
                command,
                prefix=entry_id,
                cwd=cwd,
                source="task_plan",
                symbol=entry_id,
            )
        elif adapter == "package_script":
            package_file = Path(entrypoint.get("package_json", project / "package.json")).expanduser()
            if not package_file.is_absolute():
                package_file = project / package_file
            script = _bounded_text(entrypoint.get("script"), f"entrypoint {entry_id} script", 512)
            compiler = PackageScriptCompiler(compilation, package_file)
            fragment = compiler.compile(script)
        elif adapter == "make_target":
            makefile = Path(entrypoint.get("makefile", project / "Makefile")).expanduser()
            if not makefile.is_absolute():
                makefile = project / makefile
            target = _bounded_text(entrypoint.get("target"), f"entrypoint {entry_id} target", 512)
            compiler = MakefileCompiler(compilation, makefile)
            fragment = compiler.compile(target)
            compiler.delegate(target)
        elif adapter == "compose_services":
            compose_file = Path(entrypoint.get("compose_file", project / "compose.yml")).expanduser()
            if not compose_file.is_absolute():
                compose_file = project / compose_file
            services = entrypoint.get("services")
            if (
                not isinstance(services, list)
                or not services
                or len(services) > 256
                or not all(
                    isinstance(item, str) and item and "\x00" not in item and len(item) <= 256
                    for item in services
                )
            ):
                raise AtomError(f"entrypoint {entry_id} services must be a non-empty string array")
            profiles_raw = entrypoint.get("profiles", [])
            if (
                not isinstance(profiles_raw, list)
                or len(profiles_raw) > 64
                or not all(
                    isinstance(item, str) and item and "\x00" not in item and len(item) <= 256
                    for item in profiles_raw
                )
            ):
                raise AtomError(f"entrypoint {entry_id} profiles must be a string array")
            compiler = ComposeCompiler(compilation, compose_file)
            fragment = compiler.compile(services, set(profiles_raw))
        elif adapter == "test_suite":
            fragment = compile_test_suite(
                compilation,
                entrypoint,
                entry_id=entry_id,
                effective_os=effective_os,
                native_worker_ceiling=native_worker_ceiling,
            )
        elif adapter == "powershell_file":
            if effective_os != "nt":
                raise AtomError(
                    f"entrypoint {entry_id} powershell_file requires native Windows"
                )
            script_raw = _bounded_text(
                entrypoint.get("script_path"), f"entrypoint {entry_id} script_path", 4096
            )
            script = Path(script_raw).expanduser()
            if not script.is_absolute():
                script = project / script
            script = script.resolve(strict=True)
            if not script.is_file() or script.suffix.casefold() != ".ps1":
                raise AtomError(f"entrypoint {entry_id} script_path must be an existing .ps1 file")
            cwd_raw = entrypoint.get("cwd", str(script.parent))
            cwd = Path(cwd_raw).expanduser()
            if not cwd.is_absolute():
                cwd = project / cwd
            cwd = cwd.resolve(strict=True)
            if not cwd.is_dir():
                raise AtomError(f"entrypoint {entry_id} cwd does not exist: {cwd}")
            arguments = entrypoint.get("arguments", [])
            if (
                not isinstance(arguments, list)
                or len(arguments) > 128
                or not all(isinstance(item, str) for item in arguments)
            ):
                raise AtomError(f"entrypoint {entry_id} arguments must be a bounded string array")
            accesses = entrypoint.get("declared_accesses", [])
            effects = entrypoint.get("declared_effects", [])
            if not isinstance(accesses, list) or not isinstance(effects, list):
                raise AtomError(
                    f"entrypoint {entry_id} declared_accesses and declared_effects must be arrays"
                )
            complete = entrypoint.get("effects_declared_complete", False)
            if not isinstance(complete, bool):
                raise AtomError(f"entrypoint {entry_id} effects_declared_complete must be boolean")
            side_effect = entrypoint.get("side_effect", True)
            if not isinstance(side_effect, bool):
                raise AtomError(f"entrypoint {entry_id} side_effect must be boolean")
            profile = entrypoint.get("profile", "mixed")
            if profile not in {"cpu", "io", "mixed", "accelerator"}:
                raise AtomError(f"entrypoint {entry_id} profile is unsupported")
            pwsh = resolve_windows_path_executable("pwsh")
            blockers = [] if pwsh else ["POWERSHELL_7_UNAVAILABLE"]
            snapshot = compilation.snapshot(script)
            script_access = {"resource": str(script), "mode": "snapshot"}
            atom_id = compilation.emit(
                {
                    "id": entry_id,
                    "operation": {
                        "kind": "shell",
                        "argv": [
                            pwsh or "pwsh",
                            "-NoLogo",
                            "-NoProfile",
                            "-NonInteractive",
                            "-File",
                            str(script),
                            *[
                                _bounded_text(
                                    value, f"entrypoint {entry_id} argument", 32_768
                                )
                                for value in arguments
                            ],
                        ],
                        "cwd": str(cwd),
                        "env": {},
                        "completion": "process_exit",
                        "internal_parallelism": {"kind": "unknown", "tokens": None},
                    },
                    "accesses": [script_access, *accesses],
                    "effects": effects,
                    "profile": profile,
                    "side_effect": side_effect,
                    "semantics": {
                        "idempotent": None,
                        "retryable": None,
                        "deterministic": None,
                        "cacheable": False,
                        "commutative": False,
                        "cancel_safe": None,
                        "splittable": False,
                        "reorderable": "unknown",
                    },
                    "assurance": {
                        "parse": "exact",
                        "control": "exact",
                        "effects": "complete_declared" if complete else "unknown",
                        "codegen": "exact_argv",
                        "rank": 1.0 if complete and not blockers else 0.5,
                        "blockers": blockers + ([] if complete else ["INCOMPLETE_EFFECT_MODEL"]),
                    },
                    "provenance": {
                        "adapter": "powershell_file",
                        "source": snapshot["path"],
                        "symbol": entry_id,
                        "confidence": 1.0,
                    },
                }
            )
            fragment = Fragment([atom_id], [atom_id], [atom_id])
        else:
            raise AtomError(f"entrypoint {entry_id} has unsupported adapter: {adapter}")
        roots.extend(fragment.roots)
        terminals.extend(fragment.terminals)
    result = compilation.result()
    result["roots"] = list(dict.fromkeys(roots))
    result["terminals"] = list(dict.fromkeys(terminals))
    return result
