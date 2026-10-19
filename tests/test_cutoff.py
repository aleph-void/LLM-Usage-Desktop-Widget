"""Tests for the per-provider cutoff quotas.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import usage_widget as w  # noqa: E402

CLOUD_CONFIG_DIR = w.cloud_config_dir  # the real one; TempConfig replaces it


def usage(name, *percents, error=""):
    return w.ProviderUsage(name, limits=[w.Limit(f"L{i}", p) for i, p in enumerate(percents)], error=error)


class TempConfig(unittest.TestCase):
    """Points config_dir() at a fresh temporary directory, with no cloud folder unless a test sets one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "local"
        self.cloud = None
        for name, fn in (("config_dir", lambda: self.local), ("cloud_config_dir", lambda: self.cloud)):
            patcher = mock.patch.object(w, name, side_effect=fn)
            patcher.start()
            self.addCleanup(patcher.stop)


class CutoffConfigTests(TempConfig):
    def test_cutoff_for_reads_each_provider(self):
        cfg = {"cutoff": {"Claude": 95, "Codex": 80.5}}
        self.assertEqual(w.cutoff_for(cfg, "Claude"), 95.0)
        self.assertEqual(w.cutoff_for(cfg, "Codex"), 80.5)
        self.assertIsNone(w.cutoff_for(cfg, "Bedrock"))

    def test_cutoff_for_ignores_invalid_values(self):
        for value in (0, -5, 101, "95", None, True, [95]):
            self.assertIsNone(w.cutoff_for({"cutoff": {"Claude": value}}, "Claude"), value)
        self.assertIsNone(w.cutoff_for({}, "Claude"))
        self.assertIsNone(w.cutoff_for({"cutoff": 95}, "Claude"))  # must be per provider

    def test_set_cutoff_is_per_provider(self):
        w.set_cutoff("Claude", 95)
        w.set_cutoff("Codex", 80)
        self.assertEqual(w.load_config()["cutoff"], {"Claude": 95, "Codex": 80})
        w.set_cutoff("Claude", None)
        self.assertEqual(w.load_config()["cutoff"], {"Codex": 80})
        w.set_cutoff("Codex", None)
        self.assertNotIn("cutoff", w.load_config())

    def test_set_cutoff_keeps_other_settings(self):
        w.save_config({"bedrock": {"profile": "work"}, "x": 10, "cutoff": 90})
        cfg = w.set_cutoff("Codex", 99)
        self.assertEqual(cfg, {"bedrock": {"profile": "work"}, "x": 10, "cutoff": {"Codex": 99}})
        self.assertEqual(w.load_config(), cfg)

    def test_menu_label(self):
        cfg = {"cutoff": {"Claude": 95}}
        self.assertEqual(w.cutoff_menu_label(cfg, "Claude"), "Stop Claude at: 95%")
        self.assertEqual(w.cutoff_menu_label(cfg, "Codex"), "Stop Codex at: off")

    def test_menu_providers(self):
        self.assertEqual(w.cutoff_menu_providers([]), ["Claude", "Codex"])
        self.assertEqual(w.cutoff_menu_providers([usage("Codex"), usage("Bedrock")]), ["Codex", "Bedrock"])


class CliNameTests(unittest.TestCase):
    def test_native_binaries(self):
        self.assertEqual(w.cli_name("claude"), "claude")
        self.assertEqual(w.cli_name("/Users/me/.local/bin/claude", "--resume"), "claude")
        self.assertEqual(w.cli_name("codex", "exec"), "codex")
        self.assertEqual(w.cli_name("/opt/homebrew/bin/codex"), "codex")

    def test_windows_binaries(self):
        self.assertEqual(w.cli_name(r"C:\Users\me\.local\bin\claude.exe"), "claude")
        self.assertEqual(w.cli_name(r"C:\Users\me\AppData\Roaming\npm\CODEX.EXE"), "codex")

    def test_npm_scripts(self):
        self.assertEqual(w.cli_name("node", "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"), "claude")
        self.assertEqual(w.cli_name("/usr/bin/node", "/usr/local/bin/claude"), "claude")
        self.assertEqual(w.cli_name(r"C:\Program Files\nodejs\node.exe",
                                    r"C:\Users\me\AppData\Roaming\npm\node_modules\@openai\codex\bin\codex.js"),
                         "codex")
        self.assertEqual(w.cli_name("bun", "/home/me/.bun/bin/codex.js"), "codex")

    def test_desktop_apps_are_left_alone(self):
        self.assertIsNone(w.cli_name("/Applications/Claude.app/Contents/MacOS/Claude"))
        self.assertIsNone(w.cli_name("/Applications/Codex.app/Contents/Resources/codex", "app-server"))
        self.assertIsNone(w.cli_name(r"C:\Users\me\AppData\Local\AnthropicClaude\app-1.0\claude.exe"))
        self.assertIsNone(w.cli_name(r"C:\Program Files\WindowsApps\Claude_1.0_x64\app\claude.exe"))

    def test_other_processes(self):
        self.assertIsNone(w.cli_name("Claude"))  # macOS desktop app binary name
        self.assertIsNone(w.cli_name("claude-helper"))
        self.assertIsNone(w.cli_name("node", "/srv/app/server.js"))
        self.assertIsNone(w.cli_name("node"))
        self.assertIsNone(w.cli_name("python3", "/usr/local/bin/claude"))
        self.assertIsNone(w.cli_name(""))


class CommandLineParsingTests(unittest.TestCase):
    def test_first_args(self):
        self.assertEqual(w._first_args("claude --resume abc"), ("claude", "--resume"))
        self.assertEqual(w._first_args('"C:\\Program Files\\nodejs\\node.exe" "C:\\a b\\cli.js" -p'),
                         ("C:\\Program Files\\nodejs\\node.exe", "C:\\a b\\cli.js"))
        self.assertEqual(w._first_args("codex"), ("codex", ""))
        self.assertEqual(w._first_args(""), ("", ""))

    def test_windows_listing(self):
        out = ('101\t"C:\\Users\\me\\.local\\bin\\claude.exe" --continue\n'
               '102\t"C:\\Program Files\\nodejs\\node.exe" C:\\npm\\node_modules\\@openai\\codex\\bin\\codex.js\n'
               '103\t\n'
               'garbage\n')
        done = subprocess.CompletedProcess([], 0, stdout=out)
        with mock.patch.object(w.sys, "platform", "win32"), \
                mock.patch.object(w.subprocess, "run", return_value=done) as run:
            procs = w._process_command_lines()
        self.assertEqual(procs, [(101, "C:\\Users\\me\\.local\\bin\\claude.exe", "--continue"),
                                 (102, "C:\\Program Files\\nodejs\\node.exe",
                                  "C:\\npm\\node_modules\\@openai\\codex\\bin\\codex.js")])
        self.assertEqual(run.call_args.args[0][0], "powershell")

    def test_ps_listing(self):
        out = "  201 claude --resume\n  202 /usr/bin/node /usr/local/bin/codex exec\nbad line\n"
        done = subprocess.CompletedProcess([], 0, stdout=out)
        with mock.patch.object(w.sys, "platform", "darwin"), \
                mock.patch.object(w.os.path, "isdir", return_value=False), \
                mock.patch.object(w.subprocess, "run", return_value=done):
            procs = w._process_command_lines()
        self.assertEqual(procs, [(201, "claude", "--resume"), (202, "/usr/bin/node", "/usr/local/bin/codex")])

    def test_find_skips_self_and_survives_failures(self):
        procs = [(os.getpid(), "claude", ""), (5, "codex", ""), (6, "vim", "")]
        with mock.patch.object(w, "_process_command_lines", return_value=procs):
            self.assertEqual(w.find_cli_processes(), [(5, "codex")])
        with mock.patch.object(w, "_process_command_lines", side_effect=OSError("no ps")):
            self.assertEqual(w.find_cli_processes(), [])


class CutoffClisTests(unittest.TestCase):
    def test_subscription_providers(self):
        self.assertEqual(w.cutoff_clis("Claude"), {"claude"})
        self.assertEqual(w.cutoff_clis("Codex"), {"codex"})
        self.assertEqual(w.cutoff_clis("Other"), set())

    def test_bedrock_stops_the_clis_that_use_it(self):
        cases = ((True, ["https://bedrock-mantle.us-east-1.api.aws"], {"claude", "codex"}),
                 (True, [], {"claude"}),
                 (False, ["https://bedrock-runtime.us-west-2.amazonaws.com"], {"codex"}),
                 (False, [], set()))
        for claude, urls, expected in cases:
            with mock.patch.object(w, "claude_uses_bedrock", return_value=claude), \
                    mock.patch.object(w, "codex_bedrock_urls", return_value=urls):
                self.assertEqual(w.cutoff_clis("Bedrock"), expected)


class EnforceCutoffsTests(TempConfig):
    RUNNING = [(11, "claude"), (12, "claude"), (21, "codex")]

    def enforce(self, results, cutoffs, running=RUNNING):
        w.save_config({"x": 5, "cutoff": cutoffs})
        with mock.patch.object(w, "find_cli_processes", return_value=running) as find, \
                mock.patch.object(w, "stop_process", return_value=True) as stop:
            w.enforce_cutoffs(results)
        return find, sorted(c.args[0] for c in stop.call_args_list)

    def notes(self, u):
        return [lim.label for lim in u.limits if lim.label.startswith("⛔")]

    def test_only_the_provider_over_its_own_cutoff_is_stopped(self):
        claude, codex = usage("Claude", 40, 96), usage("Codex", 96)
        _, stopped = self.enforce([claude, codex], {"Claude": 95, "Codex": 99})
        self.assertEqual(stopped, [11, 12])
        self.assertEqual(self.notes(claude), ["⛔ Cutoff 95% reached — stopped 2 claude processes; cutoff cleared"])
        self.assertEqual(self.notes(codex), [])

    def test_each_provider_has_its_own_cutoff(self):
        claude, codex = usage("Claude", 85), usage("Codex", 85)
        _, stopped = self.enforce([claude, codex], {"Claude": 90, "Codex": 80})
        self.assertEqual(stopped, [21])
        self.assertEqual(self.notes(codex), ["⛔ Cutoff 80% reached — stopped 1 codex process; cutoff cleared"])

    def test_reached_cutoff_is_cleared_and_others_kept(self):
        with mock.patch.object(w.time, "time", return_value=1000.0):
            self.enforce([usage("Claude", 85), usage("Codex", 85)], {"Claude": 90, "Codex": 80})
        self.assertEqual(w.load_config(), {"x": 5, "cutoff": {"Claude": 90},
                                           "cutoff_reached": {"Codex": {"at": 1000.0, "cutoff": 80.0}},
                                           "cutoff_handled": {"Codex": 1000.0}})

    def test_fires_only_once(self):
        _, stopped = self.enforce([usage("Codex", 96)], {"Codex": 95})
        self.assertEqual(stopped, [21])
        codex = usage("Codex", 97)
        with mock.patch.object(w, "find_cli_processes", return_value=self.RUNNING) as find, \
                mock.patch.object(w, "stop_process") as stop:
            w.enforce_cutoffs([codex])  # the next refresh, still over 95%
        find.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(self.notes(codex), [])
        self.assertNotIn("cutoff", w.load_config())

    def test_reaching_the_cutoff_exactly_counts(self):
        _, stopped = self.enforce([usage("Codex", 90)], {"Codex": 90})
        self.assertEqual(stopped, [21])

    def test_below_cutoff_does_not_even_list_processes(self):
        cutoffs = {"Claude": 95, "Codex": 95}
        find, stopped = self.enforce([usage("Claude", 94.9), usage("Codex", 50)], cutoffs)
        find.assert_not_called()
        self.assertEqual(stopped, [])
        self.assertEqual(w.load_config()["cutoff"], cutoffs)

    def test_no_cutoff_set(self):
        find, stopped = self.enforce([usage("Claude", 100)], {"Codex": 50})
        find.assert_not_called()
        self.assertEqual(stopped, [])

    def test_errors_and_informational_rows_never_trigger(self):
        failed = usage("Claude", error="HTTP 500")
        bedrock = w.ProviderUsage("Bedrock", limits=[w.Limit("This month $500", None)])
        find, stopped = self.enforce([failed, bedrock], {"Claude": 1, "Bedrock": 1})
        find.assert_not_called()
        self.assertEqual(stopped, [])
        self.assertEqual(w.load_config()["cutoff"], {"Claude": 1, "Bedrock": 1})

    def test_cleared_even_when_nothing_is_running(self):
        claude = usage("Claude", 99)
        _, stopped = self.enforce([claude], {"Claude": 95}, running=[])
        self.assertEqual(stopped, [])
        self.assertEqual(self.notes(claude), ["⛔ Cutoff 95% reached — no claude running; cutoff cleared"])
        self.assertNotIn("cutoff", w.load_config())

    def test_bedrock_budget_cutoff(self):
        bedrock = w.ProviderUsage("Bedrock", limits=[w.Limit("Budget $95 / $100", 95.0)])
        with mock.patch.object(w, "cutoff_clis", return_value={"claude"}):
            _, stopped = self.enforce([bedrock], {"Bedrock": 90})
        self.assertEqual(stopped, [11, 12])


class CloudConfig(TempConfig):
    """Also gives every computer the same temporary cloud folder."""

    def setUp(self):
        super().setUp()
        self.cloud = self.root / "cloud" / w.APP_NAME
        self.cloud.parent.mkdir()

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def use_computer(self, name):
        self.local = self.root / name


class SyncedConfigTests(CloudConfig):
    def test_local_only_without_cloud_folder(self):
        self.cloud = None
        w.save_config({"cutoff": {"Claude": 90}, "x": 1})
        self.assertEqual(self.read(self.local / "config.json"), {"cutoff": {"Claude": 90}, "x": 1})
        self.assertEqual(w.load_config(), {"cutoff": {"Claude": 90}, "x": 1})

    def test_saves_shared_settings_to_the_cloud(self):
        w.save_config({"cutoff": {"Claude": 90}, "x": 1, "y": 2, "taskbar_text": False, "topmost": False})
        self.assertEqual(self.read(self.cloud / "config.json"), {"cutoff": {"Claude": 90}, "topmost": False})
        self.assertEqual(self.read(self.local / "config.json")["x"], 1)  # full local copy as a fallback
        self.assertFalse((self.cloud / "config.json.tmp").exists())

    def test_first_run_seeds_the_cloud_from_local_settings(self):
        w._write_json(self.local / "config.json", {"cutoff": {"Codex": 80}, "x": 3})
        self.assertEqual(w.load_config(), {"cutoff": {"Codex": 80}, "x": 3})
        self.assertEqual(self.read(self.cloud / "config.json"), {"cutoff": {"Codex": 80}})

    def test_nothing_written_to_the_cloud_until_there_is_a_setting(self):
        w.update_config(x=1)
        self.assertEqual(w.load_config(), {"x": 1})
        self.assertFalse(self.cloud.exists())
        w.set_cutoff("Claude", 80)
        self.assertEqual(self.read(self.cloud / "config.json"), {"cutoff": {"Claude": 80}})

    def test_cloud_settings_win_over_stale_local_ones(self):
        w._write_json(self.local / "config.json", {"cutoff": {"Codex": 80}, "bedrock": False, "x": 3})
        w._write_json(self.cloud / "config.json", {"cutoff": {"Claude": 95}, "x": 999})
        # Settings removed on another computer stay removed; position stays this computer's.
        self.assertEqual(w.load_config(), {"cutoff": {"Claude": 95}, "x": 3})

    def test_settings_follow_to_another_computer(self):
        self.use_computer("mac")
        w.update_config(x=10)
        w.set_cutoff("Claude", 95)
        self.use_computer("pc")
        self.assertEqual(w.cutoff_for(w.load_config(), "Claude"), 95.0)
        w.set_cutoff("Codex", 90)
        w.update_config(taskbar_text=False)
        self.use_computer("mac")
        self.assertEqual(w.load_config(), {"cutoff": {"Claude": 95, "Codex": 90}, "x": 10})

    def test_unreadable_cloud_file_is_not_overwritten(self):
        w._write_json(self.local / "config.json", {"cutoff": {"Codex": 80}})
        (self.cloud / "config.json").parent.mkdir(parents=True)
        (self.cloud / "config.json").write_text("{half-synced", encoding="utf-8")
        self.assertEqual(w.load_config(), {"cutoff": {"Codex": 80}})
        self.assertEqual((self.cloud / "config.json").read_text(encoding="utf-8"), "{half-synced")

    def test_icloud_placeholder_is_left_alone(self):
        self.cloud.mkdir()
        (self.cloud / ".config.json.icloud").write_bytes(b"")
        w.save_config({"cutoff": {"Claude": 50}})
        self.assertFalse((self.cloud / "config.json").exists())
        self.assertEqual(w.load_config(), {"cutoff": {"Claude": 50}})

    def test_sync_can_be_turned_off(self):
        w._write_json(self.local / "config.json", {"sync": False, "cutoff": {"Codex": 80}})
        self.assertEqual(w.load_config(), {"sync": False, "cutoff": {"Codex": 80}})
        w.set_cutoff("Claude", 90)
        self.assertFalse((self.cloud / "config.json").exists())

    def test_window_moves_do_not_rewrite_the_cloud_file(self):
        w.set_cutoff("Claude", 90)
        path = self.cloud / "config.json"
        os.utime(path, (1, 1))
        w.update_config(x=5, y=6)
        self.assertEqual(path.stat().st_mtime, 1)

    def test_cloud_folder_lookup(self):
        home = self.root / "home"
        icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        onedrive = self.root / "OneDrive - Contoso"
        real = CLOUD_CONFIG_DIR
        with mock.patch.object(w.sys, "platform", "darwin"), mock.patch.object(w.Path, "home", return_value=home):
            self.assertIsNone(real())
            icloud.mkdir(parents=True)
            self.assertEqual(real(), icloud / w.APP_NAME)
        with mock.patch.object(w.sys, "platform", "win32"):
            with mock.patch.dict(w.os.environ, {"OneDrive": "", "OneDriveConsumer": "", "OneDriveCommercial": ""}):
                self.assertIsNone(real())
            with mock.patch.dict(w.os.environ, {"OneDrive": "", "OneDriveCommercial": str(onedrive)}):
                self.assertIsNone(real())  # not there
                onedrive.mkdir()
                self.assertEqual(real(), onedrive / w.APP_NAME)
        with mock.patch.object(w.sys, "platform", "linux"):
            self.assertIsNone(real())


class SyncedCutoffTests(CloudConfig):
    """Two computers sharing a cloud folder, each running its own CLIs."""

    def enforce_on(self, computer, results, running, now):
        self.use_computer(computer)
        with mock.patch.object(w, "find_cli_processes", return_value=running), \
                mock.patch.object(w, "stop_process", return_value=True) as stop, \
                mock.patch.object(w.time, "time", return_value=now):
            w.enforce_cutoffs(results)
        return sorted(c.args[0] for c in stop.call_args_list)

    def notes(self, u):
        return [lim.label for lim in u.limits if lim.label.startswith("⛔")]

    def test_every_computer_stops_its_own_clis_once(self):
        self.use_computer("mac")
        w.set_cutoff("Claude", 90)
        w.set_cutoff("Codex", 90)
        self.assertEqual(self.enforce_on("mac", [usage("Claude", 91)], [(1, "claude")], 1000), [1])

        pc_claude = usage("Claude", 91)
        self.assertEqual(self.enforce_on("pc", [pc_claude, usage("Codex", 10)], [(2, "claude"), (3, "codex")], 1100),
                         [2])
        self.assertEqual(self.notes(pc_claude),
                         ["⛔ Cutoff 90% reached on another computer — stopped 1 claude process; cutoff cleared"])
        # Once only, on both computers, and the other provider's cutoff is untouched.
        self.assertEqual(self.enforce_on("pc", [usage("Claude", 99)], [(4, "claude")], 1200), [])
        self.assertEqual(self.enforce_on("mac", [usage("Claude", 99)], [(5, "claude")], 1300), [])
        self.assertEqual(w.load_config()["cutoff"], {"Codex": 90})

    def test_old_cutoffs_from_other_computers_are_ignored(self):
        self.use_computer("mac")
        w.set_cutoff("Claude", 90)
        self.enforce_on("mac", [usage("Claude", 91)], [], 1000)
        stopped = self.enforce_on("pc", [usage("Claude", 91)], [(2, "claude")], 1000 + w.CUTOFF_SYNC_WINDOW + 1)
        self.assertEqual(stopped, [])

    def test_new_cutoff_after_one_was_reached(self):
        self.use_computer("mac")
        w.set_cutoff("Claude", 90)
        self.enforce_on("mac", [usage("Claude", 91)], [], 1000)
        self.enforce_on("pc", [usage("Claude", 91)], [], 1100)
        w.set_cutoff("Claude", 95)
        self.assertEqual(self.enforce_on("pc", [usage("Claude", 96)], [(2, "claude")], 1200), [2])
        self.assertEqual(self.enforce_on("mac", [usage("Claude", 96)], [(1, "claude")], 1300), [1])


class RealProcessTests(unittest.TestCase):
    """Starts a harmless stand-in named like the CLI, then finds and stops it with the real OS calls."""

    def start_fake_cli(self, name):
        if sys.platform == "win32":
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, True)
            exe = os.path.join(tmp, f"{name}.exe")
            shutil.copy(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "PING.EXE"), exe)
            proc = subprocess.Popen([exe, "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL)
        else:
            sleep = shutil.which("sleep") or "/bin/sleep"
            proc = subprocess.Popen([name, "120"], executable=sleep)  # argv[0] is the CLI's name
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def wait_listed(self, pid, name):
        deadline = time.time() + 15
        while time.time() < deadline:
            if (pid, name) in w.find_cli_processes():
                return True
            time.sleep(0.2)
        return False

    def test_find_and_stop(self):
        for name in ("claude", "codex"):
            with self.subTest(name=name):
                proc = self.start_fake_cli(name)
                self.assertTrue(self.wait_listed(proc.pid, name), w.find_cli_processes())
                self.assertTrue(w.stop_process(proc.pid))
                proc.wait(timeout=15)
                self.assertNotIn((proc.pid, name), w.find_cli_processes())

    def test_stop_missing_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(w.stop_process(proc.pid))

    def test_unrelated_process_not_listed(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertNotIn(proc.pid, [pid for pid, _ in w.find_cli_processes()])


if __name__ == "__main__":
    unittest.main()
