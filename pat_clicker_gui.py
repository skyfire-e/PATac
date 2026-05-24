#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Post Apo Tycoon ADB clicker -- Tk GUI front-end.
# Reuses ADB / calibration / worker logic from pat_clicker.py and exposes
# everything as buttons and number entries instead of CLI prompts.

import os  # File path helpers for log / config files
import sys  # Platform detection
import time  # Timing and timestamps
import threading  # Background workers (click loop, listeners, intro)
import queue  # Thread-safe log queue (worker thread -> GUI text widget)
from typing import Optional, Tuple, List, Dict, Any, Callable  # Type hints

# Tkinter is bundled with Python on Windows and macOS (python.org / Homebrew
# with the right options) and most Linux distros.
try:
    import tkinter as tk  # Core widget toolkit
    from tkinter import ttk  # Themed widgets
    from tkinter import messagebox, simpledialog  # Dialog helpers
except ImportError:
    print("[!] tkinter is not available in this Python install.")
    print("    macOS:  brew install python-tk@3.12  (or use python.org build)")
    print("    Ubuntu: sudo apt install python3-tk")
    sys.exit(1)

# pynput is required for mouse / keyboard listeners (Caps Lock activator,
# side button, cursor read on non-macOS). Imported here so we surface any
# missing-dependency message before opening a window.
try:
    from pynput import mouse, keyboard  # noqa: F401  (used indirectly)
except ImportError:
    print("[!] Missing dependency: pynput. Install with: pip install pynput")
    sys.exit(1)

# Reuse the full backend from pat_clicker.py: ADB wrapper, calibrations,
# polygon sampler, click worker, config I/O. Everything user-facing is
# rebuilt as a GUI here.
from pat_clicker import (
    ADBController,
    WindowLocator,
    ScreenToAdb,
    BilinearMap,
    PolygonSampler,
    ClickWorker,
    save_json,
    load_json,
    save_window_calibration,
    load_window_calibration,
    load_calibration,
    load_game_affine,
    save_game_affine,
    GameToAdbAffine,
    GAME_AFFINE_CALIB_FILE,
    GAME_UI_ROI_FILE,
    GAME_ANCHORS_FILE,
    _read_cursor_logical,
    CALIBRATION_FILE,
    POINTS_FILE,
    ZOOM_CALIB_FILE,
    CLICKER_OPT_FILE,
    WINDOW_CALIB_FILE,
    WINDOW_CALIB_FILE_FN1,
    FN1_CORRECTION_FILE,
)
# All five window-calibration strategies live in a separate module so the
# GUI file stays focused on UI.
import pat_calibration as calib_mod
import pat_calibration_v2 as calib_v2
# Polling-based activator state (Caps Lock toggle, mouse side button).
# This module never installs a global event tap, so it works without Input
# Monitoring permission on macOS and never crashes the background thread.
import pat_input


# ---------------------------------------------------------------------------
# Small Tk helpers
# ---------------------------------------------------------------------------
def ask_number(parent: tk.Tk, title: str, prompt: str,
               initial: float = 0.0,
               minvalue: Optional[float] = None,
               maxvalue: Optional[float] = None,
               integer: bool = False) -> Optional[float]:
    """Modal numeric dialog. Returns the entered number or None on cancel."""
    if integer:
        return simpledialog.askinteger(  # Built-in Tk dialog with validation
            title, prompt, parent=parent,
            initialvalue=int(initial) if initial is not None else 0,
            minvalue=int(minvalue) if minvalue is not None else None,
            maxvalue=int(maxvalue) if maxvalue is not None else None,
        )
    return simpledialog.askfloat(  # Same for floats
        title, prompt, parent=parent,
        initialvalue=float(initial) if initial is not None else 0.0,
        minvalue=float(minvalue) if minvalue is not None else None,
        maxvalue=float(maxvalue) if maxvalue is not None else None,
    )


class XYDialog(tk.Toplevel):
    """Modal dialog that asks for two numbers (X, Y) in a compact form."""

    def __init__(self, parent: tk.Tk, title: str, prompt: str,
                 initial_x: float = 0.0, initial_y: float = 0.0) -> None:
        super().__init__(parent)  # Init Toplevel
        self.title(title)  # Title bar
        self.resizable(False, False)  # Compact, no resize
        self.transient(parent)  # Tie to parent (stays on top)
        self.grab_set()  # Modal: block parent until closed
        self.result: Optional[Tuple[float, float]] = None  # Output

        # Prompt label spans both columns of the grid.
        ttk.Label(self, text=prompt, padding=(10, 8)).grid(
            row=0, column=0, columnspan=2, sticky="w")

        # X entry with default value.
        ttk.Label(self, text="X:").grid(row=1, column=0, padx=(10, 0), pady=4, sticky="e")
        self.var_x = tk.StringVar(value=str(initial_x))  # Text -> StringVar
        ttk.Entry(self, textvariable=self.var_x, width=12).grid(
            row=1, column=1, padx=(4, 10), pady=4, sticky="w")
        # Y entry.
        ttk.Label(self, text="Y:").grid(row=2, column=0, padx=(10, 0), pady=4, sticky="e")
        self.var_y = tk.StringVar(value=str(initial_y))
        ttk.Entry(self, textvariable=self.var_y, width=12).grid(
            row=2, column=1, padx=(4, 10), pady=4, sticky="w")

        # OK / Cancel button row.
        btn_row = ttk.Frame(self)  # Container for buttons
        btn_row.grid(row=3, column=0, columnspan=2, pady=(8, 10))
        ttk.Button(btn_row, text="OK", width=10, command=self._ok).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Cancel", width=10, command=self._cancel).pack(side="left", padx=4)

        # Center on parent.
        self.update_idletasks()  # Compute dimensions
        px = parent.winfo_rootx() + parent.winfo_width() // 2 - self.winfo_width() // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2 - self.winfo_height() // 2
        self.geometry(f"+{max(0, px)}+{max(0, py)}")  # Position
        self.bind("<Return>", lambda _e: self._ok())  # Enter accepts
        self.bind("<Escape>", lambda _e: self._cancel())  # Esc cancels
        self.wait_window()  # Block until destroyed

    def _ok(self) -> None:
        """Validate both fields then store result and close."""
        try:
            x = float(self.var_x.get())  # Parse X
            y = float(self.var_y.get())  # Parse Y
        except ValueError:
            messagebox.showerror("Invalid input", "X and Y must be numbers.", parent=self)
            return  # Stay open
        self.result = (x, y)  # Save
        self.destroy()  # Close window

    def _cancel(self) -> None:
        """Close without saving."""
        self.result = None  # Sentinel for cancel
        self.destroy()  # Close window


# ---------------------------------------------------------------------------
# Main GUI application
# ---------------------------------------------------------------------------
class PATClickerGUI:
    """
    Tk-based front-end for the Post Apo Tycoon clicker.

    Layout:
      * Top: status bar (ADB connection, screen size, current calibration).
      * Left: action buttons (start clicker, hold-to-spam, calibrations,
              points list, polygon, options).
      * Center: live offset pad (arrow buttons +/-1, +/-10).
      * Right: scrolling log panel.
      * Bottom: quit + reset buttons.
    """

    # Window geometry constants -- kept here so the layout is easy to tweak.
    # The main window is intentionally narrow now that the log panel is gone.
    WINDOW_TITLE = "Post Apo Tycoon Clicker"
    WINDOW_W = 380
    WINDOW_H = 560

    def __init__(self) -> None:
        # Backend: one ADB controller shared across all features.
        self.adb = ADBController()  # Same class used by the CLI version
        # Log queue: any thread writes here; the GUI main loop drains it.
        self.log_q: "queue.Queue[str]" = queue.Queue()
        # Background workers (start as needed, kept here for clean shutdown).
        self.active_threads: List[threading.Thread] = []
        # Coordinated stop flag set when the window is closing.
        self.shutting_down = threading.Event()
        # Live offset (added to every ADB tap from option 1 / spam).
        self.live_offset_x = 0.0  # Current X delta -- editable at runtime
        self.live_offset_y = 0.0  # Current Y delta -- editable at runtime
        # State of the various background subsystems.
        self.main_clicker_running = False  # Function 1 spam state
        self.spam_running = False  # Function 2 spam state
        self._main_state = {"running": False, "stop": threading.Event(),
                            "worker": None, "thread": None}  # fn1 plumbing
        self._spam_state = {"running": False, "stop": threading.Event(),
                            "worker": None, "thread": None,
                            "mapping": None}  # fn2 plumbing
        # Build window only AFTER state is initialised so callbacks won't NPE.
        self.root = tk.Tk()  # Root Tk window
        self.root.title(self.WINDOW_TITLE)  # Title bar
        self.root.geometry(f"{self.WINDOW_W}x{self.WINDOW_H}")  # Size
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)  # Clean shutdown
        # macOS Tk has a habit of SILENTLY swallowing exceptions raised by
        # widget command callbacks. Without this handler "button click does
        # nothing" can hide a real error deep in our handler chain. We log
        # every exception to debug.log AND show it in a messagebox so the
        # user always sees something concrete instead of silence.

        def _report_callback_exception(exc, val, tb):
            import traceback
            tb_text = "".join(traceback.format_exception(exc, val, tb))
            try:
                self.log(f"[!] Unhandled callback exception:\n{tb_text}")
            except Exception:
                pass
            try:
                messagebox.showerror("Unhandled error", f"{val}\n\n{tb_text}")
            except Exception:
                pass

        self.root.report_callback_exception = _report_callback_exception
        self._build_ui()  # Construct widgets
        # Kick off the log drain (Tk doesn't allow other threads to touch UI).
        self.root.after(50, self._drain_log_queue)  # First poll
        # Try to connect ADB in the background so the window appears instantly.
        threading.Thread(target=self._connect_adb_bg, daemon=True).start()

    # ----- UI construction -------------------------------------------------
    def _build_ui(self) -> None:
        """
        Create all the widgets and arrange them in the window.

        Layout (compact, no Log panel in the main window):
            +---------------------------------------+
            |              status bar               |
            +---------------------------------------+
            |                                       |
            |          action buttons               |
            |          (single column)              |
            |                                       |
            +---------------------------------------+
            | last-event line                       |
            +-------------------------+-------------+
            | [Live ADB offset...]    |   [Quit]    |
            +-------------------------+-------------+

        The "Live ADB offset" button opens a separate floating window with
        the arrow-pad + reset + save controls. This keeps the main window
        small and removes visual clutter.
        """
        # Status bar at the top.
        self.var_status = tk.StringVar(value="Connecting to ADB...")  # Live status
        status_bar = ttk.Label(self.root, textvariable=self.var_status,
                               anchor="w", padding=(8, 4),
                               relief="sunken")  # Sunken => looks like a status bar
        status_bar.pack(side="top", fill="x")  # Full-width

        # The "last event" line replaces the dropped log panel: every call
        # to self.log() also writes to this StringVar so the user still
        # sees what the script is doing without a scroll-back history.
        self.var_last_event = tk.StringVar(value="")
        last_event_line = ttk.Label(self.root, textvariable=self.var_last_event,
                                    anchor="w", padding=(8, 2),
                                    foreground="gray")
        last_event_line.pack(side="bottom", fill="x", before=status_bar)
        # Reorder: keep bottom-bar pinned at the very bottom.
        # We pack the bottom-bar AFTER actions so it stays beneath them.

        # --- Action column (only column now; no log panel) ----------------
        actions = ttk.LabelFrame(self.root, text="Actions", padding=8)
        actions.pack(side="top", fill="both", expand=True, padx=8, pady=(4, 4))

        # Run-time toggles.
        self.btn_run_clicker = ttk.Button(  # Toggles main clicker on/off
            actions, text="Start Main Clicker (fn 1)",
            command=self.toggle_main_clicker, width=34)
        self.btn_run_clicker.pack(fill="x", pady=2)
        self.btn_run_spam = ttk.Button(  # Toggles hold-to-spam mode
            actions, text="Enter Hold-to-Spam (fn 2)",
            command=self.toggle_hold_to_spam, width=34)
        self.btn_run_spam.pack(fill="x", pady=2)
        ttk.Separator(actions, orient="horizontal").pack(fill="x", pady=6)

        # Unified calibration (v2): the only calibration button. Runs the
        # 4-iteration overlay+OCR procedure that produces a single
        # game<->ADB affine, shared by fn 1 and fn 2.
        ttk.Label(actions, text="Calibration:",
                  font=("TkDefaultFont", 9, "bold")).pack(anchor="w", pady=(4, 2))
        ttk.Button(actions, text="Calibrate (overlay + OCR, 4 taps)",
                   command=self.calibrate_unified_v2, width=34).pack(fill="x", pady=1)

        ttk.Separator(actions, orient="horizontal").pack(fill="x", pady=6)
        ttk.Label(actions, text="Points / clicker:",
                  font=("TkDefaultFont", 9, "bold")).pack(anchor="w", pady=(4, 2))
        ttk.Button(actions, text="Edit points list (fn 3)",
                   command=self.points_menu_gui, width=34).pack(fill="x", pady=1)
        ttk.Button(actions, text="Polygon clicks (fn 4)",
                   command=self.polygon_clicks_gui, width=34).pack(fill="x", pady=1)
        ttk.Button(actions, text="Zoom calibration (fn 5)",
                   command=self.calibrate_zoom_gui, width=34).pack(fill="x", pady=1)
        ttk.Button(actions, text="Clicker options (fn 6)",
                   command=self.clicker_options_gui, width=34).pack(fill="x", pady=1)

        # --- Bottom bar: live offset window + quit ------------------------
        bottom = ttk.Frame(self.root, padding=(8, 6))
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Live ADB offset...",
                   command=self.open_offset_window, width=20).pack(side="left", padx=2)
        ttk.Button(bottom, text="Open script folder",
                   command=self._open_log_folder, width=18).pack(side="left", padx=2)
        ttk.Button(bottom, text="Quit", command=self._on_close,
                   width=10).pack(side="right", padx=2)

        # Bind arrow keys at the root so the user can nudge offset by keyboard
        # whenever the main window (or the offset window) has focus.
        self.root.bind("<Left>", lambda _e: self._adjust_offset(-1, 0))
        self.root.bind("<Right>", lambda _e: self._adjust_offset(1, 0))
        self.root.bind("<Up>", lambda _e: self._adjust_offset(0, -1))
        self.root.bind("<Down>", lambda _e: self._adjust_offset(0, 1))
        self.root.bind("<Shift-Left>", lambda _e: self._adjust_offset(-10, 0))
        self.root.bind("<Shift-Right>", lambda _e: self._adjust_offset(10, 0))
        self.root.bind("<Shift-Up>", lambda _e: self._adjust_offset(0, -10))
        self.root.bind("<Shift-Down>", lambda _e: self._adjust_offset(0, 10))

        # Defaults for objects that the rest of the class still references.
        self.log_text = None  # No log panel; log() now updates var_last_event
        self.offset_window: Optional[tk.Toplevel] = None  # Lazy-created on demand
        self.var_offset_display = tk.StringVar(value="dx = 0  dy = 0")

    # ----- logging plumbing -----------------------------------------------
    # The on-screen log panel has been removed. log() now:
    #   - persists every message to debug.log (so we still have a full trail
    #     for diagnostics)
    #   - replaces the "last event" line at the bottom of the main window
    # Thread-safe: log() can be called from any thread; the Tk update is
    # marshalled via the existing log queue + after() poll.

    def log(self, msg: str) -> None:
        """Thread-safe log: any thread may call this."""
        ts = time.strftime("%H:%M:%S")  # Timestamp prefix
        line = f"[{ts}] {msg}"
        # Persist to file unconditionally -- debug.log is the new source of
        # truth when something goes wrong.
        try:
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "debug.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass  # Best-effort; never let logging break a click loop
        # Push to queue so Tk main loop can update the on-screen label.
        self.log_q.put(line)

    def _drain_log_queue(self) -> None:
        """Called every ~50ms by Tk main loop: drain queue into the status line."""
        latest: Optional[str] = None
        try:
            while True:
                latest = self.log_q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            # Only show the most recent event -- the full history lives in
            # debug.log on disk.
            self.var_last_event.set(latest)
        if not self.shutting_down.is_set():
            self.root.after(80, self._drain_log_queue)  # Re-arm timer

    def _open_log_folder(self) -> None:
        """Open the script's directory in the OS file manager."""
        folder = os.path.dirname(os.path.abspath(__file__))  # Script dir
        if sys.platform == "darwin":
            os.system(f"open '{folder}'")  # macOS Finder
        elif sys.platform.startswith("win"):
            os.system(f'explorer "{folder}"')  # Windows Explorer
        else:
            os.system(f"xdg-open '{folder}'")  # Linux GUI

    # ----- background ADB connect -----------------------------------------
    def _connect_adb_bg(self) -> None:
        """Connect to BlueStacks in the background thread."""
        try:
            self.adb.connect()  # Discover + connect + start persistent shell
            w, h = self.adb.screen_size or (0, 0)
            self.var_status.set(  # Updated from Tk main loop via after()
                f"ADB connected: {self.adb.serial}  "
                f"emulator {w}x{h}")
            self.log(f"ADB connected: {self.adb.serial} ({w}x{h})")
        except Exception as e:
            self.var_status.set(f"ADB connect failed: {e}")
            self.log(f"[!] ADB connect failed: {e}")

    # ----- live offset handling -------------------------------------------
    def _refresh_offset_display(self) -> None:
        """Refresh the label that shows current (dx, dy)."""
        self.var_offset_display.set(
            f"dx = {int(self.live_offset_x)}  dy = {int(self.live_offset_y)}")

    def _adjust_offset(self, ddx: int, ddy: int) -> None:
        """Bump the live offset by (ddx, ddy) -- effective immediately."""
        self.live_offset_x += ddx
        self.live_offset_y += ddy
        self._refresh_offset_display()
        # Persist so subsequent runs pick it up. This also makes the
        # adjustment usable inside the running click loop because the
        # producer reads the offset from this object on every tap.
        self.log(f"Offset = ({self.live_offset_x:+.0f}, {self.live_offset_y:+.0f})")

    def _reset_offset(self) -> None:
        """Reset offset to (0, 0)."""
        self.live_offset_x = 0.0
        self.live_offset_y = 0.0
        self._refresh_offset_display()
        self.log("Offset reset to (0, 0)")

    def _save_offset_to_options(self) -> None:
        """Write the current live offset into clicker_option.json."""
        cur = load_json(CLICKER_OPT_FILE) or {}  # Read existing
        cur["offset_x"] = self.live_offset_x  # Update X
        cur["offset_y"] = self.live_offset_y  # Update Y
        save_json(CLICKER_OPT_FILE, cur)  # Persist
        self.log(f"Saved offset ({self.live_offset_x:+.0f}, "
                 f"{self.live_offset_y:+.0f}) to {CLICKER_OPT_FILE}")

    def open_offset_window(self) -> None:
        """
        Open (or focus) the floating Live ADB offset window.

        The window holds the arrow-pad, big-step buttons, reset, and save.
        It stays on top of the main window and can remain open while the
        clicker is running so the user can nudge the offset live.
        """
        # If a window already exists and is still alive, just raise it.
        if self.offset_window is not None and self.offset_window.winfo_exists():
            self.offset_window.lift()
            self.offset_window.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.title("Live ADB offset")
        win.resizable(False, False)
        win.transient(self.root)  # Stick to main window in OS chrome
        win.attributes("-topmost", True)  # Keep above BlueStacks while nudging
        # Display of current (dx, dy) at the top of the window.
        ttk.Label(win, textvariable=self.var_offset_display,
                  font=("TkDefaultFont", 11, "bold")).grid(
            row=0, column=0, columnspan=3, pady=(8, 6))
        # 3x3 arrow pad: ±1 px steps.
        ttk.Button(win, text="\u2191", width=4,
                   command=lambda: self._adjust_offset(0, -1)).grid(
            row=1, column=1, pady=2)
        ttk.Button(win, text="\u2190", width=4,
                   command=lambda: self._adjust_offset(-1, 0)).grid(
            row=2, column=0, padx=2)
        ttk.Button(win, text="\u2193", width=4,
                   command=lambda: self._adjust_offset(0, 1)).grid(
            row=2, column=1, pady=2)
        ttk.Button(win, text="\u2192", width=4,
                   command=lambda: self._adjust_offset(1, 0)).grid(
            row=2, column=2, padx=2)
        # Big-step (±10 px) row.
        ttk.Label(win, text="\u00b110 px:").grid(
            row=3, column=0, columnspan=3, pady=(10, 2))
        big = ttk.Frame(win)
        big.grid(row=4, column=0, columnspan=3)
        ttk.Button(big, text="\u2190\u219010", width=6,
                   command=lambda: self._adjust_offset(-10, 0)).pack(side="left", padx=1)
        ttk.Button(big, text="\u219110", width=5,
                   command=lambda: self._adjust_offset(0, -10)).pack(side="left", padx=1)
        ttk.Button(big, text="\u219310", width=5,
                   command=lambda: self._adjust_offset(0, 10)).pack(side="left", padx=1)
        ttk.Button(big, text="\u2192\u219210", width=6,
                   command=lambda: self._adjust_offset(10, 0)).pack(side="left", padx=1)
        # Reset and save buttons.
        ttk.Button(win, text="Reset offset",
                   command=self._reset_offset, width=22).grid(
            row=5, column=0, columnspan=3, pady=(10, 2))
        ttk.Button(win, text="Save offset to file",
                   command=self._save_offset_to_options, width=22).grid(
            row=6, column=0, columnspan=3, pady=2)
        ttk.Label(win,
                  text="Arrow keys (also Shift+arrow for \u00b110)\n"
                       "work while this window is focused.",
                  foreground="gray", justify="center").grid(
            row=7, column=0, columnspan=3, pady=(8, 8), padx=8)
        # Re-bind arrow keys on the Toplevel so the user doesn't have to
        # switch focus back to the main window.
        win.bind("<Left>", lambda _e: self._adjust_offset(-1, 0))
        win.bind("<Right>", lambda _e: self._adjust_offset(1, 0))
        win.bind("<Up>", lambda _e: self._adjust_offset(0, -1))
        win.bind("<Down>", lambda _e: self._adjust_offset(0, 1))
        win.bind("<Shift-Left>", lambda _e: self._adjust_offset(-10, 0))
        win.bind("<Shift-Right>", lambda _e: self._adjust_offset(10, 0))
        win.bind("<Shift-Up>", lambda _e: self._adjust_offset(0, -10))
        win.bind("<Shift-Down>", lambda _e: self._adjust_offset(0, 10))
        # Position next to the main window.
        self.root.update_idletasks()
        px = self.root.winfo_rootx() + self.root.winfo_width() + 10
        py = self.root.winfo_rooty()
        win.geometry(f"+{px}+{py}")
        self.offset_window = win

    # ----- generic guards --------------------------------------------------
    def _require_adb(self) -> bool:
        """Show an error if ADB isn't ready, return True iff ready."""
        if self.adb.serial is None:
            messagebox.showerror("Not connected",
                                 "ADB is not connected yet. Wait a moment and try again.")
            return False
        return True

    def _ensure_screen_size(self) -> Optional[Tuple[int, int]]:
        """Return (w, h) of the emulator, querying lazily."""
        if not self._require_adb():
            return None
        if not self.adb.screen_size:
            self.adb.screen_size = self.adb._query_screen_size()
        return self.adb.screen_size

    # ----- Five window-calibration strategies (delegated) -----------------
    # All five run the heavy work in a background thread so the GUI does
    # not freeze; status updates flow through self.log() (thread-safe).

    def _run_calibration_async(self, label: str,
                               fn: Callable[[Callable[[str], None]],
                                            Optional[ScreenToAdb]]
                               ) -> None:
        """Run a calibration function in a worker thread."""
        if not self._require_adb():
            return

        def worker() -> None:
            try:
                self.log(f"=== {label} ===")
                result = fn(self.log)  # `self.log` is the status callback
                if result is None:
                    self.log(f"[!] {label} failed.")
                else:
                    self.log(f"[+] {label} succeeded.")
            except Exception as e:
                self.log(f"[!] {label} crashed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def calibrate_A_ncc(self) -> None:
        """Variant A -- screencap NCC auto-align."""
        self._run_calibration_async(
            "Auto (screencap match)",
            lambda log: calib_mod.screencap_ncc_calibration(self.adb, log),
        )

    def calibrate_B_ripple(self) -> None:
        """Variant B -- 9-tap ripple detection."""
        if not messagebox.askokcancel(
                "Auto ripple calibration",
                "The script will send 9 ADB taps and screenshot the screen\n"
                "before/after each tap to locate the ripple effect.\n\n"
                "Make sure BlueStacks is visible and NOT covered by any\n"
                "other window. Total time: ~10 seconds.\n\n"
                "On macOS, the Terminal/Python app needs Screen Recording\n"
                "permission to take screenshots."):
            return
        self._run_calibration_async(
            "Auto (9-tap ripple)",
            lambda log: calib_mod.ripple_diff_calibration(self.adb, log),
        )

    def calibrate_C_config(self) -> None:
        """Variant C -- read BlueStacks config + OS detect + aspect-fix."""
        self._run_calibration_async(
            "Auto (config-aware)",
            lambda log: calib_mod.bluestacks_config_calibration(self.adb, log),
        )

    def calibrate_D_one_point(self) -> None:
        """Variant D -- one ADB tap + user hotkey to capture cursor pos."""
        if not self._require_adb():
            return
        # Build the wait_for_hotkey callback for the calibration module.
        # Wait for the user to flip Caps Lock state -- we POLL the LED via
        # pat_input rather than installing a pynput keyboard.Listener, so
        # this function is safe to call from any thread on macOS too.
        def wait_for_hotkey(timeout: float) -> bool:
            self.log("Hover over the ripple and press CAPS LOCK to capture.")
            initial = pat_input.is_capslock_on()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if pat_input.is_capslock_on() != initial:
                    return True  # Caps Lock toggled => user pressed it
                time.sleep(0.04)  # 25 polls/sec, lightweight
            return False  # Timed out

        def worker():
            try:
                self.log("=== Quick (1-tap + hotkey) ===")
                result = calib_mod.one_point_calibration(
                    self.adb, self.log, wait_for_hotkey)
                if result is None:
                    self.log("[!] Quick calibration failed.")
                else:
                    self.log("[+] Quick calibration succeeded.")
            except Exception as e:
                self.log(f"[!] Quick calibration crashed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def calibrate_E_manual(self) -> None:
        """Variant E -- legacy hover + dialog twice."""
        if not self._require_adb():
            return

        def capture_point(prompt: str) -> Optional[Tuple[float, float]]:
            """Show a messagebox, then read cursor when user clicks OK."""
            messagebox.showinfo(
                "Manual calibration",
                f"Move the cursor to: {prompt}\n\nClick OK to record position.")
            return _read_cursor_logical()

        def worker():
            try:
                self.log("=== Manual (hover + Enter \u00d72) ===")
                result = calib_mod.manual_two_point_calibration(
                    self.adb, capture_point)
                if result is None:
                    self.log("[!] Manual calibration failed.")
                else:
                    self.log("[+] Manual calibration succeeded.")
            except Exception as e:
                self.log(f"[!] Manual calibration crashed: {e}")

        # Manual flow uses dialogs -- run on the main thread.
        worker()

    # ----- fn 1 affine calibration (replaces all the ripple variants) -----
    # Two anchor (game, ADB) pairs solve the affine kx, ky, bx, by exactly.
    # The two methods below differ ONLY in how the ADB part of each pair
    # is captured -- the math is identical.
    #
    #   Method A (cursor): user enters game X/Y; aims cursor; we read screen
    #     coords via the same path fn 2 uses, then translate to ADB through
    #     the fn 2 window calibration (which the user has confirmed works).
    #     ZERO screenshots, ZERO ripple detection.
    #
    #   Method B (manual): user enters BOTH (game X, game Y) and (ADB X,
    #     ADB Y) numbers directly. For users who already know their ADB
    #     pixel anchors (e.g. via BlueStacks devtools) or who don't trust
    #     the fn 2 mapping for any reason.

    def _solve_affine_and_save(self,
                               g1: Tuple[float, float], a1: Tuple[float, float],
                               g2: Tuple[float, float], a2: Tuple[float, float]
                               ) -> bool:
        """Solve the affine and persist to GAME_AFFINE_CALIB_FILE."""
        try:
            aff = GameToAdbAffine.from_two_pairs(g1, a1, g2, a2)
        except ValueError as e:
            messagebox.showerror("Calibration failed", str(e))
            return False
        save_game_affine(aff, extras={
            "anchor1_game": list(g1), "anchor1_adb": list(a1),
            "anchor2_game": list(g2), "anchor2_adb": list(a2),
        })
        self.log(f"Affine saved: kx={aff.kx:.4f} ky={aff.ky:.4f} "
                 f"bx={aff.bx:.1f} by={aff.by:.1f}")
        messagebox.showinfo(
            "Calibration saved",
            f"game->ADB affine:\n"
            f"  kx = {aff.kx:.4f}    bx = {aff.bx:.1f}\n"
            f"  ky = {aff.ky:.4f}    by = {aff.by:.1f}\n\n"
            "Saved to game_affine_calibration.json.\n"
            "fn 1 will use this immediately.")
        return True

    # ----- fn 1 setup: define ROI and pick anchors (one-time) -------------
    # These two methods feed the OCR auto-calibration below. They store
    # the ROI of the in-game coord-readout field and the ADB pixel
    # coordinates of two anchor objects on the map. Both files are
    # consumed by fn1_recalibrate_auto and never touched by fn 2.

    def fn1_setup_roi(self) -> None:
        """Capture (x, y, w, h) ADB pixels of the in-game coord field."""
        if not self._require_adb():
            return
        size = self._ensure_screen_size()
        if size is None:
            return
        adb_w, adb_h = size
        existing = load_json(GAME_UI_ROI_FILE) or {}
        ix = int(existing.get("x", adb_w * 0.7))
        iy = int(existing.get("y", adb_h * 0.05))
        iw = int(existing.get("w", 240))
        ih = int(existing.get("h", 60))
        messagebox.showinfo(
            "fn 1 setup: coord ROI",
            "Enter the bounding box (in EMULATOR ADB PIXELS) of the\n"
            "in-game field that shows the selected object's coordinates,\n"
            "e.g. 'x:-106 y:158'.\n\n"
            "Tip: select any object in the game first so the field is\n"
            "visible, then read off its ADB pixel position from BlueStacks\n"
            "or estimate from screen pixels times emulator/window scale.")
        d1 = XYDialog(self.root, "Coord ROI: top-left (ADB px)",
                      "X,Y of TOP-LEFT corner of the field:",
                      initial_x=ix, initial_y=iy)
        if d1.result is None:
            return
        d2 = XYDialog(self.root, "Coord ROI: width / height (ADB px)",
                      "WIDTH and HEIGHT of the field (pixels):",
                      initial_x=iw, initial_y=ih)
        if d2.result is None:
            return
        roi = {
            "x": int(d1.result[0]),
            "y": int(d1.result[1]),
            "w": int(d2.result[0]),
            "h": int(d2.result[1]),
        }
        save_json(GAME_UI_ROI_FILE, roi)
        self.log(f"Saved coord ROI: x={roi['x']} y={roi['y']} "
                 f"w={roi['w']} h={roi['h']}")
        messagebox.showinfo("Saved", f"Coord ROI saved.\n{roi}")

    def fn1_setup_anchors(self) -> None:
        """Capture ADB pixels of two clickable game anchors."""
        if not self._require_adb():
            return
        existing = load_json(GAME_ANCHORS_FILE) or {}
        a1 = existing.get("anchor1_adb", [200, 200])
        a2 = existing.get("anchor2_adb", [800, 800])
        messagebox.showinfo(
            "fn 1 setup: pick 2 anchors",
            "Pick TWO clickable game objects (bases / resources) at\n"
            "DIAGONALLY opposite positions on the map.\n\n"
            "For each, enter the ADB pixel coordinates where an ADB tap\n"
            "would land directly on the object.\n\n"
            "Tip: the further apart the anchors, the more accurate the\n"
            "resulting calibration. Avoid horizontal/vertical alignment.")
        d1 = XYDialog(self.root, "Anchor 1 (ADB px)",
                      "ADB X / Y of FIRST clickable object:",
                      initial_x=a1[0], initial_y=a1[1])
        if d1.result is None:
            return
        d2 = XYDialog(self.root, "Anchor 2 (ADB px)",
                      "ADB X / Y of SECOND clickable object:",
                      initial_x=a2[0], initial_y=a2[1])
        if d2.result is None:
            return
        data = {
            "anchor1_adb": [int(d1.result[0]), int(d1.result[1])],
            "anchor2_adb": [int(d2.result[0]), int(d2.result[1])],
        }
        save_json(GAME_ANCHORS_FILE, data)
        self.log(f"Saved anchors: {data}")
        messagebox.showinfo("Saved", f"Anchors saved.\n{data}")

    def calibrate_unified_v2(self) -> None:
        """Unified v2 calibration: 4 ADB taps, 4 user-drawn rectangles, OCR.

        Produces a single affine game<->ADB mapping shared by fn 1 and
        fn 2. The math runs in a worker thread; the overlay (which has
        to live on the main thread for Tk) is invoked by it via Tk's
        thread-safe `after()` queue.
        """
        if not self._require_adb():
            return
        if not messagebox.askokcancel(
                "Unified calibration",
                "This will run 8 iterations for high precision. For each:\n\n"
                "  1. Script taps a point in the emulator.\n"
                "  2. Wait ~1 second for the game UI to refresh.\n"
                "  3. A transparent overlay appears OVER BlueStacks.\n"
                "  4. Drag a rectangle around the 'x:N y:N' text.\n"
                "  5. ESC to cancel at any time.\n\n"
                "Before that you outline the visible playable map ONCE\n"
                "after the intro swipe. Total: ~60 seconds.\n\n"
                "Make sure BlueStacks is visible and Post Apo Tycoon is\n"
                "the foreground app inside it. Ready?"):
            return

        # The calibration uses tk Toplevel overlays -- those MUST live on
        # the main thread. We therefore run the procedure inline on the
        # main thread; the user sees the modal "Working..." prompts
        # naturally as overlays appear.
        debug_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            result = calib_v2.run_unified_calibration(
                self.root, self.adb, self.log,
                n_iterations=8,
                debug_dir=debug_dir,
            )
        except Exception as e:
            self.log(f"[!] Calibration crashed: {e}")
            messagebox.showerror("Calibration crashed", str(e))
            return
        if result is None:
            messagebox.showinfo("Calibration not saved",
                                "Calibration was cancelled or failed.\n"
                                "See debug.log for details.")
            return
        rx = result.get("residual_x", 0.0)
        ry = result.get("residual_y", 0.0)
        used_x = result.get("used_pairs_x", "?")
        used_y = result.get("used_pairs_y", "?")
        total = result.get("total_pairs", "?")
        messagebox.showinfo(
            "Calibration saved",
            f"kx = {result['kx']:.4f}    bx = {result['bx']:.1f}\n"
            f"ky = {result['ky']:.4f}    by = {result['by']:.1f}\n\n"
            f"Max residual: X = {rx:.2f}px   Y = {ry:.2f}px\n"
            f"Pairs used: X = {used_x}/{total}, Y = {used_y}/{total}\n\n"
            f"Saved to game_affine_calibration.json.\n"
            f"fn 1 will use this immediately.")

    def fn1_recalibrate_auto(self) -> None:
        """Tap each anchor + OCR coord field + solve affine. No user input."""
        if not self._require_adb():
            return
        roi_data = load_json(GAME_UI_ROI_FILE)
        anchors = load_json(GAME_ANCHORS_FILE)
        if not roi_data:
            messagebox.showerror("Missing setup",
                                 "Define the coord ROI first (one-time setup).")
            return
        if not anchors:
            messagebox.showerror("Missing setup",
                                 "Pick the two anchors first (one-time setup).")
            return
        roi = (int(roi_data["x"]), int(roi_data["y"]),
               int(roi_data["w"]), int(roi_data["h"]))
        a1 = tuple(anchors["anchor1_adb"])
        a2 = tuple(anchors["anchor2_adb"])
        debug_dir = os.path.dirname(os.path.abspath(__file__))

        def worker(log_cb):
            result = calib_mod.ocr_auto_calibrate_fn1(
                self.adb, log_cb,
                roi=roi, anchor1_adb=a1, anchor2_adb=a2,
                debug_dir=debug_dir,
            )
            if result is None:
                return None
            # Persist directly in the affine file format used by fn 1.
            save_json(GAME_AFFINE_CALIB_FILE, result)
            log_cb("Saved affine to " + GAME_AFFINE_CALIB_FILE)
            return result

        self._run_calibration_async("fn1 auto OCR recalibrate", worker)

    def calibrate_fn1_affine_manual(self) -> None:
        """Method B: user enters both game X/Y and ADB X/Y for two anchors."""
        if not self._require_adb():
            return
        if not messagebox.askokcancel(
                "fn 1 calibration (manual method)",
                "Two anchor points are needed. For each anchor enter BOTH\n"
                "the in-game X/Y and the ADB pixel X/Y.\n\n"
                "Pick two diagonal anchors that span as much of the map\n"
                "as possible -- the further apart, the more accurate.\n\n"
                "Ready?"):
            return
        g1 = XYDialog(self.root, "Anchor 1: GAME coords",
                      "First anchor -- in-game X/Y:")
        if g1.result is None:
            return
        a1 = XYDialog(self.root, "Anchor 1: ADB coords",
                      "First anchor -- ADB pixel X/Y:")
        if a1.result is None:
            return
        g2 = XYDialog(self.root, "Anchor 2: GAME coords",
                      "Second anchor -- in-game X/Y:")
        if g2.result is None:
            return
        a2 = XYDialog(self.root, "Anchor 2: ADB coords",
                      "Second anchor -- ADB pixel X/Y:")
        if a2.result is None:
            return
        self._solve_affine_and_save(g1.result, a1.result, g2.result, a2.result)

    # ----- Function 7: 4-point map calibration ----------------------------
    def calibrate_map_gui(self) -> None:
        """Capture 4 (game, ADB) pairs through XYDialog modals."""
        if not self._require_adb():
            return
        labels = ["top-left", "top-right", "bottom-right", "bottom-left"]
        game_pts: List[Tuple[float, float]] = []
        adb_pts: List[Tuple[float, float]] = []
        for label in labels:
            g = XYDialog(self.root, f"Point #{len(game_pts) + 1}: {label}",
                         f"Enter GAME X/Y for {label} corner:")
            if g.result is None:
                self.log("Map calibration cancelled.")
                return
            game_pts.append(g.result)
            a = XYDialog(self.root, f"Point #{len(adb_pts) + 1}: {label}",
                         f"Enter ADB X/Y for {label} corner:")
            if a.result is None:
                self.log("Map calibration cancelled.")
                return
            adb_pts.append(a.result)
        save_json(CALIBRATION_FILE, {
            "game_points": game_pts,
            "adb_points": adb_pts,
            "type": "bilinear-4pt",
        })
        self.log(f"Map calibration saved with {len(game_pts)} anchors.")
        messagebox.showinfo("Saved", f"Saved {CALIBRATION_FILE}")

    # ----- Function 3: points CRUD ----------------------------------------
    def points_menu_gui(self) -> None:
        """Open a window to add / edit / delete points."""
        win = tk.Toplevel(self.root)
        win.title("Points list (fn 3)")
        win.transient(self.root)
        win.geometry("420x360")
        # Listbox showing the current points.
        listbox = tk.Listbox(win, font=("Menlo", 10))
        listbox.pack(side="top", fill="both", expand=True, padx=8, pady=8)

        def refresh() -> None:
            listbox.delete(0, "end")  # Clear
            data = load_json(POINTS_FILE) or {"points": []}
            for i, p in enumerate(data.get("points", []), start=1):
                listbox.insert("end",
                               f"{i}. {p.get('name', f'point{i}')}  "
                               f"({p['x']:.1f}, {p['y']:.1f})")

        def add() -> None:
            d = XYDialog(win, "Add point", "Enter GAME X/Y:")
            if d.result is None:
                return
            name = simpledialog.askstring("Name", "Optional name:", parent=win) or None
            data = load_json(POINTS_FILE) or {"points": []}
            pts = data.get("points", [])
            pts.append({
                "name": name or f"point{len(pts) + 1}",
                "x": d.result[0], "y": d.result[1],
            })
            save_json(POINTS_FILE, {"points": pts})
            refresh()
            self.log(f"Added point {pts[-1]['name']}")

        def edit() -> None:
            sel = listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            data = load_json(POINTS_FILE) or {"points": []}
            pts = data.get("points", [])
            p = pts[idx]
            d = XYDialog(win, f"Edit {p.get('name', '')}",
                         "Enter NEW GAME X/Y:",
                         initial_x=p["x"], initial_y=p["y"])
            if d.result is None:
                return
            p["x"], p["y"] = d.result
            save_json(POINTS_FILE, {"points": pts})
            refresh()
            self.log(f"Edited point #{idx + 1}")

        def delete() -> None:
            sel = listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            data = load_json(POINTS_FILE) or {"points": []}
            pts = data.get("points", [])
            removed = pts.pop(idx)
            save_json(POINTS_FILE, {"points": pts})
            refresh()
            self.log(f"Deleted point: {removed.get('name', '<unnamed>')}")

        # Button row.
        row = ttk.Frame(win)
        row.pack(side="bottom", fill="x", pady=4)
        ttk.Button(row, text="Add", command=add, width=10).pack(side="left", padx=4)
        ttk.Button(row, text="Edit", command=edit, width=10).pack(side="left", padx=4)
        ttk.Button(row, text="Delete", command=delete, width=10).pack(side="left", padx=4)
        ttk.Button(row, text="Close", command=win.destroy,
                   width=10).pack(side="right", padx=4)
        refresh()  # Initial populate

    # ----- Function 4: polygon clicks -------------------------------------
    def polygon_clicks_gui(self) -> None:
        """Collect polygon vertices via XYDialogs, then spam clicks inside."""
        calib = load_calibration()
        if calib is None:
            messagebox.showerror("No calibration",
                                 "Run the 4-point map calibration (fn 7) first.")
            return
        verts: List[Tuple[float, float]] = []
        while True:
            d = XYDialog(self.root,
                         f"Polygon vertex #{len(verts) + 1}",
                         "Enter GAME X/Y. Cancel to finish.")
            if d.result is None:
                break
            verts.append(d.result)
        if len(verts) < 3:
            messagebox.showerror("Not enough", "Need at least 3 vertices.")
            return
        cps = ask_number(self.root, "CPS", "Clicks per second:",
                         initial=10.0, minvalue=0.1)
        if cps is None:
            return
        # Transform vertices into ADB space using the saved 4-point calibration.
        adb_verts = [calib.game_to_adb(*v) for v in verts]
        sampler = PolygonSampler(adb_verts)
        interval = 1.0 / cps
        worker = ClickWorker(self.adb)
        state = {"running": False}
        stop_flag = threading.Event()

        def producer():
            next_t = time.monotonic()
            while not stop_flag.is_set():
                if state["running"]:
                    x, y = sampler.sample()
                    # Apply live offset so the user can nudge during the spam.
                    self.adb.tap(x + self.live_offset_x, y + self.live_offset_y)
                next_t += interval
                d = next_t - time.monotonic()
                if d > 0:
                    time.sleep(d)
                else:
                    next_t = time.monotonic()

        # Middle-mouse polling watcher (no pynput.Listener, see toggle_hold_to_spam).
        def watcher():
            prev = False
            while not stop_flag.is_set():
                try:
                    now = pat_input.is_middle_mouse_button_down()
                except Exception:
                    now = False
                if now and not prev:
                    state["running"] = not state["running"]
                    if state["running"]:
                        worker.start()
                        self.log("Polygon spam: ON")
                    else:
                        worker.stop(kill_shell=False)
                        self.log("Polygon spam: OFF")
                prev = now
                time.sleep(0.04)

        watcher_thread = threading.Thread(target=watcher, daemon=True)
        watcher_thread.start()
        prod = threading.Thread(target=producer, daemon=True)
        prod.start()
        # Show a control dialog with a Stop button so the user can leave.
        ctrl = tk.Toplevel(self.root)
        ctrl.title("Polygon clicks running")
        ctrl.transient(self.root)
        ttk.Label(ctrl, text=("Press the MIDDLE mouse button to toggle\n"
                              "polygon spam on/off.\n\n"
                              "Close this window to stop."),
                  padding=12).pack()

        def on_close():
            stop_flag.set()
            worker.stop(kill_shell=True)
            ctrl.destroy()
            self.log("Polygon clicks stopped.")

        ctrl.protocol("WM_DELETE_WINDOW", on_close)
        ttk.Button(ctrl, text="Stop", command=on_close,
                   width=12).pack(pady=8)

    # ----- Function 5: zoom calibration (ADB only) -------------------------
    def calibrate_zoom_gui(self) -> None:
        """Three modal X/Y dialogs for Settings, Zoom Out, and Swipe."""
        if not self._require_adb():
            return
        s = XYDialog(self.root, "Settings button",
                     "Enter ADB X/Y of the Settings button:")
        if s.result is None:
            return
        z = XYDialog(self.root, "Zoom Out button",
                     "Enter ADB X/Y of the Zoom Out button:")
        if z.result is None:
            return
        s1 = XYDialog(self.root, "Swipe start",
                      "Enter ADB X/Y of swipe START:")
        if s1.result is None:
            return
        s2 = XYDialog(self.root, "Swipe end",
                      "Enter ADB X/Y of swipe END:")
        if s2.result is None:
            return
        dur = ask_number(self.root, "Swipe duration",
                         "Duration in milliseconds:",
                         initial=400, minvalue=50, integer=True)
        if dur is None:
            return
        save_json(ZOOM_CALIB_FILE, {
            "settings_button": {"adb": list(s.result)},
            "zoom_out_button": {"adb": list(z.result)},
            "swipe": {"start": list(s1.result),
                      "end": list(s2.result),
                      "duration_ms": int(dur)},
        })
        self.log("Zoom calibration saved.")
        messagebox.showinfo("Saved", f"Saved {ZOOM_CALIB_FILE}")

    # ----- Function 6: clicker options ------------------------------------
    def clicker_options_gui(self) -> None:
        """Edit CPS and global ADB offsets in one combined dialog."""
        cur = load_json(CLICKER_OPT_FILE) or {}
        dlg = tk.Toplevel(self.root)
        dlg.title("Clicker options (fn 6)")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()

        # Three labelled rows: CPS, offset_x, offset_y.
        vars_ = {
            "cps": tk.DoubleVar(value=float(cur.get("cps", 10.0))),
            "offset_x": tk.DoubleVar(value=float(cur.get("offset_x", 0.0))),
            "offset_y": tk.DoubleVar(value=float(cur.get("offset_y", 0.0))),
        }
        labels_text = {
            "cps": "Clicks per second:",
            "offset_x": "Offset X (ADB px):",
            "offset_y": "Offset Y (ADB px):",
        }
        for i, key in enumerate(("cps", "offset_x", "offset_y")):
            ttk.Label(dlg, text=labels_text[key]).grid(
                row=i, column=0, padx=8, pady=4, sticky="e")
            ttk.Spinbox(dlg, from_=-9999, to=9999, increment=0.5,
                        textvariable=vars_[key], width=10).grid(
                row=i, column=1, padx=8, pady=4, sticky="w")

        def save() -> None:
            try:
                d = {k: float(v.get()) for k, v in vars_.items()}
            except tk.TclError:
                messagebox.showerror("Invalid", "Enter numeric values.", parent=dlg)
                return
            if d["cps"] <= 0:
                messagebox.showerror("Invalid", "CPS must be > 0.", parent=dlg)
                return
            save_json(CLICKER_OPT_FILE, d)
            # Also push offsets to the live offset so they apply immediately.
            self.live_offset_x = d["offset_x"]
            self.live_offset_y = d["offset_y"]
            self._refresh_offset_display()
            self.log(f"Options saved: cps={d['cps']} offset=({d['offset_x']}, {d['offset_y']})")
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=3, column=0, columnspan=2, pady=10)
        ttk.Button(btns, text="Save", command=save, width=10).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=dlg.destroy,
                   width=10).pack(side="left", padx=4)

    # ----- Function 1: main clicker toggle --------------------------------
    def toggle_main_clicker(self) -> None:
        """Start or stop the main clicker (function 1)."""
        if self.main_clicker_running:
            self._stop_main_clicker()
            return
        if not self._require_adb():
            return
        pts_data = load_json(POINTS_FILE) or {"points": []}
        pts = pts_data.get("points", [])
        if not pts:
            messagebox.showerror("Missing", "Add points (fn 3) first.")
            return
        zoom = load_json(ZOOM_CALIB_FILE)
        if not zoom:
            messagebox.showerror("Missing", "Run zoom calibration (fn 5) first.")
            return
        # Decide which game->ADB mapping to use. The new affine path is
        # the precise solution: a pure (kx, ky, bx, by) computed from two
        # well-chosen (game, ADB) pairs. If it's not been calibrated yet,
        # we fall back to the legacy 4-point bilinear from fn 7.
        affine = load_game_affine()
        bilinear = load_calibration()
        if affine is None and bilinear is None:
            messagebox.showerror(
                "Missing calibration",
                "Run the fn 1 Calibrate game->ADB wizard first.\n"
                "(Or, as a legacy alternative, fn 7 'Map calibration'.)")
            return
        opt = load_json(CLICKER_OPT_FILE) or {}
        cps = float(opt.get("cps", 10.0))
        interval = 1.0 / cps
        self.live_offset_x = float(opt.get("offset_x", 0.0))
        self.live_offset_y = float(opt.get("offset_y", 0.0))
        self._refresh_offset_display()
        # Decide game->ADB mapping. The fn 7 bilinear is kept ONLY as a
        # legacy fallback for users who haven't run unified calibration.
        if affine is not None:
            self.log(f"Using AFFINE game->ADB: kx={affine.kx:.4f} "
                     f"ky={affine.ky:.4f}  bx={affine.bx:.1f} by={affine.by:.1f}")
            # Defer the per-tap translation until AFTER the intro swipe so
            # we can realign bx, by based on the actual landed position.
            using_affine = True
        else:
            self.log("Using LEGACY 4-point bilinear (fn 7) for game->ADB")
            using_affine = False
            adb_targets = [bilinear.game_to_adb(p["x"], p["y"]) for p in pts]
        settings_adb = tuple(zoom["settings_button"]["adb"])
        zoomout_adb = tuple(zoom["zoom_out_button"]["adb"])
        swipe = zoom["swipe"]

        # Load the OCR ROI from the calibration file for per-run realignment.
        affine_data = load_json(GAME_AFFINE_CALIB_FILE) or {}
        saved_roi_raw = affine_data.get("coord_ocr_roi")
        saved_roi = (tuple(saved_roi_raw) if saved_roi_raw
                     and len(saved_roi_raw) == 4 else None)

        # Mutable container so intro/realign can update affine bx, by
        # without rebuilding the whole fn-1 setup.
        live_affine = {"kx": affine.kx if affine else 1.0,
                       "ky": affine.ky if affine else 1.0,
                       "bx": affine.bx if affine else 0.0,
                       "by": affine.by if affine else 0.0}
        # adb_targets is computed each spam-start so it picks up the
        # freshly realigned bx, by.
        adb_targets_box: Dict[str, list] = {"pts": []}

        def rebuild_targets() -> None:
            if using_affine:
                a = live_affine
                adb_targets_box["pts"] = [
                    (a["kx"] * p["x"] + a["bx"],
                     a["ky"] * p["y"] + a["by"]) for p in pts
                ]
            else:
                adb_targets_box["pts"] = [
                    bilinear.game_to_adb(p["x"], p["y"]) for p in pts
                ]

        rebuild_targets()  # Initial fill before any realignment

        worker = ClickWorker(self.adb)
        state = self._main_state
        state["worker"] = worker
        state["running"] = False
        state["stop"].clear()

        def intro():
            self.log("Main clicker intro: Settings -> ZoomOut -> swipe -> wait")
            self.adb.tap(*settings_adb)
            time.sleep(0.4)
            self.adb.tap(*zoomout_adb)
            time.sleep(0.4)
            self.adb.swipe(swipe["start"][0], swipe["start"][1],
                           swipe["end"][0], swipe["end"][1],
                           int(swipe.get("duration_ms", 400)))
            time.sleep(1.0)
            # Auto-realign bx, by using ONE probe tap+OCR. Compensates for
            # the few-pixel jitter that `adb input swipe` introduces each
            # run. Only runs when:
            #   - we have a saved OCR ROI from the unified calibration
            #   - we're using the affine mapping (not legacy bilinear)
            if using_affine and saved_roi is not None:
                self.log("Realigning bx, by with a probe tap+OCR...")
                try:
                    res = calib_v2.auto_realign_bxby(
                        self.adb,
                        kx=live_affine["kx"], ky=live_affine["ky"],
                        bx=live_affine["bx"], by=live_affine["by"],
                        saved_roi=saved_roi,
                        probe_game=(0, 0),
                        status_cb=self.log,
                    )
                except Exception as e:
                    res = None
                    self.log(f"[!] realign failed: {e}")
                if res is not None:
                    bx_new, by_new = res
                    live_affine["bx"] = bx_new
                    live_affine["by"] = by_new
                    rebuild_targets()
                    self.log(f"Realigned: bx={bx_new:.2f} by={by_new:.2f}")
                else:
                    self.log("Realign skipped/failed; using calibration as-is.")

        def producer():
            idx = 0
            next_t = time.monotonic()
            while not state["stop"].is_set():
                if state["running"]:
                    # Read targets via the mutable box so any realign that
                    # happened during intro() is picked up here.
                    targets = adb_targets_box["pts"]
                    if not targets:
                        time.sleep(0.05)
                        continue
                    base = targets[idx % len(targets)]
                    # Apply live offset right here so arrow nudges show up
                    # in the very next tap.
                    self.adb.tap(base[0] + self.live_offset_x,
                                 base[1] + self.live_offset_y)
                    idx = (idx + 1) % len(targets)
                next_t += interval
                d = next_t - time.monotonic()
                if d > 0:
                    time.sleep(d)
                else:
                    next_t = time.monotonic()

        # Shift+1 toggle implemented via polling (no pynput.Listener).
        # The hotkey doesn't conflict with fn 2 (middle/side mouse) and is
        # global -- it works regardless of which window has focus.
        def shift_one_watcher():
            prev = False  # Previous combo state for edge detection
            while not state["stop"].is_set():
                try:
                    now = pat_input.is_shift_one_combo_down()
                except Exception:
                    now = False
                if now and not prev:
                    # Press edge: toggle running state. We mark prev=True
                    # *before* starting work so a single Shift+1 press
                    # cannot retrigger if the producer is slow.
                    if not state["running"]:
                        def _start():
                            try:
                                intro()
                            except Exception as e:
                                self.log(f"[!] intro failed: {e}")
                                return
                            worker.start()
                            state["running"] = True
                            self.log(f"Main clicker: RUNNING at {cps} CPS over "
                                     f"{len(adb_targets_box['pts'])} points")
                        threading.Thread(target=_start, daemon=True).start()
                    else:
                        state["running"] = False
                        worker.stop(kill_shell=False)
                        self.log("Main clicker: PAUSED")
                prev = now
                time.sleep(0.05)  # 20 polls/sec is plenty for a toggle

        watch_thread = threading.Thread(target=shift_one_watcher, daemon=True)
        watch_thread.start()
        state["watch"] = watch_thread
        prod_thread = threading.Thread(target=producer, daemon=True)
        prod_thread.start()
        state["thread"] = prod_thread

        self.main_clicker_running = True
        self.btn_run_clicker.config(text="Stop Main Clicker (fn 1)")
        self.log("Main clicker ARMED. Press SHIFT + 1 to start/stop spamming.")

    def _stop_main_clicker(self) -> None:
        """Tear down the main clicker subsystem."""
        state = self._main_state
        state["running"] = False
        state["stop"].set()  # Producer + watcher threads exit on next tick
        if state.get("worker"):
            state["worker"].stop(kill_shell=True)
        self.main_clicker_running = False
        self.btn_run_clicker.config(text="Start Main Clicker (fn 1)")
        self.log("Main clicker stopped.")

    # ----- Function 2: hold-to-spam toggle --------------------------------
    def toggle_hold_to_spam(self) -> None:
        """
        Start or stop the hold-to-spam follow-cursor mode.

        Implementation note: this function used to install pynput.Listener
        objects for both mouse and keyboard, which on macOS 26.3 triggers a
        SIGTRAP inside HIToolbox when called from a background thread (see
        crash log analysis in CHANGES). We now POLL the activator state
        through pat_input, which does not install any event taps and is
        safe to call from any thread on every supported OS.

        Hardened against the macOS Tk quirk that SILENTLY swallows
        exceptions raised from a button callback: every prep step is
        wrapped in try/except and any failure is surfaced as a log line
        AND a messagebox so the user is never stuck wondering why a
        click on the button produced nothing.
        """
        try:
            self.log("fn 2: toggle requested")
            if self.spam_running:
                self.log("fn 2: stopping (already running)")
                self._stop_hold_to_spam()
                return
            if not self._require_adb():
                self.log("fn 2: ADB not connected -- aborting.")
                return
            size = self._ensure_screen_size()
            if size is None:
                self.log("fn 2: could not get emulator screen size -- aborting.")
                return
            adb_w, adb_h = size
            self.log(f"fn 2: emulator size = {adb_w}x{adb_h}")
            # fn 2 no longer needs a separate calibration file. The OS
            # already knows where the BlueStacks window lives (via
            # WindowLocator) and we already know the emulator resolution
            # (wm size). Together they give us a direct screen->ADB
            # mapping with NO user calibration step required.
            self.log("fn 2: looking for BlueStacks window via OS API...")
            bs_window = WindowLocator().find()
            if bs_window is None:
                self.log("fn 2: WindowLocator returned None -- aborting.")
                messagebox.showerror(
                    "BlueStacks window not found",
                    "Could not locate the BlueStacks window via the OS.\n"
                    "Make sure BlueStacks is visible and try again.")
                return
            self.log(f"fn 2: BS window found at x={bs_window['x']} "
                     f"y={bs_window['y']} w={bs_window['w']} h={bs_window['h']}")
            try:
                mapping = ScreenToAdb(
                    tl_screen=(float(bs_window["x"]), float(bs_window["y"])),
                    br_screen=(float(bs_window["x"] + bs_window["w"]),
                               float(bs_window["y"] + bs_window["h"])),
                    adb_w=adb_w, adb_h=adb_h,
                )
            except Exception as e:
                self.log(f"fn 2: ScreenToAdb constructor failed: {e}")
                messagebox.showerror("fn 2 init failed",
                                     f"ScreenToAdb constructor failed: {e}")
                return
            self.log(f"fn 2 screen->ADB: window=({bs_window['x']},{bs_window['y']}) "
                     f"size={bs_window['w']}x{bs_window['h']} "
                     f"-> ADB {adb_w}x{adb_h}")
        except Exception as e:
            # Catch-all: anything that bubbles out of the prep phase ends
            # up here so the user gets a visible error instead of silence.
            import traceback
            tb = traceback.format_exc()
            self.log(f"fn 2: prep phase crashed:\n{tb}")
            messagebox.showerror("fn 2 prep crashed", f"{e}\n\n{tb}")
            return
        worker = ClickWorker(self.adb)
        stop_flag = threading.Event()
        state = self._spam_state
        state["worker"] = worker
        state["stop"] = stop_flag
        state["mapping"] = mapping
        # We deliberately CAP the rate at the real-world ADB throughput so
        # each tap is sent for a cursor position no older than ~30 ms.
        target_cps = 30.0
        interval = 1.0 / target_cps

        # Caps Lock semantics: CL is a TOGGLE key on every OS, so the LED
        # Activator: hold the MIDDLE mouse button OR a SIDE mouse button.
        # Both are read by polling via pat_input (no global listener), so
        # this works without any extra macOS permissions.
        # Caps Lock was used previously but proved unreliable on macOS -- on
        # some keyboards the toggle event was eaten before pyobjc could see
        # the new LED state -- so it's no longer accepted as an activator.
        def is_holding() -> bool:
            return (pat_input.is_middle_mouse_button_down()
                    or pat_input.is_side_mouse_button_down())

        def producer():
            next_t = time.monotonic()
            was_holding = False
            n = 0
            while not stop_flag.is_set():
                try:
                    now_holding = is_holding()
                except Exception as e:
                    # Polling shouldn't raise, but if pyobjc has a transient
                    # hiccup we'd rather skip the tick than tear down.
                    self.log(f"[!] activator poll failed: {e}")
                    time.sleep(0.05)
                    continue
                if now_holding and not was_holding:
                    worker.start()
                    self.log("Spam: ON")
                elif (not now_holding) and was_holding:
                    worker.stop(kill_shell=False)
                    self.log("Spam: OFF")
                if now_holding:
                    # Thread-safe cursor read: on macOS this goes through
                    # AppKit.NSEvent.mouseLocation() (no HIToolbox / no
                    # CFRunLoop), on Windows / Linux it's pynput which is
                    # also safe from background threads on those OSes.
                    try:
                        sx, sy = _read_cursor_logical()
                    except Exception as e:
                        self.log(f"[!] cursor read failed: {e}")
                        time.sleep(0.05)
                        continue
                    m = state["mapping"]
                    # We INTENTIONALLY do not gate on m.in_window(sx, sy).
                    # ScreenToAdb.to_adb() clamps the result into the [0,
                    # adb_w-1] x [0, adb_h-1] range, so even if the cursor
                    # is outside the calibrated rectangle the ADB tap still
                    # lands on the edge of the emulator screen instead of
                    # being silently dropped. This matches the user's
                    # intent ("click wherever the cursor is") and removes
                    # the "cursor outside area" noise when panning/zooming
                    # the in-game map -- those motions never move the
                    # BlueStacks window, but they can move the cursor near
                    # the edges of the host screen.
                    ax, ay = m.to_adb(sx, sy)
                    ax += self.live_offset_x  # Apply live nudge offset
                    ay += self.live_offset_y
                    try:
                        self.adb.tap(ax, ay)
                    except Exception as e:
                        self.log(f"[!] tap failed: {e}")
                    n += 1
                    if n % 60 == 0:  # Every ~2 seconds at 30 CPS
                        self.log(f"#{n} screen=({sx:.0f},{sy:.0f}) "
                                 f"adb=({ax:.0f},{ay:.0f})")
                was_holding = now_holding
                next_t += interval
                d = next_t - time.monotonic()
                if d > 0:
                    time.sleep(d)
                else:
                    next_t = time.monotonic()

        # Wrap the thread-start AND button-state changes in a try block so
        # any silent macOS Tk failure surfaces as a visible error instead
        # of "button click does nothing".
        try:
            prod = threading.Thread(target=producer, daemon=True)
            prod.start()
            state["thread"] = prod
            cap = (pat_input.macos_check_input_capability()
                   if sys.platform == "darwin" else {})
            if cap:
                self.log(f"macOS input capability: {cap}")
            self.spam_running = True
            self.btn_run_spam.config(text="Stop Hold-to-Spam (fn 2)")
            self.log("Hold-to-spam ARMED. "
                     "Hold the MIDDLE or a SIDE mouse button to spam.")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.log(f"fn 2: thread/button setup crashed:\n{tb}")
            # Try to roll back the worker we already created.
            try:
                worker.stop(kill_shell=True)
            except Exception:
                pass
            self.spam_running = False
            messagebox.showerror("fn 2 start crashed", f"{e}\n\n{tb}")

    def _stop_hold_to_spam(self) -> None:
        """Tear down the hold-to-spam subsystem."""
        state = self._spam_state
        state["stop"].set()
        if state.get("worker"):
            state["worker"].stop(kill_shell=True)
        self.spam_running = False
        self.btn_run_spam.config(text="Enter Hold-to-Spam (fn 2)")
        self.log("Hold-to-spam stopped.")

    # ----- shutdown --------------------------------------------------------
    def _on_close(self) -> None:
        """Window close handler: tear down everything cleanly."""
        if self.main_clicker_running:
            self._stop_main_clicker()
        if self.spam_running:
            self._stop_hold_to_spam()
        self.shutting_down.set()
        try:
            self.adb.kill_shell()
        except Exception:
            pass
        self.root.destroy()

    # ----- entry point -----------------------------------------------------
    def run(self) -> None:
        """Enter the Tk main loop."""
        self.root.mainloop()


def main() -> None:
    """Module entry point."""
    app = PATClickerGUI()
    app.run()


if __name__ == "__main__":
    main()
