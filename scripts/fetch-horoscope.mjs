// scripts/fetch-horoscope.mjs
// Stáhne horoskop pro všech 12 znamení z freehoroscopeapi.com (bez klíče)
// a uloží je do horoscope.json. Spouští ho GitHub Actions (viz horoscope.yml).
//
// Lokálně:  node scripts/fetch-horoscope.mjs        (Node 18+)
// Volitelně (proměnné prostředí):
//   HOROSCOPE_PERIOD    monthly (výchozí) | weekly | daily
//   HOROSCOPE_API_BASE  adresa API
//   HOROSCOPE_OUT       cesta k výstupnímu souboru

import { readFile, writeFile } from 'node:fs/promises';

const BASE = (process.env.HOROSCOPE_API_BASE || 'https://freehoroscopeapi.com').replace(/\/$/, '');
const OUT = process.env.HOROSCOPE_OUT || 'horoscope.json';
const PERIOD = (process.env.HOROSCOPE_PERIOD || 'monthly').toLowerCase();
if (!['daily', 'weekly', 'monthly'].includes(PERIOD)) {
  console.error(`::error::Neplatná HOROSCOPE_PERIOD "${PERIOD}" (povoleno: daily, weekly, monthly).`);
  process.exit(1);
}
const SIGNS = ['aries', 'taurus', 'gemini', 'cancer', 'leo', 'virgo',
               'libra', 'scorpio', 'sagittarius', 'capricorn', 'aquarius', 'pisces'];
const ATTEMPTS = 3;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function fetchSign(sign) {
  let lastErr;
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    try {
      const res = await fetch(`${BASE}/api/v1/get-horoscope/${PERIOD}?sign=${sign}`, {
        headers: { accept: 'application/json' },
        signal: AbortSignal.timeout(15000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      const d = json && json.data;
      const text = d && (d.horoscope || d.horoscope_data);
      if (typeof text !== 'string' || !text.trim()) throw new Error('prázdná odpověď');
      return { date: String(d.date || ''), text: text.trim() };
    } catch (err) {
      lastErr = err;
      if (attempt < ATTEMPTS) await sleep(attempt * 2000);
    }
  }
  throw lastErr;
}

let previous = { signs: {} };
try { previous = JSON.parse(await readFile(OUT, 'utf8')); } catch { /* první běh */ }
if (!previous || typeof previous.signs !== 'object') previous = { signs: {} };
// po změně periody (např. daily → monthly) nepřebírej staré texty jiného typu
if (previous.period && previous.period !== PERIOD) previous = { signs: {} };

const signs = {};
let fetched = 0;
for (const sign of SIGNS) {
  try {
    signs[sign] = await fetchSign(sign);
    fetched++;
    console.log(`ok   ${sign} (${signs[sign].date})`);
  } catch (err) {
    // když se jedno znamení nepovede, nech v souboru poslední známý text
    if (previous.signs[sign]) signs[sign] = previous.signs[sign];
    console.log(`::warning::${sign}: ${err.message}`);
  }
  await sleep(500); // ať API zbytečně nezatěžujeme
}

if (fetched === 0) {
  console.error(`::error::Nepodařilo se stáhnout žádné znamení (period=${PERIOD}), horoscope.json zůstává beze změny. Pokud API hlásí HTTP 400, zkus HOROSCOPE_PERIOD=daily.`);
  process.exit(1);
}

// Soubor přepiš jen když se obsah opravdu změnil, ať v gitu nevznikají prázdné commity.
if (JSON.stringify(signs) === JSON.stringify(previous.signs)) {
  console.log('Beze změny.');
} else {
  const out = { source: 'freehoroscopeapi.com', period: PERIOD, updated: new Date().toISOString(), signs };
  await writeFile(OUT, JSON.stringify(out, null, 2) + '\n', 'utf8');
  console.log(`Uloženo do ${OUT} (${PERIOD}, ${fetched}/12 znamení aktualizováno).`);
}
