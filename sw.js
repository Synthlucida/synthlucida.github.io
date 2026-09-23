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
// Přidán SEOCHECKER (seochecker.html / seochecker-en.html) do seznamu appek
// vyloučených z tohoto master SW - má vlastní manifest a žádnou offline
// podporu, ať se s tímhle SW (a jeho cachí pro player) nijak nekříží.
// Přidán Dělňas EN (delnas-en.html) výslovně do seznamu vyloučených appek
// (CZ delnas.html tam už byl) a nově se vůbec nezachytávají požadavky na
// analytiku GoatCounter (gc.zgo.at, *.goatcounter.com) - tenhle SW má scope
// na celý web, takže jinak by řídil i počítání návštěv z Dělňasu.
// v35 - vyloučena i anglická Míchárna (e-liquid-mixing-calculator*.html /
// -manifest.json), která se dosud cachovala, protože seznam znal jen českou
// michani-liquidu. A nově jdou rovnou na síť (bez cache) i požadavky, které
// odcházejí ZE stránek vyloučených appek na cizí servery (Google Fonts, jsPDF
// z CDN apod.) - dřív se jejich písma ukládala do synthlucida-app cache. Číslo cache
// zvýšeno, aby se při aktualizaci smazaly už zacachované soubory těchto appek.
// v36 - vyloučeny i SEO landingy appek, které se jmenují jinak než appka
// (landing_vapetrack, landing_weather, landing_webzenith… = vše "landing_*",
// xtally-landing(-cz), relax-landing(-cz)). Cache znovu zvýšena, aby se smazaly.
// Při aktivaci se teď mažou jen staré verze VLASTNÍCH cachí (synthlucida-app-*,
// synthlucida-audio-*) - dřív se smazalo úplně všechno na synthlucida.com, tedy
// i cache jiných appek s vlastním SW. POST a jiné ne-GET požadavky (formuláře)
// jdou rovnou na síť. Odstraněn duplicitní draw.html v seznamu souborů.
// ==========================================

const APP_CACHE_NAME = 'synthlucida-app-v992';
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
          // Mazat jen staré verze vlastních cachí - cache jiných appek na stejné
          // doméně (s vlastním service workerem) nechat být.
          const isOwnOld =
            (name.startsWith('synthlucida-app-') && name !== APP_CACHE_NAME) ||
            (name.startsWith('synthlucida-audio-') && name !== AUDIO_CACHE_NAME);
          if (isOwnOld) {
            console.log('[SW] Deleting old cache:', name);
            return caches.delete(name);
          }
        })
      );
    })
  );
  self.clients.claim();
});

// Appky s vlastním manifestem / bez offline podpory, které tenhle master SW
// nemá vůbec řešit. Shoduje se se začátkem názvu souboru, takže pokrývá i jejich
// landingy, zásady, manifesty a obrázky (tripcost-landing.html, michani-liquidu-og.png...).
// "landing_" pokrývá všechny landingy pojmenované landing_něco.html.
const EXCLUDED_APPS = /\/(weather|progrese|denik-vozidla|tripcost|webzen|vyplata|michani-liquidu|e-liquid-mixing-calculator|vodovaha|vodovaha-en|vapetrack|vapetrack-en|pohadkovnik|delnas|delnas-en|seochecker|seochecker-en|landing_|xtally-landing|relax-landing)/i;

function isAudioRequest(url) {
  return /\.mp3($|\?)/i.test(url.pathname);
}

self.addEventListener('fetch', (event) => {
  // Jen GET se dá cachovat - odeslání formulářů apod. (POST) nechat jít rovnou na síť.
  if (event.request.method !== 'GET') {
    return;
  }

  const url = new URL(event.request.url);

  // Weather, Progrese, Deník vozidla, Tripcost, Webzen, Míchárna (CZ/EN), Dělňas (CZ/EN) a SEOCHECKER mají
  // vlastní scope (nebo žádnou offline podporu) a tenhle master SW to řešit
  // nemá - necháme jejich requesty projít přímo na síť, bez event.respondWith().
  // Bez tohohle vyloučení by je totiž handleAppShellRequest() tiše
  // zachytával a plnil jimi synthlucida-app cache, i když s playerem
  // vůbec nesouvisí. Kdyby jednou dostaly vlastní offline podporu,
  // dostanou vlastní sw.js se scope jen na sebe.
  if (EXCLUDED_APPS.test(url.pathname)) {
    return;
  }

  // Analytika (GoatCounter: skript z gc.zgo.at + počítání na *.goatcounter.com)
  // - nikdy nezachytávat ani necachovat, ať každé počítání jde rovnou na síť.
  // Hlídá se podle domény, protože tyhle požadavky odcházejí ze stránek appek
  // (např. Dělňasu), ale jejich vlastní URL žádné jméno appky neobsahuje.
  if (/(^|\.)(goatcounter\.com|zgo\.at)$/i.test(url.hostname)) {
    return;
  }

  // Požadavky, které odcházejí ZE stránky vyloučené appky (Google Fonts, jsPDF
  // z CDN apod.) - poslat rovnou na síť bez cache, jinak by se jejich soubory
  // ukládaly do synthlucida-app cache. Stránka se pozná podle klienta (okna),
  // ne podle Referer hlavičky - ta u cizích serverů obsahuje jen doménu.
  // Navigace (přechod na jinou stránku) se neřeší, aby se např. player.html
  // otevřený z TRIP COSTu dál normálně cachoval.
  if (event.request.mode !== 'navigate' && event.clientId) {
    event.respondWith((async () => {
      const client = await self.clients.get(event.clientId);
      if (client && EXCLUDED_APPS.test(new URL(client.url).pathname)) {
        return fetch(event.request);
      }
      return isAudioRequest(url) ? handleAudioRequest(event.request) : handleAppShellRequest(event.request);
    })());
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
