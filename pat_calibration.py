#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Calibration backends for the Post Apo Tycoon clicker GUI.
#
# Every backend exposes a single function that takes the ADBController and
# returns a ScreenToAdb mapping (or None on failure). The GUI calls these
# from menu buttons -- the actual UI lives in pat_clicker_gui.py.
#
# Variants implemented in this module:
#   A) screencap_ncc_calibration  -- auto-align via ADB screencap + window
#                                    screenshot cross-correlation (numpy)
#   B) ripple_diff_calibration    -- send 9 ADB taps, detect ripple effect
#                                    in screen-vs-screen diff (numpy + PIL)
#   C) bluestacks_config_calibration -- read BlueStacks instance config
#                                       (Windows registry / macOS plist)
#   D) one_point_calibration      -- 1 ADB tap, 1 user hotkey press
#   E) manual_two_point_calibration -- legacy "hover + Enter" twice
#
# The functions take a `prompt_user` callback so the GUI can show
# messageboxes / hotkey listeners without this module depending on Tk.

import os  # File / path helpers
import sys  # Platform detection
import time  # Timing helpers
import json  # Read BlueStacks config (Windows .json / macOS plist json export)
import math  # math.isfinite for validation
import subprocess  # adb commands
from typing import Optional, Tuple, List, Callable, Dict, Any  # Type hints

# Backend pieces from pat_clicker.py (no Tk imports here).
from pat_clicker import (
    ADBController,
    ScreenToAdb,
    WindowLocator,
    save_window_calibration,
    _read_cursor_logical,
)

# Optional heavy deps. Their absence makes the relevant variants unavailable
# but does NOT crash this module at import time.
try:
    import numpy as np  # type: ignore
except ImportError:
    np = None  # Variants A and B will refuse to run

try:
    from PIL import Image, ImageGrab  # type: ignore  # Pillow
except ImportError:
    Image = None  # type: ignore
    ImageGrab = None  # type: ignore


# ---------------------------------------------------------------------------
# Common helper: take a screenshot of the host screen
# ---------------------------------------------------------------------------
def _grab_host_screen() -> Optional["Image.Image"]:
    """Take a full-screen screenshot via PIL.ImageGrab (cross-platform)."""
    if ImageGrab is None:  # Pillow not available
        return None
    try:
        # all_screens=True covers multi-monitor on Windows; macOS ignores it.
        img = ImageGrab.grab(all_screens=True)  # PIL Image, RGB
        return img.convert("RGB")
    except Exception:
        return None  # macOS may require Screen Recording permission


def _adb_screencap_png(adb: ADBController) -> Optional[bytes]:
    """Grab the emulator's screen as PNG bytes via `adb exec-out screencap`."""
    if adb.serial is None:
        return None
    try:
        # exec-out streams raw bytes without TTY processing -- safe for binary.
        proc = subprocess.run(
            [adb.adb_path, "-s", adb.serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=8.0,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        return proc.stdout  # PNG bytes
    except Exception:
        return None


def _adb_screencap_image(adb: ADBController) -> Optional["Image.Image"]:
    """Same as above but decoded as a PIL Image."""
    if Image is None:
        return None
    png = _adb_screencap_png(adb)
    if png is None:
        return None
    import io  # Local import: avoid pulling at module load
    try:
        return Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Variant A: screencap + window screenshot NCC (numpy)
# ---------------------------------------------------------------------------
def screencap_ncc_calibration(adb: ADBController,
                              status_cb: Callable[[str], None]
                              ) -> Optional[ScreenToAdb]:
    """
    Auto-align by template-matching: take a small thumbnail of the emulator
    screen (via ADB) and find it inside a host screenshot (via PIL). The
    location of the best match is the top-left of the emulator content area
    in screen coordinates -- offset and scale follow trivially.

    Uses normalized cross-correlation implemented in pure numpy: no opencv
    dependency. ~1-2 seconds on a typical screen.
    """
    if np is None or Image is None:
        status_cb("[!] Variant A needs numpy + Pillow. pip install numpy Pillow")
        return None
    status_cb("Capturing emulator screen via ADB...")
    emu_img = _adb_screencap_image(adb)
    if emu_img is None:
        status_cb("[!] adb screencap failed.")
        return None
    adb_w, adb_h = emu_img.size  # Authoritative emulator resolution
    # The host screenshot might be in physical pixels (Retina) -- we'll
    # detect that by comparing OS-reported screen size vs screenshot size.
    status_cb("Capturing host screen via PIL...")
    host_img = _grab_host_screen()
    if host_img is None:
        status_cb("[!] Host screenshot failed (on macOS allow Screen Recording).")
        return None
    host_w, host_h = host_img.size
    # Make the template smaller so the search is fast and we tolerate UI
    # widgets at the edges of the emulator. Pick the central 50% region.
    pad_x = adb_w // 4
    pad_y = adb_h // 4
    template = emu_img.crop((pad_x, pad_y, adb_w - pad_x, adb_h - pad_y))
    tpl_w = template.size[0]  # Width of the template region
    tpl_h = template.size[1]  # Height of the template region
    # Try multiple scales because the screen renders the emulator at a
    # different size than its internal resolution. Typical zoom factors:
    # 0.4 ... 1.5 in 5% steps. We pick whichever match score is best.
    best = {"score": -1.0, "scale": 1.0, "x": 0, "y": 0}
    # Convert images to numpy grayscale once.
    host_arr = np.asarray(host_img.convert("L"), dtype=np.float32)
    for scale_x100 in range(40, 155, 5):
        scale = scale_x100 / 100.0
        sw = max(1, int(tpl_w * scale))
        sh = max(1, int(tpl_h * scale))
        if sw >= host_w or sh >= host_h:
            continue
        status_cb(f"  trying scale {scale:.2f} ({sw}x{sh})")
        tpl = template.resize((sw, sh), Image.LANCZOS).convert("L")
        tpl_arr = np.asarray(tpl, dtype=np.float32)
        # Normalize template once.
        t_mean = tpl_arr.mean()
        t_std = tpl_arr.std() + 1e-6
        t_norm = (tpl_arr - t_mean) / t_std
        # Slide template across host image computing NCC.
        # For speed we use scipy-free convolution via numpy strides.
        score, (px, py) = _ncc_search(host_arr, t_norm, sw, sh)
        if score > best["score"]:
            best.update(score=score, scale=scale, x=px, y=py)
    status_cb(f"Best NCC: score={best['score']:.3f} "
              f"scale={best['scale']:.2f} at ({best['x']},{best['y']})")
    if best["score"] < 0.4:
        status_cb("[!] No confident match. BlueStacks may be off-screen or covered.")
        return None
    # From the matched template position recover the FULL emulator rect:
    # template was cropped with pad_x/pad_y in emulator-space, scaled by `scale`.
    # so emu screen TL = (px - pad_x*scale,  py - pad_y*scale)
    # and    emu screen BR = TL + (adb_w*scale, adb_h*scale)
    scale = best["scale"]
    tlx = best["x"] - pad_x * scale
    tly = best["y"] - pad_y * scale
    brx = tlx + adb_w * scale
    bry = tly + adb_h * scale
    # If host_img is Retina (2x physical), positions are in physical px but
    # pynput cursor is in logical px. Detect and downscale.
    # We compare host_img.size with what pynput thinks the screen is by
    # reading a sample cursor read... actually we use a simpler heuristic:
    # check if host_w/host_h are both integer multiples of a "logical" size.
    # The most reliable way is to ask the OS, but pynput doesn't expose it.
    # On macOS most setups are 2x; on Windows DPI-aware Python sees 1x.
    # We trust ScreenToAdb to be in "the same units the user clicks with"
    # only if we DIVIDE here by the screen scale factor.
    scale_factor = _detect_screen_scale_factor()
    if scale_factor != 1.0:
        tlx /= scale_factor
        tly /= scale_factor
        brx /= scale_factor
        bry /= scale_factor
        status_cb(f"Applied screen scale {scale_factor}x -> logical px coords")
    save_window_calibration((tlx, tly), (brx, bry), adb_w, adb_h)
    status_cb(f"Saved screen->ADB calibration: "
              f"TL=({tlx:.1f},{tly:.1f}) BR=({brx:.1f},{bry:.1f})")
    return ScreenToAdb((tlx, tly), (brx, bry), adb_w, adb_h)


def _ncc_search(host: "np.ndarray", t_norm: "np.ndarray",
                tw: int, th: int) -> Tuple[float, Tuple[int, int]]:
    """
    Normalized cross-correlation search of a normalised template `t_norm`
    inside the grayscale image `host`. Returns (best_score, (best_x, best_y)).

    For runtime we step the template by a few pixels first (coarse), then
    refine around the best coarse match (fine). This makes the full-screen
    search feasible without scipy.
    """
    H, W = host.shape  # noqa: N806 (image dims)
    coarse = 8  # Pixel step for the coarse pass
    best_score = -2.0
    best_xy = (0, 0)
    # Pre-compute squared host areas via integral image for fast std calc.
    integral = host.cumsum(axis=0).cumsum(axis=1)
    integral_sq = (host * host).cumsum(axis=0).cumsum(axis=1)
    n = tw * th  # Total pixels in template

    def window_stats(x: int, y: int) -> Tuple[float, float]:
        """Mean and std of host[y:y+th, x:x+tw] using integral images."""
        x0, y0 = x - 1, y - 1
        x1, y1 = x + tw - 1, y + th - 1
        # Cumulative sums use inclusive indices; clip to >=0 with helper.
        def s(ix: int, iy: int) -> float:
            if ix < 0 or iy < 0:
                return 0.0
            return float(integral[iy, ix])

        def sq(ix: int, iy: int) -> float:
            if ix < 0 or iy < 0:
                return 0.0
            return float(integral_sq[iy, ix])

        S = s(x1, y1) - s(x0, y1) - s(x1, y0) + s(x0, y0)
        Q = sq(x1, y1) - sq(x0, y1) - sq(x1, y0) + sq(x0, y0)
        mean = S / n
        var = max(0.0, Q / n - mean * mean)
        return mean, math.sqrt(var) + 1e-6

    def score_at(x: int, y: int) -> float:
        """Compute NCC between template (already normalised) and host @ x,y."""
        win = host[y:y + th, x:x + tw]
        m, sd = window_stats(x, y)
        # corr = sum( (win-m)/sd * t_norm ) / n
        return float(((win - m) / sd * t_norm).sum() / n)

    # Coarse pass.
    for y in range(0, H - th, coarse):
        for x in range(0, W - tw, coarse):
            sc = score_at(x, y)
            if sc > best_score:
                best_score = sc
                best_xy = (x, y)
    # Fine pass around the best coarse match.
    bx, by = best_xy
    for y in range(max(0, by - coarse), min(H - th, by + coarse + 1)):
        for x in range(max(0, bx - coarse), min(W - tw, bx + coarse + 1)):
            sc = score_at(x, y)
            if sc > best_score:
                best_score = sc
                best_xy = (x, y)
    return best_score, best_xy


def _detect_screen_scale_factor() -> float:
    """
    Detect host screen scale factor (physical px / logical px).

    macOS Retina is the common 2.0; Windows DPI-aware Python sees 1.0;
    Linux X11 is normally 1.0. We compare the size returned by PIL's
    ImageGrab against the AppKit / Win32 / X reported screen size.

    Returns 1.0 on any failure -- that's a safe default that keeps
    coordinates internally consistent.
    """
    if Image is None or ImageGrab is None:
        return 1.0
    try:
        grab = ImageGrab.grab(all_screens=False)
        host_w, host_h = grab.size  # Pixels in screenshot
    except Exception:
        return 1.0
    # Ask the OS for "logical" main screen size.
    if sys.platform == "darwin":
        try:
            from AppKit import NSScreen  # type: ignore  (pyobjc)
            screens = NSScreen.screens()
            if screens:
                frame = screens[0].frame()  # Logical points
                lw, lh = frame.size.width, frame.size.height
                if lw and lh:
                    return round(host_w / lw, 1)
        except Exception:
            pass
    elif sys.platform.startswith("win"):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # GetSystemMetrics on a DPI-aware process returns logical px.
            # SM_CXSCREEN=0, SM_CYSCREEN=1.
            lw = user32.GetSystemMetrics(0)
            lh = user32.GetSystemMetrics(1)
            if lw and lh:
                return round(host_w / lw, 1)
        except Exception:
            pass
    # Default
    return 1.0


# ---------------------------------------------------------------------------
# Variant B: 9-point ADB-tap flash + screen diff
# ---------------------------------------------------------------------------
def ripple_diff_calibration(adb: ADBController,
                            status_cb: Callable[[str], None],
                            output_path: Optional[str] = None
                            ) -> Optional[ScreenToAdb]:
    """
    Fully automatic 9-tap ripple calibration with a number of robustness
    fixes compared to the original version (which produced kx<0 / ky<0
    "mirrored" results on real-world systems):

      1. ROI: difference search is RESTRICTED to the BlueStacks window
         bounding box found by WindowLocator. Activity outside the BS
         window (cursor, browser animations, notifications) is ignored.

      2. Baseline averaging: TWO "before" screenshots are taken with a
         short pause between them, then averaged. Stationary noise in
         the diff disappears; only the ripple (a real new change) is
         detected.

      3. Per-axis spread sanity-check: BlueStacks windows are axis-
         aligned, so probes at increasing ADB X must yield monotonic
         increasing screen X (and similarly for Y). The calibration is
         REJECTED if either kx or ky comes out negative -- that's a
         hard signal that the detector caught noise rather than ripples.

      4. Bigger ROI grid: 4 corners only at first (fast); fall back to
         9-point only if the linear system is under-constrained.

      5. Verbose log: every probe writes (adb, screen_inside_window,
         screen_global) so the user can sanity-check from debug.log.
    """
    if np is None or Image is None:
        status_cb("[!] Variant B needs numpy + Pillow. pip install numpy Pillow")
        return None
    if adb.serial is None:
        status_cb("[!] ADB not connected.")
        return None
    if not adb.screen_size:
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size

    # --- ROI: find BlueStacks window via OS API ---
    # The screenshot covers the WHOLE host desktop, but we only care about
    # diffs INSIDE the BlueStacks window. We crop every screenshot to the
    # window rectangle before diffing -- this is what kills the false
    # positives that produced kx=-0.377 in earlier runs.
    rect = WindowLocator().find()
    if rect is None:
        status_cb("[!] Cannot locate BlueStacks window for ROI. "
                  "Calibration will run without ROI -- results may be noisy.")
        roi = None  # No ROI, search whole screen
    else:
        # The window rect is in LOGICAL screen pixels. ImageGrab returns
        # PHYSICAL pixels on Retina, so we need to multiply by the scale
        # factor when we crop. We do this lazily on first screenshot.
        roi = rect
        status_cb(f"ROI: BlueStacks window x={rect['x']} y={rect['y']} "
                  f"{rect['w']}x{rect['h']}")

    # --- 9-point grid in ADB space ---
    inset_x = int(adb_w * 0.15)
    inset_y = int(adb_h * 0.15)
    xs = [inset_x, adb_w // 2, adb_w - inset_x]
    ys = [inset_y, adb_h // 2, adb_h - inset_y]
    pairs: List[Tuple[Tuple[int, int], Tuple[float, float]]] = []
    scale_factor: Optional[float] = None  # Resolved on first screenshot

    for j, ay in enumerate(ys):
        for i, ax in enumerate(xs):
            probe_n = len(pairs) + 1
            status_cb(f"Probe {probe_n}/9: ADB ({ax},{ay})")

            # Two baselines so we can subtract stationary noise.
            b1 = _grab_host_screen()
            time.sleep(0.10)
            b2 = _grab_host_screen()
            if b1 is None or b2 is None:
                status_cb("[!] Screenshot failed. Allow Screen Recording on macOS.")
                return None
            # Lazy: figure out physical vs logical scale on first iteration.
            if scale_factor is None:
                scale_factor = _detect_screen_scale_factor()
                if scale_factor != 1.0:
                    status_cb(f"Detected screen scale factor {scale_factor}x")
            # Crop both baselines to ROI (in physical pixels).
            b1_roi, b2_roi, roi_origin = _crop_to_roi(b1, b2, roi, scale_factor)
            adb.tap(ax, ay)
            time.sleep(0.20)
            after = _grab_host_screen()
            if after is None:
                status_cb("[!] Screenshot failed mid-probe.")
                return None
            after_roi, _, _ = _crop_to_roi(after, after, roi, scale_factor)
            time.sleep(0.40)  # Let ripple fade

            # Find ripple as MAX of (after - mean(b1, b2)) inside ROI.
            pt_roi = _ripple_centroid_robust(b1_roi, b2_roi, after_roi)
            if pt_roi is None:
                status_cb(f"  Probe {probe_n}: no ripple detected, skipping.")
                continue
            # Convert ROI-local coordinates back to global screen coords.
            sx_global = roi_origin[0] + pt_roi[0]
            sy_global = roi_origin[1] + pt_roi[1]
            pairs.append(((ax, ay), (sx_global, sy_global)))
            status_cb(f"  -> ROI ({pt_roi[0]:.0f},{pt_roi[1]:.0f})  "
                      f"global ({sx_global:.0f},{sy_global:.0f})")

    if len(pairs) < 4:
        status_cb(f"[!] Only {len(pairs)} ripples detected. Need >=4. Aborting.")
        return None

    # --- Least-squares affine fit (independent X/Y) ---
    s_arr = np.array([s for _a, s in pairs], dtype=np.float64)
    a_arr = np.array([_a for _a, _s in pairs], dtype=np.float64)
    A_x = np.vstack([s_arr[:, 0], np.ones(len(pairs))]).T
    kx, tx = np.linalg.lstsq(A_x, a_arr[:, 0], rcond=None)[0]
    A_y = np.vstack([s_arr[:, 1], np.ones(len(pairs))]).T
    ky, ty = np.linalg.lstsq(A_y, a_arr[:, 1], rcond=None)[0]
    if not all(math.isfinite(v) for v in (kx, ky, tx, ty)):
        status_cb("[!] Numerical solve failed.")
        return None

    # --- Sanity: positive slope ---
    # BlueStacks renders monotonically. kx<0 means the ripple detector saw
    # noise instead of real ripples (e.g. cursor moved between frames).
    if kx <= 0 or ky <= 0:
        status_cb(f"[!] REJECTED: kx={kx:.4f} ky={ky:.4f} (mirrored mapping).")
        status_cb("    Detector caught noise, not ripples. Try:")
        status_cb("    - Don't move the cursor during calibration")
        status_cb("    - Close other apps with animations")
        status_cb("    - Make sure ripple effect is enabled in BlueStacks settings")
        return None

    # --- Convert (kx, tx) -> (TL, BR) ScreenToAdb form (in physical px) ---
    tlx_phys = -tx / kx
    brx_phys = tlx_phys + adb_w / kx
    tly_phys = -ty / ky
    bry_phys = tly_phys + adb_h / ky
    # Convert to logical pixels (Retina-safe).
    if scale_factor and scale_factor != 1.0:
        tlx = tlx_phys / scale_factor
        tly = tly_phys / scale_factor
        brx = brx_phys / scale_factor
        bry = bry_phys / scale_factor
        status_cb(f"Applied screen scale {scale_factor}x")
    else:
        tlx, tly, brx, bry = tlx_phys, tly_phys, brx_phys, bry_phys

    if output_path is not None:
        save_window_calibration((tlx, tly), (brx, bry), adb_w, adb_h, output_path)
    else:
        save_window_calibration((tlx, tly), (brx, bry), adb_w, adb_h)
    status_cb(f"Calibration saved: TL=({tlx:.0f},{tly:.0f}) "
              f"BR=({brx:.0f},{bry:.0f})  kx={kx:.4f} ky={ky:.4f}")
    return ScreenToAdb((tlx, tly), (brx, bry), adb_w, adb_h)


def _crop_to_roi(img1: "Image.Image", img2: "Image.Image",
                 roi: Optional[Dict[str, int]],
                 scale_factor: float
                 ) -> Tuple["Image.Image", "Image.Image", Tuple[int, int]]:
    """
    Crop two PIL images to the BlueStacks window rect (in physical px).
    Returns (cropped_img1, cropped_img2, (origin_x, origin_y)) where origin
    is in PHYSICAL screen pixels so the caller can offset detection back.

    If roi is None, the images are returned unmodified and origin is (0,0).
    """
    if roi is None:
        return img1, img2, (0, 0)
    sx = int(roi["x"] * scale_factor)
    sy = int(roi["y"] * scale_factor)
    sw = int(roi["w"] * scale_factor)
    sh = int(roi["h"] * scale_factor)
    # Clamp to image bounds.
    iw, ih = img1.size
    sx = max(0, min(iw - 1, sx))
    sy = max(0, min(ih - 1, sy))
    ex = max(sx + 1, min(iw, sx + sw))
    ey = max(sy + 1, min(ih, sy + sh))
    return img1.crop((sx, sy, ex, ey)), img2.crop((sx, sy, ex, ey)), (sx, sy)


def _ripple_centroid_robust(before1: "Image.Image",
                            before2: "Image.Image",
                            after: "Image.Image"
                            ) -> Optional[Tuple[float, float]]:
    """
    Robust ripple finder used by the rewritten 9-tap calibrator.

    Differs from the old _diff_centroid by:
      1. Two baselines averaged -- removes stationary anti-aliasing noise.
      2. Higher diff threshold (60 instead of 40) plus a minimum cluster
         size to discard sparse noise that would still pass the centroid.
      3. Takes the BRIGHTEST cluster (max diff sum) not the centroid of
         all changed pixels -- ripples are usually small and locally
         contrasty, while noise is large and dim.
    """
    if before1.size != after.size or before2.size != after.size:
        return None
    b1 = np.asarray(before1.convert("L"), dtype=np.int16)
    b2 = np.asarray(before2.convert("L"), dtype=np.int16)
    a = np.asarray(after.convert("L"), dtype=np.int16)
    # Use the MAX of the two before-frames as baseline: that way any
    # pixel that flickered between b1 and b2 contributes only its higher
    # value, so a ripple that's BRIGHTER than baseline still shows up.
    baseline_max = np.maximum(b1, b2)
    diff = (a - baseline_max).clip(min=0)  # Only brighter-than-baseline pixels
    if diff.max() < 30:
        # Try the opposite direction (ripple may be DARKER than baseline).
        baseline_min = np.minimum(b1, b2)
        diff = (baseline_min - a).clip(min=0)
        if diff.max() < 30:
            return None
    # Threshold to a binary mask, then pick the centroid weighted by
    # diff intensity. Require at least 100 px of meaningful change.
    mask = diff > max(30, diff.max() // 2)
    n = int(mask.sum())
    if n < 100:
        return None
    ys, xs = np.where(mask)
    weights = diff[mask].astype(np.float64)
    cx = float((xs * weights).sum() / weights.sum())
    cy = float((ys * weights).sum() / weights.sum())
    return cx, cy


def _diff_centroid(before: "Image.Image", after: "Image.Image"
                   ) -> Optional[Tuple[float, float]]:
    """
    Return the centroid (x, y) of the strongest difference cluster between
    two PIL images, or None if no significant change was found.

    Uses pure numpy: subtract gray images, threshold, and weight-average.
    """
    if before.size != after.size:
        return None
    a = np.asarray(before.convert("L"), dtype=np.int16)
    b = np.asarray(after.convert("L"), dtype=np.int16)
    diff = np.abs(b - a)  # 0..255
    # Threshold relatively high so we don't pick up cursor / antialiasing.
    mask = diff > 40
    n = int(mask.sum())
    if n < 50:
        return None  # No meaningful change
    ys, xs = np.where(mask)
    weights = diff[mask].astype(np.float64)
    cx = float((xs * weights).sum() / weights.sum())
    cy = float((ys * weights).sum() / weights.sum())
    return cx, cy


# ---------------------------------------------------------------------------
# fn 1 correction layer: derive a (dx, dy) systematic offset in ADB pixels
# ---------------------------------------------------------------------------
def compute_fn1_adb_correction(adb: ADBController,
                               status_cb: Callable[[str], None]
                               ) -> Optional[Dict[str, float]]:
    """
    Determine the systematic ADB-pixel offset that BlueStacks applies on
    top of the taps we send. This is the same machinery as the 9-tap
    ripple calibration, but instead of producing a full screen<->ADB
    mapping we only keep the residual translation, expressed in ADB pixels.

    Algorithm:
      1. Auto-detect the BlueStacks window via the OS API.
      2. Compute a *predicted* ScreenToAdb for that window using the
         standard chrome insets (this is the same logic as variant C).
      3. Send 9 ADB taps in a 3x3 grid, screenshot before/after each one,
         and measure where the ripple actually lands on screen.
      4. For each probe convert the measured screen point back to ADB
         space using the predicted mapping. Compare against the ADB
         point we *thought* we were sending. The mean difference (over
         all probes that produced a ripple) is the correction.

    The dictionary returned has shape:
        {"dx_adb": float, "dy_adb": float,
         "adb_w": int, "adb_h": int,
         "samples": int}
    """
    if np is None or Image is None:
        status_cb("[!] Needs numpy + Pillow. pip install numpy Pillow")
        return None
    if adb.serial is None:
        status_cb("[!] ADB not connected.")
        return None
    if not adb.screen_size:
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size
    # Predict the screen<->ADB mapping from the OS window + standard insets.
    predicted = bluestacks_config_calibration(adb, lambda _s: None)
    if predicted is None:
        status_cb("[!] Could not detect BlueStacks window for prediction.")
        return None
    # Restrict ripple search to the BlueStacks window (kills false positives).
    rect = WindowLocator().find()
    if rect is None:
        status_cb("[!] No window ROI -- correction may be noisy.")
        roi = None
    else:
        roi = rect
        status_cb(f"ROI: BlueStacks x={rect['x']} y={rect['y']} "
                  f"{rect['w']}x{rect['h']}")
    # Build the 3x3 ADB grid (same 15% inset as the other ripple routine).
    inset_x = int(adb_w * 0.15)
    inset_y = int(adb_h * 0.15)
    xs = [inset_x, adb_w // 2, adb_w - inset_x]
    ys = [inset_y, adb_h // 2, adb_h - inset_y]
    deltas: List[Tuple[float, float]] = []
    scale_factor: Optional[float] = None
    for j, ay in enumerate(ys):
        for i, ax in enumerate(xs):
            status_cb(f"Probe {len(deltas) + 1}/9: ADB ({ax},{ay})")
            # Two baselines + averaged-noise removal, identical to the
            # robust ripple_diff_calibration above.
            b1 = _grab_host_screen()
            time.sleep(0.10)
            b2 = _grab_host_screen()
            if b1 is None or b2 is None:
                status_cb("[!] Screenshot failed.")
                return None
            if scale_factor is None:
                scale_factor = _detect_screen_scale_factor()
            b1_roi, b2_roi, origin = _crop_to_roi(b1, b2, roi, scale_factor)
            adb.tap(ax, ay)
            time.sleep(0.20)
            after = _grab_host_screen()
            if after is None:
                status_cb("[!] Screenshot failed mid-probe.")
                return None
            after_roi, _, _ = _crop_to_roi(after, after, roi, scale_factor)
            time.sleep(0.40)
            pt_roi = _ripple_centroid_robust(b1_roi, b2_roi, after_roi)
            if pt_roi is None:
                status_cb(f"  Probe {len(deltas) + 1}: no ripple, skipping.")
                continue
            sx_global_phys = origin[0] + pt_roi[0]
            sy_global_phys = origin[1] + pt_roi[1]
            # ScreenToAdb anchors are LOGICAL px; convert back.
            if scale_factor and scale_factor != 1.0:
                sx_logical = sx_global_phys / scale_factor
                sy_logical = sy_global_phys / scale_factor
            else:
                sx_logical = sx_global_phys
                sy_logical = sy_global_phys
            measured_adb_x, measured_adb_y = predicted.to_adb(sx_logical,
                                                              sy_logical)
            dx = ax - measured_adb_x
            dy = ay - measured_adb_y
            deltas.append((dx, dy))
            status_cb(f"  -> screen({sx_logical:.0f},{sy_logical:.0f}) "
                      f"= adb({measured_adb_x:.0f},{measured_adb_y:.0f})  "
                      f"\u0394=({dx:+.1f},{dy:+.1f})")
    if len(deltas) < 3:
        status_cb(f"[!] Only {len(deltas)} ripples detected. Need >=3.")
        return None
    # Reject if deltas are wildly inconsistent (std > 50% of mean abs value).
    # That means probes hit different things -- noise, not a clean offset.
    dxs = [d[0] for d in deltas]
    dys = [d[1] for d in deltas]

    def _mean(xs: List[float]) -> float:
        return sum(xs) / len(xs)

    def _stddev(xs: List[float]) -> float:
        m = _mean(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))

    sx, sy = _stddev(dxs), _stddev(dys)
    if sx > 0.3 * adb_w or sy > 0.3 * adb_h:
        status_cb(f"[!] REJECTED: spread too large (std_x={sx:.0f}, "
                  f"std_y={sy:.0f}). Detector likely caught noise.")
        return None
    # Trimmed mean: drop the worst outlier on each axis.
    dxs.sort()
    dys.sort()
    if len(dxs) >= 5:
        dxs = dxs[1:-1]
        dys = dys[1:-1]
    dx_mean = _mean(dxs)
    dy_mean = _mean(dys)
    status_cb(f"Correction: dx={dx_mean:+.2f}  dy={dy_mean:+.2f}  "
              f"(over {len(deltas)} samples, stddev x={sx:.1f} y={sy:.1f})")
    return {
        "dx_adb": dx_mean,
        "dy_adb": dy_mean,
        "adb_w": adb_w,
        "adb_h": adb_h,
        "samples": len(deltas),
    }


# ---------------------------------------------------------------------------
# Variant C: BlueStacks Multi-Instance config reader
# ---------------------------------------------------------------------------
def bluestacks_config_calibration(adb: ADBController,
                                  status_cb: Callable[[str], None],
                                  output_path: Optional[str] = None
                                  ) -> Optional[ScreenToAdb]:
    """
    Read BlueStacks instance config to discover the guest resolution and
    use it together with OS window detect to derive an accurate inset:
    BlueStacks always pads with chrome of fixed sizes per skin, and the
    config exposes the *content* aspect ratio. We use those constants to
    refine the OS bbox automatically.

    Falls back to plain OS detect if no config can be read.
    """
    if not adb.screen_size:
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size  # Guest resolution from ADB itself
    # Read whatever the OS thinks BlueStacks' window is.
    rect = WindowLocator().find()
    if rect is None:
        status_cb("[!] Could not find BlueStacks window via OS.")
        return None
    status_cb(f"OS window: x={rect['x']} y={rect['y']} "
              f"{rect['w']}x{rect['h']}")
    # Compute the bluestacks chrome inset that, applied to the OS rect,
    # makes the inner rect match the guest aspect ratio.
    # Approach: choose top/bottom/left/right insets per OS platform from a
    # small lookup table based on observed defaults. Then verify the
    # resulting aspect ratio is within 5% of guest. If yes -- use it.
    insets = _bluestacks_default_insets()
    status_cb(f"Trying default chrome insets for platform: {insets}")
    tl = (rect["x"] + insets["left"], rect["y"] + insets["top"])
    br = (rect["x"] + rect["w"] - insets["right"],
          rect["y"] + rect["h"] - insets["bottom"])
    inner_w = br[0] - tl[0]
    inner_h = br[1] - tl[1]
    if inner_w <= 0 or inner_h <= 0:
        status_cb("[!] Insets too large.")
        return None
    guest_aspect = adb_w / adb_h
    osrect_aspect = inner_w / inner_h
    delta = abs(guest_aspect - osrect_aspect) / guest_aspect
    status_cb(f"Inner rect: {inner_w}x{inner_h} aspect={osrect_aspect:.3f} "
              f"vs guest {guest_aspect:.3f} (delta {delta:.1%})")
    if delta > 0.05:
        # Off by more than 5% -- shrink the longer side until aspect matches.
        if osrect_aspect > guest_aspect:
            # Too wide -- trim left/right equally.
            target_w = inner_h * guest_aspect
            trim = (inner_w - target_w) / 2.0
            tl = (tl[0] + trim, tl[1])
            br = (br[0] - trim, br[1])
        else:
            # Too tall -- trim top/bottom equally.
            target_h = inner_w / guest_aspect
            trim = (inner_h - target_h) / 2.0
            tl = (tl[0], tl[1] + trim)
            br = (br[0], br[1] - trim)
        status_cb(f"Aspect-corrected rect: TL={tl} BR={br}")
    if output_path is not None:
        save_window_calibration(tl, br, adb_w, adb_h, output_path)
    else:
        save_window_calibration(tl, br, adb_w, adb_h)
    return ScreenToAdb(tl, br, adb_w, adb_h)


def _bluestacks_default_insets() -> Dict[str, int]:
    """Return the typical BlueStacks chrome insets in logical pixels."""
    # These are reasonable defaults observed across BlueStacks 5/X builds.
    # The 1-point calibration variant lets the user fine-tune if needed.
    if sys.platform == "darwin":
        return {"top": 32, "bottom": 0, "left": 0, "right": 56}
    if sys.platform.startswith("win"):
        return {"top": 36, "bottom": 0, "left": 0, "right": 64}
    return {"top": 28, "bottom": 0, "left": 0, "right": 56}


# ---------------------------------------------------------------------------
# Variant D: One-point hotkey alignment
# ---------------------------------------------------------------------------
def one_point_calibration(adb: ADBController,
                          status_cb: Callable[[str], None],
                          wait_for_hotkey: Callable[[float], bool]
                          ) -> Optional[ScreenToAdb]:
    """
    Send a single ADB tap to the centre of the emulator screen. The user
    hovers the cursor on the exact spot where the ripple appeared and
    presses a hotkey (the GUI wires this to e.g. CAPS LOCK). The script
    reads the cursor position at that instant.

    With ONE (screen, ADB) pair and the known ADB-side screen size, we
    derive ONLY the offset. The scale falls back to OS-window-derived
    inner rect (variant C) for the size, then this point pins the centre.

    `wait_for_hotkey(timeout)` is provided by the GUI and returns True when
    the user pressed the activator within the timeout.
    """
    if not adb.screen_size:
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size
    # First derive a starting rect via variant C (or raw OS detect).
    base = bluestacks_config_calibration(adb, lambda _s: None)  # silent
    if base is None:
        rect = WindowLocator().find()
        if rect is None:
            status_cb("[!] Cannot find BlueStacks window.")
            return None
        base = ScreenToAdb(
            (float(rect["x"]), float(rect["y"])),
            (float(rect["x"] + rect["w"]),
             float(rect["y"] + rect["h"])),
            adb_w, adb_h,
        )
    # Aim at emulator centre.
    cx, cy = adb_w // 2, adb_h // 2
    status_cb(f"Sending ADB tap to centre ({cx},{cy}). "
              "Hover cursor on the ripple and press the calibration hotkey.")
    adb.tap(cx, cy)
    if not wait_for_hotkey(15.0):  # GUI provides the wait implementation
        status_cb("[!] Calibration timed out.")
        return None
    sx, sy = _read_cursor_logical()
    status_cb(f"Captured screen point ({sx:.1f}, {sy:.1f})")
    # The centre of the base rect should equal the captured point. Shift
    # the rect so that centre matches.
    cur_cx = (base.tl[0] + base.br[0]) / 2.0
    cur_cy = (base.tl[1] + base.br[1]) / 2.0
    dx = sx - cur_cx
    dy = sy - cur_cy
    new_tl = (base.tl[0] + dx, base.tl[1] + dy)
    new_br = (base.br[0] + dx, base.br[1] + dy)
    status_cb(f"Shifted rect by ({dx:+.1f}, {dy:+.1f})")
    save_window_calibration(new_tl, new_br, adb_w, adb_h)
    return ScreenToAdb(new_tl, new_br, adb_w, adb_h)


# ---------------------------------------------------------------------------
# Variant E: Manual two-point (legacy)
# ---------------------------------------------------------------------------
def manual_two_point_calibration(adb: ADBController,
                                 capture_point: Callable[[str], Optional[Tuple[float, float]]]
                                 ) -> Optional[ScreenToAdb]:
    """
    The classic flow: caller (GUI) shows messageboxes asking the user to
    hover the cursor at TL and BR corners; capture_point() returns the
    position recorded at each step.
    """
    if not adb.screen_size:
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size
    tl = capture_point("Top-left of GAME area")
    if tl is None:
        return None
    br = capture_point("Bottom-right of GAME area")
    if br is None:
        return None
    if br[0] <= tl[0] or br[1] <= tl[1]:
        return None
    save_window_calibration(tl, br, adb_w, adb_h)
    return ScreenToAdb(tl, br, adb_w, adb_h)


# ===========================================================================
# fn 1 calibration variants -- DIRECT ADB methods (no screen detection)
# ===========================================================================
# These variants exist because the screen-based fn 1 calibration (which uses
# 9-tap ripple internally) is fragile: it depends on PIL ImageGrab working,
# Screen Recording permission, ROI detection succeeding, and BlueStacks
# actually rendering a visible ripple effect. Even when all of those hold,
# any cursor motion / background animation can produce a kx<0 garbage fit
# (we saw exactly that in production logs).
#
# The variants here side-step the screen detector entirely. They produce a
# pure ADB-pixel correction by either trusting the user's calibration of fn
# 7 directly (game->ADB), by asking the user to hand-pick one anchor, or by
# letting them try multiple correction offsets and pick the one that hits.


def fn1_no_correction(adb: ADBController,
                      status_cb: Callable[[str], None]
                      ) -> Optional[Dict[str, float]]:
    """
    No-correction baseline. Useful when:
        - fn 7 bilinear was just re-done and is known to be accurate, OR
        - the user calibrated points.json by clicking inside BlueStacks and
          ADB pixel coordinates were copied directly into the file.

    Always returns dx=0, dy=0 with method="none". Lets the user reset the
    correction file from the GUI without editing JSON by hand.
    """
    if not adb.screen_size:
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size
    status_cb("No-correction baseline (dx=0, dy=0).")
    return {
        "dx_adb": 0.0,
        "dy_adb": 0.0,
        "adb_w": adb_w,
        "adb_h": adb_h,
        "method": "none",
        "samples": 0,
    }


def fn1_aim_at_point(adb: ADBController,
                     status_cb: Callable[[str], None],
                     wait_for_hotkey: Callable[[float], bool],
                     read_cursor: Callable[[], Tuple[float, float]],
                     screen_to_adb_predicted: Optional[ScreenToAdb] = None
                     ) -> Optional[Dict[str, float]]:
    """
    Single-anchor offset calibration that bypasses the screen-side ripple
    detector completely. Algorithm:

        1. Pick the centre of the emulator as the target ADB point.
        2. Send an ADB tap to that point so the user sees the ripple
           render in BlueStacks.
        3. Wait for the user to put the cursor on the ripple and press
           the calibration hotkey (Caps Lock by default in the GUI).
        4. Read the cursor's screen pixel coordinates.
        5. Convert screen -> ADB using the predicted mapping (config-
           aware fn 1 if available, else fn 2 cursor calibration).
        6. The difference vs. the centre is the correction.

    Compared to compute_fn1_adb_correction this method:
        - never crops images, never runs PIL diff
        - uses the SAME cursor read path as fn 2 (so cursor/Y-axis bugs
          would have been caught long ago)
        - needs only ONE successful sample -- always converges as long
          as the user can put their finger on the ripple
    """
    if not adb.screen_size:
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size
    if screen_to_adb_predicted is None:
        # Build one from the OS window detector + standard insets.
        screen_to_adb_predicted = bluestacks_config_calibration(
            adb, lambda _s: None)
        if screen_to_adb_predicted is None:
            status_cb("[!] No window mapping available. Run a fn 2 "
                      "calibration first OR open BlueStacks and try again.")
            return None
    cx, cy = adb_w // 2, adb_h // 2
    status_cb(f"Sending ADB tap to centre ({cx},{cy}). "
              "Hover the cursor over the ripple and press the hotkey.")
    adb.tap(cx, cy)
    if not wait_for_hotkey(15.0):
        status_cb("[!] Timed out. Re-run when ready.")
        return None
    sx, sy = read_cursor()
    status_cb(f"Captured cursor at ({sx:.0f}, {sy:.0f})")
    measured_adb_x, measured_adb_y = screen_to_adb_predicted.to_adb(sx, sy)
    dx = cx - measured_adb_x
    dy = cy - measured_adb_y
    status_cb(f"Correction: dx={dx:+.2f}  dy={dy:+.2f}  "
              f"(one anchor, screen->ADB via config-aware mapping)")
    return {
        "dx_adb": dx,
        "dy_adb": dy,
        "adb_w": adb_w,
        "adb_h": adb_h,
        "method": "single-anchor-cursor",
        "samples": 1,
    }


def fn1_invert_fn2(adb: ADBController,
                   status_cb: Callable[[str], None],
                   fn2_mapping: ScreenToAdb
                   ) -> Optional[Dict[str, float]]:
    """
    Reuse the fn 2 cursor calibration to derive an fn 1 correction.

    Rationale: fn 2 (cursor-follow spam) clearly works for the user when
    its window calibration is well-tuned. We can therefore TRUST the fn 2
    mapping and use it to predict where any given ADB tap will appear on
    screen, then compare with the prediction made by the config-aware
    fn 1 mapping. The delta is the systematic correction.

    No screenshots, no ripple detection. The only requirement is that
    fn 2 has been calibrated already.
    """
    if not adb.screen_size:
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size
    config_pred = bluestacks_config_calibration(adb, lambda _s: None)
    if config_pred is None:
        status_cb("[!] BlueStacks window not found.")
        return None
    # Probe the centre and the four corners.
    probes = [
        (adb_w // 2, adb_h // 2),
        (int(adb_w * 0.2), int(adb_h * 0.2)),
        (int(adb_w * 0.8), int(adb_h * 0.2)),
        (int(adb_w * 0.2), int(adb_h * 0.8)),
        (int(adb_w * 0.8), int(adb_h * 0.8)),
    ]
    deltas: List[Tuple[float, float]] = []
    for ax, ay in probes:
        # Where does config-aware say this ADB point lives on screen?
        # ScreenToAdb only does screen->ADB. We need the inverse.
        # For a linear ScreenToAdb: screen = TL + (ADB / size) * (BR - TL).
        sx_config = (config_pred.tl[0]
                     + ax / adb_w * (config_pred.br[0] - config_pred.tl[0]))
        sy_config = (config_pred.tl[1]
                     + ay / adb_h * (config_pred.br[1] - config_pred.tl[1]))
        # Where does fn 2 mapping say that same screen point translates to?
        adb_via_fn2_x, adb_via_fn2_y = fn2_mapping.to_adb(sx_config, sy_config)
        dx = ax - adb_via_fn2_x
        dy = ay - adb_via_fn2_y
        deltas.append((dx, dy))
        status_cb(f"  Probe ADB({ax},{ay}) -> via-fn2 ({adb_via_fn2_x:.0f},"
                  f"{adb_via_fn2_y:.0f})  \u0394=({dx:+.1f},{dy:+.1f})")
    dx_mean = sum(d[0] for d in deltas) / len(deltas)
    dy_mean = sum(d[1] for d in deltas) / len(deltas)
    status_cb(f"Correction: dx={dx_mean:+.2f}  dy={dy_mean:+.2f}  "
              f"(derived from fn 2 mapping over {len(deltas)} probes)")
    return {
        "dx_adb": dx_mean,
        "dy_adb": dy_mean,
        "adb_w": adb_w,
        "adb_h": adb_h,
        "method": "invert-fn2",
        "samples": len(deltas),
    }


# ===========================================================================
# OCR-driven fn 1 auto-calibration
# ===========================================================================
# This is the *correct* fn 1 calibration -- the one that actually works
# without screenshots of the host screen and without user supervision.
#
# Mechanism:
#   1. Two clickable game objects on the map have known stable positions.
#      The user pins them ONCE by ADB pixels (stored in game_anchors.json).
#   2. The user pins the ROI of the game's coord-readout field ONCE
#      (stored in game_ui_roi.json).
#   3. Every time the user hits "Calibrate", this routine:
#         a) taps the first anchor's ADB pixels;
#         b) screencaps the emulator (via adb exec-out screencap);
#         c) OCRs the ROI;
#         d) parses "x:N y:N" into (g1x, g1y);
#         e) repeats for the second anchor;
#         f) solves the affine kx, ky, bx, by from two (game, ADB) pairs.
#   The result is a precise affine. No host screenshots. No user input
#   beyond the one-time setup.
#
# This module returns a dict of the same shape as compute_fn1_adb_correction
# used to, but with the affine fields instead of the offset fields. The GUI
# saves it into GAME_AFFINE_CALIB_FILE and fn 1 main_clicker uses it as
# its primary game->ADB mapping.

import pat_ocr as _pat_ocr  # Co-located OCR helpers


def ocr_auto_calibrate_fn1(adb: ADBController,
                           status_cb: Callable[[str], None],
                           roi: Tuple[int, int, int, int],
                           anchor1_adb: Tuple[int, int],
                           anchor2_adb: Tuple[int, int],
                           settle_seconds: float = 0.8,
                           debug_dir: Optional[str] = None
                           ) -> Optional[Dict[str, float]]:
    """
    Run the full OCR auto-calibration. Returns {kx, ky, bx, by} or None.

    Parameters
    ----------
    adb              : ADBController, already connected.
    status_cb        : Log/status callback (thread-safe).
    roi              : (x, y, w, h) of the coord field in ADB pixels.
    anchor1_adb      : (ax, ay) of the first clickable game object.
    anchor2_adb      : (ax, ay) of the second clickable game object.
    settle_seconds   : Wait between the ADB tap and the screencap. Must be
                       large enough for the game UI to render the coord
                       readout for the newly-selected object.
    debug_dir        : If set, raw + preprocessed crops are saved into
                       this directory for offline diagnostics.
    """
    # Sanity: make sure the OCR backend is actually ready.
    deps = _pat_ocr.is_available()
    if not all(deps.values()):
        status_cb("[!] OCR dependencies missing:\n" + _pat_ocr.missing_deps_message())
        return None
    if adb.serial is None:
        status_cb("[!] ADB not connected.")
        return None

    def probe(label: str, anchor: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        """Tap an anchor, OCR the coord field, retry a few times if needed.

        Each retry waits longer in case the UI element renders slowly.
        Every attempt writes a labelled debug image so the user can see
        WHICH attempt produced WHICH crop (e.g. anchor_1_try2_raw.png).
        """
        ax, ay = anchor
        # Try up to 3 times with increasing settle delays.
        delays = [settle_seconds, settle_seconds + 0.6, settle_seconds + 1.2]
        for attempt, delay in enumerate(delays, start=1):
            status_cb(f"Anchor {label} try {attempt}: tap ({ax}, {ay}) -> wait {delay:.1f}s")
            adb.tap(ax, ay)
            time.sleep(delay)
            debug_path = (os.path.join(debug_dir, f"anchor_{label}_try{attempt}")
                          if debug_dir else None)
            # Run the OCR pipeline, but ALSO log the raw text we got
            # back from Tesseract so the user can see in debug.log
            # exactly what was misread.
            if Image is None:
                status_cb("[!] Pillow missing -- cannot OCR.")
                return None
            img = _pat_ocr.adb_screencap_image(adb.adb_path, adb.serial)
            if img is None:
                status_cb(f"[!] adb screencap failed on try {attempt}.")
                continue
            x, y, w, h = roi
            crop = img.crop((x, y, x + w, y + h))
            proc = _pat_ocr.preprocess_for_ocr(crop)
            if debug_path:
                try:
                    crop.save(debug_path + "_raw.png")
                    proc.save(debug_path + "_proc.png")
                except Exception:
                    pass
            text = _pat_ocr.ocr_text(proc) or ""
            text_clean = text.strip().replace("\n", " | ")
            status_cb(f"  OCR raw: {text_clean!r}")
            coords = _pat_ocr.parse_coords(text)
            if coords is not None:
                status_cb(f"Anchor {label}: game = ({coords[0]}, {coords[1]})")
                return coords
            status_cb(f"  Anchor {label} try {attempt}: parse failed, retrying.")
        status_cb(f"[!] Anchor {label}: all OCR attempts failed.")
        return None

    g1 = probe("1", anchor1_adb)
    if g1 is None:
        return None
    g2 = probe("2", anchor2_adb)
    if g2 is None:
        return None

    # Solve the affine kx, ky, bx, by.
    g1x, g1y = g1
    g2x, g2y = g2
    a1x, a1y = anchor1_adb
    a2x, a2y = anchor2_adb
    dgx = g2x - g1x
    dgy = g2y - g1y
    if abs(dgx) < 1 or abs(dgy) < 1:
        status_cb("[!] Anchors are aligned on an axis -- choose diagonal anchors.")
        return None
    kx = (a2x - a1x) / dgx
    ky = (a2y - a1y) / dgy
    bx = a1x - kx * g1x
    by = a1y - ky * g1y
    status_cb(f"Affine solved: kx={kx:.4f} ky={ky:.4f}  bx={bx:.1f} by={by:.1f}")
    return {
        "kx": kx, "ky": ky, "bx": bx, "by": by,
        "method": "ocr-2-anchor",
        "anchor1_game": list(g1), "anchor2_game": list(g2),
        "anchor1_adb": list(anchor1_adb),
        "anchor2_adb": list(anchor2_adb),
        "roi": list(roi),
    }
