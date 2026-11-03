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
    chan: (r, g, b) => { const f = [r, g, b].map(p => p ? pchip(p) : null); return (c) => { for (let i = 0; i < 3; i++) if (f[i]) c[i] = f[i](c[i]); }; },
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

  const smooth = (e0, e1, x) => { const t = clamp((x - e0) / (e1 - e0), 0, 1); return t * t * (3 - 2 * t); };
  function skinMask(r, g, b) {
    const [h, s, v] = rgb2hsv(clamp(r, 0, 1), clamp(g, 0, 1), clamp(b, 0, 1));
    const dist = Math.abs(((h * 360 - 22 + 180) % 360 + 360) % 360 - 180);
    return Math.exp(-0.5 * (dist / 14) ** 2) * smooth(0.08, 0.18, s) * (1 - smooth(0.6, 0.75, s)) * smooth(0.15, 0.3, v);
  }
  const srgbToLin = (c) => { c = Math.max(c, 0); return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };

  /** Auto fix settings + scene + suggested looks, from a small RGBA image (like the desktop app). */
  function analyze(data, w, h, codeToId) {
    const n = w * h, L = new Float64Array(n), mx = new Float64Array(n), S = new Float64Array(n), lin = new Float64Array(n * 3);
    let skin = 0, green = 0, sky = 0, skyN = 0, ssum = 0;
    const topRows = Math.max(1, Math.floor(h / 3));
    for (let i = 0; i < n; i++) {
      const r = data[i * 4] / 255, g = data[i * 4 + 1] / 255, b = data[i * 4 + 2] / 255;
      const lr = srgbToLin(r), lg = srgbToLin(g), lb = srgbToLin(b);
      lin[i * 3] = lr; lin[i * 3 + 1] = lg; lin[i * 3 + 2] = lb;
      L[i] = lr * 0.2126 + lg * 0.7152 + lb * 0.0722; mx[i] = Math.max(lr, lg, lb);
      const [hh, s, v] = rgb2hsv(r, g, b); S[i] = s; ssum += s;
      const deg = hh * 360;
      if (skinMask(r, g, b) > 0.5) skin++;
      if (deg > 70 && deg < 165 && s > 0.25 && v > 0.15) green++;
      if (i < topRows * w) { skyN++; if (deg > 185 && deg < 245 && s > 0.15 && v > 0.35) sky++; }
    }
    const pct = (arr, q) => { const a = Float64Array.from(arr).sort(); const k = (a.length - 1) * q / 100, f = Math.floor(k), c = Math.min(a.length - 1, f + 1); return a[f] + (a[c] - a[f]) * (k - f); };
    const p50 = pct(L, 50), p995 = pct(mx, 99.5), p20 = pct(L, 20), p95 = pct(L, 95);
    skin /= n; green /= n; sky = sky / skyN;
    const night = p50 < 0.025, dull = ssum / n < 0.16;
    let gain = clamp(0.18 / Math.max(p50, 1e-4), 0.8, 2.0);
    if (night) gain = Math.min(gain, 1.15);
    gain = Math.min(gain, Math.max(1.0, 0.97 / Math.max(p995, 1e-4)));
    gain = gain ** 0.6;
    let cnt = 0; const avg = [0, 0, 0], all = [0, 0, 0];
    for (let i = 0; i < n; i++) {
      for (let k = 0; k < 3; k++) all[k] += lin[i * 3 + k];
      if (S[i] < 0.25 && L[i] > p20 && L[i] < p95) { cnt++; for (let k = 0; k < 3; k++) avg[k] += lin[i * 3 + k]; }
    }
    const A = cnt > 50 ? avg.map(x => x / cnt) : all.map(x => x / n);
    const mean = (A[0] + A[1] + A[2]) / 3;
    let wbm = A.map(x => clamp((mean / Math.max(x, 1e-4)) ** 0.5, 0.88, 1.14));
    const lw = wbm[0] * 0.2126 + wbm[1] * 0.7152 + wbm[2] * 0.0722; wbm = wbm.map(x => x / lw);
    const indoor = A[0] / Math.max(A[2], 1e-4) > 1.3 && !night;
    const S_ = { night: ["NT", "EM", "CD", "HM"], people: ["GP", "DP", "PP", "WM"], landscape: ["BL", "VI", "VS", "EG"],
      sky: ["BL", "SR", "MC", "VI"], indoor: ["CC", "WM", "GP", "SN"], dull: ["SN", "MC", "CD", "ST"], everyday: ["GP", "MC", "WE", "CC"] };
    let tags = [["night", night], ["people", skin > 0.03 && skin < 0.25], ["landscape", green > 0.2], ["sky", sky > 0.25], ["indoor", indoor], ["dull", dull]].filter(t => t[1]).map(t => t[0]);
    if (!tags.length) tags = ["everyday"];
    let order = [];
    if (tags.includes("people") && (tags.includes("landscape") || tags.includes("sky"))) {
      const other = tags.includes("landscape") ? "landscape" : "sky";
      order = S_.people.slice(0, 2).concat(S_[other].slice(0, 2));
    }
    for (const t of tags.concat(["everyday"])) order = order.concat(S_[t]);
    const picks = [...new Set(order)].slice(0, 4);
    return { mul: wbm.map(x => Math.round(gain * x * 1e4) / 1e4), tags, suggest: codeToId ? picks.map(c => codeToId[c]).filter(Boolean) : picks };
  }

  /* Print stage: like film paper, very bright or very saturated colours fade gently into the
     edge of what a photo can show (keeping their hue) instead of being clipped flat. */
  function finish(c) {
    const L = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    const Lc = L > 0.9 ? 0.9 + 0.1 * Math.tanh((L - 0.9) / 0.1) : Math.max(L, 0);
    let r = 0;
    for (let i = 0; i < 3; i++) {
      const d = c[i] + (Lc - L) - Lc;                                   // colour, measured from grey
      const room = d > 0 ? 1 - Lc : Lc;                                 // how far it may go before clipping
      if (d !== 0) r = Math.max(r, Math.abs(d) / Math.max(room, 1e-6));
    }
    const t = 0.8, k = r > t ? (t + (1 - t) * Math.tanh((r - t) / (1 - t))) / r : 1;
    for (let i = 0; i < 3; i++) c[i] = clamp(Lc + (c[i] + (Lc - L) - Lc) * k, 0, 1);
    return c;
  }

  /** Build a 3D colour table: Float32Array of n*n*n*4 (RGBA, red changes fastest). */
  function buildLut(look, n) {
    const f = compile(look.ops), out = new Float32Array(n * n * n * 4), c = [0, 0, 0], prot = look.skin || 0;
    let o = 0;
    for (let b = 0; b < n; b++) for (let g = 0; g < n; g++) for (let r = 0; r < n; r++) {
      c[0] = r / (n - 1); c[1] = g / (n - 1); c[2] = b / (n - 1);
      const x0 = r / (n - 1), x1 = g / (n - 1), x2 = b / (n - 1);
      f(c);
      if (prot > 0) {
        const m = skinMask(x0, x1, x2) * prot;
        if (m > 0) {
          const lx = x0 * 0.2126 + x1 * 0.7152 + x2 * 0.0722, lg = c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
          const k = lg / Math.max(lx, 1e-4);
          c[0] = c[0] * (1 - m) + x0 * k * m; c[1] = c[1] * (1 - m) + x1 * k * m; c[2] = c[2] * (1 - m) + x2 * k * m;
        }
      }
      finish(c);
      out[o++] = c[0]; out[o++] = c[1]; out[o++] = c[2]; out[o++] = 1;
    }
    return out;
  }

  const api = { buildLut, compile, analyze, skinMask };
  if (typeof module !== "undefined") module.exports = api; else root.AceEngine = api;
})(this);
