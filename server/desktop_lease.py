"""Cross-process desktop lease for the background computer-use server.

The physical Mac desktop is a singleton peripheral: one keyboard, one
mouse, one focus. Multiple ``bin/cua-server`` instances (one per MCP
client, or one per subagent) can coexist, but only one should be
*driving* at any instant. This module provides a process-safe lease
protocol that every mutating tool call wraps itself in.

Design:

- `/tmp/cua-desktop.lock` — ``fcntl.flock(LOCK_EX)`` held for the
  duration of the lease. If a holder process dies, the kernel releases
  the lock automatically, so we never wedge permanently.
- `/tmp/cua-desktop.holder.json` — rewritten on acquire with metadata
  about who holds it. Read on contention to produce a helpful error
  message (``desktop busy: held by 'codex-2' for 4.2s``).

Two usage patterns:

1. *Implicit short lease*: every mutating tool wraps its body in
   ``with guard(agent_label):`` -- lock is grabbed, tool runs, lock
   released. Uncoordinated tool calls from different agents serialize
   cleanly instead of stomping each other's keystrokes.
2. *Explicit long lease*: the ``acquire_desktop`` / ``release_desktop``
   MCP tools let an agent hold the lease across multiple turns when it
   needs atomicity (open menu -> wait -> click submenu). TTL-expired
   leases are forcibly reclaimed so a crashed agent can't wedge it.

``get_app_state`` and ``list_apps`` intentionally do *not* take the
lease -- they're read-only, per-pid, and fine to run concurrently.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterator, Optional

_LOCK_DIR = Path(os.environ.get("CUA_LEASE_DIR", "/tmp"))
_LOCK_PATH = _LOCK_DIR / "cua-desktop.lock"
_HOLDER_PATH = _LOCK_DIR / "cua-desktop.holder.json"

DEFAULT_TTL_S = float(os.environ.get("CUA_LEASE_DEFAULT_TTL_S", "30.0"))
DEFAULT_WAIT_S = float(os.environ.get("CUA_LEASE_DEFAULT_WAIT_S", "8.0"))


class DesktopBusy(RuntimeError):
    """Raised when the desktop lease could not be acquired within ``wait_s``."""


@dataclass
class LeaseHandle:
    """Opaque token returned by ``acquire``; pass it back to ``release``.

    The ``fd`` field is a file descriptor holding the ``flock`` -- closing it
    releases the lock. ``token`` is a random opaque value used by
    ``release`` / ``with_held_lease`` to verify a caller isn't releasing a
    lease it doesn't own.
    """
    fd: IO[bytes]
    token: str
    agent_label: str
    acquired_at: float
    ttl_s: float


# Process-wide pointer to the current *explicit* long-lived lease, if
# any. One MCP server process represents one agent, so a single slot is
# the right granularity: ``acquire_desktop`` parks the handle here,
# subsequent mutating tool calls see it and reuse it instead of
# re-acquiring (which would deadlock against themselves since flock is
# per-fd and we already hold it). ``release_desktop`` clears it.
_explicit_lock = threading.Lock()
_explicit_lease: Optional["LeaseHandle"] = None


def _current_process_lease() -> Optional["LeaseHandle"]:
    with _explicit_lock:
        return _explicit_lease


def _set_process_lease(lease: Optional["LeaseHandle"]) -> None:
    global _explicit_lease
    with _explicit_lock:
        _explicit_lease = lease


def _ensure_lock_file() -> IO[bytes]:
    """Open the lock file (creating it if needed) with 0o666 perms.

    The file lives in ``/tmp`` so any local user can participate; the
    world-writable mode mirrors how many Unix coordination files work
    (pid files, socket files) and is safe because the file itself
    carries no sensitive state -- only lock status.
    """
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = open(_LOCK_PATH, "a+b", buffering=0)
    try:
        os.fchmod(fd.fileno(), 0o666)
    except PermissionError:
        # File is owned by a different user; that's fine, we still got
        # an fd and can flock it. Ignore chmod failures.
        pass
    return fd


def _write_holder(agent_label: str, token: str, ttl_s: float) -> None:
    info = {
        "pid": os.getpid(),
        "agent_label": agent_label,
        "token": token,
        "acquired_at": time.time(),
        "ttl_s": ttl_s,
    }
    tmp = _HOLDER_PATH.with_suffix(".holder.json.tmp")
    tmp.write_text(json.dumps(info))
    tmp.replace(_HOLDER_PATH)


def _read_holder() -> Optional[dict]:
    try:
        return json.loads(_HOLDER_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _clear_holder_if_ours(token: str) -> None:
    info = _read_holder()
    if info and info.get("token") == token:
        try:
            _HOLDER_PATH.unlink()
        except FileNotFoundError:
            pass


def current_holder() -> Optional[dict]:
    """Return metadata about the current holder, or ``None`` if free.

    Filters out stale entries whose ``pid`` is dead or whose TTL has
    elapsed -- they're harmless "zombie" metadata because the real
    source of truth is the flock, but keeping the JSON tidy produces
    nicer error messages.
    """
    info = _read_holder()
    if info is None:
        return None
    pid = int(info.get("pid", 0))
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # still alive, just not ours to signal
    acquired = float(info.get("acquired_at", 0.0))
    ttl = float(info.get("ttl_s", 0.0))
    if ttl > 0 and time.time() - acquired > ttl:
        return None
    return info


def acquire(
    agent_label: str,
    ttl_s: float = DEFAULT_TTL_S,
    wait_s: float = DEFAULT_WAIT_S,
) -> LeaseHandle:
    """Acquire the desktop lease, blocking up to ``wait_s`` seconds.

    Raises ``DesktopBusy`` if another process holds the lock past the
    wait window. The returned ``LeaseHandle`` must be passed to
    ``release`` (or used via ``guard`` / ``with_held_lease``) to free
    the lock; if the process dies the kernel releases it for us.
    """
    fd = _ensure_lock_file()
    deadline = time.time() + max(0.0, wait_s)
    while True:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() >= deadline:
                fd.close()
                holder = current_holder() or {}
                who = holder.get("agent_label", "another agent")
                since = time.time() - float(holder.get("acquired_at", time.time()))
                raise DesktopBusy(
                    f"desktop busy: held by {who!r} for {since:.1f}s "
                    f"(pid {holder.get('pid', '?')}). Retry or ask the "
                    f"holder to release_desktop()."
                )
            time.sleep(0.05)

    token = os.urandom(8).hex()
    lease = LeaseHandle(
        fd=fd,
        token=token,
        agent_label=agent_label,
        acquired_at=time.time(),
        ttl_s=ttl_s,
    )
    try:
        _write_holder(agent_label, token, ttl_s)
    except Exception:
        # Holder-metadata write failed (permissions? full disk?). The
        # flock itself is still valid, so proceed -- error messages on
        # contention will just be less informative.
        pass
    return lease


def release(lease: LeaseHandle) -> None:
    """Release a previously-acquired lease. Idempotent."""
    try:
        _clear_holder_if_ours(lease.token)
    finally:
        try:
            fcntl.flock(lease.fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            lease.fd.close()
        except OSError:
            pass


@contextlib.contextmanager
def guard(
    agent_label: str = "",
    ttl_s: float = DEFAULT_TTL_S,
    wait_s: float = DEFAULT_WAIT_S,
) -> Iterator[LeaseHandle]:
    """Context manager for implicit short leases around a single tool call.

    If the current thread already holds a lease (e.g. inside an explicit
    ``acquire_desktop`` block), this is a no-op: we reuse the existing
    lease instead of deadlocking against ourselves. Otherwise we grab
    the lock for the duration of the ``with`` body and release it on
    exit.
    """
    existing = _current_process_lease()
    if existing is not None:
        # An explicit long-lived lease is already held by this process;
        # don't try to re-flock (would block forever) and don't release
        # it on scope exit (the explicit holder owns that).
        yield existing
        return
    label = agent_label or f"pid-{os.getpid()}"
    lease = acquire(label, ttl_s=ttl_s, wait_s=wait_s)
    try:
        yield lease
    finally:
        release(lease)


def hold_explicit(lease: LeaseHandle) -> None:
    """Register ``lease`` as this process's long-lived explicit lease.

    Used by the ``acquire_desktop`` MCP tool: the call acquires the
    flock, parks the handle in the process-wide slot so subsequent
    tool calls reuse it instead of re-acquiring, and returns to the
    model. ``release_explicit`` undoes this.
    """
    _set_process_lease(lease)


def release_explicit() -> Optional[LeaseHandle]:
    """Release this process's explicit lease, if any. Returns the handle."""
    lease = _current_process_lease()
    if lease is None:
        return None
    _set_process_lease(None)
    release(lease)
    return lease


def held_by_this_process() -> Optional[LeaseHandle]:
    """Expose this process's explicit lease (for introspection / labelling)."""
    return _current_process_lease()
