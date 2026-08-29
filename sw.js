// ==========================================
// SYNTHLUCIDA Service Worker
// v34 - app shell (HTML/CSS/JS/ikony) je teď NETWORK-FIRST: vždy se nejdřív
// zkusí čerstvá verze ze sítě a teprve když síť selže (offline), použije se
// poslední zacachovaná verze. Dřív to bylo cache-first, takže dokud se ručně
// nezměnil obsah TOHOTO souboru (a tím se nespustila nová instalace SW),
// appka uživatelům pořád servírovala starou zacachovanou verzi player.html
// atd., i když byl na serveru už nahraný nový soubor.
// Zároveň: cache.put() u audia se teď čeká (await), takže když appka řekne
// "staženo", skladba už je opravdu bezpečně uložená v Cache Storage.
// ==========================================

const APP_CACHE_NAME = 'synthlucida-app-v831';
const AUDIO_CACHE_NAME = 'synthlucida-audio-v1'; // separate cache, survives app shell updates

// App shell files cached on install (a jako offline záloha)
const ASSETS_TO_CACHE = [
  './',
  './index.html',
  './index-cz.html',
  './player.html',
  './game.html',
  './relax.html',
  './draw.html',
  './bio.html',
  './services.html',
  './tarot.html',
  './news.html',
  './manifest.json',
  './privacy_policy.html',
  './draw.html',
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

  // Weather a Progrese mají vlastní PWA scope (viz jejich manifesty) a
  // offline/cache pro ně tenhle SW řešit nemá - necháme jejich requesty
  // projít přímo na síť, bez event.respondWith(). Kdyby jednou dostaly
  // vlastní offline podporu, dostanou vlastní sw.js se scope jen na sebe.
  if (/\/(weather|progrese)/i.test(url.pathname)) {
    return;
  }

  if (isAudioRequest(url)) {
    event.respondWith(handleAudioRequest(event.request));
    return;
  }

  // Vše ostatní (HTML, JS, CSS, ikony...) - NETWORK-FIRST s cache jako
  // offline zálohou, aby se nová verze appky projevila hned při dalším
  // načtení, ne až po ruční změně APP_CACHE_NAME.
  event.respondWith(handleAppShellRequest(event.request));
});

async function handleAppShellRequest(request) {
  const cache = await caches.open(APP_CACHE_NAME);
  try {
    // cache: 'no-cache' vynutí, aby si prohlížeč vždy ověřil u serveru, jestli
    // má nejnovější verzi (podmíněný požadavek), místo aby v rámci
    // Cache-Control max-age vrátil starý soubor rovnou ze svého HTTP cache
    // bez kontaktování serveru.
    const networkResponse = await fetch(request, { cache: 'no-cache' });
    if (networkResponse && networkResponse.ok) {
      cache.put(request, networkResponse.clone()).catch((err) => {
        console.log('[SW] Could not cache app shell file:', err);
      });
    }
    return networkResponse;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}

async function handleAudioRequest(request) {
  const cache = await caches.open(AUDIO_CACHE_NAME);

  // Always match by plain URL, ignoring any Range header on the incoming request,
  // so we always find (and return) the full cached file if we have it.
  const cached = await cache.match(request.url);
  if (cached) {
    return cached;
  }

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
    // IMPORTANT: we now AWAIT this before returning, so that by the time the
    // page's fetch() promise resolves, the file is *guaranteed* to already be
    // fully written into Cache Storage - not just "probably done in the
    // background". Without this await, the page could think a track is
    // downloaded (and show "OFFLINE") a moment before it's actually saved.
    if (networkResponse) {
      try {
        await cache.put(request.url, networkResponse.clone());
      } catch (err) {
        console.log('[SW] Could not cache audio:', err);
      }
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
