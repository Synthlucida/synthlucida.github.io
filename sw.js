const CACHE_NAME = 'synthlucida-v28';

// Seznam souborů, které se uloží do paměti telefonu pro rychlé načítání
const ASSETS_TO_CACHE = [
  './',
  './player.html',
  './manifest.json',
  './icon.png',
  './logo.jpg'
];

// Instalace Service Workeru a uložení souborů do mezipaměti
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('Ukládám do cache');
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
});

// Aktivace a promazání staré cache (když v budoucnu změníš v1 na v2)
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cache) => {
          if (cache !== CACHE_NAME) {
            console.log('Mažu starou cache', cache);
            return caches.delete(cache);
          }
        })
      );
    })
  );
});

// Získávání souborů (nejprve zkusí cache, pak internet)
self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((response) => {
      // Pokud soubor najde v paměti, vrátí ho hned. Jinak ho stáhne z netu.
      return response || fetch(event.request);
    })
  );
});