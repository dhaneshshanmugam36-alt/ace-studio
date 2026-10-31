#!/usr/bin/env python3
"""
Ace Studio — an unofficial Linux desktop app for the Insta360 Ace Pro 2.

Talks to the camera over its own Wi-Fi hotspot:
  * control + status  -> TCP 6666 (Insta360's protobuf protocol, via the
                         community `insta360` Python package)
  * files + downloads -> HTTP 80 on the camera (/DCIM/...)
  * live preview      -> H.264/H.265 from the camera, shown as MJPEG
It can also read the camera's memory card over a USB cable.

The UI is a local web page served on 127.0.0.1 and shown in a native window
(pywebview) when available, otherwise in the browser.

Run `python3 app.py --demo` to try the interface without a camera.

Unofficial. Not affiliated with or endorsed by Insta360.

Copyright (C) 2026 Ace Studio contributors. Licensed under the GNU General
Public License v3 or later; see the LICENSE file. No warranty.
"""

from __future__ import annotations

__version__ = "2.0.3"
REPO = "dhaneshshanmugam36-alt/ace-studio"

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

import looks
from camera import (THUMB_DIR, DemoCamera, MjpegPipe, RealCamera, UsbSource, friendly,
                    photo_thumbnail)
from jobs import Downloads, Exporter, Shooting
from store import Config, Library, scan_for_duplicates

APP_DIR = Path(__file__).resolve().parent
log = logging.getLogger("ace-studio")


# --------------------------------------------------------------------------
# Background helpers
# --------------------------------------------------------------------------

class Supervisor(threading.Thread):
    """Connects automatically when the camera's Wi-Fi appears and reconnects after drops."""

    def __init__(self, camera, cfg):
        super().__init__(daemon=True)
        self.camera, self.cfg = camera, cfg
        self.last_try = 0.0

    def run(self):
        while True:
            time.sleep(2)
            cam = self.camera
            cam.clock_mode = self.cfg.get("clock")
            if cam.connected or cam.user_disconnected:
                continue
            want = (cam.link == "reconnecting" and self.cfg.get("auto_reconnect")) or \
                   (cam.link == "off" and self.cfg.get("auto_connect"))
            if not want or time.time() - self.last_try < 3:
                continue
            if not cam.reachable():
                continue
            self.last_try = time.time()
            was = cam.link
            try:
                log.info("Camera found; %s", "reconnecting" if was == "reconnecting" else "connecting automatically")
                cam.connect()
                if cam.preview_wanted:
                    try:
                        cam.start_preview()
                    except Exception as exc:
                        log.warning("Live view didn't restart: %s", exc)
            except Exception as exc:
                cam.link = was
                log.info("Automatic connect failed: %s", exc)


class UpdateChecker(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.latest: dict | None = None

    @staticmethod
    def _ver(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.lstrip("vV").split(".")[:3])
        except ValueError:
            return (0,)

    def check(self) -> None:
        import requests
        r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=6,
                         headers={"Accept": "application/vnd.github+json"})
        if r.ok:
            d = r.json()
            tag = d.get("tag_name", "")
            if self._ver(tag) > self._ver(__version__):
                self.latest = {"version": tag.lstrip("vV"), "url": d.get("html_url"), "notes": (d.get("body") or "")[:400]}
            else:
                self.latest = None

    def run(self):
        while True:
            if self.cfg.get("check_updates"):
                try:
                    self.check()
                    time.sleep(6 * 3600)
                    continue
                except Exception:
                    pass  # no internet (e.g. on the camera's Wi-Fi): try again soon
            time.sleep(900)


def pick_folder(title: str) -> str | None:
    """Show the system's folder picker (zenity or kdialog)."""
    if shutil.which("zenity"):
        r = subprocess.run(["zenity", "--file-selection", "--directory", f"--title={title}"],
                           capture_output=True, text=True)
        return r.stdout.strip() or None
    if shutil.which("kdialog"):
        r = subprocess.run(["kdialog", "--getexistingdirectory", str(Path.home()), "--title", title],
                           capture_output=True, text=True)
        return r.stdout.strip() or None
    raise RuntimeError("No folder picker found. Type the folder path instead.")


# --------------------------------------------------------------------------
# Web app
# --------------------------------------------------------------------------

def create_app(camera, cfg: Config, library: Library, demo: bool) -> Flask:
    app = Flask(__name__, static_folder=None)
    exporter = Exporter(cfg)

    def on_copied(job, dest: Path):
        look = job.get("look") or cfg.get("default_look")
        for lid in ([lk.id for lk in looks.LOOKS] if look == "all" else [look]):
            if lid in looks.LOOKS_BY_ID:
                exporter.add_look(dest, lid, job["path"])

    downloads = Downloads(cfg, library, on_copied)
    shooting = Shooting(camera)
    updates = UpdateChecker(cfg)
    updates.start()
    Supervisor(camera, cfg).start()
    thumb_sem = threading.Semaphore(2)
    files_cache: dict[str, dict] = {}
    usb_cache = {"src": None, "at": 0.0}
    dup_state = {"running": False}

    def ok(**extra):
        return jsonify({"ok": True, **extra})

    def fail(exc, code=400, action=""):
        msg = exc if isinstance(exc, str) else friendly(exc, action)
        log.warning("%s", msg)
        return jsonify({"ok": False, "error": msg}), code

    def usb() -> UsbSource | None:
        if time.time() - usb_cache["at"] > 3:
            usb_cache["src"], usb_cache["at"] = UsbSource.find(), time.time()
        return usb_cache["src"]

    def source():
        """Where the Library reads from: the Wi-Fi camera, or the camera on USB."""
        if camera.connected:
            return camera
        u = usb()
        if u:
            return u
        raise RuntimeError("Connect to the camera's Wi-Fi, or plug the camera in by USB, to see its files.")

    def resolve_local(name: str, size=None) -> Path | None:
        p = library.copied_path(name, size)
        if p:
            return p
        old = Path(os.path.expanduser("~/Videos/Ace Pro 2"))
        for cand in (cfg.save_dir / name, cfg.save_dir / "Pictures" / name, cfg.save_dir / "Videos" / name, old / name):
            if cand.is_file() and (not size or cand.stat().st_size == size):
                return cand
        return None

    # ---- pages -----------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(APP_DIR / "static", "index.html", max_age=0)

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(APP_DIR / "static", name)

    # ---- status & settings ------------------------------------------------
    @app.get("/api/status")
    def status():
        u = usb()
        return jsonify({**camera.status(), **shooting.status(), "demo": demo, "version": __version__,
                        "save_dir": str(cfg.save_dir).replace(str(Path.home()), "~", 1), "host": getattr(camera, "host", "demo"),
                        "usb": {"label": u.label, "path": str(u.root)} if u else None,
                        "update": updates.latest, "copying": downloads.active()})

    @app.get("/api/config")
    def get_config():
        return ok(config=cfg.public(), looks=looks.catalog(), version=__version__, repo=REPO,
                  tags=library.all_tags(), trips=library.trips())

    @app.post("/api/config")
    def set_config():
        changes = request.json or {}
        if "save_dir" in changes:
            p = Path(os.path.expanduser(str(changes["save_dir"]).strip() or "~/Downloads/Ace Pro 2"))
            try:
                p.mkdir(parents=True, exist_ok=True)
                test = p / ".ace-write-test"; test.write_text("ok"); test.unlink()
            except Exception as exc:
                return fail(friendly(exc) if not isinstance(exc, FileExistsError) else "That path isn't a folder.")
        if changes.get("backup_dir"):
            if not Path(os.path.expanduser(changes["backup_dir"])).is_dir():
                return fail("That backup folder doesn't exist. Plug in the drive or pick another folder.")
        if "default_look" in changes and changes["default_look"] not in ("", "all", *looks.LOOKS_BY_ID):
            return fail("Unknown look.")
        if "camera_host" in changes and isinstance(camera, RealCamera):
            camera.host = str(changes["camera_host"]).strip() or "192.168.42.1"
        return ok(config=cfg.update(changes))

    @app.post("/api/pick-folder")
    def api_pick_folder():
        try:
            path = pick_folder((request.json or {}).get("title", "Choose a folder"))
            return ok(path=path)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/update/check")
    def update_check():
        try:
            updates.check()
            return ok(update=updates.latest)
        except Exception:
            return fail("Couldn't check for updates. Connect to the internet (not the camera's Wi-Fi) and try again.")

    @app.post("/api/open-url")
    def open_url():
        url = str((request.json or {}).get("url", ""))
        if not url.startswith(f"https://github.com/{REPO}"):
            return fail("That link isn't allowed.")
        subprocess.Popen([shutil.which("xdg-open") or "xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ok()

    # ---- connection -------------------------------------------------------
    @app.post("/api/connect")
    def connect():
        try:
            camera.clock_mode = cfg.get("clock")
            camera.user_disconnected = False
            camera.connect()
            return ok(**camera.status())
        except Exception as exc:
            camera.last_error = str(exc)
            return fail(exc, 503)

    @app.post("/api/disconnect")
    def disconnect():
        shooting.stop_interval()
        camera.disconnect()
        return ok()

    @app.post("/api/demo/drop")
    def demo_drop():
        if not demo:
            return fail("Only in demo mode.")
        camera.simulate_drop(float((request.json or {}).get("seconds", 5)))
        return ok()

    # ---- capture ----------------------------------------------------------
    @app.post("/api/record")
    def record():
        body = request.json or {}
        action = body.get("action")
        try:
            if action == "start":
                shooting.start_recording(float(body.get("stop_after") or 0))
            else:
                shooting.stop_recording()
            return ok(**camera.status(), **shooting.status())
        except Exception as exc:
            return fail(exc, action="start recording" if action == "start" else "stop recording")

    @app.post("/api/photo")
    def photo():
        try:
            camera.take_photo()
            return ok()
        except Exception as exc:
            return fail(exc, action="take the photo")

    @app.post("/api/interval")
    def interval():
        body = request.json or {}
        try:
            if body.get("action") == "start":
                shooting.start_interval(float(body.get("every", 5)), int(body.get("count", 0)))
            else:
                shooting.stop_interval()
            return ok(**shooting.status())
        except Exception as exc:
            return fail(exc, action="start interval photos")

    @app.post("/api/mode")
    def mode():
        m = (request.json or {}).get("mode")
        try:
            camera.set_mode("photo" if m == "photo" else "video")
            return ok(mode=camera.mode)
        except Exception as exc:
            return fail(exc, action="switch modes")

    @app.post("/api/preview")
    def preview():
        on = bool((request.json or {}).get("on"))
        try:
            camera.preview_wanted = on
            camera.start_preview() if on else camera.stop_preview()
            return ok(preview=camera.preview is not None)
        except Exception as exc:
            return fail(exc, action="start the live view")

    @app.get("/stream.mjpg")
    def stream():
        pipe = camera.preview
        if pipe is None:
            return ("Live view is off", 404)

        def gen():
            for frame in pipe.frames():
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")

        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-store"})

    # ---- library ----------------------------------------------------------
    @app.get("/api/files")
    def files():
        try:
            src = source()
            items = src.list_files()
        except Exception as exc:
            return fail(exc, action="read the camera's memory card")
        shared = exporter.shared()
        looks_done = exporter.looks_done()
        elsewhere = library.data.get("elsewhere", {})
        files_cache.clear()
        for it in items:
            local = resolve_local(it["name"], it.get("size"))
            any_local = local or library.data["copies"].get(it["name"]) or (cfg.save_dir / it["name"]).exists()
            it["downloaded"] = local is not None
            it["incomplete"] = bool(any_local) and local is None
            it["local"] = str(local) if local else None
            it["shared"] = Exporter.share_name(Path(local).name if local else it["name"]) in shared
            stem = os.path.splitext(Path(local).name if local else it["name"])[0]
            it["looks"] = [lk.id for lk in looks.LOOKS if stem in looks_done.get(lk.id, ())]
            meta = library.meta(it["name"])
            it["fav"], it["note"], it["tags"] = bool(meta.get("fav")), meta.get("note", ""), meta.get("tags", [])
            it["elsewhere"] = elsewhere.get(it["name"], [])
            trip = library.trip_for((it.get("taken") or "")[:10])
            it["trip"] = trip["id"] if trip else None
            files_cache[it["path"]] = it
        items.sort(key=lambda f: (f.get("taken") or "", f["name"]), reverse=True)
        return jsonify({"ok": True, "files": items, "source": src.kind,
                        "trips": library.trips(), "tags": library.all_tags(),
                        "elsewhere_scanned": library.data.get("elsewhere_scanned")})

    @app.get("/api/thumb")
    def thumb():
        path = request.args.get("path", "")
        if not path.startswith("/DCIM/"):
            return ("bad path", 400)
        try:
            src = source()
        except Exception:
            return ("not connected", 404)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        size = (files_cache.get(path) or {}).get("size")
        key = hashlib.sha1(f"{getattr(camera, 'host', 'demo')}{path}{size}".encode()).hexdigest()
        cached = THUMB_DIR / f"{key}.jpg"
        if not cached.exists():
            with thumb_sem:
                if not cached.exists():
                    try:
                        data = src.thumbnail(path, (files_cache.get(path) or {}).get("lrv"))
                    except Exception:
                        data = None
                    if not data:
                        return ("no preview", 404)
                    cached.write_bytes(data)
        return send_from_directory(THUMB_DIR, cached.name, max_age=86400 * 30)

    @app.get("/api/details")
    def details():
        path = request.args.get("path", "")
        if not path.startswith("/DCIM/"):
            return fail("Bad path.")
        it = files_cache.get(path) or {}
        try:
            local = resolve_local(it.get("name", ""), it.get("size")) if it else None
            if local:
                from camera import photo_details, video_details
                info = photo_details(str(local)) if local.suffix.lower() in (".jpg", ".jpeg") else video_details(str(local))
            else:
                info = source().details(path)
            log.info("Details for %s: %s", path, info)
            return ok(details=info)
        except Exception as exc:
            return fail(exc, action="read the file's details")

    @app.post("/api/meta")
    def meta():
        b = request.json or {}
        names = [str(n) for n in b.get("names", [])][:5000]
        library.update_meta(names, fav=b.get("fav"), note=b.get("note"),
                            add_tags=b.get("add_tags"), remove_tags=b.get("remove_tags"))
        return ok(tags=library.all_tags())

    @app.post("/api/trips")
    def add_trip():
        b = request.json or {}
        if not b.get("start"):
            return fail("Pick at least one day for the trip.")
        return ok(trip=library.add_trip(str(b.get("name", "")), str(b["start"]), str(b.get("end") or b["start"])),
                  trips=library.trips())

    @app.post("/api/trips/delete")
    def del_trip():
        library.remove_trip(str((request.json or {}).get("id", "")))
        return ok(trips=library.trips())

    @app.post("/api/duplicates/scan")
    def dup_scan():
        if dup_state["running"]:
            return ok(running=True)
        wanted = {f["name"]: f.get("size") for f in files_cache.values()}
        if not wanted:
            return fail("Open the Library first so Ace Studio knows what's on the camera.")

        def run():
            dup_state["running"] = True
            try:
                library.set_elsewhere(scan_for_duplicates(wanted, cfg.get("dup_dirs"), cfg.save_dir))
            finally:
                dup_state["running"] = False

        threading.Thread(target=run, daemon=True).start()
        return ok(running=True)

    @app.get("/api/duplicates/status")
    def dup_status():
        return ok(running=dup_state["running"], scanned=library.data.get("elsewhere_scanned"))

    @app.post("/api/delete")
    def delete():
        """Delete files from the camera's memory card. Cannot be undone."""
        paths = (request.json or {}).get("paths", [])
        try:
            src = source()
        except Exception as exc:
            return fail(exc)
        if getattr(camera, "recording_since", None) and src is camera:
            return fail("Stop recording before deleting files.")
        busy = {j["path"] for j in downloads.snapshot() if j["state"] in ("queued", "running")}
        items = [files_cache[p] for p in paths if p in files_cache and p not in busy]
        if not items:
            return fail("Those files are being copied right now. Wait for the copy to finish, then delete.")
        try:
            failed = src.delete_files(items)
        except Exception as exc:
            return fail(exc, action="delete the files")
        for it in items:
            if it["path"] not in failed:
                files_cache.pop(it["path"], None)
        n = len(items) - len(failed)
        log.info("Deleted %d file(s) from the %s; %d failed", n, src.kind, len(failed))
        if failed and not n:
            return fail("The camera didn't delete the files. Check the card isn't locked and try again.")
        return ok(deleted=n, failed=failed, skipped=len(paths) - len(items))

    # ---- copying, share copies, looks ------------------------------------
    @app.post("/api/download")
    def download():
        paths = (request.json or {}).get("paths", [])
        look = (request.json or {}).get("look")
        if look and look != "all" and look not in looks.LOOKS_BY_ID:
            return fail("Unknown look.")
        try:
            src = source()
        except Exception as exc:
            return fail(exc)
        items = [files_cache[p] for p in paths if p in files_cache]
        n = downloads.add(items, src, look)
        return ok(added=n, jobs=all_jobs())

    def locals_for(names_or_paths: list[str]) -> list[tuple[Path, str | None]]:
        out = []
        for key in names_or_paths:
            it = files_cache.get(key) or next((f for f in files_cache.values() if f["name"] == key), None)
            if it:
                p = resolve_local(it["name"], it.get("size"))
                if p:
                    out.append((p, it["path"]))
        return out

    @app.post("/api/share")
    def share():
        b = request.json or {}
        targets = locals_for(b.get("paths") or b.get("names") or [])
        if not targets:
            return fail("Copy the files to this computer first, then make share copies.")
        for p, cam_path in targets:
            exporter.add_share(p, cam_path)
        return ok(added=len(targets))

    @app.post("/api/look")
    def apply_look():
        b = request.json or {}
        look_id = b.get("look")
        ids = [lk.id for lk in looks.LOOKS] if look_id == "all" else [look_id]
        if look_id != "all" and look_id not in looks.LOOKS_BY_ID:
            return fail("Pick a look first.")
        targets = locals_for(b.get("paths") or [])
        if not targets:
            return fail("Copy the files to this computer first, then apply a look.")
        for p, cam_path in targets:
            for lid in ids:
                exporter.add_look(p, lid, cam_path)
        return ok(added=len(targets) * len(ids))

    @app.get("/api/look/preview")
    def look_preview():
        path, look_id = request.args.get("path", ""), request.args.get("look", "")
        if look_id not in looks.LOOKS_BY_ID or not path.startswith("/DCIM/"):
            return ("bad request", 400)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        it = files_cache.get(path) or {}
        key = hashlib.sha1(f"look{look_id}{path}{it.get('size')}".encode()).hexdigest()
        cached = THUMB_DIR / f"{key}.jpg"
        if not cached.exists():
            base = None
            local = resolve_local(it.get("name", ""), it.get("size")) if it else None
            if local and local.suffix.lower() in (".jpg", ".jpeg"):
                base = photo_thumbnail(str(local), 960)
            if base is None:
                try:
                    base = source().thumbnail(path, it.get("lrv"))
                except Exception:
                    base = None
            if not base:
                return ("no preview", 404)
            out = looks.preview_jpeg(base, look_id, 960)
            if not out:
                return ("no preview", 404)
            cached.write_bytes(out)
        return send_from_directory(THUMB_DIR, cached.name, max_age=86400)

    @app.get("/api/local")
    def local_file():
        """Serve a copied file (for full-size viewing)."""
        path = request.args.get("path", "")
        it = files_cache.get(path) or {}
        p = resolve_local(it.get("name", ""), it.get("size")) if it else None
        if not p:
            return ("not copied", 404)
        return send_file(p, conditional=True)

    def all_jobs():
        return sorted(downloads.snapshot() + exporter.snapshot(), key=lambda j: j["created"])

    @app.get("/api/jobs")
    def jobs():
        return jsonify({"ok": True, "jobs": all_jobs()})

    @app.post("/api/jobs/cancel")
    def cancel():
        jid = (request.json or {}).get("id")
        downloads.cancel(jid)
        exporter.cancel(jid)
        return ok()

    @app.post("/api/jobs/clear")
    def clear():
        downloads.clear_finished()
        exporter.clear_finished()
        return ok()

    @app.post("/api/open-folder")
    def open_folder():
        b = request.json or {}
        which = b.get("which", "copies")
        folder = {"share": exporter.share_dir, "looks": exporter.looks_dir}.get(which, cfg.save_dir)
        if b.get("file"):
            f = Path(str(b["file"]))
            if f.is_file() and (cfg.save_dir in f.parents):
                folder = f.parent
        folder.mkdir(parents=True, exist_ok=True)
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ok()

    return app


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def open_window(url: str, prefer_browser: bool, cfg: Config) -> bool:
    """Show the UI in a native window. Returns True if a blocking window ran."""
    if not prefer_browser:
        try:
            import webview  # pywebview

            w, h = (cfg.get("window") or [1280, 820])[:2]
            win = webview.create_window("Ace Studio", url, width=max(900, int(w)), height=max(600, int(h)),
                                        min_size=(900, 600), background_color="#14171A")
            size = {"wh": None}

            def resized(width, height):
                size["wh"] = [width, height]

            def closing():
                if size["wh"]:
                    cfg.update({"window": size["wh"]})

            try:
                win.events.resized += resized
                win.events.closing += closing
            except Exception:
                pass
            webview.start(gui="gtk")
            return True
        except Exception as exc:
            log.info("Native window unavailable (%s); using the browser.", exc)
    for browser in ("chromium", "chromium-browser", "google-chrome", "brave-browser", "microsoft-edge"):
        exe = shutil.which(browser)
        if exe:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1280,820"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return False
    subprocess.Popen([shutil.which("xdg-open") or "xdg-open", url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Unofficial desktop app for the Insta360 Ace Pro 2")
    ap.add_argument("--demo", action="store_true", help="use a pretend camera")
    ap.add_argument("--camera", default=None, help="camera IP on its Wi-Fi (default 192.168.42.1)")
    ap.add_argument("--port", type=int, default=8765, help="local port for the interface")
    ap.add_argument("--save-dir", default=None, help="where copies go (overrides Settings for this run)")
    ap.add_argument("--browser", action="store_true", help="open in the browser instead of a window")
    ap.add_argument("--headless", action="store_true", help="only run the server")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--version", action="version", version=f"Ace Studio {__version__}")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if not shutil.which("ffmpeg"):
        print("ffmpeg is missing. Install it with your package manager (for example: sudo apt install ffmpeg).",
              file=sys.stderr)
        sys.exit(1)

    cfg = Config({"save_dir": args.save_dir, "camera_host": args.camera})
    library = Library()
    camera = DemoCamera() if args.demo else RealCamera(cfg.get("camera_host"))
    camera.clock_mode = cfg.get("clock")
    app = create_app(camera, cfg, library, args.demo)
    url = f"http://127.0.0.1:{args.port}/"

    server = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False),
        daemon=True,
    )
    server.start()
    time.sleep(0.6)
    print(f"Ace Studio {__version__} running at {url}")

    try:
        if args.headless or not open_window(url, args.browser, cfg):
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        camera.disconnect()


if __name__ == "__main__":
    main()
