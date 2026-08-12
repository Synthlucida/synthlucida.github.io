// ==========================================
// SYNTHLUCIDA Service Worker
// v33 - bumped app cache: index.html now also loads this SW (PWA/SEO),
// so it + favicon.png are added to the app shell
// v34 - fix: audio fetch handler now forwards the Range header straight
// to the network for un-cached tracks (normal playback), instead of
// always doing a full un-ranged fetch. Only requests WITHOUT a Range
// header (i.e. the explicit DOWNLOAD OFFLINE button) go through the
// cache-and-store path. This fixes unreliable/slow first playback on a
// fresh browser (empty cache), especially for new visitors.
// ==========================================

const APP_CACHE_NAME = 'synthlucida-app-v401';
const AUDIO_CACHE_NAME = 'synthlucida-audio-v1'; // separate cache, survives app shell updates

// App shell files cached on install
const ASSETS_TO_CACHE = [
  './',
  './index.html',
  './player.html',
  './game.html',
  './relax.html',
  './tarot.html',
  './manifest.json',
  './icon.png',
  './favicon.png',
  './logo.jpg'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(APP_CACHE_NAME).then((cache) => {
      console.log('[SW] Caching app shell');
      // Nepoužíváme cache.addAll() - ten je "vše nebo nic": kdyby se
      // nepodařilo stáhnout byť jediný soubor (404, chyba sítě, špatný
      // název/case na GitHub Pages...), celá instalace by selhala a
      // appka by zůstala navždy na staré verzi, i přes zvýšení čísla cache.
      // Místo toho přidáváme soubory jednotlivě a chybu jednoho souboru
      // jen zalogujeme, ale instalaci to nezastaví.
      return Promise.all(
        ASSETS_TO_CACHE.map((url) =>
          cache.add(url).catch((err) => {
            console.log('[SW] Nepodařilo se zacachovat:', url, err);
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
          if (name !== APP_CACHE_NAME && name !== AUDIO_CACHE_NAME) {
            console.log('[SW] Deleting old cache:', name);
            return caches.delete(name);
          }
        })
      );
    })
  );
  self.clients.claim();
});

function isAudioRequest(url) {
  return /\.mp3($|\?)/i.test(url.pathname);
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  if (isAudioRequest(url)) {
    event.respondWith(handleAudioRequest(event.request));
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});

async function handleAudioRequest(request) {
  const cache = await caches.open(AUDIO_CACHE_NAME);

  // Track je už stažený celý (offline) - vrátíme ho z cache. I když
  // <audio> element pošle Range hlavičku (přeskakování v čase), vrátíme
  // celý soubor; prohlížeč si z něj přehraje/posune, co potřebuje.
  const cached = await cache.match(request.url);
  if (cached) {
    return cached;
  }

  // Track NENÍ v cache (typicky čerstvý/inkognito prohlížeč). Pokud jde
  // o BĚŽNÉ PŘEHRÁVÁNÍ, prohlížeč si o daný kus souboru řekne přes Range
  // hlavičku, aby mohl začít hrát rychle a plynule streamovat/přeskakovat.
  // Tu hlavičku NESMÍME zahodit - pošleme request na síť 1:1 tak, jak
  // přišel, a necháme GitHub/CDN a prohlížeč, ať si partial content
  // (206) vyřeší mezi sebou přirozeně, stejně jako bez service workera.
  // Tohle dřív chybělo a způsobovalo nespolehlivé/pomalé první přehrání.
  const rangeHeader = request.headers.get('range');
  if (rangeHeader) {
    return fetch(request);
  }

  // Request BEZ Range hlavičky = typicky explicitní stažení přes
  // DOWNLOAD OFFLINE (downloadCurrentPlaylist() volá fetch(track.src)
  // bez Range) - tady chceme celý soubor stáhnout a rovnou uložit do cache.
  try {
    // Build a clean request with the SAME mode/credentials as the original
    // (important: audio elements load cross-origin files in "no-cors" mode,
    // and we must preserve that or the fetch gets blocked by CORS).
    const cleanRequest = new Request(request.url, {
      method: 'GET',
      mode: request.mode,
      credentials: request.credentials,
      redirect: 'follow'
    });

    const networkResponse = await fetch(cleanRequest);

    // Cache it even if it's an "opaque" response (no CORS headers from the
    // server) - that's normal for cross-origin media and still works fine
    // for playback, we just can't read its bytes in JS.
    if (networkResponse) {
      cache.put(request.url, networkResponse.clone()).catch((err) => {
        console.log('[SW] Could not cache audio:', err);
      });
    }

    return networkResponse;
  } catch (err) {
    return new Response('Offline - this track is not cached.', {
      status: 503,
      statusText: 'Offline',
      headers: { 'Content-Type': 'text/plain' }
    });
  }
}

// ==========================================
// Kliknutí na lokální notifikaci (připomínky) - zavře notifikaci a přepne
// na už otevřenou appku, nebo ji otevře, pokud zrovna neběží.
// ==========================================
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((allClients) => {
      if (allClients.length > 0) {
        return allClients[0].focus();
      }
      return self.clients.openWindow('./player.html');
    })
  );
});
