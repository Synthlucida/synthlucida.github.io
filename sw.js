const CACHE_NAME = 'synthlucida-v1';
const ASSETS_TO_CACHE = [
  './',
  './player.html',
  './manifest.json',
  './icon.png'
  // Zde můžeš přidat i další soubory, např. cesty k tvým .mp3 souborům, pokud je chceš mít offline
];

// Instalace: Uloží soubory do mezipaměti
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
});

// Aktivace: Odstraní staré verze cache
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cache) => {
          if (cache !== CACHE_NAME) return caches.delete(cache);
        })
      );
    })
  );
});

// Fetch: Pokud není internet, podívá se do cache
self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((response) => {
      return response || fetch(event.request);
    })
  );
});