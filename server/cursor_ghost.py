"""Long-running ghost-cursor overlay controlled over stdin.

The MCP server spawns one of these per session and pipes commands to its
stdin. The daemon runs AppKit on its own main thread so we don't have to
share a Cocoa run loop with the MCP server's asyncio event loop.

Protocol (one command per line, whitespace-separated)::

    move   X Y [DURATION_MS] [TARGET_WINDOW_ID]
    flash  X Y [DURATION_MS] [TARGET_WINDOW_ID]
    click_at X Y [DURATION_MS] [TARGET_WINDOW_ID]
    hide
    show
    ping
    quit

Coordinates are screen points with origin at the **top-left** (matches
``get_app_state``). ``TARGET_WINDOW_ID`` is a CGWindowID; when given, the
ghost / ring window is ordered just above that window in the global stack
so it's occluded by the same windows that occlude the target.

The daemon self-exits ``IDLE_TIMEOUT`` seconds after the last command, so
when the agent stops making tool calls the ghost cursor disappears.
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
        NSColor,
        NSCursor,
        NSImage,
        NSMakeRect,
        NSScreen,
        NSWindow,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
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


IDLE_TIMEOUT = float(os.environ.get("CUA_GHOST_IDLE_S", "5.0"))

_NS_SCREEN_SAVER_LEVEL = 1000
_NS_NORMAL_WINDOW_LEVEL = 0
_NS_WINDOW_ABOVE = 1
_NS_WINDOW_STYLE_BORDERLESS = 0

# The system arrow cursor's hotspot is at the image's top-left, so
# top-left of our window == the tip of the cursor == the click target.
# Size is queried from the actual NSCursor image at setup time so we
# match whatever Retina scale the system cursor is rendered at.
CURSOR_HOTSPOT_X = 0.0
CURSOR_HOTSPOT_Y = 0.0

RING_SIZE = 44.0


def _arrow_cursor_image() -> tuple[NSImage, float, float]:
    """Fetch the system arrow cursor as an NSImage plus its logical size.

    Using the system cursor image means the ghost looks like a real
    macOS pointer and its hotspot is exactly at the top-left, so we
    don't have to guess offsets when positioning the window.
    """
    cursor = NSCursor.arrowCursor()
    img = cursor.image()
    size = img.size()
    return img, float(size.width), float(size.height)


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

    def setup(self) -> None:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._make_ghost_window()
        self._make_ring_window()
        AppHelper.callLater(1.0, self._idle_tick)

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
        win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorTransient
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        return win

    def _make_ghost_window(self) -> None:
        cursor_img, cw, ch = _arrow_cursor_image()
        self.cursor_w = cw
        self.cursor_h = ch
        win = self._make_transparent_window(cw, ch)
        # High level by default; will be re-ordered relative to a target
        # window on each move/flash when a target_window_id is supplied.
        win.setLevel_(_NS_SCREEN_SAVER_LEVEL)
        content = win.contentView()
        content.setWantsLayer_(True)
        layer = content.layer()
        cg = cursor_img.CGImageForProposedRect_context_hints_(None, None, None)
        if isinstance(cg, tuple):
            cg = cg[0]
        if cg is not None:
            layer.setContents_(cg)
        # Slight blue tint + partial transparency so the ghost cursor is
        # visually distinguishable from the user's real cursor.
        layer.setOpacity_(0.8)
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.24, 0.56, 1.0, 0.18
            ).CGColor()
        )
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

    def _cmd_click_at(
        self, x: float, y: float, duration_ms: float | None, target_window_id: int
    ) -> None:
        # Compute how long the move animation will take *before* we kick it
        # off (since _cmd_move updates self.current_{x,y} to the target
        # immediately). Schedule the ring flash to fire slightly before the
        # ghost arrives so the ring is already visible at the landing frame.
        dist = math.hypot(x - self.current_x, y - self.current_y)
        move_dur = duration_for_distance(dist) if dist > 1.5 else 0.0
        flash_ms = float(duration_ms) if duration_ms else 420.0
        self._cmd_move(x, y, None, target_window_id)
        AppHelper.callLater(
            max(0.0, move_dur - 0.05),
            lambda: self._cmd_flash(x, y, flash_ms, target_window_id),
        )

    # --------------------------------------------------------------- lifecycle

    def _idle_tick(self) -> None:
        if time.time() - self.last_cmd_time > IDLE_TIMEOUT:
            AppHelper.stopEventLoop()
            return
        AppHelper.callLater(1.0, self._idle_tick)


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
    # Safety: hard-exit well after IDLE_TIMEOUT in case anything wedges.
    threading.Timer(IDLE_TIMEOUT * 4 + 10.0, lambda: os._exit(0)).start()

    ghost = Ghost()
    ghost.setup()

    reader = threading.Thread(target=_reader_loop, args=(ghost,), daemon=True)
    reader.start()

    AppHelper.runEventLoop(installInterrupt=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
