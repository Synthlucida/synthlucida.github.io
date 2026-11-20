# -*- coding: utf-8 -*-
"""
SYNTHLUCIDA – Test playeru na GitHubu
=====================================
Spouští ho GitHub (soubor .github/workflows/test-playeru.yml) po každém nahrání player.html nebo sw.js.

  python testy/ci_run.py player.html sw.js testy

Udělá:
  * spustí všechny testy z test_playeru.py (stejné jako program TestPlayeru na počítači),
  * test, který neprojde, zkusí ještě jednou (pomalý počítač na GitHubu občas zakolísá),
  * zapíše stránku s výsledky testy/index.html a historii testy/historie.json (posledních 30 běhů),
  * krátký souhrn ukáže i přímo na GitHubu v záložce Actions.

Skončí vždy kódem 0 (aby se stránka s výsledky uložila i při chybě). Jestli vše prošlo, předá GitHubu
v proměnné result (ok / fail); podle ní pak workflow svítí zeleně nebo červeně.
"""
import base64
import datetime
import hashlib
import hmac
import html
import json
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("TESTPLAYERU_BUNDLED", "1")

HISTORY_MAX = 30


def now_prague():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/Prague"))
    except Exception:
        return datetime.datetime.now(datetime.timezone.utc)


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def gh_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s=%s\n" % (key, value))


def gh_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def run_all(player, sw):
    import test_playeru as tp
    tests = {}
    info = {"browser": ""}

    def ev(kind, d):
        if kind == "info":
            info["browser"] = d
            print(d, flush=True)
        elif kind == "done":
            i, name, c, sec = d
            tests[i] = {"name": name, "ok": c.passed, "sec": round(sec, 1),
                        "items": [[bool(o), t] for o, t in c.items], "retry": False}
            print(("OK   " if c.passed else "FAIL ") + "%2d. %s (%.0f s)" % (i + 1, name, sec), flush=True)
            for o, t in c.items:
                if not o:
                    print("       - " + t, flush=True)

    tp.run_tests(player, sw, ev)
    failed = [i for i, t in tests.items() if not t["ok"]]
    if failed:
        print("\nZkouším znovu testy, které neprošly: %s" % ", ".join(str(i + 1) for i in failed), flush=True)
        second = {}

        def ev2(kind, d):
            if kind == "done":
                i, name, c, sec = d
                second[i] = {"name": name, "ok": c.passed, "sec": round(sec, 1),
                             "items": [[bool(o), t] for o, t in c.items]}
                print(("OK   " if c.passed else "FAIL ") + "%2d. %s – 2. pokus" % (i + 1, name), flush=True)

        tp.run_tests(player, sw, ev2, only=set(failed))
        for i in failed:
            if i in second and second[i]["ok"]:
                first_fail = [t for o, t in tests[i]["items"] if not o]
                tests[i] = dict(second[i], retry=True, first_fail=first_fail)
    ordered = [tests[i] for i in sorted(tests)]
    return ordered, info["browser"], len(tp.TESTS)


LIVE_JS = r"""<script>
/* Stránka se sama obnoví, když GitHub uloží nové výsledky (není potřeba Ctrl+F5).
   Každou minutu se podívá do historie.json (malý soubor) a při změně načte stránku znovu,
   s jiným ?v=, aby prohlížeč ani GitHub nevrátily starou kopii z mezipaměti.
   Každých 5 minut se navíc zeptá GitHubu, jestli zrovna neběží nové testy. */
(function () {
  var STAMP = __STAMP__, REPO = __REPO__;
  var live = document.getElementById('live');
  var running = false, checked = '';
  function hhmm(d) { return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2); }
  function show() {
    if (!live) return;
    live.hidden = false;
    live.className = 'live' + (running ? ' run' : '');
    live.textContent = running
      ? 'Právě běží nové testy – stránka se po nich sama obnoví (asi za 10 minut).'
      : 'Stránka se obnovuje sama' + (checked ? ' · naposledy zkontrolováno ' + checked : '') + '.';
  }
  function reload() {
    var last = 0;
    try { last = +sessionStorage.getItem('testy-reload') || 0; } catch (e) {}
    if (Date.now() - last < 120000) return; // pojistka proti opakovanému načítání
    try { sessionStorage.setItem('testy-reload', String(Date.now())); } catch (e) {}
    location.replace(location.pathname + '?v=' + Date.now());
  }
  function checkResults() {
    if (document.hidden) return;
    fetch('historie.json?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (h) {
        if (!h || !h.length) return;
        var x = h[0], stamp = (x.run_url || '') + '|' + (x.date || '') + '|' + (x.commit || '');
        checked = hhmm(new Date());
        if (stamp !== STAMP) reload(); else show();
      })
      .catch(function () {});
  }
  function checkRunning() {
    if (!REPO || document.hidden) return;
    fetch('https://api.github.com/repos/' + REPO + '/actions/workflows/test-playeru.yml/runs?per_page=1', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var run = d && d.workflow_runs && d.workflow_runs[0];
        running = !!(run && run.status && run.status !== 'completed');
        show();
      })
      .catch(function () {});
  }
  show();
  checkResults(); checkRunning();
  setInterval(checkResults, 60000);
  setInterval(checkRunning, 300000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { checkResults(); checkRunning(); } });
})();
</script>"""


def build_page(entry, history):
    e = html.escape

    def status_of(t):
        if not t["ok"]:
            return "bad", "✗"
        if t.get("retry"):
            return "warn", "⚠"
        return "ok", "✓"

    rows = []
    for idx, t in enumerate(entry.get("tests") or []):
        cls, mark = status_of(t)
        items = "".join('<li class="%s">%s %s</li>' % ("ok" if o else "bad", "✓" if o else "✗", e(txt)) for o, txt in t["items"])
        extra = ""
        if t.get("retry"):
            extra = ('<p class="note">Napoprvé neprošlo, napodruhé ano – nejspíš jen vytížený počítač na GitHubu. '
                     'Když se to bude opakovat, je to skutečná chyba. Napoprvé: %s</p>'
                     % e("; ".join(t.get("first_fail") or [])))
        rows.append('<details class="test %s"%s><summary><span class="mark">%s</span><span class="tn">%d. %s</span>'
                    '<span class="sec">%s s</span></summary>%s<ul>%s</ul></details>'
                    % (cls, " open" if cls == "bad" else "", mark, idx + 1, e(t["name"]), t["sec"], extra, items))

    hist = []
    for h in history:
        cls = "ok" if h.get("result") == "ok" else ("warn" if h.get("result") == "warn" else "bad")
        res = {"ok": "✓ vše prošlo", "warn": "⚠ prošlo napodruhé", "fail": "✗ %s neprošlo" % h.get("failed", "?"),
               "error": "✗ testy nedoběhly"}.get(h.get("result"), "?")
        commit = h.get("commit") or ""
        clink = ('<a href="%s">%s</a>' % (e(h.get("commit_url") or "#"), e(commit[:7]))) if commit else "–"
        rlink = ('<a href="%s">detail</a>' % e(h["run_url"])) if h.get("run_url") else ""
        hist.append("<tr><td>%s</td><td>%s</td><td>%s</td><td class=\"%s\">%s</td><td>%s</td><td>%s</td></tr>"
                    % (e(h.get("date", "")), e(h.get("version") or "?"), e(str(h.get("sw") or "?")), cls, res, clink, rlink))

    res = entry.get("result")
    if res == "ok":
        big, bigcls = "✓ Vše prošlo", "ok"
        sub = "Všech %d testů je v pořádku." % entry.get("total", 0)
    elif res == "warn":
        big, bigcls = "⚠ Prošlo, ale napodruhé", "warn"
        sub = "Všechny testy nakonec prošly, některé až při druhém pokusu (viz žluté řádky)."
    elif res == "error":
        big, bigcls = "✗ Testy nedoběhly", "bad"
        sub = "Testy se nepodařilo spustit nebo dokončit. Podrobnosti jsou v detailu běhu na GitHubu."
    else:
        big, bigcls = "✗ %d z %d testů neprošlo" % (entry.get("failed", 0), entry.get("total", 0)), "bad"
        sub = "Červené řádky níže ukazují, co přesně je špatně."

    meta = []
    meta.append("<div><b>Kdy</b>%s</div>" % e(entry.get("date", "")))
    meta.append("<div><b>Verze playeru</b>%s</div>" % e(entry.get("version") or "?"))
    meta.append("<div><b>sw.js</b>%s</div>" % e(str(entry.get("sw") or "?")))
    if entry.get("commit"):
        meta.append('<div><b>Změna</b><a href="%s">%s</a></div>' % (e(entry.get("commit_url") or "#"), e(entry["commit"][:7])))
    meta.append("<div><b>Doba testu</b>%s</div>" % e(entry.get("duration", "")))
    if entry.get("run_url"):
        meta.append('<div><b>Na GitHubu</b><a href="%s">detail běhu</a></div>' % e(entry["run_url"]))

    error_box = ""
    if entry.get("error"):
        error_box = '<pre class="err">%s</pre>' % e(entry["error"])

    page = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Test playeru</title>
<style>
  :root { --bg:#120d0b; --card:#1d1613; --line:#3a2b24; --text:#efe6e1; --muted:#a8968d; --accent:#f0a070;
          --ok:#7fdc8f; --bad:#ff7d7d; --warn:#f3c969; }
  @media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) { --bg:#f7f2ef; --card:#ffffff; --line:#e4d8d1; --text:#2a1f1a;
          --muted:#7a6a62; --accent:#b8582a; --ok:#1d8a3a; --bad:#c62828; --warn:#a36a00; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif; }
  main { max-width:860px; margin:0 auto; padding:28px 16px 60px; }
  h1 { font-size:14px; letter-spacing:3px; text-transform:uppercase; color:var(--accent); margin:0 0 18px; font-weight:600; }
  h2 { font-size:13px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); margin:34px 0 10px; font-weight:600; }
  .hero { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px 18px; }
  .big { font-size:28px; font-weight:700; margin:0 0 4px; }
  .big.ok { color:var(--ok); } .big.bad { color:var(--bad); } .big.warn { color:var(--warn); }
  .sub { color:var(--muted); margin:0 0 16px; }
  .meta { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px 18px; }
  .meta b { display:block; font-size:11px; letter-spacing:1px; text-transform:uppercase; color:var(--muted); font-weight:600; }
  a { color:var(--accent); }
  details.test { background:var(--card); border:1px solid var(--line); border-radius:10px; margin:0 0 8px; }
  details.test.bad { border-color:color-mix(in srgb,var(--bad) 60%%,var(--line)); }
  details.test.warn { border-color:color-mix(in srgb,var(--warn) 60%%,var(--line)); }
  summary { display:flex; gap:10px; align-items:baseline; padding:10px 14px; cursor:pointer; list-style:none; }
  summary::-webkit-details-marker { display:none; }
  .mark { width:1.2em; font-weight:700; flex:none; }
  .ok > summary .mark { color:var(--ok); } .bad > summary .mark { color:var(--bad); } .warn > summary .mark { color:var(--warn); }
  .tn { flex:1; min-width:0; overflow-wrap:anywhere; }
  .sec { color:var(--muted); font-size:12px; flex:none; font-variant-numeric:tabular-nums; }
  details ul { margin:0; padding:0 14px 12px 40px; }
  details li { list-style:none; margin:3px 0; overflow-wrap:anywhere; }
  li.ok { color:var(--muted); } li.bad { color:var(--bad); }
  .note { margin:0 14px 8px 40px; color:var(--warn); font-size:13px; }
  .tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--card); }
  table { width:100%%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { color:var(--muted); font-weight:600; font-size:11px; letter-spacing:1px; text-transform:uppercase; }
  tr:last-child td { border-bottom:0; }
  td.ok { color:var(--ok); } td.bad { color:var(--bad); } td.warn { color:var(--warn); }
  .foot { color:var(--muted); font-size:12px; margin-top:30px; }
  .live { margin:14px 0 0; font-size:12px; color:var(--muted); }
  .live.run { color:var(--warn); }
  .live.run::before { content:""; display:inline-block; width:8px; height:8px; border-radius:50%%; background:var(--warn); margin-right:7px; animation:pulse 1.2s ease-in-out infinite; vertical-align:1px; }
  @keyframes pulse { 50%% { opacity:.25; } }
  @media (prefers-reduced-motion: reduce) { .live.run::before { animation:none; } }
  pre.err { white-space:pre-wrap; overflow-wrap:anywhere; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; font-size:12px; color:var(--bad); }
</style>
</head>
<body>
<main>
  <h1>SYNTHLUCIDA · Test playeru</h1>
  <section class="hero">
    <p class="big %s">%s</p>
    <p class="sub">%s</p>
    <div class="meta">%s</div>
    <p class="live" id="live" hidden></p>
  </section>
  %s
  <h2>Testy</h2>
  %s
  <h2>Historie</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Kdy</th><th>Verze</th><th>sw.js</th><th>Výsledek</th><th>Změna</th><th></th></tr></thead>
    <tbody>%s</tbody>
  </table></div>
  <p class="foot">Testy běží samy na GitHubu po každém nahrání player.html nebo sw.js (a jdou spustit i ručně v záložce Actions).
  Nic se při nich neposílá na skutečný web, GitHub ani R2. %s</p>
</main>
</body>
</html>
""" % (bigcls, e(big), e(sub), "".join(meta), error_box, "\n".join(rows) or "<p class=\"sub\">Žádné výsledky.</p>",
       "\n".join(hist), e(entry.get("browser") or ""))
    return page.replace(
        "</body>",
        LIVE_JS.replace("__STAMP__", json.dumps((entry.get("run_url") or "") + "|" + (entry.get("date") or "") + "|" + (entry.get("commit") or "")))
               .replace("__REPO__", json.dumps(os.environ.get("GITHUB_REPOSITORY", ""))) + "\n</body>")


# --------------------------------------------------------------------------- #
#  Heslo na stránce s výsledky
#  Celá stránka se před uložením zašifruje heslem (PBKDF2-SHA256, 300 000 kol + HMAC-SHA256 jako proud + kontrolní součet).
#  V souboru index.html tedy není ani heslo, ani čitelný obsah – jen šifra a malé přihlašovací okno.
#  Heslo čte GitHub z tajné hodnoty TESTY_HESLO (Settings → Secrets and variables → Actions), v kódu není.
# --------------------------------------------------------------------------- #
PBKDF2_ROUNDS = 300000


def _stream(key, nonce, n):
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def encrypt_page(page_html, password):
    salt, nonce = os.urandom(16), os.urandom(16)
    km = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS, 64)
    ke, km2 = km[:32], km[32:]
    data = page_html.encode("utf-8")
    ks = _stream(ke, nonce, len(data))
    ct = (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(len(data), "big") if data else b""
    tag = hmac.new(km2, salt + nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(salt + nonce + tag + ct).decode("ascii")


GATE_JS = r"""
(async function () {
  var DATA = __DATA__, ROUNDS = __ROUNDS__;
  var raw = Uint8Array.from(atob(DATA), function (c) { return c.charCodeAt(0); });
  var salt = raw.slice(0, 16), nonce = raw.slice(16, 32), tag = raw.slice(32, 64), ct = raw.slice(64);
  var enc = new TextEncoder(), S = crypto.subtle;
  var form = document.getElementById('f'), pw = document.getElementById('pw'), msg = document.getElementById('msg');
  function b64(u) { var s = ''; for (var i = 0; i < u.length; i++) s += String.fromCharCode(u[i]); return btoa(s); }
  function hk(bytes, use) { return S.importKey('raw', bytes, { name: 'HMAC', hash: 'SHA-256' }, false, use); }
  async function open(km) {
    var ke = await hk(km.slice(0, 32), ['sign']), km2 = await hk(km.slice(32), ['verify']);
    var all = new Uint8Array(salt.length + nonce.length + ct.length);
    all.set(salt, 0); all.set(nonce, 16); all.set(ct, 32);
    if (!(await S.verify('HMAC', km2, tag, all))) return false;
    var out = new Uint8Array(ct.length), blocks = Math.ceil(ct.length / 32);
    for (var s = 0; s < blocks; s += 256) {
      var jobs = [];
      for (var i = s; i < Math.min(blocks, s + 256); i++) {
        var m = new Uint8Array(20); m.set(nonce, 0); new DataView(m.buffer).setUint32(16, i);
        jobs.push(S.sign('HMAC', ke, m));
      }
      var res = await Promise.all(jobs);
      for (var j = 0; j < res.length; j++) {
        var k = new Uint8Array(res[j]), off = (s + j) * 32;
        for (var b = 0; b < 32 && off + b < ct.length; b++) out[off + b] = ct[off + b] ^ k[b];
      }
    }
    try { sessionStorage.setItem('testy-key', b64(km)); } catch (e) {}
    var html = new TextDecoder().decode(out);
    document.open(); document.write(html); document.close();
    return true;
  }
  async function fromPassword(p) {
    var base = await S.importKey('raw', enc.encode(p), 'PBKDF2', false, ['deriveBits']);
    var bits = await S.deriveBits({ name: 'PBKDF2', salt: salt, iterations: ROUNDS, hash: 'SHA-256' }, base, 512);
    return new Uint8Array(bits);
  }
  try {
    var saved = sessionStorage.getItem('testy-key');
    if (saved && await open(Uint8Array.from(atob(saved), function (c) { return c.charCodeAt(0); }))) return;
    sessionStorage.removeItem('testy-key');
  } catch (e) {}
  form.hidden = false; pw.focus();
  form.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    msg.textContent = 'Ověřuji…';
    var ok = false;
    try { ok = await open(await fromPassword(pw.value)); } catch (e) { msg.textContent = 'Tento prohlížeč to neumí otevřít.'; return; }
    if (!ok) { msg.textContent = 'Špatné heslo.'; pw.value = ''; pw.focus(); }
  });
})();
"""


def build_gate(page_html, password):
    data = encrypt_page(page_html, password)
    js = GATE_JS.replace("__DATA__", json.dumps(data)).replace("__ROUNDS__", str(PBKDF2_ROUNDS))
    return """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Test playeru</title>
<style>
  :root { color-scheme: dark light; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#120d0b; color:#efe6e1;
         font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif; }
  @media (prefers-color-scheme: light) { body { background:#f7f2ef; color:#2a1f1a; } }
  form { width:min(320px,86vw); text-align:center; }
  h1 { font-size:13px; letter-spacing:3px; text-transform:uppercase; color:#f0a070; margin:0 0 18px; font-weight:600; }
  input, button { font:inherit; width:100%%; box-sizing:border-box; padding:11px 12px; border-radius:10px; border:1px solid #3a2b24; }
  input { background:transparent; color:inherit; margin-bottom:10px; }
  button { background:#f0a070; color:#1a0f0a; border:0; font-weight:600; cursor:pointer; }
  #msg { min-height:1.4em; margin:10px 0 0; font-size:13px; color:#ff7d7d; }
</style>
</head>
<body>
<form id="f" hidden autocomplete="off">
  <h1>SYNTHLUCIDA · Test playeru</h1>
  <input id="pw" type="password" placeholder="Heslo" autocomplete="current-password" aria-label="Heslo">
  <button type="submit">Otevřít</button>
  <p id="msg"></p>
</form>
<noscript>Pro zobrazení výsledků je potřeba JavaScript.</noscript>
<script>%s</script>
</body>
</html>
""" % js


def build_locked_notice():
    return ("<!doctype html><html lang=\"cs\"><meta charset=\"utf-8\"><meta name=\"robots\" content=\"noindex, nofollow\">"
            "<title>Test playeru</title><body style=\"font:15px system-ui;padding:40px;max-width:520px;margin:auto\">"
            "<p>Stránka s výsledky se nezveřejnila, protože na GitHubu není nastavené heslo (tajná hodnota TESTY_HESLO).</p></body></html>")


def main(argv):
    if len(argv) < 3:
        print("Použití: python testy/ci_run.py player.html sw.js testy")
        return 2
    player, sw, outdir = argv[0], argv[1], argv[2]
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    psrc, swsrc = read(player), read(sw)
    v = re.search(r'<meta name="app-version" content="([^"]+)">', psrc)
    m = re.search(r"synthlucida-app-v(\d+)", swsrc)
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    entry = {
        "date": now_prague().strftime("%d.%m.%Y %H:%M"),
        "version": v.group(1) if v else "",
        "sw": m.group(1) if m else "",
        "commit": sha,
        "commit_url": ("%s/%s/commit/%s" % (server, repo, sha)) if (repo and sha) else "",
        "run_url": ("%s/%s/actions/runs/%s" % (server, repo, run_id)) if (repo and run_id) else "",
    }
    try:
        tests, browser, total_defined = run_all(player, sw)
        failed = sum(1 for t in tests if not t["ok"])
        retried = sum(1 for t in tests if t.get("retry"))
        entry.update(tests=tests, browser=browser, total=len(tests), failed=failed)
        if len(tests) < total_defined:
            entry["result"] = "error"
            entry["error"] = "Doběhlo jen %d z %d testů." % (len(tests), total_defined)
        else:
            entry["result"] = "fail" if failed else ("warn" if retried else "ok")
    except Exception:
        entry.update(result="error", tests=[], total=0, failed=0, error=traceback.format_exc()[-3000:])
        print(entry["error"], flush=True)
    secs = int(time.time() - t0)
    entry["duration"] = "%d min %02d s" % (secs // 60, secs % 60)

    hist_path = os.path.join(outdir, "historie.json")
    try:
        history = json.loads(read(hist_path) or "[]")
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []
    short = {k: entry.get(k) for k in ("date", "version", "sw", "commit", "commit_url", "run_url", "result", "failed", "total")}
    history = [short] + [h for h in history if isinstance(h, dict)][:HISTORY_MAX - 1]
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    password = os.environ.get("TESTY_HESLO", "")
    if password:
        out_page = build_gate(build_page(entry, history), password)
    else:
        print("POZOR: není nastavené TESTY_HESLO – stránka s výsledky se nezveřejní.", flush=True)
        out_page = build_locked_notice()
    with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(out_page)

    ok = entry["result"] in ("ok", "warn")
    gh_output("result", "ok" if ok else "fail")
    lines = ["## Test playeru – " + {"ok": "✓ vše prošlo", "warn": "⚠ prošlo, některé testy napodruhé",
                                     "fail": "✗ %d testů neprošlo" % entry.get("failed", 0),
                                     "error": "✗ testy nedoběhly"}[entry["result"]],
             "", "Verze playeru **%s**, sw.js **%s**, doba %s." % (entry["version"] or "?", entry["sw"] or "?", entry["duration"]), ""]
    for i, t in enumerate(entry.get("tests") or []):
        mark = "✗" if not t["ok"] else ("⚠" if t.get("retry") else "✓")
        lines.append("- %s %d. %s" % (mark, i + 1, t["name"]))
        if not t["ok"]:
            lines += ["  - " + txt for o, txt in t["items"] if not o]
    gh_summary("\n".join(lines))
    print("\nVýsledek: " + entry["result"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
