"""
Film looks for Ace Studio.

Each look is Ace Studio's own colour style, inspired by a classic film stock
or a camera's colour mode. A look is built as a 3D colour table (a .cube
LUT, the same kind pro editors use), so it can treat each colour on its own:
calm the reds, push skies toward cyan, keep skin tones soft. On top of that
come film grain that sits mostly in the mid-tones, an optional red glow
around bright lights (halation), and a gentle vignette.

The camera can't apply these itself, so Ace Studio applies them to a *new
copy* after a file is on this computer; originals are never changed.

The "inspired" line names the film or camera mode a look is modelled on.
Ace Studio isn't affiliated with any of those makers, and the looks are hand
tuned approximations, not their processing.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

LUT_SIZE = 65
LUT_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ace-studio" / "luts"
LUT_VERSION = "5"


# --------------------------------------------------------------------------
# Colour helpers (work on float arrays shaped (..., 3), sRGB-encoded 0..1)
# --------------------------------------------------------------------------

def _pchip(x: np.ndarray, pts: list[tuple[float, float]]) -> np.ndarray:
    """Smooth, overshoot-free curve through the points (like a curves tool)."""
    px = np.array([p[0] for p in pts], float)
    py = np.array([p[1] for p in pts], float)
    h = np.diff(px)
    d = np.diff(py) / h
    m = np.zeros_like(py)
    for k in range(1, len(px) - 1):
        if d[k - 1] * d[k] > 0:
            w1, w2 = 2 * h[k] + h[k - 1], h[k] + 2 * h[k - 1]
            m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k])
    m[0], m[-1] = d[0], d[-1]
    xc = np.clip(x, px[0], px[-1])
    i = np.clip(np.searchsorted(px, xc) - 1, 0, len(px) - 2)
    t = (xc - px[i]) / h[i]
    h00, h10, h01, h11 = 2*t**3 - 3*t**2 + 1, t**3 - 2*t**2 + t, -2*t**3 + 3*t**2, t**3 - t**2
    return h00 * py[i] + h10 * h[i] * m[i] + h01 * py[i + 1] + h11 * h[i] * m[i + 1]


def luma(c):
    return c[..., 0] * 0.2126 + c[..., 1] * 0.7152 + c[..., 2] * 0.0722


def tone(c, pts):
    """The same curve on all three channels (film-like contrast)."""
    return _pchip(c, pts)


def chan(c, r=None, g=None, b=None):
    out = c.copy()
    for i, pts in enumerate((r, g, b)):
        if pts:
            out[..., i] = _pchip(c[..., i], pts)
    return out


def wb(c, warm=0.0, tint=0.0):
    """warm > 0 adds amber, < 0 adds blue. tint > 0 adds magenta, < 0 green."""
    k = np.array([1 + warm, 1 - tint, 1 - warm])
    return c * k


def sat(c, s):
    L = luma(c)[..., None]
    return L + (c - L) * s


def fade(c, black=0.0, white=1.0):
    return black + c * (white - black)


def split(c, shadows=(0, 0, 0), highlights=(0, 0, 0), balance=0.5):
    """Tint the shadows and the highlights separately."""
    L = luma(c)[..., None]
    ws = np.clip(1 - L / max(balance, 1e-3), 0, 1) ** 1.5
    wh = np.clip((L - balance) / max(1 - balance, 1e-3), 0, 1) ** 1.5
    return c + ws * np.array(shadows) + wh * np.array(highlights)


def _rgb_to_hsv(c):
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    mx, mn = c.max(-1), c.min(-1)
    d = mx - mn
    h = np.zeros_like(mx)
    nz = d > 1e-9
    rc = np.where(nz, (mx - r) / np.where(nz, d, 1), 0)
    gc = np.where(nz, (mx - g) / np.where(nz, d, 1), 0)
    bc = np.where(nz, (mx - b) / np.where(nz, d, 1), 0)
    h = np.where(r == mx, bc - gc, np.where(g == mx, 2.0 + rc - bc, 4.0 + gc - rc))
    h = np.where(nz, (h / 6.0) % 1.0, 0)
    s = np.where(mx > 1e-9, d / np.where(mx > 1e-9, mx, 1), 0)
    return h, s, mx


def _hsv_to_rgb(h, s, v):
    i = np.floor(h * 6.0)
    f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    i = i.astype(int) % 6
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], -1)


def hue(c, adjust: dict[int, tuple[float, float, float]], width: float = 28):
    """Per-colour tweaks: {hue in degrees: (hue shift deg, saturation x, brightness x)}.
    Colours: red 0, orange 30, yellow 55, green 120, cyan 185, blue 220, purple 275, magenta 320.
    Greys are left alone."""
    h, s, v = _rgb_to_hsv(np.clip(c, 0, 1))
    deg = h * 360
    dh = np.zeros_like(h); sm = np.zeros_like(h); vm = np.zeros_like(h); wsum = np.zeros_like(h)
    for center, (shift, smul, vmul) in adjust.items():
        dist = np.abs((deg - center + 180) % 360 - 180)
        w = np.exp(-0.5 * (dist / width) ** 2)
        dh += w * shift; sm += w * (smul - 1); vm += w * (vmul - 1); wsum += w
    norm = np.maximum(wsum, 1.0)   # overlapping colour ranges average instead of stacking
    dh, sm, vm = dh / norm, sm / norm, vm / norm
    amt = np.clip(s * 2.5, 0, 1)  # only act on colours that have colour
    h2 = (deg + dh * amt) / 360 % 1.0
    s2 = np.clip(s * (1 + sm * amt), 0, 1)
    v2 = np.clip(v * (1 + vm * amt), 0, 1.2)
    return _hsv_to_rgb(h2, s2, v2)


def mono(c, r=0.3, g=0.59, b=0.11):
    L = c[..., 0] * r + c[..., 1] * g + c[..., 2] * b
    return np.repeat(L[..., None], 3, -1)


def soft_clip(c):
    """Roll off anything pushed past white instead of hard-clipping it."""
    over = c > 0.9
    return np.where(over, 0.9 + 0.1 * np.tanh((c - 0.9) / 0.1), np.maximum(c, 0))


# --------------------------------------------------------------------------
# The looks
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Look:
    id: str
    code: str               # short code added to file names, e.g. "SN"
    name: str
    family: str
    inspired: str            # what it's modelled on
    style: str               # plain-language description
    ops: tuple = field(repr=False, compare=False)
    grain: float = 0         # 0-10
    grain_size: float = 1.0  # 1 = fine 35 mm, bigger = coarser
    vignette: float = 0      # 0-1
    halation: float = 0      # 0-1, red glow around bright lights
    mono: bool = False
    swatch: tuple = ("#888", "#444")

    def grade(self, c):
        return run_ops(c, self.ops)


def film(c, amount=0.3):
    """Film loses colour in its brightest highlights and deepest shadows."""
    L = luma(np.clip(c, 0, 1))[..., None]
    t_hi = np.clip((L - 0.72) / 0.28, 0, 1)
    t_lo = np.clip(1 - L / 0.12, 0, 1)
    w = amount * (t_hi * t_hi * (3 - 2 * t_hi)) + 0.6 * amount * (t_lo * t_lo * (3 - 2 * t_lo))
    return L + (c - L) * (1 - w)


def bleach(c, amount=0.55):
    L = mono(c, 0.2126, 0.7152, 0.0722)
    return c * (1 - amount) + L * amount


def run_ops(c, ops):
    """Apply a look's recipe (from looks.json). The phone app runs the same recipe."""
    for op in ops:
        name, args = op[0], op[1:]
        if name == "wb":
            c = wb(c, *args)
        elif name == "hue":
            c = hue(c, {int(k): tuple(v) for k, v in args[0].items()}, args[1] if len(args) > 1 else 28)
        elif name == "tone":
            c = tone(c, [tuple(p) for p in args[0]])
        elif name == "split":
            c = split(c, tuple(args[0]), tuple(args[1]), args[2] if len(args) > 2 else 0.5)
        elif name == "sat":
            c = sat(c, args[0])
        elif name == "mono":
            c = mono(c, *args)
        elif name == "bleach":
            c = bleach(c, *args)
        elif name == "film":
            c = film(c, *args)
        elif name == "fade":
            c = fade(c, *args)
        else:
            raise ValueError(f"Unknown look step: {name}")
    return c


RECIPES_FILE = Path(__file__).resolve().parent / "looks.json"
_RECIPES = json.loads(RECIPES_FILE.read_text())

LOOKS: list[Look] = [
    Look(id=d["id"], code=d["code"], name=d["name"], family=d["family"], inspired=d["inspired"], style=d["style"],
         ops=tuple(tuple(o) if not isinstance(o, dict) else o for o in d["ops"]), grain=d.get("grain", 0),
         grain_size=d.get("grain_size", 1.0), vignette=d.get("vignette", 0), halation=d.get("halation", 0),
         mono=d.get("mono", False), swatch=tuple(d.get("swatch", ("#888", "#444"))))
    for d in _RECIPES["looks"]
]
LOOKS_BY_ID = {lk.id: lk for lk in LOOKS}

# Short codes added to the end of file names, e.g. "IMG_1124 SN.jpg" = Soft Negative.
CODES = {lk.id: lk.code for lk in LOOKS}
LOOK_BY_CODE = {v: k for k, v in CODES.items()}


def legend_text() -> str:
    lines = ["Ace Studio film looks", "The letters at the end of each file name say which look it has:", ""]
    lines += [f"{CODES[lk.id]:<3} {lk.name}  (inspired by {lk.inspired})" for lk in LOOKS]
    return "\n".join(lines) + "\n"


def catalog() -> list[dict]:
    return [{"id": lk.id, "code": lk.code, "name": lk.name, "family": lk.family, "inspired": lk.inspired, "style": lk.style,
             "mono": lk.mono, "swatch": list(lk.swatch)} for lk in LOOKS]


# --------------------------------------------------------------------------
# LUT files
# --------------------------------------------------------------------------

def lut_path(look: Look) -> Path:
    """Write the look's .cube file once (cached) and return its path."""
    import inspect
    src = json.dumps(look.ops) + inspect.getsource(hue) + inspect.getsource(split) + inspect.getsource(film)
    key = hashlib.sha1((LUT_VERSION + src).encode()).hexdigest()[:10]
    path = LUT_DIR / f"{look.id}-{key}.cube"
    if path.exists():
        return path
    n = LUT_SIZE
    v = np.linspace(0, 1, n)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")          # red changes fastest in .cube
    grid = np.stack([r, g, b], -1).reshape(-1, 3)
    out = np.clip(soft_clip(look.grade(grid.copy())), 0, 1)
    LUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        fh.write(f'TITLE "Ace Studio {look.name}"\nLUT_3D_SIZE {n}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        np.savetxt(fh, out, fmt="%.6f")
    tmp.replace(path)
    for old in LUT_DIR.glob(f"{look.id}-*.cube"):
        if old != path:
            old.unlink(missing_ok=True)
    return path


def _q(p: Path) -> str:
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


# --------------------------------------------------------------------------
# ffmpeg filter graphs
# --------------------------------------------------------------------------

def filter_chain(look: Look, kind: str, width: int | None = None, scale_to: int | None = None) -> str:
    """One-input, one-output ffmpeg filter graph for a look.

    width: the width of the picture going through the look (for grain and glow size).
    scale_to: if given, resize to this width first.
    """
    w = scale_to or width or 1920
    parts = []
    if scale_to:
        parts.append(f"scale={scale_to}:-2:flags=lanczos")
    # 16 bits per channel so smooth skies don't band
    parts.append(f"format=gbrp16le,lut3d=file='{_q(lut_path(look))}':interp=tetrahedral")
    graph = ",".join(parts)
    if look.halation:
        sigma = max(2.0, w / 160)
        graph += (f",split[base][glow];[glow]curves=all='0/0 0.7/0 1/1',"
                  f"colorchannelmixer=rr=0.45:rg=0.45:rb=0.1:gr=0.06:gg=0.06:gb=0.02:br=0.02:bg=0.02:bb=0.01,"
                  f"gblur=sigma={sigma:.1f}[g2];[base][g2]blend=all_mode=addition:all_opacity={0.9 * look.halation:.2f}")
    if look.grain:
        size = max(0.45, w / 3200) * look.grain_size
        strength = look.grain * (2.6 if kind == "photo" else 1.8)
        flags = "t+u" if kind == "video" else "u"
        graph += (f",split[pic][gr];[gr]format=gray,lut=c0=128,noise=c0s={strength:.1f}:c0f={flags},"
                  f"gblur=sigma={size:.2f},format=gbrp16le[noise];"
                  f"[pic][noise]blend=all_mode=overlay:all_opacity=1")
    graph += ",format=gbrp"
    if look.vignette:
        graph += f",vignette=angle={0.25 + 0.45 * look.vignette:.2f}"
    graph += ",format=yuv420p" if kind == "video" else ",format=rgb24"
    return graph


def preview_jpeg(src_jpeg: bytes, look_id: str, width: int = 640) -> bytes:
    """Render a small preview of a look from JPEG bytes (a thumbnail)."""
    look = LOOKS_BY_ID[look_id]
    chain = filter_chain(look, "photo", scale_to=width)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-i", "pipe:0",
           "-vf", chain, "-frames:v", "1", "-q:v", "3", "-f", "image2", "-vcodec", "mjpeg", "pipe:1"]
    return subprocess.run(cmd, input=src_jpeg, capture_output=True, timeout=60).stdout


def apply_photo(src_path: str, dst_path: str, look_id: str) -> None:
    """Apply a look to a full-size photo, keeping its EXIF data (date, camera)."""
    from PIL import Image, ImageOps

    look = LOOKS_BY_ID[look_id]
    with Image.open(src_path) as im:
        exif = im.getexif()
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        raw = im.tobytes()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-i", "pipe:0", "-vf", filter_chain(look, "photo", width=w),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    out = subprocess.run(cmd, input=raw, capture_output=True, timeout=600)
    if out.returncode != 0 or len(out.stdout) != w * h * 3:
        raise RuntimeError((out.stderr.decode(errors="ignore").strip().splitlines() or ["The look couldn't be applied."])[-1])
    graded = Image.frombytes("RGB", (w, h), out.stdout)
    if 0x0112 in exif:
        exif[0x0112] = 1  # already rotated upright
    kw = {"icc_profile": icc} if icc else {}
    graded.save(dst_path, "JPEG", quality=95, subsampling=0, optimize=True, exif=exif.tobytes(), **kw)


def video_cmd(src_path: str, dst_path: str, look_id: str, size: str) -> list[str]:
    """ffmpeg command for a video with a look. size: 'full' or '1080p'."""
    look = LOOKS_BY_ID[look_id]
    if size == "1080p":
        chain = "scale=-2:'min(1080,ih)':flags=lanczos," + filter_chain(look, "video", width=1920)
    else:
        chain = filter_chain(look, "video", width=3840)
    return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-i", src_path, "-map", "0:v:0", "-map", "0:a:0?", "-vf", chain,
            "-c:v", "libx264", "-preset", "medium" if size == "1080p" else "fast",
            "-crf", "16" if size == "full" else "18", "-tune", "grain" if look.grain >= 5 else "film",
            "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", dst_path]


if __name__ == "__main__":
    print(json.dumps(catalog(), indent=1))
