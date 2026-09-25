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
// v37 - player: test připojení (icon.png?probe=… každých 30 s, fetch s
// cache:'no-store') se už NEUKLÁDÁ do cache - dřív každý test přidal do
// synthlucida-app cache další kopii ikony, takže při otevřeném playeru
// cache rostla o ~2 880 souborů denně. Požadavky s cache:'no-store' jdou
// teď rovnou na síť. Cizí servery (počítadlo návštěv countapi, Firebase
// chat, …) se už také necachují - do cache jdou jen soubory z vlastního
// webu, písma Google Fonts a skripty z www.gstatic.com (Firebase SDK)
// a samozřejmě MP3 do audio cache. Kliknutí na notifikaci teď otevře
// player, i když je na webu otevřená jiná stránka (dřív se přepnulo na
// první nalezené okno, třeba TRIP COST). Cache zvýšena na v994, aby se
// smazala stará cache i se všemi uloženými kopiemi testu připojení.
// v38 - skladba se začne přehrávat hned, jakmile dorazí první data - dřív SW
// čekal, až se do cache uloží CELÁ skladba, a teprve pak ji pustil přehrávači
// (na pomalém mobilu i desítky sekund ticha). Ukládání teď doběhne na pozadí;
// tlačítko DOWNLOAD OFFLINE dál čeká na kompletní uložení. Safari / iPhone
// dostává hudbu online přímo ze sítě (jeho přehrávač vyžaduje odpovědi 206,
// které z uložené kopie z GitHubu vyrobit nejde); uložená kopie je záloha
// pro offline. Připravena podpora serverů s CORS (CORS_AUDIO_HOSTS): čitelné
// kopie se kontrolují a z cache se vrací i části souboru (206).
// ==========================================

const APP_CACHE_NAME = 'synthlucida-app-v1011';
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

// Cizí servery, jejichž soubory má smysl mít i offline (písma a Firebase SDK).
// Vše ostatní z cizích domén (API, počítadla, chat) jde rovnou na síť bez cache.
const CACHEABLE_CDN = /^(fonts\.googleapis\.com|fonts\.gstatic\.com|www\.gstatic\.com)$/i;

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

  // Požadavky, které si výslovně nepřejí cache (cache:'no-store' - např. test
  // připojení v playeru, icon.png?probe=…), nechat jít rovnou na síť. Jinak by
  // se každý z nich uložil do synthlucida-app cache jako nový soubor.
  if (event.request.cache === 'no-store') {
    return;
  }

  // Cizí domény kromě MP3 a písem/Firebase SDK (API počítadla návštěv,
  // Firebase chat, …) - rovnou na síť, nic necachovat.
  if (url.origin !== self.location.origin && !isAudioRequest(url) && !CACHEABLE_CDN.test(url.hostname)) {
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
      return isAudioRequest(url) ? handleAudioRequest(event) : handleAppShellRequest(event.request);
    })());
    return;
  }

  if (isAudioRequest(url)) {
    event.respondWith(handleAudioRequest(event));
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

// Apple WebKit = Safari a všechny prohlížeče na iPhonu/iPadu. Jejich přehrávač
// chce na požadavky s hlavičkou Range odpověď "206 Partial Content". Kopii
// skladby uloženou z GitHubu (bez CORS = "opaque") ale service worker rozkrojit
// neumí, proto tam hudba online jde rovnou ze sítě (s původní Range hlavičkou)
// a uložená kopie slouží jen jako záloha, když síť není.
const IS_APPLE_WEBKIT = (() => {
  const ua = (self.navigator && self.navigator.userAgent) || '';
  return /iPhone|iPad|iPod/.test(ua) ||
    (/AppleWebKit/.test(ua) && /Safari/.test(ua) && !/(Chrome|Chromium|CriOS|Edg|OPR|Android)/.test(ua));
})();

// Servery s MP3, které posílají CORS hlavičku (Access-Control-Allow-Origin).
// Z nich se skladby stahují čitelně - SW pak ověří, že je soubor v pořádku
// (ne chybová stránka), a umí vracet části souboru (206) pro Safari.
// GitHub Releases CORS neposílá, proto je seznam zatím prázdný. Po případném
// přesunu MP3 na server s CORS sem stačí doplnit jeho doménu.
const CORS_AUDIO_HOSTS = [];

function offlineAudioResponse() {
  return new Response('Offline - this track is not cached.', {
    status: 503,
    statusText: 'Offline',
    headers: { 'Content-Type': 'text/plain' }
  });
}

// Z celé (čitelné) odpovědi vyrobí odpověď na Range požadavek: 206 s danou
// částí souboru. Bez Range hlavičky vrátí odpověď beze změny.
async function rangeResponse(request, response) {
  const range = request.headers.get('range');
  const m = range && /^bytes=(\d*)-(\d*)$/.exec(range.trim());
  if (!m || (m[1] === '' && m[2] === '')) return response;
  const blob = await response.blob();
  const size = blob.size;
  let start;
  let end;
  if (m[1] === '') {
    start = Math.max(0, size - Number(m[2]));
    end = size - 1;
  } else {
    start = Number(m[1]);
    end = m[2] === '' ? size - 1 : Math.min(Number(m[2]), size - 1);
  }
  if (start >= size || start > end) {
    return new Response(null, { status: 416, statusText: 'Range Not Satisfiable', headers: { 'Content-Range': 'bytes */' + size } });
  }
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    statusText: 'Partial Content',
    headers: {
      'Content-Type': response.headers.get('Content-Type') || 'audio/mpeg',
      'Content-Range': 'bytes ' + start + '-' + end + '/' + size,
      'Content-Length': String(end - start + 1),
      'Accept-Ranges': 'bytes'
    }
  });
}

async function handleAudioRequest(event) {
  const request = event.request;
  const url = new URL(request.url);
  const cache = await caches.open(AUDIO_CACHE_NAME);

  // Always match by plain URL, ignoring any Range header on the incoming request,
  // so we always find (and return) the full cached file if we have it.
  const cached = await cache.match(request.url);

  // Požadavek přímo od přehrávače (<audio>), ne stažení tlačítkem DOWNLOAD OFFLINE.
  const isMedia = request.destination === 'audio' || request.destination === 'video' || request.headers.has('range');

  // Čitelná uložená kopie (server s CORS) - rovnou z cache, i s částmi souboru (206).
  if (cached && cached.type !== 'opaque') {
    return rangeResponse(request, cached);
  }

  // Safari / iPhone: online přímo ze sítě s původní Range hlavičkou, cache jen offline.
  if (IS_APPLE_WEBKIT && isMedia) {
    try {
      return await fetch(request);
    } catch (err) {
      return cached || offlineAudioResponse();
    }
  }

  if (cached) {
    return cached;
  }

  try {
    const corsHost = url.origin === self.location.origin || CORS_AUDIO_HOSTS.includes(url.hostname);
    // Build a clean request (no Range header -> the whole file). GitHub MP3s have
    // no CORS headers, so they are fetched in the same "no-cors" mode the <audio>
    // element uses; servers from CORS_AUDIO_HOSTS are fetched readable ("cors").
    const cleanRequest = new Request(request.url, {
      method: 'GET',
      mode: corsHost ? 'cors' : request.mode,
      credentials: corsHost ? 'same-origin' : request.credentials,
      redirect: 'follow'
    });

    const networkResponse = await fetch(cleanRequest);

    // U čitelné odpovědi se uloží jen opravdu v pořádku stažený soubor (ne 404
    // ani přihlašovací stránka hotelové wifi). U "opaque" odpovědi z GitHubu
    // stav vidět není, ta se ukládá jako dřív.
    const readable = networkResponse.type !== 'opaque';
    if (readable && networkResponse.status !== 200) {
      return networkResponse;
    }

    const putDone = cache.put(request.url, networkResponse.clone()).catch((err) => {
      console.log('[SW] Could not cache audio:', err);
    });

    if (isMedia) {
      // Přehrávač dostane odpověď hned a hraje, jakmile dorazí první data;
      // uložení celé skladby do cache doběhne na pozadí. (Dřív se čekalo na
      // stažení CELÉ skladby, takže na pomalém mobilním připojení trvalo
      // i desítky sekund, než se hudba vůbec rozehrála.)
      try { event.waitUntil(putDone); } catch (e) { /* ignore */ }
      return networkResponse;
    }

    // DOWNLOAD OFFLINE (fetch z appky): odpověď až ve chvíli, kdy je skladba
    // opravdu celá uložená - "✓ OFFLINE" tak vždy odpovídá realitě.
    await putDone;
    return networkResponse;
  } catch (err) {
    return offlineAudioResponse();
  }
}

// ==========================================
// Kliknutí na lokální notifikaci (připomínky) - zavře notifikaci a přepne
// na už otevřenou appku, nebo ji otevře, pokud zrovna neběží.
// ==========================================
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  // Notifikace posílá player (připomínky, komunitní chat) - přepnout na okno
  // s playerem. Jiné otevřené stránky webu (TRIP COST, Deník vozidla…) se
  // přeskočí; když player otevřený není, otevře se.
  const target = (event.notification.data && event.notification.data.url) || './player.html';
  const targetPath = new URL(target, self.location.href).pathname;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((allClients) => {
      const match = allClients.find((c) => new URL(c.url).pathname === targetPath);
      if (match) {
        return match.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
