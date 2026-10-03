"""Dark title bar and menu bar for the window, whatever the Windows theme: the pages are always dark."""
from __future__ import annotations

import ctypes
import logging

log = logging.getLogger("vsm.desktop")

BACKGROUND = (0x09, 0x09, 0x0B)  # zinc-950, the page background
HOVER = (0x27, 0x27, 0x2A)  # zinc-800
BORDER = (0x3F, 0x3F, 0x46)  # zinc-700
TEXT = (0xE4, 0xE4, 0xE7)  # zinc-200

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35

_color_table = None


def colorref(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return (b << 16) | (g << 8) | r


def _title_bar(hwnd: int) -> None:
    set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
    set_attribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    # The colour attributes need Windows 11; on Windows 10 only the dark mode switch applies.
    for attribute, value in (
        (DWMWA_USE_IMMERSIVE_DARK_MODE, 1),
        (DWMWA_CAPTION_COLOR, colorref(BACKGROUND)),
        (DWMWA_BORDER_COLOR, colorref(BORDER)),
    ):
        set_attribute(hwnd, attribute, ctypes.byref(ctypes.c_int(value)), 4)


def _dark_color_table():
    """A WinForms colour table for the menu bar and its drop-downs, built once."""
    global _color_table
    if _color_table is None:
        from System.Drawing import Color
        from System.Windows.Forms import ProfessionalColorTable

        background, hover, border = (Color.FromArgb(*c) for c in (BACKGROUND, HOVER, BORDER))
        flat = {
            background: (
                "MenuStripGradientBegin", "MenuStripGradientEnd", "ToolStripDropDownBackground",
                "ImageMarginGradientBegin", "ImageMarginGradientMiddle", "ImageMarginGradientEnd",
            ),
            hover: (
                "MenuItemSelected", "MenuItemSelectedGradientBegin", "MenuItemSelectedGradientEnd",
                "MenuItemPressedGradientBegin", "MenuItemPressedGradientMiddle", "MenuItemPressedGradientEnd",
            ),
            border: ("MenuBorder", "MenuItemBorder", "SeparatorDark", "SeparatorLight"),
        }
        members = {"__namespace__": "VeinServerManager"}
        for color, names in flat.items():
            for name in names:
                members["get_" + name] = lambda self, color=color: color
        _color_table = type("DarkColorTable", (ProfessionalColorTable,), members)
    return _color_table()


def _menu(form) -> None:
    from System.Drawing import Color
    from System.Windows.Forms import MenuStrip, ToolStripMenuItem, ToolStripProfessionalRenderer

    text = Color.FromArgb(*TEXT)
    background = Color.FromArgb(*BACKGROUND)

    def paint(items) -> None:
        for item in items:
            item.ForeColor = text
            if isinstance(item, ToolStripMenuItem):
                paint(item.DropDownItems)

    for control in form.Controls:
        if isinstance(control, MenuStrip):
            control.Renderer = ToolStripProfessionalRenderer(_dark_color_table())
            control.BackColor = background
            control.ForeColor = text
            paint(control.Items)


def darken(window) -> None:
    """Call once the window is shown. Purely cosmetic, so a failure is only logged."""
    try:
        from System import Action

        form = window.native

        def apply() -> None:
            _title_bar(form.Handle.ToInt64())
            _menu(form)

        if form.InvokeRequired:
            form.Invoke(Action(apply))
        else:
            apply()
    except Exception as e:
        log.warning("could not apply the dark window theme: %s", e)


def darken_when_shown(window) -> None:
    """A window opened while the program is running can be on screen before create_window returns,
    so its "shown" event may already have passed: darken it now in that case. Twice does no harm."""
    window.events.shown += lambda: darken(window)
    if window.events.shown.is_set():
        darken(window)
