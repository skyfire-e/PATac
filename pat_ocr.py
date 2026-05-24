#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Tesseract-based OCR of the in-game coordinate field for Post Apo Tycoon.
#
# Reads pixels directly from the emulator via `adb exec-out screencap`, so:
#   * no host-screen screenshots involved (no macOS Screen Recording perm)
#   * no Retina / DPI / window-position math
#   * ROI coordinates are in ADB pixels -- the same coord system the tap
#     commands use, which keeps the calibration entirely linear.

import io  # In-memory PNG decode
import os  # Path helpers
import re  # Regex parsing of OCR output
import shutil  # Locate the tesseract binary on PATH
import subprocess  # adb exec-out + tesseract fallback
from typing import Optional, Tuple, Dict, Any, List  # Type hints

# Optional deps -- absence is detected and surfaced as a clear error.
try:
    import numpy as np  # type: ignore  # Pre-processing (threshold)
except ImportError:
    np = None  # type: ignore

try:
    from PIL import Image  # type: ignore  # PNG decode + image ops
except ImportError:
    Image = None  # type: ignore

try:
    import pytesseract  # type: ignore  # Tesseract wrapper
except ImportError:
    pytesseract = None  # type: ignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_available() -> Dict[str, bool]:
    """Return a dict listing which OCR backends/modules are present."""
    return {
        "numpy": np is not None,
        "Pillow": Image is not None,
        "pytesseract": pytesseract is not None,
        "tesseract_binary": shutil.which("tesseract") is not None,
    }


def missing_deps_message() -> str:
    """Human-readable list of what to install if anything is missing."""
    avail = is_available()
    missing: List[str] = []
    if not avail["numpy"] or not avail["Pillow"] or not avail["pytesseract"]:
        missing.append("pip install numpy Pillow pytesseract")
    if not avail["tesseract_binary"]:
        missing.append("brew install tesseract  (macOS) "
                       "or apt install tesseract-ocr  (Linux)")
    if not missing:
        return "All OCR dependencies are present."
    return "Missing OCR deps -- install:\n  " + "\n  ".join(missing)


def adb_screencap_png(adb_path: str, serial: str,
                      timeout: float = 6.0) -> Optional[bytes]:
    """Return raw PNG bytes of the emulator screen via `adb exec-out screencap`."""
    try:
        proc = subprocess.run(
            [adb_path, "-s", serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=timeout,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        return proc.stdout
    except Exception:
        return None


def adb_screencap_image(adb_path: str, serial: str) -> Optional["Image.Image"]:
    """Same as above but decoded as a PIL Image in RGB mode."""
    if Image is None:
        return None
    png = adb_screencap_png(adb_path, serial)
    if png is None:
        return None
    try:
        return Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# OCR pipeline
# ---------------------------------------------------------------------------
# Regex tuned to the exact format the user confirmed: "x:-106 y:158".
# We tolerate ALL of the following Tesseract failure modes we observed:
#   * Missing separator between the two numbers (whitelist eats whitespace
#     in some Tesseract builds): "x:-211y:-153"
#   * Different punctuation after the axis letter ("x:" / "x;" / "X-" / "X ")
#   * Lower or upper case axis letters
#   * Stray non-digit chars between the digits (very rare; the threshold
#     image is clean enough that we expect digit-only output)
#
# The regex therefore:
#   * requires only [xX] then optional punct/space, then signed integer
#   * the "between" block accepts ANY non-digit run including empty
#   * then requires [yY] then optional punct/space, then signed integer
_RE_COORDS = re.compile(
    r"[xX][^0-9\-]*(-?\d+)[^0-9\-yY]*[yY][^0-9\-]*(-?\d+)"
)


def preprocess_for_ocr(crop: "Image.Image") -> "Image.Image":
    """
    Convert a small crop containing 'x:N y:N' (white text on dark bg)
    into a high-contrast binary image suitable for Tesseract.

    Steps:
      1. Convert to grayscale.
      2. Upscale 3x (Tesseract is happier with larger glyphs).
      3. Otsu threshold via numpy (binary white-on-black).
      4. INVERT so we get the Tesseract-preferred black-on-white.
    """
    if Image is None or np is None:
        return crop
    img = crop.convert("L")  # Grayscale
    new_size = (img.size[0] * 3, img.size[1] * 3)
    img = img.resize(new_size, Image.LANCZOS)  # 3x scale for better OCR
    arr = np.asarray(img, dtype=np.uint8)
    # Otsu threshold (vectorised numpy implementation).
    hist, _ = np.histogram(arr, bins=256, range=(0, 256))
    total = arr.size
    sum_total = float((np.arange(256) * hist).sum())
    sum_b = 0.0
    w_b = 0
    var_max = -1.0
    threshold = 127
    for i in range(256):
        w_b += int(hist[i])
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * int(hist[i])
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        var_between = w_b * w_f * (m_b - m_f) ** 2
        if var_between > var_max:
            var_max = var_between
            threshold = i
    # Text is BRIGHT (white) on dark, so pixels above threshold are text.
    # Tesseract prefers dark text on white background -> invert mask.
    mask = arr > threshold
    binary = np.where(mask, 0, 255).astype(np.uint8)  # Invert: text=0 (black)
    return Image.fromarray(binary, mode="L")


def ocr_text(crop: "Image.Image") -> Optional[str]:
    """Run Tesseract on a preprocessed crop, returning raw text or None.

    Tries several PSM modes and returns the FIRST output that the regex
    can parse into a coord pair. Falls back to the raw best-effort text
    so caller diagnostics can still see something useful.

    PSM 7 (single line) is the natural choice for our field, but on
    real-world Tesseract 5 builds it sometimes ignores whitespace inside
    a whitelist and merges the X/Y numbers. PSM 6 (single uniform block)
    handles that case more reliably.
    """
    if pytesseract is None:
        return None
    if shutil.which("tesseract") is None:
        return None
    # Try a few PSM modes. The first one whose output parses cleanly wins.
    # We deliberately DROP the character whitelist: it costs us the space
    # between X and Y in some Tesseract builds and we gain nothing -- the
    # regex below is strict enough.
    candidates = ("--psm 7", "--psm 6", "--psm 11", "")
    fallback: Optional[str] = None
    for cfg in candidates:
        try:
            text = pytesseract.image_to_string(crop, config=cfg)
        except Exception:
            continue
        if not text:
            continue
        if fallback is None:
            fallback = text  # Remember the first non-empty result
        if parse_coords(text) is not None:
            return text  # This config produced a parseable string
    return fallback  # Return whatever we got, caller may still salvage


def parse_coords(text: str) -> Optional[Tuple[int, int]]:
    """Extract (x, y) integers from the raw OCR text. None on failure."""
    if not text:
        return None
    # The regex anchors on x: and y:, so any junk around it is ignored.
    m = _RE_COORDS.search(text)
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return None


def read_game_coords(adb_path: str, serial: str,
                     roi: Tuple[int, int, int, int],
                     debug_save_path: Optional[str] = None
                     ) -> Optional[Tuple[int, int]]:
    """
    Take a fresh emulator screencap, crop to ROI (in ADB pixels),
    preprocess, OCR, parse. Return (game_x, game_y) or None.

    `roi` is (x, y, w, h) in ADB pixels.

    If `debug_save_path` is given, the raw crop and the preprocessed
    binary are saved next to it (`*_raw.png`, `*_proc.png`) for offline
    inspection -- invaluable when calibration silently misreads digits.
    """
    if Image is None or np is None or pytesseract is None:
        return None
    img = adb_screencap_image(adb_path, serial)
    if img is None:
        return None
    x, y, w, h = roi
    crop = img.crop((x, y, x + w, y + h))
    proc = preprocess_for_ocr(crop)
    if debug_save_path:
        try:
            crop.save(debug_save_path + "_raw.png")
            proc.save(debug_save_path + "_proc.png")
        except Exception:
            pass  # Diagnostics must never break calibration
    text = ocr_text(proc)
    if not text:
        return None
    return parse_coords(text)
