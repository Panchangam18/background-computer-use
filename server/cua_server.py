"""Background computer-use MCP server.

Exposes the nine-tool surface pioneered by the Codex computer-use plugin
(`list_apps`, `get_app_state`, `click`, `drag`, `perform_secondary_action`,
`press_key`, `scroll`, `set_value`, `type_text`) over MCP stdio, implemented
with macOS Accessibility + Quartz Core Graphics APIs through PyObjC.

Design goals, in order:

1.  Drive target apps **without activating them**. Events are posted via
    ``CGEventPostToPid`` so the user's foreground app and cursor are not
    disturbed.
2.  Return a hybrid payload on ``get_app_state`` -- an indexed
    accessibility tree (machine + human readable) *and* a window
    screenshot -- so the model can use either rail.
3.  Keep element indexes snapshot-local: every ``get_app_state`` call
    rebuilds a fresh index; subsequent tool calls in the same turn look
    up elements from that cache by index.

This file is intentionally self-contained so the launcher script only
has to install ``requirements.txt``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# PyObjC / macOS imports
# ---------------------------------------------------------------------------

try:
    from AppKit import (  # type: ignore
        NSWorkspace,
        NSRunningApplication,
        NSApplicationActivationPolicyRegular,
    )
    import Quartz  # type: ignore
    from Quartz import (  # type: ignore
        CGEventCreateMouseEvent,
        CGEventCreateKeyboardEvent,
        CGEventCreateScrollWheelEvent,
        CGEventKeyboardSetUnicodeString,
        CGEventSetFlags,
        CGEventPost,
        CGEventPostToPid,
        CGEventSetIntegerValueField,
        CGWindowListCopyWindowInfo,
        CGWindowListCreateImage,
        CGImageGetWidth,
        CGImageGetHeight,
        CGImageGetDataProvider,
        CGDataProviderCopyData,
        CGImageGetBytesPerRow,
        CGImageGetBitsPerPixel,
        CGMainDisplayID,
        CGDisplayBounds,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGEventLeftMouseDragged,
        kCGEventRightMouseDown,
        kCGEventRightMouseUp,
        kCGEventOtherMouseDown,
        kCGEventOtherMouseUp,
        kCGEventMouseMoved,
        kCGEventKeyDown,
        kCGEventKeyUp,
        kCGMouseButtonLeft,
        kCGMouseButtonRight,
        kCGMouseButtonCenter,
        kCGHIDEventTap,
        kCGSessionEventTap,
        kCGScrollEventUnitLine,
        kCGScrollEventUnitPixel,
        kCGMouseEventClickState,
        kCGWindowListOptionOnScreenOnly,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionIncludingWindow,
        kCGWindowImageBoundsIgnoreFraming,
        kCGWindowImageNominalResolution,
        kCGNullWindowID,
    )
    import ApplicationServices as AXS  # type: ignore
    from ApplicationServices import (  # type: ignore
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        AXUIElementCopyAttributeNames,
        AXUIElementCopyActionNames,
        AXUIElementSetAttributeValue,
        AXUIElementPerformAction,
        AXIsProcessTrusted,
    )
except ImportError as exc:  # pragma: no cover - happens only off-macOS
    sys.stderr.write(
        "[cua-server] Missing macOS PyObjC bindings. "
        "This plugin only runs on macOS. Original error: %s\n" % exc
    )
    raise

from PIL import Image  # noqa: E402

# ---------------------------------------------------------------------------
# MCP SDK
# ---------------------------------------------------------------------------

from mcp.server.fastmcp import FastMCP, Image as MCPImage  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("CUA_LOG_LEVEL", "INFO"),
    format="[cua-server] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("cua-server")

CUA_VERSION = "CUA-Claude 0.1.0"
PLUGIN_ROOT = Path(os.environ.get("CUA_PLUGIN_ROOT", Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Accessibility helpers
# ---------------------------------------------------------------------------

# Attribute names are just CFStrings in AX; using string literals avoids
# pulling in the kAX* constants whose availability varies by PyObjC build.
kAXRole = "AXRole"
kAXRoleDescription = "AXRoleDescription"
kAXSubrole = "AXSubrole"
kAXTitle = "AXTitle"
kAXValue = "AXValue"
kAXDescription = "AXDescription"
kAXHelp = "AXHelp"
kAXIdentifier = "AXIdentifier"
kAXChildren = "AXChildren"
kAXPosition = "AXPosition"
kAXSize = "AXSize"
kAXEnabled = "AXEnabled"
kAXSelected = "AXSelected"
kAXFocused = "AXFocused"
kAXSettable = "AXSettable"
kAXWindows = "AXWindows"
kAXFocusedWindow = "AXFocusedWindow"
kAXMainWindow = "AXMainWindow"
kAXVisibleChildren = "AXVisibleChildren"
kAXPlaceholderValue = "AXPlaceholderValue"
kAXSelectedText = "AXSelectedText"

AX_SUCCESS = 0  # kAXErrorSuccess


def _ax_copy(element, attribute: str) -> Any:
    """Read an accessibility attribute; return ``None`` on any error."""
    try:
        err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    except Exception:
        return None
    if err != AX_SUCCESS:
        return None
    return value


def _ax_attribute_names(element) -> list[str]:
    try:
        err, names = AXUIElementCopyAttributeNames(element, None)
    except Exception:
        return []
    if err != AX_SUCCESS or names is None:
        return []
    return list(names)


def _ax_action_names(element) -> list[str]:
    try:
        err, names = AXUIElementCopyActionNames(element, None)
    except Exception:
        return []
    if err != AX_SUCCESS or names is None:
        return []
    return list(names)


def _ax_cfvalue_to_tuple(cfvalue) -> Optional[tuple[float, float]]:
    """Decode AXValue CGPoint/CGSize to a (x/width, y/height) tuple."""
    if cfvalue is None:
        return None
    # AXValueGetType returns an integer tag. We try CGPoint then CGSize.
    for ax_type in (AXS.kAXValueCGPointType, AXS.kAXValueCGSizeType):
        try:
            ok, val = AXS.AXValueGetValue(cfvalue, ax_type, None)
        except Exception:
            ok, val = False, None
        if ok and val is not None:
            # val is an NSPoint-ish or NSSize-ish struct
            return (float(val.x), float(val.y)) if hasattr(val, "x") else (
                float(val.width),
                float(val.height),
            )
    return None


# ---------------------------------------------------------------------------
# Element snapshot data structure
# ---------------------------------------------------------------------------


@dataclass
class ElementSnapshot:
    index: int
    ax_ref: Any  # AXUIElementRef
    role: str
    subrole: Optional[str]
    role_description: Optional[str]
    title: Optional[str]
    value: Optional[str]
    description: Optional[str]
    help_text: Optional[str]
    identifier: Optional[str]
    position: Optional[tuple[float, float]]
    size: Optional[tuple[float, float]]
    enabled: Optional[bool]
    selected: Optional[bool]
    focused: Optional[bool]
    settable: Optional[bool]
    actions: list[str] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    depth: int = 0


@dataclass
class AppState:
    pid: int
    bundle_id: str
    display_name: str
    window_title: Optional[str]
    captured_at: float
    elements: list[ElementSnapshot]
    screenshot_png: Optional[bytes]
    window_bounds: Optional[tuple[float, float, float, float]]  # x,y,w,h


# pid -> last captured state (single most recent snapshot per app)
_STATE_CACHE: dict[int, AppState] = {}


# Chromium-based browsers expose the webpage DOM through accessibility only
# when an assistive technology requests it. Touching the ``AXManualAccessibility``
# attribute is the canonical opt-in signal (Chromium flips on renderer-side
# accessibility even though the set call itself reports "attribute unsupported").
# Without this, ``get_app_state`` sees only the browser chrome (~48 elements)
# instead of the full webpage (1000+ elements), and webpage clicks can't use
# AXPress.
_CHROMIUM_BUNDLE_IDS: frozenset[str] = frozenset({
    "com.google.Chrome",
    "com.google.Chrome.canary",
    "com.google.Chrome.beta",
    "com.google.Chrome.dev",
    "com.brave.Browser",
    "com.microsoft.edgemac",
    "com.microsoft.edgemac.Beta",
    "com.microsoft.edgemac.Dev",
    "com.microsoft.edgemac.Canary",
    "com.operasoftware.Opera",
    "com.vivaldi.Vivaldi",
    "company.thebrowser.Browser",  # Arc
    "company.thebrowser.dia",
    "com.openai.atlas",  # ChatGPT Atlas browser
})

# Electron apps embed Chromium and therefore also honor ``AXManualAccessibility``
# as the opt-in flag that makes the renderer expose its DOM as an accessibility
# tree. Without it, the tree for a typical Electron app contains only the
# native window chrome (~10 elements) while the real UI -- Slack channels,
# Cursor editor, VSCode files, Notion pages -- is invisible to the assistant.
_ELECTRON_BUNDLE_IDS: frozenset[str] = frozenset({
    "com.tinyspeck.slackmacgap",         # Slack
    "com.microsoft.VSCode",              # VS Code
    "com.microsoft.VSCodeInsiders",      # VS Code Insiders
    "com.todesktop.230313mzl4w4u92",     # Cursor
    "com.openai.codex",                  # Codex
    "com.anthropic.claudefordesktop",    # Claude Desktop
    "com.hnc.Discord",                   # Discord
    "com.hnc.Discord.canary",
    "com.hnc.Discord.ptb",
    "notion.id",                         # Notion
    "com.notion.id",
    "com.notion.desktop",
    "notion.notion",
    "com.linear",                        # Linear
    "com.linearapp.linear",
    "com.figma.Desktop",                 # Figma
    "com.spotify.client",                # Spotify
    "com.electron.dockerdesktop",        # Docker Desktop
    "com.github.GitHubDesktop",          # GitHub Desktop
    "com.postmanlabs.mac",               # Postman
    "com.google.antigravity",            # Antigravity
})

# Pids whose Chromium renderer accessibility we've already turned on. Chromium
# keeps the flag on for the life of the process so we never need to re-enable.
_CHROMIUM_AX_ENABLED: set[int] = set()


def _maybe_enable_chromium_ax(bundle_id: str, ax_app: Any, pid: int) -> bool:
    """If the target is a Chromium-based app, nudge it to expose its DOM.

    This covers both desktop Chromium browsers (Chrome, Arc, Edge, …) and
    Electron apps (Slack, VS Code, Cursor, Notion, Discord, …), which all
    embed Chromium. Different flavours respond to different opt-in signals:

    * Browsers honor ``AXManualAccessibility`` alone.
    * Some Electron apps (notably Slack) need ``AXEnhancedUserInterface``
      to flip their renderer into "VoiceOver-aware" mode; others build
      the tree only after both flags are set.

    We set both. Both calls are documented to return "attribute
    unsupported" while still having the side effect of turning on the
    renderer accessibility path, so the AXError return value is ignored.

    Returns ``True`` if the app was recognized as Chromium/Electron (and
    the flags were set this call or previously), so callers know to
    expect a richer tree after a short delay.
    """
    if bundle_id not in _CHROMIUM_BUNDLE_IDS and bundle_id not in _ELECTRON_BUNDLE_IDS:
        return False
    if pid in _CHROMIUM_AX_ENABLED:
        return True
    for attr in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        try:
            AXUIElementSetAttributeValue(ax_app, attr, True)
        except Exception:  # pragma: no cover - the set call is best-effort
            pass
    _CHROMIUM_AX_ENABLED.add(pid)
    return True


# ---------------------------------------------------------------------------
# App discovery
# ---------------------------------------------------------------------------


def _running_apps() -> list[NSRunningApplication]:
    ws = NSWorkspace.sharedWorkspace()
    # ``NSWorkspace.runningApplications`` is kept up-to-date via NSWorkspace
    # launch/terminate notifications -- but those notifications only arrive
    # if the process is pumping an NSRunLoop in default mode. This server
    # doesn't (it runs an asyncio stdio loop instead), so the returned list
    # is frozen at whichever snapshot NSWorkspace held when it was first
    # touched. Pump the run loop briefly before reading so freshly-launched
    # and freshly-terminated apps are reflected.
    try:
        from Foundation import NSRunLoop, NSDate  # type: ignore
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
    except Exception:
        pass
    return list(ws.runningApplications())


def _resolve_app(app_query: str) -> Optional[NSRunningApplication]:
    """Resolve an app handle by bundle id or localized name (case-insensitive)."""
    q = app_query.strip().lower()
    candidates: list[NSRunningApplication] = []
    for proc in _running_apps():
        bid = (proc.bundleIdentifier() or "").lower()
        name = (proc.localizedName() or "").lower()
        if q == bid or q == name:
            return proc
        if q in bid or q in name:
            candidates.append(proc)
    if len(candidates) == 1:
        return candidates[0]
    # Disambiguate by preferring regular (dock) apps
    regular = [
        c for c in candidates
        if c.activationPolicy() == NSApplicationActivationPolicyRegular
    ]
    if len(regular) == 1:
        return regular[0]
    return None


# ---------------------------------------------------------------------------
# Accessibility tree walker
# ---------------------------------------------------------------------------

# Hard cap to keep payloads bounded.
MAX_ELEMENTS = int(os.environ.get("CUA_MAX_ELEMENTS", "1200"))
MAX_DEPTH = int(os.environ.get("CUA_MAX_DEPTH", "30"))


def _short(text: Any, limit: int = 120) -> Optional[str]:
    if text is None:
        return None
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return None
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if not text:
        return None
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _snapshot_element(element, index: int, depth: int) -> ElementSnapshot:
    role = _short(_ax_copy(element, kAXRole)) or "unknown"
    subrole = _short(_ax_copy(element, kAXSubrole))
    role_desc = _short(_ax_copy(element, kAXRoleDescription))
    title = _short(_ax_copy(element, kAXTitle))
    value_raw = _ax_copy(element, kAXValue)
    value = _short(value_raw, limit=200) if not isinstance(value_raw, (list, tuple)) else None
    description = _short(_ax_copy(element, kAXDescription))
    help_text = _short(_ax_copy(element, kAXHelp))
    identifier = _short(_ax_copy(element, kAXIdentifier))

    position = _ax_cfvalue_to_tuple(_ax_copy(element, kAXPosition))
    size = _ax_cfvalue_to_tuple(_ax_copy(element, kAXSize))

    def _bool(attr):
        v = _ax_copy(element, attr)
        if v is None:
            return None
        try:
            return bool(v)
        except Exception:
            return None

    enabled = _bool(kAXEnabled)
    selected = _bool(kAXSelected)
    focused = _bool(kAXFocused)
    settable = _bool(kAXSettable)

    actions = _ax_action_names(element)

    return ElementSnapshot(
        index=index,
        ax_ref=element,
        role=role,
        subrole=subrole,
        role_description=role_desc,
        title=title,
        value=value,
        description=description,
        help_text=help_text,
        identifier=identifier,
        position=position,
        size=size,
        enabled=enabled,
        selected=selected,
        focused=focused,
        settable=settable,
        actions=actions,
        depth=depth,
    )


def _walk_tree(root_element) -> list[ElementSnapshot]:
    """Breadth-first walk that assigns stable-within-snapshot indexes."""
    elements: list[ElementSnapshot] = []
    queue: list[tuple[Any, int, int]] = [(root_element, 0, -1)]  # (el, depth, parent_idx)

    while queue and len(elements) < MAX_ELEMENTS:
        element, depth, parent_idx = queue.pop(0)
        if depth > MAX_DEPTH:
            continue
        idx = len(elements)
        snap = _snapshot_element(element, idx, depth)
        elements.append(snap)
        if parent_idx >= 0:
            elements[parent_idx].children.append(idx)

        children = _ax_copy(element, kAXChildren) or []
        for child in children:
            if len(elements) + len(queue) >= MAX_ELEMENTS:
                break
            queue.append((child, depth + 1, idx))

    return elements


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------


def _cgwindow_id_for_pid(pid: int, title: Optional[str]) -> Optional[int]:
    """Find a CGWindowID belonging to ``pid`` (prefers matching title)."""
    opts = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    windows = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) or []
    pid_windows = [w for w in windows if int(w.get("kCGWindowOwnerPID", -1)) == pid]
    if not pid_windows:
        return None
    if title:
        for w in pid_windows:
            if (w.get("kCGWindowName") or "") == title:
                return int(w["kCGWindowNumber"])
    # Fallback: largest on-screen window for that pid
    def area(w):
        b = w.get("kCGWindowBounds") or {}
        return float(b.get("Width", 0)) * float(b.get("Height", 0))

    pid_windows.sort(key=area, reverse=True)
    return int(pid_windows[0]["kCGWindowNumber"])


def _cgimage_to_png(cg_image) -> Optional[bytes]:
    if cg_image is None:
        return None
    width = int(CGImageGetWidth(cg_image))
    height = int(CGImageGetHeight(cg_image))
    if width == 0 or height == 0:
        return None
    data_provider = CGImageGetDataProvider(cg_image)
    raw = CGDataProviderCopyData(data_provider)
    bytes_per_row = int(CGImageGetBytesPerRow(cg_image))
    bpp = int(CGImageGetBitsPerPixel(cg_image))
    pil_mode = "RGBA" if bpp == 32 else "RGB"
    try:
        img = Image.frombuffer(
            pil_mode,
            (width, height),
            bytes(raw),
            "raw",
            "BGRA" if pil_mode == "RGBA" else "BGR",
            bytes_per_row,
            1,
        )
    except Exception as exc:
        log.warning("screenshot decode failed: %s", exc)
        return None
    if pil_mode == "RGBA":
        img = img.convert("RGB")
    # Downscale huge retina captures to keep MCP payloads sane.
    max_edge = int(os.environ.get("CUA_SCREENSHOT_MAX_EDGE", "1600"))
    if max(img.size) > max_edge:
        ratio = max_edge / max(img.size)
        new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
        img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _screenshot_window(pid: int, title: Optional[str]) -> tuple[Optional[bytes], Optional[tuple[float, float, float, float]]]:
    window_id = _cgwindow_id_for_pid(pid, title)
    bounds: Optional[tuple[float, float, float, float]] = None
    if window_id is None:
        return None, None
    # Find its bounds for reference
    opts = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    windows = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) or []
    for w in windows:
        if int(w.get("kCGWindowNumber", -1)) == window_id:
            b = w.get("kCGWindowBounds") or {}
            bounds = (
                float(b.get("X", 0)),
                float(b.get("Y", 0)),
                float(b.get("Width", 0)),
                float(b.get("Height", 0)),
            )
            break
    try:
        cg_image = CGWindowListCreateImage(
            Quartz.CGRectNull,
            kCGWindowListOptionIncludingWindow,
            window_id,
            kCGWindowImageBoundsIgnoreFraming | kCGWindowImageNominalResolution,
        )
    except Exception as exc:
        log.warning("CGWindowListCreateImage failed: %s", exc)
        return None, bounds
    return _cgimage_to_png(cg_image), bounds


# ---------------------------------------------------------------------------
# State rendering
# ---------------------------------------------------------------------------


def _render_state(state: AppState) -> str:
    lines: list[str] = []
    lines.append(f"Computer Use state ({CUA_VERSION})")
    lines.append("<app_state>")
    lines.append(f"App={state.bundle_id} (pid {state.pid})")
    lines.append(
        f'Window: "{state.window_title or "(unknown)"}", App: {state.display_name}.'
    )
    if state.window_bounds is not None:
        x, y, w, h = state.window_bounds
        lines.append(f"Window bounds: x={x:.0f} y={y:.0f} w={w:.0f} h={h:.0f}")
    lines.append(f"Elements: {len(state.elements)} (max {MAX_ELEMENTS})")
    lines.append("")
    for el in state.elements:
        indent = "    " * min(el.depth, 20)
        parts: list[str] = [str(el.index)]
        role_label = el.role_description or el.role
        parts.append(role_label)
        if el.subrole and el.subrole not in role_label:
            parts.append(f"({el.subrole})")
        if el.title:
            parts.append(f'"{el.title}"')
        if el.value:
            parts.append(f"Value: {el.value!r}")
        if el.description and el.description != el.title:
            parts.append(f"Desc: {el.description!r}")
        if el.identifier:
            parts.append(f"ID: {el.identifier}")
        if el.position is not None and el.size is not None:
            px, py = el.position
            sw, sh = el.size
            if sw > 0 and sh > 0:
                parts.append(
                    f"at=({px:.0f},{py:.0f}) size=({sw:.0f}x{sh:.0f})"
                )
        flags: list[str] = []
        if el.selected:
            flags.append("selected")
        if el.focused:
            flags.append("focused")
        if el.enabled is False:
            flags.append("disabled")
        if el.settable:
            flags.append("settable")
        if flags:
            parts.append("[" + ",".join(flags) + "]")
        extra_actions = [
            a for a in el.actions
            if a not in ("AXPress", "AXShowDefaultUI", "AXScrollToVisible")
        ]
        if extra_actions:
            parts.append("Actions: " + ",".join(a.replace("AX", "") for a in extra_actions))
        lines.append(indent + " ".join(parts))
    lines.append("</app_state>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Event posting helpers
# ---------------------------------------------------------------------------


def _post_event(event, pid: Optional[int]) -> None:
    """Post an event preferring PID-scoped delivery when available."""
    if pid is not None:
        try:
            CGEventPostToPid(int(pid), event)
            return
        except Exception as exc:  # pragma: no cover
            log.warning("CGEventPostToPid failed (%s); falling back to global post", exc)
    CGEventPost(kCGHIDEventTap, event)


_GHOST_SCRIPT = Path(__file__).with_name("cursor_ghost.py")
_OVERLAY_DISABLED = os.environ.get("CUA_CLICK_OVERLAY", "1") in {"0", "false", "False", "no"}

# Lazily-spawned persistent ghost-cursor daemon. It reads commands from its
# stdin and self-exits a few seconds after the last one -- so as long as the
# agent keeps making tool calls the ghost stays alive, and it disappears
# once the agent is idle. Guarded by a lock because FastMCP may dispatch
# tool calls from multiple threads.
_GHOST_LOCK = threading.Lock()
_GHOST_PROC: Optional[subprocess.Popen] = None


def _ghost_alive(proc: Optional[subprocess.Popen]) -> bool:
    return proc is not None and proc.poll() is None and proc.stdin is not None


def _spawn_ghost() -> Optional[subprocess.Popen]:
    if not _GHOST_SCRIPT.exists():
        return None
    try:
        stderr_target = subprocess.DEVNULL
        stderr_path = os.environ.get("CUA_GHOST_STDERR")
        if stderr_path:
            try:
                stderr_target = open(stderr_path, "ab", buffering=0)
            except Exception:
                stderr_target = subprocess.DEVNULL
        proc = subprocess.Popen(
            [sys.executable, str(_GHOST_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr_target,
            close_fds=True,
            start_new_session=True,
        )
        return proc
    except Exception as exc:  # pragma: no cover
        log.debug("ghost daemon failed to launch: %s", exc)
        return None


def _send_ghost(command: str) -> None:
    """Send a single-line command to the ghost daemon, respawning if needed."""
    if _OVERLAY_DISABLED:
        return
    global _GHOST_PROC
    with _GHOST_LOCK:
        if not _ghost_alive(_GHOST_PROC):
            _GHOST_PROC = _spawn_ghost()
            if _GHOST_PROC is None:
                return
        assert _GHOST_PROC is not None and _GHOST_PROC.stdin is not None
        try:
            _GHOST_PROC.stdin.write((command.rstrip("\n") + "\n").encode())
            _GHOST_PROC.stdin.flush()
        except (BrokenPipeError, OSError):
            # Ghost died between our check and the write; respawn and retry
            # once.
            _GHOST_PROC = _spawn_ghost()
            if _GHOST_PROC is None or _GHOST_PROC.stdin is None:
                return
            try:
                _GHOST_PROC.stdin.write((command.rstrip("\n") + "\n").encode())
                _GHOST_PROC.stdin.flush()
            except Exception:  # pragma: no cover
                pass


def _show_click_overlay(
    x: float,
    y: float,
    target_pid: Optional[int] = None,
) -> None:
    """Animate the ghost cursor to ``(x, y)`` and flash a ring on landing.

    Talks to the persistent cursor-ghost daemon so the ghost moves smoothly
    between consecutive clicks and disappears when the agent goes idle.

    If ``target_pid`` is given, we look up the app's CGWindowID and pass it
    along so the ghost and ring are ordered just above that window, sharing
    its occlusion with other apps.
    """
    if _OVERLAY_DISABLED:
        return
    target_window_id: Optional[int] = None
    if target_pid is not None:
        state = _STATE_CACHE.get(int(target_pid))
        title = state.window_title if state is not None else None
        try:
            target_window_id = _cgwindow_id_for_pid(int(target_pid), title)
        except Exception:
            target_window_id = None
    twid = str(int(target_window_id)) if target_window_id else "0"
    _send_ghost(f"click_at {x:.1f} {y:.1f} 480 {twid}")


def _ping_ghost() -> None:
    """Keep the ghost alive during non-click tool activity so it doesn't
    fade away while the agent is still actively using other tools."""
    if _OVERLAY_DISABLED:
        return
    _send_ghost("ping")


_BROWSER_JS_DIALECTS: dict[str, str] = {
    # Bundle ID -> AppleScript that evaluates ``JS`` in the foreground tab.
    # The chosen syntax must return a value so we can detect "JS not
    # allowed" errors from the corresponding app.
    "com.google.Chrome": (
        'tell application "Google Chrome" to '
        'execute active tab of front window javascript JS'
    ),
    "com.google.Chrome.canary": (
        'tell application "Google Chrome Canary" to '
        'execute active tab of front window javascript JS'
    ),
    "com.google.Chrome.beta": (
        'tell application "Google Chrome Beta" to '
        'execute active tab of front window javascript JS'
    ),
    "com.google.Chrome.dev": (
        'tell application "Google Chrome Dev" to '
        'execute active tab of front window javascript JS'
    ),
    "com.brave.Browser": (
        'tell application "Brave Browser" to '
        'execute active tab of front window javascript JS'
    ),
    "com.microsoft.edgemac": (
        'tell application "Microsoft Edge" to '
        'execute active tab of front window javascript JS'
    ),
    # Safari needs "Allow JavaScript from Apple Events" in its Develop menu.
    # We still support it as a last-resort fallback, and surface the error
    # clearly when it isn't enabled.
    "com.apple.Safari": (
        'tell application "Safari" to '
        'do JavaScript JS in current tab of front window'
    ),
}


def _applescript_run_js(bundle_id: str, js: str, timeout: float = 4.0) -> tuple[bool, str]:
    """Run ``js`` in the active tab of ``bundle_id``'s front window via AppleScript.

    Works for Chromium-family browsers (Chrome/Brave/Edge) without any
    special toggles. Works for Safari only if the user has "Allow
    JavaScript from Apple Events" enabled in the Develop menu.

    Returns ``(ok, message)``. ``message`` is the AppleScript result (the
    JS expression's return value as a string) on success, or a human-
    readable error on failure. This runs **in the background** -- the
    target app does not activate and the user's cursor doesn't move.
    """
    template = _BROWSER_JS_DIALECTS.get(bundle_id)
    if template is None:
        return False, f"no AppleScript JS dialect registered for {bundle_id!r}"
    # AppleScript string escape: backslashes and double-quotes only.
    escaped = js.replace("\\", "\\\\").replace('"', '\\"')
    script = template.replace("JS", f'"{escaped}"')
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "osascript not available"
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"
    except Exception as exc:  # pragma: no cover
        return False, f"osascript invocation failed: {exc}"
    if result.returncode != 0:
        err = (result.stderr or "").strip().replace("\n", " ")
        return False, f"exit {result.returncode}: {err or '(no stderr)'}"
    return True, (result.stdout or "").strip()


def _applescript_click(process_name: str, x: float, y: float) -> tuple[bool, str]:
    """Dispatch a left click at (x, y) via ``System Events`` in ``process_name``.

    AppleScript's ``click at {x, y}`` hit-tests **by screen coordinates against
    the topmost window at that pixel**. That means:

    * If ``process_name``'s window is the frontmost thing at (x, y), the click
      lands on its real UI element and the command returns that element's
      accessibility path -- success.
    * If ``process_name`` has no window available at all, the call errors with
      ``-10005 noWindowsAvailable`` -- same behavior as Codex's computer-use.
    * If ``process_name`` has windows but they're occluded at (x, y) by another
      app, System Events silently falls back to returning
      ``"menu bar 1 of application process <name>"`` and the click has no real
      effect. We detect this fallback and report it as a miss so the caller
      can fall back to the CGEvent path.

    Returns ``(ok, message)``. ``message`` is the AppleScript-reported element
    path on success or an error/sentinel string on failure.
    """
    if not process_name:
        return False, "no process name"
    # AppleScript string escape: backslashes and double-quotes only.
    escaped = process_name.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'tell application "System Events" to tell process "{escaped}" '
        f"to click at {{{int(round(x))}, {int(round(y))}}}"
    )
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return False, "osascript not available"
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"osascript invocation failed: {exc}"
    if result.returncode != 0:
        err = (result.stderr or "").strip().replace("\n", " ")
        return False, f"exit {result.returncode}: {err or '(no stderr)'}"
    hit = (result.stdout or "").strip()
    # Hit-test miss sentinel: when the target app is occluded at (x, y),
    # System Events returns the process's menu bar as the "clicked" element.
    # (A legit menu bar click is very rare and explicit; we'd rather treat
    # this as a miss and let CGEvent fallback try.)
    if hit.startswith("menu bar 1 of application process"):
        return False, f"hit fallback: {hit}"
    return True, hit or "dispatched"


_MOUSE_BUTTON_DOWN = {
    "left": kCGEventLeftMouseDown,
    "right": kCGEventRightMouseDown,
    "middle": kCGEventOtherMouseDown,
}
_MOUSE_BUTTON_UP = {
    "left": kCGEventLeftMouseUp,
    "right": kCGEventRightMouseUp,
    "middle": kCGEventOtherMouseUp,
}
_MOUSE_BUTTON_CG = {
    "left": kCGMouseButtonLeft,
    "right": kCGMouseButtonRight,
    "middle": kCGMouseButtonCenter,
}


def _post_click(pid: Optional[int], x: float, y: float, button: str, click_count: int) -> None:
    btn_down = _MOUSE_BUTTON_DOWN[button]
    btn_up = _MOUSE_BUTTON_UP[button]
    cg_btn = _MOUSE_BUTTON_CG[button]
    for i in range(click_count):
        click_state = i + 1  # 1 = single, 2 = double, 3 = triple
        down = CGEventCreateMouseEvent(None, btn_down, (x, y), cg_btn)
        up = CGEventCreateMouseEvent(None, btn_up, (x, y), cg_btn)
        try:
            CGEventSetIntegerValueField(down, kCGMouseEventClickState, click_state)
            CGEventSetIntegerValueField(up, kCGMouseEventClickState, click_state)
        except Exception:
            pass
        _post_event(down, pid)
        _post_event(up, pid)
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Keycode mapping for press_key (xdotool-ish names)
# ---------------------------------------------------------------------------

# Partial mapping of xdotool-style key names to macOS virtual key codes.
# Source: Carbon HIToolbox Events.h. Only the names the model is most
# likely to emit; unknown names fall through to `type_text` behaviour.
_KEYCODES: dict[str, int] = {
    # Letters
    **{chr(c): kc for c, kc in zip(
        range(ord("a"), ord("z") + 1),
        [0x00, 0x0B, 0x08, 0x02, 0x0E, 0x03, 0x05, 0x04, 0x22, 0x26, 0x28, 0x25,
         0x2E, 0x2D, 0x1F, 0x23, 0x0C, 0x0F, 0x01, 0x11, 0x20, 0x09, 0x0D, 0x07,
         0x10, 0x06]
    )},
    # Digits
    "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15,
    "5": 0x17, "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
    # Named keys (xdotool flavored)
    "return": 0x24, "enter": 0x24,
    "tab": 0x30,
    "space": 0x31,
    "backspace": 0x33, "delete": 0x33, "bksp": 0x33,
    "escape": 0x35, "esc": 0x35,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
    "capslock": 0x39,
    "insert": 0x72, "forwarddelete": 0x75,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60,
    "f6": 0x61, "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D,
    "f11": 0x67, "f12": 0x6F,
    "minus": 0x1B, "equal": 0x18, "semicolon": 0x29, "apostrophe": 0x27,
    "comma": 0x2B, "period": 0x2F, "slash": 0x2C, "backslash": 0x2A,
    "bracketleft": 0x21, "bracketright": 0x1E, "grave": 0x32,
    # Keypad
    "kp_0": 0x52, "kp_1": 0x53, "kp_2": 0x54, "kp_3": 0x55, "kp_4": 0x56,
    "kp_5": 0x57, "kp_6": 0x58, "kp_7": 0x59, "kp_8": 0x5B, "kp_9": 0x5C,
    "kp_decimal": 0x41, "kp_enter": 0x4C, "kp_add": 0x45, "kp_subtract": 0x4E,
    "kp_multiply": 0x43, "kp_divide": 0x4B,
}

# macOS modifier masks (CGEventFlags)
_MOD_MASK = {
    "shift": 0x00020000,   # kCGEventFlagMaskShift
    "control": 0x00040000, # kCGEventFlagMaskControl
    "ctrl": 0x00040000,
    "alt": 0x00080000,     # kCGEventFlagMaskAlternate (option)
    "option": 0x00080000,
    "opt": 0x00080000,
    "command": 0x00100000, # kCGEventFlagMaskCommand
    "cmd": 0x00100000,
    "super": 0x00100000,   # xdotool uses super for cmd on macOS
    "meta": 0x00100000,
    "fn": 0x00800000,      # kCGEventFlagMaskSecondaryFn
}


def _parse_key(key_combo: str) -> tuple[int, int]:
    """Parse ``super+shift+t`` -> (flags_mask, keycode).

    Key names are matched case-insensitively and with underscores stripped,
    so xdotool-style names like ``Page_Down``, ``Caps_Lock``, ``BackSpace``,
    or ``Num_Lock`` resolve the same as ``pagedown``/``capslock`` etc. This
    matches how humans and the skill docs tend to write these names.
    """
    parts = [p.strip() for p in key_combo.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty key combo: {key_combo!r}")
    flags = 0
    for mod in parts[:-1]:
        mask = _MOD_MASK.get(mod.lower().replace("_", ""))
        if mask is None:
            raise ValueError(f"unknown modifier: {mod!r}")
        flags |= mask
    key_name = parts[-1]
    # Normalize: lowercase + drop underscores so ``Page_Down`` -> ``pagedown``.
    lookup = key_name.lower().replace("_", "")
    code = _KEYCODES.get(lookup)
    if code is None:
        # Keypad names are stored as ``kp_<x>`` in the table; re-add the
        # underscore after ``kp`` if the caller wrote e.g. ``KP0``.
        if lookup.startswith("kp") and len(lookup) > 2:
            code = _KEYCODES.get("kp_" + lookup[2:])
    if code is None:
        raise ValueError(f"unknown key: {key_name!r}")
    return flags, code


def _post_key(pid: Optional[int], key_combo: str) -> None:
    flags, code = _parse_key(key_combo)
    down = CGEventCreateKeyboardEvent(None, code, True)
    up = CGEventCreateKeyboardEvent(None, code, False)
    if flags:
        CGEventSetFlags(down, flags)
        CGEventSetFlags(up, flags)
    _post_event(down, pid)
    _post_event(up, pid)


def _post_text(pid: Optional[int], text: str) -> None:
    # Chunk to avoid hitting the Unicode string buffer limit (~20 UTF-16 units
    # per event on older macOS).
    CHUNK = 18
    for i in range(0, len(text), CHUNK):
        chunk = text[i : i + CHUNK]
        down = CGEventCreateKeyboardEvent(None, 0, True)
        up = CGEventCreateKeyboardEvent(None, 0, False)
        CGEventKeyboardSetUnicodeString(down, len(chunk), chunk)
        CGEventKeyboardSetUnicodeString(up, len(chunk), chunk)
        _post_event(down, pid)
        _post_event(up, pid)
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# State lookups used by action tools
# ---------------------------------------------------------------------------


class ToolError(RuntimeError):
    """Raised when a tool call has a user-visible problem."""


def _require_state(pid: int) -> AppState:
    state = _STATE_CACHE.get(pid)
    if state is None:
        raise ToolError(
            "No captured app state for this app. Call get_app_state(app) first."
        )
    return state


def _require_element(pid: int, index: int) -> ElementSnapshot:
    state = _require_state(pid)
    if index < 0 or index >= len(state.elements):
        raise ToolError(
            f"element_index {index} is out of range (0..{len(state.elements) - 1})"
        )
    return state.elements[index]


def _element_center(element: ElementSnapshot) -> Optional[tuple[float, float]]:
    if element.position is None or element.size is None:
        return None
    x, y = element.position
    w, h = element.size
    return (x + w / 2, y + h / 2)


# ---------------------------------------------------------------------------
# Permissions check
# ---------------------------------------------------------------------------


def _check_ax_permissions() -> None:
    if not AXIsProcessTrusted():
        log.warning(
            "Accessibility permission NOT granted. Grant it in "
            "System Settings > Privacy & Security > Accessibility for the process "
            "running this server (usually your terminal or Claude Code)."
        )


# ---------------------------------------------------------------------------
# FastMCP server + tool definitions
# ---------------------------------------------------------------------------

mcp = FastMCP("background-computer-use")


@mcp.tool()
def list_apps() -> str:
    """List running apps on this Mac.

    Returns one app per line in the form
    ``"<Name> — <bundle-id> [active, running, launched=YYYY-MM-DD]"`` so the
    model can pick an app for subsequent tool calls. The ``app`` argument on
    every other tool accepts either the display name or the bundle identifier.
    """
    _check_ax_permissions()
    lines: list[str] = []
    for proc in _running_apps():
        name = proc.localizedName() or "?"
        bid = proc.bundleIdentifier() or "?"
        tags: list[str] = []
        if proc.isActive():
            tags.append("active")
        tags.append("running")
        if proc.isHidden():
            tags.append("hidden")
        pol = proc.activationPolicy()
        if pol != NSApplicationActivationPolicyRegular:
            tags.append("background")
        launch = proc.launchDate()
        if launch is not None:
            tags.append(f"launched={launch.description()[:10]}")
        lines.append(f"{name} — {bid} [{', '.join(tags)}]")
    lines.sort()
    header = f"Running apps ({len(lines)}):"
    return header + "\n" + "\n".join(lines)


@mcp.tool(structured_output=False)
def get_app_state(app: str) -> list[Any]:
    """Start/refresh an app-use session and return the key window's accessibility
    tree together with a screenshot.

    Call this once per turn before any interaction with ``app``. Element
    indexes returned here are valid only until the next ``get_app_state`` call.

    Args:
        app: App display name or bundle identifier.
    """
    _check_ax_permissions()
    proc = _resolve_app(app)
    if proc is None:
        raise ToolError(
            f"Could not find a running app matching {app!r}. "
            "Call list_apps() to see options."
        )
    pid = int(proc.processIdentifier())
    bundle_id = proc.bundleIdentifier() or ""
    display_name = proc.localizedName() or bundle_id or "?"

    ax_app = AXUIElementCreateApplication(pid)

    is_chromium_like = _maybe_enable_chromium_ax(bundle_id, ax_app, pid)

    focused = _ax_copy(ax_app, kAXFocusedWindow)
    root = focused
    window_title: Optional[str] = None
    if root is None:
        windows = _ax_copy(ax_app, kAXWindows) or []
        if windows:
            root = windows[0]
    if root is not None:
        window_title = _short(_ax_copy(root, kAXTitle))
    else:
        # As a last resort, walk the whole application element tree so the
        # model still gets menu bar / app-level controls.
        root = ax_app

    elements = _walk_tree(root)

    # Chromium/Electron renderers build the AX tree lazily after the
    # opt-in flags flip. If this is the first get_app_state call for a
    # Chromium/Electron pid and the tree came back near-empty, wait a
    # short moment for the renderer to publish and retry the walk. One
    # retry is enough in practice; we bound the extra wait to ~1s.
    if is_chromium_like and len(elements) < 40:
        for extra_wait_ms in (150, 400, 600):
            time.sleep(extra_wait_ms / 1000.0)
            # Re-fetch the focused window in case the renderer swapped
            # contents behind our back.
            focused = _ax_copy(ax_app, kAXFocusedWindow)
            new_root = focused
            if new_root is None:
                windows = _ax_copy(ax_app, kAXWindows) or []
                if windows:
                    new_root = windows[0]
            if new_root is None:
                new_root = ax_app
            retry_elements = _walk_tree(new_root)
            if len(retry_elements) > len(elements):
                elements = retry_elements
                root = new_root
                window_title = _short(_ax_copy(new_root, kAXTitle))
            if len(elements) >= 40:
                break

    screenshot, bounds = _screenshot_window(pid, window_title)

    state = AppState(
        pid=pid,
        bundle_id=bundle_id,
        display_name=display_name,
        window_title=window_title,
        captured_at=time.time(),
        elements=elements,
        screenshot_png=screenshot,
        window_bounds=bounds,
    )
    _STATE_CACHE[pid] = state

    rendered = _render_state(state)
    # Include per-app instructions if bundled.
    hints = _load_app_hints(bundle_id, display_name)
    if hints:
        rendered += "\n\n<app_hints>\n" + hints + "\n</app_hints>"

    output: list[Any] = [rendered]
    if screenshot:
        output.append(MCPImage(data=screenshot, format="png"))
    return output


# Bundle-id → canonical hint file basename. Files live in
# ``server/app-hints/<Name>.md`` and are shipped alongside the MCP server
# implementation (they are server-private data, not standard plugin
# components). Missing files are silently ignored.
_HINT_ALIASES: dict[str, str] = {
    "com.apple.clock": "Clock",
    "com.apple.iwork.numbers": "Numbers",
    "com.apple.music": "AppleMusic",
    "com.apple.itunes": "AppleMusic",
    "com.apple.safari": "Safari",
    "com.google.chrome": "Chrome",
    "com.google.chrome.canary": "Chrome",
    "com.google.chrome.beta": "Chrome",
    "com.google.chrome.dev": "Chrome",
    "com.spotify.client": "Spotify",
    "notion.id": "Notion",
    "com.notion.id": "Notion",
    "com.notion.desktop": "Notion",
    "notion.notion": "Notion",
}

# Candidate directories for app-hint files, searched in order. The first
# one is the canonical home; the second is kept as a legacy fallback for
# anyone pointing an older checkout at the server.
_APP_HINTS_DIRS: tuple[Path, ...] = (
    Path(__file__).parent / "app-hints",
    PLUGIN_ROOT / "app-instructions",
)


def _load_app_hints(bundle_id: str, display_name: str) -> Optional[str]:
    candidates: list[str] = []
    alias = _HINT_ALIASES.get((bundle_id or "").lower())
    if alias:
        candidates.append(alias)
    for v in (bundle_id, display_name, display_name.replace(" ", "")):
        if v:
            candidates.append(v)
    for directory in _APP_HINTS_DIRS:
        for cand in candidates:
            path = directory / f"{cand}.md"
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8")
                except Exception:
                    return None
    return None


def _resolve_target_pid(app: str) -> int:
    return _resolve_target(app)[0]


def _resolve_target(app: str) -> tuple[int, str]:
    """Return (pid, process_name) for the target app.

    ``process_name`` is the NSRunningApplication.localizedName, which is also
    what ``System Events`` uses to address the process from AppleScript.
    """
    proc = _resolve_app(app)
    if proc is None:
        raise ToolError(
            f"Could not find a running app matching {app!r}. "
            "Call list_apps() then get_app_state(app) first."
        )
    name = proc.localizedName() or proc.bundleIdentifier() or ""
    return int(proc.processIdentifier()), str(name)


@mcp.tool()
def click(
    app: str,
    element_index: Optional[int] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    click_count: int = 1,
    mouse_button: str = "left",
) -> str:
    """Click an element or a raw screen coordinate.

    Prefer ``element_index`` (from the most recent ``get_app_state``) when the
    target is represented in the accessibility tree. Fall back to ``x``/``y``
    (screen points, top-left origin) when accessibility is insufficient.

    Args:
        app: App display name or bundle identifier.
        element_index: Index from the last ``get_app_state`` for this app.
        x: Raw screen x coordinate (points). Required if ``element_index`` is not given.
        y: Raw screen y coordinate (points). Required if ``element_index`` is not given.
        click_count: 1 = single, 2 = double, 3 = triple.
        mouse_button: ``"left"``, ``"right"``, or ``"middle"``.
    """
    if mouse_button not in _MOUSE_BUTTON_DOWN:
        raise ToolError(f"mouse_button must be one of left/right/middle, got {mouse_button!r}")
    if click_count < 1 or click_count > 3:
        raise ToolError("click_count must be 1, 2, or 3")

    pid, process_name = _resolve_target(app)

    target_xy: Optional[tuple[float, float]] = None
    if element_index is not None:
        el = _require_element(pid, element_index)
        # Precompute the element's center so we can show the click overlay
        # even when AXPress takes the fast path (which doesn't use coords).
        element_center = _element_center(el)
        # Fast path for left single clicks: AXPress without moving the mouse.
        if (
            click_count == 1
            and mouse_button == "left"
            and "AXPress" in el.actions
        ):
            err = AXUIElementPerformAction(el.ax_ref, "AXPress")
            if err == AX_SUCCESS:
                if element_center is not None:
                    _show_click_overlay(*element_center, target_pid=pid)
                return (
                    f"AXPress on element {element_index} ({el.role}"
                    f"{(' ' + repr(el.title)) if el.title else ''}) in pid {pid}"
                )
            log.info("AXPress failed (%s); falling back to coordinate click", err)
        target_xy = element_center
        if target_xy is None:
            raise ToolError(
                f"element {element_index} has no position/size; "
                "pass explicit x,y to click it."
            )
    else:
        if x is None or y is None:
            raise ToolError("either element_index or both x and y are required")
        target_xy = (float(x), float(y))

    # For left single clicks, try AppleScript (System Events) first -- it
    # delivers real clicks to apps via AX when the target's window is the
    # topmost thing at (x, y). When the window is occluded or absent, the
    # call either errors with ``-10005 noWindowsAvailable`` or returns the
    # "menu bar 1" hit-test fallback; either way we drop down to the CGEvent
    # path below (which is at least no worse).
    if click_count == 1 and mouse_button == "left":
        ok, info = _applescript_click(process_name, target_xy[0], target_xy[1])
        if ok:
            _show_click_overlay(target_xy[0], target_xy[1], target_pid=pid)
            return (
                f"left click x1 at ({target_xy[0]:.0f},{target_xy[1]:.0f}) "
                f"via AppleScript on {process_name!r} (pid {pid}) -> {info}"
            )
        log.info("AppleScript click did not land (%s); falling back to CGEvent post", info)

    _post_click(pid, target_xy[0], target_xy[1], mouse_button, click_count)
    _show_click_overlay(target_xy[0], target_xy[1], target_pid=pid)
    return (
        f"{mouse_button} click x{click_count} at ({target_xy[0]:.0f},{target_xy[1]:.0f}) "
        f"posted to pid {pid}"
    )


@mcp.tool()
def drag(app: str, from_x: float, from_y: float, to_x: float, to_y: float) -> str:
    """Drag the left mouse button from one coordinate to another within ``app``.

    Coordinates are screen points, top-left origin.
    """
    pid = _resolve_target_pid(app)
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (from_x, from_y), kCGMouseButtonLeft)
    _post_event(down, pid)
    # Smooth drag: ~20 steps.
    steps = 20
    for i in range(1, steps + 1):
        t = i / steps
        cx = from_x + (to_x - from_x) * t
        cy = from_y + (to_y - from_y) * t
        mv = CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, (cx, cy), kCGMouseButtonLeft)
        _post_event(mv, pid)
        time.sleep(0.01)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (to_x, to_y), kCGMouseButtonLeft)
    _post_event(up, pid)
    return f"drag ({from_x:.0f},{from_y:.0f}) -> ({to_x:.0f},{to_y:.0f}) posted to pid {pid}"


# Accessibility actions whose implementation typically requires the target
# app to be frontmost (to present a menu or modal surface). Calling them
# via AX silently brings the app to the foreground, which violates the
# background-use contract. We refuse these by default and require an
# explicit ``allow_foreground_activation=True`` opt-in.
_FOREGROUND_STEALING_ACTIONS: frozenset[str] = frozenset({
    "AXShowMenu",       # right-click / context-menu equivalent
    "AXShowAlternateUI",  # some apps pop overlay UI that needs front focus
    "AXRaise",           # explicitly requests front state (honest about it)
})


@mcp.tool()
def perform_secondary_action(
    app: str,
    element_index: int,
    action: str,
    allow_foreground_activation: bool = False,
) -> str:
    """Invoke a named accessibility action on an element.

    Action names come from the ``Secondary Actions`` shown in
    ``get_app_state`` (e.g. ``"Raise"``, ``"open"``, ``"Increment"``,
    ``"ScrollToVisible"``, ``"Pick"``). The server adds the standard
    ``AX`` prefix if the caller omits it.

    **Background-safety gate**: a small number of actions (``AXShowMenu``,
    ``AXShowAlternateUI``, ``AXRaise``) virtually always require the
    target app to take key-window focus, because they need to present a
    visible menu or modal surface. By default we refuse these and
    return a ``ToolError`` explaining the trade-off. Pass
    ``allow_foreground_activation=True`` to override when you've
    confirmed that user-visible activation of the target app is
    acceptable for this turn.

    All other actions (``Increment``, ``Decrement``, ``Pick``,
    ``ScrollToVisible``, ``Confirm``, etc.) run through without the gate.
    """
    pid = _resolve_target_pid(app)
    el = _require_element(pid, element_index)
    ax_action = action if action.startswith("AX") else "AX" + action[:1].upper() + action[1:]
    if ax_action not in el.actions and action not in el.actions:
        raise ToolError(
            f"element {element_index} does not advertise action {action!r}. "
            f"Available: {', '.join(el.actions) or '(none)'}"
        )
    if ax_action in _FOREGROUND_STEALING_ACTIONS and not allow_foreground_activation:
        raise ToolError(
            f"action {ax_action!r} on element {element_index} would almost "
            "certainly bring the target app to the foreground (it needs to "
            "present a visible menu or modal). Refusing to run it in "
            "background mode. If you really want this, pass "
            "allow_foreground_activation=True -- this will violate the "
            "background-use contract and steal key focus."
        )
    resolved = ax_action if ax_action in el.actions else action
    err = AXUIElementPerformAction(el.ax_ref, resolved)
    if err != AX_SUCCESS:
        raise ToolError(f"AXUIElementPerformAction({resolved}) failed with AXError {err}")
    note = ""
    if ax_action in _FOREGROUND_STEALING_ACTIONS:
        note = " (foreground activation allowed by caller)"
    return f"performed {resolved} on element {element_index} in pid {pid}{note}"


@mcp.tool()
def press_key(
    app: str,
    key: str,
    element_index: Optional[int] = None,
) -> str:
    """Send a key or key combination to ``app`` (xdotool-style syntax).

    Examples: ``"a"``, ``"Return"``, ``"Tab"``, ``"super+c"``, ``"Up"``, ``"KP_0"``,
    ``"shift+command+t"``.

    Key events are delivered to whichever view is first-responder in the
    target app. If ``element_index`` is supplied, that element is focused
    via ``AXFocused=True`` before the key is posted -- this is the reliable
    way to route keystrokes to a specific text field without raising the
    app's window to key state.
    """
    pid = _resolve_target_pid(app)
    focused_note = ""
    if element_index is not None:
        el = _require_element(pid, element_index)
        ferr = AXUIElementSetAttributeValue(el.ax_ref, kAXFocused, True)
        if ferr != AX_SUCCESS:
            log.info(
                "AXFocused on element %s failed (%s); sending key anyway",
                element_index, ferr,
            )
        else:
            focused_note = f" (focused element {element_index})"
    try:
        _post_key(pid, key)
    except ValueError as exc:
        raise ToolError(str(exc))
    return f"pressed {key} -> pid {pid}{focused_note}"


_AX_SCROLL_ACTIONS = {
    "up":    "AXScrollUpByPage",
    "down":  "AXScrollDownByPage",
    "left":  "AXScrollLeftByPage",
    "right": "AXScrollRightByPage",
}

# Bundle IDs whose on-screen windows own the scrollable content inside a
# host app. If the host app's scroll area geometrically contains one of
# these windows, we target wheel events at the child process's pid -- the
# host-app pid itself will discard them because the first responder in
# the backing view lives in the child process.
_WEBVIEW_CHILD_BUNDLES: frozenset[str] = frozenset({
    "com.apple.WebKit.WebContent",   # Safari, Atlas, Raycast, etc.
    "com.google.Chrome.helper",      # Chrome
    "com.google.Chrome.helper.Renderer",
    "com.microsoft.edgemac.helper",  # Edge
    "com.brave.Browser.helper",      # Brave
})

_WEBVIEW_CHILD_OWNER_NAMES: frozenset[str] = frozenset({
    "Safari Web Content",
    "Google Chrome Helper (Renderer)",
    "Google Chrome Helper",
    "Microsoft Edge Helper (Renderer)",
    "Brave Browser Helper (Renderer)",
})


def _web_content_pid_for_rect(rect: tuple[float, float, float, float]) -> Optional[int]:
    """Find a WebKit content-process pid whose window overlaps ``rect``.

    ``rect`` is (x, y, w, h) in screen points (top-left origin). Returns
    ``None`` if no candidate window is visible.
    """
    try:
        opts = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
        windows = CGWindowListCopyWindowInfo(opts, 0) or []
    except Exception:
        return None
    rx, ry, rw, rh = rect
    rcx, rcy = rx + rw / 2, ry + rh / 2
    best: Optional[tuple[int, float]] = None
    for w in windows:
        pid = int(w.get("kCGWindowOwnerPID", -1))
        if pid <= 0:
            continue
        # Match either by owner name or by bundle id of the running app.
        owner = str(w.get("kCGWindowOwnerName") or "")
        if owner not in _WEBVIEW_CHILD_OWNER_NAMES:
            proc = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if proc is None or str(proc.bundleIdentifier() or "") not in _WEBVIEW_CHILD_BUNDLES:
                continue
        b = w.get("kCGWindowBounds") or {}
        try:
            wx = float(b.get("X", 0)); wy = float(b.get("Y", 0))
            ww = float(b.get("Width", 0)); wh = float(b.get("Height", 0))
        except Exception:
            continue
        if ww <= 0 or wh <= 0:
            continue
        # Does the element's center sit inside this window? Prefer tightest
        # containing window (smallest area) as the best match.
        if wx <= rcx <= wx + ww and wy <= rcy <= wy + wh:
            area = ww * wh
            if best is None or area < best[1]:
                best = (pid, area)
    return best[0] if best else None


def _scroll_position(state: Optional["AppState"], el: ElementSnapshot) -> Optional[float]:
    """Return the scroll-bar value (0..1) of the first scroll-bar child of ``el``.

    Used to detect whether a scroll actually moved the content. Returns
    ``None`` if no scroll bar child is found or its value can't be read.
    """
    if state is None:
        return None
    for child_idx in el.children:
        try:
            child = state.elements[child_idx]
        except IndexError:
            continue
        if child.role == "AXScrollBar":
            v = _ax_copy(child.ax_ref, kAXValue)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
    return None


@mcp.tool()
def scroll(
    app: str,
    element_index: int,
    direction: str,
    pages: int = 1,
    smooth: bool = True,
) -> str:
    """Scroll an element by pages in ``up``, ``down``, ``left``, or ``right``.

    This tool is strictly **background-safe**: it never focuses the target
    window, never raises it to key state, and never moves the user's
    cursor. If the background-safe paths fail, it returns an explicit
    error so the caller can decide whether to fall back to a technique
    that would disturb the user (e.g. ``press_key key="PageDown"``).

    By default ``smooth=True`` breaks each page of movement into a series
    of small increments spread over ~200ms so the page visibly animates
    rather than teleporting. Pass ``smooth=False`` to emit one big
    wheel/value change per page (snappier, slightly jarring; useful when
    you're driving a long jump and don't care about the animation).

    Strategy, in order (all background-safe):

    1. The element's (or a scroll-bar child's) direction-specific
       ``AXScroll*ByPage`` action, when advertised.
    2. Setting the scroll-bar child's ``AXValue`` directly -- standard
       AppKit ``NSScrollView`` machinery honors this without any focus
       or event posting.
    2b. For Chromium-family browsers (Chrome, Brave, Edge), drive
       page-level scrolling via AppleScript ``execute ... javascript``.
       This is the only background-safe path that works on Chrome --
       synthetic wheel events against Chromium's main pid are silently
       dropped when the window isn't key.
    3. Pixel-unit scroll wheel events posted to the correct pid. If the
       element sits inside a WKWebView, events are routed to the
       ``com.apple.WebKit.WebContent`` helper pid that owns the view.

    If the scroll-bar value does not visibly change after all attempts,
    the tool raises ``ToolError``. The caller can then explicitly call
    ``press_key(key="PageDown")``, which WILL steal key focus -- but
    that choice is left to the caller rather than hidden inside this
    tool.
    """
    if direction not in ("up", "down", "left", "right"):
        raise ToolError("direction must be one of up/down/left/right")
    if pages < 1 or pages > 50:
        raise ToolError("pages must be between 1 and 50")

    # Smoothness tuning. ~12 steps per page × ~18ms gives ~215ms per page,
    # which reads as animated but doesn't drag out long scrolls. For
    # ``smooth=False`` we collapse to a single step per page.
    if smooth:
        steps_per_page = 12
        step_interval_s = 0.018
    else:
        steps_per_page = 1
        step_interval_s = 0.03

    pid = _resolve_target_pid(app)
    el = _require_element(pid, element_index)
    state = _STATE_CACHE.get(pid)

    # Locate the immediate scroll-bar child (if any) once -- we use it
    # for both the AX action path, the direct-value path, and the
    # "did it actually scroll?" verification.
    scroll_bar_child: Optional[ElementSnapshot] = None
    if state is not None:
        for child_idx in el.children:
            try:
                child = state.elements[child_idx]
            except IndexError:
                continue
            if child.role == "AXScrollBar":
                scroll_bar_child = child
                break

    # Pick a vertical or horizontal scroll bar if the element itself
    # isn't the AXScrollBar but has siblings. Also keep track of
    # orientation so we can pick the right one. For now we prefer the
    # first scroll-bar child we find.

    before = _scroll_position(state, el)

    # ------------------------------------------------------------------
    # Path 1: native AX scroll-by-page action.
    # ------------------------------------------------------------------
    ax_action = _AX_SCROLL_ACTIONS[direction]
    ax_targets: list[tuple[str, Any]] = []
    if ax_action in el.actions:
        ax_targets.append(("element", el.ax_ref))
    if scroll_bar_child is not None and ax_action in scroll_bar_child.actions:
        ax_targets.append(("scroll-bar child", scroll_bar_child.ax_ref))
    if ax_targets:
        label, ax_ref = ax_targets[0]
        success = 0
        for _ in range(pages):
            err = AXUIElementPerformAction(ax_ref, ax_action)
            if err != AX_SUCCESS:
                break
            success += 1
            time.sleep(0.02)
        if success > 0:
            return (
                f"scrolled {direction} x{success}/{pages} via {ax_action} on "
                f"{label} of element {element_index} in pid {pid}"
            )
        log.info("AX %s returned non-success; trying next path", ax_action)

    # ------------------------------------------------------------------
    # Path 2: set the scroll bar's AXValue directly. For AppKit
    # NSScrollView this is the most reliable background-safe path.
    # ------------------------------------------------------------------
    if scroll_bar_child is not None and before is not None:
        # Page magnitude of 0.1 (10% of scroll range) per page is a
        # reasonable approximation of a real Page Down on most views.
        total_delta = 0.1 * pages
        if direction in ("down", "right"):
            target_val = min(1.0, before + total_delta)
        else:
            target_val = max(0.0, before - total_delta)
        if abs(target_val - before) > 1e-4:
            total_steps = steps_per_page * pages
            any_success = False
            any_fail = False
            for i in range(1, total_steps + 1):
                t = i / total_steps
                intermediate = before + (target_val - before) * t
                err = AXUIElementSetAttributeValue(
                    scroll_bar_child.ax_ref, kAXValue, intermediate
                )
                if err == AX_SUCCESS:
                    any_success = True
                else:
                    any_fail = True
                    break
                time.sleep(step_interval_s)
            if any_success and not any_fail:
                after = _scroll_position(state, el)
                if after is not None and abs(after - before) > 1e-4:
                    return (
                        f"scrolled {direction} x{pages} via AXValue on scroll-bar "
                        f"child of element {element_index} in pid {pid} "
                        f"({before:.3f} -> {after:.3f})"
                    )
                log.info(
                    "AXValue set on scroll bar returned success but value did "
                    "not change (%s -> %s); trying wheel path",
                    before, after,
                )
            else:
                log.info(
                    "AXValue stepping on scroll bar returned AXError "
                    "mid-sequence; trying wheel path",
                )

    # ------------------------------------------------------------------
    # Path 2b: for Chromium-family browsers, drive scrolling via
    # AppleScript "execute ... javascript". This is fully background-
    # safe (doesn't raise the window, doesn't move the cursor) and works
    # even when the app isn't key, which synthetic wheel events against
    # Chrome's main pid don't. Only attempted when the element sits in
    # an identified browser's DOM -- we key on the app's bundle id and
    # the element's AXRole being inside a web area.
    # ------------------------------------------------------------------
    if state is not None and state.bundle_id in _BROWSER_JS_DIALECTS:
        # Choose a reasonable per-page pixel distance. 600px ~= one Page
        # Down on a typical viewport.
        px = 600 * pages
        if direction == "up":
            dy, dx = -px, 0
        elif direction == "down":
            dy, dx = px, 0
        elif direction == "left":
            dy, dx = 0, -px
        else:  # right
            dy, dx = 0, px
        behavior = "smooth" if smooth else "instant"
        # Find the real scrollable element. Many modern web apps
        # (LinkedIn, Notion, Slack-web, Gmail, etc.) scroll a specific
        # inner div rather than the window, with a sticky header. Picking
        # the nearest scrollable ancestor at the viewport center gives
        # us the main content pane in "app-like" pages while still
        # falling back to the document scroller on traditional pages.
        scroll_js = (
            "(function(){"
            "var cx=window.innerWidth/2,cy=window.innerHeight/2;"
            "var el=document.elementFromPoint(cx,cy);"
            "var axis='" + ('x' if direction in ('left', 'right') else 'y') + "';"
            "function ok(e){if(!e)return false;"
            "var cs=getComputedStyle(e);"
            "var of=axis==='y'?cs.overflowY:cs.overflowX;"
            "if(!/(auto|scroll|overlay)/.test(of))return false;"
            "return axis==='y'?e.scrollHeight>e.clientHeight:"
            "e.scrollWidth>e.clientWidth;}"
            "var target=null;"
            "while(el){if(ok(el)){target=el;break;}el=el.parentElement;}"
            "if(!target)target=document.scrollingElement||document.documentElement;"
            "var bx=target.scrollLeft,by=target.scrollTop;"
            f"target.scrollBy({{top:{dy},left:{dx},behavior:'{behavior}'}});"
            "return JSON.stringify({"
            "tag:target.tagName,"
            "id:String(target.id||''),"
            "before:[bx,by],"
            "ch:target.clientHeight,sh:target.scrollHeight});"
            "})();"
        )
        ok, msg = _applescript_run_js(state.bundle_id, scroll_js)
        if ok and msg.startswith("{"):
            # Give smooth-scroll a moment to settle before probing.
            if smooth:
                time.sleep(0.2 + 0.05 * pages)
            # Parse target info from the JS response and re-read scroll
            # position of that same element to verify movement.
            try:
                info = json.loads(msg)
            except Exception:
                info = None
            if info is not None:
                # Use a probe that finds the same target (nearest scroll
                # ancestor at viewport center) and returns its position.
                # Using the same selection heuristic means we're
                # comparing apples to apples.
                probe_js = (
                    "(function(){"
                    "var cx=window.innerWidth/2,cy=window.innerHeight/2;"
                    "var el=document.elementFromPoint(cx,cy);"
                    "var axis='" + ('x' if direction in ('left', 'right') else 'y') + "';"
                    "function ok(e){if(!e)return false;"
                    "var cs=getComputedStyle(e);"
                    "var of=axis==='y'?cs.overflowY:cs.overflowX;"
                    "if(!/(auto|scroll|overlay)/.test(of))return false;"
                    "return axis==='y'?e.scrollHeight>e.clientHeight:"
                    "e.scrollWidth>e.clientWidth;}"
                    "var target=null;"
                    "while(el){if(ok(el)){target=el;break;}el=el.parentElement;}"
                    "if(!target)target=document.scrollingElement||document.documentElement;"
                    "return target.scrollLeft+','+target.scrollTop;"
                    "})();"
                )
                ok2, now_xy = _applescript_run_js(state.bundle_id, probe_js)
                bx, by = info.get("before", [0, 0])
                if ok2 and now_xy and now_xy != f"{bx},{by}":
                    tag = info.get("tag", "?")
                    eid = info.get("id", "")
                    id_suf = f"#{eid}" if eid else ""
                    return (
                        f"scrolled {direction} x{pages} via AppleScript JS on "
                        f"{state.bundle_id} (pid {pid}); target <{tag.lower()}"
                        f"{id_suf}> scrollTop {by} -> {now_xy.split(',')[1]}"
                    )
                log.info(
                    "browser JS scroll appeared to no-op "
                    "(before=%s,%s after=%s); falling through to wheel path",
                    bx, by, now_xy,
                )
        else:
            log.info(
                "browser JS scroll failed (%s); falling through to wheel path",
                msg,
            )

    # ------------------------------------------------------------------
    # Path 3: synthetic pixel-unit scroll wheel events, posted to the
    # right pid. No mouse move, no focus change.
    # ------------------------------------------------------------------
    center = _element_center(el)
    if center is None:
        raise ToolError(
            f"element {element_index} has no geometry and no other scroll path "
            "worked; cannot scroll without stealing focus"
        )

    # Pixel magnitude per page event. ~600px per page is comparable to a
    # real Page Down on a typical 1000px-tall content area. When smoothing
    # is enabled we emit ``steps_per_page`` smaller events per page so the
    # content visibly animates instead of teleporting.
    total_px = 600 * pages
    total_steps = steps_per_page * pages
    step_px = total_px // total_steps if total_steps > 0 else total_px
    # Any leftover from integer division is added to the last step so we
    # end up at exactly the intended distance.
    leftover_px = total_px - (step_px * total_steps)

    dy_step = 0
    dx_step = 0
    if direction == "up":
        dy_step = step_px
    elif direction == "down":
        dy_step = -step_px
    elif direction == "left":
        dx_step = step_px
    elif direction == "right":
        dx_step = -step_px

    def _with_leftover(is_last: bool) -> tuple[int, int]:
        if not is_last or leftover_px == 0:
            return dy_step, dx_step
        extra = leftover_px if direction in ("up", "left") else -leftover_px
        if direction in ("up", "down"):
            return dy_step + extra, dx_step
        return dy_step, dx_step + extra

    # If the scroll area hosts a WKWebView, target its child pid -- wheel
    # events posted to Safari's main pid are dropped by the web content
    # process's event loop.
    wheel_pid = pid
    wheel_pid_note = ""
    if el.position is not None and el.size is not None:
        rect = (el.position[0], el.position[1], el.size[0], el.size[1])
        wc_pid = _web_content_pid_for_rect(rect)
        if wc_pid is not None and wc_pid != pid:
            wheel_pid = wc_pid
            wheel_pid_note = f" via web-content pid {wheel_pid}"

    for step_idx in range(total_steps):
        dy, dx = _with_leftover(step_idx == total_steps - 1)
        evt = CGEventCreateScrollWheelEvent(
            None, kCGScrollEventUnitPixel, 2, dy, dx
        )
        _post_event(evt, wheel_pid)
        time.sleep(step_interval_s)

    after = _scroll_position(state, el)
    visibly_scrolled = (
        before is not None and after is not None and abs(after - before) > 1e-4
    )
    if visibly_scrolled:
        return (
            f"scrolled {direction} x{pages} over element {element_index} "
            f"at ({center[0]:.0f},{center[1]:.0f}) in pid {pid}{wheel_pid_note} "
            f"({before:.3f} -> {after:.3f})"
        )

    # None of the background-safe paths actually moved content. Tell the
    # caller honestly rather than silently resorting to a focus-stealing
    # keystroke.
    wc_detail = (
        f"; web-content pid lookup returned {wheel_pid}"
        if wheel_pid != pid
        else "; no web-content pid overlap found"
    )
    raise ToolError(
        f"could not scroll element {element_index} in a background-safe way "
        f"(AX scroll action not advertised; AXValue write rejected or ignored; "
        f"wheel events to pid {wheel_pid} produced no scroll-bar change"
        f"{wc_detail}). "
        "If you're willing to steal key focus, call "
        "press_key(key=\"PageDown\") or similar explicitly."
    )


# Identifiers of elements whose ``kAXValue`` we have learned is "sticky" --
# ``AXUIElementSetAttributeValue`` returns success and the AX tree reflects
# the new value, but the app's view layer never actually engages the edit,
# so follow-up Return presses do nothing useful. For these we skip AX and
# go straight to the focus + select-all + type + Return path.
_STICKY_AX_VALUE_IDENTIFIERS: frozenset[str] = frozenset({
    # Safari's unified URL/search bar.
    "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD",
})


def _type_into_field(pid: int, el: ElementSnapshot, value: str, submit: bool) -> str:
    """Focus ``el``, replace its contents with ``value`` via typing, optionally submit.

    Used as a fallback when ``AXUIElementSetAttributeValue`` either refuses the
    value or silently fails to engage the view's editor (e.g. Safari's URL bar).
    """
    AXUIElementSetAttributeValue(el.ax_ref, kAXFocused, True)
    # Brief delay so the app notices focus before we hammer it with keys.
    time.sleep(0.02)
    # Select-all + type replaces whatever was there with ``value``.
    _post_key(pid, "cmd+a")
    time.sleep(0.01)
    if value:
        _post_text(pid, value)
    else:
        # Empty value: pressing delete after select-all clears the field.
        _post_key(pid, "delete")
    submitted_via: Optional[str] = None
    if submit:
        # Give the app a moment to finish absorbing the typed text before
        # we press Return (otherwise Safari in particular sometimes commits
        # a partial URL).
        time.sleep(0.03)
        _post_key(pid, "Return")
        submitted_via = "Return key (typed)"
    return submitted_via or "typed"


@mcp.tool()
def set_value(
    app: str,
    element_index: int,
    value: str,
    submit: bool = True,
) -> str:
    """Set the value of a settable accessibility element (text field, slider, …).

    For plain text fields, setting the AX value is usually enough. For
    search/URL bars, the app only *navigates* when Return is pressed; by
    default we therefore focus the element and post a pid-scoped Return
    after setting the value. Pass ``submit=False`` to skip that (useful for
    sliders/steppers, or when you want to prepare a field without firing
    its submit action).

    The server auto-detects two failure modes and falls back to a
    focus + ``cmd+a`` + ``type_text`` + Return sequence:

    * the AX set-value call succeeds but the field's live value does not
      actually update (the view never committed the edit);
    * the element's accessibility ``identifier`` is on the known-sticky
      list -- currently Safari's ``WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD``,
      which accepts ``AXValue`` writes but never navigates from them.

    Fails if the element is not marked ``settable`` in the last
    ``get_app_state`` AND it is also not a known-sticky text field.
    """
    pid = _resolve_target_pid(app)
    el = _require_element(pid, element_index)

    sticky = bool(el.identifier and el.identifier in _STICKY_AX_VALUE_IDENTIFIERS)

    if el.settable is False and not sticky:
        raise ToolError(
            f"element {element_index} reports settable=false; use type_text or click instead"
        )

    # Short-circuit known-sticky fields straight to the typing path.
    if sticky:
        submitted_note = _type_into_field(pid, el, value, submit)
        return (
            f"set value on element {element_index} in pid {pid} "
            f"via typing fallback (sticky AX field: {el.identifier}); "
            f"{submitted_note}"
        )

    err = AXUIElementSetAttributeValue(el.ax_ref, kAXValue, value)
    if err != AX_SUCCESS:
        # Last-resort: try the typing path for text fields. Sliders/steppers
        # will fail here anyway because they don't accept keyboard text, so
        # we only attempt it when the element has a text-field role.
        if el.role in ("AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"):
            submitted_note = _type_into_field(pid, el, value, submit)
            return (
                f"set value on element {element_index} in pid {pid} "
                f"via typing fallback (AXSetValue returned {err}); {submitted_note}"
            )
        # AXError -25205 = kAXErrorAttributeUnsupported -- the element
        # doesn't expose AXValue at all, which is common for buttons,
        # images, group containers, and anything that isn't a value-
        # bearing control. Give the caller a more useful hint.
        if err == -25205:
            raise ToolError(
                f"element {element_index} ({el.role}) does not accept an "
                "AXValue write; set_value is only meaningful on text "
                "fields, sliders, steppers, and similar value-bearing "
                "controls. Use click / perform_secondary_action for "
                "buttons and menu items."
            )
        raise ToolError(
            f"AXUIElementSetAttributeValue failed with AXError {err} on "
            f"element {element_index} ({el.role})"
        )

    # Verify the AX write actually took. Text fields that silently reject
    # ``kAXValue`` writes are the main thing we're guarding against here.
    current = _ax_copy(el.ax_ref, kAXValue)
    if (
        isinstance(current, str)
        and isinstance(value, str)
        and current != value
        and el.role in ("AXTextField", "AXTextArea", "AXSearchField", "AXComboBox")
    ):
        log.info(
            "AX set-value on element %s did not stick (wanted %r, got %r); "
            "falling back to typing",
            element_index, value, current,
        )
        submitted_note = _type_into_field(pid, el, value, submit)
        return (
            f"set value on element {element_index} in pid {pid} "
            f"via typing fallback (AX write did not stick); {submitted_note}"
        )

    submitted: Optional[str] = None
    if submit:
        # Focus the element first so keystrokes (and in some cases the
        # ``AXConfirm`` action) are routed to it.
        AXUIElementSetAttributeValue(el.ax_ref, kAXFocused, True)
        try:
            _post_key(pid, "Return")
            submitted = "Return key"
        except ValueError as exc:
            # As a last resort, try ``AXConfirm`` if the element offers it.
            if "AXConfirm" in el.actions:
                cerr = AXUIElementPerformAction(el.ax_ref, "AXConfirm")
                if cerr == AX_SUCCESS:
                    submitted = "AXConfirm"
            if submitted is None:
                raise ToolError(f"failed to submit value: {exc}") from exc
    suffix = f"; submitted via {submitted}" if submitted else ""
    return f"set value on element {element_index} in pid {pid}{suffix}"


@mcp.tool()
def type_text(
    app: str,
    text: str,
    element_index: Optional[int] = None,
    press_enter: bool = False,
) -> str:
    """Type literal text as keyboard input into ``app``.

    For semantic keys and shortcuts use ``press_key`` instead. Text is sent
    via synthesized keyboard events with Unicode payloads, so it does not
    require a particular keyboard layout to be active.

    If ``element_index`` is provided, that element is focused via
    ``AXFocused=True`` first. For text fields that advertise a ``Confirm``
    action, prefer ``set_value(..., submit=True)`` -- it skips keyboard
    synthesis entirely and is more reliable when the app isn't frontmost.

    ``press_enter=True`` appends a Return key after the text (useful for
    single-line inputs that submit on Enter).
    """
    pid = _resolve_target_pid(app)
    focused_note = ""
    if element_index is not None:
        el = _require_element(pid, element_index)
        ferr = AXUIElementSetAttributeValue(el.ax_ref, kAXFocused, True)
        if ferr != AX_SUCCESS:
            log.info(
                "AXFocused on element %s failed (%s); typing anyway",
                element_index, ferr,
            )
        else:
            focused_note = f" (focused element {element_index})"
    _post_text(pid, text)
    if press_enter:
        try:
            _post_key(pid, "Return")
        except ValueError as exc:
            raise ToolError(f"press_enter failed: {exc}") from exc
    suffix = " + Return" if press_enter else ""
    return f"typed {len(text)} chars{suffix} into pid {pid}{focused_note}"


_PLAYGROUND_SCRIPT = Path(__file__).with_name("cursor_playground.py")


@mcp.tool()
def open_cursor_playground() -> str:
    """Open the ghost-cursor animation playground.

    Spawns a full-screen translucent overlay window that captures every
    click and sends it to the ghost-cursor daemon, so you can eyeball how
    natural the cursor-move animation feels, A/B-test different targets,
    and tune without running the full agent loop. Click the red ``X`` in
    the top-left of the window to dismiss it.
    """
    if not _PLAYGROUND_SCRIPT.exists():
        raise ToolError(f"playground script missing at {_PLAYGROUND_SCRIPT}")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(_PLAYGROUND_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception as exc:
        raise ToolError(f"failed to launch playground: {exc}") from exc
    return f"playground launched (pid {proc.pid}); click the red ✕ in the top-left to exit."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("starting %s (plugin root: %s)", CUA_VERSION, PLUGIN_ROOT)
    _check_ax_permissions()
    mcp.run()


if __name__ == "__main__":
    main()
