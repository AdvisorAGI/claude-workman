"""Native keyboard shortcuts, as a table to query instead of a guess to make.

An agent driving a desktop is far more reliable through the keyboard than
through the mouse: a chord hits the same command whatever the window is doing,
survives a layout change, and needs no screenshot to aim. The problem is that
the chords differ per OS *and* per app, and a model that half-remembers them
does real damage — `ctrl+w` in the wrong window closes the user's work, and
`ctrl+c` in a terminal kills the job it was supposed to copy from. So the
knowledge lives here as data with an explicit "I do not know" value, rather
than in prose the model has to recall under pressure.

Three decisions shape the table:

**Everything is xdotool syntax, on every platform.** `desktop.press_key` takes
one spelling ('Return', 'shift+ctrl+t', 'super+v') and each backend translates
it, mapping `super` to Command on macOS and Win on Windows. A table that
emitted native glyphs would have to be un-translated before it could be
pressed, so it emits exactly what `press_key` accepts. Key *names* are the X
keysym spelling ('BackSpace', 'Page_Down', 'Print') because the Linux backend
hands the string straight to xdotool, which is case-sensitive, while the other
two lowercase before lookup — the X spelling is the one all three accept.

**A miss is a result, not an exception.** An unknown action comes back with
`ok: False` and near-matches, and a known action the platform genuinely lacks
comes back with `keys: None`, `unsupported: True` and a note saying what to do
instead. This matches `platform.base.unsupported`: a tool that raises tells a
model nothing about its next move.

**A blank is better than a plausible chord.** Where a platform has no real
equivalent, or where the answer is only a convention some apps happen to
follow, the entry is None with a note. Coverage is worth less than the promise
that a chord which IS listed is a chord that works.

**A blank names its replacement.** An action with no chord can still have a
route, and a row may carry one as a third element: a small dict naming the
tool that performs it and the arguments to perform it with. macOS window
tiling is the case that forced this. Its chords are Globe(fn) plus an arrow,
and macOS resolves fn below the event tap, so the flag is stripped from a
synthetic event and nothing happens; measured, posting Delete with
kCGEventFlagMaskSecondaryFn set changed nothing while plain ForwardDelete
removed a character. The menu items have no key equivalent either
(AXMenuItemCmdChar is missing value), so there is no chord to find. The
feature is still there, reachable through the Window > Move & Resize menu, and
`route` is what tells a caller so instead of leaving it at "not supported".

Resolution order is app layer, then system layer, so `resolve('copy',
app='terminal')` returns the terminal's `shift+ctrl+c` on Linux rather than the
`ctrl+c` that would interrupt the running command.

    python -m workman.shortcuts command_palette --app vscode
    python -m workman.shortcuts --list --group window
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from typing import Any

from .platform import backend_name

#: Platform keys used throughout. macOS 26, GNOME on X11 (Ubuntu 24.04 /
#: GNOME 46 is the reference), Windows 11.
PLATFORMS: tuple[str, ...] = ("darwin", "linux", "win32")

#: Coarse buckets, so `list_actions(group="window")` is a usable survey rather
#: than a wall.
GROUPS: tuple[str, ...] = ("window", "tabs", "navigation", "editing",
                           "text_motion", "system", "app")

# Modifiers are written shift, ctrl, alt, super in that order throughout.
# Order is irrelevant to `base.split_combo`, but one order makes the table
# diffable and makes a stray glyph or a capitalised modifier obvious on sight.

_PLATFORM_ALIASES = {
    "darwin": "darwin", "mac": "darwin", "macos": "darwin", "osx": "darwin",
    "linux": "linux", "linux_x11": "linux", "x11": "linux", "gnome": "linux",
    "win32": "win32", "win": "win32", "windows": "win32",
}

_BACKEND_TO_PLATFORM = {"linux_x11": "linux", "darwin": "darwin", "win32": "win32"}

_ACTION_ALIASES = {
    "overview": "mission_control",
    "expose": "mission_control",
    "launcher": "spotlight",
    "start_menu": "spotlight",
    "find_in_page": "find",
    "replace": "find_replace",
    "task_view": "mission_control",
    "url_bar": "address_bar",
    "location_bar": "address_bar",
    "goto_line": "go_to_line",
    "goto_file": "go_to_file",
    "quit": "quit_app",
    "close": "close_window",
    "screenshot": "screenshot_region",
}

_APP_ALIASES = {
    "vscode": "vscode", "code": "vscode", "vs_code": "vscode",
    "visual_studio_code": "vscode", "cursor": "vscode", "windsurf": "vscode",
    "chrome": "chrome", "google_chrome": "chrome", "chromium": "chrome",
    "brave": "chrome", "edge": "chrome",
    "terminal": "terminal", "iterm": "terminal", "iterm2": "terminal",
    "gnome_terminal": "terminal", "gnome-terminal": "terminal",
    "windows_terminal": "terminal", "wt": "terminal", "konsole": "terminal",
    "files": "files", "finder": "files", "explorer": "files",
    "file_explorer": "files", "nautilus": "files",
}

# ---------------------------------------------------------------------------
# The system layer: what the OS itself (or a convention every app on it
# follows) provides. Each entry is (keys, note) per platform, optionally
# (keys, note, route); keys=None means "there is no chord for this here", the
# note says what to do instead, and the route says it as data.
# ---------------------------------------------------------------------------
_SYSTEM: dict[str, dict[str, Any]] = {
    # ---- window ----------------------------------------------------------
    "minimize": {
        "group": "window",
        "what": "Hide the active window to the dock/taskbar",
        "darwin": ("super+m", ""),
        "linux": ("super+h", "the GNOME default; a few desktops leave minimize "
                             "unbound, so screenshot to confirm it took"),
        "win32": ("super+Down", "on a maximized window the first press restores "
                                "it and a second press minimizes"),
    },
    "close_window": {
        "group": "window",
        "what": "Close the active window, leaving the app running",
        "darwin": ("super+w", ""),
        "linux": ("alt+F4", "GNOME also binds super+q to close the window, not "
                            "to quit the app"),
        "win32": ("alt+F4", "quits the app too when this is its last window"),
    },
    "quit_app": {
        "group": "window",
        "what": "Quit the whole application, not just this window",
        "darwin": ("super+q", ""),
        "linux": (None, "no desktop-wide quit chord: ctrl+q is only a GTK "
                        "convention and major apps ignore it. Use the app layer "
                        "(chrome, terminal, vscode) or close every window"),
        "win32": ("alt+F4", "Windows has no separate quit; this closes the "
                            "active window and ends the app with the last one"),
    },
    "fullscreen": {
        "group": "window",
        "what": "Toggle fullscreen for the active window",
        "darwin": ("ctrl+super+f", ""),
        "linux": ("F11", "an app-level convention (browsers, editors, file "
                         "managers); the window manager itself binds nothing"),
        "win32": ("F11", "an app-level convention; not every app implements it"),
    },
    "maximize": {
        "group": "window",
        "what": "Fill the work area without entering fullscreen",
        "darwin": (None,
                   "macOS has no maximize chord: the green button enters full "
                   "screen, which is a different thing. The Window menu's own "
                   "Fill is what maximize means here, so use "
                   "window_tile(action='fill'), or layout_full, which asks for "
                   "Fill and computes the rectangle only if the app has no "
                   "Window menu",
                   {"tool": "window_tile", "action": "fill",
                    "menu_path": ["Window", "Fill"], "fallback": "layout_full"}),
        "linux": ("super+Up", ""),
        "win32": ("super+Up", ""),
    },
    "move_window_left_half": {
        "group": "window",
        "what": "Snap the active window to the left half of the screen",
        "darwin": (None,
                   "the built-in chord is Globe(fn)+ctrl+Left, and macOS "
                   "resolves fn below the event tap: a synthetic event arrives "
                   "with the flag stripped, so it does nothing. The menu item "
                   "has no key equivalent either, so there is no chord to send "
                   "at all. Use window_tile(action='tile_left'), which clicks "
                   "Window > Move & Resize > Left and gets the OS's own half",
                   {"tool": "window_tile", "action": "tile_left",
                    "menu_path": ["Window", "Move & Resize", "Left"],
                    "fallback": "window_geometry"}),
        "linux": ("super+Left", ""),
        "win32": ("super+Left", "Snap Assist may then offer to fill the other "
                                "half; press Escape to dismiss it"),
    },
    "move_window_right_half": {
        "group": "window",
        "what": "Snap the active window to the right half of the screen",
        "darwin": (None,
                   "same as move_window_left_half: the fn modifier does not "
                   "survive a synthetic event, so the chord cannot be sent. "
                   "Use window_tile(action='tile_right'), which clicks "
                   "Window > Move & Resize > Right",
                   {"tool": "window_tile", "action": "tile_right",
                    "menu_path": ["Window", "Move & Resize", "Right"],
                    "fallback": "window_geometry"}),
        "linux": ("super+Right", ""),
        "win32": ("super+Right", "Escape dismisses the Snap Assist chooser"),
    },
    "switch_window_same_app": {
        "group": "window",
        "what": "Cycle windows belonging to the app already in front",
        "darwin": ("super+grave", ""),
        "linux": ("alt+grave", ""),
        "win32": (None, "Windows cycles all windows, not one app's. Pick the "
                        "window by name with focus_window instead"),
    },

    # ---- tabs ------------------------------------------------------------
    # Tabs are an app feature, not an OS one, but the chords are consistent
    # enough across browsers, editors and file managers to be worth a default.
    # The terminal layer overrides them, because terminals are the exception.
    "new_tab": {
        "group": "tabs",
        "what": "Open a new tab in a tabbed app",
        "darwin": ("super+t", ""),
        "linux": ("ctrl+t", ""),
        "win32": ("ctrl+t", ""),
    },
    "close_tab": {
        "group": "tabs",
        "what": "Close the current tab",
        "darwin": ("super+w", "closes the window when only one tab is left"),
        "linux": ("ctrl+w", "closes the window when only one tab is left"),
        "win32": ("ctrl+w", "closes the window when only one tab is left"),
    },
    "reopen_tab": {
        "group": "tabs",
        "what": "Reopen the tab that was just closed",
        "darwin": ("shift+super+t", ""),
        "linux": ("shift+ctrl+t", ""),
        "win32": ("shift+ctrl+t", ""),
    },
    "next_tab": {
        "group": "tabs",
        "what": "Move to the tab on the right",
        "darwin": ("ctrl+Tab", "browsers also accept alt+super+Right"),
        "linux": ("ctrl+Tab", ""),
        "win32": ("ctrl+Tab", ""),
    },
    "prev_tab": {
        "group": "tabs",
        "what": "Move to the tab on the left",
        "darwin": ("shift+ctrl+Tab", "browsers also accept alt+super+Left"),
        "linux": ("shift+ctrl+Tab", ""),
        "win32": ("shift+ctrl+Tab", ""),
    },

    # ---- navigation ------------------------------------------------------
    "address_bar": {
        "group": "navigation",
        "what": "Focus the URL/location bar and select what is in it",
        "darwin": ("super+l", "browsers and file managers; typing replaces the "
                              "selection, so no need to clear it first"),
        "linux": ("ctrl+l", "browsers and file managers"),
        "win32": ("ctrl+l", "browsers and File Explorer"),
    },
    "find": {
        "group": "navigation",
        "what": "Open find-in-page / find-in-document",
        "darwin": ("super+f", ""),
        "linux": ("ctrl+f", ""),
        "win32": ("ctrl+f", ""),
    },
    "find_replace": {
        "group": "navigation",
        "what": "Open find-and-replace",
        "darwin": (None, "macOS has no single convention: editors use "
                         "alt+super+f, document apps use shift+super+h. Ask for "
                         "this action with the app named"),
        "linux": ("ctrl+h", "the editor convention; browsers have no replace"),
        "win32": ("ctrl+h", "the editor convention; browsers have no replace"),
    },

    # ---- editing ---------------------------------------------------------
    "copy": {
        "group": "editing",
        "what": "Copy the selection to the clipboard",
        "darwin": ("super+c", ""),
        "linux": ("ctrl+c", "NOT in a terminal, where it interrupts the running "
                            "command; resolve with app='terminal' there"),
        "win32": ("ctrl+c", "NOT in a console, where it interrupts; resolve "
                            "with app='terminal' there"),
    },
    "cut": {
        "group": "editing",
        "what": "Cut the selection to the clipboard",
        "darwin": ("super+x", ""),
        "linux": ("ctrl+x", ""),
        "win32": ("ctrl+x", ""),
    },
    "paste": {
        "group": "editing",
        "what": "Paste the clipboard",
        "darwin": ("super+v", ""),
        "linux": ("ctrl+v", "terminals need shift+ctrl+v; see app='terminal'"),
        "win32": ("ctrl+v", ""),
    },
    "paste_plain": {
        "group": "editing",
        "what": "Paste without carrying the source formatting",
        "darwin": ("shift+alt+super+v", "Paste and Match Style, a Cocoa "
                                        "convention most native apps honour"),
        "linux": (None, "no system-wide plain paste; Chrome and most GTK apps "
                        "use shift+ctrl+v, editors paste plain already"),
        "win32": (None, "no system-wide plain paste; Chrome and Office use "
                        "shift+ctrl+v, editors paste plain already"),
    },
    "undo": {
        "group": "editing",
        "what": "Undo the last edit",
        "darwin": ("super+z", ""),
        "linux": ("ctrl+z", "in a terminal this suspends the foreground job "
                            "instead; see app='terminal'"),
        "win32": ("ctrl+z", ""),
    },
    "redo": {
        "group": "editing",
        "what": "Redo the edit that was just undone",
        "darwin": ("shift+super+z", ""),
        "linux": ("shift+ctrl+z", ""),
        "win32": ("ctrl+y", "many apps accept shift+ctrl+z as well; ctrl+y is "
                            "the one Windows apps agree on"),
    },
    "select_all": {
        "group": "editing",
        "what": "Select everything in the focused field or document",
        "darwin": ("super+a", ""),
        "linux": ("ctrl+a", "in a terminal this jumps to the start of the line"),
        "win32": ("ctrl+a", ""),
    },
    "save": {
        "group": "editing",
        "what": "Save the current document",
        "darwin": ("super+s", ""),
        "linux": ("ctrl+s", "in a terminal this sends XOFF and freezes output; "
                            "see app='terminal'"),
        "win32": ("ctrl+s", ""),
    },
    "delete_word": {
        "group": "editing",
        "what": "Delete the word before the cursor",
        "darwin": ("alt+BackSpace", ""),
        "linux": ("ctrl+BackSpace", "a terminal does not receive this as a "
                                    "distinct key; see app='terminal'"),
        "win32": ("ctrl+BackSpace", ""),
    },
    "new_line_below": {
        "group": "editing",
        "what": "Open a fresh line under the cursor and put the cursor on it",
        "darwin": (None, "an editor feature, not an OS one. Press line_end then "
                         "Return, or resolve with app='vscode'"),
        "linux": (None, "an editor feature, not an OS one. Press line_end then "
                        "Return, or resolve with app='vscode'"),
        "win32": (None, "an editor feature, not an OS one. Press line_end then "
                        "Return, or resolve with app='vscode'"),
    },

    # ---- text motion -----------------------------------------------------
    "word_left": {
        "group": "text_motion",
        "what": "Move the cursor one word left (add shift+ to select)",
        "darwin": ("alt+Left", ""),
        "linux": ("ctrl+Left", ""),
        "win32": ("ctrl+Left", ""),
    },
    "word_right": {
        "group": "text_motion",
        "what": "Move the cursor one word right (add shift+ to select)",
        "darwin": ("alt+Right", ""),
        "linux": ("ctrl+Right", ""),
        "win32": ("ctrl+Right", ""),
    },
    "line_start": {
        "group": "text_motion",
        "what": "Move to the start of the current line",
        "darwin": ("super+Left", "only inside a text field. With focus "
                                 "elsewhere a browser reads this as Back and "
                                 "can lose a half-filled form. Cocoa text views "
                                 "also honour ctrl+a; a bare Home scrolls the "
                                 "document instead"),
        "linux": ("Home", ""),
        "win32": ("Home", ""),
    },
    "line_end": {
        "group": "text_motion",
        "what": "Move to the end of the current line",
        "darwin": ("super+Right", "only inside a text field. With focus "
                                  "elsewhere a browser reads this as Forward. "
                                  "Cocoa text views also honour ctrl+e"),
        "linux": ("End", ""),
        "win32": ("End", ""),
    },
    "doc_start": {
        "group": "text_motion",
        "what": "Move to the top of the document",
        "darwin": ("super+Up", ""),
        "linux": ("ctrl+Home", ""),
        "win32": ("ctrl+Home", ""),
    },
    "doc_end": {
        "group": "text_motion",
        "what": "Move to the bottom of the document",
        "darwin": ("super+Down", ""),
        "linux": ("ctrl+End", ""),
        "win32": ("ctrl+End", ""),
    },

    # ---- system ----------------------------------------------------------
    "switch_app": {
        "group": "system",
        "what": "Cycle to another running application",
        "darwin": ("super+Tab", "holds the switcher only while Command is down; "
                                "a single press flips to the previous app"),
        "linux": ("alt+Tab", "a single press flips to the previous app"),
        "win32": ("alt+Tab", "a single press flips to the previous app"),
    },
    "mission_control": {
        "group": "system",
        "what": "Show every window / the desktop overview",
        "darwin": ("ctrl+Up", ""),
        "linux": ("super+s", "GNOME's toggle-overview; a bare super tap does "
                             "the same, and typing there searches apps"),
        "win32": ("super+Tab", "opens Task View and leaves it open"),
    },
    "next_desktop": {
        "group": "system",
        "what": "Move to the next virtual desktop / space",
        "darwin": ("ctrl+Right", "only if more than one space exists; spaces "
                                 "cannot be created from the keyboard"),
        "linux": ("super+Page_Down", "GNOME 40+ lays workspaces out "
                                     "horizontally; ctrl+alt+Right also works"),
        "win32": ("ctrl+super+Right", ""),
    },
    "spotlight": {
        "group": "system",
        "what": "Open the app launcher / system search",
        "darwin": ("super+space", ""),
        "linux": ("super+a", "the GNOME application grid; the overview "
                             "(mission_control) also searches as you type"),
        "win32": ("super+s", "opens search; a bare super opens the Start menu"),
    },
    "screenshot_region": {
        "group": "system",
        "what": "Capture a region of the screen",
        "darwin": ("shift+super+4", "then drag; the file lands on the Desktop. "
                                    "Prefer this server's screenshot_region "
                                    "tool, which needs no drag and returns the "
                                    "image directly"),
        "linux": ("Print", "opens the GNOME screenshot UI, which still needs a "
                           "drag. Prefer this server's screenshot_region tool"),
        "win32": ("shift+super+s", "opens the snip overlay, which still needs a "
                                   "drag. Prefer this server's "
                                   "screenshot_region tool"),
    },
    "force_quit": {
        "group": "system",
        "what": "Kill an application that has stopped responding",
        "darwin": ("alt+super+Escape", "opens the Force Quit chooser; the "
                                       "hold-to-kill variant needs a 3s hold "
                                       "this server cannot send"),
        "linux": (None, "GNOME binds no force-quit chord. Use terminate_app, or "
                        "kill_window for a single window"),
        "win32": (None, "shift+ctrl+Escape only opens Task Manager, it kills "
                        "nothing. Use terminate_app"),
    },
}

# ---------------------------------------------------------------------------
# App layers. These win over the system layer, and only carry what genuinely
# differs or what the app alone provides — a survey via list_actions(app=...)
# merges the system layer back in, so nothing is lost by keeping these thin.
# ---------------------------------------------------------------------------
_APPS: dict[str, dict[str, dict[str, Any]]] = {
    # VS Code and its forks. Chords read out of the shipped default keybinding
    # table, not from memory, because the Linux defaults differ from the
    # Windows ones wherever the window manager already owns the chord.
    "vscode": {
        "command_palette": {
            "what": "Open the command palette",
            "darwin": ("shift+super+p", "F1 also works"),
            "linux": ("shift+ctrl+p", "F1 also works"),
            "win32": ("shift+ctrl+p", "F1 also works"),
        },
        "go_to_file": {
            "what": "Quick-open a file by name",
            "darwin": ("super+p", ""),
            "linux": ("ctrl+p", ""),
            "win32": ("ctrl+p", ""),
        },
        "go_to_line": {
            "what": "Jump to a line number",
            "darwin": ("ctrl+g", "Control, not Command: super+g is find-next"),
            "linux": ("ctrl+g", ""),
            "win32": ("ctrl+g", ""),
        },
        "toggle_terminal": {
            "what": "Show or hide the integrated terminal",
            "darwin": ("ctrl+grave", "Control, not Command, on every platform"),
            "linux": ("ctrl+grave", ""),
            "win32": ("ctrl+grave", ""),
        },
        "split_editor": {
            "what": "Split the editor into another group",
            "darwin": ("super+backslash", ""),
            "linux": ("ctrl+backslash", ""),
            "win32": ("ctrl+backslash", ""),
        },
        "comment_toggle": {
            "what": "Comment or uncomment the selected lines",
            "darwin": ("super+slash", ""),
            "linux": ("ctrl+slash", ""),
            "win32": ("ctrl+slash", ""),
        },
        "find_replace": {
            "what": "Open find-and-replace in the editor",
            "darwin": ("alt+super+f", ""),
            "linux": ("ctrl+h", ""),
            "win32": ("ctrl+h", ""),
        },
        "select_next_occurrence": {
            "group": "app",
            "what": "Add the next match of the selection as another cursor",
            "darwin": ("super+d", ""),
            "linux": ("ctrl+d", ""),
            "win32": ("ctrl+d", ""),
        },
        "add_cursor_below": {
            "group": "app",
            "what": "Put a second cursor on the line below",
            "darwin": ("alt+super+Down", ""),
            "linux": ("shift+alt+Down", "differs from Windows: ctrl+alt+Down is "
                                        "a workspace switch on GNOME"),
            "win32": ("ctrl+alt+Down", ""),
        },
        "add_cursor_above": {
            "group": "app",
            "what": "Put a second cursor on the line above",
            "darwin": ("alt+super+Up", ""),
            "linux": ("shift+alt+Up", "differs from Windows: ctrl+alt+Up is a "
                                      "workspace switch on GNOME"),
            "win32": ("ctrl+alt+Up", ""),
        },
        "new_line_below": {
            "what": "Open a fresh line under the cursor from anywhere on it",
            "darwin": ("super+Return", ""),
            "linux": ("ctrl+Return", ""),
            "win32": ("ctrl+Return", ""),
        },
        "paste_plain": {
            "what": "Paste without formatting",
            "darwin": (None, "the editor holds plain text, so an ordinary paste "
                             "is already plain"),
            "linux": (None, "the editor holds plain text, so an ordinary paste "
                            "is already plain"),
            "win32": (None, "the editor holds plain text, so an ordinary paste "
                            "is already plain"),
        },
        "quit_app": {
            "what": "Quit the editor",
            "darwin": ("super+q", ""),
            "linux": ("ctrl+q", ""),
            "win32": (None, "the editor registers no quit chord on Windows; "
                            "alt+F4 closes the window instead"),
        },
    },

    "chrome": {
        "new_window": {
            "group": "app",
            "what": "Open a new browser window",
            "darwin": ("super+n", ""),
            "linux": ("ctrl+n", ""),
            "win32": ("ctrl+n", ""),
        },
        "new_incognito_window": {
            "group": "app",
            "what": "Open a private window",
            "darwin": ("shift+super+n", ""),
            "linux": ("shift+ctrl+n", ""),
            "win32": ("shift+ctrl+n", ""),
        },
        "devtools": {
            "group": "app",
            "what": "Open or close DevTools",
            "darwin": ("alt+super+i", ""),
            "linux": ("shift+ctrl+i", "F12 also works"),
            "win32": ("shift+ctrl+i", "F12 also works"),
        },
        "back": {
            "group": "navigation",
            "what": "Go back one page",
            "darwin": ("super+bracketleft", ""),
            "linux": ("alt+Left", ""),
            "win32": ("alt+Left", ""),
        },
        "forward": {
            "group": "navigation",
            "what": "Go forward one page",
            "darwin": ("super+bracketright", ""),
            "linux": ("alt+Right", ""),
            "win32": ("alt+Right", ""),
        },
        "reload": {
            "group": "navigation",
            "what": "Reload the page",
            "darwin": ("super+r", ""),
            "linux": ("ctrl+r", ""),
            "win32": ("ctrl+r", ""),
        },
        "hard_reload": {
            "group": "navigation",
            "what": "Reload ignoring the cache",
            "darwin": ("shift+super+r", ""),
            "linux": ("shift+ctrl+r", ""),
            "win32": ("shift+ctrl+r", ""),
        },
        "find_next": {
            "group": "navigation",
            "what": "Jump to the next find-in-page match",
            "darwin": ("super+g", ""),
            "linux": ("ctrl+g", "Return in the find box does the same"),
            "win32": ("ctrl+g", "Return in the find box does the same"),
        },
        "find_replace": {
            "what": "Not a browser feature",
            "darwin": (None, "a browser has nothing to replace; use find"),
            "linux": (None, "a browser has nothing to replace; use find"),
            "win32": (None, "a browser has nothing to replace; use find"),
        },
        "paste_plain": {
            "what": "Paste into a web field without formatting",
            "darwin": ("shift+alt+super+v", ""),
            "linux": ("shift+ctrl+v", ""),
            "win32": ("shift+ctrl+v", ""),
        },
        "zoom_in": {
            "group": "app",
            "what": "Enlarge the page",
            "darwin": ("super+equal", ""),
            "linux": ("ctrl+equal", ""),
            "win32": ("ctrl+equal", ""),
        },
        "zoom_out": {
            "group": "app",
            "what": "Shrink the page",
            "darwin": ("super+minus", ""),
            "linux": ("ctrl+minus", ""),
            "win32": ("ctrl+minus", ""),
        },
        "zoom_reset": {
            "group": "app",
            "what": "Return the page to 100%",
            "darwin": ("super+0", ""),
            "linux": ("ctrl+0", ""),
            "win32": ("ctrl+0", ""),
        },
        "quit_app": {
            "what": "Quit the browser, closing every window",
            "darwin": ("super+q", "Chrome ships with Warn Before Quitting on, so "
                                  "one press only shows a hold-to-quit banner. "
                                  "Hold the combo with key_hold for ~1.5s, or "
                                  "use terminate_app"),
            "linux": ("shift+ctrl+q", ""),
            "win32": (None, "Chrome has no quit chord on Windows; close the "
                            "last window with alt+F4"),
        },
    },

    # Terminals are where a wrong chord costs the most: the ctrl+letter space
    # belongs to the shell and the running job, so the app's own bindings all
    # carry shift. The refusals here are as valuable as the chords.
    "terminal": {
        "copy": {
            "what": "Copy the selection out of the terminal",
            "darwin": ("super+c", ""),
            "linux": ("shift+ctrl+c", "a bare ctrl+c sends SIGINT and kills the "
                                      "running command"),
            "win32": ("shift+ctrl+c", "a bare ctrl+c interrupts the running "
                                      "command"),
        },
        "paste": {
            "what": "Paste into the terminal",
            "darwin": ("super+v", ""),
            "linux": ("shift+ctrl+v", ""),
            "win32": ("shift+ctrl+v", ""),
        },
        "new_tab": {
            "what": "Open another terminal tab",
            "darwin": ("super+t", ""),
            "linux": ("shift+ctrl+t", ""),
            "win32": ("shift+ctrl+t", ""),
        },
        "close_tab": {
            "what": "Close the terminal tab",
            "darwin": ("super+w", ""),
            "linux": ("shift+ctrl+w", "kills whatever is running in it"),
            "win32": ("shift+ctrl+w", "kills whatever is running in it"),
        },
        "next_tab": {
            "what": "Move to the next terminal tab",
            "darwin": ("ctrl+Tab", ""),
            "linux": ("ctrl+Page_Down", "GNOME Terminal does not bind ctrl+Tab"),
            "win32": ("ctrl+Tab", ""),
        },
        "prev_tab": {
            "what": "Move to the previous terminal tab",
            "darwin": ("shift+ctrl+Tab", ""),
            "linux": ("ctrl+Page_Up", "GNOME Terminal does not bind ctrl+Tab"),
            "win32": ("shift+ctrl+Tab", ""),
        },
        "new_window": {
            "group": "app",
            "what": "Open another terminal window",
            "darwin": ("super+n", ""),
            "linux": ("shift+ctrl+n", ""),
            "win32": ("shift+ctrl+n", ""),
        },
        "find": {
            "what": "Search the scrollback",
            "darwin": ("super+f", ""),
            "linux": ("shift+ctrl+f", ""),
            "win32": ("shift+ctrl+f", ""),
        },
        "interrupt": {
            "group": "app",
            "what": "Stop the command that is running",
            "darwin": ("ctrl+c", ""),
            "linux": ("ctrl+c", ""),
            "win32": ("ctrl+c", ""),
        },
        "clear_screen": {
            "group": "app",
            "what": "Clear the visible scrollback at the prompt",
            "darwin": ("ctrl+l", ""),
            "linux": ("ctrl+l", ""),
            "win32": ("ctrl+l", ""),
        },
        "address_bar": {
            "what": "Not a terminal feature",
            "darwin": (None, "a terminal has no address bar, and ctrl+l clears "
                             "the screen here"),
            "linux": (None, "a terminal has no address bar, and ctrl+l clears "
                            "the screen here"),
            "win32": (None, "a terminal has no address bar, and ctrl+l clears "
                            "the screen here"),
        },
        "save": {
            "what": "Not a terminal feature",
            "darwin": (None, "nothing to save; ctrl+s sends XOFF and freezes "
                             "the terminal until ctrl+q"),
            "linux": (None, "nothing to save; ctrl+s sends XOFF and freezes the "
                            "terminal until ctrl+q"),
            "win32": (None, "nothing to save; ctrl+s can freeze output"),
        },
        "undo": {
            "what": "Not a terminal feature",
            "darwin": (None, "ctrl+z suspends the foreground job, it does not "
                             "undo. Resume it with the shell's fg"),
            "linux": (None, "ctrl+z suspends the foreground job, it does not "
                            "undo. Resume it with the shell's fg"),
            "win32": (None, "no undo in a console"),
        },
        "delete_word": {
            "what": "Delete the word before the cursor at the prompt",
            "darwin": ("ctrl+w", "the shell line editor's word kill; it eats a "
                                 "whole whitespace-delimited word, which is more "
                                 "than an editor's delete_word would"),
            "linux": ("ctrl+w", "the shell line editor's word kill; it eats a "
                                "whole whitespace-delimited word, which is more "
                                "than an editor's delete_word would"),
            "win32": (None, "depends on the shell in the pane, and the default "
                            "PowerShell edit mode does not bind ctrl+w"),
        },
        "quit_app": {
            "what": "Quit the terminal",
            "darwin": ("super+q", "closes every window"),
            "linux": ("shift+ctrl+q", "GNOME Terminal calls this Close Window: "
                                      "it takes the current window and its jobs, "
                                      "other windows stay up. A bare ctrl+q "
                                      "resumes output after XOFF and quits "
                                      "nothing"),
            "win32": (None, "no quit chord; alt+F4 closes the window"),
        },
        "select_all": {
            "what": "Select the whole buffer",
            "darwin": ("super+a", ""),
            "linux": ("shift+ctrl+a", "a bare ctrl+a jumps to the start of the "
                                      "prompt line instead"),
            "win32": (None, "not bound by default in Windows Terminal; select "
                            "with the mouse"),
        },
    },

    # Finder / Nautilus / File Explorer. The renaming chord is the classic
    # cross-platform trap: Return renames on Linux and Windows but OPENS on
    # macOS, where Return is itself the rename key.
    "files": {
        "rename": {
            "group": "app",
            "what": "Rename the selected item",
            "darwin": ("Return", "Return renames here; it does not open"),
            "linux": ("F2", ""),
            "win32": ("F2", ""),
        },
        "open_item": {
            "group": "app",
            "what": "Open the selected item",
            "darwin": ("super+Down", "a bare Return would rename it"),
            "linux": ("Return", ""),
            "win32": ("Return", ""),
        },
        "new_folder": {
            "group": "app",
            "what": "Create a folder in the current directory",
            "darwin": ("shift+super+n", ""),
            "linux": ("shift+ctrl+n", ""),
            "win32": ("shift+ctrl+n", ""),
        },
        "go_up": {
            "group": "navigation",
            "what": "Go to the parent folder",
            "darwin": ("super+Up", ""),
            "linux": ("alt+Up", ""),
            "win32": ("alt+Up", ""),
        },
        "back": {
            "group": "navigation",
            "what": "Go back to the previous folder",
            "darwin": ("super+bracketleft", ""),
            "linux": ("alt+Left", ""),
            "win32": ("alt+Left", ""),
        },
        "delete_item": {
            "group": "app",
            "what": "Move the selection to the trash",
            "darwin": ("super+BackSpace", ""),
            "linux": ("Delete", ""),
            "win32": ("Delete", "shift+Delete would delete permanently, with no "
                                "Recycle Bin copy; do not send it"),
        },
        "show_hidden": {
            "group": "app",
            "what": "Toggle hidden files",
            "darwin": ("shift+super+period", ""),
            "linux": ("ctrl+h", ""),
            "win32": (None, "File Explorer binds no chord; toggle Hidden items "
                            "on the View ribbon"),
        },
        "properties": {
            "group": "app",
            "what": "Show info/properties for the selection",
            "darwin": ("super+i", ""),
            "linux": ("ctrl+i", ""),
            "win32": ("alt+Return", ""),
        },
        "address_bar": {
            "what": "Type a path to jump to",
            "darwin": ("shift+super+g", "opens the Go to Folder sheet"),
            "linux": ("ctrl+l", ""),
            "win32": ("ctrl+l", ""),
        },
        "new_window": {
            "group": "app",
            "what": "Open another file-manager window",
            "darwin": ("super+n", ""),
            "linux": ("ctrl+n", ""),
            "win32": ("ctrl+n", ""),
        },
    },
}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def detect_platform() -> str:
    """Which platform key this machine is, using the package's own detection.

    Delegates to `workman.platform.backend_name()` so a WORKMAN_BACKEND
    override moves the shortcut table and the key injection together — a table
    that disagreed with the backend actually pressing the keys would be worse
    than no table. A bad override is reported by every other tool already, so
    here it degrades to the host's real OS rather than raising inside a lookup.
    """
    try:
        return _BACKEND_TO_PLATFORM.get(backend_name(), "linux")
    except ValueError:
        if sys.platform == "darwin":
            return "darwin"
        if sys.platform.startswith("win"):
            return "win32"
        return "linux"


def groups() -> list[str]:
    """The group names, in survey order."""
    return list(GROUPS)


def apps() -> list[str]:
    """App layers that exist, canonical names only."""
    return sorted(_APPS)


def _norm(value: str | None, aliases: dict[str, str]) -> str | None:
    """Canonicalise a caller's spelling: case, spaces and dashes all forgiven."""
    if value is None:
        return None
    key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    return aliases.get(key, key)


def _entry(action: str, app: str | None) -> tuple[dict | None, str, str]:
    """Find an action, app layer first. Returns (entry, source, group)."""
    if app and app in _APPS:
        found = _APPS[app].get(action)
        if found is not None:
            group = found.get("group") or (_SYSTEM.get(action, {}).get("group")
                                           or "app")
            return found, "app", group
    found = _SYSTEM.get(action)
    if found is not None:
        return found, "system", found.get("group", "system")
    return None, "", ""


def _survey_actions(app: str | None) -> set[str]:
    """What `list_actions` should show: the system layer, plus one app's.

    Deliberately NOT the union of every app layer when no app is named — a
    survey listing `command_palette` while no app is selected would advertise
    an action that cannot resolve, which is exactly the false confidence this
    module exists to remove.
    """
    names = set(_SYSTEM)
    if app and app in _APPS:
        names |= set(_APPS[app])
    return names


def _suggestion_pool(app: str | None) -> set[str]:
    """Names a near-match may come from — every layer, so a miss can point at
    the app layer that would have answered."""
    names = set(_SYSTEM) | set(_ACTION_ALIASES)
    for layer_name, layer in _APPS.items():
        if app is None or layer_name == app:
            names |= set(layer)
    return names


def _layers_carrying(action: str) -> list[str]:
    """Which app layers define this action, for a miss that names the fix."""
    return sorted(name for name, layer in _APPS.items() if action in layer)


def resolve(action: str, app: str | None = None,
            platform: str | None = None) -> dict:
    """The chord for one action, ready to hand to `press_key`.

    Three shapes come back, and the caller should branch on `ok`:

    * a hit          — ok=True with `keys` in xdotool syntax
    * no equivalent  — ok=False, unsupported=True, keys=None, and a `note`
                       naming the route that does work
    * unknown action — ok=False with `suggestions` of near-matches

    Nothing raises; an unusable answer is still an answer the model can read.
    """
    requested = str(action)
    name = _norm(action, _ACTION_ALIASES)
    layer = _norm(app, _APP_ALIASES)
    where = _norm(platform, _PLATFORM_ALIASES) or detect_platform()

    out: dict[str, Any] = {"ok": False, "action": name, "keys": None,
                           "platform": where, "app": layer, "group": "",
                           "source": "", "note": ""}

    if where not in PLATFORMS:
        out["error"] = f"unknown platform {platform!r}"
        out["suggestions"] = list(PLATFORMS)
        return out
    if layer is not None and layer not in _APPS:
        # An unknown app is not fatal: the system layer still answers, and
        # saying so beats refusing a question that has a usable answer.
        out["note"] = (f"no shortcut layer for app {app!r}; answered from the "
                       f"system layer. Known layers: {', '.join(apps())}")
        layer = None
        out["app"] = None

    entry, source, group = _entry(name or "", layer)
    if entry is None:
        # Learned store fills the gap for apps the static table never saw.
        learned = _learned_hit(name or "", app=app, platform=where)
        if learned is not None:
            return learned
        out["error"] = f"no such action {requested!r}"
        pool = sorted(_suggestion_pool(layer) | _learned_actions(app, where))
        # Only genuine near-matches go in here. Padding the list with whatever
        # sorts first would dress unrelated actions up as suggestions, which is
        # the same false confidence a guessed chord would give.
        out["suggestions"] = difflib.get_close_matches(name or "", pool, n=5,
                                                       cutoff=0.5)
        elsewhere = _layers_carrying(name or "")
        out["note"] = (
            f"known in the {', '.join(elsewhere)} layer; pass app="
            f"'{elsewhere[0]}'" if elsewhere else
            "list_actions() shows every action; shortcut_learn(app) harvests "
            "menu chords for apps not in the static table")
        return out

    row = entry[where]
    keys, note = row[0], row[1]
    # A third element is optional and is the route to the tool that performs
    # this action where no chord can: shortcuts stays a lookup, but a caller
    # reading a keys=None answer gets something it can act on rather than a
    # dead end.
    route = row[2] if len(row) > 2 else None
    out.update({"group": group, "source": source, "what": entry.get("what", "")})
    if route:
        out["route"] = dict(route)
    note_prefix = out["note"]
    out["note"] = f"{note_prefix} {note}".strip() if note_prefix else note
    if keys is None:
        # Static says unsupported — a learned chord for this app still wins.
        learned = _learned_hit(name or "", app=app, platform=where)
        if learned is not None and learned.get("keys"):
            return learned
        out["unsupported"] = True
        out["error"] = f"{name} has no keyboard shortcut on {where}"
        if route:
            # Unsupported by keyboard is not unsupported. Say which tool does
            # it, so a caller that got here does not fall back to the mouse.
            out["error"] += (f"; {route['tool']}(action={route['action']!r}) "
                             f"performs it")
        return out
    out["ok"] = True
    out["keys"] = keys
    return out


def _learned_hit(action: str, app: str | None, platform: str) -> dict | None:
    try:
        from . import learned_shortcuts
    except Exception:
        return None
    row = learned_shortcuts.lookup(action, app=app, platform=platform)
    if not row or not row.get("keys"):
        return None
    return {
        "ok": True,
        "action": str(row.get("action") or action),
        "keys": row["keys"],
        "platform": platform,
        "app": row.get("app_key") or app,
        "group": "app",
        "source": "learned",
        "note": f"from {row.get('source') or 'learned'} store "
                f"({row.get('label') or row.get('action')})",
        "what": str(row.get("label") or ""),
        "menu_path": list(row.get("menu_path") or []),
        "learned": True,
    }


def _learned_actions(app: str | None, platform: str) -> set[str]:
    try:
        from . import learned_shortcuts
    except Exception:
        return set()
    return {str(r.get("action") or "") for r in learned_shortcuts.list_for(app, platform)
            if r.get("action")}


def lookup_many(actions: list[str], app: str | None = None,
                platform: str | None = None) -> list[dict]:
    """Resolve several actions in one call; each result carries its own `ok`."""
    return [resolve(name, app=app, platform=platform) for name in (actions or [])]


def list_actions(app: str | None = None, platform: str | None = None,
                 group: str | None = None) -> list[dict]:
    """Every action with its chord here — the survey an agent reads first.

    With `app` set the app layer is merged over the system layer, so the list
    is everything usable in that app rather than only its overrides. Sorted by
    group then action so a printed list reads as a reference card.
    """
    layer = _norm(app, _APP_ALIASES)
    if layer is not None and layer not in _APPS:
        layer = None
    wanted = _norm(group, {}) if group else None
    names = set(_survey_actions(layer)) | _learned_actions(app, _norm(platform, _PLATFORM_ALIASES) or detect_platform())
    rows = [resolve(name, app=app if app else layer, platform=platform)
            for name in names if name]
    # Drop pure misses so a learned survey stays usable.
    rows = [row for row in rows if row.get("ok") or row.get("unsupported")]
    if wanted:
        rows = [row for row in rows if row.get("group") == wanted]
    return sorted(rows, key=lambda row: (GROUPS.index(row["group"])
                                         if row.get("group") in GROUPS else 99,
                                         row.get("action") or ""))


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """`python -m workman.shortcuts <action> [--app X]` — smoke test from a shell."""
    parser = argparse.ArgumentParser(
        prog="python -m workman.shortcuts",
        description="Look up the native keyboard shortcut for an action.")
    parser.add_argument("action", nargs="?", help="e.g. fullscreen, copy, new_tab")
    parser.add_argument("--app", help="app layer: vscode, chrome, terminal, files")
    parser.add_argument("--platform",
                        help="darwin | linux | win32 (default: this machine)")
    parser.add_argument("--group", help="filter a --list by group")
    parser.add_argument("--list", action="store_true", help="list every action")
    args = parser.parse_args(argv)

    if args.list or not args.action:
        rows = list_actions(app=args.app, platform=args.platform, group=args.group)
        print(json.dumps(rows, indent=2))
        return 0
    result = resolve(args.action, app=args.app, platform=args.platform)
    print(json.dumps(result, indent=2))
    # A miss exits non-zero so a shell test fails loudly instead of reading as
    # a pass with an empty chord.
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
