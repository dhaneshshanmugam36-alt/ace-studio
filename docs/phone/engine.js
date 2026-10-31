/* Ace Looks colour engine: runs the same recipes (looks.json) as the desktop app
   and turns each one into a 3D colour table. Pure functions, no DOM. */
(function (root) {
  "use strict";

  function pchip(pts) {
    const n = pts.length, px = pts.map(p => p[0]), py = pts.map(p => p[1]);
    const h = [], d = [], m = new Array(n).fill(0);
    for (let k = 0; k < n - 1; k++) { h.push(px[k + 1] - px[k]); d.push((py[k + 1] - py[k]) / h[k]); }
    for (let k = 1; k < n - 1; k++) {
      if (d[k - 1] * d[k] > 0) {
        const w1 = 2 * h[k] + h[k - 1], w2 = h[k] + 2 * h[k - 1];
        m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k]);
      }
    }
    m[0] = d[0]; m[n - 1] = d[n - 2];
    return function (x) {
      const xc = Math.min(px[n - 1], Math.max(px[0], x));
      let i = 0;
      while (i < n - 2 && xc > px[i + 1]) i++;
      // numpy searchsorted(left) - 1, clipped: a point exactly on a knot uses the segment before it
      if (i > 0 && xc === px[i]) i = i - 1;
      const t = (xc - px[i]) / h[i];
      const h00 = 2 * t ** 3 - 3 * t ** 2 + 1, h10 = t ** 3 - 2 * t ** 2 + t, h01 = -2 * t ** 3 + 3 * t ** 2, h11 = t ** 3 - t ** 2;
      return h00 * py[i] + h10 * h[i] * m[i] + h01 * py[i + 1] + h11 * h[i] * m[i + 1];
    };
  }

  const luma = (c) => c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
  const clamp = (x, a, b) => Math.min(b, Math.max(a, x));

  function rgb2hsv(r, g, b) {
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    let h = 0;
    if (d > 1e-9) {
      const rc = (mx - r) / d, gc = (mx - g) / d, bc = (mx - b) / d;
      h = r === mx ? bc - gc : g === mx ? 2 + rc - bc : 4 + gc - rc;
      h = ((h / 6) % 1 + 1) % 1;
    }
    return [h, mx > 1e-9 ? d / mx : 0, mx];
  }
  function hsv2rgb(h, s, v) {
    const i6 = Math.floor(h * 6), f = h * 6 - i6, i = ((i6 % 6) + 6) % 6;
    const p = v * (1 - s), q = v * (1 - s * f), t = v * (1 - s * (1 - f));
    return [[v, q, p, p, t, v][i], [t, v, v, q, p, p][i], [p, p, t, v, v, q][i]];
  }

  // Each op returns a function that changes one colour [r, g, b] in place.
  const OPS = {
    wb: (warm = 0, tint = 0) => (c) => { c[0] *= 1 + warm; c[1] *= 1 - tint; c[2] *= 1 - warm; },
    sat: (s) => (c) => { const L = luma(c); for (let i = 0; i < 3; i++) c[i] = L + (c[i] - L) * s; },
    fade: (black = 0, white = 1) => (c) => { for (let i = 0; i < 3; i++) c[i] = black + c[i] * (white - black); },
    tone: (pts) => { const f = pchip(pts); return (c) => { for (let i = 0; i < 3; i++) c[i] = f(c[i]); }; },
    split: (sh, hl, bal = 0.5) => (c) => {
      const L = luma(c);
      const ws = clamp(1 - L / Math.max(bal, 1e-3), 0, 1) ** 1.5, wh = clamp((L - bal) / Math.max(1 - bal, 1e-3), 0, 1) ** 1.5;
      for (let i = 0; i < 3; i++) c[i] += ws * sh[i] + wh * hl[i];
    },
    mono: (r = 0.3, g = 0.59, b = 0.11) => (c) => { const L = c[0] * r + c[1] * g + c[2] * b; c[0] = c[1] = c[2] = L; },
    bleach: (a = 0.55) => (c) => {
      const L = c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
      for (let i = 0; i < 3; i++) c[i] = c[i] * (1 - a) + L * a;
    },
    film: (a = 0.3) => (c) => {
      const L = luma([clamp(c[0], 0, 1), clamp(c[1], 0, 1), clamp(c[2], 0, 1)]);
      const th = clamp((L - 0.72) / 0.28, 0, 1), tl = clamp(1 - L / 0.12, 0, 1);
      const w = a * th * th * (3 - 2 * th) + 0.6 * a * tl * tl * (3 - 2 * tl);
      for (let i = 0; i < 3; i++) c[i] = L + (c[i] - L) * (1 - w);
    },
    hue: (adj, width = 28) => {
      const list = Object.entries(adj).map(([k, v]) => [Number(k), v[0], v[1], v[2]]);
      return (c) => {
        const [h, s, v] = rgb2hsv(clamp(c[0], 0, 1), clamp(c[1], 0, 1), clamp(c[2], 0, 1));
        const deg = h * 360;
        let dh = 0, sm = 0, vm = 0, ws = 0;
        for (const [center, shift, smul, vmul] of list) {
          const dist = Math.abs(((deg - center + 180) % 360 + 360) % 360 - 180);
          const w = Math.exp(-0.5 * (dist / width) ** 2);
          dh += w * shift; sm += w * (smul - 1); vm += w * (vmul - 1); ws += w;
        }
        const norm = Math.max(ws, 1), amt = clamp(s * 2.5, 0, 1);
        const h2 = (((deg + dh / norm * amt) / 360) % 1 + 1) % 1;
        const s2 = clamp(s * (1 + sm / norm * amt), 0, 1), v2 = clamp(v * (1 + vm / norm * amt), 0, 1.2);
        const o = hsv2rgb(h2, s2, v2); c[0] = o[0]; c[1] = o[1]; c[2] = o[2];
      };
    },
  };

  function compile(ops) {
    const fns = ops.map(([name, ...args]) => {
      if (!OPS[name]) throw new Error("Unknown look step " + name);
      return OPS[name](...args);
    });
    return (c) => { for (const f of fns) f(c); return c; };
  }

  const softClip = (x) => (x > 0.9 ? 0.9 + 0.1 * Math.tanh((x - 0.9) / 0.1) : Math.max(x, 0));

  /** Build a 3D colour table: Float32Array of n*n*n*4 (RGBA, red changes fastest). */
  function buildLut(look, n) {
    const f = compile(look.ops), out = new Float32Array(n * n * n * 4), c = [0, 0, 0];
    let o = 0;
    for (let b = 0; b < n; b++) for (let g = 0; g < n; g++) for (let r = 0; r < n; r++) {
      c[0] = r / (n - 1); c[1] = g / (n - 1); c[2] = b / (n - 1);
      f(c);
      out[o++] = clamp(softClip(c[0]), 0, 1); out[o++] = clamp(softClip(c[1]), 0, 1); out[o++] = clamp(softClip(c[2]), 0, 1); out[o++] = 1;
    }
    return out;
  }

  const api = { buildLut, compile };
  if (typeof module !== "undefined") module.exports = api; else root.AceEngine = api;
})(this);
