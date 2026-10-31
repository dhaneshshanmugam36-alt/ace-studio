"""Settings, library notes (favourites, tags, trips) and the copy log."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

OLD_SAVE_DIR = "~/Videos/Ace Pro 2"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ace-studio"

DEFAULTS = {
    # Copying
    "save_dir": "~/Downloads/Ace Pro 2",
    "split_kinds": True,             # photos in "Pictures", videos in "Videos"
    "folders_by_date": False,
    "file_names": "original",        # "original" | "date"
    "backup_dir": "",
    "default_look": "",              # look applied to every new copy ("" = none)
    "look_video_size": "1080p",      # "1080p" | "full"
    # Camera
    "camera_host": "192.168.42.1",
    "auto_connect": True,
    "auto_reconnect": True,
    "clock": "computer",             # "computer" | "leave"
    # Appearance
    "theme": "system",               # "system" | "light" | "dark"
    "seed": "camera",
    "lang": "en",                    # "en" | "ms"
    # Other
    "check_updates": True,
    "first_run_done": False,
    "window": [1280, 820],
    "last_view": "live",
    "dup_dirs": ["~/Pictures", "~/Videos", "~/Downloads"],
}


class JsonFile:
    """A small JSON document saved atomically, safe to use from several threads."""

    def __init__(self, path: Path, defaults: dict):
        self.path = path
        self.lock = threading.RLock()
        self.data = json.loads(json.dumps(defaults))
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass
        except Exception:
            # A damaged file shouldn't stop the app; keep a copy for debugging.
            try:
                path.replace(path.with_suffix(".broken.json"))
            except OSError:
                pass

    def save(self) -> None:
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
                tmp.replace(self.path)
            except OSError:
                pass


class Config(JsonFile):
    def __init__(self, overrides: dict | None = None):
        super().__init__(CONFIG_DIR / "config.json", DEFAULTS)
        self.overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        # 2.0.1: copies moved from ~/Videos to ~/Downloads by default.
        if self.data.get("save_dir") == OLD_SAVE_DIR and not self.data.get("save_dir_chosen"):
            self.data["save_dir"] = DEFAULTS["save_dir"]
            self.save()

    def get(self, key):
        if key in self.overrides:
            return self.overrides[key]
        return self.data.get(key, DEFAULTS.get(key))

    def public(self) -> dict:
        return {k: self.get(k) for k in DEFAULTS}

    def update(self, changes: dict) -> dict:
        with self.lock:
            for k, v in changes.items():
                if k not in DEFAULTS:
                    continue
                kind = type(DEFAULTS[k])
                if kind is bool:
                    v = bool(v)
                elif kind is list and not isinstance(v, list):
                    continue
                elif kind is str:
                    v = str(v).strip()
                self.data[k] = v
                if k == "save_dir":
                    self.data["save_dir_chosen"] = True
                self.overrides.pop(k, None)
            self.save()
        return self.public()

    @property
    def save_dir(self) -> Path:
        return Path(os.path.expanduser(self.get("save_dir") or DEFAULTS["save_dir"]))


class Library(JsonFile):
    """Per-file notes keyed by the camera's file name, plus trips and the copy log."""

    def __init__(self):
        super().__init__(CONFIG_DIR / "library.json",
                         {"items": {}, "trips": [], "copies": {}, "elsewhere": {}, "elsewhere_scanned": None})

    # -- favourites, notes, tags ------------------------------------------
    def meta(self, name: str) -> dict:
        return self.data["items"].get(name, {})

    def update_meta(self, names: list[str], fav=None, note=None, add_tags=None, remove_tags=None) -> None:
        with self.lock:
            for name in names:
                m = self.data["items"].setdefault(name, {})
                if fav is not None:
                    m["fav"] = bool(fav)
                if note is not None:
                    m["note"] = str(note)[:500]
                tags = set(m.get("tags", []))
                tags |= {t.strip()[:40] for t in (add_tags or []) if t.strip()}
                tags -= set(remove_tags or [])
                m["tags"] = sorted(tags)
                if not m.get("fav") and not m.get("note") and not m.get("tags"):
                    self.data["items"].pop(name, None)
            self.save()

    def all_tags(self) -> list[str]:
        return sorted({t for m in self.data["items"].values() for t in m.get("tags", [])})

    # -- trips --------------------------------------------------------------
    def trips(self) -> list[dict]:
        return sorted(self.data["trips"], key=lambda t: t["start"], reverse=True)

    def add_trip(self, name: str, start: str, end: str) -> dict:
        if end < start:
            start, end = end, start
        trip = {"id": f"t{int(time.time() * 1000)}", "name": name.strip()[:60] or "Trip", "start": start, "end": end}
        with self.lock:
            self.data["trips"].append(trip)
            self.save()
        return trip

    def remove_trip(self, trip_id: str) -> None:
        with self.lock:
            self.data["trips"] = [t for t in self.data["trips"] if t["id"] != trip_id]
            self.save()

    def trip_for(self, day: str | None) -> dict | None:
        if not day:
            return None
        for t in self.trips():
            if t["start"] <= day <= t["end"]:
                return t
        return None

    # -- copy log -----------------------------------------------------------
    def record_copy(self, name: str, path: Path, size: int) -> None:
        with self.lock:
            self.data["copies"][name] = {"path": str(path), "size": size, "time": time.time()}
            self.save()

    def copied_path(self, name: str, size: int | None) -> Path | None:
        c = self.data["copies"].get(name)
        if not c:
            return None
        p = Path(c["path"])
        try:
            have = p.stat().st_size
        except OSError:
            return None
        return p if (not size or have == size) else None

    # -- found elsewhere (duplicate check) -----------------------------------
    def set_elsewhere(self, found: dict[str, list[str]]) -> None:
        with self.lock:
            self.data["elsewhere"] = found
            self.data["elsewhere_scanned"] = time.time()
            self.save()


def scan_for_duplicates(names_sizes: dict[str, int | None], dirs: list[str], skip: Path,
                        limit_files: int = 200_000) -> dict[str, list[str]]:
    """Find files with the same name and size as camera files in other folders."""
    wanted = {n.lower(): (n, s) for n, s in names_sizes.items()}
    found: dict[str, list[str]] = {}
    seen = 0
    skip = skip.resolve()
    for d in dirs:
        root = Path(os.path.expanduser(d))
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            p = Path(dirpath)
            try:
                if p.resolve() == skip or skip in p.resolve().parents:
                    dirnames[:] = []
                    continue
            except OSError:
                continue
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            for fn in filenames:
                seen += 1
                if seen > limit_files:
                    return found
                hit = wanted.get(fn.lower())
                if not hit:
                    continue
                name, size = hit
                fp = p / fn
                try:
                    if size and fp.stat().st_size != size:
                        continue
                except OSError:
                    continue
                found.setdefault(name, []).append(str(fp))
    return found
