// ==========================================
// SYNTHLUCIDA Service Worker
// v29 - přidána offline cache pro audio (MP3) s podporou přetáčení (Range requests)
// ==========================================

const APP_CACHE_NAME = 'synthlucida-app-v30';
const AUDIO_CACHE_NAME = 'synthlucida-audio-v1'; // samostatná cache, aby se nemazala při update appky

// Soubory appky (shell), které se uloží při instalaci
const ASSETS_TO_CACHE = [
  './',
  './player.html',
  './manifest.json',
  './icon.png',
  './logo.jpg'
];

// Instalace - uložení základních souborů appky
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(APP_CACHE_NAME).then((cache) => {
      console.log('[SW] Ukládám app shell do cache');
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
  self.skipWaiting();
});

// Aktivace - smazání starých verzí app cache (audio cache se NIKDY nemaže automaticky)
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((name) => {
          if (name !== APP_CACHE_NAME && name !== AUDIO_CACHE_NAME) {
            console.log('[SW] Mažu starou cache:', name);
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

  // MP3 soubory řešíme vlastní logikou (offline cache + seek podpora)
  if (isAudioRequest(url)) {
    event.respondWith(handleAudioRequest(event.request));
    return;
  }

  // Ostatní soubory appky - klasický cache-first
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});

async function handleAudioRequest(request) {
  const cache = await caches.open(AUDIO_CACHE_NAME);

  // Cache klíčujeme podle URL (bez Range hlaviček), takže vždy najdeme celý stažený soubor
  let fullResponse = await cache.match(request.url);

  if (!fullResponse) {
    try {
      // První stažení - vždy chceme CELÝ soubor (bez Range), ať máme co cachovat
      const networkResponse = await fetch(request.url);
      if (networkResponse && networkResponse.ok) {
        await cache.put(request.url, networkResponse.clone());
        fullResponse = networkResponse;
      } else {
        return networkResponse;
      }
    } catch (err) {
      // Nejsme online a nemáme to stažené
      return new Response('Offline - tato skladba není stažená v cache.', {
        status: 503,
        statusText: 'Offline',
        headers: { 'Content-Type': 'text/plain' }
      });
    }
  }

  // Pokud prohlížeč/audio element žádá konkrétní byte range (kvůli přetáčení),
  // musíme mu z plné cachované odpovědi ten úsek vyříznout ručně,
  // protože Cache API si Range samo neumí poradit.
  const rangeHeader = request.headers.get('range');
  if (rangeHeader) {
    return sliceResponseForRange(fullResponse, rangeHeader);
  }

  return fullResponse;
}

async function sliceResponseForRange(fullResponse, rangeHeader) {
  const buffer = await fullResponse.clone().arrayBuffer();
  const totalLength = buffer.byteLength;

  const match = /bytes=(\d+)-(\d*)/.exec(rangeHeader);
  if (!match) return fullResponse;

  const start = parseInt(match[1], 10);
  const end = match[2] ? parseInt(match[2], 10) : totalLength - 1;
  const chunk = buffer.slice(start, end + 1);

  return new Response(chunk, {
    status: 206,
    statusText: 'Partial Content',
    headers: {
      'Content-Type': fullResponse.headers.get('Content-Type') || 'audio/mpeg',
      'Content-Range': `bytes ${start}-${end}/${totalLength}`,
      'Content-Length': String(chunk.byteLength),
      'Accept-Ranges': 'bytes'
    }
  });
}
