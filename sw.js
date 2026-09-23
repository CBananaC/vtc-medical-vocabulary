const CACHE_NAME = "ielts-vocab-pwa-v49-20260923-spell-cooldown-lavender-visual";
const VOCABULARY_MANIFEST_PATH = "/data/vocabulary-manifest.json";

const APP_SHELL = [
  "/",
  "/index.html",
  VOCABULARY_MANIFEST_PATH,
  "/manifest.webmanifest",
  "/icon.png",
  "/apple-touch-icon.png",
  "/icon-192.png",
  "/icon-512.png",
  "/app.js?v=20260923-spell-cooldown-lavender-visual-v1",
  "/styles.css?v=20260923-spell-cooldown-lavender-visual-v1"
];

function manifestDatasetPaths(manifest) {
  const datasets = Array.isArray(manifest?.datasets) ? manifest.datasets : [];
  return datasets
    .map(dataset => String(dataset?.path || "").trim())
    .filter(path => /^data\/[^/]+\/[^/]+\/[^/]+\.json$/i.test(path))
    .map(path => `/${path}`);
}

async function cacheAppShell(cache) {
  await cache.addAll(APP_SHELL);
  const manifestResponse = await cache.match(VOCABULARY_MANIFEST_PATH);
  if (!manifestResponse) return;

  const manifest = await manifestResponse.json();
  const datasetPaths = manifestDatasetPaths(manifest);
  await Promise.all(datasetPaths.map(path => cache.add(path).catch(() => null)));
}

self.addEventListener("install", event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cacheAppShell(cache).catch(() => null))
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
