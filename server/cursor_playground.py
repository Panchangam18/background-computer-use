"""Interactive playground for tuning the natural-cursor animation.

Run directly::

    .venv/bin/python server/cursor_playground.py

This opens a full-screen translucent window that captures every click and
sends it to a ``cursor_ghost.py`` daemon so you can watch the ghost cursor
animate from its previous position to wherever you clicked. Clicking the
red ``X`` in the top-left exits. The ghost self-exits when idle, just like
in production.

Handy for A/B-testing path shape, duration, jitter, and easing without
having to restart the MCP server or perform real automation.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

try:
    import objc  # type: ignore  # noqa: F401
    from AppKit import (  # type: ignore
        NSApp,
        NSApplication,
        NSApplicationActivationPolicyRegular,
        NSBackingStoreBuffered,
        NSBezierPath,
        NSColor,
        NSEvent,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSMakePoint,
        NSMakeRect,
        NSMakeSize,
        NSScreen,
        NSString,
        NSView,
        NSWindow,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
    )
    from Foundation import NSObject  # type: ignore  # noqa: F401
    from PyObjCTools import AppHelper  # type: ignore
except ImportError as exc:
    sys.stderr.write(f"[cursor-playground] missing PyObjC: {exc}\n")
    sys.exit(1)


_NS_WINDOW_STYLE_BORDERLESS = 0
_NS_FLOATING_WINDOW_LEVEL = 3

GHOST_SCRIPT = Path(__file__).resolve().with_name("cursor_ghost.py")

# A little hit-box in the top-left that lets the user quit.
QUIT_BOX_SIZE = 44


def _launch_ghost() -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    # Keep the ghost alive longer while the playground is running so it
    # doesn't idle-exit between our clicks during manual testing.
    env["CUA_GHOST_IDLE_S"] = "15.0"
    return subprocess.Popen(
        [sys.executable, str(GHOST_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
        env=env,
    )


class _PlaygroundView(NSView):
    def initWithFrame_(self, frame):  # type: ignore[override]
        self = objc.super(_PlaygroundView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._ghost: subprocess.Popen[bytes] | None = None
        self._last_point: tuple[float, float] | None = None
        return self

    def acceptsFirstResponder(self):  # type: ignore[override]
        return True

    def isFlipped(self):  # type: ignore[override]
        # Draw in top-left origin to match the rest of the system.
        return True

    def drawRect_(self, rect):  # type: ignore[override]
        # Background: faintly tinted dark overlay that still lets the user
        # see the desktop through it -- plenty of contrast for the ghost.
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.05, 0.06, 0.09, 0.55).setFill()
        NSBezierPath.fillRect_(rect)

        # Instructions.
        title = NSString.stringWithString_(
            "Cursor animation playground"
        )
        subtitle = NSString.stringWithString_(
            "Click anywhere to send the ghost cursor there. Tap the red ✕ (top-left) to quit."
        )
        title_attrs = {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(26.0),
            NSForegroundColorAttributeName: NSColor.whiteColor(),
        }
        sub_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(14.0),
            NSForegroundColorAttributeName: NSColor.colorWithCalibratedWhite_alpha_(1, 0.75),
        }
        title.drawAtPoint_withAttributes_(NSMakePoint(80, 60), title_attrs)
        subtitle.drawAtPoint_withAttributes_(NSMakePoint(80, 96), sub_attrs)

        # Quit hit-box (top-left corner).
        quit_rect = NSMakeRect(12, 12, QUIT_BOX_SIZE, QUIT_BOX_SIZE)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.82, 0.18, 0.22, 0.92).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(quit_rect).fill()
        x_attrs = {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(22.0),
            NSForegroundColorAttributeName: NSColor.whiteColor(),
        }
        NSString.stringWithString_("✕").drawAtPoint_withAttributes_(
            NSMakePoint(24, 17), x_attrs
        )

        # Show the last click target as a crosshair so the user can
        # visually verify the ghost landed precisely.
        if self._last_point is not None:
            lx, ly = self._last_point
            path = NSBezierPath.bezierPath()
            path.setLineWidth_(1.0)
            path.moveToPoint_(NSMakePoint(lx - 18, ly))
            path.lineToPoint_(NSMakePoint(lx + 18, ly))
            path.moveToPoint_(NSMakePoint(lx, ly - 18))
            path.lineToPoint_(NSMakePoint(lx, ly + 18))
            NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.95, 0.2, 0.85).setStroke()
            path.stroke()

    def mouseDown_(self, event):  # type: ignore[override]
        loc_win = event.locationInWindow()
        # ``locationInWindow`` is always in AppKit (bottom-left) coords
        # regardless of whether the view is flipped -- flipping only affects
        # drawing. Convert to screen (bottom-left) first, then to top-left.
        win = self.window()
        screen_pt_bl = win.convertPointToScreen_(loc_win)
        screen_h = float(NSScreen.mainScreen().frame().size.height)
        sx = float(screen_pt_bl.x)
        sy = screen_h - float(screen_pt_bl.y)  # top-left origin for the ghost

        # Top-left-origin point within the (flipped) view for drawing.
        view_pt_tl = (loc_win.x, win.frame().size.height - loc_win.y)

        # Quit hit-box (top-left corner of the flipped view).
        if view_pt_tl[0] <= 12 + QUIT_BOX_SIZE and view_pt_tl[1] <= 12 + QUIT_BOX_SIZE:
            AppHelper.stopEventLoop()
            return

        self._last_point = view_pt_tl
        self.setNeedsDisplay_(True)
        self._send_click(sx, sy)

    def _send_click(self, x: float, y: float) -> None:
        if self._ghost is None or self._ghost.poll() is not None:
            self._ghost = _launch_ghost()
        assert self._ghost.stdin is not None
        try:
            self._ghost.stdin.write(f"click_at {x:.1f} {y:.1f} 520\n".encode())
            self._ghost.stdin.flush()
        except (BrokenPipeError, OSError):
            # Pipe died -- respawn on next click.
            self._ghost = None


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    screen = NSScreen.mainScreen()
    frame = screen.frame() if screen is not None else NSMakeRect(0, 0, 1280, 800)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, _NS_WINDOW_STYLE_BORDERLESS, NSBackingStoreBuffered, False
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setLevel_(_NS_FLOATING_WINDOW_LEVEL)
    window.setHasShadow_(False)
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
    )
    window.setIgnoresMouseEvents_(False)
    view = _PlaygroundView.alloc().initWithFrame_(frame)
    window.setContentView_(view)
    window.makeFirstResponder_(view)

    window.makeKeyAndOrderFront_(None)
    NSApp.activateIgnoringOtherApps_(True)

    AppHelper.runEventLoop(installInterrupt=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
