// ==========================================
// VAPETRACK Service Worker
// Samostatný worker jen pro vapetrack.html a vapetrack-en.html.
// I když leží fyzicky v rootu (stejně jako hlavnisw.js), registruje se
// s scope: '/vapetrack' - scope se porovnává jako prostý prefix textu,
// takže sedne na "/vapetrack.html" i "/vapetrack-en.html", ale ne na
// jiné stránky webu (ty zůstávají pod hlavnisw.js se scope '/').
// Díky delšímu/specifičtějšímu scope vyhrává tento worker nad hlavním
// na vapetrack stránkách - není potřeba nic stěhovat do podsložky.
// ==========================================

const APP_CACHE_NAME = 'vapetrack-app-v1';

// Vše, co appka potřebuje pro plně offline chod (appka sama nemá žádný
// externí JS/CSS - je to single-file HTML, takže stačí HTML + ikony/manifest)
const ASSETS_TO_CACHE = [
  './vapetrack.html',
  './vapetrack-en.html',
  './vapetrack-manifest.json',
  './vapetrack-en-manifest.json',
  './vapetrack-favicon-32.png',
  './vapetrack-favicon-16.png',
  './vapetrack-favicon.ico',
  './vapetrack-apple-touch-icon.png',
  './vapetrack-en-favicon-32.png',
  './vapetrack-en-favicon-16.png',
  './vapetrack-en-favicon.ico',
  './vapetrack-en-apple-touch-icon.png',
  './vapetrack-icon-192.png',
  './vapetrack-icon-512.png',
  './vapetrack-og-image.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(APP_CACHE_NAME).then((cache) => {
      console.log('[SW-vapetrack] Caching app shell');
      // Po jednom, ať chybějící/přejmenovaný soubor nezastaví celou instalaci
      return Promise.all(
        ASSETS_TO_CACHE.map((url) =>
          cache.add(url).catch((err) => {
            console.log('[SW-vapetrack] Nepodařilo se zacachovat:', url, err);
          })
        )
      );
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((name) => {
          if (name !== APP_CACHE_NAME) {
            console.log('[SW-vapetrack] Deleting old cache:', name);
            return caches.delete(name);
          }
        })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Jen pro jistotu - i mimo scope by se sem nic jiného dostat nemělo,
  // ale kdyby se appka rozrostla o vlastní JS/CSS soubory, tahle podmínka
  // je propustí normálně přes network-first logiku níž.
  event.respondWith(handleRequest(event.request, url));
});

async function handleRequest(request, url) {
  const cache = await caches.open(APP_CACHE_NAME);
  try {
    // network-first: vždy zkusit čerstvou verzi, cache jen jako offline záloha
    const networkResponse = await fetch(request, { cache: 'no-cache' });
    if (networkResponse && networkResponse.ok) {
      cache.put(request, networkResponse.clone()).catch((err) => {
        console.log('[SW-vapetrack] Could not cache:', err);
      });
    }
    return networkResponse;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}
