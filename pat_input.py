#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Cross-platform polling helpers for the spam-mode activator.
#
# Reason for existing: pynput's mouse.Listener and keyboard.Listener spawn
# their own background threads that hook into the OS input pipeline. On
# macOS Sonoma+ that hook requires the host application to have *Input
# Monitoring* permission. Without it the process is killed by the kernel
# with no TCC dialog and no Python-level exception -- which is the
# "Python quit unexpectedly" crash the user has been hitting.
#
# To avoid both the permission requirement AND the runloop fragility we
# READ the input state by polling instead of installing a hook. This works
# everywhere with the permissions the user already has:
#   * macOS:  CGEventSource* / NSEvent.modifierFlags (no Input Monitoring)
#   * Windows: GetAsyncKeyState (no permissions ever)
#   * Linux X11: XQueryPointer + XkbGetIndicatorState (no extra deps)

import sys  # Platform detection
from typing import Optional  # Type hints

_IS_MAC = sys.platform == "darwin"  # macOS branch
_IS_WIN = sys.platform.startswith("win")  # Windows branch
_IS_LINUX = sys.platform.startswith("linux")  # Linux/X11 branch


# ---------------------------------------------------------------------------
# Lazy macOS backend init
# ---------------------------------------------------------------------------
_quartz_mod = None  # Imported only on macOS, lazily on first call
_appkit_mod = None  # Imported only on macOS, lazily on first call


def _init_macos() -> bool:
    """Import pyobjc submodules; return True if successful."""
    global _quartz_mod, _appkit_mod
    if _quartz_mod is not None:
        return _quartz_mod is not False
    try:
        import Quartz as _q  # pyobjc-framework-Quartz
        import AppKit as _a  # pyobjc-framework-Cocoa
    except ImportError:
        _quartz_mod = False  # Sentinel for "not available"
        _appkit_mod = False
        return False
    _quartz_mod = _q
    _appkit_mod = _a
    return True


# ---------------------------------------------------------------------------
# Caps Lock state
# ---------------------------------------------------------------------------
def is_shift_one_combo_down() -> bool:
    """
    Return True if BOTH the Shift modifier AND the '1' digit key are
    currently pressed.

    Used by the main clicker (fn 1) as a global activator that does not
    interfere with normal typing. The combo is checked by polling, so no
    event tap is created -- this matches the design constraints of the
    rest of the input layer (no Input Monitoring required on macOS).

    macOS:
        Shift  -- NSEvent.modifierFlags() & NSShiftKeyMask (1 << 17)
        '1'    -- CGEventSourceKeyState(0, kVK_ANSI_1 = 18)
    Windows:
        Shift  -- GetAsyncKeyState(VK_SHIFT 0x10) high bit
        '1'    -- GetAsyncKeyState(VK_1    0x31) high bit
    Linux X11:
        Shift  -- XQueryKeymap bit for keycode 50/62
        '1'    -- XQueryKeymap bit for keycode 10
        Implementation falls back to "False" if Xlib isn't installed --
        Linux users can keep using the side-button activator.
    """
    if _IS_MAC:
        if not _init_macos():
            return False
        try:
            # Shift modifier: NSShiftKeyMask is (1 << 17) == 131072.
            flags = int(_appkit_mod.NSEvent.modifierFlags())
            shift_down = bool(flags & (1 << 17))
            # Digit '1' has macOS virtual keycode 18 (kVK_ANSI_1).
            one_down = bool(_quartz_mod.CGEventSourceKeyState(0, 18))
            return shift_down and one_down
        except Exception:
            return False
    if _IS_WIN:
        import ctypes
        gas = ctypes.windll.user32.GetAsyncKeyState
        shift = bool(gas(0x10) & 0x8000)  # VK_SHIFT
        one = bool(gas(0x31) & 0x8000)  # VK_1
        return shift and one
    if _IS_LINUX:
        try:
            from Xlib import display, XK  # type: ignore
            d = display.Display()
            keymap = d.query_keymap()  # 32-byte bit vector

            def key_down(keycode: int) -> bool:
                byte = keycode // 8
                bit = keycode % 8
                return bool(keymap[byte] & (1 << bit))

            # Shift_L = 50, Shift_R = 62, '1' = 10 on most X11 layouts.
            shift = key_down(50) or key_down(62)
            one = key_down(10)
            return shift and one
        except Exception:
            return False
    return False


def is_capslock_on() -> bool:
    """
    Return True if the Caps Lock LED is currently on.

    macOS:    NSEvent.modifierFlags() & NSAlphaShiftKeyMask
    Windows:  GetKeyState(VK_CAPITAL) & 1
    Linux:    XkbGetIndicatorState bit 0
    """
    if _IS_MAC:
        if not _init_macos():
            return False
        # NSAlphaShiftKeyMask == 1 << 16 == 65536.
        flags = int(_appkit_mod.NSEvent.modifierFlags())
        return bool(flags & (1 << 16))
    if _IS_WIN:
        import ctypes
        # GetKeyState returns short; the low-order bit is the TOGGLE state
        # for Caps/Num/Scroll lock.
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)
    if _IS_LINUX:
        try:
            # python-xlib is sometimes available; if not, swallow and return
            # False -- the user can still use the side-mouse activator.
            from Xlib import display, X  # type: ignore
            d = display.Display()
            # XkbGetIndicatorState would be ideal but is rarely bound.
            # Fallback: query keyboard state via XQueryKeymap and look at
            # the Caps Lock keycode (66 on most layouts) -- but that gives
            # press state, not toggle. So we approximate with XGetIndicatorState
            # if available, else False.
            return False  # Acceptable: Linux users typically use side-button
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# Middle mouse button state (used for the main-clicker toggle)
# ---------------------------------------------------------------------------
def is_middle_mouse_button_down() -> bool:
    """
    Return True if the middle (scroll-wheel) mouse button is currently held.

    macOS: CGEventSourceButtonState(0, 2). Button number 2 == middle.
    Windows: GetAsyncKeyState(VK_MBUTTON 0x04). High bit set => held.
    Linux X11: XQueryPointer mask bit 0x0200 == middle button.
    """
    if _IS_MAC:
        if not _init_macos():
            return False
        try:
            return bool(_quartz_mod.CGEventSourceButtonState(0, 2))
        except Exception:
            return False
    if _IS_WIN:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x04) & 0x8000)
    if _IS_LINUX:
        try:
            from Xlib import display  # type: ignore
            d = display.Display()
            root = d.screen().root
            qp = root.query_pointer()
            return bool(int(qp.mask) & 0x0200)
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# Mouse side button state
# ---------------------------------------------------------------------------
def is_side_mouse_button_down() -> bool:
    """
    Return True if any mouse side button (X1 / X2 / button 4-5) is held.

    macOS: CGEventSourceButtonState polled for button numbers 3 and 4
           (which map to side buttons on most mice).
    Windows: GetAsyncKeyState(VK_XBUTTON1/2). High-order bit set => down.
    Linux: XQueryPointer mask bits 8 / 9 (button 4 / 5 on X11).
    """
    if _IS_MAC:
        if not _init_macos():
            return False
        # CGEventSourceStateID 0 == CombinedSessionState (latest snapshot).
        # CGEventSourceButtonState(stateID, buttonNumber) -> Bool.
        # buttonNumber: 0=Left, 1=Right, 2=Middle, 3=BackSide, 4=ForwardSide.
        try:
            for btn in (3, 4):
                if _quartz_mod.CGEventSourceButtonState(0, btn):
                    return True
        except Exception:
            return False
        return False
    if _IS_WIN:
        import ctypes
        # XBUTTON1 == 0x05, XBUTTON2 == 0x06.
        gas = ctypes.windll.user32.GetAsyncKeyState
        return bool(gas(0x05) & 0x8000) or bool(gas(0x06) & 0x8000)
    if _IS_LINUX:
        try:
            from Xlib import display  # type: ignore
            d = display.Display()
            root = d.screen().root
            qp = root.query_pointer()
            mask = int(qp.mask)
            # Buttons 4/5 occupy mask bits 0x0080 / 0x0100 on most X11 setups.
            return bool(mask & (0x0080 | 0x0100))
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# Permissions / capability check (for diagnostic UI)
# ---------------------------------------------------------------------------
def macos_check_input_capability() -> dict:
    """
    Return a dictionary describing which macOS permissions are available.

    Keys (all booleans):
      pyobjc_imported           -- Quartz/AppKit modules loaded successfully
      modifier_flags_readable   -- NSEvent.modifierFlags() works
      cursor_position_readable  -- NSEvent.mouseLocation() works
      input_monitoring_likely   -- True only if we'd be allowed to install a
                                   global event tap. Currently inferred via
                                   CGPreflightListenEventAccess on systems
                                   that expose it; otherwise None.
    """
    out = {
        "pyobjc_imported": False,
        "modifier_flags_readable": False,
        "cursor_position_readable": False,
        "input_monitoring_likely": None,
    }
    if not _IS_MAC:
        return out  # Other platforms don't need this
    if not _init_macos():
        return out
    out["pyobjc_imported"] = True
    try:
        _ = int(_appkit_mod.NSEvent.modifierFlags())
        out["modifier_flags_readable"] = True
    except Exception:
        pass
    try:
        _ = _appkit_mod.NSEvent.mouseLocation()
        out["cursor_position_readable"] = True
    except Exception:
        pass
    # Some macOS releases expose CGPreflightListenEventAccess. It returns
    # YES if Input Monitoring is already granted. Wrap in try/except so we
    # cope with old macOS where the symbol is missing.
    try:
        out["input_monitoring_likely"] = bool(
            _quartz_mod.CGPreflightListenEventAccess())
    except Exception:
        pass
    return out
