"""Background work: copying from the camera, share copies, film looks, and shooting timers."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import looks
from camera import NAME_RE, friendly

log = logging.getLogger("ace-studio")


def copy_name(item: dict, style: str) -> str:
    """File name for a copy: the camera's own, or a readable date-based one."""
    name = item["name"]
    if style != "date":
        return name
    m = NAME_RE.match(name)
    if not m:
        return name
    d, t = m["date"], m["time"]
    ext = os.path.splitext(name)[1].lower()
    kind = "Video" if item.get("kind") == "video" else "Photo"
    return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}.{t[2:4]}.{t[4:]} {kind} {m['idx']}{ext}"


class Downloads:
    def __init__(self, cfg, library, on_copied=None):
        self.cfg = cfg
        self.library = library
        self.on_copied = on_copied
        self.jobs: dict[str, dict] = {}
        self.order: list[str] = []
        self.sources: dict[str, object] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    def dest_for(self, item: dict) -> Path:
        folder = self.cfg.save_dir
        if self.cfg.get("split_kinds"):
            folder = folder / ("Videos" if item.get("kind") == "video" else "Pictures")
        if self.cfg.get("folders_by_date") and item.get("taken"):
            folder = folder / item["taken"][:10]
        return folder / copy_name(item, self.cfg.get("file_names"))

    def add(self, items: list[dict], source, look: str | None = None) -> int:
        added = 0
        with self.lock:
            for it in items:
                if any(j["path"] == it["path"] and j["state"] in ("queued", "running") for j in self.jobs.values()):
                    continue
                jid = uuid.uuid4().hex[:10]
                self.jobs[jid] = {"id": jid, "kind": "copy", "path": it["path"], "name": it["name"],
                                  "item": {k: it.get(k) for k in ("name", "kind", "taken", "size")},
                                  "done": 0, "total": it.get("size"), "state": "queued", "speed": 0,
                                  "error": None, "note": None, "created": time.time(), "via": source.kind,
                                  "look": look or None}
                self.sources[jid] = source
                self.cancel_events[jid] = threading.Event()
                self.order.append(jid)
                added += 1
        self.wake.set()
        return added

    def cancel(self, jid: str | None) -> None:
        with self.lock:
            for i in ([jid] if jid else list(self.jobs)):
                j = self.jobs.get(i)
                if j and j["state"] in ("queued", "running"):
                    self.cancel_events[i].set()
                    if j["state"] == "queued":
                        j["state"] = "cancelled"

    def clear_finished(self) -> None:
        with self.lock:
            for i in list(self.order):
                if self.jobs[i]["state"] in ("done", "cancelled", "failed"):
                    self.order.remove(i)
                    self.jobs.pop(i)
                    self.cancel_events.pop(i, None)
                    self.sources.pop(i, None)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [{k: v for k, v in self.jobs[i].items() if k != "item"} for i in self.order]

    def active(self) -> bool:
        with self.lock:
            return any(j["state"] in ("queued", "running") for j in self.jobs.values())

    def _backup(self, src: Path, job: dict) -> None:
        bdir = self.cfg.get("backup_dir")
        if not bdir:
            return
        root = Path(os.path.expanduser(bdir))
        if not root.is_dir():
            job["note"] = "Backup skipped: the backup folder isn't available. Is the drive plugged in?"
            return
        try:
            rel = src.relative_to(self.cfg.save_dir)
        except ValueError:
            rel = Path(src.name)
        dest = root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(".part-" + dest.name)
            shutil.copy2(src, tmp)
            if tmp.stat().st_size != src.stat().st_size:
                raise OSError("size mismatch")
            tmp.replace(dest)
            job["note"] = "Also backed up"
        except Exception as exc:
            job["note"] = "Backup failed: " + friendly(exc)

    def _worker(self) -> None:
        while True:
            self.wake.wait()
            self.wake.clear()
            while True:
                with self.lock:
                    jid = next((i for i in self.order if self.jobs[i]["state"] == "queued"), None)
                    if jid is None:
                        break
                    job = self.jobs[jid]
                    job["state"] = "running"
                cancel, source = self.cancel_events[jid], self.sources[jid]
                dest = self.dest_for(job["item"])
                started = time.time()

                def progress(done, total, job=job, started=started):
                    job["done"] = done
                    if total:
                        job["total"] = total
                    job["speed"] = done / max(time.time() - started, 0.1)

                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    job["dest"] = str(dest)
                    ok = False
                    for attempt in range(4):
                        try:
                            ok = source.download(job["path"], dest, progress, cancel)
                            err = None
                        except Exception as exc:
                            err = exc
                        if ok or cancel.is_set() or source.kind != "wifi" or attempt == 3:
                            if err is not None:
                                raise err
                            break
                        # Wi-Fi dropped mid-copy: wait for the automatic reconnect, then resume.
                        job["note"] = "Connection dropped. Waiting for the camera to come back…"
                        until = time.time() + 120
                        while time.time() < until and not cancel.is_set() and not getattr(source, "connected", False):
                            time.sleep(1)
                        if not getattr(source, "connected", False):
                            if err is not None:
                                raise err
                            break
                        job["note"] = "Resumed after a dropped connection"
                    if cancel.is_set():
                        job["state"] = "cancelled"
                    elif ok:
                        have = dest.stat().st_size if dest.exists() else -1
                        if job["total"] and have != job["total"]:
                            job["state"] = "failed"
                            job["error"] = "The copy is incomplete. Copy it again."
                        else:
                            job["state"] = "done"
                            job["done"] = job["total"] = have
                            self.library.record_copy(job["name"], dest, have)
                            self._backup(dest, job)
                            if self.on_copied:
                                self.on_copied(job, dest)
                    else:
                        job["state"], job["error"] = "failed", "The copy stopped. Check the connection and copy again."
                except Exception as exc:
                    log.warning("Copy failed: %r", exc)
                    job["state"], job["error"] = "failed", friendly(exc)


class Exporter:
    """Share copies (small 1080p / 2048 px) and film-look copies, made from files already copied."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs: dict[str, dict] = {}
        self.order: list[str] = []
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    # -- output locations ----------------------------------------------------
    @property
    def share_dir(self) -> Path:
        return self.cfg.save_dir / "Share"

    @property
    def looks_dir(self) -> Path:
        return self.cfg.save_dir / "Looks"

    @staticmethod
    def share_name(name: str) -> str:
        stem, ext = os.path.splitext(name)
        return f"{stem}_share{'.jpg' if ext.lower() in ('.jpg', '.jpeg') else '.mp4'}"

    def look_path(self, src_name: str, look_id: str) -> Path:
        lk = looks.LOOKS_BY_ID[look_id]
        stem, ext = os.path.splitext(src_name)
        return self.looks_dir / lk.name / f"{stem}{'.jpg' if ext.lower() in ('.jpg', '.jpeg') else '.mp4'}"

    def shared(self) -> set[str]:
        try:
            return {p.name for p in self.share_dir.iterdir() if p.is_file()}
        except OSError:
            return set()

    def looks_done(self) -> dict[str, set[str]]:
        """{look folder name: {file stems}} for marking Library items."""
        out: dict[str, set[str]] = {}
        try:
            for d in self.looks_dir.iterdir():
                if d.is_dir():
                    out[d.name] = {os.path.splitext(p.name)[0] for p in d.iterdir() if p.is_file() and not p.name.startswith(".")}
        except OSError:
            pass
        return out

    # -- queue ---------------------------------------------------------------
    def _add(self, job: dict) -> None:
        with self.lock:
            if any(j["out"] == job["out"] and j["state"] in ("queued", "running") for j in self.jobs.values()):
                return
            job.update({"id": uuid.uuid4().hex[:10], "done": 0, "total": 100, "state": "queued",
                        "speed": 0, "error": None, "created": time.time(), "path": job.get("path")})
            self.jobs[job["id"]] = job
            self.order.append(job["id"])
        self.wake.set()

    def add_share(self, src: Path, camera_path: str | None = None) -> None:
        out = self.share_dir / self.share_name(src.name)
        self._add({"kind": "share", "name": src.name, "src": str(src), "out": str(out), "path": camera_path})

    def add_look(self, src: Path, look_id: str, camera_path: str | None = None) -> None:
        if look_id not in looks.LOOKS_BY_ID:
            raise ValueError("Unknown look")
        out = self.look_path(src.name, look_id)
        self._add({"kind": "look", "look": look_id, "look_name": looks.LOOKS_BY_ID[look_id].name,
                   "name": src.name, "src": str(src), "out": str(out), "path": camera_path})

    def cancel(self, jid: str | None) -> None:
        with self.lock:
            for i in ([jid] if jid else list(self.jobs)):
                j = self.jobs.get(i)
                if not j or j["state"] not in ("queued", "running"):
                    continue
                j["state"] = "cancelled"
                proc = self.procs.get(i)
                if proc:
                    proc.terminate()

    def clear_finished(self) -> None:
        with self.lock:
            for i in list(self.order):
                if self.jobs[i]["state"] in ("done", "cancelled", "failed"):
                    self.order.remove(i)
                    self.jobs.pop(i)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [{k: v for k, v in self.jobs[i].items() if k != "src"} for i in self.order]

    @staticmethod
    def _duration(path: str) -> float:
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
            return float(out.stdout.strip())
        except Exception:
            return 0.0

    def _run_ffmpeg(self, jid: str, job: dict, cmd: list[str], total: float) -> tuple[int, str]:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs[jid] = proc
        for line in proc.stdout:
            if line.startswith("out_time_us=") and total > 0:
                try:
                    job["done"] = min(99, int(int(line.split("=")[1]) / 1e6 / total * 100))
                except ValueError:
                    pass
        err = proc.stderr.read()
        code = proc.wait()
        self.procs.pop(jid, None)
        return code, err

    def _worker(self) -> None:
        while True:
            self.wake.wait()
            self.wake.clear()
            while True:
                with self.lock:
                    jid = next((i for i in self.order if self.jobs[i]["state"] == "queued"), None)
                    if jid is None:
                        break
                    job = self.jobs[jid]
                    job["state"] = "running"
                src, dest = job["src"], Path(job["out"])
                tmp = dest.with_name(".part-" + dest.name)
                is_photo = dest.suffix == ".jpg"
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not Path(src).is_file():
                        raise FileNotFoundError("The copied file isn't on this computer any more. Copy it again.")
                    code, err = 0, ""
                    if job["kind"] == "look" and is_photo:
                        job["done"] = 30
                        looks.apply_photo(src, str(tmp), job["look"])
                    elif job["kind"] == "look":
                        cmd = looks.video_cmd(src, str(tmp), job["look"], self.cfg.get("look_video_size"))
                        code, err = self._run_ffmpeg(jid, job, cmd, self._duration(src))
                    elif is_photo:
                        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                               "-vf", "scale='min(2048,iw)':-2", "-q:v", "3", "-f", "image2", str(tmp)]
                        code, err = self._run_ffmpeg(jid, job, cmd, 0)
                    else:
                        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
                               "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:'min(1080,ih)'",
                               "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
                               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-f", "mp4", str(tmp)]
                        code, err = self._run_ffmpeg(jid, job, cmd, self._duration(src))
                    if job["state"] == "cancelled":
                        tmp.unlink(missing_ok=True)
                    elif code == 0 and tmp.exists():
                        tmp.replace(dest)
                        job["state"], job["done"], job["size"] = "done", 100, dest.stat().st_size
                    else:
                        tmp.unlink(missing_ok=True)
                        job["state"], job["error"] = "failed", (err.strip().splitlines() or ["Export failed"])[-1]
                except Exception as exc:
                    tmp.unlink(missing_ok=True)
                    job["state"], job["error"] = "failed", friendly(exc)


class Shooting:
    """Timed recording and interval photos (run by this computer)."""

    def __init__(self, camera):
        self.camera = camera
        self.stop_at: float | None = None
        self._timer: threading.Timer | None = None
        self.interval: dict | None = None
        self._interval_stop = threading.Event()

    def start_recording(self, stop_after: float | None) -> None:
        self.camera.start_recording()
        self._cancel_timer()
        if stop_after and stop_after > 0:
            self.stop_at = time.time() + stop_after
            self._timer = threading.Timer(stop_after, self._auto_stop)
            self._timer.daemon = True
            self._timer.start()

    def stop_recording(self) -> None:
        self._cancel_timer()
        self.camera.stop_recording()

    def _auto_stop(self) -> None:
        self.stop_at = None
        if self.camera.recording_since is not None:
            try:
                self.camera.stop_recording()
                log.info("Timed recording finished")
            except Exception as exc:
                log.warning("Timed stop failed: %s", exc)

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer, self.stop_at = None, None

    def start_interval(self, every: float, count: int) -> None:
        if self.interval and self.interval["running"]:
            return
        if self.camera.mode != "photo":
            self.camera.set_mode("photo")
        every = max(2.0, float(every))
        self._interval_stop.clear()
        self.interval = {"running": True, "every": every, "count": int(count), "taken": 0,
                         "next_at": time.time(), "error": None}
        threading.Thread(target=self._interval_loop, daemon=True).start()

    def stop_interval(self) -> None:
        self._interval_stop.set()
        if self.interval:
            self.interval["running"] = False

    def _interval_loop(self) -> None:
        iv = self.interval
        while not self._interval_stop.is_set():
            wait = iv["next_at"] - time.time()
            if wait > 0 and self._interval_stop.wait(wait):
                break
            try:
                self.camera.take_photo()
                iv["taken"] += 1
            except Exception as exc:
                iv["error"] = "Interval photos stopped: " + friendly(exc)
                break
            if iv["count"] and iv["taken"] >= iv["count"]:
                break
            iv["next_at"] = max(iv["next_at"] + iv["every"], time.time())
        iv["running"] = False

    def status(self) -> dict:
        return {"stop_at": self.stop_at, "interval": self.interval}
