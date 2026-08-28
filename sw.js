const CACHE_NAME = "ielts-vocab-pwa-v6-20260615-opaque-words-header";

const APP_SHELL = [
  "/",
  "/index.html",
  "/index.simplified.html",
  "/vocab.json",
  "/manifest.webmanifest",
  "/icon.png",
  "/apple-touch-icon.png",
  "/icon-192.png",
  "/icon-512.png",
  "/app.js",
  "/app.simplified.js",
  "/styles.css"
];

self.addEventListener("install", event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL).catch(() => null))
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const request = event.request;

  if (request.method !== "GET") return;

  if (new URL(request.url).pathname.startsWith("/api/")) {
    return;
  }

  event.respondWith(
    fetch(request)
      .then(response => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(request, copy)).catch(() => null);
        return response;
      })
      .catch(() => caches.match(request).then(cached => cached || caches.match("/index.html")))
  );
});
