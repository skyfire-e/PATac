#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Post Apo Tycoon ADB clicker / automation script.
# Cross-platform (macOS / Windows / Linux). All clicks are sent via ADB so the
# user's mouse stays free and clicks are delivered to BlueStacks in background.

import json  # JSON load/save for all config files
import os  # OS / path helpers
import sys  # Platform detection and exit
import time  # Sleeps and timing
import random  # Random points inside a polygon
import shutil  # Locate the adb executable in PATH
import threading  # Background workers (click loop, listeners)
import queue  # Thread-safe command queue for the persistent ADB shell
import subprocess  # Launch the adb process / persistent shell
import platform  # OS name for cross-platform branches
from typing import List, Tuple, Optional, Dict, Any  # Type hints

# Third-party imports are wrapped so the script still loads and can print a
# helpful message if a dependency is missing.
try:
    from pynput import mouse, keyboard  # Cross-platform mouse + keyboard listeners
except ImportError:  # If pynput is not installed, fail loudly with instructions
    print("[!] Missing dependency: pynput. Install with: pip install pynput")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants / file names
# ---------------------------------------------------------------------------
CALIBRATION_FILE = "calibration.json"  # Map calibration (4-point bilinear)
POINTS_FILE = "points.json"  # User list of game points to click
ZOOM_CALIB_FILE = "calibration_zoom.json"  # Settings btn, zoom-out btn, swipe
CLICKER_OPT_FILE = "clicker_option.json"  # Speed (CPS) for the main clicker
CONFIG_FILE = "config.json"  # ADB host:port override (optional)
WINDOW_CALIB_FILE = "window_calibration_fn2.json"  # Screen-px -> ADB-px mapping for fn 2 ONLY
# fn 1 keeps its own copy so changes to one calibration cannot break the
# other. Same file format, just a different path.
WINDOW_CALIB_FILE_FN1 = "window_calibration_fn1.json"  # For fn 1 ONLY
# Systematic ADB-pixel correction applied to every fn 1 tap (added on top
# of the fn 7 bilinear mapping). Produced by the fn1 ripple calibration.
FN1_CORRECTION_FILE = "fn1_correction.json"
# Affine game->ADB calibration produced by the new fn 1 wizard. Two pairs of
# (game_xy, adb_xy) are enough to recover {kx, ky, bx, by} -- see
# GameToAdbAffine. This is the precise, screenshot-free replacement for the
# old 4-point bilinear (calibration.json) for top-down maps.
GAME_AFFINE_CALIB_FILE = "game_affine_calibration.json"
# fn 1 one-time setup: the ROI of the in-game coordinate field (in ADB
# pixels of the emulator screen) and two known clickable game objects.
# Together with the affine calibration above they let the script perform
# an automatic recalibration on demand by tapping each anchor and OCR'ing
# the resulting "x:N y:N" UI text.
GAME_UI_ROI_FILE = "game_ui_roi.json"
GAME_ANCHORS_FILE = "game_anchors.json"

# Platform detection constants used by the OS-specific window locator.
_IS_MAC = sys.platform == "darwin"  # macOS
_IS_WIN = sys.platform.startswith("win")  # Windows
_IS_LINUX = sys.platform.startswith("linux")  # Linux / X11

# Sentinel value used by all prompts: typing "b" returns to the main menu.
BACK_TOKEN = "b"


# ---------------------------------------------------------------------------
# Helper: pretty I/O with main-menu exit
# ---------------------------------------------------------------------------
class BackToMenu(Exception):
    """Raised inside any sub-function to bubble up to the main menu."""
    pass  # No payload, just control flow


def ask(prompt: str) -> str:
    """Blocking input prompt; raises BackToMenu when user types 'b'."""
    try:
        # Append a hint so the user always sees how to escape.
        raw = input(f"{prompt} (or '{BACK_TOKEN}' to go back): ").strip()
    except (EOFError, KeyboardInterrupt):
        # Ctrl+C / Ctrl+D inside a prompt = go back, not crash.
        print()  # Newline for readability
        raise BackToMenu()
    if raw.lower() == BACK_TOKEN:  # User explicitly asked to go back
        raise BackToMenu()
    return raw  # Otherwise return the raw input string


def ask_int(prompt: str, min_v: Optional[int] = None, max_v: Optional[int] = None) -> int:
    """Ask the user for an integer, re-asking on invalid input."""
    while True:  # Loop until we get a valid integer or BackToMenu
        raw = ask(prompt)  # Reuse the back-aware prompt
        try:
            v = int(raw)  # Convert to int
        except ValueError:
            print("[!] Please enter an integer.")  # Bad input
            continue  # Try again
        if min_v is not None and v < min_v:  # Range check
            print(f"[!] Value must be >= {min_v}.")
            continue
        if max_v is not None and v > max_v:  # Range check
            print(f"[!] Value must be <= {max_v}.")
            continue
        return v  # Valid integer


def ask_float(prompt: str, min_v: Optional[float] = None) -> float:
    """Ask the user for a float, re-asking on invalid input."""
    while True:  # Loop until valid
        raw = ask(prompt)  # Back-aware prompt
        try:
            v = float(raw)  # Convert
        except ValueError:
            print("[!] Please enter a number.")  # Bad input
            continue
        if min_v is not None and v < min_v:  # Lower bound
            print(f"[!] Value must be >= {min_v}.")
            continue
        return v  # Valid float


def ask_xy(prompt: str) -> Tuple[float, float]:
    """Ask the user for an 'X Y' coordinate pair (space or comma separated)."""
    while True:  # Loop until valid
        raw = ask(prompt)  # Back-aware prompt
        # Accept both "100,200" and "100 200" formats for convenience.
        parts = raw.replace(",", " ").split()
        if len(parts) != 2:  # Exactly two numbers required
            print("[!] Please enter two numbers, e.g. '100 200'.")
            continue
        try:
            return float(parts[0]), float(parts[1])  # Parse both
        except ValueError:
            print("[!] Both values must be numbers.")  # Bad input
            continue


# ---------------------------------------------------------------------------
# JSON config helpers
# ---------------------------------------------------------------------------
def load_json(path: str) -> Optional[Dict[str, Any]]:
    """Load a JSON file if it exists, otherwise return None."""
    if not os.path.isfile(path):  # File missing
        return None  # Caller decides what to do
    try:
        with open(path, "r", encoding="utf-8") as f:  # Open as UTF-8 text
            return json.load(f)  # Parse JSON
    except (json.JSONDecodeError, OSError) as e:  # Corrupted / unreadable
        print(f"[!] Failed to read {path}: {e}")
        return None  # Treat as missing


def save_json(path: str, data: Any) -> None:
    """Save a python object to JSON with indentation."""
    with open(path, "w", encoding="utf-8") as f:  # Truncate/create file
        json.dump(data, f, indent=2, ensure_ascii=False)  # Pretty print
    print(f"[+] Saved {path}")  # Confirmation for the user


# ---------------------------------------------------------------------------
# ADB wrapper: persistent shell + safe stop
# ---------------------------------------------------------------------------
class ADBController:
    """
    Manages an adb connection to BlueStacks and a *persistent* `adb shell`
    process. Commands are pushed through stdin, which is dramatically faster
    than spawning a new `adb shell input tap` process per click.

    Safe-stop strategy (matches the user's requirement):
      * `stop_event` -- threading.Event, checked before every command flush.
      * `cmd_queue`  -- queue.Queue, *drained* on stop so any buffered taps
                        are discarded.
      * `kill_shell()` -- last resort that terminates the persistent shell
                          subprocess; clicks physically cannot be sent until
                          the shell is re-created.
    """

    # Common BlueStacks ADB ports (5555 default; emulator-555X are also common)
    DEFAULT_PORTS = [5555, 5565, 5575, 5585, 5595]

    def __init__(self) -> None:
        # Locate the adb binary in PATH (Auto-detect, per user's choice).
        self.adb_path: str = self._find_adb()
        # Serial of the currently connected device (e.g. "127.0.0.1:5555").
        self.serial: Optional[str] = None
        # Persistent shell subprocess (Popen) -- None while disconnected.
        self.shell_proc: Optional[subprocess.Popen] = None
        # Lock to serialize writes to the shell's stdin from multiple threads.
        self.shell_lock = threading.Lock()
        # Cached screen resolution (width, height) read once via `wm size`.
        self.screen_size: Optional[Tuple[int, int]] = None

    # --- discovery ------------------------------------------------------
    def _find_adb(self) -> str:
        """Return a usable adb executable path or raise."""
        # 1) Honor optional config override.
        cfg = load_json(CONFIG_FILE) or {}  # Empty dict if no config
        override = cfg.get("adb_path")  # User-specified path
        if override and os.path.isfile(override):  # Use override if valid
            return override
        # 2) PATH lookup (works on macOS/Linux/Windows).
        found = shutil.which("adb")  # Cross-platform "which"
        if found:
            return found
        # 3) Common install locations to try as a last resort.
        candidates = [
            "/usr/local/bin/adb",  # Homebrew Intel
            "/opt/homebrew/bin/adb",  # Homebrew Apple Silicon
            os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),  # macOS SDK
            r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe",  # BlueStacks 5 Windows
            r"C:\Program Files (x86)\BlueStacks\HD-Adb.exe",  # BlueStacks 4
        ]
        for c in candidates:  # Try each candidate
            if os.path.isfile(c):
                return c
        raise RuntimeError("adb executable not found. Install Android platform-tools "
                           "or set 'adb_path' in config.json.")

    # --- connection -----------------------------------------------------
    def _run(self, args: List[str], timeout: float = 5.0) -> Tuple[int, str, str]:
        """Run a one-shot adb command and return (rc, stdout, stderr)."""
        # subprocess.run with capture_output for a single short-lived call.
        proc = subprocess.run(
            [self.adb_path, *args],  # Prepend adb path
            capture_output=True, text=True, timeout=timeout,  # Capture text
        )
        return proc.returncode, proc.stdout, proc.stderr  # Tuple back to caller

    def connect(self) -> None:
        """Discover and connect to a BlueStacks ADB endpoint."""
        cfg = load_json(CONFIG_FILE) or {}  # Optional config
        # Allow user to pin a specific host:port in config.json.
        forced = cfg.get("adb_host_port")  # e.g. "127.0.0.1:5555"
        ports = [forced.split(":")[1]] if forced else self.DEFAULT_PORTS  # Choose ports list
        # First, see what is already attached (BlueStacks 5 attaches automatically).
        rc, out, _ = self._run(["devices"])  # `adb devices`
        attached = []
        if rc == 0:  # Parse `adb devices` output
            for line in out.splitlines()[1:]:  # Skip header line
                line = line.strip()
                if line.endswith("device"):  # Only fully-connected devices
                    attached.append(line.split()[0])  # First column = serial
        # If something is already attached, use it directly.
        if attached:
            self.serial = attached[0]  # Pick the first attached device
            print(f"[+] Using already-attached device: {self.serial}")
        else:
            # Otherwise try `adb connect 127.0.0.1:<port>` for known ports.
            host = "127.0.0.1"  # BlueStacks always listens on loopback
            for p in ports:  # Try each candidate port
                target = f"{host}:{p}"
                rc, out, _ = self._run(["connect", target])  # `adb connect`
                if "connected" in out.lower():  # Success keyword
                    self.serial = target  # Remember serial
                    print(f"[+] Connected to {target}")
                    break  # Stop on first success
            if not self.serial:  # Nothing worked
                raise RuntimeError("Could not connect to BlueStacks via ADB. "
                                   "Make sure ADB is enabled in BlueStacks settings.")
        # Cache the screen resolution -- used for centering the first click.
        self.screen_size = self._query_screen_size()  # Stored as (w, h)
        print(f"[+] Emulator screen size: {self.screen_size[0]}x{self.screen_size[1]}")
        # Spin up the persistent shell that we will reuse for tap commands.
        self._start_shell()

    def _query_screen_size(self) -> Tuple[int, int]:
        """Ask the emulator for its current screen resolution via `wm size`."""
        rc, out, _ = self._run(["-s", self.serial, "shell", "wm", "size"])  # adb shell wm size
        # Output looks like: "Physical size: 1920x1080" (or "Override size: ...").
        size = (540, 960)  # Safe default if parsing fails
        for line in out.splitlines():  # Walk every line
            if "size:" in line.lower():  # Match either Physical/Override
                token = line.split(":")[-1].strip()  # e.g. "1920x1080"
                if "x" in token:  # Sanity check
                    try:
                        w, h = token.split("x")  # Split width/height
                        size = (int(w), int(h))  # Parse as ints
                    except ValueError:
                        pass  # Keep default on parse error
        return size  # Final tuple

    # --- persistent shell ----------------------------------------------
    def _start_shell(self) -> None:
        """(Re)start the persistent `adb shell` subprocess."""
        self.kill_shell()  # Make sure no leftover process is running
        # Popen with stdin pipe so we can stream commands without process churn.
        self.shell_proc = subprocess.Popen(
            [self.adb_path, "-s", self.serial, "shell"],  # Pin to current serial
            stdin=subprocess.PIPE,  # We will write commands here
            stdout=subprocess.DEVNULL,  # Ignore stdout (taps don't print)
            stderr=subprocess.DEVNULL,  # Ignore stderr too
            bufsize=0,  # Unbuffered for low latency
        )

    def kill_shell(self) -> None:
        """Forcefully terminate the persistent shell. Used as the safety net."""
        proc = self.shell_proc  # Local reference
        self.shell_proc = None  # Clear immediately to prevent new writes
        if proc and proc.poll() is None:  # Still alive?
            try:
                proc.stdin.close()  # Close stdin -- usually triggers clean exit
            except Exception:
                pass  # Best effort
            try:
                proc.terminate()  # SIGTERM (or TerminateProcess on Windows)
                proc.wait(timeout=1.0)  # Give it a moment
            except Exception:
                try:
                    proc.kill()  # Hard kill if still alive
                except Exception:
                    pass  # Best effort

    def _send_shell(self, line: str) -> None:
        """Write one command line to the persistent shell."""
        if self.shell_proc is None or self.shell_proc.poll() is not None:
            # Shell died (e.g. user killed it via stop) -- bring it back.
            self._start_shell()
        try:
            with self.shell_lock:  # Serialize writes across threads
                self.shell_proc.stdin.write((line + "\n").encode("utf-8"))  # Newline-terminated
                self.shell_proc.stdin.flush()  # Make sure it leaves immediately
        except (BrokenPipeError, OSError):
            # The shell process is gone -- recreate it and retry once.
            self._start_shell()
            try:
                with self.shell_lock:
                    self.shell_proc.stdin.write((line + "\n").encode("utf-8"))
                    self.shell_proc.stdin.flush()
            except Exception as e:
                print(f"[!] ADB shell write failed: {e}")

    # --- public click / swipe primitives -------------------------------
    def tap(self, x: float, y: float) -> None:
        """Send a single left-click (tap) to (x, y) in emulator coordinates."""
        # `input tap` is the most compatible command across Android versions
        # and BlueStacks builds. It is dispatched by the persistent shell so
        # there is no process-spawn overhead.
        self._send_shell(f"input tap {int(x)} {int(y)}")

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300) -> None:
        """Send a swipe from (x1,y1) to (x2,y2) with given duration."""
        self._send_shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def center_tap(self) -> None:
        """Tap exactly in the center of the emulator window."""
        if not self.screen_size:  # Defensive: refresh if missing
            self.screen_size = self._query_screen_size()
        w, h = self.screen_size  # Unpack
        self.tap(w / 2, h / 2)  # Center coordinates


# ---------------------------------------------------------------------------
# WindowLocator: cross-platform OS-level BlueStacks window detection
# ---------------------------------------------------------------------------
class WindowLocator:
    """
    Locates the BlueStacks window on the host OS and returns its bounding
    rectangle in *logical screen pixels* (same units pynput uses for the
    mouse cursor). The rectangle approximates the emulator's CLIENT area
    (i.e. excluding title bar / sidebars) -- but on most BlueStacks builds
    the title bar offset is tiny and any residual offset is absorbed by the
    user calibration step (function 2 still asks the user to confirm two
    anchor points, just pre-filled by the auto-detect).

    Returns dict: {"x":, "y":, "w":, "h":} or None if not found.

    Supported process / title substrings (case-insensitive):
      BlueStacks, HD-Player, BlueStacksGP, BlueStacksApp
    """

    # Substrings we consider "BlueStacks-ish" -- match either process name
    # (macOS) or window title (Windows/Linux fallback).
    NEEDLES = ("bluestacks", "hd-player", "bluestacksgp", "bluestacksapp")

    # ----- public entry point ---------------------------------------------
    def find(self) -> Optional[Dict[str, int]]:
        """Try the OS-specific detector; return None on any failure."""
        try:
            if _IS_MAC:  # macOS branch
                return self._find_macos()
            if _IS_WIN:  # Windows branch
                return self._find_windows()
            if _IS_LINUX:  # Linux/X11 branch
                return self._find_linux()
        except Exception as e:  # Any binding error -> caller falls back
            print(f"[!] Window auto-detect failed: {e}")
        return None  # Unsupported OS or detector missing

    # ----- macOS ----------------------------------------------------------
    def _find_macos(self) -> Optional[Dict[str, int]]:
        """Use Quartz CGWindowList to enumerate on-screen windows."""
        try:
            from Quartz import (  # pyobjc-framework-Quartz
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )
        except ImportError:
            print("[!] pyobjc not installed. Install with: pip install pyobjc-framework-Quartz")
            return None  # Triggers fallback
        # Snapshot of every on-screen window across all spaces of all apps.
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly,  # Only currently visible
            kCGNullWindowID,  # All windows, no relative-to filter
        )
        best = None  # Keep the largest matching window if there are several
        for w in windows:  # Walk every window dict
            owner = (w.get("kCGWindowOwnerName") or "").lower()  # Process name
            title = (w.get("kCGWindowName") or "").lower()  # Window title
            # Match against either process name or title for robustness.
            if not any(n in owner or n in title for n in self.NEEDLES):
                continue  # Not BlueStacks -- skip
            b = w.get("kCGWindowBounds") or {}  # CGRect-shaped dict
            rect = {  # Cast to ints (Quartz returns CGFloats)
                "x": int(b.get("X", 0)),
                "y": int(b.get("Y", 0)),
                "w": int(b.get("Width", 0)),
                "h": int(b.get("Height", 0)),
            }
            if rect["w"] < 200 or rect["h"] < 200:  # Skip tiny tool windows
                continue
            if best is None or rect["w"] * rect["h"] > best["w"] * best["h"]:
                best = rect  # Prefer the largest BlueStacks window
        return best  # Maybe None if BS not running

    # ----- Windows --------------------------------------------------------
    def _find_windows(self) -> Optional[Dict[str, int]]:
        """Use ctypes + user32 to enumerate top-level windows."""
        # Using ctypes avoids requiring pywin32 (extra dependency).
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32  # USER32.DLL handle
        # Make the script DPI-aware so GetClientRect returns physical pixels
        # matching pynput's mouse coordinates on high-DPI displays.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
        except Exception:
            try:
                user32.SetProcessDPIAware()  # Older fallback
            except Exception:
                pass  # Best effort

        # Callback signature for EnumWindows.
        EnumWindowsProc = ctypes.WINFUNCTYPE(  # noqa: N806 (Win API name)
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
        )
        results: List[Tuple[int, str]] = []  # Collected (hwnd, title)

        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):  # Skip hidden windows
                return True
            length = user32.GetWindowTextLengthW(hwnd)  # Title length
            if length == 0:
                return True  # No title -> probably system window
            buf = ctypes.create_unicode_buffer(length + 1)  # Allocate
            user32.GetWindowTextW(hwnd, buf, length + 1)  # Read title
            title = buf.value
            if any(n in title.lower() for n in self.NEEDLES):  # Title match
                results.append((hwnd, title))
            return True  # Continue enumeration

        user32.EnumWindows(EnumWindowsProc(_cb), 0)  # Walk top-level windows
        if not results:
            return None  # Nothing matched
        # Pick the largest window among matches.
        best = None
        for hwnd, _title in results:
            rect = wintypes.RECT()  # CLIENT rect in screen coords needs conv
            # GetWindowRect returns the full window incl. borders -- close
            # enough for the game area on BlueStacks; user can recalibrate.
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 200 or h < 200:
                continue  # Too small
            cand = {"x": rect.left, "y": rect.top, "w": w, "h": h}
            if best is None or cand["w"] * cand["h"] > best["w"] * best["h"]:
                best = cand
        return best  # Possibly None

    # ----- Linux ----------------------------------------------------------
    def _find_linux(self) -> Optional[Dict[str, int]]:
        """Try xdotool (X11) -- BlueStacks on Linux runs via Wine/proton."""
        # xdotool is the lowest common denominator on X11; on Wayland this
        # will likely fail and the caller will fall back to manual calib.
        if shutil.which("xdotool") is None:  # Tool missing
            return None
        # Search for any window whose name matches our needles.
        for needle in self.NEEDLES:
            proc = subprocess.run(
                ["xdotool", "search", "--name", needle],
                capture_output=True, text=True,
            )
            ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            if not ids:
                continue  # Try next needle
            # Use the first match's geometry.
            wid = ids[0]
            info = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", wid],
                capture_output=True, text=True,
            )
            # Output lines look like: WIDTH=1280 / HEIGHT=720 / X=200 / Y=120
            kv: Dict[str, int] = {}
            for line in info.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    try:
                        kv[k.strip()] = int(v.strip())
                    except ValueError:
                        pass
            if {"X", "Y", "WIDTH", "HEIGHT"}.issubset(kv):
                return {"x": kv["X"], "y": kv["Y"], "w": kv["WIDTH"], "h": kv["HEIGHT"]}
        return None  # Nothing found


# ---------------------------------------------------------------------------
# Screen-pixel -> ADB-pixel mapping (used by function 2)
# ---------------------------------------------------------------------------
class ScreenToAdb:
    """
    Maps OS-screen pixel coordinates (where the user's cursor lives) to the
    emulator's internal ADB coordinate space.

    Two anchor points are sufficient for a pure scale+translate mapping. We
    avoid trying to handle rotation since BlueStacks windows are never
    rotated relative to the screen.

    Anchors:
      top_left_screen  -> (0, 0)            in ADB
      bottom_right_screen -> (adb_w, adb_h) in ADB

    where (adb_w, adb_h) is the emulator's wm size.
    """

    def __init__(self, tl_screen: Tuple[float, float],
                 br_screen: Tuple[float, float],
                 adb_w: int, adb_h: int) -> None:
        self.tl = tl_screen  # Screen-px of upper-left of game area
        self.br = br_screen  # Screen-px of lower-right of game area
        self.adb_w = adb_w  # Emulator width
        self.adb_h = adb_h  # Emulator height
        # Cache scale factors to keep the per-click math cheap.
        dx = br_screen[0] - tl_screen[0]  # Width of game area in screen px
        dy = br_screen[1] - tl_screen[1]  # Height in screen px
        if dx == 0 or dy == 0:  # Defensive: would divide by zero
            raise ValueError("Degenerate calibration rectangle.")
        self.kx = adb_w / dx  # screen-px -> adb-px scale on X
        self.ky = adb_h / dy  # screen-px -> adb-px scale on Y

    def in_window(self, sx: float, sy: float) -> bool:
        """True if the screen point lies inside the calibrated rectangle."""
        return (self.tl[0] <= sx <= self.br[0]
                and self.tl[1] <= sy <= self.br[1])

    def to_adb(self, sx: float, sy: float) -> Tuple[float, float]:
        """Linearly map a screen-pixel point to ADB coordinates."""
        ax = (sx - self.tl[0]) * self.kx  # X scaled to ADB space
        ay = (sy - self.tl[1]) * self.ky  # Y scaled to ADB space
        # Clamp into the emulator rectangle so an out-of-bounds cursor still
        # produces a valid tap (avoids errors / spurious off-screen taps).
        ax = max(0.0, min(self.adb_w - 1.0, ax))
        ay = max(0.0, min(self.adb_h - 1.0, ay))
        return ax, ay


def save_window_calibration(stl: Tuple[float, float],
                            sbr: Tuple[float, float],
                            adb_w: int, adb_h: int,
                            path: str = WINDOW_CALIB_FILE) -> None:
    """
    Persist a window calibration to `path` for reuse between sessions.

    The `path` parameter lets fn 1 and fn 2 keep COMPLETELY SEPARATE
    calibrations. Default stays WINDOW_CALIB_FILE so legacy call sites in
    the GUI keep working unchanged.
    """
    save_json(path, {
        "screen_top_left": list(stl),
        "screen_bottom_right": list(sbr),
        "adb_w": adb_w,
        "adb_h": adb_h,
    })


def load_window_calibration(adb_w: int, adb_h: int,
                            path: str = WINDOW_CALIB_FILE) -> Optional[ScreenToAdb]:
    """Load a previously-saved ScreenToAdb mapping from `path`, if any."""
    data = load_json(path)  # Read whichever file the caller asks for
    if not data:
        return None
    # If ADB resolution changed since last calibration, invalidate.
    if int(data.get("adb_w", 0)) != adb_w or int(data.get("adb_h", 0)) != adb_h:
        print(f"[!] Stored {os.path.basename(path)} is for a different "
              "emulator resolution. Re-calibrate.")
        return None
    try:
        tl = tuple(data["screen_top_left"])
        br = tuple(data["screen_bottom_right"])
        return ScreenToAdb(tl, br, adb_w, adb_h)
    except (KeyError, ValueError, TypeError) as e:
        print(f"[!] Invalid window calibration file {path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Coordinate transform: game (x,y) -> ADB (x,y) via PURE AFFINE
# ---------------------------------------------------------------------------
class GameToAdbAffine:
    """
    Affine mapping between game-space coordinates (as shown in the game UI)
    and ADB-pixel coordinates of the emulator screen. For a top-down game
    without perspective, the transformation is exactly:

        adb_x = kx * game_x + bx
        adb_y = ky * game_y + by

    Four numbers (kx, ky, bx, by) are sufficient. Two well-spaced (game,
    ADB) pairs are mathematically enough to solve the system without any
    iterative methods, screenshots, or pixel detection.

    This class is intentionally tiny and has NO geometric assumptions
    beyond "no rotation, no shear" -- which is what BlueStacks always
    gives us for top-down 2D games like Post Apo Tycoon.
    """

    def __init__(self, kx: float, ky: float, bx: float, by: float) -> None:
        self.kx = kx  # ADB pixels per game-X unit
        self.ky = ky  # ADB pixels per game-Y unit
        self.bx = bx  # ADB X of game (0, 0)
        self.by = by  # ADB Y of game (0, 0)

    @staticmethod
    def from_two_pairs(g1: Tuple[float, float], a1: Tuple[float, float],
                       g2: Tuple[float, float], a2: Tuple[float, float]
                       ) -> "GameToAdbAffine":
        """Solve {kx, ky, bx, by} from two (game, ADB) pairs.

        Raises ValueError if the two game points coincide on either axis,
        which would make the solve degenerate.
        """
        g1x, g1y = g1
        g2x, g2y = g2
        a1x, a1y = a1
        a2x, a2y = a2
        dgx = g2x - g1x  # Game-X delta between the two anchors
        dgy = g2y - g1y  # Game-Y delta between the two anchors
        if abs(dgx) < 1e-6 or abs(dgy) < 1e-6:
            raise ValueError(
                "Calibration anchors must differ on BOTH game-X and game-Y. "
                "Pick points that are diagonal to each other, not aligned.")
        kx = (a2x - a1x) / dgx  # Slope on X axis
        ky = (a2y - a1y) / dgy  # Slope on Y axis
        bx = a1x - kx * g1x  # Intercept on X (ADB of game x=0)
        by = a1y - ky * g1y  # Intercept on Y (ADB of game y=0)
        return GameToAdbAffine(kx, ky, bx, by)

    def game_to_adb(self, gx: float, gy: float) -> Tuple[float, float]:
        """Apply the affine to a (game_x, game_y) -> (adb_x, adb_y)."""
        return (self.kx * gx + self.bx,
                self.ky * gy + self.by)

    def to_dict(self) -> Dict[str, float]:
        """Serialise to a plain dict for JSON persistence."""
        return {"kx": self.kx, "ky": self.ky,
                "bx": self.bx, "by": self.by}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> Optional["GameToAdbAffine"]:
        """Deserialise from a dict produced by to_dict; None on bad data."""
        try:
            return GameToAdbAffine(
                kx=float(d["kx"]), ky=float(d["ky"]),
                bx=float(d["bx"]), by=float(d["by"]))
        except (KeyError, TypeError, ValueError):
            return None


def load_game_affine() -> Optional[GameToAdbAffine]:
    """Load the affine calibration from disk, returning None if missing."""
    data = load_json(GAME_AFFINE_CALIB_FILE)
    if not data:
        return None
    return GameToAdbAffine.from_dict(data)


def save_game_affine(aff: GameToAdbAffine, extras: Optional[Dict[str, Any]] = None) -> None:
    """Persist the affine calibration to disk."""
    out = aff.to_dict()  # {kx, ky, bx, by}
    if extras:
        out.update(extras)  # Pair-source info for debugging
    save_json(GAME_AFFINE_CALIB_FILE, out)


# ---------------------------------------------------------------------------
# Coordinate transform: game (x,y) -> ADB (x,y) via bilinear interpolation
# ---------------------------------------------------------------------------
class BilinearMap:
    """
    Bilinear interpolation between 4 game-space anchors and their matching
    4 ADB-space anchors. The 4 game anchors must form a quadrilateral; we
    parametrize them by a (u,v) in [0,1]^2 and solve for (u,v) from a query
    game point, then evaluate the ADB-space bilinear surface at that (u,v).

    Anchor order: TL, TR, BR, BL (top-left, top-right, bottom-right, bottom-left
    in *game* coordinates as the user perceives them). For BL/TR clarity we
    don't actually care about literal screen position; only the same vertex
    order in both lists is required.
    """

    def __init__(self, game_pts: List[Tuple[float, float]],
                 adb_pts: List[Tuple[float, float]]) -> None:
        if len(game_pts) != 4 or len(adb_pts) != 4:  # Guard
            raise ValueError("Bilinear calibration needs exactly 4 points.")
        self.g = game_pts  # Cache game-side anchors
        self.a = adb_pts  # Cache ADB-side anchors

    @staticmethod
    def _bilinear(p00, p10, p11, p01, u: float, v: float) -> Tuple[float, float]:
        """Standard bilinear blend across 4 corner points."""
        # (1-u)(1-v)*p00 + u(1-v)*p10 + uv*p11 + (1-u)v*p01
        x = ((1 - u) * (1 - v) * p00[0] + u * (1 - v) * p10[0]
             + u * v * p11[0] + (1 - u) * v * p01[0])  # Blended X
        y = ((1 - u) * (1 - v) * p00[1] + u * (1 - v) * p10[1]
             + u * v * p11[1] + (1 - u) * v * p01[1])  # Blended Y
        return x, y  # Return as tuple

    def _solve_uv(self, px: float, py: float) -> Tuple[float, float]:
        """Numerically solve game (px,py) -> (u,v) via Newton iteration."""
        # Start guess at the center of the quad.
        u, v = 0.5, 0.5
        for _ in range(20):  # 20 iters is more than enough for double precision
            # Current predicted game point at (u,v).
            gx, gy = self._bilinear(self.g[0], self.g[1], self.g[2], self.g[3], u, v)
            # Residual vector (target - prediction).
            rx, ry = px - gx, py - gy
            # Partial derivatives of the bilinear surface wrt u and v.
            # dG/du = (1-v)*(p10-p00) + v*(p11-p01)
            dgdu_x = (1 - v) * (self.g[1][0] - self.g[0][0]) + v * (self.g[2][0] - self.g[3][0])
            dgdu_y = (1 - v) * (self.g[1][1] - self.g[0][1]) + v * (self.g[2][1] - self.g[3][1])
            # dG/dv = (1-u)*(p01-p00) + u*(p11-p10)
            dgdv_x = (1 - u) * (self.g[3][0] - self.g[0][0]) + u * (self.g[2][0] - self.g[1][0])
            dgdv_y = (1 - u) * (self.g[3][1] - self.g[0][1]) + u * (self.g[2][1] - self.g[1][1])
            # Solve the 2x2 linear system J * d = r for d = (du, dv).
            det = dgdu_x * dgdv_y - dgdu_y * dgdv_x  # Jacobian determinant
            if abs(det) < 1e-12:  # Degenerate -- bail out with current guess
                break
            du = (rx * dgdv_y - ry * dgdv_x) / det  # Cramer's rule
            dv = (dgdu_x * ry - dgdu_y * rx) / det  # Cramer's rule
            u += du  # Newton update
            v += dv  # Newton update
            if abs(du) + abs(dv) < 1e-9:  # Converged
                break
        return u, v  # Best (u,v)

    def game_to_adb(self, gx: float, gy: float) -> Tuple[float, float]:
        """Map a game point to ADB coordinates."""
        u, v = self._solve_uv(gx, gy)  # Find parametric coords
        # Evaluate the ADB bilinear surface at the same (u,v).
        return self._bilinear(self.a[0], self.a[1], self.a[2], self.a[3], u, v)


def load_calibration() -> Optional[BilinearMap]:
    """Load and instantiate a BilinearMap from calibration.json, or None."""
    data = load_json(CALIBRATION_FILE)  # Read file
    if not data:  # Missing or unreadable
        return None
    try:
        game = [tuple(p) for p in data["game_points"]]  # 4 game anchors
        adb = [tuple(p) for p in data["adb_points"]]  # 4 adb anchors
        return BilinearMap(game, adb)  # Build the map
    except (KeyError, ValueError) as e:  # Corrupted shape
        print(f"[!] Invalid calibration file: {e}")
        return None


# ---------------------------------------------------------------------------
# Polygon helpers (random point inside a polygon via triangle fan)
# ---------------------------------------------------------------------------
def _triangle_area(a, b, c) -> float:
    """2D triangle area (absolute) for a, b, c."""
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) * 0.5


def _random_point_in_triangle(a, b, c) -> Tuple[float, float]:
    """Uniform random point inside triangle a-b-c (Turk's method)."""
    r1 = random.random()  # First barycentric weight
    r2 = random.random()  # Second barycentric weight
    if r1 + r2 > 1.0:  # Reflect into the triangle if outside
        r1, r2 = 1 - r1, 1 - r2
    # Barycentric to cartesian.
    x = a[0] + r1 * (b[0] - a[0]) + r2 * (c[0] - a[0])
    y = a[1] + r1 * (b[1] - a[1]) + r2 * (c[1] - a[1])
    return x, y


class PolygonSampler:
    """Uniformly samples random points inside an arbitrary simple polygon."""

    def __init__(self, verts: List[Tuple[float, float]]) -> None:
        if len(verts) < 3:  # Need at least a triangle
            raise ValueError("Polygon needs at least 3 vertices.")
        self.verts = verts  # Keep original vertex list
        # Fan-triangulate from vertex 0. This works correctly for convex
        # polygons and "well-behaved" concave ones drawn manually by the user.
        self.triangles: List[Tuple[Any, Any, Any]] = []  # List of triangles
        self.weights: List[float] = []  # Area weights for proportional sampling
        for i in range(1, len(verts) - 1):  # Build the fan
            tri = (verts[0], verts[i], verts[i + 1])  # Triangle from anchor
            self.triangles.append(tri)  # Save triangle
            self.weights.append(_triangle_area(*tri))  # Save its area
        total = sum(self.weights) or 1.0  # Avoid div-by-zero
        # Pre-compute cumulative distribution for O(log n) sampling.
        self.cdf: List[float] = []  # Cumulative weights, normalized
        acc = 0.0
        for w in self.weights:
            acc += w / total  # Normalize
            self.cdf.append(acc)  # Save

    def sample(self) -> Tuple[float, float]:
        """Return one uniformly-random point inside the polygon."""
        r = random.random()  # Uniform [0,1)
        # Find the first cdf entry >= r (linear scan is fine for small N).
        idx = 0
        for i, c in enumerate(self.cdf):
            if r <= c:  # Found containing bucket
                idx = i
                break
        tri = self.triangles[idx]  # Pick that triangle
        return _random_point_in_triangle(*tri)  # Uniform inside it


# ---------------------------------------------------------------------------
# Click-spam worker (used by functions 1, 2 and 7)
# ---------------------------------------------------------------------------
class ClickWorker:
    """
    A background worker that pulls (x, y) tuples from a queue and taps them
    through the ADBController. Designed for *guaranteed* stop:

      * stop_event -- prevents new taps from leaving the queue.
      * queue drain -- discards buffered taps when stop is requested.
      * adb.kill_shell() -- nuclear option that severs the channel entirely.
    """

    def __init__(self, adb: ADBController) -> None:
        self.adb = adb  # Reference to ADB controller
        self.q: queue.Queue = queue.Queue(maxsize=256)  # Bounded backlog
        self.stop_event = threading.Event()  # Set => worker stops sending
        self.stop_event.set()  # Start in stopped state
        self.thread: Optional[threading.Thread] = None  # Worker thread

    def start(self) -> None:
        """Begin processing taps from the queue."""
        # Drain anything left from a previous run before unblocking the flag.
        self._drain()
        self.stop_event.clear()  # Allow taps to flow
        if self.thread is None or not self.thread.is_alive():  # Spin up if needed
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self, kill_shell: bool = False) -> None:
        """
        Stop sending taps and discard any buffered ones.

        kill_shell=False (default) is a "soft stop" used during normal
        toggle ON/OFF cycles: it sets the stop flag, drains the queue, and
        relies on `submit()` refusing new work to guarantee silence. The
        persistent adb shell stays alive so the NEXT start is instant.

        kill_shell=True is the nuclear option, intended only when exiting
        the calling function entirely. It terminates the persistent shell
        so that no buffered HID input could possibly leak out.
        """
        self.stop_event.set()  # Worker will exit its inner loop
        self._drain()  # Erase queued taps so they cannot fire later
        if kill_shell:
            # Hard guarantee: tear down the channel completely.
            self.adb.kill_shell()

    def submit(self, x: float, y: float) -> bool:
        """Enqueue a tap; returns False if dropped (stopped or full)."""
        if self.stop_event.is_set():  # Refuse new work while stopped
            return False
        try:
            self.q.put_nowait((x, y))  # Non-blocking put
            return True
        except queue.Full:
            return False  # Backpressure -- drop tap, caller can throttle

    def _drain(self) -> None:
        """Empty the queue without sending anything."""
        try:
            while True:  # Pop until empty
                self.q.get_nowait()
        except queue.Empty:
            pass  # Done

    def _run(self) -> None:
        """Worker loop -- runs in its own daemon thread."""
        while True:  # Live for the lifetime of the worker
            try:
                # Block for up to 0.1s waiting for an item.
                x, y = self.q.get(timeout=0.1)
            except queue.Empty:
                if self.stop_event.is_set():
                    # Idle and stopped -- park here but keep thread alive
                    # in case the user starts clicking again.
                    time.sleep(0.05)
                continue  # Loop again
            if self.stop_event.is_set():
                # Double-check after dequeue: if we stopped while waiting,
                # drop this tap on the floor.
                continue
            try:
                self.adb.tap(x, y)  # Actually send the tap
            except Exception as e:
                # Never let the worker thread die from a single failure.
                print(f"[!] tap failed: {e}")


# ---------------------------------------------------------------------------
# Function 7 (menu key '7') -- Game<->ADB calibration (4 points, bilinear)
# ---------------------------------------------------------------------------
def calibrate_map(adb: ADBController) -> None:
    """Capture 4 game points and 4 matching ADB points into calibration.json."""
    print("\n--- Map calibration (4 points, bilinear) ---")
    print("Enter four game points and the corresponding ADB points for each.")
    print("Recommended order: top-left, top-right, bottom-right, bottom-left.")
    game_points: List[Tuple[float, float]] = []  # Accumulator
    adb_points: List[Tuple[float, float]] = []  # Accumulator
    labels = ["#1 top-left", "#2 top-right", "#3 bottom-right", "#4 bottom-left"]
    for label in labels:  # Iterate the 4 anchors
        print(f"\nPoint {label}:")
        gx, gy = ask_xy(f"  Game X Y for {label}")  # In-game coords
        ax, ay = ask_xy(f"  ADB X Y for {label}")  # Emulator coords
        game_points.append((gx, gy))  # Save
        adb_points.append((ax, ay))  # Save
    # Persist to disk in the schema BilinearMap expects.
    save_json(CALIBRATION_FILE, {
        "game_points": game_points,
        "adb_points": adb_points,
        "type": "bilinear-4pt",  # Marker for future migrations
    })


# ---------------------------------------------------------------------------
# Function 3 (menu key '3') -- Points CRUD
# ---------------------------------------------------------------------------
def points_menu(adb: ADBController) -> None:
    """List / add / edit / delete game points stored in points.json."""
    while True:  # Sub-menu loop
        data = load_json(POINTS_FILE) or {"points": []}  # Default empty list
        pts: List[Dict[str, Any]] = data.get("points", [])  # Current points
        print("\n--- Points list ---")
        if not pts:  # Empty case
            print("  (no points yet)")
        for i, p in enumerate(pts, start=1):  # 1-based listing
            name = p.get("name", f"point{i}")  # Optional human name
            print(f"  {i}. {name}: game=({p['x']:.1f}, {p['y']:.1f})")
        print("  a) add   e) edit   d) delete   b) back")
        try:
            choice = ask("Choose action").lower()  # Sub-menu input
        except BackToMenu:
            return  # User pressed 'b' at the prompt
        if choice == "b":  # Explicit back
            return
        if choice == "a":  # Add a point
            name = ask("Name (optional)") or f"point{len(pts) + 1}"  # Default name
            gx, gy = ask_xy("Game X Y")  # Coordinates
            pts.append({"name": name, "x": gx, "y": gy})  # Append entry
            save_json(POINTS_FILE, {"points": pts})  # Persist
        elif choice == "e":  # Edit
            if not pts:
                print("[!] Nothing to edit.")
                continue
            idx = ask_int("Index to edit", min_v=1, max_v=len(pts)) - 1  # 0-based
            name = ask("New name (blank=keep)")  # Optional rename
            gx, gy = ask_xy("New Game X Y")  # New coords
            if name:
                pts[idx]["name"] = name  # Update name
            pts[idx]["x"], pts[idx]["y"] = gx, gy  # Update coords
            save_json(POINTS_FILE, {"points": pts})  # Persist
        elif choice == "d":  # Delete
            if not pts:
                print("[!] Nothing to delete.")
                continue
            idx = ask_int("Index to delete", min_v=1, max_v=len(pts)) - 1  # 0-based
            removed = pts.pop(idx)  # Remove entry
            print(f"[+] Removed {removed.get('name', '<unnamed>')}")
            save_json(POINTS_FILE, {"points": pts})  # Persist
        else:
            print("[!] Unknown action.")  # Bad input


# ---------------------------------------------------------------------------
# Function 4 (menu key '4') -- Polygon clicks
# ---------------------------------------------------------------------------
def _polygon_producer(worker: "ClickWorker",
                      sampler: PolygonSampler,
                      state: Dict[str, bool],
                      interval: float,
                      stop_flag: threading.Event) -> None:
    """Helper used by polygon_clicks (kept top-level so it's reusable)."""
    next_t = time.monotonic()  # Schedule the next click time
    while not stop_flag.is_set():  # Run until the outer function tells us
        if state.get("running"):  # Only enqueue when toggled on
            x, y = sampler.sample()  # Random point in ADB space
            worker.submit(x, y)  # Enqueue tap
        next_t += interval  # Advance schedule
        delay = next_t - time.monotonic()  # How long to wait
        if delay > 0:
            time.sleep(delay)  # Wait the remaining time
        else:
            next_t = time.monotonic()  # Re-base if we fell behind


def polygon_clicks(adb: ADBController) -> None:
    """Define a polygon by game vertices, then spam random clicks inside it."""
    calib = load_calibration()  # Need calibration to translate coords
    if calib is None:
        print("[!] No calibration found. Run option 7 first.")
        return
    print("\n--- Polygon click region ---")
    print("Enter polygon vertices in game coordinates (>=3 points).")
    print("Type 'done' (or press Enter on an empty line) to finish.")
    verts: List[Tuple[float, float]] = []  # Game-space vertices
    while True:  # Collect vertices
        try:
            raw = ask(f"Vertex #{len(verts) + 1} 'X Y' or 'done'")
        except BackToMenu:
            return
        if raw.lower() in ("done", ""):
            if len(verts) < 3:
                print("[!] Need at least 3 vertices.")
                continue
            break
        parts = raw.replace(",", " ").split()
        if len(parts) != 2:
            print("[!] Enter two numbers.")
            continue
        try:
            verts.append((float(parts[0]), float(parts[1])))
        except ValueError:
            print("[!] Numbers only.")
    adb_verts = [calib.game_to_adb(x, y) for x, y in verts]  # Pre-transform
    sampler = PolygonSampler(adb_verts)  # Build sampler in ADB space
    cps = ask_float("Clicks per second (CPS)", min_v=0.1)  # User CPS
    interval = 1.0 / cps  # Delay between clicks
    worker = ClickWorker(adb)  # Background tapper
    state: Dict[str, bool] = {"running": False}  # Toggle flag
    stop_flag = threading.Event()  # Tells producer/listener to exit
    print("\nPress MIDDLE mouse button to START/STOP spamming.")
    print("Type 'q' then Enter here to return to main menu.")

    def on_click(x, y, button, pressed):  # pynput callback
        if pressed and button == mouse.Button.middle:  # Toggle on middle press
            state["running"] = not state["running"]
            if state["running"]:
                worker.start()  # Resume sending
                print("[*] Polygon spam: ON")
            else:
                worker.stop(kill_shell=False)  # Soft stop: drain queue, keep shell
                print("[*] Polygon spam: OFF")

    listener = mouse.Listener(on_click=on_click)  # Async listener
    listener.start()  # Begin observing
    producer = threading.Thread(  # Producer thread feeding the queue
        target=_polygon_producer,
        args=(worker, sampler, state, interval, stop_flag),
        daemon=True,
    )
    producer.start()  # Go
    try:
        while True:  # Foreground input loop = "type q + Enter to leave"
            line = input()
            if line.strip().lower() == "q":
                break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop_flag.set()  # Tell producer to exit
        worker.stop(kill_shell=True)  # Hard stop on function exit
        listener.stop()  # Stop mouse listener


# ---------------------------------------------------------------------------
# Function 5 (menu key '5') -- Settings/ZoomOut button + swipe calibration
# ---------------------------------------------------------------------------
def calibrate_zoom(adb: ADBController) -> None:
    """Store ADB coords for the Settings and Zoom-Out buttons plus a swipe."""
    print("\n--- Zoom calibration ---")
    print("Step 1/3: Settings button (ADB coords only).")
    ax_s, ay_s = ask_xy("  ADB X Y of the Settings button")  # Emulator pixel
    print("Step 2/3: Zoom Out button (ADB coords only).")
    ax_z, ay_z = ask_xy("  ADB X Y of the Zoom Out button")  # Emulator pixel
    print("Step 3/3: Map drag swipe (in ADB coordinates).")
    sx1, sy1 = ask_xy("  Swipe start ADB X Y")  # Grab start
    sx2, sy2 = ask_xy("  Swipe end   ADB X Y")  # Drop end
    dur = ask_int("  Swipe duration in milliseconds (e.g. 400)", min_v=50)  # Speed
    save_json(ZOOM_CALIB_FILE, {  # Persist everything for option 1
        "settings_button": {"adb": [ax_s, ay_s]},  # ADB-only now
        "zoom_out_button": {"adb": [ax_z, ay_z]},  # ADB-only now
        "swipe": {"start": [sx1, sy1], "end": [sx2, sy2], "duration_ms": dur},
    })


# ---------------------------------------------------------------------------
# Function 6 (menu key '6') -- Clicker speed (CPS) setting
# ---------------------------------------------------------------------------
def clicker_options(adb: ADBController) -> None:
    """
    Configure the main clicker (option 1):
      * CPS: how many taps per second across the point list.
      * Offset (dx, dy): a constant ADB-coordinate offset added to every
        tap. Use this to compensate for the "all points shifted in one
        direction" symptom you hit on macOS without redoing the full map
        calibration. Negative values are allowed.
    """
    current = load_json(CLICKER_OPT_FILE) or {}  # Current value (if any)
    if "cps" in current:
        print(f"Current CPS: {current['cps']}")  # Show existing for context
    cps = ask_float("Clicks per second per point", min_v=0.1)  # Ask new value
    cur_dx = float(current.get("offset_x", 0.0))  # Current X offset
    cur_dy = float(current.get("offset_y", 0.0))  # Current Y offset
    print(f"Current global ADB offset: dx={cur_dx} dy={cur_dy}")
    # Allow user to keep current by entering 0 for both, or just press 'b'.
    print("Enter a constant ADB pixel offset added to every tap.")
    print("If your clicks land consistently 30px below and 10px right of")
    print("the target, enter dx=-10 dy=-30 to compensate.")
    dx = ask_float("Offset X (can be negative)")  # New X offset
    dy = ask_float("Offset Y (can be negative)")  # New Y offset
    save_json(CLICKER_OPT_FILE, {  # Persist all settings
        "cps": cps,
        "offset_x": dx,
        "offset_y": dy,
    })


# ---------------------------------------------------------------------------
# Function 1 (menu key '1') -- Main clicker
# ---------------------------------------------------------------------------
def main_clicker(adb: ADBController) -> None:
    """
    Full sequence:
      1) Read points.json + calibration_zoom.json + clicker_option.json.
      2) On middle-mouse-press: center-click, settings, zoom-out, swipe,
         wait 1s, then spam taps on each saved point at the configured CPS.
      3) Another middle-mouse press stops the spam (and kills the ADB shell
         to guarantee no in-flight taps reach the device).
    """
    calib = load_calibration()  # Game->ADB transform
    if calib is None:
        print("[!] No calibration. Run option 7 first.")
        return
    pts_data = load_json(POINTS_FILE) or {"points": []}  # Saved points
    pts = pts_data.get("points", [])  # Extract list
    if not pts:
        print("[!] points.json is empty. Run option 3 first.")
        return
    zoom = load_json(ZOOM_CALIB_FILE)  # Settings/zoom/swipe data
    if not zoom:
        print("[!] No zoom calibration. Run option 5 first.")
        return
    opt = load_json(CLICKER_OPT_FILE) or {}  # CPS + offsets
    cps = float(opt.get("cps", 10.0))  # Default 10 CPS if not configured
    interval = 1.0 / cps  # Time between clicks
    # Global ADB offset (applied to every tap) -- compensates the "all
    # points shifted in one direction" symptom without redoing the full
    # 4-point calibration. Configure via option 6.
    off_x = float(opt.get("offset_x", 0.0))
    off_y = float(opt.get("offset_y", 0.0))
    if off_x or off_y:
        print(f"[i] Applying global ADB offset: dx={off_x} dy={off_y}")
    # Pre-translate all game-space points into ADB-space ones for speed,
    # then apply the offset once so we never recompute it per click.
    adb_targets = [
        (gx + off_x, gy + off_y)
        for gx, gy in (calib.game_to_adb(p["x"], p["y"]) for p in pts)
    ]
    settings_adb = tuple(zoom["settings_button"]["adb"])  # (x, y)
    zoomout_adb = tuple(zoom["zoom_out_button"]["adb"])  # (x, y)
    swipe = zoom["swipe"]  # Dict with start/end/duration

    worker = ClickWorker(adb)  # Background tapper
    state: Dict[str, bool] = {"running": False, "want_exit": False}  # Flags
    stop_flag = threading.Event()  # Producer exit flag

    def producer():
        """Cycle through saved points at the configured CPS while running."""
        idx = 0  # Round-robin index
        next_t = time.monotonic()  # Schedule
        while not stop_flag.is_set():
            if state["running"]:  # Only when toggled on
                x, y = adb_targets[idx]  # Pick next target
                worker.submit(x, y)  # Enqueue tap
                idx = (idx + 1) % len(adb_targets)  # Advance with wrap-around
            next_t += interval
            d = next_t - time.monotonic()
            if d > 0:
                time.sleep(d)
            else:
                next_t = time.monotonic()  # Re-base on overrun

    def run_intro_sequence():
        """Settings -> zoom-out -> swipe -> 1s wait. (No center tap.)"""
        adb.tap(*settings_adb)  # Step 1: open settings menu
        time.sleep(0.4)  # Wait for menu animation
        adb.tap(*zoomout_adb)  # Step 2: zoom out
        time.sleep(0.4)  # Wait for zoom animation
        adb.swipe(swipe["start"][0], swipe["start"][1],  # Step 3: drag map
                  swipe["end"][0], swipe["end"][1],
                  int(swipe.get("duration_ms", 400)))
        time.sleep(1.0)  # Step 4: 1s settle before spam, per spec

    def on_click(x, y, button, pressed):  # pynput callback
        if pressed and button == mouse.Button.middle:  # Toggle event
            if not state["running"]:  # OFF -> ON
                print("[*] Main clicker: starting intro sequence...")
                # Run the intro *off* the listener thread to avoid blocking.
                threading.Thread(target=_start_main, daemon=True).start()
            else:  # ON -> OFF
                state["running"] = False  # Stop the producer from enqueueing
                worker.stop(kill_shell=False)  # Soft stop: keep shell alive
                print("[*] Main clicker: STOPPED")

    def _start_main():
        """Helper to perform the intro then enable spam."""
        try:
            run_intro_sequence()  # Centered click + UI navigation + swipe
        except Exception as e:
            print(f"[!] Intro sequence failed: {e}")
            return
        worker.start()  # Reset stop flag and resume tapping
        state["running"] = True  # Producer will start enqueueing
        print(f"[*] Main clicker: RUNNING at {cps} CPS over {len(adb_targets)} points")

    listener = mouse.Listener(on_click=on_click)  # Async listener
    listener.start()  # Begin observing the middle mouse button
    prod_thread = threading.Thread(target=producer, daemon=True)  # Click producer
    prod_thread.start()  # Begin scheduling

    print("\nMain clicker armed.")
    print("  Middle mouse button: START / STOP")
    print("  Type 'q' then Enter here to return to main menu.")
    try:
        while True:
            line = input()  # Foreground stays responsive
            if line.strip().lower() == "q":
                break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop_flag.set()  # Producer exit
        state["running"] = False  # Make sure flag is off
        worker.stop(kill_shell=True)  # Hard stop on function exit
        listener.stop()  # Detach mouse listener


# ---------------------------------------------------------------------------
# Function 2 helpers: window calibration + cursor->ADB mapping
# ---------------------------------------------------------------------------
def _calibrate_window_interactive(adb: ADBController) -> Optional[ScreenToAdb]:
    """
    Build a ScreenToAdb mapping by asking the user to hover over the
    TOP-LEFT and BOTTOM-RIGHT corners of the actual GAME area inside
    BlueStacks (i.e. the playable rectangle, excluding the BlueStacks
    toolbar / title bar / side controls).

    NOTE: Earlier versions of this function offered an "auto-detect + accept"
    shortcut that used the raw OS window bounding box. That turned out to be
    inaccurate because the OS bbox always includes the BlueStacks chrome,
    which made clicks land in the wrong place. The auto-detected rectangle
    is now only PRINTED as a reference; the user still confirms two anchor
    points manually so the calibration matches exactly the playable area.
    """
    if not adb.screen_size:  # Make sure we know the emulator resolution
        adb.screen_size = adb._query_screen_size()  # Lazy refresh
    adb_w, adb_h = adb.screen_size  # Unpack
    # Informational only: show where the OS thinks BlueStacks is.
    locator = WindowLocator()
    rect = locator.find()
    if rect is not None:
        print(f"[i] OS-detected BlueStacks window (FYI only): "
              f"x={rect['x']} y={rect['y']} w={rect['w']} h={rect['h']}")
    return _manual_window_calibration(adb_w, adb_h)


# ---------------------------------------------------------------------------
# Native cursor readers
# ---------------------------------------------------------------------------
# History of crashes that led to this implementation:
#
#   1) First version used Quartz.CGEventCreate(None) from a producer thread.
#      That triggered CoreGraphics asserts because there is no CFRunLoop in
#      the calling thread -> SIGSEGV.
#
#   2) Second version used pynput.mouse.Controller.position to "go through
#      a thread-safe API". On macOS 26.3 that pynput call goes through
#      HIToolbox's TSMGetInputSourceProperty which has a HARD assertion that
#      it must run on the main dispatch queue. From a background thread it
#      now triggers dispatch_assert_queue_fail -> SIGTRAP. This is exactly
#      the crash log the user just sent (Thread 12, TSM in HIToolbox).
#
#   3) Final (current) version: on macOS we read AppKit NSEvent.mouseLocation()
#      DIRECTLY. This call:
#          - is documented as thread-safe (it just reads HID state)
#          - does NOT touch HIToolbox / TSM / input sources
#          - does NOT require CFRunLoop
#          - returns coordinates in Cocoa's bottom-left origin system, so we
#            FLIP Y against the main screen's height to get the same
#            top-left coordinates that pynput returned during calibration.
#
# The Y-flip is what caused the "constant offset" the user has been chasing
# all along: NSEvent gives Y from the bottom of the main screen; pynput gave
# Y from the top. Calibration anchors were captured with one convention,
# clicks were dispatched with the other -> systematic Y-axis offset.

_macos_main_screen_height: Optional[float] = None  # Cached on first read


def _macos_get_main_screen_height() -> Optional[float]:
    """Return the *logical* height of the main display on macOS, or None."""
    global _macos_main_screen_height  # Module-level cache
    if _macos_main_screen_height is not None:
        return _macos_main_screen_height
    try:
        from AppKit import NSScreen  # type: ignore (pyobjc)
        screens = NSScreen.screens()  # All screens
        if not screens:
            return None
        # mainScreen() returns the one with key focus -- not what we want.
        # The main display in Cocoa terms is screens[0] (NSScreen ordering),
        # whose frame origin is (0, 0). All other screens are positioned
        # relative to it. NSEvent.mouseLocation() uses screens[0] for the
        # bottom-left origin we need to flip.
        _macos_main_screen_height = float(screens[0].frame().size.height)
        return _macos_main_screen_height
    except Exception:
        return None


def _read_cursor_logical() -> Tuple[float, float]:
    """
    Return the cursor position in *logical* top-left screen pixels.

    macOS:    AppKit NSEvent.mouseLocation() then flip Y against the main
              screen height. Thread-safe, no HIToolbox / CFRunLoop / TSM
              involvement. This is what we should have been using from the
              start.

    Windows:  pynput.mouse.Controller.position -> GetCursorPos. Safe.
    Linux:    pynput.mouse.Controller.position -> XQueryPointer. Safe.
    """
    if _IS_MAC:
        try:
            from AppKit import NSEvent  # type: ignore (pyobjc-framework-Cocoa)
            loc = NSEvent.mouseLocation()  # Cocoa bottom-left coordinates
            sh = _macos_get_main_screen_height()
            if sh is None:
                # Fall through to pynput as a last resort (may crash on 26.3
                # but we have nothing better in that case).
                return mouse.Controller().position
            # Flip Y: top-left origin is what calibration anchors use.
            return float(loc.x), float(sh - loc.y)
        except Exception:
            # Any pyobjc problem -- fall back. Better wrong than crashed.
            return mouse.Controller().position
    # Non-macOS platforms: pynput is safe and consistent.
    return mouse.Controller().position


def _manual_window_calibration(adb_w: int, adb_h: int) -> Optional[ScreenToAdb]:
    """Ask the user to hover over the game-area corners and press Enter."""
    print("\n--- Manual window calibration ---")
    print("You will record TWO anchor points by HOVERING the cursor and")
    print("pressing Enter. Anchor the corners of the PLAYABLE GAME AREA,")
    print("not the BlueStacks toolbar / window border.")
    print()
    print("Step 1/2: move the cursor to the TOP-LEFT corner of the GAME area.")
    try:
        input("Press Enter when the cursor is positioned correctly... ")
    except (EOFError, KeyboardInterrupt):
        return None  # User aborted
    tl = _read_cursor_logical()  # Snapshot cursor position
    print(f"  Top-left  screen XY = ({tl[0]:.1f}, {tl[1]:.1f})")
    print("Step 2/2: move the cursor to the BOTTOM-RIGHT corner of the GAME area.")
    try:
        input("Press Enter when the cursor is positioned correctly... ")
    except (EOFError, KeyboardInterrupt):
        return None
    br = _read_cursor_logical()  # Snapshot cursor position
    print(f"  Bottom-right screen XY = ({br[0]:.1f}, {br[1]:.1f})")
    if br[0] <= tl[0] or br[1] <= tl[1]:  # Sanity check
        print("[!] Bottom-right must be below-and-right of top-left.")
        return None
    mapping = ScreenToAdb(tl, br, adb_w, adb_h)  # Build mapping
    save_window_calibration(tl, br, adb_w, adb_h)  # Persist for next time
    print(f"[+] Calibration saved. Emulator resolution: {adb_w}x{adb_h}")
    print(f"    Screen rect width x height: "
          f"{br[0] - tl[0]:.1f} x {br[1] - tl[1]:.1f} (logical px)")
    print(f"    Scale factors: kx={mapping.kx:.4f}, ky={mapping.ky:.4f}")
    return mapping


# ---------------------------------------------------------------------------
# Function 2 (menu key '2') -- Hold-to-spam at the *current cursor location*
# ---------------------------------------------------------------------------
def hold_to_spam(adb: ADBController) -> None:
    """
    While the user holds the side mouse button (or Ctrl), spam ADB left
    clicks at the emulator point that corresponds to the *current* host
    cursor position. The cursor position is re-read for every tap.

    Workflow:
      1) Load or build a window calibration (auto-detect first, manual
         fallback). The calibration maps screen px -> ADB px.
      2) Start mouse + keyboard listeners.
      3) A producer thread polls the cursor while the user is holding the
         activator and submits ADB taps to the ClickWorker. The cursor is
         read FRESH for every tap so movement is reflected instantly.
      4) Press 'r' + Enter to re-calibrate without leaving the function
         (e.g. after the user moved or resized BlueStacks).
      5) Press 'q' + Enter to return to the main menu.
    """
    if not adb.screen_size:  # Ensure we have emulator resolution
        adb.screen_size = adb._query_screen_size()
    adb_w, adb_h = adb.screen_size  # Cache
    # Try cached calibration first; only prompt if missing.
    mapping = load_window_calibration(adb_w, adb_h)
    if mapping is None:
        mapping = _calibrate_window_interactive(adb)
    if mapping is None:  # User cancelled
        return
    print("\n--- Hold-to-spam (follow cursor) ---")
    print("  Hold SIDE MOUSE BUTTON  =>  spam clicks at cursor (release to stop)")
    print("  Press CAPS LOCK         =>  toggle spam ON/OFF (LED reflects state)")
    print("  Type 'r' + Enter        =>  re-calibrate the BlueStacks window")
    print("  Type 'd' + Enter        =>  debug: print one cursor/ADB sample")
    print("  Type 'q' + Enter        =>  return to main menu")

    worker = ClickWorker(adb)  # Background ADB tapper with safe-stop
    holding = {"mouse": False, "kbd": False}  # Activator state (OR-combined)
    stop_flag = threading.Event()  # Producer thread exit flag
    # Producer rate: 200 CPS was previously specified, but in practice the
    # persistent adb shell + `input tap` saturates around 30-50 CPS on
    # BlueStacks. Scheduling faster than the channel can drain just fills
    # the bounded queue with stale (X,Y) pairs that fire LATER than the
    # cursor sample was taken -- which is exactly the "random misses while
    # the cursor is still" symptom. We cap the producer at 30 CPS so each
    # enqueued tap corresponds to a cursor sample no older than ~30ms.
    target_cps = 30.0  # Producer rate ceiling -- match real ADB throughput
    interval = 1.0 / target_cps  # Scheduler tick (~33ms)
    # Open a debug log alongside the script. Every tap (and every error)
    # is appended so the user can diagnose miss patterns offline.
    debug_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "debug.log")
    try:
        debug_log = open(debug_log_path, "a", buffering=1, encoding="utf-8")
        debug_log.write(f"\n=== session start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    except OSError:
        debug_log = None  # Logging is best-effort
    log_every = 10  # Write a log line every Nth tap to keep file size sane
    log_counter = {"n": 0}  # Mutable counter for nested function

    # Shared mutable mapping holder so the producer picks up re-calibrations
    # without needing to be restarted.
    mapping_ref = {"m": mapping}  # Wrapped so it's mutable from inner fns

    # Cursor reads always go through _read_cursor_logical() -- this is the
    # single source of truth used both during calibration and during click
    # production. Using the *same* read path on both ends is what guarantees
    # the math is consistent across macOS Retina / Windows HiDPI / Linux.

    def is_holding() -> bool:
        """True iff either activator (mouse side or Caps Lock) is engaged."""
        return holding["mouse"] or holding["kbd"]

    def producer():
        """
        Per-tick loop: read cursor, map to ADB, submit one tap.

        We deliberately bypass the worker's queue and call adb.tap() DIRECTLY
        from this thread. Reasoning: with a queue, the (X,Y) pair sits there
        until the consumer drains it, by which time the cursor may have moved
        -- so even with fresh sampling here, the actual tap reflects a stale
        sample. Direct dispatch keeps "sampled" and "sent" tightly bound,
        which is exactly the behaviour the user wants.

        The persistent ADB shell can still buffer commands, but since it
        processes them in FIFO order at full speed, queue depth stays at 1.
        """
        next_t = time.monotonic()  # Schedule clock
        was_holding = False  # For rising/falling edge transitions
        last_warn = 0.0  # Throttle out-of-window warnings (one per second)
        while not stop_flag.is_set():
            now_holding = is_holding()  # Sample activator state
            if now_holding and not was_holding:  # Rising edge: start tapping
                worker.start()  # Reset stop_event (so worker.submit won't refuse)
                print("[*] Spam: ON")
                if debug_log:
                    debug_log.write(f"[{time.strftime('%H:%M:%S')}] SPAM ON\n")
            elif (not now_holding) and was_holding:  # Falling edge: stop
                worker.stop(kill_shell=False)  # Soft stop: drain queue, keep shell warm
                print("[*] Spam: OFF")
                if debug_log:
                    debug_log.write(f"[{time.strftime('%H:%M:%S')}] SPAM OFF\n")
            if now_holding:
                # FRESH cursor sample on EVERY tick (Quartz on macOS, pynput
                # elsewhere). Sample three times back-to-back and pick the
                # median to filter out any single bad read.
                samples = [_read_cursor_logical() for _ in range(3)]
                samples.sort()  # Lexicographic sort gives median at index 1
                sx, sy = samples[1]
                m = mapping_ref["m"]  # Current calibration
                if m.in_window(sx, sy):  # Cursor inside calibrated area
                    ax, ay = m.to_adb(sx, sy)  # Map to ADB coordinates
                    # Direct dispatch -- do NOT use worker.submit() here so
                    # the time between "sample cursor" and "send tap" stays
                    # minimal and there is no queue lag.
                    try:
                        adb.tap(ax, ay)
                    except Exception as e:
                        if debug_log:
                            debug_log.write(f"[ERR] tap failed: {e}\n")
                    # Log every Nth tap so we have a trail to debug misses.
                    log_counter["n"] += 1
                    if debug_log and (log_counter["n"] % log_every == 0):
                        debug_log.write(
                            f"[{time.strftime('%H:%M:%S')}] "
                            f"#{log_counter['n']} "
                            f"screen=({sx:.1f},{sy:.1f}) "
                            f"adb=({ax:.1f},{ay:.1f}) "
                            f"tl=({m.tl[0]:.1f},{m.tl[1]:.1f}) "
                            f"br=({m.br[0]:.1f},{m.br[1]:.1f})\n"
                        )
                else:
                    now = time.monotonic()
                    if now - last_warn > 1.0:
                        print(f"[!] Cursor ({sx:.0f},{sy:.0f}) outside calibrated "
                              f"BlueStacks area -- move it back, or 'r'+Enter to recalibrate.")
                        last_warn = now
            was_holding = now_holding  # Save edge state
            next_t += interval  # Advance schedule
            d = next_t - time.monotonic()
            if d > 0:
                time.sleep(d)  # Wait the remaining slice
            else:
                next_t = time.monotonic()  # Re-base on overrun

    def on_click(_x, _y, button, pressed):
        """pynput mouse callback: track side-button hold state."""
        # Side buttons surface under different names on different OSes.
        side_buttons = set()
        for name in ("x1", "x2", "button8", "button9"):
            if hasattr(mouse.Button, name):
                side_buttons.add(getattr(mouse.Button, name))
        if button in side_buttons:
            holding["mouse"] = pressed  # True on press, False on release

    # --- Caps Lock activator -------------------------------------------
    # Caps Lock is a "toggle" key on every OS: pynput delivers a press +
    # release pair every time the user presses it, regardless of how long
    # the key is physically held down. That makes a literal "hold" semantic
    # impossible to detect from key events alone.
    #
    # We therefore use Caps Lock as a TOGGLE: first press starts the spam,
    # the next press stops it. Visually the user can still treat the Caps
    # Lock LED as the spam-status indicator (LED on = spam on).
    def on_press(key):
        """pynput keyboard callback: Caps Lock toggles spam on/off."""
        if key == keyboard.Key.caps_lock:  # Only react to Caps Lock
            # Flip the kbd-activator flag; producer picks it up on next tick.
            holding["kbd"] = not holding["kbd"]

    def on_release(_key):
        """Caps Lock is handled as a toggle on press; ignore releases."""
        # Intentionally empty: state is driven entirely by on_press toggles.
        return None

    mlistener = mouse.Listener(on_click=on_click)  # Mouse listener
    klistener = keyboard.Listener(on_press=on_press, on_release=on_release)  # Kbd listener
    mlistener.start()  # Begin observing the mouse
    klistener.start()  # Begin observing the keyboard
    prod_thread = threading.Thread(target=producer, daemon=True)  # Cursor follower
    prod_thread.start()  # Go

    try:
        while True:
            line = input().strip().lower()  # Foreground command loop
            if line == "q":
                break  # Exit to main menu
            if line == "r":
                # Pause spamming during recalibration so we don't fire taps
                # at random points while the user is moving the cursor.
                holding["mouse"] = False  # Force-disengage both activators
                holding["kbd"] = False  # so spam stops during prompts
                worker.stop(kill_shell=False)  # Pause taps during recalibration
                new_map = _calibrate_window_interactive(adb)  # New calibration
                if new_map is not None:
                    mapping_ref["m"] = new_map  # Swap atomically
                    print("[+] Calibration updated.")
                else:
                    print("[!] Calibration cancelled; keeping previous one.")
            elif line == "d":
                # Diagnostic helper: read the cursor, compute the mapped ADB
                # point, send EXACTLY ONE tap, and print everything. Use this
                # to verify the calibration is correct: place the cursor on a
                # known game element, press 'd'+Enter, and check whether the
                # tap landed where the cursor is.
                sx, sy = _read_cursor_logical()  # Same path as the producer
                m = mapping_ref["m"]
                inside = m.in_window(sx, sy)
                ax, ay = m.to_adb(sx, sy)
                print("---- DEBUG SAMPLE ----")
                print(f"  Cursor screen XY  : ({sx:.1f}, {sy:.1f})  in_window={inside}")
                print(f"  Calibration TL/BR : ({m.tl[0]:.1f},{m.tl[1]:.1f}) / "
                      f"({m.br[0]:.1f},{m.br[1]:.1f})")
                print(f"  Calibration scale : kx={m.kx:.4f}  ky={m.ky:.4f}")
                print(f"  Emulator size     : {m.adb_w} x {m.adb_h}")
                print(f"  Computed ADB XY   : ({ax:.1f}, {ay:.1f})")
                # Send a single tap directly (bypass the worker/queue) so the
                # diagnostic is independent of the spam pipeline.
                try:
                    adb.tap(ax, ay)
                    print(f"  Sent tap -> compare with what BlueStacks shows.")
                except Exception as e:
                    print(f"  [!] tap failed: {e}")
                print("----------------------")
            # Any other input is ignored intentionally.
    except (KeyboardInterrupt, EOFError):
        pass  # Treat as 'q'
    finally:
        stop_flag.set()  # Producer exit
        holding["mouse"] = False  # Ensure clean disengage
        holding["kbd"] = False
        worker.stop(kill_shell=True)  # Hard stop on function exit
        mlistener.stop()  # Detach listeners
        klistener.stop()
        if debug_log:
            try:
                debug_log.write(f"=== session end {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                debug_log.close()  # Flush + close
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------
MENU_TEXT = """
=========== Post Apo Tycoon Clicker ===========
  1) Run main clicker (uses points + zoom calib)
  2) Hold-to-spam mode (side mouse button / Ctrl)
  3) Manage points list (add / edit / delete)
  4) Polygon area click region
  5) Calibrate Settings + Zoom-Out buttons + swipe
  6) Configure clicker speed (CPS)
  7) Calibrate game<->ADB coordinates (4 points)
  0) Quit
================================================
"""  # Static menu text shown each loop


def main() -> None:
    """Entry point: connect to ADB then loop the menu."""
    print(f"[*] Platform: {platform.system()} {platform.release()}")  # Debug info
    adb = ADBController()  # Locate adb + prepare to connect
    try:
        adb.connect()  # Connect to BlueStacks
    except Exception as e:
        print(f"[!] Failed to initialize ADB: {e}")
        sys.exit(2)  # Cannot proceed without ADB
    # Dispatch table maps menu key -> (description, handler).
    actions = {
        "1": ("Main clicker", main_clicker),
        "2": ("Hold-to-spam", hold_to_spam),
        "3": ("Points list", points_menu),
        "4": ("Polygon clicks", polygon_clicks),
        "5": ("Zoom calibration", calibrate_zoom),
        "6": ("Clicker options", clicker_options),
        "7": ("Map calibration", calibrate_map),
    }
    try:
        while True:  # Main menu loop
            print(MENU_TEXT)  # Show menu
            try:
                choice = input("Select option: ").strip()  # Direct input (no 'b')
            except (EOFError, KeyboardInterrupt):
                print()  # Newline for tidiness
                break  # Treat Ctrl+C at menu as quit
            if choice == "0":  # Quit
                break
            handler = actions.get(choice)  # Look up the action
            if not handler:
                print("[!] Unknown option.")
                continue
            _, fn = handler  # Unpack
            try:
                fn(adb)  # Invoke; functions may raise BackToMenu
            except BackToMenu:
                continue  # User pressed 'b' inside the function
            except Exception as e:  # Don't crash the menu on a handler bug
                print(f"[!] Error in '{handler[0]}': {e}")
    finally:
        adb.kill_shell()  # Always release the persistent shell on exit
        print("Bye.")  # Final message


if __name__ == "__main__":  # Standard "python pat_clicker.py" entry
    main()  # Run the menu
