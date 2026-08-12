// ==========================================
// SYNTHLUCIDA Service Worker
// v33 - bumped app cache: index.html now also loads this SW (PWA/SEO),
// so it + favicon.png are added to the app shell
// v35 - REVERTED the v34 Range-passthrough experiment: it caused rapid
// NotSupportedError failures in practice. Root cause: these MP3s live on
// GitHub Releases, which redirects (302) to a time-limited signed URL on
// objects.githubusercontent.com that can differ between requests. Letting
// the browser make multiple native Range requests for the same track means
// each one can land on a different redirected URL, which browsers reject
// stitching together for security reasons. Back to always fetching the
// whole file in one request and serving it from cache - this avoids the
// multi-redirect problem entirely.
// ==========================================

const APP_CACHE_NAME = 'synthlucida-app-v420';
const AUDIO_CACHE_NAME = 'synthlucida-audio-v2'; // separate cache, survives app shell updates

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

  // Always match by plain URL, ignoring any Range header on the incoming request,
  // so we always find (and return) the full cached file if we have it.
  const cached = await cache.match(request.url);
  if (cached) {
    return cached;
  }

  // DŮLEŽITÉ: záměrně NEpředáváme Range hlavičku dál na síť a vždy stahujeme
  // celý soubor jedním požadavkem. Tyhle MP3 jsou na GitHub Releases, což je
  // jen přesměrování (302) na dočasnou podepsanou URL na objects.githubusercontent.com
  // - a ta se může mezi jednotlivými požadavky lišit (jiný token). Když si
  // <audio> element řekne o týž track vícekrát po sobě přes Range (běžné při
  // plynulém přehrávání/seeku), každý dílčí požadavek by mohl skončit na jiné
  // přesměrované URL - a prohlížeč z bezpečnostních důvodů odmítne poskládat
  // audio z kousků, které nepocházejí ze stejné cílové adresy (NotSupportedError,
  // kaskáda rychlých chyb). Jediný spolehlivý fetch celého souboru tohle obchází.
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
