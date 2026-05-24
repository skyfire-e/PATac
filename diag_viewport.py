#!/usr/bin/env python3
"""
Diagnostic: figure out the real game viewport bounds in ADB pixels.

Sends 1 tap at the center of the Android display and dumps EVERYTHING
the Android system says about open windows + their bounds. The game
window's bounds in ADB pixels are then ready to read by eye.

Run:
    cd /Users/skyfire/Documents/oc_agent
    source .venv/bin/activate
    python3 diag_viewport.py
"""

import shutil
import subprocess
import sys


def adb(*args, timeout=8):
    adb_bin = shutil.which("adb") or "adb"
    proc = subprocess.run([adb_bin, *args],
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


# 1. List active windows with their visible frames -- this tells us the
#    exact bounds (in physical pixels) where each Android window lives.
section("dumpsys window windows | mShownFrame / Frame")
_, out, _ = adb("shell", "dumpsys", "window", "windows")
for line in out.splitlines():
    s = line.strip()
    if any(tag in s for tag in (
            "Window #", "mPackageName", "mFrame", "Frame:",
            "mShownFrame", "ContentFrame", "DisplayFrame", "VisibleFrame")):
        print(s)

# 2. Find the foreground (focused) window and dump just its info.
section("dumpsys window | focused window")
_, out, _ = adb("shell", "dumpsys", "window")
in_focus_block = False
for line in out.splitlines():
    if "mCurrentFocus" in line or "mFocusedApp" in line:
        print(line.rstrip())

# 3. Activity manager: which task is on top and what are its bounds?
section("dumpsys activity activities | top task bounds")
_, out, _ = adb("shell", "dumpsys", "activity", "activities")
keep = False
for line in out.splitlines():
    s = line.strip()
    if "Task{" in s or "TaskRecord{" in s:
        keep = True
    if keep:
        if any(tag in s for tag in ("Task{", "TaskRecord{",
                                    "mBounds", "Bounds=", "mLastNonFullscreenBounds",
                                    "fullscreen=", "ActivityRecord{", "topResumed",
                                    "realActivity")):
            print(s)
        if s.startswith("Hist #0"):  # Top-most history
            keep = False

# 4. Pull the game's package name from focused-window output, then ask
#    the system explicitly about that activity's visible region.
section("guessed PAT package")
_, out, _ = adb("shell", "dumpsys", "window")
for line in out.splitlines():
    if "mCurrentFocus" in line:
        # Looks like "  mCurrentFocus=Window{... u0 com.pkg/com.Activity}"
        parts = line.strip().split()
        for p in parts:
            if "/" in p and "." in p:
                pkg = p.split("/")[0].strip("}")
                print(f"  package = {pkg}")
                # Now dumpsys for the activity itself:
                section(f"dumpsys activity {pkg}")
                _, out2, _ = adb("shell", "dumpsys", "activity", pkg)
                for ln in out2.splitlines():
                    s = ln.strip()
                    if any(t in s for t in ("mBounds", "Bounds=", "Hist",
                                             "DecorView", "Surface")):
                        print(s)
                break
        break
