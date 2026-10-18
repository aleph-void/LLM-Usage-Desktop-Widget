#!/usr/bin/env python3
"""Desktop widget showing Claude and Codex (ChatGPT) plan usage limits.

Reads the OAuth logins that Claude Code and the Codex CLI already store on
this machine, so there is nothing to configure: if `claude` and `codex` are
logged in, the widget works. Credentials are only ever read, never written,
and are sent only to the provider that issued them.

Appears where the system status icons live: the Linux top bar (AppIndicator,
via the system's PyGObject), the macOS menu bar and the Windows notification
area (pystray + Pillow). `--widget` shows a floating window that needs only
the standard library.

    python3 usage_widget.py            # tray / top bar / menu bar
    python3 usage_widget.py --widget   # floating window instead
    python3 usage_widget.py --once     # print usage to the terminal and exit
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"

APP_NAME = "llm-usage-widget"
USER_AGENT = f"llm-usage-widget/{__version__}"
DEFAULT_INTERVAL = 180  # seconds between polls; the endpoints are not meant to be hammered
MIN_INTERVAL = 60


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Limit:
    label: str
    percent: float
    resets_at: datetime | None = None
    severity: str = ""  # "normal" | "warning" | "critical"; derived from percent if empty

    def level(self) -> str:
        if self.severity in ("warning", "critical"):
            return self.severity
        if self.percent >= 90:
            return "critical"
        if self.percent >= 75:
            return "warning"
        return "normal"


@dataclass
class ProviderUsage:
    name: str
    plan: str = ""
    limits: list[Limit] = field(default_factory=list)
    error: str = ""
    fetched_at: float = 0.0


class AuthError(Exception):
    """Login missing or expired; the user has to act in the provider's CLI."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def http_json(url: str, headers: dict[str, str], timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(f"HTTP {e.code}: login rejected") from e
        raise RuntimeError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network: {e.reason}") from e


def parse_time(value) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None


def format_remaining(when: datetime | None) -> str:
    if when is None:
        return ""
    secs = int((when - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "resetting"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def window_label(seconds) -> str:
    if not seconds:
        return "Limit"
    seconds = int(seconds)
    if seconds == 604800:
        return "Weekly"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


# --------------------------------------------------------------------------
# Claude (claude.ai subscription via Claude Code's OAuth login)
# --------------------------------------------------------------------------

def _claude_credentials() -> dict:
    sources = []
    if sys.platform == "darwin":
        # Claude Code keeps its login in the macOS Keychain.
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                sources.append(json.loads(out.stdout))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    path = cfg / ".credentials.json"
    if path.exists():
        try:
            sources.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    for data in sources:
        if isinstance(data, dict) and data.get("claudeAiOauth", {}).get("accessToken"):
            return data["claudeAiOauth"]
    raise AuthError("not logged in — run `claude` and /login")


def fetch_claude() -> ProviderUsage:
    creds = _claude_credentials()
    expires = creds.get("expiresAt")
    if expires and expires / 1000 < time.time():
        raise AuthError("login expired — open Claude Code to refresh it")
    data = http_json(
        "https://api.anthropic.com/api/oauth/usage",
        {"Authorization": f"Bearer {creds['accessToken']}", "anthropic-beta": "oauth-2025-04-20"},
    )
    usage = ProviderUsage("Claude", plan=str(creds.get("subscriptionType") or ""))

    if isinstance(data.get("limits"), list) and data["limits"]:
        names = {"session": "Session (5h)", "weekly_all": "Weekly"}
        for item in data["limits"]:
            kind = item.get("kind", "")
            label = names.get(kind) or kind.replace("_", " ").capitalize()
            scope = item.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name")
            surface = scope.get("surface")
            if kind == "weekly_scoped" or model or surface:
                label = "Weekly · " + str(model or surface or "scoped")
            usage.limits.append(Limit(label, float(item.get("percent") or 0),
                                      parse_time(item.get("resets_at")), item.get("severity") or ""))
    else:
        # Older response shape.
        for key, label in (("five_hour", "Session (5h)"), ("seven_day", "Weekly"),
                           ("seven_day_opus", "Weekly · Opus"), ("seven_day_sonnet", "Weekly · Sonnet")):
            item = data.get(key)
            if isinstance(item, dict) and item.get("utilization") is not None:
                usage.limits.append(Limit(label, float(item["utilization"]), parse_time(item.get("resets_at"))))

    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("used_credits"):
        cap = extra.get("monthly_limit")
        pct = float(extra.get("utilization") or 0)
        label = f"Extra usage ${extra['used_credits']:.2f}" + (f" / ${cap:.0f}" if cap else "")
        usage.limits.append(Limit(label, pct))
    return usage


# --------------------------------------------------------------------------
# Codex (ChatGPT subscription via the Codex CLI's login)
# --------------------------------------------------------------------------

def _codex_tokens() -> dict:
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AuthError("not logged in — run `codex login`") from None
    except (OSError, json.JSONDecodeError) as e:
        raise AuthError(f"cannot read {path}: {e}") from None
    tokens = data.get("tokens") or {}
    if not tokens.get("access_token"):
        if data.get("OPENAI_API_KEY"):
            raise AuthError("API-key login has no plan limits — use `codex login` with ChatGPT")
        raise AuthError("not logged in — run `codex login`")
    return tokens


def _codex_windows(prefix: str, rl: dict | None, out: list[Limit]) -> None:
    if not isinstance(rl, dict):
        return
    reached = rl.get("limit_reached")
    for key in ("primary_window", "secondary_window"):
        win = rl.get(key)
        if not isinstance(win, dict):
            continue
        label = window_label(win.get("limit_window_seconds"))
        if prefix:
            label = f"{prefix} · {label}"
        pct = float(win.get("used_percent") or 0)
        out.append(Limit(label, pct, parse_time(win.get("reset_at")),
                         "critical" if reached and pct >= 100 else ""))


def fetch_codex() -> ProviderUsage:
    tokens = _codex_tokens()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    data = http_json("https://chatgpt.com/backend-api/wham/usage", headers)
    usage = ProviderUsage("Codex", plan=str(data.get("plan_type") or ""))
    _codex_windows("", data.get("rate_limit"), usage.limits)
    _codex_windows("Code review", data.get("code_review_rate_limit"), usage.limits)
    extra = data.get("additional_rate_limits")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, dict):
                name = item.get("limit_name") or item.get("metered_feature") or item.get("name") or "Other"
                _codex_windows(str(name), item.get("rate_limit", item), usage.limits)
    credits = data.get("credits") or {}
    if credits.get("has_credits") and not credits.get("unlimited"):
        usage.limits.append(Limit(f"Credits: {credits.get('balance', '0')}", 0))
    return usage


PROVIDERS = (("Claude", fetch_claude), ("Codex", fetch_codex))


def fetch_all() -> list[ProviderUsage]:
    results = []
    for name, fn in PROVIDERS:
        try:
            u = fn()
        except AuthError as e:
            u = ProviderUsage(name, error=str(e))
        except Exception as e:  # never let one provider break the widget
            u = ProviderUsage(name, error=str(e) or type(e).__name__)
        u.fetched_at = time.time()
        results.append(u)
    return results


# --------------------------------------------------------------------------
# Terminal output
# --------------------------------------------------------------------------

def print_once() -> int:
    for u in fetch_all():
        print(f"{u.name}" + (f" ({u.plan})" if u.plan else ""))
        if u.error:
            print(f"  ! {u.error}")
        for lim in u.limits:
            bar = "#" * round(lim.percent / 5)
            reset = format_remaining(lim.resets_at)
            print(f"  {lim.label:<24} {lim.percent:5.0f}%  [{bar:<20}]  {('resets ' + reset) if reset else ''}")
    return 0


# --------------------------------------------------------------------------
# Widget
# --------------------------------------------------------------------------

THEME = {
    "bg": "#1c1d21",
    "panel": "#26272d",
    "fg": "#e8e8ea",
    "muted": "#9a9ca5",
    "track": "#3a3c44",
    "normal": "#4fa3ff",
    "warning": "#f0b429",
    "critical": "#ef5350",
    "error": "#ff8a80",
    "claude": "#d97757",
    "codex": "#10a37f",
}
BAR_WIDTH = 210
BAR_HEIGHT = 6


class Widget:
    def __init__(self, interval: int, framed: bool):
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.interval = max(MIN_INTERVAL, interval)
        self.config_path = config_dir() / "config.json"
        self.cfg = self._load_config()
        self.data: list[ProviderUsage] = []
        self.fetching = False

        root = self.root = tk.Tk()
        root.title("LLM Usage")
        root.configure(bg=THEME["bg"])
        root.resizable(False, False)
        if not framed:
            root.overrideredirect(True)
        root.attributes("-topmost", bool(self.cfg.get("topmost", True)))
        try:
            root.attributes("-alpha", 0.95)
        except tk.TclError:
            pass
        if "x" in self.cfg and "y" in self.cfg:
            root.geometry(f"+{int(self.cfg['x'])}+{int(self.cfg['y'])}")
        else:
            root.geometry(f"+{root.winfo_screenwidth() - 320}+60")

        base = tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_title = tkfont.Font(family=base, size=10, weight="bold")
        self.f_label = tkfont.Font(family=base, size=9)
        self.f_small = tkfont.Font(family=base, size=8)

        self.header = tk.Frame(root, bg=THEME["bg"])
        self.header.pack(fill="x", padx=10, pady=(8, 2))
        self.status = tk.Label(self.header, text="Loading…", bg=THEME["bg"], fg=THEME["muted"], font=self.f_small)
        self.status.pack(side="left")
        for text, cmd in (("✕", self.quit), ("⟳", self.refresh)):
            b = tk.Label(self.header, text=text, bg=THEME["bg"], fg=THEME["muted"], font=self.f_label, cursor="hand2")
            b.pack(side="right", padx=(6, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())

        self.body = tk.Frame(root, bg=THEME["bg"])
        self.body.pack(fill="both", padx=10, pady=(0, 10))

        self.menu = tk.Menu(root, tearoff=0)
        self.topmost_var = tk.BooleanVar(value=bool(self.cfg.get("topmost", True)))
        self.menu.add_command(label="Refresh now", command=self.refresh)
        self.menu.add_checkbutton(label="Always on top", variable=self.topmost_var, command=self._toggle_topmost)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        self._bind_drag(root)
        self.refresh()
        self._tick()

    # -- config --------------------------------------------------------------

    def _load_config(self) -> dict:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_config(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    # -- window behaviour -----------------------------------------------------

    def _bind_drag(self, widget) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start, add="+")
        widget.bind("<B1-Motion>", self._drag_move, add="+")
        widget.bind("<ButtonRelease-1>", self._drag_end, add="+")
        widget.bind("<Button-3>", self._popup, add="+")
        widget.bind("<Button-2>", self._popup, add="+")  # macOS secondary click
        for child in widget.winfo_children():
            if not isinstance(child, self.tk.Menu):
                self._bind_drag(child)

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if getattr(self, "_drag", None):
            dx, dy = self._drag
            self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _drag_end(self, _e):
        if getattr(self, "_drag", None):
            self._drag = None
            self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
            self._save_config()

    def _popup(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_topmost(self):
        self.cfg["topmost"] = self.topmost_var.get()
        self.root.attributes("-topmost", self.cfg["topmost"])
        self._save_config()

    def quit(self):
        self.root.destroy()

    # -- data ----------------------------------------------------------------

    def refresh(self):
        if self.fetching:
            return
        self.fetching = True
        self.status.config(text="Updating…")

        def work():
            results = fetch_all()
            self.root.after(0, lambda: self._apply(results))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, results: list[ProviderUsage]):
        self.fetching = False
        previous = {u.name: u for u in self.data}
        for u in results:
            # Keep the last good numbers on transient failures, flagged as stale.
            old = previous.get(u.name)
            if u.error and not u.limits and old and old.limits:
                u.limits, u.plan = old.limits, old.plan
        self.data = results
        self.render()

    def _tick(self):
        # Poll on the configured interval; redraw countdowns every 30 s in between.
        now = time.time()
        last = max((u.fetched_at for u in self.data), default=0)
        if not self.fetching and now - last >= self.interval:
            self.refresh()
        elif self.data:
            self.render()
        self.root.after(30_000 if self.data else 1_000, self._tick)

    # -- drawing -------------------------------------------------------------

    def render(self):
        tk = self.tk
        for child in self.body.winfo_children():
            child.destroy()

        last = max((u.fetched_at for u in self.data), default=0)
        self.status.config(text="Updated " + time.strftime("%H:%M", time.localtime(last)) if last else "")

        for i, u in enumerate(self.data):
            panel = tk.Frame(self.body, bg=THEME["panel"], padx=10, pady=8)
            panel.pack(fill="x", pady=(6 if i else 2, 0))

            head = tk.Frame(panel, bg=THEME["panel"])
            head.pack(fill="x")
            tk.Label(head, text="●", fg=THEME[u.name.lower()] if u.name.lower() in THEME else THEME["fg"],
                     bg=THEME["panel"], font=self.f_label).pack(side="left")
            tk.Label(head, text=u.name, fg=THEME["fg"], bg=THEME["panel"], font=self.f_title).pack(side="left", padx=(4, 0))
            if u.plan:
                tk.Label(head, text=u.plan.capitalize(), fg=THEME["muted"], bg=THEME["panel"],
                         font=self.f_small).pack(side="right")

            if u.error:
                tk.Label(panel, text=("⚠ stale — " if u.limits else "⚠ ") + u.error, fg=THEME["error"],
                         bg=THEME["panel"], font=self.f_small, wraplength=BAR_WIDTH + 40,
                         justify="left").pack(anchor="w", pady=(4, 0))

            for lim in u.limits:
                self._draw_limit(panel, lim)

        self._bind_drag(self.body)

    def _draw_limit(self, parent, lim: Limit):
        tk = self.tk
        row = tk.Frame(parent, bg=THEME["panel"])
        row.pack(fill="x", pady=(6, 0))
        top = tk.Frame(row, bg=THEME["panel"])
        top.pack(fill="x")
        tk.Label(top, text=lim.label, fg=THEME["fg"], bg=THEME["panel"], font=self.f_label).pack(side="left")
        color = THEME[lim.level()]
        tk.Label(top, text=f"{lim.percent:.0f}%", fg=color if lim.level() != "normal" else THEME["fg"],
                 bg=THEME["panel"], font=self.f_label).pack(side="right")

        bar = tk.Canvas(row, width=BAR_WIDTH, height=BAR_HEIGHT, bg=THEME["panel"], highlightthickness=0, bd=0)
        bar.pack(fill="x", pady=(2, 0))
        bar.create_rectangle(0, 0, BAR_WIDTH, BAR_HEIGHT, fill=THEME["track"], width=0)
        filled = max(0, min(100, lim.percent)) / 100 * BAR_WIDTH
        if filled:
            bar.create_rectangle(0, 0, filled, BAR_HEIGHT, fill=color, width=0)

        reset = format_remaining(lim.resets_at)
        if reset:
            local = lim.resets_at.astimezone().strftime("%a %H:%M")
            tk.Label(row, text=f"resets in {reset}  ·  {local}", fg=THEME["muted"], bg=THEME["panel"],
                     font=self.f_small).pack(anchor="w")

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
# Linux top-bar indicator (AppIndicator / StatusNotifierItem)
# --------------------------------------------------------------------------

USAGE_PAGES = {
    "Claude": "https://claude.ai/settings/usage",
    "Codex": "https://chatgpt.com/codex/settings/usage",
}

# Three rising bars, drawn white like the other top-bar icons; tinted when a limit runs high.
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
<rect x="3" y="12" width="4" height="7" rx="1" fill="{c1}"/>
<rect x="9" y="8" width="4" height="11" rx="1" fill="{c2}"/>
<rect x="15" y="3" width="4" height="16" rx="1" fill="{c3}"/>
</svg>"""
ICON_COLORS = {
    "normal": ("#ffffff", "#ffffff", "#ffffff"),
    "warning": ("#ffffff", "#ffffff", THEME["warning"]),
    "critical": ("#ffffff", THEME["critical"], THEME["critical"]),
    "error": ("#ffffff", "#ffffff80", "#ffffff40"),
}


def load_appindicator():
    """Return the AppIndicator module, or None if PyGObject/libappindicator are missing."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
    except (ImportError, ValueError):
        return None
    for name in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(name, "0.1")
            return __import__("gi.repository", fromlist=[name]).__dict__[name]
        except (ImportError, ValueError, KeyError):
            continue
    return None


class Tray:
    def __init__(self, interval: int, appindicator):
        from gi.repository import GLib, Gtk

        self.GLib, self.Gtk, self.AI = GLib, Gtk, appindicator
        self.interval = max(MIN_INTERVAL, interval)
        self.data: list[ProviderUsage] = []
        self.fetching = False
        self.last_fetch = 0.0

        # Icons are referenced by name from our own theme dir so that switching
        # between them actually repaints (AppIndicator caches by path).
        self.icon_dir = config_dir() / "icons"
        self.icon_dir.mkdir(parents=True, exist_ok=True)
        for level, (c1, c2, c3) in ICON_COLORS.items():
            (self.icon_dir / f"llm-usage-{level}.svg").write_text(ICON_SVG.format(c1=c1, c2=c2, c3=c3))

        AI = appindicator
        self.ind = AI.Indicator.new(APP_NAME, "llm-usage-normal", AI.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_icon_theme_path(str(self.icon_dir))
        self.ind.set_status(AI.IndicatorStatus.ACTIVE)
        self.ind.set_title("LLM Usage")
        self.ind.set_label("…", "")
        self.render()

        self.refresh()
        GLib.timeout_add_seconds(30, self._tick)

    def refresh(self, *_):
        if self.fetching:
            return
        self.fetching = True

        def work():
            results = fetch_all()
            self.GLib.idle_add(self._apply, results)

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, results: list[ProviderUsage]):
        self.fetching = False
        self.last_fetch = time.time()
        previous = {u.name: u for u in self.data}
        for u in results:
            old = previous.get(u.name)
            if u.error and not u.limits and old and old.limits:
                u.limits, u.plan = old.limits, old.plan
        self.data = results
        self.render()
        return False

    def _tick(self):
        if not self.fetching and time.time() - self.last_fetch >= self.interval:
            self.refresh()
        else:
            self.render()  # keep the "resets in" countdowns current
        return True

    def render(self):
        Gtk = self.Gtk
        # Top bar: the fullest limit of each provider, e.g. "Claude 99%  Codex 13%".
        parts, worst = [], "normal"
        rank = {"normal": 0, "warning": 1, "critical": 2}
        for u in self.data:
            if u.limits:
                top = max(u.limits, key=lambda l: l.percent)
                parts.append(f"{u.name} {top.percent:.0f}%")
                if rank[top.level()] > rank[worst]:
                    worst = top.level()
            elif u.error:
                parts.append(f"{u.name} –")
        if self.data and not any(u.limits for u in self.data):
            worst = "error"
        self.ind.set_label("  ".join(parts) if parts else "…", "")
        self.ind.set_icon_full(f"llm-usage-{worst}", "LLM usage")

        menu = Gtk.Menu()

        def info(text):
            item = Gtk.MenuItem(label=text)
            item.set_sensitive(False)
            menu.append(item)

        if not self.data:
            info("Loading…")
        for i, u in enumerate(self.data):
            if i:
                menu.append(Gtk.SeparatorMenuItem())
            info(f"{u.name}" + (f"  ({u.plan.capitalize()})" if u.plan else ""))
            if u.error:
                info(("⚠ stale — " if u.limits else "⚠ ") + u.error)
            for lim in u.limits:
                filled = round(max(0, min(100, lim.percent)) / 10)
                bar = "█" * filled + "░" * (10 - filled)
                mark = {"warning": "  ▲", "critical": "  ●"}.get(lim.level(), "")
                info(f"    {lim.label}:  {lim.percent:.0f}%{mark}")
                reset = format_remaining(lim.resets_at)
                tail = f"  resets in {reset}" if reset else ""
                info(f"    {bar}{tail}")

        menu.append(Gtk.SeparatorMenuItem())
        if self.last_fetch:
            info("Updated " + time.strftime("%H:%M", time.localtime(self.last_fetch)))
        refresh = Gtk.MenuItem(label="Refresh now")
        refresh.connect("activate", self.refresh)
        menu.append(refresh)
        for name, url in USAGE_PAGES.items():
            item = Gtk.MenuItem(label=f"Open {name} usage page")
            item.connect("activate", lambda _w, u=url: Gtk.show_uri_on_window(None, u, Gtk.get_current_event_time()))
            menu.append(item)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)
        menu.show_all()
        self.ind.set_menu(menu)

    def run(self):
        import signal
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        self.Gtk.main()


# --------------------------------------------------------------------------
# Windows notification area / macOS menu bar (pystray + Pillow)
# --------------------------------------------------------------------------

def top_limit(u: ProviderUsage) -> Limit | None:
    return max(u.limits, key=lambda l: l.percent) if u.limits else None


def summary_text(data: list[ProviderUsage]) -> str:
    """e.g. "Claude 99%  Codex 13%" — the fullest limit of each provider."""
    parts = []
    for u in data:
        top = top_limit(u)
        if top:
            parts.append(f"{u.name} {top.percent:.0f}%")
        elif u.error:
            parts.append(f"{u.name} –")
    return "  ".join(parts)


def gauge_image(data: list[ProviderUsage], size: int = 64):
    """One vertical gauge per provider, filled to its fullest limit.

    Windows tray icons cannot show text, so the icon itself carries the numbers.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    names = [u.name for u in data] or [name for name, _ in PROVIDERS]
    by_name = {u.name: u for u in data}
    gap = size // 10
    width = (size - gap * (len(names) + 1)) // len(names)
    for i, name in enumerate(names):
        x0 = gap + i * (width + gap)
        x1 = x0 + width
        draw.rounded_rectangle((x0, gap, x1, size - gap), radius=size // 12, fill=THEME["track"])
        top = top_limit(by_name[name]) if name in by_name else None
        if top and top.percent > 0:
            height = (size - 2 * gap) * min(100, top.percent) / 100
            level = top.level()
            color = THEME.get(name.lower(), THEME["normal"]) if level == "normal" else THEME[level]
            draw.rectangle((x0, size - gap - height, x1, size - gap), fill=color)
    return img


class DesktopTray:
    def __init__(self, interval: int):
        import pystray

        self.pystray = pystray
        self.interval = max(MIN_INTERVAL, interval)
        self.data: list[ProviderUsage] = []
        self.last_fetch = 0.0
        self.wake = threading.Event()
        self.icon = pystray.Icon(APP_NAME, gauge_image([]), "LLM Usage", menu=self._menu())
        if sys.platform == "darwin":
            # Menu-bar only: no Dock icon or app menu.
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)

    def _on_main(self, fn):
        # AppKit objects may only be touched from the main thread.
        if sys.platform == "darwin":
            from PyObjCTools import AppHelper
            AppHelper.callAfter(fn)
        else:
            fn()

    def _menu(self):
        item, sep = self.pystray.MenuItem, self.pystray.Menu.SEPARATOR
        items = []

        def info(text):
            items.append(item(text, None, enabled=False))

        if not self.data:
            info("Loading…")
        for i, u in enumerate(self.data):
            if i:
                items.append(sep)
            info(u.name + (f"  ({u.plan.capitalize()})" if u.plan else ""))
            if u.error:
                info(("⚠ stale — " if u.limits else "⚠ ") + u.error)
            for lim in u.limits:
                mark = {"warning": "  ▲", "critical": "  ●"}.get(lim.level(), "")
                reset = format_remaining(lim.resets_at)
                info(f"    {lim.label}:  {lim.percent:.0f}%{mark}" + (f"   ·  resets in {reset}" if reset else ""))
        items.append(sep)
        if self.last_fetch:
            info("Updated " + time.strftime("%H:%M", time.localtime(self.last_fetch)))
        items.append(item("Refresh now", lambda: self.wake.set()))
        for name, url in USAGE_PAGES.items():
            items.append(item(f"Open {name} usage page", (lambda u: lambda: webbrowser.open(u))(url)))
        items.append(item("Quit", lambda: self.icon.stop()))
        return self.pystray.Menu(*items)

    def _render(self):
        text = summary_text(self.data)
        self.icon.icon = gauge_image(self.data)
        self.icon.title = text or "LLM Usage"  # tooltip
        self.icon.menu = self._menu()
        if sys.platform == "darwin":
            # Show the summary next to the icon, like the Linux top bar.
            try:
                button = self.icon._status_item.button()
                button.setTitle_(" " + text if text else "")
                button.setImagePosition_(2)  # NSImageLeft
            except Exception:
                pass

    def _apply(self, results: list[ProviderUsage]):
        previous = {u.name: u for u in self.data}
        for u in results:
            old = previous.get(u.name)
            if u.error and not u.limits and old and old.limits:
                u.limits, u.plan = old.limits, old.plan
        self.data = results
        self._render()

    def _loop(self, icon):
        icon.visible = True
        while True:
            if self.wake.is_set() or time.time() - self.last_fetch >= self.interval:
                self.wake.clear()
                results = fetch_all()
                self.last_fetch = time.time()
                self._on_main(lambda r=results: self._apply(r))
            else:
                self._on_main(self._render)  # keep the "resets in" countdowns current
            self.wake.wait(30)

    def run(self):
        self.icon.run(setup=lambda icon: threading.Thread(target=self._loop, args=(icon,), daemon=True).start())


def warn(msg: str) -> None:
    # Windowed builds (pythonw, PyInstaller --windowed) have no stderr.
    if sys.stderr:
        print(msg, file=sys.stderr)


def acquire_single_instance():
    """Hold a lock for the life of the process; None if another copy has it."""
    path = config_dir() / "instance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--once", action="store_true", help="print current usage and exit")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"seconds between refreshes (min {MIN_INTERVAL}, default {DEFAULT_INTERVAL})")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--tray", action="store_true", help="show in the top bar / menu bar / notification area (the default)")
    mode.add_argument("--widget", action="store_true", help="show the floating widget window")
    ap.add_argument("--framed", action="store_true",
                    help="use a normal window with title bar (if your window manager mishandles borderless windows)")
    args = ap.parse_args()
    if args.once:
        return print_once()

    lock = acquire_single_instance()  # noqa: F841 — held until the process exits
    if lock is None:
        warn("LLM Usage Widget is already running.")
        return 0

    if sys.platform.startswith("linux") and not args.widget:
        appindicator = load_appindicator()
        if appindicator:
            Tray(args.interval, appindicator).run()
            return 0
        msg = ("Top-bar mode needs PyGObject and libayatana-appindicator "
               "(Debian/Ubuntu: sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1). "
               "If they are installed, run this with the system Python: /usr/bin/python3")
        if args.tray:
            warn(msg)
            return 1
        warn(msg + "\nFalling back to the floating widget.")
    elif sys.platform in ("win32", "darwin") and not args.widget:
        try:
            import PIL  # noqa: F401
            import pystray  # noqa: F401
        except ImportError:
            msg = "Tray mode needs pystray and Pillow: python3 -m pip install -r requirements.txt"
            if args.tray:
                warn(msg)
                return 1
            warn(msg + "\nFalling back to the floating widget.")
        else:
            DesktopTray(args.interval).run()
            return 0

    Widget(args.interval, args.framed).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
