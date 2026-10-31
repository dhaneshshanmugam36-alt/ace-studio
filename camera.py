"""
Camera access for Ace Studio: the Wi-Fi camera, a demo camera, and a camera
plugged in by USB (mass-storage mode).
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

import errno
import socket

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


def friendly(exc: BaseException, action: str = "") -> str:
    """Turn an exception into a message that says what happened and how to fix it."""
    text = str(exc)
    chain, e = [], exc
    while e is not None and len(chain) < 6:
        chain.append(e); e = e.__cause__ or e.__context__
    for e in chain:
        if isinstance(e, OSError) and e.errno == errno.ENOSPC:
            return "This computer's disk is full. Free some space, or choose another folder in Settings."
        if isinstance(e, PermissionError):
            return "Ace Studio can't write to that folder. Choose another folder in Settings."
        if isinstance(e, (ConnectionRefusedError, TimeoutError, socket.timeout)):
            return "The camera isn't answering. Check its Wi-Fi is still on and this computer is joined to it."
        name = type(e).__name__
        if name in ("ConnectionError", "ReadTimeout", "ConnectTimeout", "ChunkedEncodingError"):
            return "The Wi-Fi link to the camera dropped. Copy again and it carries on where it stopped."
        if name == "CameraNotConnectedException":
            return "The camera disconnected. Ace Studio will reconnect when it's back."
    if "did not receive a response" in text or "did not succeed" in text:
        return f"The camera didn't respond{(' to ' + action) if action else ''}. It may be busy; try again in a moment."
    return (f"Couldn't {action}: {text}" if action else text) or "Something went wrong."


CARD_MESSAGES = {
    "STOR_CS_NOCARD": "There's no memory card in the camera.",
    "STOR_CS_NOSPACE": "The camera's memory card is full.",
    "STOR_CS_INVALID_FORMAT": "The camera can't read its memory card. Format it in the camera.",
    "STOR_CS_WPCARD": "The camera's memory card is write-protected.",
    "STOR_CS_OTHER_ERROR": "The camera reports a problem with its memory card.",
}


def photo_thumbnail(data_or_path, width: int = 480) -> bytes | None:
    """Upright JPEG thumbnail of a photo (follows the EXIF rotation)."""
    try:
        import io
        from PIL import Image, ImageOps
        src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
        with Image.open(src) as im:
            im.draft("RGB", (width * 2, width * 2))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((width, width * 2))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=80)
            return out.getvalue()
    except Exception:
        return None


EXIF_TAGS = {0x010F: "make", 0x0110: "model", 0x9003: "taken", 0x829A: "exposure", 0x829D: "aperture",
             0x8827: "iso", 0x920A: "focal_length", 0xA405: "focal_35mm", 0x9204: "ev",
             0xA403: "white_balance", 0x0131: "software"}


def photo_details(data_or_path) -> dict:
    import io
    from PIL import Image
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
    out: dict = {}
    with Image.open(src) as im:
        out["width"], out["height"] = im.size
        exif = im.getexif()
        merged = dict(exif)
        try:
            merged.update(exif.get_ifd(0x8769))
        except Exception:
            pass
        for tag, key in EXIF_TAGS.items():
            v = merged.get(tag)
            if v is None:
                continue
            try:
                if key == "exposure":
                    f = float(v); v = f"1/{round(1 / f)} s" if 0 < f < 1 else f"{f:g} s"
                elif key == "aperture":
                    v = f"f/{float(v):.1f}"
                elif key == "focal_length":
                    v = f"{float(v):.1f} mm"
                elif key == "focal_35mm":
                    v = f"{int(v)} mm"
                elif key == "ev":
                    v = f"{float(v):+.1f} EV"
                elif key == "white_balance":
                    v = "Manual" if int(v) == 1 else "Auto"
                elif isinstance(v, bytes):
                    v = v.decode(errors="ignore").strip("\x00 ")
                else:
                    v = str(v).strip("\x00 ")
            except Exception:
                v = str(v)
            if v:
                out[key] = v
        if 0x0112 in exif and exif[0x0112] in (5, 6, 7, 8):
            out["width"], out["height"] = out["height"], out["width"]
    return out


def video_details(url_or_path: str) -> dict:
    """Duration, size, frame rate and codec of a video (works over HTTP too)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,profile:stream_side_data=rotation",
                        "-of", "json", url_or_path], capture_output=True, text=True, timeout=40)
    data = json.loads(r.stdout or "{}")
    fmt = data.get("format", {})
    out: dict = {}
    if fmt.get("duration"):
        out["duration"] = float(fmt["duration"])
    if fmt.get("bit_rate"):
        out["bitrate"] = int(fmt["bit_rate"])
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and "codec" not in out:
            out["codec"] = {"hevc": "H.265", "h264": "H.264"}.get(st.get("codec_name"), st.get("codec_name"))
            out["width"], out["height"] = st.get("width"), st.get("height")
            num, _, den = str(st.get("r_frame_rate", "0/1")).partition("/")
            try:
                out["fps"] = round(float(num) / float(den or 1), 2)
            except (ValueError, ZeroDivisionError):
                pass
            for sd in st.get("side_data_list", []) or []:
                if abs(int(sd.get("rotation", 0) or 0)) in (90, 270):
                    out["width"], out["height"] = out["height"], out["width"]
        if st.get("codec_type") == "audio":
            out["audio"] = st.get("codec_name")
    if not out and r.stderr:
        raise RuntimeError(r.stderr.strip().splitlines()[-1])
    return out


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
        # "off" | "connected" | "reconnecting"
        self.link = "off"
        self.user_disconnected = False    # pressed Disconnect: don't auto-connect
        self.preview_wanted = False       # turn live view back on after a reconnect
        self.clock_mode = "computer"      # "computer" | "leave"
        self.drop_count = 0

    kind = "wifi"

    def reachable(self) -> bool:
        return False

    def on_dropped(self) -> None:
        """Called when the link to the camera is lost without the user asking."""
        if self.user_disconnected:
            self.link = "off"
            return
        self.drop_count += 1
        self.preview_wanted = self.preview is not None or self.preview_wanted
        pipe, self.preview = self.preview, None
        if pipe:
            pipe.close()
        self.connected = False
        self.link = "reconnecting"

    def status(self) -> dict:
        card = self.info.get("card")
        return {
            "connected": self.connected,
            "link": "connected" if self.connected else self.link,
            "card_message": CARD_MESSAGES.get(card) if card else None,
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
            if self.client is not None:          # leftover from a dropped link
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None
            client = Client(host=self.host, callback=self._on_notification, enable_hwaccel=False)
            # Ace Studio handles reconnecting itself (faster than the library's 30 s retry).
            client._on_should_reconnect = lambda: None
            # The library sets the camera clock with a GMT offset of 0 (UTC). Send this
            # computer's real offset instead, or leave the camera's clock alone.
            _sync = client.sync_local_time_to_camera
            if self.clock_mode == "leave":
                client.sync_local_time_to_camera = lambda *a, **k: -1
            else:
                _offset = int(datetime.now().astimezone().utcoffset().total_seconds())
                client.sync_local_time_to_camera = lambda *a, **k: _sync(*a, seconds_from_gmt=_offset, **k)

            @client.on_event("video_stream", uid="ace-studio-preview", wait=True)
            async def _video(data=None, **_):
                pipe = self.preview
                if pipe is not None and data:
                    pipe.write(data)

            @client.on_event("disconnect", uid="ace-studio-disconnect")
            async def _gone(**_):
                if self.client is client and self.connected:
                    log.info("Camera link dropped")
                    self.on_dropped()

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
            self.link = "connected"
            self.user_disconnected = False
            try:
                self.refresh()
            except Exception as exc:
                log.warning("Couldn't read camera status: %r", exc)
            self._poller = threading.Thread(target=self._poll, daemon=True)
            self._poller.start()

    def disconnect(self) -> None:
        with self.lock:
            self.user_disconnected = True
            self.preview_wanted = False
            self.stop_preview()
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
            self.client = None
            self.connected = False
            self.link = "off"

    def reachable(self) -> bool:
        try:
            socket.create_connection((self.host, 6666), timeout=1).close()
            return True
        except OSError:
            return False

    def _poll(self) -> None:
        while self.connected:
            time.sleep(6)
            if not self.connected:
                break
            try:
                self.refresh()
            except Exception as exc:
                log.debug("status poll failed: %s", exc)
            # No packets for a while but no disconnect event: treat as dropped.
            conn = getattr(self.client, "_connection", None)
            if self.connected and conn is not None and time.time() - conn.last_pkt_recv_time > 15:
                log.info("No data from the camera for 15 s; reconnecting")
                self.on_dropped()

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

    def delete_files(self, items: list[dict]) -> list[str]:
        """Delete files (and their low-res .lrv previews) from the camera's card.
        Returns the paths that are still there afterwards."""
        from lib_one_proto import delete_files_pb2

        client = self._require()
        code = 12  # PHONE_COMMAND_DELETE_FILES
        client.pb_msg_classes.setdefault(code, delete_files_pb2.DeleteFiles)
        client.pb_resp_classes.setdefault(code, delete_files_pb2.DeleteFilesResp)
        uri_map = getattr(self, "uri_map", {})
        targets = []
        for it in items:
            targets.append(it["path"])
            if it.get("lrv"):
                targets.append(it["lrv"])
        for i in range(0, len(targets), 50):
            batch = [uri_map.get(p, p) for p in targets[i:i + 50]]
            log.info("Deleting from camera: %s", batch)
            seq = client._send_message({"uri": batch}, code)
            client._check_if_command_successful(seq, wait_for_seconds=20)
        # The camera answers errors the same way as success, so check the card again.
        left = {p for p, _ in self._list_via_protocol()}
        return [it["path"] for it in items if it["path"] in left]

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
        uri_map = {}
        for u in uris:
            p = urlparse(u).path if "://" in u else u
            if not p.startswith("/"):
                p = "/" + p
            paths.append(p)
            uri_map[p] = u
        self.uri_map = uri_map
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

    def fetch(self, path: str) -> bytes:
        import requests
        r = requests.get(self.base_url() + path, timeout=30)
        r.raise_for_status()
        return r.content

    def details(self, path: str) -> dict:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            return photo_details(self.fetch(path))
        if ext in VIDEO_EXT:
            return video_details(self.base_url() + path)
        return {}

    def thumbnail(self, path: str, lrv: str | None) -> bytes | None:
        src = self.base_url() + (lrv or path)
        is_video = os.path.splitext(path)[1].lower() in VIDEO_EXT
        if os.path.splitext(path)[1].lower() in {".dng", ".insp"}:
            return None
        if not is_video:
            try:
                thumb = photo_thumbnail(self.fetch(path))
                if thumb:
                    return thumb
            except Exception:
                pass
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

    available = False   # becomes True after the first manual connect

    def reachable(self):
        return self.available and time.time() > getattr(self, "_down_until", 0)

    def simulate_drop(self, seconds: float = 5) -> None:
        self._down_until = time.time() + seconds
        self.on_dropped()

    def delete_files(self, items):
        gone = {it["path"] for it in items}
        self.files = [f for f in self.files if f["path"] not in gone]
        return []

    def details(self, path):
        f = next((x for x in self.files if x["path"] == path), None) or {}
        if f.get("kind") == "photo":
            return {"width": 8192, "height": 6144, "make": "Arashi Vision", "model": "Insta360 Ace Pro 2",
                    "taken": (f.get("taken") or "").replace("T", " "), "exposure": "1/250 s", "aperture": "f/2.6",
                    "iso": "100", "focal_length": "2.7 mm", "focal_35mm": "16 mm", "white_balance": "Auto"}
        return {"duration": f.get("duration") or 0, "width": 3840, "height": 2160, "fps": 30.0,
                "codec": "H.265", "bitrate": 120_000_000, "audio": "aac"}

    def connect(self):
        time.sleep(0.8)
        self.available = True
        self.user_disconnected = False
        self.link = "connected"
        self.connected = True
        self.info = {"model": "Insta360 Ace Pro 2", "firmware": "v1.4.12 (demo)", "battery": 78,
                     "charging": False, "card": "STOR_CS_PASS",
                     "free": 41 * 1024**3, "total": 119 * 1024**3, "temp": 41}

    def disconnect(self):
        self.user_disconnected = True
        self.preview_wanted = False
        self.stop_preview()
        self.connected = False
        self.link = "off"

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


class UsbSource:
    """A camera plugged in by USB cable, which shows up as a memory card (U-Disk mode)."""

    kind = "usb"

    def __init__(self, root: Path):
        self.root = root
        self.label = root.name

    @staticmethod
    def find() -> "UsbSource | None":
        override = os.environ.get("ACE_USB_ROOT")
        candidates: list[Path] = []
        if override:
            candidates.append(Path(override))
        user = os.environ.get("USER") or Path.home().name
        for base in (Path("/media") / user, Path("/run/media") / user, Path("/media")):
            try:
                candidates += [p for p in base.iterdir() if p.is_dir()]
            except OSError:
                pass
        for c in candidates:
            dcim = c / "DCIM"
            try:
                if dcim.is_dir() and any(NAME_RE.match(f.name) for d in dcim.iterdir() if d.is_dir()
                                         for f in list(d.iterdir())[:50]):
                    return UsbSource(c)
            except OSError:
                continue
        return None

    def local(self, path: str) -> Path:
        p = (self.root / path.lstrip("/")).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("bad path")
        return p

    def list_files(self) -> list[dict]:
        found = []
        for dirpath, _, filenames in os.walk(self.root / "DCIM"):
            for fn in filenames:
                fp = Path(dirpath) / fn
                rel = "/" + str(fp.relative_to(self.root))
                try:
                    found.append((rel, fp.stat().st_size))
                except OSError:
                    pass
        all_paths = {p for p, _ in found}
        files = []
        for path, size in found:
            d = describe_file(path, size)
            if d:
                d["lrv"] = RealCamera._find_lrv(path, all_paths)
                files.append(d)
        return files

    def delete_files(self, items: list[dict]) -> list[str]:
        failed = []
        for it in items:
            try:
                self.local(it["path"]).unlink()
                if it.get("lrv"):
                    try:
                        self.local(it["lrv"]).unlink()
                    except OSError:
                        pass
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("Couldn't delete %s: %s", it["path"], exc)
                failed.append(it["path"])
        try:
            os.sync()
        except Exception:
            pass
        return failed

    def thumbnail(self, path: str, lrv: str | None) -> bytes | None:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            return photo_thumbnail(str(self.local(path)))
        if ext not in VIDEO_EXT:
            return None
        src = str(self.local(lrv or path))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", src, "-frames:v", "1",
               "-vf", "scale=480:-2", "-q:v", "5", "-f", "image2", "-vcodec", "mjpeg", "pipe:1"]
        try:
            return subprocess.run(cmd, capture_output=True, timeout=40).stdout or None
        except subprocess.TimeoutExpired:
            return None

    def details(self, path: str) -> dict:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            return photo_details(str(self.local(path)))
        if ext in VIDEO_EXT:
            return video_details(str(self.local(path)))
        return {}

    def download(self, path: str, dest: Path, progress, cancel: threading.Event) -> bool:
        src = self.local(path)
        total = src.stat().st_size
        tmp = dest.with_name(".part-" + dest.name)
        done = 0
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                if cancel.is_set():
                    fo.close(); tmp.unlink(missing_ok=True)
                    return False
                chunk = fi.read(4 * 1024 * 1024)
                if not chunk:
                    break
                fo.write(chunk)
                done += len(chunk)
                progress(done, total)
        tmp.replace(dest)
        try:
            shutil.copystat(src, dest)
        except OSError:
            pass
        return True
