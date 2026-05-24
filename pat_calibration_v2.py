#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Unified calibration v2 -- single mapping shared by fn 1 and fn 2.
#
# Workflow (4 iterations):
#   1. Script sends an ADB tap to a known emulator pixel.
#   2. Game shows "x:N y:N" in its UI.
#   3. A transparent full-screen Tk overlay appears OVER BlueStacks.
#   4. User drag-selects a rectangle around the "x:N y:N" text.
#   5. Script converts that screen rectangle to ADB pixels using the
#      OS-level BlueStacks window bounds + emulator resolution.
#   6. Script crops the live emulator screencap to that ADB rect and OCRs.
#   7. After 4 iterations we have 4 (adb_xy -> game_xy) pairs and solve
#      an affine kx, ky, bx, by by least squares.
#
# Result is saved to game_affine_calibration.json. Both fn 1 (game->ADB)
# and fn 2 (planned to use the same mapping) consume that file.

import os
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, Tuple, List, Dict, Callable, Any

# Backend pieces -- the only thing we need from pat_clicker is the ADB
# controller and the JSON helpers.
from pat_clicker import (
    ADBController,
    WindowLocator,
    save_json,
    load_json,
    GAME_AFFINE_CALIB_FILE,
    ZOOM_CALIB_FILE,
)
import pat_ocr

# numpy is optional but speeds up the least-squares fit. Falls back to
# pure-python if missing.
try:
    import numpy as np  # type: ignore
except ImportError:
    np = None  # type: ignore


# ---------------------------------------------------------------------------
# Coordinate translation helpers
# ---------------------------------------------------------------------------
def screen_rect_to_adb_rect(screen_rect: Tuple[int, int, int, int],
                            bs_window: Dict[str, int],
                            adb_w: int, adb_h: int,
                            pad_pct: float = 0.6
                            ) -> Tuple[int, int, int, int]:
    """
    Convert a screen-pixel rectangle to ADB-pixel rectangle using the OS
    BlueStacks window bounds. Returns (x, y, w, h) in ADB pixels.

    `pad_pct` adds a fractional safety margin on each side (default 60%
    of the rectangle's own width/height). The user is hand-drawing the
    rectangle while the overlay alpha-blends with the screen, so it is
    NORMAL to miss the actual text bounds by 20-40 px. The padding makes
    the OCR pipeline forgiving of those small inaccuracies -- without
    it, a tight rectangle that just clips the bottom of 'y:' will cause
    OCR to fail with no useful feedback.

    Assumption: the BlueStacks window content area fills its OS rectangle
    1:1 with the emulator display. If BS adds chrome (toolbar, sidebar)
    the user can still draw a wider rectangle to compensate.
    """
    sx, sy, sw, sh = screen_rect
    bx, by, bw, bh = bs_window["x"], bs_window["y"], bs_window["w"], bs_window["h"]
    if bw <= 0 or bh <= 0:
        raise ValueError("Invalid BlueStacks window size")
    # Inflate the rectangle by pad_pct on each side BEFORE we translate
    # to ADB coordinates. This keeps the math simple (one transform).
    pad_w = sw * pad_pct
    pad_h = sh * pad_pct
    sx -= pad_w
    sy -= pad_h
    sw += 2 * pad_w
    sh += 2 * pad_h
    # Translate screen point to BS-window-relative point.
    rel_x = sx - bx
    rel_y = sy - by
    # Scale to ADB pixels.
    scale_x = adb_w / bw
    scale_y = adb_h / bh
    ax = int(rel_x * scale_x)
    ay = int(rel_y * scale_y)
    aw = int(sw * scale_x)
    ah = int(sh * scale_y)
    # Clamp into ADB display bounds.
    ax = max(0, min(adb_w - 1, ax))
    ay = max(0, min(adb_h - 1, ay))
    aw = max(1, min(adb_w - ax, aw))
    ah = max(1, min(adb_h - ay, ah))
    return ax, ay, aw, ah


# ---------------------------------------------------------------------------
# Transparent overlay for drawing a rectangle over BlueStacks
# ---------------------------------------------------------------------------
class OverlayRectPicker:
    """
    Full-screen semi-transparent Tk overlay that lets the user drag a
    rectangle anywhere on screen. Returns (x, y, w, h) in screen pixels.

    Lifecycle:
      - call .pick(prompt) which BLOCKS until the user finishes drag,
        presses ESC to cancel, or closes the window.
      - returns the rectangle as (x, y, w, h) or None on cancel.

    The overlay sits above ALL other windows (including BlueStacks) so
    the user actually sees the game beneath the semi-transparent layer.
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._result: Optional[Tuple[int, int, int, int]] = None

    def pick(self, prompt: str) -> Optional[Tuple[int, int, int, int]]:
        """
        Show the overlay and BLOCK until the user finishes a drag.

        Implementation notes:
          * Tk on macOS has TWO transparency mechanisms:
              - `attributes('-alpha', X)`: makes the WHOLE window (and all
                widgets inside it) semi-transparent. The number X is the
                opacity, NOT the transparency.
              - `attributes('-transparentcolor', '<color>')`: only works
                on Windows; raises TclError on macOS.
            So we use `-alpha` and keep it high enough (~0.35-0.40) that
            both the game underneath AND our overlay markings are visible.
            A pure-black background at this alpha looks too dark, so we
            use a very dark gray ('gray2' = 5/255 brightness) which lets
            ~95% of the underlying pixels show through.

          * The earlier version put tk.Label widgets at the top of the
            window for the prompt text. Labels capture mouse events that
            never reach the canvas underneath -- which is why the user
            saw "drawing not responding". We now draw the prompt text
            DIRECTLY on the canvas with `create_text`, so the canvas is
            the only widget and gets every event.
        """
        self._result = None
        top = tk.Toplevel(self.root)
        # Critical ordering on macOS: overrideredirect MUST be set BEFORE
        # we apply geometry / show the window, otherwise the WM paints a
        # native title bar (~28px on macOS) that offsets all winfo_rooty
        # coordinates downward. We previously did it the other way around
        # and got rectangles that visually looked correct but were drawn
        # ~28 px lower in ADB coordinates -- which is exactly the "crop
        # is offset" symptom seen on the debug PNG.
        top.withdraw()  # Stay hidden until fully configured
        try:
            top.overrideredirect(True)
        except tk.TclError:
            pass
        sw_screen = top.winfo_screenwidth()
        sh_screen = top.winfo_screenheight()
        top.geometry(f"{sw_screen}x{sh_screen}+0+0")
        top.attributes("-topmost", True)
        # Opacity: 1.0 = solid, 0.0 = invisible. 0.35 lets the game show
        # through clearly while still tinting the screen so the user can
        # see the active overlay area.
        try:
            top.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        # Very dark gray (almost black). Pure black + low alpha can render
        # as solid black on some macOS versions due to a Tk quirk.
        bg_color = "gray2"
        top.config(bg=bg_color, cursor="crosshair")

        # Single full-screen canvas. No separate Label widgets so nothing
        # blocks mouse events from reaching the canvas.
        canvas = tk.Canvas(top, highlightthickness=0,
                           bg=bg_color, cursor="crosshair",
                           bd=0,
                           width=sw_screen, height=sh_screen)
        canvas.pack(fill="both", expand=True)
        # Force layout to apply BEFORE we start drawing text on the canvas.
        top.update_idletasks()
        # Use the screen size directly. winfo_width on a freshly-packed
        # canvas sometimes returns 1 on macOS until the first redraw.
        sw = sw_screen

        # Prompt text rendered on the canvas itself -- the canvas is still
        # the top-most clickable widget.
        canvas.create_text(sw // 2, 30, text=prompt,
                           font=("Helvetica", 18, "bold"),
                           fill="white", anchor="n")
        canvas.create_text(sw // 2, 70,
                           text="Drag the mouse to draw a rectangle around "
                                "the x:N y:N field.  ESC to cancel.",
                           font=("Helvetica", 13),
                           fill="#ffe070", anchor="n")

        state: Dict[str, Any] = {"start": None, "rect_id": None,
                                  "text_id": None}

        def on_press(ev):
            # Store the press position in ROOT coordinates so we can
            # report screen pixels regardless of the canvas origin.
            state["start"] = (ev.x_root, ev.y_root)
            if state["rect_id"] is not None:
                canvas.delete(state["rect_id"])
                state["rect_id"] = None
            if state["text_id"] is not None:
                canvas.delete(state["text_id"])
                state["text_id"] = None

        def on_drag(ev):
            if state["start"] is None:
                return
            sx, sy = state["start"]
            # Canvas is full-screen and rooted at (0,0) on the screen,
            # so root-coords == canvas-coords for our purposes.
            x0, y0 = sx, sy
            x1, y1 = ev.x_root, ev.y_root
            if state["rect_id"] is not None:
                canvas.delete(state["rect_id"])
            state["rect_id"] = canvas.create_rectangle(
                x0, y0, x1, y1, outline="#ff3030", width=3, fill="")
            if state["text_id"] is not None:
                canvas.delete(state["text_id"])
            w = abs(x1 - x0)
            h = abs(y1 - y0)
            tx = (x0 + x1) // 2
            ty = max(0, min(y0, y1) - 14)
            state["text_id"] = canvas.create_text(
                tx, ty, text=f"{w}\u00d7{h} px",
                font=("Helvetica", 11, "bold"),
                fill="#ff3030", anchor="s")

        def on_release(ev):
            if state["start"] is None:
                return
            sx, sy = state["start"]
            ex, ey = ev.x_root, ev.y_root
            x = min(sx, ex)
            y = min(sy, ey)
            w = abs(ex - sx)
            h = abs(ey - sy)
            if w < 5 or h < 5:
                # Misclick -- reset and let the user try again.
                state["start"] = None
                if state["rect_id"] is not None:
                    canvas.delete(state["rect_id"])
                    state["rect_id"] = None
                return
            self._result = (x, y, w, h)
            top.destroy()

        def on_escape(_ev):
            self._result = None
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", on_escape)
        top.protocol("WM_DELETE_WINDOW", lambda: on_escape(None))
        # Show the now-fully-configured borderless overlay. We deliberately
        # held it hidden via .withdraw() above so the user never sees the
        # brief moment when macOS would otherwise paint a title bar.
        top.deiconify()
        # Acquire keyboard / mouse focus so the bindings fire reliably.
        top.update()
        top.focus_force()
        canvas.focus_set()
        try:
            top.grab_set()  # Modal -- but DO NOT block other Tk windows
        except tk.TclError:
            pass
        top.wait_window()
        return self._result


# ---------------------------------------------------------------------------
# Main calibration entry point
# ---------------------------------------------------------------------------
def run_unified_calibration(root: tk.Tk,
                            adb: ADBController,
                            status_cb: Callable[[str], None],
                            n_iterations: int = 8,
                            settle_seconds: float = 1.0,
                            debug_dir: Optional[str] = None
                            ) -> Optional[Dict[str, Any]]:
    """
    Run the full unified calibration. Returns the affine dict or None.

    `root` must be the main GUI Tk root (so overlays can be created as
    Toplevels). Calls back into the OCR module for digit recognition.

    `n_iterations` >= 2 -- the more, the more accurate the least squares
    fit. Default is 4. Each iteration costs ~3 seconds plus user time to
    draw the rectangle.
    """
    # --- Sanity checks --------------------------------------------------
    deps = pat_ocr.is_available()
    if not all(deps.values()):
        status_cb("[!] OCR deps missing:\n" + pat_ocr.missing_deps_message())
        return None
    if adb.serial is None:
        status_cb("[!] ADB not connected.")
        return None
    if not adb.screen_size:
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size

    # --- Locate BlueStacks window once ---------------------------------
    bs_window = WindowLocator().find()
    if bs_window is None:
        messagebox.showerror(
            "BlueStacks window not found",
            "Could not locate the BlueStacks window via OS API.\n"
            "Make sure BlueStacks is running and visible, then try again.")
        return None
    status_cb(f"BS window: x={bs_window['x']} y={bs_window['y']} "
              f"w={bs_window['w']} h={bs_window['h']}")

    # --- Bring the in-game map to the same "ready" state fn 1 will use -
    # The main clicker runs an intro sequence (open Settings, zoom out,
    # swipe to recentre) before it starts spamming. The calibration MUST
    # be performed in EXACTLY the same map state, otherwise the saved
    # affine maps game->ADB for state A but fn 1 then taps in state B,
    # producing systematic misses across every saved point.
    #
    # We replay the same Settings->ZoomOut->swipe procedure here using
    # the existing calibration_zoom.json. If that file is missing the
    # user is asked to bring the map to its target state manually before
    # we begin tapping.
    zoom = load_json(ZOOM_CALIB_FILE)
    if zoom:
        try:
            settings_adb = tuple(zoom["settings_button"]["adb"])
            zoomout_adb = tuple(zoom["zoom_out_button"]["adb"])
            swipe = zoom["swipe"]
            status_cb("Running intro sequence (Settings -> ZoomOut -> swipe)...")
            adb.tap(*settings_adb)
            time.sleep(0.4)
            adb.tap(*zoomout_adb)
            time.sleep(0.4)
            adb.swipe(swipe["start"][0], swipe["start"][1],
                      swipe["end"][0], swipe["end"][1],
                      int(swipe.get("duration_ms", 400)))
            time.sleep(1.0)
            status_cb("Intro sequence done; map is in its post-zoom state.")
        except (KeyError, ValueError, TypeError) as e:
            status_cb(f"[!] calibration_zoom.json is malformed ({e}); "
                      "skipping intro sequence.")
    else:
        # No zoom config -- warn but proceed. The user may have set up
        # the map manually already.
        proceed = messagebox.askyesno(
            "No zoom calibration",
            "No calibration_zoom.json found. fn 1 normally zooms out and\n"
            "swipes the map before spamming. If you proceed now, the\n"
            "calibration will be taken at the CURRENT map state and\n"
            "will only be accurate if fn 1 sees the same state later.\n\n"
            "Continue with the current map state?")
        if not proceed:
            return None
        status_cb("Skipped intro sequence (no calibration_zoom.json).")

    # --- Ask the user where the playable map is after the swipe -------
    # The intro swipe shifts the map so it no longer fills the emulator.
    # We can't predict where exactly it ends up, so we ask the user to
    # outline the visible playable region ONCE. Tap candidates are then
    # generated INSIDE that rectangle.
    overlay = OverlayRectPicker(root)
    region_rect = overlay.pick(
        "Draw a rectangle around the VISIBLE PLAYABLE MAP "
        "(the area where map tiles are shown -- not UI bars). "
        "Taps will be placed inside this region.")
    if region_rect is None:
        status_cb("[!] Calibration cancelled at region-select step.")
        return None
    try:
        region_adb = screen_rect_to_adb_rect(
            region_rect, bs_window, adb_w, adb_h, pad_pct=0.0)
    except ValueError as e:
        status_cb(f"[!] {e}")
        return None
    rgx, rgy, rgw, rgh = region_adb
    status_cb(f"Playable region (ADB): x={rgx} y={rgy} w={rgw} h={rgh}")
    # Build candidate tap points INSIDE the region, with a 15% margin
    # from each edge so we don't accidentally tap on a UI element that
    # sits flush against the visible map.
    margin_x = int(rgw * 0.15)
    margin_y = int(rgh * 0.15)
    inner_x0 = rgx + margin_x
    inner_x1 = rgx + rgw - margin_x
    inner_y0 = rgy + margin_y
    inner_y1 = rgy + rgh - margin_y
    if inner_x1 - inner_x0 < 100 or inner_y1 - inner_y0 < 100:
        status_cb("[!] Region too small after applying 15% margins.")
        return None
    mid_x = (inner_x0 + inner_x1) // 2
    mid_y = (inner_y0 + inner_y1) // 2
    # 9 candidates in a 3x3 grid spanning the inner region. We start with
    # the four corners (best for affine fit), then the centre, then the
    # four mid-edges as fallbacks.
    candidates: List[Tuple[int, int]] = [
        (inner_x0, inner_y0),  # top-left corner
        (inner_x1, inner_y0),  # top-right corner
        (inner_x1, inner_y1),  # bottom-right corner
        (inner_x0, inner_y1),  # bottom-left corner
        (mid_x,    mid_y),     # centre
        (inner_x0, mid_y),     # mid-left edge
        (inner_x1, mid_y),     # mid-right edge
        (mid_x,    inner_y0),  # mid-top edge
        (mid_x,    inner_y1),  # mid-bottom edge
    ]
    status_cb(f"Generated 9 candidate tap points inside the region "
              f"(inner box x={inner_x0}..{inner_x1}, y={inner_y0}..{inner_y1})")
    pairs: List[Tuple[Tuple[int, int], Tuple[int, int]]] = []
    used_game_points: set = set()  # game-coord tuples we've already captured
    cand_idx = 0  # cursor into the candidates list
    last_good_roi: Optional[Tuple[int, int, int, int]] = None  # for fn 1 realign

    iter_n = max(2, n_iterations)
    iteration = 1
    while iteration <= iter_n:
        # Find the next candidate ADB point that hasn't been used yet.
        # If we run out of candidates -- abort with a clear message.
        if cand_idx >= len(candidates):
            status_cb("[!] Ran out of candidate tap points. Make sure the "
                      "game shows a coord readout for taps in the central "
                      "area of the map.")
            return None
        ax, ay = candidates[cand_idx]
        cand_idx += 1
        status_cb(f"--- Iteration {iteration}/{iter_n} ---")
        status_cb(f"ADB tap at ({ax}, {ay})")
        adb.tap(ax, ay)
        time.sleep(settle_seconds)
        rect = overlay.pick(
            f"Iteration {iteration}/{iter_n}: "
            f"Drag a rectangle around the x:N y:N field shown in the game.")
        if rect is None:
            status_cb("[!] Calibration cancelled by user.")
            return None
        sx, sy, sw, sh = rect
        status_cb(f"Screen rect: x={sx} y={sy} w={sw} h={sh}")
        try:
            adb_roi = screen_rect_to_adb_rect(rect, bs_window, adb_w, adb_h)
        except ValueError as e:
            status_cb(f"[!] {e}")
            return None
        status_cb(f"ADB ROI: x={adb_roi[0]} y={adb_roi[1]} "
                  f"w={adb_roi[2]} h={adb_roi[3]}")
        debug_path = (os.path.join(debug_dir, f"v2_iter_{iteration}")
                      if debug_dir else None)
        coords = _read_with_retry(adb, adb_roi, status_cb, debug_path)
        if coords is None:
            status_cb(f"[!] Iteration {iteration}: OCR failed, "
                      "trying next candidate tap point.")
            continue  # Same iteration index, next candidate
        gx, gy = coords
        # Detect "tap didn't move the readout": if the game-coord we just
        # OCR'd is identical to one we have already captured, this tap
        # landed on a non-interactive area (e.g. UI bar) and the readout
        # still shows the result of the previous successful tap. Skip
        # and try a different candidate.
        if (gx, gy) in used_game_points:
            status_cb(f"[!] Iteration {iteration}: game=({gx},{gy}) "
                      "matches a previous reading. Tap landed off-map; "
                      "trying next candidate.")
            continue
        status_cb(f"Iteration {iteration}: ADB=({ax},{ay}) -> game=({gx},{gy})")
        pairs.append(((ax, ay), (gx, gy)))
        used_game_points.add((gx, gy))
        # Remember the last ROI that produced a valid OCR -- it's the
        # most accurate snapshot of where the coord-readout actually
        # sits, and fn 1 will reuse it for its per-run realignment tap.
        last_good_roi = tuple(adb_roi)
        iteration += 1

    # --- Solve affine kx, ky, bx, by ------------------------------------
    if len(pairs) < 2:
        status_cb("[!] Need at least 2 pairs.")
        return None
    aff = _solve_affine(pairs, status_cb=status_cb)
    if aff is None:
        return None
    status_cb(f"Calibration: kx={aff['kx']:.4f} ky={aff['ky']:.4f} "
              f"bx={aff['bx']:.1f} by={aff['by']:.1f}")
    # Persist with full audit trail.
    out: Dict[str, Any] = dict(aff)
    out["method"] = "unified-v2-overlay-ocr"
    out["pairs"] = [
        {"adb": list(p[0]), "game": list(p[1])} for p in pairs
    ]
    out["bs_window"] = bs_window
    out["adb_w"] = adb_w
    out["adb_h"] = adb_h
    # Stash the last-known-good ROI so fn 1 can do a per-run realignment
    # tap+OCR before each spam session (compensates intro-swipe jitter).
    if last_good_roi is not None:
        out["coord_ocr_roi"] = list(last_good_roi)
    save_json(GAME_AFFINE_CALIB_FILE, out)
    status_cb(f"Saved to {GAME_AFFINE_CALIB_FILE}")
    return out


def _read_with_retry(adb: ADBController,
                     adb_roi: Tuple[int, int, int, int],
                     status_cb: Callable[[str], None],
                     debug_path: Optional[str]
                     ) -> Optional[Tuple[int, int]]:
    """
    Take a fresh emulator screencap, crop to adb_roi, OCR, parse. Retry
    up to 3 times with short waits between if the parse fails.
    """
    for attempt in range(1, 4):
        coords = pat_ocr.read_game_coords(
            adb.adb_path, adb.serial, adb_roi,
            debug_save_path=(f"{debug_path}_try{attempt}" if debug_path else None))
        if coords is not None:
            return coords
        status_cb(f"  OCR try {attempt} failed, retrying after 0.5s...")
        time.sleep(0.5)
    return None


def _linreg_1d(xs: List[float], ys: List[float]) -> Optional[Tuple[float, float, float]]:
    """One-dimensional linear regression y = k*x + b.

    Returns (k, b, max_residual) or None if degenerate.
    `max_residual` is the maximum |y_i - (k*x_i + b)| over the input set,
    in the same units as y. Used by the caller to decide if outlier
    rejection is needed.
    """
    n = len(xs)
    if n < 2:
        return None
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(xs[i] * ys[i] for i in range(n))
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        return None
    k = (n * sxy - sx * sy) / den
    b = (sy - k * sx) / n
    residuals = [ys[i] - (k * xs[i] + b) for i in range(n)]
    max_res = max(abs(r) for r in residuals)
    return float(k), float(b), float(max_res)


def _solve_affine(pairs: List[Tuple[Tuple[int, int], Tuple[int, int]]],
                  status_cb: Optional[Callable[[str], None]] = None
                  ) -> Optional[Dict[str, float]]:
    """
    Solve `adb = k * game + b` (independent on X and Y) by least squares.

    With Post Apo Tycoon's discrete tile readout, every (game_x, game_y)
    returned by OCR is rounded to a TILE centre, which adds up to 0.5
    tile of noise on each axis. A naive least-squares over a few samples
    can therefore swing both kx and the intercept by several ADB pixels.

    Mitigations applied here:
      1) Fit X and Y INDEPENDENTLY. The two axes are unrelated noise
         sources, so fitting them jointly buys nothing and can correlate
         their errors via shared denominator.
      2) Compute max residual and, if it's above a small threshold,
         iteratively REMOVE the worst-fitting point on each axis. This
         keeps the long-distance pairs (where rounding error is dwarfed
         by the signal) and drops any tap that landed in an off-tile UI
         element or where OCR misread a digit.
      3) Refuse to return a fit that still has > 5 ADB-px residual on
         either axis. Better to fail loudly than to silently save a bad
         calibration.
    """
    if len(pairs) < 2:
        return None
    gx = [float(p[1][0]) for p in pairs]
    ax = [float(p[0][0]) for p in pairs]
    gy = [float(p[1][1]) for p in pairs]
    ay = [float(p[0][1]) for p in pairs]

    def fit_axis(gs: List[float], asx: List[float],
                 label: str) -> Optional[Tuple[float, float, float, int]]:
        # Iteratively drop the single worst-residual point until either
        # max residual is small OR fewer than 3 points remain. Returns
        # (k, b, max_res, used_n).
        gs = list(gs)
        asx = list(asx)
        while True:
            res = _linreg_1d(gs, asx)
            if res is None:
                return None
            k, b, max_res = res
            if max_res <= 1.5 or len(gs) <= 3:
                return k, b, max_res, len(gs)
            # Find worst point and drop.
            worst_idx = 0
            worst_err = -1.0
            for i in range(len(gs)):
                err = abs(asx[i] - (k * gs[i] + b))
                if err > worst_err:
                    worst_err = err
                    worst_idx = i
            if status_cb:
                status_cb(f"  {label}: dropping pair#{worst_idx} "
                          f"(residual={worst_err:.2f} ADB-px)")
            del gs[worst_idx]
            del asx[worst_idx]

    fx = fit_axis(gx, ax, "X-axis")
    fy = fit_axis(gy, ay, "Y-axis")
    if fx is None or fy is None:
        return None
    kx, bx, res_x, used_x = fx
    ky, by, res_y, used_y = fy
    if status_cb:
        status_cb(f"X-axis: kx={kx:.4f} bx={bx:.2f} residual={res_x:.2f}px "
                  f"(used {used_x}/{len(pairs)} pairs)")
        status_cb(f"Y-axis: ky={ky:.4f} by={by:.2f} residual={res_y:.2f}px "
                  f"(used {used_y}/{len(pairs)} pairs)")
    # Hard refusal: a calibration this far off would be worse than nothing.
    if res_x > 5.0 or res_y > 5.0:
        if status_cb:
            status_cb(f"[!] Residuals too large -- calibration REJECTED.")
        return None
    return {"kx": float(kx), "ky": float(ky),
            "bx": float(bx), "by": float(by),
            "residual_x": float(res_x),
            "residual_y": float(res_y),
            "used_pairs_x": int(used_x),
            "used_pairs_y": int(used_y),
            "total_pairs": int(len(pairs))}


# ===========================================================================
# Per-run rebase: tighten bx, by right before the main clicker starts
# ===========================================================================
def auto_realign_bxby(adb: ADBController,
                      kx: float, ky: float, bx: float, by: float,
                      saved_roi: Optional[Tuple[int, int, int, int]] = None,
                      probe_game: Tuple[int, int] = (0, 0),
                      settle_seconds: float = 1.0,
                      status_cb: Optional[Callable[[str], None]] = None
                      ) -> Optional[Tuple[float, float]]:
    """
    Refresh (bx, by) by tapping ONE known point and reading the actual
    game-coordinate the game responds with. Returns the corrected
    (bx_new, by_new) or None on any failure.

    Why this matters: the intro swipe is implemented as `adb input swipe`
    which is NOT perfectly reproducible -- BlueStacks rounds and adds
    +/- a few pixels of jitter each time. The slope kx, ky between game
    and ADB stays constant (it's purely the camera zoom), but the origin
    bx, by drifts by the same few-pixel jitter on every intro. This
    function corrects exactly that drift in one extra ADB-tap + OCR.

    Arguments:
      saved_roi -- the same coord-readout ROI we saved during unified
                   calibration (from the GAME_AFFINE_CALIB_FILE if
                   available). If None, no realignment is performed.
      probe_game -- the game coordinate we expect at the probe site. The
                    default (0, 0) means "the centre of the map" which
                    is always inside the visible play area regardless
                    of zoom level.
    """
    def log(msg: str) -> None:
        if status_cb:
            status_cb(msg)

    if saved_roi is None:
        log("auto_realign: no saved ROI -- skipping realignment.")
        return None
    if adb.serial is None:
        log("auto_realign: ADB not connected.")
        return None
    # Where the affine SAYS probe_game should sit.
    pgx, pgy = probe_game
    target_ax = kx * pgx + bx
    target_ay = ky * pgy + by
    log(f"auto_realign: tapping ({target_ax:.0f},{target_ay:.0f}) for game={probe_game}")
    adb.tap(target_ax, target_ay)
    time.sleep(settle_seconds)

    # Robust OCR: try the saved ROI first, then progressively wider ROIs
    # centred on the same spot. The saved ROI was captured during the
    # unified calibration but the game UI element may have moved a few
    # pixels (or even relocated) due to zoom changes or screen layout
    # tweaks, so a tight crop can miss it. Each retry widens the search
    # window without going outside the emulator display.
    rx, ry, rw, rh = saved_roi
    rcx = rx + rw / 2.0
    rcy = ry + rh / 2.0

    def _try_ocr(name: str,
                 roi: Tuple[int, int, int, int]
                 ) -> Optional[Tuple[int, int]]:
        log(f"auto_realign: OCR attempt '{name}' roi={roi}")
        return pat_ocr.read_game_coords(adb.adb_path, adb.serial, roi)

    # Iterate over scale multipliers: 1x (exact saved ROI), 2x, 4x.
    coords: Optional[Tuple[int, int]] = None
    for mult, label in ((1.0, "saved"), (2.0, "2x"), (4.0, "4x")):
        new_w = max(rw, int(rw * mult))
        new_h = max(rh, int(rh * mult))
        new_x = int(rcx - new_w / 2.0)
        new_y = int(rcy - new_h / 2.0)
        # Clamp to emulator bounds if known.
        if adb.screen_size:
            scr_w, scr_h = adb.screen_size
            new_x = max(0, min(scr_w - 1, new_x))
            new_y = max(0, min(scr_h - 1, new_y))
            new_w = max(1, min(scr_w - new_x, new_w))
            new_h = max(1, min(scr_h - new_y, new_h))
        coords = _try_ocr(label, (new_x, new_y, new_w, new_h))
        if coords is not None:
            break

    if coords is None:
        log("auto_realign: OCR failed at all ROI scales -- keeping current bx, by.")
        log("              (the saved ROI may be stale; recalibrate to refresh)")
        return None

    actual_gx, actual_gy = coords
    # Actual ADB tap landed where we wanted (target_ax, target_ay). The
    # game reports it corresponds to (actual_gx, actual_gy) instead of
    # the expected (pgx, pgy). The affine should satisfy
    #     target_ax = kx * actual_gx + bx_new
    #     target_ay = ky * actual_gy + by_new
    # We solve for bx_new, by_new directly.
    bx_new = target_ax - kx * actual_gx
    by_new = target_ay - ky * actual_gy
    # Sanity check: a drift larger than 100 ADB px on either axis means
    # something else moved (e.g. zoom changed). Refuse to apply such a
    # huge correction; the user should recalibrate.
    if abs(bx_new - bx) > 100 or abs(by_new - by) > 100:
        log(f"auto_realign: REJECTED -- drift too large "
            f"(bx {bx_new - bx:+.0f}, by {by_new - by:+.0f}). "
            f"Probably zoom changed. Run a fresh calibration.")
        return None
    log(f"auto_realign: OCR={coords}, expected={probe_game}, "
        f"shift bx={bx_new - bx:+.2f} by={by_new - by:+.2f}")
    return float(bx_new), float(by_new)
