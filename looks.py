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

LUT_SIZE = 33
LUT_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ace-studio" / "luts"
LUT_VERSION = "4"


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
    name: str
    family: str
    inspired: str            # what it's modelled on
    style: str               # plain-language description
    grade: Callable = field(repr=False, compare=False)
    grain: float = 0         # 0-10
    grain_size: float = 1.0  # 1 = fine 35 mm, bigger = coarser
    vignette: float = 0      # 0-1
    halation: float = 0      # 0-1, red glow around bright lights
    mono: bool = False
    swatch: tuple = ("#888", "#444")


def _golden_portrait(c):
    c = wb(c, 0.035, 0.005)
    c = hue(c, {0: (6, 0.88, 1.0), 30: (2, 0.9, 1.04), 120: (-12, 0.78, 1.0), 220: (-10, 0.85, 1.0)})
    c = tone(c, [(0, 0.045), (0.2, 0.2), (0.5, 0.52), (0.8, 0.8), (1, 0.955)])
    c = split(c, (0.0, 0.004, 0.012), (0.012, 0.004, -0.012))
    return sat(c, 0.92)


def _sunny_gold(c):
    c = wb(c, 0.035, -0.005)
    c = hue(c, {0: (4, 1.12, 1.0), 55: (-4, 1.25, 1.05), 120: (-14, 0.9, 1.0), 220: (-6, 0.92, 0.96)})
    c = tone(c, [(0, 0.02), (0.22, 0.2), (0.5, 0.52), (0.78, 0.81), (1, 0.97)])
    c = split(c, (0.004, 0.002, -0.006), (0.014, 0.008, -0.014))
    return sat(c, 1.1)


def _soft_negative(c):
    c = hue(c, {0: (-6, 0.85, 0.96), 55: (-10, 0.7, 1.0), 120: (25, 0.6, 0.94), 220: (-12, 0.85, 0.95)})
    c = tone(c, [(0, 0), (0.18, 0.12), (0.5, 0.5), (0.82, 0.86), (1, 0.97)])
    c = split(c, (-0.02, 0.012, 0.03), (0.03, 0.0, -0.01), 0.45)
    return sat(c, 0.72)


def _muted_chrome(c):
    c = hue(c, {0: (4, 0.68, 0.94), 30: (0, 0.85, 1.0), 120: (-6, 0.72, 0.95), 200: (-10, 0.9, 0.9), 225: (-6, 0.9, 0.92)})
    c = tone(c, [(0, 0.0), (0.18, 0.13), (0.5, 0.49), (0.8, 0.82), (1, 0.97)])
    c = split(c, (0.0, 0.004, 0.012), (0.012, 0.006, -0.008))
    return sat(c, 0.74)


def _velvet_slide(c):
    c = wb(c, 0.0, 0.012)
    c = hue(c, {0: (0, 1.2, 0.98), 55: (-4, 1.2, 1.0), 120: (-6, 1.35, 0.94), 220: (-4, 1.3, 0.9), 300: (0, 1.2, 1.0)})
    c = tone(c, [(0, 0), (0.2, 0.14), (0.5, 0.5), (0.8, 0.86), (1, 1)])
    return sat(c, 1.22)


def _cine_flat(c):
    c = hue(c, {30: (0, 0.95, 1.0), 120: (8, 0.8, 1.0), 220: (-8, 0.85, 1.0)})
    c = tone(c, [(0, 0.06), (0.25, 0.27), (0.5, 0.5), (0.75, 0.72), (1, 0.92)])
    c = split(c, (-0.012, 0.008, 0.02), (0.015, 0.006, -0.008))
    return sat(c, 0.76)


def _positive(c):
    c = wb(c, 0.03, 0.01)
    c = hue(c, {0: (2, 1.25, 1.0), 30: (0, 1.15, 1.03), 55: (-3, 1.2, 1.03), 120: (-8, 1.0, 0.96), 220: (6, 1.05, 0.92)})
    c = tone(c, [(0, 0), (0.22, 0.18), (0.5, 0.52), (0.78, 0.83), (1, 1)])
    return sat(c, 1.12)


def _hard_mono(c):
    c = mono(c, 0.35, 0.55, 0.10)
    return tone(c, [(0, 0), (0.18, 0.05), (0.45, 0.4), (0.7, 0.85), (1, 1)])


def _rangefinder(c):
    c = wb(c, 0.02, 0.0)
    c = hue(c, {0: (2, 1.04, 0.97), 30: (2, 0.95, 1.02), 120: (-4, 0.9, 0.96), 220: (-4, 1.0, 0.95)})
    c = tone(c, [(0, 0), (0.12, 0.08), (0.5, 0.5), (0.88, 0.91), (1, 0.98)])
    return sat(c, 0.97)


def _silver_mono(c):
    c = mono(c, 0.33, 0.52, 0.15)
    return tone(c, [(0, 0), (0.15, 0.1), (0.5, 0.5), (0.85, 0.89), (1, 0.985)])


def _bright_landscape(c):
    c = wb(c, -0.005, 0.0)
    c = hue(c, {0: (0, 1.3, 0.98), 30: (0, 1.25, 1.02), 55: (-2, 1.1, 1.02), 120: (4, 1.15, 0.95), 220: (-4, 1.3, 0.94)})
    c = tone(c, [(0, 0), (0.2, 0.16), (0.5, 0.5), (0.8, 0.84), (1, 0.99)])
    return sat(c, 1.18)


def _night_tungsten(c):
    c = wb(c, -0.07, 0.01)
    c = hue(c, {0: (0, 1.1, 1.0), 30: (-6, 1.05, 1.02), 185: (-5, 1.15, 1.0), 220: (-6, 1.1, 1.0)})
    c = tone(c, [(0, 0.035), (0.2, 0.17), (0.5, 0.5), (0.82, 0.85), (1, 0.97)])
    c = split(c, (-0.01, 0.015, 0.03), (0.02, 0.0, -0.01), 0.4)
    return sat(c, 1.04)


def _iconic_slide(c):
    c = wb(c, 0.02, 0.0)
    c = hue(c, {0: (-4, 1.3, 0.92), 30: (-4, 1.1, 1.0), 55: (-6, 1.0, 1.0), 120: (-10, 0.9, 0.9), 220: (6, 1.2, 0.85)})
    c = tone(c, [(0, 0), (0.2, 0.13), (0.5, 0.48), (0.8, 0.84), (1, 0.98)])
    return sat(c, 1.08)


def _classic_grain(c):
    c = mono(c, 0.42, 0.48, 0.10)
    return tone(c, [(0, 0.02), (0.15, 0.08), (0.5, 0.5), (0.85, 0.9), (1, 0.98)])


def _pastel_portrait(c):
    c = wb(c, -0.01, -0.006)
    c = hue(c, {0: (6, 0.85, 1.02), 30: (0, 0.85, 1.04), 120: (22, 0.8, 1.04), 220: (-8, 0.8, 1.03)})
    c = tone(c, [(0, 0.07), (0.25, 0.33), (0.5, 0.6), (0.78, 0.84), (1, 0.97)])
    c = split(c, (-0.005, 0.01, 0.015), (0.0, 0.006, 0.004))
    return sat(c, 0.82)


def _warm_memory(c):
    c = wb(c, 0.03, 0.0)
    c = hue(c, {0: (6, 1.05, 0.98), 30: (2, 1.1, 1.02), 55: (-6, 1.05, 1.0), 120: (-16, 0.8, 0.96), 220: (-8, 0.85, 0.95)})
    c = tone(c, [(0, 0.03), (0.2, 0.18), (0.5, 0.5), (0.8, 0.8), (1, 0.95)])
    c = split(c, (0.0, 0.006, 0.01), (0.035, 0.015, -0.025), 0.45)
    return sat(c, 0.95)


def _fine_mono(c):
    c = mono(c, 0.25, 0.65, 0.10)
    return tone(c, [(0, 0), (0.15, 0.09), (0.5, 0.49), (0.85, 0.88), (1, 0.99)])


def _negative_snap(c):
    c = wb(c, -0.015, 0.0)
    c = hue(c, {0: (-4, 0.85, 1.0), 55: (-4, 0.85, 1.0), 120: (12, 0.8, 1.0), 220: (-6, 0.9, 1.0)})
    c = tone(c, [(0, 0.04), (0.22, 0.22), (0.5, 0.5), (0.78, 0.78), (1, 0.95)])
    c = split(c, (-0.01, 0.006, 0.015), (0.005, 0.004, 0.0))
    return sat(c, 0.84)


def _bleach_bypass(c):
    L = mono(c, 0.2126, 0.7152, 0.0722)
    c = c * 0.45 + L * 0.55
    return tone(c, [(0, 0), (0.2, 0.12), (0.5, 0.5), (0.8, 0.9), (1, 1)])


def _leica_chrome(c):
    c = wb(c, -0.02, 0.0)
    c = hue(c, {0: (0, 0.92, 0.97), 30: (0, 0.92, 1.0), 120: (4, 0.9, 0.96), 220: (-4, 1.1, 0.9)})
    c = tone(c, [(0, 0), (0.15, 0.1), (0.5, 0.5), (0.85, 0.89), (1, 0.99)])
    return sat(c, 0.92)


def _instant_photo(c):
    c = wb(c, 0.025, -0.01)
    c = hue(c, {0: (6, 0.9, 1.0), 120: (16, 0.75, 1.0), 220: (-10, 0.8, 1.0)})
    c = tone(c, [(0, 0.09), (0.25, 0.27), (0.5, 0.52), (0.8, 0.8), (1, 0.93)])
    c = split(c, (-0.01, 0.025, 0.02), (0.03, 0.02, -0.02), 0.45)
    return sat(c, 0.85)


def _purple_dream(c):
    c = hue(c, {55: (-55, 1.0, 1.0), 95: (-160, 1.15, 0.92), 135: (-170, 1.15, 0.9), 220: (-45, 0.95, 1.0)}, width=24)
    c = tone(c, [(0, 0), (0.2, 0.15), (0.5, 0.5), (0.8, 0.84), (1, 0.99)])
    return sat(c, 1.1)


LOOKS: list[Look] = [
    # Colour negative film
    Look("golden-portrait", "Golden Portrait", "Colour negative", "Kodak Portra 400",
         "Warm, soft and flattering. Gentle contrast, smooth skin tones, calm greens.",
         _golden_portrait, grain=4, swatch=("#E8B27A", "#7A5A44")),
    Look("sunny-gold", "Sunny Gold", "Colour negative", "Kodak Gold 200",
         "Golden-yellow everyday film. Rich reds and yellows, a holiday-snapshot feel.",
         _sunny_gold, grain=5, swatch=("#F2B33D", "#B0452A")),
    Look("bright-landscape", "Bright Landscape", "Colour negative", "Kodak Ektar 100",
         "Vivid, clean and very fine grained. Bold reds and blues, made for scenery.",
         _bright_landscape, grain=1.5, swatch=("#D2452E", "#2A6FB5")),
    Look("pastel-portrait", "Pastel Portrait", "Colour negative", "Fujifilm Pro 400H",
         "Bright and airy pastels with minty greens and soft, cool skin.",
         _pastel_portrait, grain=3.5, swatch=("#BFE3D0", "#E9C9C1")),
    Look("soft-negative", "Soft Negative", "Colour negative", "Fujifilm Classic Negative",
         "Muted colour with hard shadows: teal darks, warm highlights, faded greens.",
         _soft_negative, grain=4.5, swatch=("#8FA7A0", "#B07C6E")),
    Look("warm-memory", "Warm Memory", "Colour negative", "Fujifilm Nostalgic Neg",
         "Amber highlights and soft shadows, like prints from the 70s.",
         _warm_memory, grain=4, swatch=("#E0A458", "#5E6B5C")),
    # Slide and chrome
    Look("muted-chrome", "Muted Chrome", "Slide & chrome", "Fujifilm Classic Chrome",
         "Documentary-magazine look: low saturation, deep shadows, calm reds, cyan skies.",
         _muted_chrome, grain=2.5, swatch=("#7E8C8E", "#3A4448")),
    Look("velvet-slide", "Velvet Slide", "Slide & chrome", "Fujifilm Velvia 50",
         "Very saturated landscape slide film. Deep greens and blues, strong contrast.",
         _velvet_slide, grain=1.5, swatch=("#2E8B57", "#1F4E9A")),
    Look("iconic-slide", "Iconic Slide", "Slide & chrome", "Kodak Kodachrome 64",
         "The classic magazine slide: deep reds, dark blue skies, warm mid-tones.",
         _iconic_slide, grain=2.5, vignette=0.25, swatch=("#C0392B", "#1E3F66")),
    # Cinema
    Look("cine-flat", "Cine Flat", "Cinema", "Fujifilm Eterna",
         "Soft cinema stock: low contrast, gentle colour and a teal shadow. Good to grade further.",
         _cine_flat, grain=2.5, swatch=("#6F8C8A", "#C2A48A")),
    Look("night-tungsten", "Night Tungsten", "Cinema", "CineStill 800T",
         "Cool night film with red glow around lights. Made for city lights and neon.",
         _night_tungsten, grain=5.5, grain_size=1.3, halation=0.8, swatch=("#2F5D7C", "#E0503C")),
    # Compact cameras
    Look("positive", "Positive", "Compact camera", "Ricoh GR Positive Film",
         "Bright, punchy and warm, with strong reds and yellows.",
         _positive, grain=3, swatch=("#E0533A", "#F0C445")),
    Look("negative-snap", "Negative Snap", "Compact camera", "Ricoh GR Negative Film",
         "Soft and slightly cool, with lifted shadows and quiet colour.",
         _negative_snap, grain=3.5, swatch=("#9DB4B8", "#C9B8A6")),
    Look("bleach-bypass", "Bleach Bypass", "Compact camera", "Ricoh GR Bleach Bypass",
         "Silvery, low colour and high contrast. Gritty and dramatic for action.",
         _bleach_bypass, grain=4.5, vignette=0.35, swatch=("#8D9396", "#2B2E30")),
    # Rangefinder
    Look("rangefinder", "Rangefinder Natural", "Rangefinder", "Leica standard colour",
         "Natural and slightly warm, with rich blacks and smooth highlights.",
         _rangefinder, grain=2, vignette=0.3, swatch=("#A9865B", "#2F3A40")),
    Look("leica-chrome", "Rangefinder Chrome", "Rangefinder", "Leica Looks Chrome",
         "Cooler, cleaner and crisper, with deep blues.",
         _leica_chrome, grain=1.5, vignette=0.25, swatch=("#4F6D8A", "#B7B9B5")),
    # Black and white
    Look("silver-mono", "Silver Mono", "Black & white", "Leica Monochrom",
         "Smooth black and white with deep blacks and a wide range of greys.",
         _silver_mono, grain=3, vignette=0.3, mono=True, swatch=("#2A2A2A", "#BDBDBD")),
    Look("fine-mono", "Fine Mono", "Black & white", "Fujifilm Acros",
         "Very fine grain and smooth tones, with rich mid-greys.",
         _fine_mono, grain=2, mono=True, swatch=("#3A3A3A", "#D6D6D6")),
    Look("classic-grain", "Classic Grain", "Black & white", "Kodak Tri-X 400",
         "Gritty documentary black and white with punchy contrast and visible grain.",
         _classic_grain, grain=7, grain_size=1.4, vignette=0.3, mono=True, swatch=("#1E1E1E", "#A8A8A8")),
    Look("hard-mono", "Hard Mono", "Black & white", "Ricoh GR Hi-Contrast B&W",
         "Crushed blacks, bright whites and heavy grain. Bold street style.",
         _hard_mono, grain=8, grain_size=1.5, vignette=0.55, mono=True, swatch=("#111111", "#EEEEEE")),
    # Instant and creative
    Look("instant-photo", "Instant Photo", "Instant & creative", "Polaroid 600",
         "Faded blacks, green-tinted shadows and warm, creamy highlights.",
         _instant_photo, grain=3, vignette=0.45, swatch=("#E8D9B0", "#5F7F72")),
    Look("purple-dream", "Purple Dream", "Instant & creative", "LomoChrome Purple",
         "Dreamy colour swap: greens turn purple, blues turn teal. Best on nature shots.",
         _purple_dream, grain=5, grain_size=1.2, vignette=0.3, swatch=("#8E44AD", "#D4708F")),
]
LOOKS_BY_ID = {lk.id: lk for lk in LOOKS}


def catalog() -> list[dict]:
    return [{"id": lk.id, "name": lk.name, "family": lk.family, "inspired": lk.inspired, "style": lk.style,
             "mono": lk.mono, "swatch": list(lk.swatch)} for lk in LOOKS]


# --------------------------------------------------------------------------
# LUT files
# --------------------------------------------------------------------------

def lut_path(look: Look) -> Path:
    """Write the look's .cube file once (cached) and return its path."""
    import inspect
    src = inspect.getsource(look.grade) + inspect.getsource(hue) + inspect.getsource(split)
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
