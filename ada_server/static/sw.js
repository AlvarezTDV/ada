const CACHE_PREFIX = "ada-shell-";
const CACHE = `${CACHE_PREFIX}1`;
const SHELL = ["/", "/manifest.webmanifest", "/static/app.js", "/static/styles.css", "/static/icon.svg"];

self.addEventListener("install", (event) => {
  const requests = SHELL.map((path) => new Request(new URL(path, self.location.origin), {
    cache: "reload", credentials: "omit",
  }));
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(requests)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE).map((key) => caches.delete(key)),
  )));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin ||
      url.search || !SHELL.includes(url.pathname)) return;
  event.respondWith(caches.open(CACHE).then((cache) => cache.match(event.request))
    .then((response) => response || fetch(event.request)));
});
