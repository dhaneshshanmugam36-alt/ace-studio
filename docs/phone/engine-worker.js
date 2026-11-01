/* Background work so the screen never stutters: look colour tables and scene analysis. */
importScripts("engine.js");
onmessage = (e) => {
  const m = e.data;
  if (m.type === "analyzeBitmap") {          // a small camera frame: read its pixels here, off the main thread
    const oc = new OffscreenCanvas(m.bitmap.width, m.bitmap.height), ctx = oc.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(m.bitmap, 0, 0); m.bitmap.close();
    const d = ctx.getImageData(0, 0, oc.width, oc.height).data;
    let c = 0; for (let i = 0; i < d.length; i += 4) if (Math.max(d[i], d[i + 1], d[i + 2]) >= 250) c++;
    const analysis = AceEngine.analyze(d, oc.width, oc.height, m.codeToId);
    analysis.clipped = c / (d.length / 4);
    postMessage({ id: m.id, analysis });
    return;
  }
  if (m.type === "analyze") {
    postMessage({ id: m.id, analysis: AceEngine.analyze(m.data, m.w, m.h, m.codeToId) });
    return;
  }
  const data = AceEngine.buildLut(m.look, m.n);
  postMessage({ id: m.id, n: m.n, data }, [data.buffer]);
};
