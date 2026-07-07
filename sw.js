// ==========================================
// SYNTHLUCIDA Service Worker
// v30 - offline cache for audio (MP3), CORS-safe (no manual byte slicing)
// ==========================================

const APP_CACHE_NAME = 'synthlucida-app-v65';
const AUDIO_CACHE_NAME = 'synthlucida-audio-v1'; // separate cache, survives app shell updates

// App shell files cached on install
const ASSETS_TO_CACHE = [
  './',
  './player.html',
  './game.html',
  './relax.html',
  './manifest.json',
  './icon.png',
  './logo.jpg'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(APP_CACHE_NAME).then((cache) => {
      console.log('[SW] Caching app shell');
      return cache.addAll(ASSETS_TO_CACHE);
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
