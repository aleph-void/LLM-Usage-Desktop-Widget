"""Windows taskbar text: layout and placement logic, with Win32 and UI Automation faked.

TaskbarStrip.__init__ needs real Win32, so these build the object bare and set what each method uses.
"""

from __future__ import annotations

import ctypes
import types

import pytest

import usage_widget as w


class FakeFont:
    def __init__(self, per_char):
        self.per_char = per_char

    def measure(self, text):
        return len(text) * self.per_char


def make_strip(data=None, **attrs):
    strip = object.__new__(w.TaskbarStrip)
    strip.tray = types.SimpleNamespace(data=data or [], cfg={})
    strip.fonts = {"line": (FakeFont(7), FakeFont(8)), "stacked": (FakeFont(6), FakeFont(7))}
    strip.pad = 4
    strip.bg = "#1c1c1c"
    strip.uia = None
    strip.hwnd = 500
    strip.ctypes = ctypes
    strip.__dict__.update(attrs)
    return strip


# -- _parts --------------------------------------------------------------------

def test_parts():
    strip = make_strip([
        w.ProviderUsage("Claude", limits=[w.Limit("s", 99.4)]),
        w.ProviderUsage("Codex", limits=[w.Limit("s", 76)]),
        w.ProviderUsage("Bedrock", summary="$3.00"),
        w.ProviderUsage("Broken", error="x"),
        w.ProviderUsage("Empty"),
    ])
    assert strip._parts() == [("Claude", "99%", "critical"), ("Codex", "76%", "warning"),
                              ("Bedrock", "$3.00", "normal"), ("Broken", "–", "muted")]


def test_parts_switch_to_credits_left_once_the_plan_is_full():
    strip = make_strip([
        w.ProviderUsage("Claude", limits=[w.Limit("s", 100)], credits=w.Credits(used=45, cap=50, unit="$")),
        w.ProviderUsage("Codex", limits=[w.Limit("s", 100)], credits=w.Credits(left=42)),
    ])
    assert strip._parts() == [("Claude", "10% left", "critical"), ("Codex", "42 left", "normal")]


def test_parts_placeholder():
    assert make_strip()._parts() == [("LLM usage", "…", "muted")]


# -- _width / _choose ----------------------------------------------------------

PARTS = [("Claude", "99%", "critical"), ("Codex", "16%", "normal")]


def test_width():
    strip = make_strip()
    # line: (6*7 + 2 + 3*8) + (5*7 + 2 + 3*8) + 2*4*2
    assert strip._width(PARTS, "line") == 68 + 61 + 16
    # stacked: widest name + pad + widest value + 2*pad
    assert strip._width(PARTS, "stacked") == 6 * 6 + 4 + 3 * 7 + 8
    assert strip._width(w.TaskbarStrip.LOGO, "line") == 4 * 4 + 2 * 4


def test_choose_prefers_one_line_next_to_tray():
    strip = make_strip()
    assert strip._choose(PARTS, [(800, 1000), (0, 300)], 5) == ("line", 995)


def test_choose_stacks_when_crowded():
    strip = make_strip()
    assert strip._choose(PARTS, [(900, 1000)], 5) == ("stacked", 995)


def test_choose_moves_to_another_gap():
    strip = make_strip()
    assert strip._choose(PARTS, [(950, 1000), (0, 400)], 5) == ("line", 395)


def test_choose_gives_up_when_full():
    assert make_strip()._choose(PARTS, [(0, 50), (60, 100)], 5) is None


# -- _free_spans ---------------------------------------------------------------

class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class Element:
    def __init__(self, rect, cls="Taskbar.TaskListButtonAutomationPeer"):
        self.CurrentBoundingRectangle = Rect(*rect)
        self.CurrentClassName = cls


def fake_uia(elements, fail=False):
    class Found:
        Length = len(elements)

        def GetElement(self, i):
            return elements[i]

    class Root:
        def FindAll(self, scope, cond):
            if fail:
                raise OSError("COM error")
            return Found()

    automation = types.SimpleNamespace(ElementFromHandle=lambda hwnd: Root(), CreateTrueCondition=lambda: None)
    return types.SimpleNamespace(TreeScope_Descendants=4), automation


TASKBAR = (0, 1040, 1920, 1088)  # 48 px tall, full width


def test_free_spans_without_uia():
    assert make_strip()._free_spans(1, TASKBAR, 1700) == [(0, 1700)]


def test_free_spans_uia_failure_falls_back():
    strip = make_strip(uia=fake_uia([], fail=True))
    assert strip._free_spans(1, TASKBAR, 1700) == [(0, 1700)]


def test_free_spans_between_buttons():
    elements = [
        Element((800, 1040, 850, 1088)),
        Element((840, 1040, 900, 1088)),  # overlaps the previous one
        Element((300, 1044, 400, 1084)),  # search box
        Element((0, 1040, 1920, 1088), "Shell_TrayWnd"),  # the whole taskbar: too wide
        Element((500, 1040, 520, 1050)),  # too short to be a button
        Element((1000, 1040, 1100, 1088), "MSTaskListWClass"),  # legacy rectangle, ignored
        Element((1750, 1040, 1800, 1088)),  # inside the tray
        Element((600, 1040, 600, 1088)),  # zero width
    ]
    strip = make_strip(uia=fake_uia(elements))
    assert strip._free_spans(1, TASKBAR, 1700) == [(900, 1700), (400, 800), (0, 300)]


def test_free_spans_buttons_reaching_the_tray():
    strip = make_strip(uia=fake_uia([Element((0, 1040, 1699, 1088)), Element((100, 1040, 1720, 1088))]))
    assert strip._free_spans(1, TASKBAR, 1700) == []


# -- _placement / _fullscreen_app ----------------------------------------------

class FakeUser32:
    SHELL_PID = 1234  # explorer.exe, which owns the taskbar (hwnd 1)

    def __init__(self, visible=True, screen=(1920, 1080), foreground=0, fg_class="Notepad", fg_pid=99):
        self.visible, self.screen, self.foreground, self.fg_class = visible, screen, foreground, fg_class
        self.fg_pid = fg_pid
        self.calls = []

    def FindWindowW(self, cls, name):
        return 1 if cls == "Shell_TrayWnd" else 0

    def FindWindowExW(self, parent, after, cls, name):
        return 2 if cls == "TrayNotifyWnd" else 0

    def IsWindowVisible(self, hwnd):
        return self.visible

    def GetSystemMetrics(self, index):
        return self.screen[index]

    def GetForegroundWindow(self):
        return self.foreground

    def GetClassNameW(self, hwnd, buf, size):
        buf.value = self.fg_class
        return len(self.fg_class)

    def GetWindowThreadProcessId(self, hwnd, pid):
        if pid is not None:
            pid.value = self.SHELL_PID if hwnd == 1 else self.fg_pid
        return 42  # the thread

    def AttachThreadInput(self, me, other, attach):
        self.calls.append(("attach" if attach else "detach", other))
        return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("foreground", hwnd))
        return True


def placement_strip(rects, **user32):
    strip = make_strip(user32=FakeUser32(**user32))
    strip._rect = lambda hwnd: rects.get(hwnd)
    return strip


def test_placement_normal():
    strip = placement_strip({1: TASKBAR, 2: (1650, 1040, 1920, 1088)})
    assert strip._placement() == (1, TASKBAR, 1650)


def test_placement_without_tray_window():
    strip = placement_strip({1: TASKBAR})
    assert strip._placement() == (1, TASKBAR, 1920 - 300)


@pytest.mark.parametrize("rects, user32", [
    ({}, {}),  # no taskbar
    ({1: TASKBAR}, {"visible": False}),
    ({1: (0, 0, 48, 1080)}, {}),  # vertical taskbar
    ({1: (0, 1078, 1920, 1126)}, {}),  # auto-hidden below the screen
    ({1: (0, -46, 1920, 2)}, {}),  # auto-hidden above the screen
    ({1: TASKBAR, 7: (0, 0, 1920, 1080)}, {"foreground": 7}),  # full-screen app
])
def test_placement_hidden(rects, user32):
    assert placement_strip(rects, **user32)._placement() is None


@pytest.mark.parametrize("foreground, rect, cls, pid, expected", [
    (0, None, "X", 99, False),  # nothing in front
    (1, (0, 0, 1920, 1080), "X", 99, False),  # the taskbar itself
    (500, (0, 0, 1920, 1080), "X", 99, False),  # the strip itself
    (7, (0, 0, 1920, 1080), "Progman", 99, False),  # the desktop
    (7, (0, 0, 1920, 1080), "WorkerW", 99, False),
    # The Windows 11 shell's screen-sized flyout host, foreground after the tray menu closes.
    (7, (0, 0, 1920, 1080), "XamlExplorerHostIslandWindow", FakeUser32.SHELL_PID, False),
    (7, (100, 100, 800, 600), "X", 99, False),  # an ordinary window
    (7, (-8, -8, 1928, 1088), "X", 99, True),  # maximised past the edges counts as full screen
    (7, (0, 0, 1920, 1080), "X", 99, True),  # a full-screen app from another process
    (7, None, "X", 99, False),  # window vanished
])
def test_fullscreen_app(foreground, rect, cls, pid, expected):
    strip = placement_strip({7: rect} if rect else {}, foreground=foreground, fg_class=cls, fg_pid=pid)
    assert strip._fullscreen_app(1) is expected


# -- take_focus_from_taskbar -----------------------------------------------------

def test_take_focus_from_taskbar():
    # The shell has the focus (as after dismissing the menu by clicking on the taskbar):
    # activate the strip while sharing the shell thread's input queue, then let go of it.
    strip = placement_strip({}, foreground=7, fg_pid=FakeUser32.SHELL_PID)
    assert strip.take_focus_from_taskbar() is True
    assert strip.user32.calls == [("attach", 42), ("foreground", 500), ("detach", 42)]


@pytest.mark.parametrize("user32", [
    {"foreground": 0},  # nothing has the focus
    {"foreground": 7, "fg_pid": 99},  # an app has it
    {"foreground": 500, "fg_pid": 99},  # the strip itself
])
def test_take_focus_from_taskbar_leaves_others_alone(user32):
    strip = placement_strip({}, **user32)
    assert strip.take_focus_from_taskbar() is False
    assert strip.user32.calls == []


# -- _taskbar_color --------------------------------------------------------------

def test_taskbar_color():
    released = []
    strip = make_strip(
        user32=types.SimpleNamespace(GetDC=lambda hwnd: "dc", ReleaseDC=lambda hwnd, dc: released.append(dc)),
        gdi32=types.SimpleNamespace(GetPixel=lambda dc, x, y: 0x00332211),  # COLORREF is 0x00BBGGRR
    )
    assert strip._taskbar_color(10, 20) == "#112233"
    assert released == ["dc"]


def test_taskbar_color_invalid():
    strip = make_strip(user32=types.SimpleNamespace(GetDC=lambda h: 1, ReleaseDC=lambda h, d: 1),
                       gdi32=types.SimpleNamespace(GetPixel=lambda dc, x, y: 0xFFFFFFFF))
    assert strip._taskbar_color(0, 0) == "#1c1c1c"


# -- _tick / _update -----------------------------------------------------------

class FakeRoot:
    def __init__(self, state="withdrawn"):
        self._state = state
        self.after_calls = []
        self.destroyed = False
        self.geometries = []

    def after(self, ms, fn):
        self.after_calls.append(ms)

    def destroy(self):
        self.destroyed = True

    def state(self):
        return self._state

    def withdraw(self):
        self._state = "withdrawn"

    def deiconify(self):
        self._state = "normal"

    def update_idletasks(self):
        pass

    def geometry(self, spec):
        self.geometries.append(spec)


def test_tick_survives_errors_and_reschedules():
    strip = make_strip(root=FakeRoot(), closing=w.threading.Event())
    strip._update = lambda: (_ for _ in ()).throw(OSError("win32 hiccup"))
    strip._tick()
    assert strip.root.after_calls == [w.TaskbarStrip.TICK_MS]


def test_close_stops_the_loop():
    strip = make_strip(root=FakeRoot(), closing=w.threading.Event())
    strip.close()
    strip._tick()
    assert strip.root.destroyed
    assert strip.root.after_calls == []


def test_update_hidden_when_turned_off():
    strip = make_strip(root=FakeRoot("normal"), placed=(1, 2, 3, 4))
    strip.tray.cfg["taskbar_text"] = False
    strip._placement = lambda: pytest.fail("should not look for the taskbar")
    strip._update()
    assert strip.root.state() == "withdrawn"
    assert strip.placed is None


def test_update_shows_the_logo_when_collapsed():
    strip = make_strip(
        [w.ProviderUsage("Claude", limits=[w.Limit("s", 50)])],
        root=FakeRoot(), placed=None, spans=None, spans_at=0.0,
        inner=types.SimpleNamespace(winfo_reqwidth=lambda: 24),
        user32=types.SimpleNamespace(SetWindowPos=lambda *a: None),
    )
    strip.tray.cfg["collapsed"] = True
    strip._placement = lambda: (1, TASKBAR, 1650)
    drawn = []
    strip._draw = lambda bg, parts, layout: drawn.append((bg, parts, layout))
    strip._taskbar_color = lambda x, y: "#202020"
    strip._update()
    assert drawn == [("#202020", w.TaskbarStrip.LOGO, "line")]
    assert strip.root.state() == "normal"  # still on the taskbar



def test_update_collapsed_hides_when_taskbar_full():
    strip = make_strip(root=FakeRoot("normal"), placed=(1, 2, 3, 4), spans=None, spans_at=0.0)
    strip.tray.cfg["collapsed"] = True
    strip._placement = lambda: (1, TASKBAR, 1650)
    strip._free_spans = lambda *a: [(0, 10)]
    strip._update()
    assert strip.root.state() == "withdrawn"


def test_update_collapsed_hidden_when_turned_off():
    strip = make_strip(root=FakeRoot("normal"), placed=(1, 2, 3, 4))
    strip.tray.cfg.update(collapsed=True, taskbar_text=False)
    strip._placement = lambda: pytest.fail("should not look for the taskbar")
    strip._update()
    assert strip.root.state() == "withdrawn"

def test_update_places_the_strip():
    positions = []
    strip = make_strip(
        [w.ProviderUsage("Claude", limits=[w.Limit("s", 50)])],
        root=FakeRoot(), placed=None, spans=None, spans_at=0.0,
        inner=types.SimpleNamespace(winfo_reqwidth=lambda: 120),
        user32=types.SimpleNamespace(SetWindowPos=lambda *a: positions.append(a)),
    )
    strip._placement = lambda: (1, TASKBAR, 1650)
    drawn = []
    strip._draw = lambda bg, parts, layout: drawn.append((bg, parts, layout))
    strip._taskbar_color = lambda x, y: "#202020"
    strip._update()
    margin = 48 // 8
    assert drawn == [("#202020", [("Claude", "50%", "normal")], "line")]
    assert strip.root.geometries == [f"120x{48 - 2 * margin}+{1650 - margin - 120}+{1040 + margin}"]
    assert strip.root.state() == "normal"
    assert positions == [(500, -1, 1650 - margin - 120, 1040 + margin, 120, 48 - 2 * margin, 0x0010)]
    strip._update()  # same place: geometry not set again
    assert len(strip.root.geometries) == 1


def test_update_hides_when_taskbar_full():
    strip = make_strip([w.ProviderUsage("Claude", limits=[w.Limit("s", 50)])], root=FakeRoot("normal"),
                       placed=(1, 2, 3, 4), spans=None, spans_at=0.0)
    strip._placement = lambda: (1, TASKBAR, 1650)
    strip._free_spans = lambda *a: [(0, 10)]
    strip._update()
    assert strip.root.state() == "withdrawn"
