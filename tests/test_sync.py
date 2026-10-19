"""Settings sync through iCloud Drive and OneDrive, each computer with its own local config folder."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import usage_widget as w


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def shared(path: Path) -> dict:
    """A cloud copy without its "saved_at" stamp."""
    return {k: v for k, v in read(path).items() if k != "saved_at"}


class Computers:
    """Switches between computers: each has its own local folder and sees some of the cloud folders."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.icloud = root / "icloud" / w.APP_NAME
        self.onedrive = root / "onedrive" / w.APP_NAME
        for cloud in (self.icloud, self.onedrive):
            cloud.parent.mkdir(parents=True)
        self.local = root / "this"
        self.clouds = [self.icloud]
        self.now = 1000.0
        monkeypatch.setattr(w, "config_dir", lambda: self.local)
        monkeypatch.setattr(w, "cloud_config_dirs", lambda: list(self.clouds))
        monkeypatch.setattr(w.time, "time", lambda: self.now)

    def use(self, name: str, *clouds: Path):
        self.local = self.root / name
        self.clouds = list(clouds)
        self.now += 10


@pytest.fixture
def pcs(tmp_path, monkeypatch) -> Computers:
    return Computers(tmp_path / "sync", monkeypatch)


# -- one cloud ---------------------------------------------------------------------

def test_local_only_without_cloud_folder(pcs):
    pcs.clouds = []
    w.save_config({"cutoff": {"Claude": 90}, "x": 1})
    assert read(pcs.local / "config.json") == {"cutoff": {"Claude": 90}, "x": 1}
    assert w.load_config() == {"cutoff": {"Claude": 90}, "x": 1}


def test_saves_shared_settings_to_the_cloud(pcs):
    w.save_config({"cutoff": {"Claude": 90}, "x": 1, "y": 2, "taskbar_text": False, "collapsed": True,
                   "topmost": False, "cutoff_handled": {"Claude": 5.0}})
    assert read(pcs.icloud / "config.json") == {"cutoff": {"Claude": 90}, "topmost": False, "saved_at": 1000.0}
    assert read(pcs.local / "config.json")["x"] == 1  # full local copy as a fallback
    assert not (pcs.icloud / "config.json.tmp").exists()
    assert w.load_config() == {"cutoff": {"Claude": 90}, "topmost": False, "x": 1, "y": 2, "taskbar_text": False,
                               "collapsed": True, "cutoff_handled": {"Claude": 5.0}}


def test_first_run_seeds_the_cloud_from_local_settings(pcs):
    w._write_json(pcs.local / "config.json", {"cutoff": {"Codex": 80}, "x": 3})
    assert w.load_config() == {"cutoff": {"Codex": 80}, "x": 3}
    assert shared(pcs.icloud / "config.json") == {"cutoff": {"Codex": 80}}


def test_nothing_written_to_the_cloud_until_there_is_a_setting(pcs):
    w.update_config(x=1)
    assert w.load_config() == {"x": 1}
    assert not pcs.icloud.exists()
    w.set_cutoff("Claude", 80)
    assert shared(pcs.icloud / "config.json") == {"cutoff": {"Claude": 80}}


def test_cloud_settings_win_over_stale_local_ones(pcs):
    w._write_json(pcs.local / "config.json", {"cutoff": {"Codex": 80}, "bedrock": False, "x": 3})
    w._write_json(pcs.icloud / "config.json", {"cutoff": {"Claude": 95}, "x": 999, "saved_at": 1.0})
    # Settings removed on another computer stay removed; the position stays this computer's.
    assert w.load_config() == {"cutoff": {"Claude": 95}, "x": 3}


def test_settings_follow_to_another_computer(pcs):
    pcs.use("mac", pcs.icloud)
    w.update_config(x=10)
    w.set_cutoff("Claude", 95)
    pcs.use("pc", pcs.icloud)
    assert w.cutoff_for(w.load_config(), "Claude") == 95.0
    w.set_cutoff("Codex", 90)
    w.update_config(taskbar_text=False)
    pcs.use("mac", pcs.icloud)
    assert w.load_config() == {"cutoff": {"Claude": 95, "Codex": 90}, "x": 10}


def test_unreadable_cloud_file_is_not_overwritten(pcs):
    w._write_json(pcs.local / "config.json", {"cutoff": {"Codex": 80}})
    pcs.icloud.mkdir()
    (pcs.icloud / "config.json").write_text("{half-synced", encoding="utf-8")
    assert w.load_config() == {"cutoff": {"Codex": 80}}
    w.set_cutoff("Claude", 50)
    assert (pcs.icloud / "config.json").read_text(encoding="utf-8") == "{half-synced"


def test_icloud_placeholder_is_left_alone(pcs):
    pcs.icloud.mkdir()
    (pcs.icloud / ".config.json.icloud").write_bytes(b"")
    w.save_config({"cutoff": {"Claude": 50}})
    assert not (pcs.icloud / "config.json").exists()
    assert w.load_config() == {"cutoff": {"Claude": 50}}


def test_sync_can_be_turned_off(pcs):
    w._write_json(pcs.local / "config.json", {"sync": False, "cutoff": {"Codex": 80}})
    assert w.load_config() == {"sync": False, "cutoff": {"Codex": 80}}
    w.set_cutoff("Claude", 90)
    assert not (pcs.icloud / "config.json").exists()


def test_window_moves_do_not_rewrite_the_cloud_file(pcs):
    w.set_cutoff("Claude", 90)
    path = pcs.icloud / "config.json"
    os.utime(path, (1, 1))
    w.update_config(x=5, y=6)
    w.load_config()
    assert path.stat().st_mtime == 1


def test_save_config_swallows_cloud_errors(pcs, monkeypatch):
    blocker = pcs.root / "file"
    blocker.write_text("")
    pcs.clouds = [blocker / w.APP_NAME]  # its parent is a file: writing fails
    w.save_config({"cutoff": {"Claude": 90}})  # must not raise
    assert w.load_config() == {"cutoff": {"Claude": 90}}


# -- iCloud and OneDrive together -------------------------------------------------------

def test_saves_to_every_cloud(pcs):
    pcs.clouds = [pcs.icloud, pcs.onedrive]
    w.set_cutoff("Claude", 90)
    assert read(pcs.icloud / "config.json") == read(pcs.onedrive / "config.json") == {
        "cutoff": {"Claude": 90}, "saved_at": 1000.0}


def test_newest_copy_wins_and_is_copied_to_the_other_cloud(pcs):
    pcs.clouds = [pcs.icloud, pcs.onedrive]
    w._write_json(pcs.icloud / "config.json", {"cutoff": {"Claude": 80}, "saved_at": 50.0})
    w._write_json(pcs.onedrive / "config.json", {"cutoff": {"Codex": 95}, "saved_at": 60.0})
    assert w.load_config() == {"cutoff": {"Codex": 95}}
    assert read(pcs.icloud / "config.json") == {"cutoff": {"Codex": 95}, "saved_at": 60.0}


def test_hand_edit_wins_a_tie(pcs):
    pcs.clouds = [pcs.icloud, pcs.onedrive]
    w._write_json(pcs.icloud / "config.json", {"cutoff": {"Claude": 80}, "saved_at": 50.0})
    w._write_json(pcs.onedrive / "config.json", {"cutoff": {"Claude": 99}, "saved_at": 50.0})
    os.utime(pcs.icloud / "config.json", (100, 100))
    os.utime(pcs.onedrive / "config.json", (200, 200))  # edited by hand later; saved_at unchanged
    assert w.load_config() == {"cutoff": {"Claude": 99}}


def test_a_new_cloud_gets_the_settings(pcs):
    pcs.clouds = [pcs.icloud]
    w.set_cutoff("Claude", 90)
    pcs.clouds = [pcs.icloud, pcs.onedrive]  # OneDrive signed in later
    w.load_config()
    assert shared(pcs.onedrive / "config.json") == {"cutoff": {"Claude": 90}}


def test_a_computer_with_both_links_icloud_and_onedrive_computers(pcs):
    pcs.use("mac", pcs.icloud)
    w.set_cutoff("Claude", 90)
    pcs.use("pc", pcs.onedrive)
    w.set_cutoff("Codex", 80)  # hasn't seen the Mac's change yet
    pcs.use("laptop", pcs.icloud, pcs.onedrive)
    assert w.load_config()["cutoff"] == {"Codex": 80}  # the newest save wins
    w.set_cutoff("Claude", 95)
    pcs.use("mac", pcs.icloud)
    assert w.load_config()["cutoff"] == {"Codex": 80, "Claude": 95}
    w.set_cutoff("Codex", None)
    pcs.use("laptop", pcs.icloud, pcs.onedrive)
    w.load_config()  # passes the Mac's change on to OneDrive
    pcs.use("pc", pcs.onedrive)
    assert w.load_config()["cutoff"] == {"Claude": 95}


def test_unreadable_copy_in_one_cloud_does_not_stop_the_other(pcs):
    pcs.clouds = [pcs.icloud, pcs.onedrive]
    pcs.icloud.mkdir()
    (pcs.icloud / "config.json").write_text("{half-synced", encoding="utf-8")
    w._write_json(pcs.onedrive / "config.json", {"cutoff": {"Codex": 70}, "saved_at": 5.0})
    assert w.load_config() == {"cutoff": {"Codex": 70}}
    w.set_cutoff("Claude", 90)
    assert shared(pcs.onedrive / "config.json") == {"cutoff": {"Codex": 70, "Claude": 90}}
    assert (pcs.icloud / "config.json").read_text(encoding="utf-8") == "{half-synced"


# -- finding the cloud folders ----------------------------------------------------------

def test_cloud_folders_on_macos(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert w.cloud_config_dirs() == []
    icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    icloud.mkdir(parents=True)
    assert w.cloud_config_dirs() == [icloud / w.APP_NAME]
    storage = home / "Library" / "CloudStorage"
    for name in ("OneDrive-Personal", "OneDrive-Contoso", "GoogleDrive-me@example.com"):
        (storage / name).mkdir(parents=True)
    assert w.cloud_config_dirs() == [icloud / w.APP_NAME, storage / "OneDrive-Contoso" / w.APP_NAME,
                                     storage / "OneDrive-Personal" / w.APP_NAME]


def test_cloud_folders_on_macos_older_onedrive(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    (home / "OneDrive").mkdir()
    assert w.cloud_config_dirs() == [home / "OneDrive" / w.APP_NAME]


def test_cloud_folders_on_windows(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert w.cloud_config_dirs() == []
    personal, work = home / "OneDrive", home / "OneDrive - Contoso"
    personal.mkdir()
    work.mkdir()
    monkeypatch.setenv("OneDrive", str(personal))
    monkeypatch.setenv("OneDriveConsumer", str(personal))  # the same folder twice
    monkeypatch.setenv("OneDriveCommercial", str(work))
    assert w.cloud_config_dirs() == [personal / w.APP_NAME, work / w.APP_NAME]
    (home / "iCloudDrive").mkdir()
    assert w.cloud_config_dirs()[-1] == home / "iCloudDrive" / w.APP_NAME


def test_cloud_folders_ignore_missing_onedrive(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("OneDrive", str(home / "gone"))
    assert w.cloud_config_dirs() == []


def test_no_cloud_folders_on_linux(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    (home / "OneDrive").mkdir()
    assert w.cloud_config_dirs() == []
