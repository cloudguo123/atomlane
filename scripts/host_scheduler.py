#!/usr/bin/env python3
"""Cross-process host admission control for AtomLane execution sessions.

The Atom compiler owns task semantics.  This module owns only a small set of
host capacity tokens so concurrent Codex/AtomLane processes cannot each assume
they have the whole machine.  State is advisory for utilization but fail-closed
for admission: malformed state never becomes an excuse to overcommit the host.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from platform_adapter import default_stats_path, exclusive_file_lock

SCHEDULER_SCHEMA = "atomlane/host-scheduler/v1"
MANAGED_RESOURCES = (
    "worker_slot",
    "cpu_core",
    "memory_mb",
    "accelerator_slot",
)
MAX_SESSIONS = 512
MAX_RESERVATIONS = 4096
MAX_STATE_BYTES = 8 * 1024 * 1024
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
HOST_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DEFAULT_STALE_SECONDS = 15.0
DEFAULT_HEARTBEAT_SECONDS = 2.0


class HostSchedulerError(RuntimeError):
    """The shared host scheduler could not preserve its admission contract."""


def default_scheduler_state_path() -> Path:
    override = os.environ.get("ATOMLANE_SCHEDULER_STATE_PATH")
    if override:
        return Path(override).expanduser()
    return default_stats_path().with_name("host-scheduler-v1.json")


def host_fingerprint(machine: dict[str, Any]) -> str:
    """Return a privacy-safe identity for one scheduling capacity domain."""
    payload = {
        "execution_boundary": (
            machine.get("execution_environment", {}).get("boundary")
            if isinstance(machine.get("execution_environment"), dict)
            else None
        ),
        "machine": machine.get("machine"),
        "chip": machine.get("chip"),
        "model_identifier": machine.get("model_identifier"),
        "logical_cpus": machine.get("logical_cpus"),
        "physical_cpus": machine.get("physical_cpus"),
        "memory_total_bytes": machine.get("memory_total_bytes"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _bounded_vector(raw: Any, label: str, *, allow_zero: bool) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise HostSchedulerError(f"{label} must be an object")
    unknown = set(raw) - set(MANAGED_RESOURCES)
    if unknown:
        raise HostSchedulerError(f"{label} contains unmanaged resources: {sorted(unknown)}")
    result: dict[str, float] = {}
    for key, value in raw.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < (0.0 if allow_zero else 1e-12)
            or float(value) > 1_000_000_000
        ):
            qualifier = "non-negative" if allow_zero else "positive"
            raise HostSchedulerError(f"{label}.{key} must be a finite {qualifier} number")
        result[key] = float(value)
    return result


def _empty_state(host_id: str) -> dict[str, Any]:
    return {
        "schema": SCHEDULER_SCHEMA,
        "host_fingerprint": host_id,
        "generation": 0,
        "updated_at_epoch_seconds": 0.0,
        "sessions": {},
        "reservations": {},
    }


def _pid_is_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                # Access denied proves that a protected process owns the PID;
                # other failures cannot safely prove liveness.
                return ctypes.get_last_error() == 5
            exit_code = wintypes.DWORD()
            try:
                return bool(
                    kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    and exit_code.value == 259
                )
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _process_identity(pid: int) -> str | None:
    """Return a kernel/process-table start identity when the host exposes one."""
    system = platform.system()
    if system == "Linux":
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = stat_text.rsplit(")", 1)[1].split()
            return f"linux-start-ticks:{fields[19]}"
        except (IndexError, OSError, UnicodeError):
            return None
    if system == "Darwin":
        try:
            completed = subprocess.run(
                ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1,
                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip() if completed.returncode == 0 else ""
        return f"darwin-lstart:{value}" if value else None
    if system == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            try:
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                ):
                    return None
            finally:
                kernel32.CloseHandle(handle)
            ticks = (int(creation.dwHighDateTime) << 32) | int(
                creation.dwLowDateTime
            )
            return f"windows-creation-ticks:{ticks}"
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    return None


class HostScheduler:
    """Coordinate generic host tokens across independent AtomLane processes."""

    def __init__(
        self,
        host_id: str,
        *,
        path: Path | None = None,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
    ) -> None:
        if not isinstance(host_id, str) or not HOST_ID_RE.fullmatch(host_id):
            raise HostSchedulerError("host fingerprint must be a sha256 identity")
        self.host_id = host_id
        self.path = (path or default_scheduler_state_path()).expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.stale_seconds = max(2.0, float(stale_seconds))
        self.heartbeat_seconds = min(
            DEFAULT_HEARTBEAT_SECONDS,
            self.stale_seconds / 3.0,
        )

    def _load(self) -> dict[str, Any]:
        try:
            path_state = os.lstat(self.path)
        except FileNotFoundError:
            return _empty_state(self.host_id)
        except OSError as exc:
            raise HostSchedulerError("host scheduler state cannot be inspected") from exc
        if stat.S_ISLNK(path_state.st_mode):
            raise HostSchedulerError(
                "host scheduler state path must not be a symbolic link"
            )
        if not stat.S_ISREG(path_state.st_mode) or path_state.st_size > MAX_STATE_BYTES:
            raise HostSchedulerError(
                "host scheduler state must be a bounded regular file"
            )
        try:
            raw = self.path.read_text(encoding="utf-8")
            state = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HostSchedulerError("host scheduler state is unreadable or invalid") from exc
        if not isinstance(state, dict) or state.get("schema") != SCHEDULER_SCHEMA:
            raise HostSchedulerError("host scheduler state has an unsupported schema")
        if state.get("host_fingerprint") != self.host_id:
            # A hardware/realm change creates a new capacity domain only when
            # the old domain is no longer active.  Active foreign records fail
            # closed instead of being silently discarded.
            if state.get("sessions") or state.get("reservations"):
                raise HostSchedulerError(
                    "host scheduler state belongs to a different hardware or execution realm"
                )
            return _empty_state(self.host_id)
        generation = state.get("generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise HostSchedulerError("host scheduler contains an invalid generation")
        sessions = state.get("sessions")
        reservations = state.get("reservations")
        if (
            not isinstance(sessions, dict)
            or not isinstance(reservations, dict)
            or len(sessions) > MAX_SESSIONS
            or len(reservations) > MAX_RESERVATIONS
        ):
            raise HostSchedulerError("host scheduler state exceeds its bounded shape")
        for session_id, session in sessions.items():
            if not SESSION_ID_RE.fullmatch(session_id) or not isinstance(session, dict):
                raise HostSchedulerError("host scheduler contains an invalid session")
            if (
                isinstance(session.get("pid"), bool)
                or not isinstance(session.get("pid"), int)
                or session["pid"] <= 0
            ):
                raise HostSchedulerError("host scheduler contains an invalid process id")
            _bounded_vector(session.get("capacities"), "session capacities", allow_zero=False)
            for field in ("created_at", "heartbeat_at"):
                value = session.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) < 0
                ):
                    raise HostSchedulerError(f"host scheduler contains an invalid {field}")
            process_identity = session.get("process_identity")
            if process_identity is not None and (
                not isinstance(process_identity, str)
                or not process_identity
                or len(process_identity) > 256
            ):
                raise HostSchedulerError(
                    "host scheduler contains an invalid process identity"
                )
            for field in ("profile", "responsiveness", "project_hash"):
                value = session.get(field, "")
                if not isinstance(value, str) or len(value) > 128:
                    raise HostSchedulerError(
                        f"host scheduler contains an invalid {field}"
                    )
            for field in ("ready_tasks", "running_tasks"):
                value = session.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise HostSchedulerError(f"host scheduler contains an invalid {field}")
        for reservation_id, reservation in reservations.items():
            if not SESSION_ID_RE.fullmatch(reservation_id) or not isinstance(
                reservation, dict
            ):
                raise HostSchedulerError("host scheduler contains an invalid reservation")
            if reservation.get("session_id") not in sessions:
                raise HostSchedulerError("host scheduler contains an orphan reservation")
            atom_id = reservation.get("atom_id")
            if not isinstance(atom_id, str) or not atom_id or len(atom_id) > 128:
                raise HostSchedulerError("host scheduler contains an invalid atom id")
            acquired_at = reservation.get("acquired_at")
            if (
                isinstance(acquired_at, bool)
                or not isinstance(acquired_at, (int, float))
                or not math.isfinite(float(acquired_at))
                or float(acquired_at) < 0
            ):
                raise HostSchedulerError(
                    "host scheduler contains an invalid reservation time"
                )
            _bounded_vector(
                reservation.get("claims"), "reservation claims", allow_zero=True
            )
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state["generation"] = int(state.get("generation", 0)) + 1
        state["updated_at_epoch_seconds"] = round(time.time(), 3)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix="host-scheduler-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(state, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError as exc:
            raise HostSchedulerError("host scheduler state cannot be committed") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _clean_stale(self, state: dict[str, Any], now: float) -> bool:
        stale: list[str] = []
        for session_id, session in state["sessions"].items():
            age = max(0.0, now - float(session["heartbeat_at"]))
            if age <= self.stale_seconds:
                continue
            pid = int(session["pid"])
            alive = _pid_is_alive(pid)
            recorded_identity = session.get("process_identity")
            current_identity = _process_identity(pid) if alive else None
            identity_reused = bool(
                recorded_identity
                and current_identity
                and recorded_identity != current_identity
            )
            if not alive or identity_reused:
                stale.append(session_id)
        if not stale:
            return False
        stale_set = set(stale)
        state["sessions"] = {
            key: value
            for key, value in state["sessions"].items()
            if key not in stale_set
        }
        state["reservations"] = {
            key: value
            for key, value in state["reservations"].items()
            if value["session_id"] not in stale_set
        }
        return True

    def _with_state(self, callback: Any) -> Any:
        if self.lock_path.is_symlink():
            raise HostSchedulerError("host scheduler lock path must not be a symbolic link")
        try:
            with exclusive_file_lock(self.lock_path, timeout_seconds=2.0):
                state = self._load()
                now = time.time()
                changed = self._clean_stale(state, now)
                result, callback_changed = callback(state, now)
                if changed or callback_changed:
                    self._save(state)
                return result
        except HostSchedulerError:
            raise
        except (OSError, TimeoutError) as exc:
            raise HostSchedulerError(
                "host scheduler state lock is unavailable"
            ) from exc

    def register(
        self,
        session_id: str,
        *,
        capacities: dict[str, float],
        profile: str,
        responsiveness: str,
        project_hash: str,
    ) -> None:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise HostSchedulerError("invalid host scheduler session id")
        normalized = _bounded_vector(capacities, "capacities", allow_zero=False)

        def mutate(state: dict[str, Any], now: float) -> tuple[None, bool]:
            existing = state["sessions"].get(session_id)
            if existing is not None and existing.get("pid") != os.getpid():
                raise HostSchedulerError("host scheduler session id collision")
            state["sessions"][session_id] = {
                "pid": os.getpid(),
                "process_identity": _process_identity(os.getpid()),
                "created_at": (
                    float(existing["created_at"]) if existing is not None else now
                ),
                "heartbeat_at": now,
                "profile": str(profile)[:32],
                "responsiveness": str(responsiveness)[:32],
                "project_hash": str(project_hash)[:80],
                "capacities": normalized,
                "ready_tasks": 0,
                "running_tasks": 0,
            }
            return None, True

        self._with_state(mutate)

    def heartbeat(
        self,
        session_id: str,
        *,
        ready_tasks: int | None = None,
        running_tasks: int | None = None,
        capacities: dict[str, float] | None = None,
    ) -> None:
        normalized = (
            _bounded_vector(capacities, "capacities", allow_zero=False)
            if capacities is not None
            else None
        )

        def mutate(state: dict[str, Any], now: float) -> tuple[None, bool]:
            session = state["sessions"].get(session_id)
            if session is None or session.get("pid") != os.getpid():
                raise HostSchedulerError("host scheduler session is no longer registered")
            changed = False
            if now - float(session["heartbeat_at"]) >= self.heartbeat_seconds:
                session["heartbeat_at"] = now
                changed = True
            if ready_tasks is not None:
                next_ready = max(0, int(ready_tasks))
                if session["ready_tasks"] != next_ready:
                    session["ready_tasks"] = next_ready
                    changed = True
            if running_tasks is not None:
                next_running = max(0, int(running_tasks))
                if session["running_tasks"] != next_running:
                    session["running_tasks"] = next_running
                    changed = True
            if normalized is not None and session["capacities"] != normalized:
                session["capacities"] = normalized
                changed = True
            return None, changed

        self._with_state(mutate)

    @staticmethod
    def _effective_capacities(state: dict[str, Any]) -> dict[str, float]:
        active = [
            session
            for session in state["sessions"].values()
            if session["ready_tasks"] > 0 or session["running_tasks"] > 0
        ]
        if not active:
            return {}
        result: dict[str, float] = {}
        for resource in MANAGED_RESOURCES:
            values = [
                float(session["capacities"][resource])
                for session in active
                if resource in session["capacities"]
            ]
            if values:
                result[resource] = min(values)
        return result

    @staticmethod
    def _usage(state: dict[str, Any]) -> dict[str, float]:
        usage = {resource: 0.0 for resource in MANAGED_RESOURCES}
        for reservation in state["reservations"].values():
            for resource, units in reservation["claims"].items():
                usage[resource] += float(units)
        return usage

    def try_acquire(
        self,
        session_id: str,
        atom_id: str,
        claims: dict[str, float],
    ) -> dict[str, Any] | None:
        result = self.try_acquire_batch(
            session_id,
            [{"atom_id": atom_id, "claims": claims}],
        )
        return result["admitted"].get(atom_id)

    def try_acquire_batch(
        self,
        session_id: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Admit one ordered ready set under a single global state decision."""
        if not isinstance(candidates, list) or len(candidates) > 128:
            raise HostSchedulerError("host admission batch must contain at most 128 atoms")
        normalized_candidates: list[tuple[str, dict[str, float], str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise HostSchedulerError("host admission candidate must be an object")
            atom_id = candidate.get("atom_id")
            if (
                not isinstance(atom_id, str)
                or not atom_id
                or len(atom_id) > 128
                or atom_id in seen
            ):
                raise HostSchedulerError("host admission atom ids must be unique and bounded")
            seen.add(atom_id)
            claims = _bounded_vector(
                candidate.get("claims"), "claims", allow_zero=True
            )
            reservation_id = "r_" + hashlib.sha256(
                f"{session_id}\0{atom_id}".encode()
            ).hexdigest()[:48]
            normalized_candidates.append((atom_id, claims, reservation_id))

        def mutate(
            state: dict[str, Any], now: float
        ) -> tuple[dict[str, Any], bool]:
            session = state["sessions"].get(session_id)
            if session is None or session.get("pid") != os.getpid():
                raise HostSchedulerError("host scheduler session is no longer registered")
            changed = False
            if now - float(session["heartbeat_at"]) >= self.heartbeat_seconds:
                session["heartbeat_at"] = now
                changed = True
            capacities = self._effective_capacities(state)
            usage = self._usage(state)
            waiting_sessions = [
                key
                for key, item in state["sessions"].items()
                if item["ready_tasks"] > 0
            ]
            worker_capacity = capacities.get("worker_slot", 1.0)
            fair_share = worker_capacity / max(1, len(waiting_sessions))
            own_worker_usage = sum(
                float(item["claims"].get("worker_slot", 0.0))
                for item in state["reservations"].values()
                if item["session_id"] == session_id
            )
            other_waiter_exists = any(key != session_id for key in waiting_sessions)
            admitted: dict[str, dict[str, Any]] = {}
            denied: list[dict[str, str]] = []
            for atom_id, claims, reservation_id in normalized_candidates:
                existing = state["reservations"].get(reservation_id)
                if existing is not None:
                    if existing["claims"] != claims:
                        raise HostSchedulerError("host reservation claims changed")
                    admitted[atom_id] = {
                        "reservation_id": reservation_id,
                        "effective_capacities": capacities,
                        "usage": dict(usage),
                        "fair_worker_share": None,
                    }
                    continue

                blocked_resource = next(
                    (
                        resource
                        for resource, units in claims.items()
                        if capacities.get(resource) is None
                        or usage.get(resource, 0.0) + units
                        > float(capacities[resource]) + 1e-9
                    ),
                    None,
                )
                if blocked_resource is not None:
                    denied.append(
                        {"atom_id": atom_id, "reason": f"capacity:{blocked_resource}"}
                    )
                    continue
                requested_worker = claims.get("worker_slot", 0.0)
                if (
                    other_waiter_exists
                    and own_worker_usage > 0
                    and own_worker_usage + requested_worker
                    > math.ceil(fair_share) + 1e-9
                ):
                    denied.append({"atom_id": atom_id, "reason": "fair_share"})
                    continue

                state["reservations"][reservation_id] = {
                    "session_id": session_id,
                    "atom_id": atom_id,
                    "claims": claims,
                    "acquired_at": now,
                }
                changed = True
                for resource, units in claims.items():
                    usage[resource] = usage.get(resource, 0.0) + units
                own_worker_usage += requested_worker
                admitted[atom_id] = {
                    "reservation_id": reservation_id,
                    "effective_capacities": capacities,
                    "usage": dict(usage),
                    "fair_worker_share": round(fair_share, 6),
                }
            return {
                "admitted": admitted,
                "denied": denied,
                "effective_capacities": capacities,
                "usage": usage,
                "fair_worker_share": round(fair_share, 6),
                "decision_scope": "ordered_ready_batch",
            }, changed

        return self._with_state(mutate)

    def release(self, session_id: str, reservation_id: str) -> None:
        def mutate(state: dict[str, Any], now: float) -> tuple[None, bool]:
            session = state["sessions"].get(session_id)
            if session is not None and session.get("pid") == os.getpid():
                session["heartbeat_at"] = now
            reservation = state["reservations"].get(reservation_id)
            if reservation is None:
                return None, session is not None
            if reservation["session_id"] != session_id:
                raise HostSchedulerError("host reservation belongs to another session")
            del state["reservations"][reservation_id]
            return None, True

        self._with_state(mutate)

    def close(self, session_id: str) -> None:
        def mutate(state: dict[str, Any], _now: float) -> tuple[None, bool]:
            session = state["sessions"].get(session_id)
            if session is None:
                return None, False
            if session.get("pid") != os.getpid():
                raise HostSchedulerError("host scheduler session belongs to another process")
            del state["sessions"][session_id]
            state["reservations"] = {
                key: value
                for key, value in state["reservations"].items()
                if value["session_id"] != session_id
            }
            return None, True

        self._with_state(mutate)

    def snapshot(self) -> dict[str, Any]:
        def read(state: dict[str, Any], _now: float) -> tuple[dict[str, Any], bool]:
            usage = self._usage(state)
            capacities = self._effective_capacities(state)
            return {
                "schema": SCHEDULER_SCHEMA,
                "host_fingerprint": self.host_id,
                "active_sessions": len(state["sessions"]),
                "active_reservations": len(state["reservations"]),
                "waiting_tasks": sum(
                    int(item["ready_tasks"]) for item in state["sessions"].values()
                ),
                "running_tasks": sum(
                    int(item["running_tasks"]) for item in state["sessions"].values()
                ),
                "effective_capacities": capacities,
                "reserved": usage,
                "generation": int(state.get("generation", 0)),
            }, False

        return self._with_state(read)
