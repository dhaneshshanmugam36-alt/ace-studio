// Ace Looks service worker: works offline, and receives photos shared from the gallery.
const CACHE = "ace-looks-v1";
const SHELL = ["./", "index.html", "engine.js", "looks.json", "manifest.webmanifest", "icon.svg", "icon-192.png", "icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== "ace-looks-shared").map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method === "POST" && url.pathname.endsWith("/share")) {
    e.respondWith((async () => {
      const form = await e.request.formData();
      const c = await caches.open("ace-looks-shared");
      let i = 0;
      for (const f of form.getAll("photos")) {
        if (!(f instanceof File)) continue;
        await c.put(new Request(`./shared/${i++}/${encodeURIComponent(f.name || "photo.jpg")}`), new Response(f, { headers: { "Content-Type": f.type || "image/jpeg" } }));
      }
      return Response.redirect("./?shared=1", 303);
    })());
    return;
  }
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // Network first (so updates arrive), falling back to the saved copy when offline.
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request, { ignoreSearch: true }).then((r) => r || caches.match("index.html"))));
});
