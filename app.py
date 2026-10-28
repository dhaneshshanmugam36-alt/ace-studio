#!/usr/bin/env python3
"""
Ace Studio — a desktop companion for the Insta360 Ace Pro 2 on Linux.

Talks to the camera over its own Wi-Fi hotspot:
  * control + status  -> TCP 6666 (Insta360's protobuf protocol, via the
                         community `insta360` Python package)
  * files + downloads -> HTTP 80 on the camera (/DCIM/...)
  * live preview      -> H.264/H.265 packets from the camera, re-encoded to
                         MJPEG by ffmpeg so any web view can show them

The UI is a local web page served on 127.0.0.1 and shown in a native window
(pywebview) when available, otherwise in the browser.

Run `python3 app.py --demo` to try the interface without a camera.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ace-studio"
THUMB_DIR = CACHE_DIR / "thumbs"
log = logging.getLogger("ace-studio")

NAME_RE = re.compile(
    r"^(?P<kind>VID|IMG|LRV|PRO_VID|PRO_LRV|PRO_IMG)_(?P<date>\d{8})_(?P<time>\d{6})(?:_(?P<lens>\d{2}))?_(?P<idx>\d{3})",
    re.I,
)
VIDEO_EXT = {".mp4", ".mov", ".insv"}
PHOTO_EXT = {".jpg", ".jpeg", ".insp", ".dng"}
HIDDEN_EXT = {".lrv", ".thm", ".list", ".log", ".txt", ".bin"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_size(text: str) -> int | None:
    """'1.2G' / '845M' / '12 KB' / '123456' -> bytes."""
    if not text:
        return None
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?)i?B?\s*$", text.strip(), re.I)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def to_bytes_guess(value) -> int | None:
    """Camera reports storage in an undocumented unit; pick the plausible one."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return 0
    if v < 10_000_000:      # MB
        return v * 1024**2
    if v < 4_000_000_000:   # KB (cards up to ~4 TB)
        return v * 1024
    return v                # bytes


def describe_file(path: str, size: int | None = None) -> dict | None:
    name = path.rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1].lower()
    if ext in HIDDEN_EXT or name.startswith("."):
        return None
    if ext in VIDEO_EXT:
        kind = "video"
    elif ext in PHOTO_EXT:
        kind = "photo"
    else:
        return None
    m = NAME_RE.match(name)
    taken = None
    if m:
        try:
            taken = datetime.strptime(m["date"] + m["time"], "%Y%m%d%H%M%S").isoformat()
        except ValueError:
            pass
    return {
        "path": path,
        "name": name,
        "kind": kind,
        "ext": ext.lstrip("."),
        "size": size,
        "taken": taken,
        "key": f"{m['date']}_{m['time']}_{m['idx']}" if m else name,
    }


def detect_codec(buf: bytes) -> str | None:
    """Spot H.264 vs H.265 from Annex-B start codes, so ffmpeg can skip probing."""
    i = 0
    while True:
        i = buf.find(b"\x00\x00\x01", i)
        if i < 0 or i + 4 >= len(buf):
            return None
        b0, b1 = buf[i + 3], buf[i + 4]
        if (b0 >> 1) & 0x3F in (32, 33) and b1 == 0x01:   # HEVC VPS / SPS
            return "hevc"
        if b0 & 0x1F == 7 and not b0 & 0x80:               # H.264 SPS
            return "h264"
        i += 3


class MjpegPipe:
    """Feeds encoded video into ffmpeg and keeps only the newest JPEG frame.

    For the camera stream ffmpeg is started lazily: the first bytes are
    inspected to learn the codec, which lets ffmpeg start decoding at once
    with no probing delay. Decoding runs single-threaded because frame
    threading holds back one frame per thread.
    """

    # Note: "-fflags nobuffer" is left out on purpose: with raw H.264 on a pipe it
    # makes ffmpeg drop every frame.
    LOW_DELAY = ["-flags", "low_delay", "-threads", "1"]

    def __init__(self, input_args: list[str] | None = None):
        self.cond = threading.Condition()
        self.frame: bytes | None = None
        self.frame_no = 0
        self.codec: str | None = None
        self.proc: subprocess.Popen | None = None
        self._pending = b""
        self._times: list[float] = []      # frame output times, for fps
        self._bytes_in: list[tuple[float, int]] = []
        self.started = time.time()
        self.closed = False
        if input_args is not None:          # demo / fixed source
            self._start(input_args)

    def _start(self, input_args: list[str]) -> None:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            *self.LOW_DELAY, *input_args,
            "-an", "-vsync", "passthrough",
            "-vf", "scale=960:-2:flags=fast_bilinear",
            "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        threading.Thread(target=self._read, daemon=True).start()

    def write(self, data: bytes) -> None:
        now = time.time()
        self._bytes_in.append((now, len(data)))
        if len(self._bytes_in) > 400:
            del self._bytes_in[:200]
        if self.proc is None:
            if self.closed:
                return
            self._pending += data
            codec = detect_codec(self._pending)
            if codec:
                self.codec = codec
                log.info("Live view codec: %s", codec)
                self._start(["-probesize", "32", "-analyzeduration", "0", "-f", codec, "-i", "pipe:0"])
            elif len(self._pending) > 1_500_000:
                self.codec = "auto"
                log.info("Live view codec not recognised from start codes; letting ffmpeg probe")
                self._start(["-probesize", "512k", "-i", "pipe:0"])
            else:
                return
            data, self._pending = self._pending, b""
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def stats(self) -> dict:
        now = time.time()
        recent = [t for t in self._times if now - t < 2]
        kb = sum(n for t, n in self._bytes_in if now - t < 2)
        return {
            "fps": round(len(recent) / 2, 1),
            "kbps": round(kb * 8 / 2 / 1000),
            "codec": self.codec,
            "waiting": self.frame is None,
            "since": round(now - self.started, 1),
        }

    def _read(self) -> None:
        buf = b""
        out = self.proc.stdout
        while True:
            chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2)
                if start < 0 or end < 0:
                    if start > 0:
                        buf = buf[start:]
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                now = time.time()
                self._times.append(now)
                if len(self._times) > 240:
                    del self._times[:120]
                with self.cond:
                    self.frame = frame
                    self.frame_no += 1
                    self.cond.notify_all()

    def frames(self):
        last = -1
        while not self.closed and (self.proc is None or self.proc.poll() is None):
            with self.cond:
                self.cond.wait_for(lambda: self.frame_no != last, timeout=2)
                if self.frame_no == last or self.frame is None:
                    continue
                last, frame = self.frame_no, self.frame
            yield frame

    def close(self) -> None:
        self.closed = True
        with self.cond:
            self.cond.notify_all()
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


# --------------------------------------------------------------------------
# Camera backends
# --------------------------------------------------------------------------

class BaseCamera:
    def __init__(self):
        self.lock = threading.RLock()
        self.connected = False
        self.info: dict = {}
        self.recording_since: float | None = None
        self.mode = "video"
        self.preview: MjpegPipe | None = None
        self.last_error: str | None = None

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "recording": self.recording_since is not None,
            "recording_since": self.recording_since,
            "mode": self.mode,
            "preview": self.preview is not None,
            "preview_stats": self.preview.stats() if self.preview is not None else None,
            "error": self.last_error,
            **self.info,
        }


class RealCamera(BaseCamera):
    def __init__(self, host: str):
        super().__init__()
        self.host = host
        self.client = None
        self._poller: threading.Thread | None = None

    # -- connection -------------------------------------------------------
    def connect(self) -> None:
        from insta360.rtmp import Client  # imported lazily: heavy deps

        with self.lock:
            if self.connected:
                return
            self.last_error = None
            client = Client(host=self.host, callback=self._on_notification, enable_hwaccel=False)

            @client.on_event("video_stream", uid="ace-studio-preview", wait=True)
            async def _video(data=None, **_):
                pipe = self.preview
                if pipe is not None and data:
                    pipe.write(data)

            @client.on_event("disconnect", uid="ace-studio-disconnect")
            async def _gone(**_):
                self.connected = False

            # The library checks reachability via /osc/info, an endpoint only the
            # 360 cameras serve. The Ace Pro 2 returns 404 there, so check the
            # control port directly instead.
            def _port_check():
                import socket
                try:
                    socket.create_connection((self.host, 6666), timeout=5).close()
                except OSError as exc:
                    from insta360.rtmp.exception import CameraNotConnectedException
                    raise CameraNotConnectedException(str(exc)) from exc

            client.ensure_camera_connected = _port_check

            try:
                ok = client.open()
            except Exception as exc:
                log.warning("open() failed: %r", exc)
                try:
                    client.close()
                except Exception:
                    pass
                if "not connected" in type(exc).__name__.lower() or isinstance(exc, OSError):
                    raise RuntimeError(
                        "Can't reach the camera. Join its Wi-Fi network first, then try again."
                    ) from exc
                raise RuntimeError(f"The camera answered but didn't finish the handshake ({exc}).") from exc
            if ok is False and not client.is_connected:
                raise RuntimeError("The camera refused the connection. Restart its Wi-Fi and retry.")
            self.client = client
            self.connected = True
            try:
                self.refresh()
            except Exception as exc:
                log.warning("Couldn't read camera status: %r", exc)
            self._poller = threading.Thread(target=self._poll, daemon=True)
            self._poller.start()

    def disconnect(self) -> None:
        with self.lock:
            self.stop_preview()
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
            self.client = None
            self.connected = False

    def _poll(self) -> None:
        while self.connected:
            time.sleep(6)
            if not self.connected:
                break
            try:
                self.refresh()
            except Exception as exc:
                log.debug("status poll failed: %s", exc)

    def _on_notification(self, msg: dict) -> None:
        state = str(msg.get("state", ""))
        if "IDLE" in state or "NOT_CAPTURE" in state:
            self.recording_since = None
        elif "RECORD" in state or "CAPTURE" in state:
            if self.recording_since is None:
                t = int(msg.get("captureTime", 0) or 0)
                self.recording_since = time.time() - t

    def _require(self):
        if not self.connected or self.client is None:
            raise RuntimeError("Not connected to the camera.")
        return self.client

    # -- status -----------------------------------------------------------
    def refresh(self) -> None:
        client = self._require()
        resp = client.get_camera_info(wait_for_seconds=5)
        value = (resp or {}).get("value", {}) if isinstance(resp, dict) else {}
        batt = value.get("batteryStatus", {})
        level, scale = batt.get("batteryLevel"), batt.get("batteryScale") or 100
        stor = value.get("storageState", {})
        info = {
            "model": value.get("cameraType") or "Insta360 camera",
            "firmware": value.get("firmwareRevision"),
            "battery": round(100 * int(level) / int(scale)) if level is not None else None,
            "charging": "USB" in str(batt.get("powerType", "")).upper(),
            "card": stor.get("cardState"),
            "free": to_bytes_guess(stor.get("freeSpace")),
            "total": to_bytes_guess(stor.get("totalSpace")),
            "temp": value.get("tempValue"),
        }
        if value.get("photoSubMode") == "PHOTO_NONE":
            self.mode = "video"
        elif value.get("videoSubMode") == "VIDEO_NONE":
            self.mode = "photo"
        self.info = {k: v for k, v in info.items() if v is not None}

    # -- capture ----------------------------------------------------------
    def start_recording(self) -> None:
        client = self._require()
        seq = client.start_capture()
        client._check_if_command_successful(seq, wait_for_seconds=6)
        self.recording_since = time.time()

    def stop_recording(self) -> None:
        client = self._require()
        seq = client.stop_capture()
        client._check_if_command_successful(seq, wait_for_seconds=10)
        self.recording_since = None

    def take_photo(self) -> None:
        self._require().take_picture(wait_for_seconds=10)

    def set_mode(self, mode: str) -> None:
        client = self._require()
        client.set_camera_mode("VIDEO_NORMAL" if mode == "video" else "PHOTO_SINGLE", wait_for_seconds=6)
        self.mode = mode

    # -- preview ----------------------------------------------------------
    PREVIEW_RESOLUTIONS = ["RES_1920_1080P30", "RES_1280_720P30", "RES_1440_720P30"]

    def start_preview(self) -> None:
        client = self._require()
        with self.lock:
            if self.preview:
                return
            self.preview = MjpegPipe()  # starts ffmpeg once the codec is known
            last_exc = None
            for res in self.PREVIEW_RESOLUTIONS:
                msg = {
                    "enableVideo": True,
                    "audioSampleRate": 48000,
                    "enableGyro": False,
                    "resolution": res,
                    "resolution1": "RES_424_240P15",
                }
                try:
                    seq = client._send_message(msg, client.PHONE_COMMAND_START_LIVE_STREAM)
                    client._check_if_command_successful(seq, wait_for_seconds=5)
                    client.preview_stream_started = True
                    return
                except Exception as exc:
                    last_exc = exc
            self.preview.close()
            self.preview = None
            raise RuntimeError("The camera didn't start its live view.") from last_exc

    def stop_preview(self) -> None:
        with self.lock:
            pipe, self.preview = self.preview, None
        if pipe is None:
            return
        try:
            if self.client is not None and self.connected:
                self.client.stop_preview_stream()
        except Exception:
            pass
        pipe.close()

    # -- files ------------------------------------------------------------
    def base_url(self) -> str:
        return f"http://{self.host}"

    def _list_via_protocol(self) -> list[tuple[str, int | None]]:
        """Ask the camera for its file list over the control connection."""
        from urllib.parse import urlparse

        client = self._require()
        uris, offset = [], 0
        for _ in range(50):  # hard stop: 50 pages
            resp = client.get_camera_files_list(limit=100, offset=offset, wait_for_seconds=10)
            batch = list(resp.uri or [])
            uris.extend(batch)
            if not batch or len(uris) >= (resp.totalCount or 0):
                break
            offset += len(batch)
        paths = []
        for u in uris:
            p = urlparse(u).path if "://" in u else u
            if not p.startswith("/"):
                p = "/" + p
            paths.append(p)
        log.info("Camera reported %d files; first: %s", len(paths), paths[:3])
        return [(p, None) for p in paths]

    def _fill_sizes(self, found: list[tuple[str, int | None]]) -> list[tuple[str, int | None]]:
        import requests
        from concurrent.futures import ThreadPoolExecutor

        def head(item):
            path, size = item
            if size is not None or path.lower().endswith(".lrv"):
                return item
            try:
                r = requests.head(self.base_url() + path, timeout=5)
                return path, int(r.headers.get("Content-Length")) if r.ok else None
            except Exception:
                return item

        with ThreadPoolExecutor(6) as pool:
            return list(pool.map(head, found))

    def list_files(self) -> list[dict]:
        self._require()
        try:
            found = self._fill_sizes(self._list_via_protocol())
        except Exception as exc:
            log.warning("Protocol file list failed (%r); trying the web listing", exc)
            found = self._list_via_web()
        return self._describe_all(found)

    def _list_via_web(self) -> list[tuple[str, int | None]]:
        import requests
        from bs4 import BeautifulSoup

        found, queue = [], ["/DCIM/"]
        while queue:
            cur = queue.pop(0)
            r = requests.get(self.base_url() + cur, timeout=15)
            if r.status_code != 200 and cur == "/DCIM/":
                raise RuntimeError("the camera didn't share its file list")
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.select("table tr"):
                a = row.select_one("td:nth-child(1) a")
                cells = row.select("td")
                if not a or len(cells) < 3:
                    continue
                name = a.text.strip()
                size_txt = cells[2].text.strip()
                if name in ("..", "../", "Parent Directory") or name.startswith("."):
                    continue
                if "directory" in size_txt.lower() or name.endswith("/"):
                    queue.append(cur + name.rstrip("/") + "/")
                    continue
                found.append((cur + name, parse_size(size_txt)))
        return found

    def _describe_all(self, found: list[tuple[str, int | None]]) -> list[dict]:
        all_paths = {p for p, _ in found}
        files = []
        for path, size in found:
            d = describe_file(path, size)
            if d:
                d["lrv"] = self._find_lrv(path, all_paths)
                files.append(d)
        return files

    @staticmethod
    def _find_lrv(path: str, all_paths: set[str]) -> str | None:
        folder, name = path.rsplit("/", 1)
        m = NAME_RE.match(name)
        if not m or not m["kind"].upper().endswith("VID"):
            return None
        prefix = "PRO_LRV" if m["kind"].upper().startswith("PRO") else "LRV"
        for p in all_paths:
            n = p.rsplit("/", 1)[-1]
            if n.upper().startswith(prefix + f"_{m['date']}_{m['time']}_") and n.endswith(f"_{m['idx']}.lrv") and p.startswith(folder):
                return p
        return None

    def thumbnail(self, path: str, lrv: str | None) -> bytes | None:
        src = self.base_url() + (lrv or path)
        is_video = os.path.splitext(path)[1].lower() in VIDEO_EXT
        if os.path.splitext(path)[1].lower() in {".dng", ".insp"}:
            return None
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if is_video:
            cmd += ["-ss", "1"]
        cmd += ["-i", src, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5",
                "-f", "image2", "-vcodec", "mjpeg", "pipe:1"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=40).stdout
        except subprocess.TimeoutExpired:
            return None
        return out or None

    def download(self, path: str, dest: Path, progress, cancel: threading.Event) -> bool:
        client = self._require()
        return client.download_file(
            path, str(dest), resume=True,
            progress_callback=lambda done, total: progress(done, total),
            cancel_event=cancel,
        )


class DemoCamera(BaseCamera):
    """A pretend camera so the UI can be tried without hardware."""

    def __init__(self):
        super().__init__()
        rnd = random.Random(7)
        self.files = []
        base = datetime(2026, 10, 25, 7, 12, 0)
        for i in range(17):
            t = base.timestamp() + i * 2700 + rnd.randint(0, 900) - (86400 if i < 6 else 0)
            dt = datetime.fromtimestamp(t)
            is_photo = i % 4 == 3
            stamp = dt.strftime("%Y%m%d_%H%M%S")
            name = f"IMG_{stamp}_00_{i:03d}.jpg" if is_photo else f"VID_{stamp}_00_{i:03d}.mp4"
            size = rnd.randint(6, 14) * 1024**2 if is_photo else rnd.randint(180, 3800) * 1024**2
            d = describe_file(f"/DCIM/Camera01/{name}", size)
            d["lrv"] = None
            d["duration"] = None if is_photo else rnd.randint(8, 420)
            self.files.append(d)

    def connect(self):
        time.sleep(0.8)
        self.connected = True
        self.info = {"model": "Insta360 Ace Pro 2", "firmware": "v1.4.12 (demo)", "battery": 78,
                     "charging": False, "card": "STOR_CS_PASS",
                     "free": 41 * 1024**3, "total": 119 * 1024**3, "temp": 41}

    def disconnect(self):
        self.stop_preview()
        self.connected = False

    def refresh(self):
        pass

    def start_recording(self):
        self.recording_since = time.time()

    def stop_recording(self):
        dur = time.time() - (self.recording_since or time.time())
        self.recording_since = None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        d = describe_file(f"/DCIM/Camera01/VID_{stamp}_00_{len(self.files):03d}.mp4",
                          int(max(dur, 1) * 12 * 1024**2))
        d["lrv"], d["duration"] = None, int(dur)
        self.files.append(d)

    def take_photo(self):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        d = describe_file(f"/DCIM/Camera01/IMG_{stamp}_00_{len(self.files):03d}.jpg", 9 * 1024**2)
        d["lrv"] = None
        self.files.append(d)

    def set_mode(self, mode):
        self.mode = mode

    def start_preview(self):
        with self.lock:
            if not self.preview:
                self.preview = MjpegPipe(["-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30"])
                self.preview.codec = "demo"

    def stop_preview(self):
        with self.lock:
            pipe, self.preview = self.preview, None
        if pipe:
            pipe.close()

    def list_files(self):
        time.sleep(0.4)
        return list(self.files)

    def thumbnail(self, path, lrv):
        h = int(hashlib.md5(path.encode()).hexdigest()[:6], 16)
        hue = h % 360
        src = (f"gradients=size=480x270:c0=0x{h:06x}:c1=0x{(h * 7919) & 0xFFFFFF:06x}:"
               f"x0=0:y0=0:x1=480:y1=270:duration=1,hue=h={hue}")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", src,
               "-frames:v", "1", "-q:v", "5", "-f", "image2", "-vcodec", "mjpeg", "pipe:1"]
        return subprocess.run(cmd, capture_output=True, timeout=20).stdout or None

    def download(self, path, dest, progress, cancel):
        total = next((f["size"] for f in self.files if f["path"] == path), 50 * 1024**2)
        done = 0
        while done < total:
            if cancel.is_set():
                return False
            time.sleep(0.05)
            done = min(total, done + 30 * 1024**2 // 20)
            progress(done, total)
        # Write a small real file so share copies can be tried in demo mode.
        f = next((x for x in self.files if x["path"] == path), None)
        if path.lower().endswith(".jpg"):
            src = ["-f", "lavfi", "-i", "testsrc2=size=4000x3000", "-frames:v", "1"]
        else:
            src = ["-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=30", "-f", "lavfi",
                   "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mp4"]
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *src, str(dest)], timeout=120)
        if f and dest.exists():
            f["size"] = dest.stat().st_size
            progress(f["size"], f["size"])
        return True


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------

class Downloads:
    def __init__(self, camera: BaseCamera, folder: Path):
        self.camera = camera
        self.folder = folder
        self.jobs: dict[str, dict] = {}
        self.order: list[str] = []
        self.cancel_events: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, items: list[dict]) -> None:
        with self.lock:
            for it in items:
                if any(j["path"] == it["path"] and j["state"] in ("queued", "running")
                       for j in self.jobs.values()):
                    continue
                jid = uuid.uuid4().hex[:10]
                self.jobs[jid] = {"id": jid, "kind": "copy", "path": it["path"],
                                  "name": it["path"].rsplit("/", 1)[-1],
                                  "done": 0, "total": it.get("size"), "state": "queued",
                                  "speed": 0, "error": None, "created": time.time()}
                self.cancel_events[jid] = threading.Event()
                self.order.append(jid)
        self.wake.set()

    def cancel(self, jid: str | None) -> None:
        with self.lock:
            ids = [jid] if jid else list(self.jobs)
            for i in ids:
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

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [dict(self.jobs[i]) for i in self.order]

    def downloaded(self) -> dict[str, int]:
        """Files already in the save folder, with their sizes."""
        if not self.folder.exists():
            return {}
        return {p.name: p.stat().st_size for p in self.folder.iterdir() if p.is_file()}

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
                cancel = self.cancel_events[jid]
                self.folder.mkdir(parents=True, exist_ok=True)
                dest = self.folder / job["name"]
                started = time.time()

                def progress(done, total, job=job, started=started):
                    job["done"] = done
                    if total:
                        job["total"] = total
                    job["speed"] = done / max(time.time() - started, 0.1)

                try:
                    ok = self.camera.download(job["path"], dest, progress, cancel)
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
                    else:
                        job["state"], job["error"] = "failed", "The transfer stopped. Check the Wi-Fi link and retry."
                except Exception as exc:
                    job["state"], job["error"] = "failed", str(exc)


# --------------------------------------------------------------------------
# Share copies (small files for WhatsApp / Telegram)
# --------------------------------------------------------------------------

class Exporter:
    """Makes 1080p H.264 copies of videos and 2048px copies of photos."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.out = folder / "Share"
        self.jobs: dict[str, dict] = {}
        self.order: list[str] = []
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    @staticmethod
    def share_name(name: str) -> str:
        stem, ext = os.path.splitext(name)
        return f"{stem}_share{'.jpg' if ext.lower() in ('.jpg', '.jpeg') else '.mp4'}"

    def shared(self) -> set[str]:
        if not self.out.exists():
            return set()
        return {p.name for p in self.out.iterdir() if p.is_file()}

    def add(self, names: list[str]) -> None:
        with self.lock:
            for name in names:
                name = os.path.basename(name)
                src = self.folder / name
                if not src.is_file() or any(j["name"] == name and j["state"] in ("queued", "running")
                                            for j in self.jobs.values()):
                    continue
                jid = uuid.uuid4().hex[:10]
                self.jobs[jid] = {"id": jid, "kind": "share", "name": name, "path": None,
                                  "out": self.share_name(name), "done": 0, "total": 100,
                                  "state": "queued", "speed": 0, "error": None, "created": time.time()}
                self.order.append(jid)
        self.wake.set()

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
            return [dict(self.jobs[i]) for i in self.order]

    @staticmethod
    def _duration(path: Path) -> float:
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30)
            return float(out.stdout.strip())
        except Exception:
            return 0.0

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
                src = self.folder / job["name"]
                self.out.mkdir(parents=True, exist_ok=True)
                dest = self.out / job["out"]
                tmp = dest.with_name(".part-" + dest.name)
                is_photo = dest.suffix == ".jpg"
                if is_photo:
                    args = ["-i", str(src), "-vf", "scale='min(2048,iw)':-2", "-q:v", "3", "-f", "image2"]
                    total = 0.0
                else:
                    total = self._duration(src)
                    args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                            "-vf", "scale=-2:'min(1080,ih)'", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                            "-movflags", "+faststart", "-f", "mp4"]
                cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
                       "-progress", "pipe:1", *args, str(tmp)]
                try:
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
                    if job["state"] == "cancelled":
                        tmp.unlink(missing_ok=True)
                    elif code == 0 and tmp.exists():
                        tmp.replace(dest)
                        job["state"], job["done"] = "done", 100
                        job["size"] = dest.stat().st_size
                    else:
                        tmp.unlink(missing_ok=True)
                        job["state"], job["error"] = "failed", (err.strip().splitlines() or ["Export failed"])[-1]
                except Exception as exc:
                    job["state"], job["error"] = "failed", str(exc)


# --------------------------------------------------------------------------
# Timed recording and interval photos
# --------------------------------------------------------------------------

class Shooting:
    def __init__(self, camera: BaseCamera):
        self.camera = camera
        self.stop_at: float | None = None
        self._timer: threading.Timer | None = None
        self.interval: dict | None = None
        self._interval_stop = threading.Event()

    # timed recording
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

    # interval photos
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
                iv["error"] = f"Stopped: {exc}"
                break
            if iv["count"] and iv["taken"] >= iv["count"]:
                break
            iv["next_at"] = max(iv["next_at"] + iv["every"], time.time())
        iv["running"] = False

    def status(self) -> dict:
        return {"stop_at": self.stop_at, "interval": self.interval}


# --------------------------------------------------------------------------
# Web app
# --------------------------------------------------------------------------

def create_app(camera: BaseCamera, save_dir: Path, demo: bool) -> Flask:
    app = Flask(__name__, static_folder=None)
    downloads = Downloads(camera, save_dir)
    exporter = Exporter(save_dir)
    shooting = Shooting(camera)
    thumb_sem = threading.Semaphore(2)
    files_cache: dict[str, dict] = {}

    def ok(**extra):
        return jsonify({"ok": True, **extra})

    def fail(exc: Exception, code=400):
        msg = str(exc) or exc.__class__.__name__
        cause = getattr(exc, "__cause__", None)
        log.warning("%s (%s)", msg, cause)
        return jsonify({"ok": False, "error": msg}), code

    @app.get("/")
    def index():
        return send_from_directory(APP_DIR / "static", "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(APP_DIR / "static", name)

    @app.get("/api/status")
    def status():
        return jsonify({**camera.status(), **shooting.status(), "demo": demo, "save_dir": str(save_dir),
                        "host": getattr(camera, "host", "demo")})

    @app.post("/api/connect")
    def connect():
        try:
            camera.connect()
            return ok(**camera.status())
        except Exception as exc:
            camera.last_error = str(exc)
            return fail(exc, 503)

    @app.post("/api/disconnect")
    def disconnect():
        camera.disconnect()
        return ok()

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
            return fail(RuntimeError(f"Couldn't {'start' if action == 'start' else 'stop'} recording: {exc}"))

    @app.post("/api/photo")
    def photo():
        try:
            camera.take_photo()
            return ok()
        except Exception as exc:
            return fail(RuntimeError(f"Couldn't take the photo: {exc}"))

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
            return fail(RuntimeError(f"Couldn't start interval photos: {exc}"))

    @app.post("/api/share")
    def share():
        names = (request.json or {}).get("names", [])
        exporter.add(names)
        return ok()

    @app.post("/api/mode")
    def mode():
        m = (request.json or {}).get("mode")
        try:
            camera.set_mode("photo" if m == "photo" else "video")
            return ok(mode=camera.mode)
        except Exception as exc:
            return fail(RuntimeError(f"The camera didn't switch modes: {exc}"))

    @app.post("/api/preview")
    def preview():
        on = bool((request.json or {}).get("on"))
        try:
            camera.start_preview() if on else camera.stop_preview()
            return ok(preview=camera.preview is not None)
        except Exception as exc:
            return fail(exc)

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

    @app.get("/api/files")
    def files():
        try:
            items = camera.list_files()
        except Exception as exc:
            return fail(RuntimeError(f"Couldn't read the camera's memory card: {exc}"))
        have = downloads.downloaded()
        shared = exporter.shared()
        files_cache.clear()
        for it in items:
            local = have.get(it["name"])
            # "Copied" only when the file on this computer is complete.
            it["downloaded"] = local is not None and (not it.get("size") or local == it["size"])
            it["incomplete"] = local is not None and not it["downloaded"]
            it["shared"] = Exporter.share_name(it["name"]) in shared
            files_cache[it["path"]] = it
        items.sort(key=lambda f: (f.get("taken") or "", f["name"]), reverse=True)
        return jsonify({"ok": True, "files": items})

    @app.get("/api/thumb")
    def thumb():
        path = request.args.get("path", "")
        if not path.startswith("/DCIM/"):
            return ("bad path", 400)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{getattr(camera, 'host', 'demo')}{path}".encode()).hexdigest()
        cached = THUMB_DIR / f"{key}.jpg"
        if not cached.exists():
            with thumb_sem:
                if not cached.exists():
                    data = camera.thumbnail(path, (files_cache.get(path) or {}).get("lrv"))
                    if not data:
                        return ("no preview", 404)
                    cached.write_bytes(data)
        return send_from_directory(THUMB_DIR, cached.name, max_age=86400 * 30)

    @app.post("/api/download")
    def download():
        paths = (request.json or {}).get("paths", [])
        items = [files_cache.get(p, {"path": p, "size": None}) for p in paths if p.startswith("/DCIM/")]
        downloads.add(items)
        return ok(jobs=all_jobs())

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
        folder = save_dir / "Share" if (request.json or {}).get("share") else save_dir
        folder.mkdir(parents=True, exist_ok=True)
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ok()

    return app


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def open_window(url: str, prefer_browser: bool) -> bool:
    """Show the UI in a native window. Returns True if a blocking window ran."""
    if not prefer_browser:
        try:
            import webview  # pywebview

            webview.create_window("Ace Studio", url, width=1280, height=820, min_size=(900, 600),
                                  background_color="#14171A")
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
    ap = argparse.ArgumentParser(description="Desktop companion for the Insta360 Ace Pro 2")
    ap.add_argument("--demo", action="store_true", help="use a pretend camera")
    ap.add_argument("--camera", default="192.168.42.1", help="camera IP on its Wi-Fi (default 192.168.42.1)")
    ap.add_argument("--port", type=int, default=8765, help="local port for the interface")
    ap.add_argument("--save-dir", default=str(Path.home() / "Videos" / "Ace Pro 2"),
                    help="where downloaded clips go")
    ap.add_argument("--browser", action="store_true", help="open in the browser instead of a window")
    ap.add_argument("--headless", action="store_true", help="only run the server")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    if not shutil.which("ffmpeg"):
        print("ffmpeg is missing. Install it with:  sudo apt install ffmpeg", file=sys.stderr)
        sys.exit(1)

    camera = DemoCamera() if args.demo else RealCamera(args.camera)
    app = create_app(camera, Path(args.save_dir).expanduser(), args.demo)
    url = f"http://127.0.0.1:{args.port}/"

    server = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False),
        daemon=True,
    )
    server.start()
    time.sleep(0.6)
    print(f"Ace Studio running at {url}")

    try:
        if args.headless or not open_window(url, args.browser):
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        camera.disconnect()


if __name__ == "__main__":
    main()
