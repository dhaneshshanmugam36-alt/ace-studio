/* Builds look colour tables in the background so the screen never stutters. */
importScripts("engine.js");
onmessage = (e) => {
  const { id, look, n } = e.data;
  const data = AceEngine.buildLut(look, n);
  postMessage({ id, n, data }, [data.buffer]);
};
