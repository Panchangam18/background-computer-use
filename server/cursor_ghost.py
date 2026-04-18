"""Long-running ghost-cursor overlay controlled over stdin.

The MCP server spawns one of these per session and pipes commands to its
stdin. The daemon runs AppKit on its own main thread so we don't have to
share a Cocoa run loop with the MCP server's asyncio event loop.

Protocol (one command per line, whitespace-separated)::

    move   X Y [DURATION_MS] [TARGET_WINDOW_ID]
    flash  X Y [DURATION_MS] [TARGET_WINDOW_ID]
    click_at X Y [DURATION_MS] [TARGET_WINDOW_ID]
    park   X Y W H [TARGET_WINDOW_ID]
    hide
    show
    ping
    quit

Coordinates are screen points with origin at the **top-left** (matches
``get_app_state``). ``TARGET_WINDOW_ID`` is a CGWindowID; when given, the
ghost / ring window is ordered just above that window in the global stack
so it's occluded by the same windows that occlude the target.

``park`` tells the ghost which app window its agent "belongs to" (as a
bounding rect). After ``PARK_IDLE_S`` seconds with no further commands,
the ghost drifts toward the center of that rect instead of sitting on
top of whatever pixel it last clicked -- so multiple agents each stay
near their own app instead of piling up on the last click location.

Lifetime: the daemon exits when its parent process dies (``CUA_GHOST_PARENT_PID``,
set by the server spawner), when stdin closes, or after a long hard-idle
backstop so it can't leak if both of the above fail.
"""
from __future__ import annotations

import math
import os
import signal
import sys
import threading
import time
from pathlib import Path

try:
    import objc  # type: ignore # noqa: F401  (needed so PyObjC loads AppKit correctly)
    from AppKit import (  # type: ignore
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSBitmapImageRep,
        NSColor,
        NSCursor,
        NSGraphicsContext,
        NSImage,
        NSMakeRect,
        NSScreen,
        NSWindow,
        NSWindowCollectionBehaviorIgnoresCycle,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorTransient,
    )
    from PyObjCTools import AppHelper  # type: ignore
    import Quartz  # type: ignore
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(f"[cursor-ghost] missing PyObjC bindings: {exc}\n")
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cursor_paths import duration_for_distance, natural_path  # noqa: E402


# Hard-idle backstop: if the parent-alive check and stdin-close paths both
# somehow fail, self-exit after this many seconds without a command to
# prevent leaked ghost processes. Long by default because the lease model
# wants ghosts to stay visible between an agent's turns.
HARD_IDLE_TIMEOUT = float(os.environ.get("CUA_GHOST_HARD_IDLE_S", "1800.0"))

# Seconds of inactivity after which we drift the ghost toward its parked
# destination (the center of the last-known target-window rect). Short so
# the visual handoff between "I just clicked" and "I'm waiting near my
# app" feels snappy without competing with the click animation.
PARK_IDLE_S = float(os.environ.get("CUA_GHOST_PARK_IDLE_S", "1.5"))

# How often to re-assert the ghost's z-order against its target window.
# Short enough that bringing the target app forward feels snappy (the
# ghost rises within ~150ms of a Cmd-Tab), long enough that we're not
# constantly churning the window server with reorders.
Z_TRACK_INTERVAL_S = float(os.environ.get("CUA_GHOST_Z_TRACK_S", "0.15"))

# Parent PID to watch. When set (by the server spawner), the ghost polls
# ``kill(parent, 0)`` every second and exits as soon as the parent dies.
# This is how "ghost cursor persists for the life of the agent" works.
_PARENT_PID_ENV = os.environ.get("CUA_GHOST_PARENT_PID", "").strip()
try:
    PARENT_PID = int(_PARENT_PID_ENV) if _PARENT_PID_ENV else None
except ValueError:
    PARENT_PID = None

_NS_SCREEN_SAVER_LEVEL = 1000
_NS_NORMAL_WINDOW_LEVEL = 0
_NS_WINDOW_ABOVE = 1
_NS_WINDOW_STYLE_BORDERLESS = 0

# Logical size (in points) the ghost cursor image is rendered at. The SVG
# is drawn into an NSImage of this size; the window matches so the
# click hotspot math stays simple.
CURSOR_SIZE = 36.0

# Per-built-in hotspot defaults (fractions of CURSOR_SIZE). The hotspot
# is the logical "tip" of the pointer -- the pixel that corresponds to
# the click target. For bundled SVGs these are hand-tuned to match each
# asset's artwork.
_BUILTIN_HOTSPOTS = {
    # default.svg: tip is at (3, 3) in a 70x90 viewBox -> ~(0.043, 0.033).
    "default": (3.0 / 70.0, 3.0 / 90.0),
    # claude.svg has no meaningful "tip"; center works best.
    "claude": (0.5, 0.5),
}
_cursor_name = os.environ.get("CUA_CURSOR", "default").strip() or "default"
_default_hotspot = _BUILTIN_HOTSPOTS.get(_cursor_name, (0.0, 0.0))
_hotspot_env = os.environ.get("CUA_CURSOR_HOTSPOT", "").strip()
if _hotspot_env:
    try:
        _hx_frac, _hy_frac = (float(v) for v in _hotspot_env.split(",", 1))
    except ValueError:
        _hx_frac, _hy_frac = _default_hotspot
else:
    _hx_frac, _hy_frac = _default_hotspot
CURSOR_HOTSPOT_X = _hx_frac * CURSOR_SIZE
CURSOR_HOTSPOT_Y = _hy_frac * CURSOR_SIZE

# Optional ring overlay that flashes on click landing. Off by default
# because the cursor-shrink "press" animation is the primary tell.
RING_SIZE = 44.0
RING_ENABLED = os.environ.get("CUA_CLICK_RING", "0") in {"1", "true", "True", "yes"}

# How far the cursor image shrinks when "pressed" (e.g. 0.7 = 70% size).
CLICK_PRESS_SCALE = float(os.environ.get("CUA_CLICK_PRESS_SCALE", "0.7"))

_CURSOR_DIR = Path(__file__).resolve().parent / "cursors"
_BUILTIN_CURSORS = {
    "default": _CURSOR_DIR / "default.svg",
    "claude": _CURSOR_DIR / "claude.svg",
}


def _resolve_cursor_path() -> Path | None:
    """Resolve which cursor asset to load based on CUA_CURSOR.

    Accepts one of the built-in names (``default``, ``claude``) or an
    absolute filesystem path to a user-supplied SVG/PNG. Returns ``None``
    if the named file is missing -- callers then fall back to the system
    arrow cursor so we never render a blank ghost.
    """
    name = os.environ.get("CUA_CURSOR", "default").strip()
    if not name:
        name = "default"
    if name in _BUILTIN_CURSORS:
        path = _BUILTIN_CURSORS[name]
    else:
        path = Path(name).expanduser()
    return path if path.is_file() else None


def _rasterize(img: NSImage, size: float, scale: int = 2) -> "Quartz.CGImageRef | None":
    """Render ``img`` (which may be SVG-backed) into a bitmap CGImage.

    CALayer's ``setContents:`` wants a CGImage, and NSImage's
    ``CGImageForProposedRect:`` returns ``None`` for vector-backed
    images in many macOS versions. Rasterize explicitly into a pixel
    buffer at 2x so the layer has crisp contents on Retina.
    """
    px = int(size * scale)
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 32,
    )
    if rep is None:
        return None
    rep.setSize_((size, size))
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    if ctx is None:
        return None
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    img.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, size, size), NSMakeRect(0, 0, 0, 0), 2, 1.0,
    )
    NSGraphicsContext.restoreGraphicsState()
    return rep.CGImage()


def _load_cursor_image() -> tuple["Quartz.CGImageRef | None", float, float]:
    """Load the configured cursor image (SVG/PNG) rasterized to ``CURSOR_SIZE``.

    NSImage natively decodes SVGs on macOS 13+. We draw it into a
    bitmap rep at 2x so CALayer can render it crisply on Retina. If the
    configured asset is missing, fall back to the system arrow cursor.
    """
    path = _resolve_cursor_path()
    if path is not None:
        img = NSImage.alloc().initWithContentsOfFile_(str(path))
        if img is not None and img.isValid():
            img.setSize_((CURSOR_SIZE, CURSOR_SIZE))
            cg = _rasterize(img, CURSOR_SIZE)
            if cg is not None:
                return cg, CURSOR_SIZE, CURSOR_SIZE
    cursor = NSCursor.arrowCursor()
    img = cursor.image()
    size = img.size()
    cg = _rasterize(img, float(size.width))
    return cg, float(size.width), float(size.height)


class Ghost:
    def __init__(self) -> None:
        self.last_cmd_time = time.time()
        self.ghost_win: NSWindow | None = None
        self.ring_win: NSWindow | None = None
        self.current_x = -1000.0
        self.current_y = -1000.0
        self.ring_hide_token = 0  # cancels stale ring-hide callbacks
        self.cursor_w: float = 22.0
        self.cursor_h: float = 32.0
        # Parked destination: the center of the agent's target app's
        # window. Updated on every ``park`` command (and piggybacked from
        # ``click_at``). When the ghost goes idle for ``PARK_IDLE_S``, it
        # drifts here so it visibly "belongs to" its app.
        self.park_target: tuple[float, float] | None = None
        self.park_window_id: int = 0
        # Monotonic counter so stale park animations don't fight new
        # commands: a new command bumps this, and any in-flight park
        # callback that sees a mismatched token bails out.
        self.park_token = 0
        self.parked = False  # last movement was a park drift, not a user click

    def setup(self) -> None:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._make_ghost_window()
        self._make_ring_window()
        # Lifecycle: poll parent-alive every second, exit on death or
        # hard-idle timeout. Replaces the old short "exit after 5s of
        # silence" behavior so ghosts persist for the life of their
        # agent's server process and multiple agents can each keep
        # their own cursor visible simultaneously.
        AppHelper.callLater(1.0, self._lifecycle_tick)
        # Z-tracking: every ~150ms, re-assert the ghost's z-order
        # relative to its target app's window. If the user brings the
        # target app to the front (e.g. Cmd-Tab to Calculator), the
        # ghost rises with it; if another app is raised above the
        # target, the ghost drops behind it too.
        AppHelper.callLater(Z_TRACK_INTERVAL_S, self._z_track_tick)

    # ------------------------------------------------------------------ setup

    def _make_transparent_window(self, w: float, h: float) -> NSWindow:
        frame = NSMakeRect(-1000, -1000, w, h)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, _NS_WINDOW_STYLE_BORDERLESS, NSBackingStoreBuffered, False
        )
        win.setBackgroundColor_(NSColor.clearColor())
        win.setOpaque_(False)
        win.setHasShadow_(False)
        win.setIgnoresMouseEvents_(True)
        # Intentionally *not* setting CanJoinAllSpaces: the ghost should
        # stay on the Space where its target app lives, so that a
        # three-finger swipe to a different Space leaves the ghost
        # behind (where it still visibly "belongs to" its app) rather
        # than following the user into whatever they swiped to. The
        # ghost is re-ordered-front relative to its target window on
        # every move/flash, which also forces it onto that window's
        # Space via AppKit's space-tracking.
        win.setCollectionBehavior_(
            NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorTransient
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        return win

    def _make_ghost_window(self) -> None:
        cg_image, cw, ch = _load_cursor_image()
        self.cursor_w = cw
        self.cursor_h = ch
        win = self._make_transparent_window(cw, ch)
        # High level by default; will be re-ordered relative to a target
        # window on each move/flash when a target_window_id is supplied.
        win.setLevel_(_NS_SCREEN_SAVER_LEVEL)
        content = win.contentView()
        content.setWantsLayer_(True)
        layer = content.layer()
        layer.setFrame_(NSMakeRect(0, 0, cw, ch))
        layer.setContentsGravity_("resize")
        if cg_image is not None:
            layer.setContents_(cg_image)
        layer.setOpacity_(1.0)
        self.cursor_layer = layer
        self.ghost_win = win

    def _make_ring_window(self) -> None:
        win = self._make_transparent_window(RING_SIZE, RING_SIZE)
        win.setLevel_(_NS_SCREEN_SAVER_LEVEL)
        content = win.contentView()
        content.setWantsLayer_(True)
        layer = content.layer()
        layer.setFrame_(NSMakeRect(0, 0, RING_SIZE, RING_SIZE))
        layer.setCornerRadius_(RING_SIZE / 2.0)
        layer.setMasksToBounds_(True)
        layer.setBorderWidth_(3.0)
        stroke = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.55, 1.00, 0.95)
        fill = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.55, 1.00, 0.22)
        layer.setBorderColor_(stroke.CGColor())
        layer.setBackgroundColor_(fill.CGColor())
        layer.setOpacity_(0.0)
        self.ring_win = win

    # ------------------------------------------------------------------ utils

    @staticmethod
    def _screen_height() -> float:
        s = NSScreen.mainScreen()
        return float(s.frame().size.height) if s is not None else 1000.0

    def _set_pos(self, win: NSWindow, x: float, y: float, w: float, h: float) -> None:
        """Place a window so that its logical hotspot lands on ``(x, y)``
        (screen coords with top-left origin).

        For the ghost cursor, the hotspot is the arrow tip, which for the
        system arrow cursor is the top-left corner of the image. For the
        ring, the hotspot is the window center.
        """
        sh = self._screen_height()
        if win is self.ghost_win:
            # Align image top-left with (x, y).
            win.setFrameOrigin_((x - CURSOR_HOTSPOT_X, sh - y - h + CURSOR_HOTSPOT_Y))
        else:
            win.setFrameOrigin_((x - w / 2.0, sh - y - h / 2.0))

    def _order_above(self, win: NSWindow, target_window_id: int) -> None:
        """Place ``win`` in the global z-stack just above ``target_window_id``.

        ``orderWindow:relativeTo:`` only reorders windows *within the same
        window level*. If ``win`` is at ``NSScreenSaverWindowLevel`` (our
        default for full-screen visibility) it will always float above
        normal app windows regardless of the relative order. So when we're
        asked to respect a target window, drop the ghost down to the normal
        level first -- that way another app's window stacked above the
        target will correctly occlude the ghost too.
        """
        if target_window_id > 0:
            try:
                if win.level() != _NS_NORMAL_WINDOW_LEVEL:
                    win.setLevel_(_NS_NORMAL_WINDOW_LEVEL)
                win.orderWindow_relativeTo_(_NS_WINDOW_ABOVE, target_window_id)
            except Exception:
                pass
        else:
            try:
                if win.level() != _NS_SCREEN_SAVER_LEVEL:
                    win.setLevel_(_NS_SCREEN_SAVER_LEVEL)
            except Exception:
                pass

    # --------------------------------------------------------------- commands

    def handle_line(self, line: str) -> None:
        """Entry point for each stdin line; must run on the main thread."""
        self.last_cmd_time = time.time()
        # Any new command invalidates a pending park drift so we don't
        # animate toward the parked spot while the agent is still
        # actively driving. Non-activity commands (ping/show/hide) set
        # ``parked=False`` too so the next drift is allowed to re-run
        # if the agent stops again.
        self.park_token += 1
        self.parked = False
        parts = line.strip().split()
        if not parts:
            return
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "move":
                self._cmd_move(*_parse_xy_args(args))
            elif cmd == "flash":
                self._cmd_flash(*_parse_xy_args(args, default_duration=420.0))
            elif cmd == "click_at":
                self._cmd_click_at(*_parse_xy_args(args, default_duration=420.0))
            elif cmd == "park":
                self._cmd_park(args)
            elif cmd == "hide":
                if self.ghost_win is not None:
                    self.ghost_win.orderOut_(None)
            elif cmd == "show":
                if self.ghost_win is not None:
                    self.ghost_win.orderFrontRegardless()
            elif cmd == "ping":
                pass
            elif cmd == "quit":
                AppHelper.stopEventLoop()
            else:
                sys.stderr.write(f"[cursor-ghost] unknown command: {cmd!r}\n")
        except Exception as exc:
            sys.stderr.write(f"[cursor-ghost] error handling {line.strip()!r}: {exc}\n")

    def _cmd_park(self, args: list[str]) -> None:
        """Remember where to drift to once the agent goes idle.

        Arguments: ``x y w h [target_window_id]`` -- the bounding rect
        of the agent's target app window in top-left screen coords. We
        store the center as the "rest position" and the window id so
        the drift animation orders the ghost above the same window.
        """
        if len(args) < 4:
            return
        try:
            x = float(args[0])
            y = float(args[1])
            w = float(args[2])
            h = float(args[3])
        except ValueError:
            return
        twid = 0
        if len(args) > 4 and args[4]:
            try:
                twid = int(args[4])
            except ValueError:
                twid = 0
        # Aim for a slightly-offset interior point (about 1/3 in from
        # the window's top-left) rather than the dead center, so two
        # ghosts parked on different windows don't sit exactly on top
        # of whatever UI is centered in their window.
        cx = x + max(40.0, min(w * 0.33, w - 40.0))
        cy = y + max(40.0, min(h * 0.33, h - 40.0))
        self.park_target = (cx, cy)
        self.park_window_id = twid

    def _cmd_move(
        self, x: float, y: float, duration_ms: float | None, target_window_id: int
    ) -> None:
        if self.ghost_win is None:
            return
        # If ghost is hidden or off-screen, teleport to a point near (x, y)
        # rather than animating from the previous location (which may be
        # stale from a previous idle cycle).
        cw, ch = self.cursor_w, self.cursor_h
        if not self.ghost_win.isVisible() or self.current_x < 0:
            start_x = x - 120.0
            start_y = y + 80.0
            self._set_pos(self.ghost_win, start_x, start_y, cw, ch)
            self.current_x, self.current_y = start_x, start_y

        self.ghost_win.orderFrontRegardless()
        self._order_above(self.ghost_win, target_window_id)
        # Remember this window id so the background z-tracker can keep
        # the ghost ordered relative to it between commands. Without
        # this, bringing the target app to the front by any means
        # other than an MCP tool call wouldn't raise the ghost too.
        if target_window_id > 0:
            self.park_window_id = target_window_id

        duration_s = (duration_ms / 1000.0) if duration_ms else None
        path = natural_path(self.current_x, self.current_y, x, y, duration=duration_s)
        for px, py, pt in path:
            AppHelper.callLater(
                pt,
                lambda px=px, py=py: self._set_pos(self.ghost_win, px, py, cw, ch),
            )
        # Update logical position to the final target.
        self.current_x, self.current_y = x, y

    def _cmd_flash(
        self, x: float, y: float, duration_ms: float | None, target_window_id: int
    ) -> None:
        if self.ring_win is None:
            return
        dur = float(duration_ms) if duration_ms else 420.0
        dur_s = dur / 1000.0
        self.ring_hide_token += 1
        token = self.ring_hide_token

        self._set_pos(self.ring_win, x, y, RING_SIZE, RING_SIZE)

        layer = self.ring_win.contentView().layer()
        # Snap to full opacity without animating in.
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        layer.setOpacity_(1.0)
        Quartz.CATransaction.commit()

        self.ring_win.orderFrontRegardless()
        self._order_above(self.ring_win, target_window_id)

        # Fade in the last ~120ms.
        fade_start = max(0.0, dur_s - 0.12)

        def begin_fade() -> None:
            if self.ring_hide_token != token:
                return
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setAnimationDuration_(0.12)
            layer.setOpacity_(0.0)
            Quartz.CATransaction.commit()

        def finish() -> None:
            if self.ring_hide_token != token:
                return
            self.ring_win.orderOut_(None)

        AppHelper.callLater(fade_start, begin_fade)
        AppHelper.callLater(dur_s + 0.03, finish)

    def _cmd_press(self) -> None:
        """Briefly shrink the ghost cursor then spring it back to full size.

        This is the visible "click" tell: the cursor image scales down to
        ``CLICK_PRESS_SCALE`` over ~80ms and springs back over ~140ms.
        Because the layer's anchor point is set to the cursor hotspot in
        ``_make_ghost_window``, the tip stays glued to the target pixel
        while the rest of the image pulses inward.
        """
        layer = getattr(self, "cursor_layer", None)
        if layer is None:
            return
        s = max(0.1, min(1.0, CLICK_PRESS_SCALE))
        down = Quartz.CATransform3DMakeScale(s, s, 1.0)
        up = Quartz.CATransform3DIdentity

        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.08)
        layer.setTransform_(down)
        Quartz.CATransaction.commit()

        def release() -> None:
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setAnimationDuration_(0.14)
            layer.setTransform_(up)
            Quartz.CATransaction.commit()

        AppHelper.callLater(0.09, release)

    def _cmd_click_at(
        self, x: float, y: float, duration_ms: float | None, target_window_id: int
    ) -> None:
        # Compute how long the move animation will take *before* we kick it
        # off (since _cmd_move updates self.current_{x,y} to the target
        # immediately). Schedule the ring flash / press animation to fire
        # slightly before the ghost arrives so the tell is visible at the
        # landing frame.
        dist = math.hypot(x - self.current_x, y - self.current_y)
        move_dur = duration_for_distance(dist) if dist > 1.5 else 0.0
        flash_ms = float(duration_ms) if duration_ms else 420.0
        self._cmd_move(x, y, None, target_window_id)
        delay = max(0.0, move_dur - 0.05)
        AppHelper.callLater(delay, self._cmd_press)
        if RING_ENABLED:
            AppHelper.callLater(
                delay,
                lambda: self._cmd_flash(x, y, flash_ms, target_window_id),
            )

    # --------------------------------------------------------------- lifecycle

    def _parent_alive(self) -> bool:
        """Return False if our spawning server process has exited.

        Uses ``kill(pid, 0)`` which is the standard Unix idiom: signal
        0 means "don't actually send anything, just check reachability".
        ``ProcessLookupError`` = pid is gone; ``PermissionError`` means
        the process exists but isn't ours to signal (still alive).
        """
        if PARENT_PID is None:
            return True
        try:
            os.kill(PARENT_PID, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _lifecycle_tick(self) -> None:
        """Periodic tick: exit when parent dies or hard-idle elapses,
        and drift toward the parked rest spot after a short lull."""
        if not self._parent_alive():
            AppHelper.stopEventLoop()
            return
        if time.time() - self.last_cmd_time > HARD_IDLE_TIMEOUT:
            AppHelper.stopEventLoop()
            return
        # If we've been idle long enough and we have a park target
        # that isn't already where we are, drift there. The drift
        # itself is treated as "idle movement" so it doesn't refresh
        # last_cmd_time, but it does consume one park_token slot so
        # we don't kick off a second drift while the first is running.
        idle_for = time.time() - self.last_cmd_time
        if (
            idle_for >= PARK_IDLE_S
            and self.park_target is not None
            and not self.parked
            and self.ghost_win is not None
        ):
            px, py = self.park_target
            dist = math.hypot(px - self.current_x, py - self.current_y)
            if dist > 12.0:
                self._cmd_move(px, py, None, self.park_window_id)
                self.parked = True
        AppHelper.callLater(1.0, self._lifecycle_tick)

    def _z_track_tick(self) -> None:
        """Re-assert the ghost's z-order relative to its target window.

        Runs on a ~150ms cadence so that:

        * bringing the target app to the front via Cmd-Tab, Dock click,
          or any other means raises the ghost with it;
        * raising an unrelated app above the target drops the ghost
          behind that app too (the ghost correctly reads as "owned by"
          its target window);
        * minimizing the target window (which yields a zero-height or
          off-screen CGWindow rect) keeps the ghost at its last order
          rather than floating stranded at screen-saver level.

        ``orderWindow:relativeTo:`` is effectively idempotent when the
        relative order hasn't changed, so polling at this rate is
        cheap and doesn't produce visible flicker.
        """
        if self.ghost_win is not None and self.park_window_id > 0:
            try:
                self._order_above(self.ghost_win, self.park_window_id)
                # Only track the ring's z too when it's currently
                # visible; otherwise we'd force it onscreen.
                if self.ring_win is not None and self.ring_win.isVisible():
                    self._order_above(self.ring_win, self.park_window_id)
            except Exception:
                pass
        AppHelper.callLater(Z_TRACK_INTERVAL_S, self._z_track_tick)


def _parse_xy_args(
    args: list[str], default_duration: float | None = None
) -> tuple[float, float, float | None, int]:
    x = float(args[0])
    y = float(args[1])
    duration = float(args[2]) if len(args) > 2 and args[2] else default_duration
    target_window_id = int(args[3]) if len(args) > 3 and args[3] else 0
    return x, y, duration, target_window_id


def _reader_loop(ghost: Ghost) -> None:
    """Read stdin lines and dispatch to the main thread."""
    for line in sys.stdin:
        AppHelper.callAfter(ghost.handle_line, line)
    # Parent closed stdin -- exit gracefully.
    AppHelper.callAfter(AppHelper.stopEventLoop)


def main() -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    # Safety: hard-exit well after the hard-idle timeout in case the
    # AppKit event loop is wedged and its timer callbacks aren't firing.
    # ``threading.Timer`` runs off the main thread so it gets through
    # even if the main event loop is stuck.
    threading.Timer(HARD_IDLE_TIMEOUT * 2 + 60.0, lambda: os._exit(0)).start()

    ghost = Ghost()
    ghost.setup()

    reader = threading.Thread(target=_reader_loop, args=(ghost,), daemon=True)
    reader.start()

    AppHelper.runEventLoop(installInterrupt=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
