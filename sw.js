// Service worker: lets the P-mag Configurator open with no internet.
//
// Network first: when online, the latest version is always fetched (so
// changes to the engine table reach everyone straight away) and the saved
// copy is refreshed. When offline, the saved copy is used.
//
// Bump CACHE when the list of files below changes.
const CACHE = 'pmag-configurator-v1';
const NETWORK_TIMEOUT_MS = 4000;
const FILES = ['./', 'index.html', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) return;

  const fromCache = () =>
    caches.match(req, { ignoreSearch: true })
      .then(hit => hit || (req.mode === 'navigate' ? caches.match('./') : undefined));

  const fromNetwork = fetch(req).then(res => {
    if (res.ok) {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(req, copy));
    }
    return res;
  });
  fromNetwork.catch(() => {});   // handled below; avoids an "unhandled rejection" warning

  // Weak Wi-Fi can leave a request hanging; after 4 s use the saved copy
  // (the network request still finishes and refreshes the cache).
  const timeout = new Promise(resolve => setTimeout(resolve, NETWORK_TIMEOUT_MS)).then(fromCache);

  event.respondWith(
    Promise.race([fromNetwork, timeout.then(hit => hit || fromNetwork)])
      .catch(() => fromCache())
      .then(res => res || new Response('Offline, and this page has not been saved yet.', { status: 503 }))
  );
});
