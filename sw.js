const CACHE_NAME = "ielts-vocab-pwa-v66-20261015-session-part-v1";
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
  "/app.js?v=20261015-session-part-v1",
  "/styles.css?v=20261015-session-part-v1"
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
      Promise.all(keys.filter(key => key.startsWith("ielts-vocab-pwa-") && key !== CACHE_NAME).map(key => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const request = event.request;

  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  // Startup adds a fresh timestamp to every dataset request. Cache those under
  // one stable key instead of accumulating another complete vocabulary copy.
  const cacheUrl = new URL(url);
  cacheUrl.searchParams.delete("ts");
  const cacheKey = cacheUrl.href;
  const cacheable = APP_SHELL.some(path => new URL(path, self.location.origin).pathname === url.pathname)
    || /^\/data\/.+\.json$/i.test(url.pathname)
    || /^\/whoami-assets\//.test(url.pathname);

  event.respondWith(
    fetch(request)
      .then(response => {
        if (cacheable && response.ok) {
          const copy = response.clone();
          event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.put(cacheKey, copy)).catch(() => null));
        }
        return response;
      })
      .catch(() => caches.match(cacheKey).then(cached => {
        if (cached) return cached;
        if (request.mode === "navigate") return caches.match("/index.html");
        return Response.error();
      }))
  );
});
