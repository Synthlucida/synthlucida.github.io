# -*- coding: utf-8 -*-
"""
SYNTHLUCIDA - Test playeru
==========================
Maly program, ktery pred nahranim na web sam vyzkousi player.html a sw.js.

Co dela:
  * na tomto pocitaci si spusti tri male "falesne" servery (web, GitHub, R2),
  * otevre player v neviditelnem prohlizeci (Microsoft Edge / Chrome) a proklika ho,
  * ukaze, co proslo a co ne.

Nic se neposila na skutecny web, GitHub ani R2. Prohlizec ma behem testu zakazany
pristup na internet (jen localhost), takze se nepocitaji navstevy ani chat.
Chat a spolecne dychani (TOGETHER) se zkousi na falesnem Firebase, ktery bezi jen v pameti stranky
(chovaji se v nem jako ostatni posluchaci) - na skutecny Firebase se nic neposila.

Spusteni bez okna (pro kontrolu):  python test_playeru.py --cli player.html sw.js
"""
import io
import json
import math
import os
import queue
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_TITLE = "SYNTHLUCIDA – Test playeru"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".synthlucida_test_playeru.json")
GH_PREFIX = "https://github.com/Synthlucida/synthlucida.github.io/releases/download/"

# okenni exe nema konzoli -> knihovny, ktere pisou na stdout/stderr, by spadly
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


# --------------------------------------------------------------------------- #
#  Falesne servery
# --------------------------------------------------------------------------- #
def make_wav(seconds=60, rate=16000):
    """Tichy ton, ktery prohlizec prehraje jako skladbu (cca 1,9 MB)."""
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    one = b"".join(struct.pack("<h", int(2500 * math.sin(2 * math.pi * 220 * i / rate))) for i in range(rate))
    for _ in range(seconds):
        w.writeframesraw(one)
    w.close()
    return buf.getvalue()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Env:
    """Testovaci prostredi: upravene kopie player.html / sw.js + tri servery."""

    def __init__(self, player_path, sw_path):
        with open(player_path, "r", encoding="utf-8") as f:
            self.player_src = f.read()
        with open(sw_path, "r", encoding="utf-8") as f:
            self.sw_src = f.read()
        self.onair_src = None  # onair.html (ON AIR) – hledá se vedle player.html
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(player_path)), "onair.html"), "r", encoding="utf-8") as f:
                self.onair_src = f.read()
        except Exception:
            pass
        self.manager_src = None  # manager.html (FILE MANAGER) – hledá se vedle player.html
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(player_path)), "manager.html"), "r", encoding="utf-8") as f:
                self.manager_src = f.read()
        except Exception:
            pass
        self.wav = make_wav()
        self.gh = {"mode": "ok", "ms": 0, "hits": 0}
        self.r2 = {"mode": "ok", "cors": True, "status": 200, "hits": 0}
        self.servers = []
        self.warnings = []
        self.version_override = None
        self.p_site = self.p_gh = self.p_r2 = 0
        self.html = ""
        self.sw = ""

    # --- priprava souboru ---------------------------------------------------
    def prepare(self):
        self.p_site, self.p_gh, self.p_r2 = free_port(), free_port(), free_port()
        local_gh = "http://localhost:%d/dl/" % self.p_gh
        if GH_PREFIX not in self.player_src:
            raise RuntimeError("V player.html nejsou adresy skladeb na GitHubu – je to správný soubor?")
        self.html = self.player_src.replace(GH_PREFIX, local_gh)
        sw = self.sw_src.replace(GH_PREFIX, local_gh)
        sw, n1 = re.subn(r"const CORS_AUDIO_HOSTS = \[[^\]]*\];", "const CORS_AUDIO_HOSTS = ['127.0.0.1'];", sw)
        sw, n2 = re.subn(r"const R2_BASE = '[^']*';", "const R2_BASE = 'http://127.0.0.1:%d/';" % self.p_r2, sw)
        if not n1 or not n2 or local_gh not in sw:
            self.warnings.append("V sw.js jsem nenašel nastavení zálohy R2 – testy R2 zřejmě neprojdou.")
        self.sw = sw + "\n// test %s\n" % time.time()

    @property
    def url(self):
        return "http://localhost:%d/player.html" % self.p_site

    @property
    def r2_url(self):
        return "http://127.0.0.1:%d/" % self.p_r2

    def page_html(self):
        if self.version_override:
            return re.sub(r'<meta name="app-version" content="[^"]*">',
                          '<meta name="app-version" content="%s">' % self.version_override, self.html)
        return self.html

    def reset(self):
        self.gh.update(mode="ok", ms=0, hits=0)
        self.r2.update(mode="ok", cors=True, status=200, hits=0)
        self.version_override = None

    # --- servery ------------------------------------------------------------
    def start(self):
        env = self

        class Quiet(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send_bytes(self, code, body, headers):
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(body)
                    except Exception:
                        pass

        def audio_handler(who):
            st = env.gh if who == "gh" else env.r2

            class H(Quiet):
                def do_OPTIONS(self):
                    self.send_bytes(204, b"", {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Range",
                                               "Access-Control-Allow-Methods": "GET,HEAD"})

                def do_HEAD(self):
                    self.do_GET()

                def do_GET(self):
                    path = self.path.split("?")[0]
                    if not path.lower().endswith(".mp3"):
                        return self.send_bytes(404, b"", {})
                    st["hits"] += 1
                    mode = st["mode"]
                    if mode == "dead":  # spojeni se hned utne (jako nedostupny server)
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except Exception:
                            pass
                        try:
                            self.connection.close()
                        except Exception:
                            pass
                        return
                    h = {"Content-Type": "audio/wav", "Accept-Ranges": "bytes"}
                    cors = who == "r2" and st.get("cors")
                    if cors:
                        h["Access-Control-Allow-Origin"] = "*"
                        h["Access-Control-Expose-Headers"] = "Content-Range,Content-Length,Accept-Ranges,Content-Type"
                    if mode == "404" or (who == "r2" and st.get("status", 200) != 200):
                        code = 404 if who == "gh" else st.get("status", 404)
                        eh = {"Content-Type": "text/html"}
                        if cors:
                            eh["Access-Control-Allow-Origin"] = "*"
                        return self.send_bytes(code, b"<html>nope</html>", eh)
                    if mode == "slow":
                        time.sleep(st.get("ms", 0) / 1000.0)
                    total = len(env.wav)
                    m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
                    if m:
                        a = int(m.group(1)) if m.group(1) else 0
                        b = min(int(m.group(2)), total - 1) if m.group(2) else total - 1
                        h["Content-Range"] = "bytes %d-%d/%d" % (a, b, total)
                        return self.send_bytes(206, env.wav[a:b + 1], h)
                    return self.send_bytes(200, env.wav, h)

            return H

        class Site(Quiet):
            def do_GET(self):
                path = self.path.split("?")[0]
                nc = {"Cache-Control": "no-cache"}
                if path in ("/", "/player.html"):
                    return self.send_bytes(200, env.page_html().encode("utf-8"), dict(nc, **{"Content-Type": "text/html; charset=utf-8"}))
                if path == "/sw.js":
                    return self.send_bytes(200, env.sw.encode("utf-8"), dict(nc, **{"Content-Type": "application/javascript"}))
                if path == "/manager.html" and env.manager_src is not None:
                    return self.send_bytes(200, env.manager_src.encode("utf-8"), dict(nc, **{"Content-Type": "text/html; charset=utf-8"}))
                if path == "/onair.html" and env.onair_src is not None:
                    return self.send_bytes(200, env.onair_src.encode("utf-8"), dict(nc, **{"Content-Type": "text/html; charset=utf-8"}))
                if path in ("/favicon.png", "/icon.png", "/logo.jpg"):
                    return self.send_bytes(200, b"", dict(nc, **{"Content-Type": "image/png"}))
                if path == "/manifest.json":
                    return self.send_bytes(200, b"{}", dict(nc, **{"Content-Type": "application/json"}))
                return self.send_bytes(404, b"", nc)

        for port, handler in ((self.p_site, Site), (self.p_gh, audio_handler("gh")), (self.p_r2, audio_handler("r2"))):
            srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.servers.append(srv)

    def stop(self):
        for s in self.servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        self.servers = []


# --------------------------------------------------------------------------- #
#  Pomocnici pro testy
# --------------------------------------------------------------------------- #
SEEN_JS = "try{localStorage.setItem('sl-support-dismissed','1');localStorage.setItem('sl-tour-seen','1')}catch(e){}"


# --------------------------------------------------------------------------- #
#  Hodnoty, ktere jde zmenit v Editoru playeru (SYNTHLUCIDA AUDIO EDITOR)
# --------------------------------------------------------------------------- #
# Testy je ctou primo z testovaneho player.html (ne napevno), aby po uprave v editoru dal prochazely:
# kod uvodniho okna, cas jeho zobrazeni, kod 666 do chatu, XP pro admina, texty spolecneho dychani, kroky pruvodce.
PCFG = {}


def _js_unq(body):
    """Obsah JS retezce (bez uvozovek) -> text."""
    out, i = [], 0
    esc = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out.append(ch)
            i += 1
            continue
        nx = body[i + 1]
        if nx == "u" and re.match(r"[0-9a-fA-F]{4}", body[i + 2:i + 6]):
            out.append(chr(int(body[i + 2:i + 6], 16)))
            i += 6
        elif nx == "x" and re.match(r"[0-9a-fA-F]{2}", body[i + 2:i + 4]):
            out.append(chr(int(body[i + 2:i + 4], 16)))
            i += 4
        elif nx == "\n":
            i += 2
        else:
            out.append(esc.get(nx, nx))
            i += 2
    return "".join(out)


_JS_WS = re.compile(r"(?:\s+|//[^\n]*|/\*.*?\*/)*", re.S)
_JS_KEY = re.compile(r"""(?:([A-Za-z_$][\w$]*)|(['"])((?:\\.|(?!\2)[^\\\n])*)\2)\s*:\s*""")
_JS_STR = re.compile(r"""(['"])((?:\\.|(?!\1)[^\\\n])*)\1""")


def _js_val(src, i):
    i = _JS_WS.match(src, i).end()
    c = src[i]
    if c in "'\"":
        m = _JS_STR.match(src, i)
        return _js_unq(m.group(2)), m.end()
    if c == "{":
        out = {}
        i += 1
        while True:
            i = _JS_WS.match(src, i).end()
            if src[i] == "}":
                return out, i + 1
            if src[i] == ",":
                i += 1
                continue
            m = _JS_KEY.match(src, i)
            if not m:
                raise ValueError("JS objekt")
            key = m.group(1) or _js_unq(m.group(3))
            out[key], i = _js_val(src, m.end())
    if c == "[":
        out = []
        i += 1
        while True:
            i = _JS_WS.match(src, i).end()
            if src[i] == "]":
                return out, i + 1
            if src[i] == ",":
                i += 1
                continue
            v, i = _js_val(src, i)
            out.append(v)
    m = re.compile(r"[^,}\]\n]+").match(src, i)
    return m.group(0).strip(), m.end()


def js_object(src, decl):
    """Hodnota JS promenne/objektu (napr. 'const LANG = ') jako dict / list, nebo None."""
    i = src.find(decl)
    if i < 0:
        return None
    try:
        return _js_val(src, i + len(decl))[0]
    except Exception:
        return None


TGL = {  # texty spolecneho dychani (prepisou se texty z player.html)
    "en": {"title": "BREATHE TOGETHER", "entryBtn": "TOGETHER", "connecting": "Connecting…",
           "offline": "Offline — the rhythm keeps going by the clock",
           "alone": "You're the only one here right now — anyone who joins falls right into your rhythm",
           "many": "Breathing together right now: {n}", "waitLabel": "Join on the next inhale", "inLabel": "Inhale through the nose",
           "holdLabel": "Hold your breath", "outLabel": "Exhale through the mouth", "heartBtn": "SEND CALM",
           "today": "People today: {p} · minutes together: {m}", "eveningToday": "Evening group session today {time} · in {in}",
           "eveningTomorrow": "Next evening group session tomorrow {time} · in {in}", "eveningLiveBanner": "EVENING SESSION LIVE · {left} left",
           "timeAt": "at 21:00", "timeAtLocal": "at 21:00 Prague time ({local} your time)", "durMin": "{m} min", "durH": "{h} h",
           "durHMin": "{h} h {m} min", "entryCount": "{n} now", "icsFile": "breathe-together.ics"},
    "cz": {"title": "SPOLEČNÉ DÝCHÁNÍ", "entryBtn": "SPOLEČNĚ", "connecting": "Připojuji…",
           "offline": "Bez připojení — rytmus běží dál podle hodin",
           "alone": "Zatím tu dýcháš sám/sama — kdo se připojí, naskočí přesně do tvého rytmu",
           "many": "Právě tu spolu dýcháte: {n}", "heartBtn": "POSLAT KLID", "icsFile": "spolecne-dychani.ics"},
}


TGL_DEFAULT = {k: dict(v) for k, v in TGL.items()}


def tgt(key, lang="en", **kw):
    """Text spolecneho dychani s doplnenymi hodnotami ({n}, {p}…)."""
    t = TGL[lang][key]
    for k, v in kw.items():
        t = t.replace("{%s}" % k, str(v))
    return t


def tgrx(key, lang="en", **kw):
    """Regularni vyraz z textu: pevne casti doslova, {neco} = zadany vyraz (jinak cokoli)."""
    out = []
    for part in re.split(r"(\{\w+\})", TGL[lang][key]):
        m = re.fullmatch(r"\{(\w+)\}", part)
        out.append(kw.get(m.group(1), ".*?") if m else re.escape(part))
    return "".join(out)


def tgpre(key, lang="en", n=24):
    """Zacatek textu (pred prvni promennou) pro kontrolu startswith."""
    return TGL[lang][key].split("{")[0][:n]


def player_cfg(src):
    c = {"sp_key": "sl-support-dismissed", "sp_on": True, "sp_first": 1500, "admin_code": "666", "admin_xp": 1000,
         "layer_step": ["LAYER", "VRSTVA"], "lang": {}}
    i = src.find("document.getElementById('support-popup');")
    if i >= 0:
        seg = src[i:src.find("})();", i)]
        m = re.search(r"var KEY = '((?:[^'\\\n]|\\.)*)';", seg)
        if m:
            c["sp_key"] = _js_unq(m.group(1))
        m = re.search(r"var FIRST_MS = (\d+);", seg)
        if m:
            c["sp_first"] = int(m.group(1))
        if "if (isDismissed()) return;" in seg:
            c["sp_on"] = bool(re.search(r"if \(isDismissed\(\)\) return;\s*schedule\(FIRST_MS\);", seg))
    m = re.search(r"if \(text === '((?:[^'\\\n]|\\.)*)'\) \{\s*input\.value = '';\s*showAdminLogin\(\);", src)
    if m:
        c["admin_code"] = _js_unq(m.group(1))
    m = re.search(r"const ADMIN_TEST_XP = (\d+(?:\.\d+)?);", src)
    if m:
        c["admin_xp"] = int(round(float(m.group(1))))
    lang = js_object(src, "const LANG = ")
    if isinstance(lang, dict):
        c["lang"] = lang
    m = re.search(r"sel: '#btn-layer', t: ", src)
    if m:
        try:
            t = _js_val(src, m.end())[0]
            names = [v for v in t.values() if isinstance(v, str) and v.strip()]
            if names:
                c["layer_step"] = names
        except Exception:
            pass
    return c


def configure(src):
    """Nastavi testy podle testovaneho player.html (pokazde od vychozich hodnot - v okne jde testovat vic souboru)."""
    for lg in TGL:
        TGL[lg].clear()
        TGL[lg].update(TGL_DEFAULT[lg])
    PCFG.clear()
    PCFG.update(player_cfg(src))
    for lg in ("en", "cz"):
        new = PCFG["lang"].get(lg)
        if isinstance(new, dict):
            TGL[lg].update({k: v for k, v in new.items() if isinstance(v, str)})
    TG.update({"wait": TGL["en"]["waitLabel"], "in": TGL["en"]["inLabel"], "hold": TGL["en"]["holdLabel"],
               "out": TGL["en"]["outLabel"], "title": TGL["en"]["title"], "alone": tgpre("alone"),
               "offline": tgpre("offline"), "connecting": tgpre("connecting")})


def seen_js():
    """Init skript: navstevnik uz zna uvodni okno i pruvodce (klic okna podle player.html)."""
    key = PCFG.get("sp_key") or "sl-support-dismissed"
    return "try{localStorage.setItem(%s,'1');localStorage.setItem('sl-tour-seen','1')}catch(e){}" % json.dumps(key)


def click_vis(p, sel):
    """Klepne na prvek; kdyz je v Editoru playeru skryty (napr. v EXPLORE), spusti jeho akci primo."""
    if p.is_visible(sel):
        p.click(sel)
    else:
        p.evaluate("(s)=>document.querySelector(s).click()", sel)
IOS_JS = """Object.defineProperty(HTMLMediaElement.prototype,'volume',{configurable:true,get(){return 1},set(v){}});
try{delete Element.prototype.requestFullscreen}catch(e){} try{delete Navigator.prototype.wakeLock}catch(e){} try{delete window.Notification}catch(e){}"""
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.0 Mobile/15E148 Safari/604.1")
PIANO_JS = "Array.from(document.querySelectorAll('.btn-atmos')).find(x=>/PIANO/i.test(x.textContent)).click()"
TOAST_JS = "document.getElementById('app-toast-text').textContent"
CLEAR_AUDIO_CACHE_JS = "caches.keys().then(k=>Promise.all(k.filter(x=>x.startsWith('synthlucida-audio')).map(x=>caches.delete(x))))"
CACHE_TYPE_JS = "async(src)=>{const c=await caches.open(AUDIO_CACHE_NAME);const m=await c.match(src);return m?m.type:null}"
OVERFLOW_JS = """()=>{const W=document.documentElement.clientWidth;const out=[];
 const vis=e=>{const r=e.getBoundingClientRect();const c=getComputedStyle(e);return r.width>0&&r.height>0&&c.visibility!=='hidden'&&c.display!=='none'&&c.opacity!=='0'};
 for(const e of document.querySelectorAll('body *')){if(!vis(e))continue;const r=e.getBoundingClientRect();
  if(r.right>W+1||r.left<-1){let a=e.parentElement,clipped=false;while(a&&a!==document.body&&a!==document.documentElement){const c=getComputedStyle(a);if(c.overflowX!=='visible'){const ar=a.getBoundingClientRect();clipped=ar.right<=W+1&&ar.left>=-1;if(clipped)break;}a=a.parentElement}
   if(!clipped)out.push((e.id?'#'+e.id:e.tagName.toLowerCase()+'.'+String(e.className).split(' ')[0])+' '+Math.round(r.left)+'..'+Math.round(r.right));}}
 return [...new Set(out)].slice(0,6);}"""


class Checks:
    def __init__(self):
        self.items = []  # (ok, text)

    def ok(self, cond, text):
        self.items.append((bool(cond), text))
        return bool(cond)

    @property
    def passed(self):
        return all(i[0] for i in self.items) and bool(self.items)


class Session:
    """Jeden beh: prohlizec + prostredi."""

    def __init__(self, env, browser):
        self.env = env
        self.browser = browser
        self._open = []

    def page(self, w=390, h=800, seen=True, init=None, sw=False, wait=1500, **opts):
        ctx = self.browser.new_context(viewport={"width": w, "height": h}, accept_downloads=True, **opts)
        self._open.append(ctx)
        # druha pojistka: vse mimo tento pocitac se rovnou zahodi (pocitadla navstev, chat, fonty)
        ctx.route(re.compile(r"^(?:https?|wss?)://(?!localhost[:/]|127\.0\.0\.1[:/])"), lambda route: route.abort())
        if seen:
            ctx.add_init_script(seen_js())
        if init:
            ctx.add_init_script(init)
        p = ctx.new_page()
        p.set_default_timeout(20000)
        errs = []
        p.on("pageerror", lambda e: errs.append(str(e)[:160]))
        p.on("dialog", lambda d: d.accept())
        p.goto(self.env.url)
        if sw:
            js = "()=>navigator.serviceWorker&&navigator.serviceWorker.controller"
            try:
                p.wait_for_function(js, timeout=20000)
            except Exception:
                p.reload()
                p.wait_for_function(js, timeout=20000)
        p.wait_for_timeout(wait)
        return ctx, p, errs

    def close_all(self):
        for c in self._open:
            try:
                c.close()
            except Exception:
                pass
        self._open = []


# --------------------------------------------------------------------------- #
#  TESTY
# --------------------------------------------------------------------------- #
def t_static(s, c):
    src = s.env.player_src
    ids = re.findall(r'\sid="([^"${}]+)"', src)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    c.ok(not dup, "Žádné zdvojené označení prvků (id)" + (": " + ", ".join(dup[:5]) if dup else ""))
    tr = re.findall(r'src: "(%s[^"]+)"' % re.escape(GH_PREFIX), src)
    dupt = sorted({t.split("/")[-1] for t in tr if tr.count(t) > 1})
    c.ok(len(tr) > 20 and not dupt, "Seznam skladeb: %d skladeb, žádná dvakrát" % len(tr) + (" – zdvojené: " + ", ".join(dupt[:5]) if dupt else ""))
    bad = [t.split("/")[-1] for t in tr if not t.lower().endswith(".mp3") or " " in t]
    c.ok(not bad, "Adresy skladeb končí na .mp3 a nemají mezery" + (": " + ", ".join(bad[:5]) if bad else ""))
    v = re.search(r'<meta name="app-version" content="([^"]+)">', src)
    c.ok(bool(v), "Číslo verze playeru: " + (v.group(1) if v else "CHYBÍ"))
    m = re.search(r"synthlucida-app-v(\d+)", s.env.sw_src)
    c.ok(bool(m), "Číslo v sw.js: " + (m.group(1) if m else "CHYBÍ"))
    for w in s.env.warnings:
        c.ok(False, w)


def t_load(s, c):
    ctx, p, errs = s.page()
    r = p.evaluate("()=>({rows:document.querySelectorAll('#playlist li').length,title:document.getElementById('track-title').textContent,"
                   "lists:Object.keys(tracks).filter(k=>Array.isArray(tracks[k])&&tracks[k].length).length})")
    c.ok(r["rows"] > 5, "Po načtení je vidět seznam skladeb (%d řádků)" % r["rows"])
    c.ok(r["lists"] >= 3, "Jsou načtené playlisty ASMR / PIANO / CHILL")
    for fn in ("toggleHistory()", "toggleSearch()", "toggleCustomQueue()", "toggleMainGuide()", "toggleMainStats()",
               "toggleMainFiles()", "toggleMainReminders()", "toggleSetup()"):
        try:
            p.evaluate(fn)
            p.wait_for_timeout(200)
            p.evaluate(fn)
        except Exception as e:
            errs.append("%s: %s" % (fn, str(e)[:100]))
    c.ok(not errs, "Načtení a otevření všech oken bez chyby" + (": " + " | ".join(errs[:3]) if errs else ""))
    p.set_viewport_size({"width": 320, "height": 700})
    p.evaluate("showToast('Ke stažení této skladby do zařízení je potřeba internet. Připoj se a zkus to znovu.','📶')")
    p.wait_for_timeout(500)
    t = p.evaluate("()=>{const e=document.getElementById('app-toast-text'),b=document.getElementById('app-toast').getBoundingClientRect();return {cut:e.scrollHeight>e.clientHeight+1,in:b.left>=0&&b.right<=innerWidth}}")
    c.ok(not t["cut"] and t["in"], "Delší hláška se na malém displeji zobrazí celá")
    p.wait_for_timeout(3000)
    c.ok(p.evaluate("document.getElementById('app-toast').classList.contains('show')"), "Delší hláška zůstane vidět déle, aby se dala dočíst")


def t_play(s, c):
    ctx, p, errs = s.page()
    p.evaluate("playTrack(1)")
    p.wait_for_timeout(2500)
    r = p.evaluate("()=>({paused:audio.paused,t:audio.currentTime,title:document.getElementById('track-title').textContent,src:audio.src.split('/').pop()})")
    c.ok(not r["paused"] and r["t"] > 0.5, "Skladba hraje (%s, čas %.1f s)" % (r["title"], r["t"]))
    p.evaluate("playNextTrack()")
    p.wait_for_timeout(2000)
    r2 = p.evaluate("()=>({paused:audio.paused,src:audio.src.split('/').pop(),title:document.getElementById('track-title').textContent})")
    c.ok(not r2["paused"] and r2["src"] != r["src"], "Další skladba naskočí (%s)" % r2["title"])
    p.click("#btn-play")
    p.wait_for_timeout(600)
    c.ok(p.evaluate("audio.paused"), "Tlačítko pauzy skladbu zastaví")
    p.click("#btn-play")
    p.wait_for_timeout(1200)
    c.ok(not p.evaluate("audio.paused"), "Tlačítko play ji znovu spustí")
    v0 = p.evaluate("+document.getElementById('volume-slider').value")
    p.evaluate("adjustVolume(0.05)")
    p.wait_for_timeout(300)
    v1 = p.evaluate("+document.getElementById('volume-slider').value")
    c.ok(v1 > v0, "Hlasitost jde přidat (%.2f → %.2f)" % (v0, v1))
    p.evaluate("toggleMute()")
    c.ok(p.evaluate("audio.muted"), "Ztlumení funguje")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_switch(s, c):
    ctx, p, errs = s.page()
    p.evaluate("playlistEl.scrollTop=300")
    p.wait_for_timeout(200)
    b0 = p.evaluate("playlistEl.scrollTop")
    p.evaluate("playlistEl.children[12].click()")
    p.wait_for_timeout(700)
    c.ok(abs(p.evaluate("playlistEl.scrollTop") - b0) < 2, "Klepnutí na skladbu neposune seznam")
    p.evaluate(PIANO_JS)
    p.wait_for_timeout(700)
    r = p.evaluate("()=>({top:playlistEl.scrollTop,key:currentPlaylistKey,first:playlistEl.querySelector('.track-name').textContent})")
    c.ok(r["key"] == "piano" and r["top"] == 0, "Po přepnutí na PIANO začíná seznam nahoře")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_queue(s, c):
    ctx, p, errs = s.page()
    p.evaluate("()=>{for(let i=0;i<4;i++)window.addToCustomQueue('asmr',i)}")
    p.wait_for_timeout(400)
    r = p.evaluate("()=>({n:customQueue.length,liked:isTrackInCustomQueue('asmr',2),heart:document.getElementById('heart-btn-2').innerText})")
    c.ok(r["n"] == 4 and r["liked"] and r["heart"] == "♥", "Skladby jdou přidat do My Playlist a srdíčko svítí")
    p.evaluate("()=>{toggleCustomQueue();}")
    p.wait_for_timeout(300)
    p.evaluate("()=>{document.querySelectorAll('#queue-list li .history-name')[1].click();toggleCustomQueue();}")
    p.wait_for_timeout(2000)
    r = p.evaluate("()=>({q:mainListShowsQueue,active:customQueueActive,idx:customQueueIndex,paused:audio.paused,rows:playlistEl.children.length})")
    c.ok(r["q"] and r["active"] and r["idx"] == 1 and not r["paused"] and r["rows"] == 4, "My Playlist se přehrává a je vidět v hlavním okně")
    dl = "()=>[getComputedStyle(document.getElementById('btn-download')).display==='none',getComputedStyle(document.getElementById('offline-status')).display==='none'].join()"
    geo = """()=>{const W=document.documentElement.clientWidth;const row=document.querySelector('.effects-container');
      const bs=Array.from(row.children).filter(e=>getComputedStyle(e).display!=='none'&&e.getBoundingClientRect().width>0).map(e=>e.getBoundingClientRect());
      const lines={};bs.forEach(r=>{const k=Math.round((r.top+r.height/2)/14);(lines[k]=lines[k]||[]).push(r)});
      const off=Object.values(lines).map(l=>Math.abs((Math.min(...l.map(r=>r.left))+Math.max(...l.map(r=>r.right)))/2-W/2));
      return {h:Math.round(row.getBoundingClientRect().height),top:Math.round(document.querySelector('.player-section').getBoundingClientRect().top),off:Math.max(...off)}}"""
    c.ok(p.evaluate(dl) == "true,true", "V My Playlist zmizí DOWNLOAD OFFLINE i počet stažených skladeb")
    g1 = p.evaluate(geo)
    c.ok(g1["off"] <= 2, "Zbylá tlačítka (WEATHER, SUNSET, MY PLAYLIST) jsou vycentrovaná")
    p.evaluate("playNextTrack()")
    p.wait_for_timeout(1500)
    c.ok(p.evaluate("customQueueIndex") == 2, "Další skladba je ta následující z My Playlist")
    p.evaluate(PIANO_JS)
    p.wait_for_timeout(700)
    c.ok(p.evaluate(dl) == "false,false" and not p.evaluate("mainListShowsQueue"), "Po přepnutí na PIANO jsou zpět")
    g2 = p.evaluate(geo)
    c.ok(g1["h"] == g2["h"] and g1["top"] == g2["top"], "Přehrávač se při tom na obrazovce neposune")
    p.reload()
    p.wait_for_timeout(1500)
    c.ok(p.evaluate("customQueue.length") == 4, "My Playlist zůstane uložený i po novém načtení")
    p.evaluate("toggleTrackLike('asmr',0)")
    c.ok(p.evaluate("customQueue.length") == 3, "Srdíčkem jde skladba odebrat")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_sections(s, c):
    ctx, p, errs = s.page()
    r = p.evaluate("""()=>{for(let i=0;i<6;i++)window.addToCustomQueue('asmr',i);queueMoods=['Sleep','Work'];saveQueueMoods();
      customQueue[2].mood='Sleep';customQueue[3].mood='Sleep';customQueue[4].mood='Work';saveCustomQueue();renderCustomQueue();
      const n0=customQueue.length;copyEntryToMood(2,'Work');
      const out={n0,n1:customQueue.length,sleep:moodCount('Sleep'),work:moodCount('Work')};
      copyEntryToMood(2,'Work');out.n2=customQueue.length;
      customQueueActive=true;customQueueMoodPlay=null;queueMoodView=null;customQueueIndex=5;const seq=[];let g=0;
      do{customQueueIndex=queueNeighborPos(1);seq.push(customQueueIndex)}while(customQueueIndex!==5&&++g<30);out.seq=seq.join();
      customQueueMoodPlay='Work';customQueueIndex=4;const w=[];for(let i=0;i<2;i++){customQueueIndex=queueNeighborPos(1);w.push(customQueue[customQueueIndex].mood)}
      customQueueMoodPlay=null;customQueueActive=false;out.w=w.join();
      toggleTrackLike('asmr',2);out.afterHeart=customQueue.length;out.liked=isTrackInCustomQueue('asmr',2);return out}""")
    c.ok(r["n1"] == r["n0"] + 1 and r["sleep"] == 2 and r["work"] == 2, "Skladbu jde dát i do druhé sekce")
    c.ok(r["n2"] == r["n1"], "Do stejné sekce nejde dát dvakrát")
    c.ok(r["seq"] == "0,1,2,3,4,5", "Při přehrávání celého My Playlist hraje taková skladba jen jednou")
    c.ok(r["w"] == "Work,Work", "Sekce přehrává své skladby")
    c.ok(r["afterHeart"] == r["n1"] - 2 and not r["liked"], "Srdíčko odebere skladbu ze všech sekcí")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_find(s, c):
    ctx, p, errs = s.page()
    p.click("#pl-find-btn")
    p.wait_for_timeout(300)
    r = p.evaluate("()=>({vis:document.getElementById('plfind-panel').classList.contains('visible'),scope:document.getElementById('plfind-scope').textContent})")
    c.ok(r["vis"] and r["scope"] == "ASMR", "Tlačítko hledání otevře okno „Hledat v ASMR“")
    name = p.evaluate("playlistEl.children[7].querySelector('.track-name').textContent")
    p.fill("#plfind-input", name)
    p.wait_for_timeout(300)
    res = p.evaluate("Array.from(document.querySelectorAll('#plfind-results li .history-name')).map(x=>x.textContent)")
    c.ok(name in res, "Najde skladbu podle názvu (%s)" % name)
    p.fill("#plfind-input", "nebula piano")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("document.querySelectorAll('#plfind-results li').length") == 0, "Nehledá v jiných playlistech")
    p.fill("#plfind-input", name)
    p.wait_for_timeout(300)
    p.evaluate("(n)=>Array.from(document.querySelectorAll('#plfind-results li')).find(l=>l.querySelector('.history-name').textContent===n).querySelector('.search-play').click()", name)
    p.wait_for_timeout(1800)
    r = p.evaluate("()=>({paused:audio.paused,title:document.getElementById('track-title').textContent,vis:document.getElementById('plfind-panel').classList.contains('visible')})")
    c.ok(not r["paused"] and r["title"].lower() == name.lower() and not r["vis"], "Tlačítko ▶ u výsledku skladbu přehraje a okno zavře")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_sort(s, c):
    ctx, p, errs = s.page()
    vis = """()=>Array.from(playlistEl.children).map((li,i)=>({i,top:li.getBoundingClientRect().top+playlistEl.scrollTop,name:li.querySelector('.track-name').textContent,
      dur:(li.querySelector('.track-dur')||{}).textContent||''})).sort((a,b)=>a.top-b.top)"""
    geo = "()=>{const r=playlistEl.getBoundingClientRect(),w=document.querySelector('.player-section').getBoundingClientRect();return [r.left,r.top,r.width,r.height,w.height].map(Math.round).join()}"
    g0 = p.evaluate(geo)
    orig = [r["name"] for r in p.evaluate(vis)]
    c.ok(p.evaluate("!!document.getElementById('pl-sort-btn')"), "Pod hledáním je tlačítko řazení")
    p.click("#pl-sort-btn")
    p.wait_for_timeout(400)
    rows = p.evaluate(vis)
    names = [r["name"] for r in rows]
    want = p.evaluate("(n)=>n.slice().sort((a,b)=>a.localeCompare(b,undefined,{numeric:true,sensitivity:'base'}))", orig)
    c.ok(names == want and names != orig, "První klepnutí seřadí seznam podle abecedy")
    c.ok(p.evaluate("Array.from(playlistEl.children).map(li=>li.querySelector('.track-name').textContent)") == orig, "Vnitřní pořadí skladeb se nemění (srdíčka a uložené playlisty zůstávají platné)")
    c.ok(p.evaluate(geo) == g0, "Velikost seznamu ani okna se nezměnila")
    first = next(r for r in rows if "SPECIAL" not in r["name"].upper())
    p.evaluate("(i)=>playTrack(i)", first["i"])
    p.wait_for_timeout(1200)
    p.evaluate("playNextTrack()")
    p.wait_for_timeout(1200)
    after = [r for r in rows if "SPECIAL" not in r["name"].upper()]
    c.ok(p.evaluate("currentTrackIndex") == after[1]["i"], "Další skladba je ta pod ní v seřazeném seznamu")
    p.evaluate("audio.pause()")
    p.click("#pl-sort-btn")
    p.wait_for_timeout(500)
    rows = p.evaluate(vis)
    secs = []
    for r in rows:
        m = re.match(r"^(\d+):(\d\d)$", r["dur"])
        secs.append(int(m.group(1)) * 60 + int(m.group(2)) if m else None)
    known = [x for x in secs if x is not None]
    c.ok(len(known) == len(rows) and known == sorted(known), "Druhé klepnutí seřadí podle délky a u skladeb ukáže čas (%s … %s)" % (rows[0]["dur"], rows[-1]["dur"]))
    p.reload()
    p.wait_for_timeout(1500)
    c.ok([r["name"] for r in p.evaluate(vis)] == [r["name"] for r in rows], "Zvolené řazení se pamatuje")
    p.evaluate(PIANO_JS)
    p.wait_for_timeout(600)
    pr = p.evaluate(vis)
    c.ok(all(r["dur"] for r in pr) and p.evaluate("playlistEl.classList.contains('pl-sorted')"), "Platí i pro další playlist (PIANO)")
    p.click("#pl-sort-btn")
    p.wait_for_timeout(400)
    back = p.evaluate("()=>({sorted:playlistEl.classList.contains('pl-sorted'),names:Array.from(playlistEl.children).map(li=>li.querySelector('.track-name').textContent)})")
    c.ok(not back["sorted"] and [r["name"] for r in p.evaluate(vis)] == back["names"], "Třetí klepnutí vrátí původní pořadí")
    # My Playlist: vlastni volba razeni, ulozene poradi se nemeni
    p.evaluate("()=>{[5,1,3,8].forEach(i=>window.addToCustomQueue('piano',i));toggleCustomQueue()}")
    p.wait_for_timeout(300)
    p.evaluate("()=>{document.querySelectorAll('#queue-list li .history-name')[0].click();toggleCustomQueue()}")
    p.wait_for_timeout(1200)
    want = p.evaluate("[5,1,3,8].map(i=>displayTrackTitle(tracks.piano[i].title))")
    q = p.evaluate("()=>({q:mainListShowsQueue,sorted:playlistEl.classList.contains('pl-sorted')})")
    c.ok(q["q"] and not q["sorted"] and [r["name"] for r in p.evaluate(vis)] == want, "My Playlist se nejdřív ukáže v pořadí, jak ho uživatel poskládal")
    p.click("#pl-sort-btn")
    p.wait_for_timeout(400)
    names = [r["name"] for r in p.evaluate(vis)]
    c.ok(names == p.evaluate("(n)=>n.slice().sort((a,b)=>a.localeCompare(b,undefined,{numeric:true,sensitivity:'base'}))", want) and names != want,
         "Tlačítko řazení funguje i v My Playlist (A–Z)")
    c.ok(p.evaluate("customQueue.map(e=>displayTrackTitle(e.title))") == want, "Uložené pořadí My Playlist se tím nemění")
    rows = p.evaluate(vis)
    p.evaluate("(i)=>playCustomQueueAt(i)", p.evaluate("(n)=>customQueue.findIndex(e=>displayTrackTitle(e.title)===n)", rows[0]["name"]))
    p.wait_for_timeout(1000)
    p.evaluate("playNextTrack()")
    p.wait_for_timeout(1000)
    c.ok(p.evaluate("displayTrackTitle(customQueue[customQueueIndex].title)") == rows[1]["name"], "Další skladba v My Playlist je ta pod ní v seřazeném seznamu")
    p.click("#pl-sort-btn")
    p.wait_for_timeout(500)
    rows = p.evaluate(vis)
    secs = [int(r["dur"].split(":")[0]) * 60 + int(r["dur"].split(":")[1]) if re.match(r"^\d+:\d\d$", r["dur"]) else None for r in rows]
    c.ok(None not in secs and secs == sorted(secs), "My Playlist jde seřadit i podle délky")
    p.click("#pl-sort-btn")
    p.wait_for_timeout(400)
    c.ok([r["name"] for r in p.evaluate(vis)] == want and not p.evaluate("playlistEl.classList.contains('pl-sorted')"), "Třetí klepnutí vrátí vlastní pořadí")
    p.evaluate("audio.pause()")
    p.evaluate(PIANO_JS)
    p.wait_for_timeout(600)
    c.ok(not p.evaluate("playlistEl.classList.contains('pl-sorted')"), "Řazení My Playlist a řazení PIANO jsou na sobě nezávislé")
    # trvale serazeni My Playlist podle delky (tlacitko v okne My Playlist)
    p.evaluate("toggleCustomQueue()")
    p.wait_for_timeout(300)
    labels = p.evaluate("Array.from(document.querySelectorAll('#queue-bulk-bar button, .myfiles-bulk-row button')).map(b=>b.textContent)")
    c.ok(any("LENGTH" in x or "DÉLKY" in x for x in labels), "V okně My Playlist je vedle ŘADIT A–Z i řazení podle délky")
    p.evaluate("sortQueueByLength()")
    p.wait_for_timeout(1200)
    d = p.evaluate("customQueue.map(e=>trackDuration(e))")
    c.ok(None not in d and d == sorted(d), "Tlačítko seřadí My Playlist podle délky natrvalo")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_layer(s, c):
    ctx, p, errs = s.page()
    p.evaluate("playTrack(3)")
    p.wait_for_timeout(1200)
    p.evaluate("pickLayerTrack(4)")
    p.wait_for_timeout(800)
    r = p.evaluate("""()=>{const rnd=new Set();for(let i=0;i<300;i++)rnd.add(randomTrackIndex(playlist,currentTrackIndex));
      return {layer:layerActive,seq:stepTrackIndex(playlist,currentTrackIndex,1),has:rnd.has(4)}}""")
    c.ok(r["layer"], "Druhá vrstva (ASMR MIX) se zapne")
    c.ok(r["seq"] == 5, "Po dohrání naskočí až skladba ZA tou z druhé vrstvy")
    c.ok(not r["has"], "Náhodný výběr nikdy nevybere skladbu z druhé vrstvy")
    p.evaluate("playNextTrack()")
    p.wait_for_timeout(1200)
    r = p.evaluate("()=>({main:audio.src.split('/').pop(),layer:layerAudio.src.split('/').pop(),on:layerActive})")
    c.ok(r["main"] != r["layer"] and r["on"], "Skladba se nezdvojí, vrstva hraje dál")
    marks = "Array.from(playlistEl.children).filter(li=>li.classList.contains('layer-track')).length"
    c.ok(p.evaluate(marks) == 1, "Značku druhé vrstvy (2) má v seznamu jen jeden řádek")
    # stejna skladba nesmi hrat jako hlavni i jako druha vrstva
    p.evaluate("stopLayer()")
    p.evaluate("pickLayerTrack(currentTrackIndex)")
    p.wait_for_timeout(600)
    c.ok(not p.evaluate("layerActive") and p.evaluate("layerAudio.paused"), "Právě hrající skladbu nejde přidat sama na sebe jako druhou vrstvu")
    p.evaluate("pickLayerTrack(7)")
    p.wait_for_timeout(600)
    p.evaluate("playlistEl.children[7].click()")
    p.wait_for_timeout(1500)
    r = p.evaluate("()=>({on:layerActive,layerPaused:layerAudio.paused,main:audio.src.split('/').pop(),paused:audio.paused,want:playlist[7].src.split('/').pop()})")
    c.ok(not r["on"] and r["layerPaused"] and r["main"] == r["want"] and not r["paused"], "Klepnutí na skladbu z druhé vrstvy ji pustí jako hlavní a vrstvu vypne (nehraje dvakrát)")
    # po ulozeni a novem spusteni zustane jedna vrstva
    p.evaluate("playTrack(3)")
    p.wait_for_timeout(900)
    p.evaluate("pickLayerTrack(5)")
    p.wait_for_timeout(500)
    p.evaluate("saveSettings()")
    p.reload()
    p.wait_for_timeout(2500)
    p.evaluate("()=>{if(audio.paused)document.getElementById('btn-play').click()}")
    p.wait_for_timeout(1500)
    r = p.evaluate("()=>({on:layerActive,marks:%s,audios:[audio,layerAudio].filter(a=>!a.paused).length})" % marks)
    c.ok(r["on"] and r["marks"] == 1 and r["audios"] == 2, "Po novém spuštění se druhá vrstva obnoví právě jednou")
    p.click("#btn-layer")
    p.wait_for_timeout(400)
    c.ok(not p.evaluate("layerActive") and p.evaluate(marks) == 0, "Tlačítko vrstvy ji po obnovení vypne (nepřidá další)")
    # My Playlist se stejnou skladbou ve dvou sekcich: znacka jen u jednoho radku
    p.evaluate("""()=>{for(let i=0;i<4;i++)window.addToCustomQueue('asmr',i);queueMoods=['Sleep','Work'];saveQueueMoods();
      customQueue[1].mood='Sleep';saveCustomQueue();copyEntryToMood(1,'Work');toggleCustomQueue()}""")
    p.wait_for_timeout(300)
    p.evaluate("()=>{document.querySelectorAll('#queue-list li .history-name')[0].click();toggleCustomQueue()}")
    p.wait_for_timeout(1200)
    p.evaluate("pickLayerTrack(4)")
    p.wait_for_timeout(700)
    r = p.evaluate("()=>({q:mainListShowsQueue,rows:playlistEl.children.length,marked:Array.from(playlistEl.children).map((li,i)=>li.classList.contains('layer-track')?i:-1).filter(i=>i>=0)})")
    c.ok(r["q"] and r["rows"] == 5 and r["marked"] == [4], "Skladba ve dvou sekcích My Playlist: značka 2 je jen u řádku, na který se klepnulo")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_offline(s, c):
    s.env.reset()
    ctx, p, errs = s.page(sw=True)
    p.evaluate(CLEAR_AUDIO_CACHE_JS)
    p.evaluate(PIANO_JS)
    p.wait_for_timeout(600)
    p.evaluate("runOfflineDownload([2,3])")
    p.wait_for_timeout(5000)
    r = p.evaluate("async()=>({a:await isTrackCached(tracks.piano[2].src),b:await isTrackCached(tracks.piano[3].src),badge:document.getElementById('offline-badge-2').innerText})")
    c.ok(r["a"] and r["b"], "DOWNLOAD OFFLINE uloží vybrané skladby")
    c.ok("OFFLINE" in r["badge"], "U stažené skladby je značka ✓ OFFLINE")
    ctx.set_offline(True)
    p.wait_for_timeout(300)
    p.evaluate("playTrack(2)")
    p.wait_for_timeout(2500)
    c.ok(not p.evaluate("audio.paused") and p.evaluate("audio.currentTime") > 0.5, "Stažená skladba hraje i bez internetu")
    p.evaluate("audio.pause()")
    p.evaluate("runOfflineDownload([5])")
    p.wait_for_timeout(700)
    toast = p.evaluate(TOAST_JS)
    btn = p.evaluate("document.getElementById('btn-download').innerText")
    c.ok(re.search("internet", toast, re.I) and not re.search("FAILED|DOWNLOADING", btn), "Bez internetu stahování řekne, že je potřeba připojení (%s)" % toast[:60])
    ctx.set_offline(False)
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_playing_download(s, c):
    s.env.reset()
    ctx, p, errs = s.page(sw=True)
    p.evaluate(CLEAR_AUDIO_CACHE_JS)
    p.evaluate(PIANO_JS)
    p.wait_for_timeout(600)
    p.evaluate("playTrack(2)")
    p.wait_for_timeout(2000)
    p.evaluate("runOfflineDownload([2,3])")
    p.wait_for_timeout(4500)
    r = p.evaluate("async()=>({a:await isTrackCached(tracks.piano[2].src),b:await isTrackCached(tracks.piano[3].src),btn:document.getElementById('btn-download').innerText})")
    c.ok(not r["a"] and r["b"] and "FAILED" not in r["btn"], "Právě hrající skladba se odloží, ostatní se stáhnou, nehlásí se chyba")
    p.evaluate("playTrack(4)")
    p.wait_for_timeout(9000)
    c.ok(p.evaluate("isTrackCached(tracks.piano[2].src)"), "Odložená skladba se uloží sama, když začne hrát jiná")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_r2(s, c):
    s.env.reset()
    ctx, p, errs = s.page(sw=True)
    p.evaluate(CLEAR_AUDIO_CACHE_JS)
    p.evaluate("playTrack(2)")
    p.wait_for_timeout(2500)
    c.ok(not p.evaluate("audio.paused") and s.env.gh["hits"] > 0 and s.env.r2["hits"] == 0, "Běžně se hraje z GitHubu, R2 se nepoužije")
    p.evaluate("audio.pause()")
    s.env.gh.update(mode="dead", hits=0)
    s.env.r2["hits"] = 0
    p.evaluate("playTrack(6)")
    okp = False
    for _ in range(30):
        p.wait_for_timeout(1000)
        if p.evaluate("!audio.paused && audio.currentTime>0.5 && /Noise|noise|./.test(audio.src)") and s.env.r2["hits"] > 0:
            okp = True
            break
    title = p.evaluate("document.getElementById('track-title').textContent")
    c.ok(okp, "Když GitHub nejde, skladba se sama přehraje ze zálohy R2 (%s)" % title)
    s.env.reset()
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_xp_download(s, c):
    s.env.reset()
    ctx, p, errs = s.page(sw=True)
    popups = []
    ctx.on("page", lambda pg: popups.append(1))
    p.evaluate(CLEAR_AUDIO_CACHE_JS)
    with p.expect_download(timeout=10000) as dl:
        p.evaluate("downloadTrackFile(playlist[2].src, playlist[2].title)")
    d = dl.value
    size = os.path.getsize(d.path())
    c.ok(d.suggested_filename.lower().endswith(".mp3") and size > 100000, "Stažení za XP uloží soubor do zařízení (%s)" % d.suggested_filename)
    c.ok(s.env.r2["hits"] >= 1 and s.env.gh["hits"] == 0, "Soubor se bere ze zálohy R2")
    c.ok(p.evaluate(CACHE_TYPE_JS, p.evaluate("playlist[2].src")) is None, "Stažení za XP nesahá na offline úložiště")
    # opaque kopie z GitHubu + bez internetu -> hlaska
    p.evaluate("fetch(playlist[3].src,{mode:'no-cors'})")
    p.wait_for_timeout(1500)
    ctx.set_offline(True)
    p.wait_for_timeout(300)
    n0 = len(popups)
    p.evaluate("downloadTrackFile(playlist[3].src,'x')")
    p.wait_for_timeout(1200)
    toast = p.evaluate(TOAST_JS)
    c.ok(re.search("internet", toast, re.I) and len(popups) == n0, "Bez internetu řekne, že je potřeba připojení, a neotevře prázdnou kartu")
    ctx.set_offline(False)
    p.wait_for_timeout(300)
    # R2 nejde -> posledni zachrana = odkaz na GitHub
    s.env.r2.update(mode="dead")
    n0 = len(popups)
    p.evaluate("downloadTrackFile(playlist[5].src,'x')")
    p.wait_for_timeout(3000)
    c.ok(len(popups) > n0, "Když R2 nejde, otevře se odkaz na GitHub jako poslední záchrana")
    s.env.reset()
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_xp_no_break(s, c):
    """Chyba z 14. 11.: stazeni za XP behem poslechu nesmi rozbit prehravani."""
    s.env.reset()
    ctx, p, errs = s.page(sw=True)
    p.evaluate(CLEAR_AUDIO_CACHE_JS)
    p.evaluate("fetch(playlist[2].src,{mode:'no-cors'})")
    p.wait_for_timeout(1500)
    p.evaluate("window.__mediaErr=0;audio.addEventListener('error',()=>{window.__mediaErr++});playTrack(2)")
    p.wait_for_timeout(2000)
    with p.expect_download(timeout=10000):
        p.evaluate("downloadTrackFile(playlist[2].src,'x')")
    typ = p.evaluate(CACHE_TYPE_JS, p.evaluate("playlist[2].src"))
    c.ok(typ == "opaque", "Uložená offline kopie zůstala beze změny")
    for f in (0.4, 0.8):
        p.evaluate("(f)=>{audio.currentTime=audio.duration*f}", f)
        p.wait_for_timeout(1500)
    r = p.evaluate("()=>({err:window.__mediaErr,paused:audio.paused,t:audio.currentTime,d:audio.duration})")
    c.ok(r["err"] == 0 and not r["paused"] and r["t"] > r["d"] * 0.75, "Skladba po stažení za XP hraje dál až do konce")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_admin(s, c):
    s.env.reset()
    init = "try{if(!sessionStorage.getItem('x')){localStorage.setItem('synthlucida_music_source','r2');sessionStorage.setItem('x','1')}}catch(e){}"
    ctx, p, errs = s.page(sw=True, init=init)
    ui = """()=>({seg:Array.from(document.querySelectorAll('.su-seg[data-musicsrc] button')).map(b=>b.textContent+(b.getAttribute('aria-pressed')==='true'?'*':'')).join(),
      dis:document.getElementById('su-r2-mode').disabled,locked:document.getElementById('su-admin-r2-row').classList.contains('su-locked')})"""
    p.evaluate("toggleSetup()")
    p.wait_for_timeout(300)
    u = p.evaluate(ui)
    c.ok(u["dis"] and u["locked"] and u["seg"] == "AUTO*,SERVER 1", "Volba R2 je bez admina zamčená a zdroj je AUTO")
    p.evaluate("()=>{window.__slIsAdmin=true;document.dispatchEvent(new CustomEvent('sladminchange'))}")
    p.wait_for_timeout(400)
    u = p.evaluate(ui)
    c.ok(not u["dis"] and not u["locked"], "Po přihlášení admina se volba R2 odemkne")
    x = p.evaluate("(w)=>({avail:computeAvailableXP(stats),level:xpLevelInfo(computeXP(stats)).level,want:xpLevelInfo(w).level})", PCFG.get("admin_xp", 1000))
    c.ok(x["avail"] == PCFG.get("admin_xp", 1000) and x["level"] >= x["want"],
         "Admin dostane %d XP na vyzkoušení (úroveň %s)" % (PCFG.get("admin_xp", 1000), x["level"]))
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_iphone(s, c):
    ctx, p, errs = s.page(w=390, h=780, init=IOS_JS, user_agent=IPHONE_UA, has_touch=True, is_mobile=True)
    off = p.evaluate("Array.from(document.querySelectorAll('.ios-off')).map(e=>e.id||e.className.split(' ')[0])")
    need = ["btn-vol-down", "btn-vol-up", "vol-slider-row", "layer-knob-wrap", "crossfade-group", "btn-fullscreen-toggle", "btn-wakelock"]
    miss = [n for n in need if n not in off]
    c.ok(not miss, "Na iPhonu jsou nepodporované funkce zašedlé" + (" – chybí: " + ", ".join(miss) if miss else ""))
    c.ok("btn-mute" not in off and "btn-play" not in off, "Play a ztlumení zůstávají funkční")
    c.ok(p.evaluate("getComputedStyle(document.getElementById('ios-vol-note')).display") == "block", "Pod hlasitostí je vysvětlení pro iPhone")
    v0 = p.evaluate("document.getElementById('volume-slider').value")
    p.tap("#btn-vol-up", force=True)
    p.wait_for_timeout(400)
    toast = p.evaluate(TOAST_JS)
    c.ok(p.evaluate("document.getElementById('volume-slider').value") == v0 and "iPhone" in toast, "Klepnutí na zašedlé tlačítko jen vysvětlí proč")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))
    ctx.close()
    ctx, p, errs = s.page(w=412, h=800)
    c.ok(p.evaluate("document.querySelectorAll('.ios-off').length") == 0, "Na ostatních zařízeních není zašedlé nic")


def t_tour(s, c):
    sp_on = PCFG.get("sp_on", True)
    ctx, p, errs = s.page(seen=False, wait=max(2800, PCFG.get("sp_first", 1500) + 1300) if sp_on else 2800)
    r = p.evaluate("()=>({sp:!document.getElementById('support-popup').hidden,hint:!document.getElementById('tour-hint').hidden})")
    if sp_on:
        c.ok(r["sp"] and not r["hint"], "První návštěva: okno s poděkováním, bublina průvodce čeká")
        p.click("#sp-ok")
        p.wait_for_timeout(1800)
    else:
        c.ok(not r["sp"], "Úvodní okno je vypnuté (Editor playeru) – při první návštěvě se neukáže")
    r = p.evaluate("()=>({hint:!document.getElementById('tour-hint').hidden,blink:document.querySelector('.main-guide-btn').classList.contains('tour-attention')})")
    c.ok(r["hint"] and r["blink"], "Pak se ukáže bublina „Jsi tu poprvé?“ a bliká ⓘ")
    p.click("#tour-hint-go")
    p.wait_for_timeout(600)
    n = p.evaluate("document.getElementById('tour-step').textContent")
    c.ok(re.match(r"1 / \d+", n or ""), "Průvodce se spustí (krok %s)" % n)
    seen_layer = False
    for _ in range(14):
        if re.search("|".join(re.escape(x) for x in PCFG.get("layer_step", ["LAYER", "VRSTVA"])),
                     p.evaluate("document.getElementById('tour-card').innerText"), re.I):
            seen_layer = True
            break
        p.keyboard.press("ArrowRight")
        p.wait_for_timeout(400)
    c.ok(seen_layer, "Průvodce má krok o druhé ASMR vrstvě (i pod vlastní hudbou)")
    p.keyboard.press("Escape")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("document.getElementById('tour').hidden"), "Esc průvodce zavře")
    c.ok(p.evaluate("!!document.querySelector('.guide-layer-tip .guide-tip-text') && document.querySelector('.guide-layer-tip .guide-tip-text').textContent.trim().length>20"),
         "V návodu je zvýrazněný tip, že ASMR vrstvy fungují i v playlistech a pod vlastními soubory")
    p.reload()
    p.wait_for_timeout(3500)
    c.ok(p.evaluate("document.getElementById('tour-hint').hidden"), "Podruhé se bublina už neukáže")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_update(s, c):
    slog = "()=>({txt:document.getElementById('slogan-container').innerText,vis:document.getElementById('slogan-container').classList.contains('visible'),seen:localStorage.getItem('sl-app-version-seen')})"
    try:
        s.env.version_override = "9.0.1"
        ctx, p, errs = s.page(wait=2500)
        r = p.evaluate(slog)
        c.ok(not re.search("aktualiz|updated", r["txt"], re.I) and r["seen"] == "9.0.1", "Nový návštěvník hlášku o aktualizaci nevidí")
        s.env.version_override = "9.0.2"
        p.reload()
        p.wait_for_timeout(8000)
        r = p.evaluate(slog)
        c.ok("9.0.2" not in r["txt"], "Hned po načtení se hláška ještě neukazuje (stránka se může sama obnovit)")
        # stranka se behem cekani sama obnovi (jako po nahrani noveho sw.js) - hlaska se nesmi ztratit
        p.reload()
        p.wait_for_timeout(22500)
        r = p.evaluate(slog)
        c.ok("9.0.2" in r["txt"] and r["vis"], "Asi 20 s po načtení se hláška ukáže (%s)" % r["txt"].strip()[:60])
        p.reload()  # obnoveni uprostred zobrazeni -> ukaze se znovu
        p.wait_for_timeout(22500)
        r = p.evaluate(slog)
        c.ok("9.0.2" in r["txt"] and r["vis"], "Když ji obnovení stránky přeruší, ukáže se po něm znovu")
        p.wait_for_timeout(12500)
        r = p.evaluate(slog)
        c.ok(not r["vis"] and r["seen"] == "9.0.2", "Po doběhnutí zmizí a verze se zapíše jako viděná")
        p.reload()
        p.wait_for_timeout(22500)
        r = p.evaluate(slog)
        c.ok("9.0.2" not in r["txt"], "Pak už se neukazuje")
        c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))
    finally:
        s.env.version_override = None


def t_special(s, c):
    ctx, p, errs = s.page()
    st = "()=>({sub:document.querySelector('.brand-sub').textContent,tf:getComputedStyle(document.querySelector('.brand-wordmark .brand-name')).transform})"
    idx = p.evaluate("()=>{const k=Object.keys(tracks).find(k=>Array.isArray(tracks[k])&&tracks[k].some(t=>/special/i.test(t.title)));"
                     "return k?{k,i:tracks[k].findIndex(t=>/special/i.test(t.title)),n:tracks[k].findIndex(t=>!/special/i.test(t.title))}:null}")
    if not idx:
        c.ok(True, "V playeru není žádná skladba SPECIAL – test přeskočen")
        return
    p.evaluate("(k)=>Array.from(document.querySelectorAll('.btn-atmos')).find(x=>new RegExp(k,'i').test(x.textContent)).click()", idx["k"])
    p.wait_for_timeout(600)
    p.evaluate("(i)=>playTrack(i)", idx["i"])
    p.wait_for_timeout(2600)
    r = p.evaluate(st)
    c.ok(r["sub"] == "DancePlayer2" and "matrix(-1" in r["tf"], "Skladba SPECIAL: nadpis se zrcadlově otočí a je tam DancePlayer2")
    p.evaluate("(i)=>playTrack(i)", idx["n"])
    p.wait_for_timeout(2600)
    r = p.evaluate(st)
    c.ok(r["sub"] == "RelaxPlayer2" and r["tf"] == "none", "Běžná skladba vrátí nadpis zpět")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_orbs(s, c):
    ctx, p, errs = s.page()
    st = "()=>({n:getComputedStyle(document.body,'::before').backgroundImage.split('radial-gradient').length-1,dots:getComputedStyle(document.body,'::before').display,op:getComputedStyle(document.getElementById('btn-orbs')).opacity})"
    r = p.evaluate(st)
    c.ok(r["n"] == 2 and r["dots"] == "block", "Výchozí jsou původní koule")
    p.evaluate("toggleSetup()")
    p.wait_for_timeout(400)
    p.click('.su-seg[data-orbstyle] button[data-v="disco"]')
    p.wait_for_timeout(300)
    c.ok(p.evaluate(st)["n"] == 6, "V SETUP jde zapnout vzhled DISCO")
    p.reload()
    p.wait_for_timeout(1500)
    c.ok(p.evaluate(st)["n"] == 6, "Volba se pamatuje")
    p.evaluate("toggleSetup()")
    p.wait_for_timeout(300)
    p.click('.su-sw[data-opt="lite"]')
    p.wait_for_timeout(500)
    r = p.evaluate(st)
    c.ok(r["dots"] == "none" and r["op"] == "0.3", "V úsporném režimu koule zmizí a tlačítko ORBS zašedne")
    p.click('.su-sw[data-opt="lite"]')
    p.wait_for_timeout(500)
    p.evaluate("toggleSetup()")
    c.ok(p.evaluate(st)["dots"] == "block", "Po vypnutí úsporného režimu jsou zpět")
    for _ in range(4):
        if "NIGHT" in p.evaluate("document.getElementById('btn-sunset').innerText"):
            break
        p.click("#btn-sunset")
        p.wait_for_timeout(500)
    r = p.evaluate(st)
    night = "NIGHT" in p.evaluate("document.getElementById('btn-sunset').innerText")
    c.ok(night and r["dots"] == "none" and r["op"] == "0.3", "Při NIGHT koule zmizí a ORBS zašedne")
    for _ in range(4):
        if "OFF" in p.evaluate("document.getElementById('btn-sunset').innerText"):
            break
        p.click("#btn-sunset")
        p.wait_for_timeout(500)
    c.ok(p.evaluate(st)["dots"] == "block", "Po vypnutí NIGHT se vrátí")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_own_files(s, c):
    tmp = os.path.join(tempfile.gettempdir(), "Moje_Testovaci_Nahravka.wav")
    with open(tmp, "wb") as f:
        f.write(s.env.wav[:400000 + 44])
    ctx, p, errs = s.page()
    p.evaluate("toggleMainFiles()")
    p.wait_for_timeout(300)
    p.set_input_files("#myfiles-input", tmp)
    p.wait_for_timeout(1800)
    lib = p.evaluate("myFilesLibrary.map(f=>({id:f.id,name:f.name}))")
    c.ok(len(lib) == 1, "Vlastní soubor jde přidat do sekce F (%s)" % (lib[0]["name"] if lib else "-"))
    if lib:
        p.evaluate("(id)=>playMyFileNow(id)", lib[0]["id"])
        p.wait_for_timeout(2000)
        r = p.evaluate("()=>({blob:audio.src.indexOf('blob:')===0,paused:audio.paused})")
        c.ok(r["blob"] and not r["paused"], "Vlastní soubor se přehraje")
        p.evaluate("""(lib)=>{myPlaylists.push({id:'tmix',name:'Test mix',items:[{type:'file',fileId:lib.id,title:lib.name},
          {type:'track',playlist:'piano',index:1,title:tracks.piano[1].title}],createdAt:1});playMyPlaylist('tmix')}""", lib[0])
        p.wait_for_timeout(1800)
        r = p.evaluate("()=>({rows:playlistEl.children.length,dl:getComputedStyle(document.getElementById('btn-download')).display==='none'?'hidden':'shown',"
                       "na:document.getElementById('pl-sort-btn').classList.contains('pl-sort-na')})")
        c.ok(r["rows"] == 2 and r["dl"] == "hidden", "Vlastní playlist ze sekce F: DOWNLOAD OFFLINE zmizí")
        # ASMR vrstva pod vlastnim souborem: do playlistu pribude ASMR skladba a pusti se jako druha vrstva
        p.evaluate("""(lib)=>{const pl=myPlaylists.find(x=>x.id==='tmix');pl.items.push({type:'track',playlist:'asmr',index:2,title:tracks.asmr[2].title});playMyPlaylist('tmix')}""", lib[0])
        p.wait_for_timeout(1800)
        p.evaluate("pickLayerTrack(2)")
        p.wait_for_timeout(900)
        r = p.evaluate("()=>({own:audio.src.indexOf('blob:')===0,main:!audio.paused,on:layerActive,layer:!layerAudio.paused})")
        c.ok(r["own"] and r["main"] and r["on"] and r["layer"], "Pod vlastním souborem jde pustit ASMR skladbu jako druhou vrstvu")
        p.evaluate("()=>{stopLayer();const pl=myPlaylists.find(x=>x.id==='tmix');pl.items.pop();playMyPlaylist('tmix')}")
        p.wait_for_timeout(1500)
        p.evaluate("audio.pause()")
        p.click("#pl-sort-btn")
        p.wait_for_timeout(300)
        p.click("#pl-sort-btn")
        p.wait_for_timeout(2500)
        r = p.evaluate("""()=>({sorted:playlistEl.classList.contains('pl-sorted-len'),
          rows:Array.from(playlistEl.children).map(li=>({top:li.getBoundingClientRect().top,d:(li.querySelector('.track-dur')||{}).textContent||''})).sort((a,b)=>a.top-b.top).map(x=>x.d),
          stored:myPlaylists.find(x=>x.id==='tmix').items.map(i=>i.type)})""")
        secs = [int(x.split(":")[0]) * 60 + int(x.split(":")[1]) if re.match(r"^\d+:\d\d$", x) else None for x in r["rows"]]
        c.ok(r["sorted"] and None not in secs and secs == sorted(secs) and r["stored"] == ["file", "track"],
             "Playlist ze sekce F jde v hlavním okně seřadit podle délky (i vlastní soubor: %s), uložené pořadí se nemění" % ", ".join(r["rows"]))
        p.evaluate("()=>{const pl=myPlaylists.find(x=>x.id==='tmix');pl.items.reverse();sortMyPlaylistByLength('tmix')}")
        p.wait_for_timeout(1500)
        c.ok(p.evaluate("myPlaylists.find(x=>x.id==='tmix').items.map(i=>i.type)") == ["file", "track"], "Tlačítko PODLE DÉLKY v sekci F seřadí playlist natrvalo (kratší vlastní soubor první)")
        p.click("#pl-sort-btn")
        p.wait_for_timeout(300)
        p.evaluate("()=>{const el=document.getElementById('myfiles-gain');el.value=60;el.dispatchEvent(new Event('input',{bubbles:true}))}")
        p.wait_for_timeout(400)
        p.reload()
        p.wait_for_timeout(1500)
        c.ok(str(p.evaluate("document.getElementById('myfiles-gain').value")) == "60", "Hlasitost vlastních souborů se pamatuje")
        c.ok(p.evaluate("myFilesLibrary.length") == 1, "Soubor zůstane uložený i po novém načtení")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_own_bulk(s, c):
    """Sekce F: přeskočení už přidaných souborů, ukazatel průběhu, hromadné mazání."""
    wav = s.env.wav[:60000 + 44]

    def f(name, extra=0):
        return {"name": name, "mimeType": "audio/wav", "buffer": wav + b"\0" * extra}

    ctx, p, errs = s.page()
    p.evaluate("toggleMainFiles()")
    p.wait_for_timeout(300)
    if not p.evaluate("typeof bulkDeleteSelectedMyFiles==='function'&&!!document.getElementById('myfiles-upload')"):
        c.ok(False, "Player nemá hromadné mazání nebo ukazatel průběhu (je to aktuální player.html?)")
        return
    # průběh nahrávání: zaznamenat, co se během nahrávání ukazuje
    p.evaluate("""()=>{window.__up=[];const box=document.getElementById('myfiles-upload');
      new MutationObserver(()=>window.__up.push({hidden:box.hidden,pct:document.getElementById('myfiles-upload-pct').textContent,
        locked:document.getElementById('myfiles-add-btn').disabled})).observe(box,{subtree:true,childList:true,characterData:true,attributes:true})}""")
    p.set_input_files("#myfiles-input", [f("Test A.wav"), f("Test B.wav", 10), f("Test C.wav", 20), f("Test D.wav", 30)])
    p.wait_for_function("myFilesLibrary.length===4&&!document.getElementById('myfiles-add-btn').disabled", timeout=20000)
    up = p.evaluate("window.__up")
    pcts = [int(re.sub(r"\D", "", u["pct"]) or 0) for u in up if not u["hidden"]]
    c.ok(up and any(u["locked"] for u in up) and pcts and pcts[-1] == 100 and pcts == sorted(pcts),
         "Při nahrávání se ukáže průběh v procentech (%s) a tlačítko přidání je zatím zamčené" % " → ".join("%d %%" % x for x in pcts[:6]))
    p.wait_for_timeout(2900)
    c.ok(p.evaluate("document.getElementById('myfiles-upload').hidden"), "Ukazatel průběhu po dokončení sám zmizí")

    # duplicity: stejné soubory znovu + jeden nový + jeden dvakrát v jedné dávce
    p.set_input_files("#myfiles-input", [f("Test A.wav"), f("Test B.wav", 10), f("Test E.wav", 40), f("Test E.wav", 40)])
    p.wait_for_function("!document.getElementById('myfiles-add-btn').disabled", timeout=20000)
    p.wait_for_timeout(300)
    names = sorted(p.evaluate("myFilesLibrary.map(x=>x.name)"))
    toast = p.evaluate(TOAST_JS)
    c.ok(names == ["Test A", "Test B", "Test C", "Test D", "Test E"],
         "Už přidaný soubor se při dalším nahrání přeskočí a nezdvojí se (v knihovně: %d)" % len(names))
    c.ok(re.search(r"(Skipped|Přeskočeno) 3\b", toast), "Hláška řekne, kolik souborů se přeskočilo (2 už přidané + 1 vybraný dvakrát)")
    p.set_input_files("#myfiles-input", [f("Test A.wav", 999)])
    p.wait_for_function("myFilesLibrary.length===6", timeout=20000)
    c.ok(True, "Soubor se stejným názvem, ale jinou velikostí se přidá (jiná verze)")

    # hromadné mazání: soubory v playlistu F, v My Playlist, v historii a jeden právě hraje
    p.evaluate("""async()=>{myPlaylists.push({id:'tbulk',name:'Bulk',items:myFilesLibrary.map(x=>({type:'file',fileId:x.id,title:x.name})),createdAt:1});
      saveMyPlaylistsToStorage();toggleMyFilesSelectMode();selectAllMyFiles(true);await bulkAddSelectedToQueue();selectAllMyFiles(false);
      localStorage.setItem(HISTORY_KEY,JSON.stringify(myFilesLibrary.map(x=>({title:x.name,playlist:'custom:file:'+x.id,fileId:x.id,time:Date.now()}))));
      await playMyFileNow(myFilesLibrary.find(x=>x.name==='Test B').id);
      myFilesLibrary.filter(x=>x.name==='Test B'||x.name==='Test C').forEach(x=>toggleMyFileSelected(x.id,true));}""")
    p.wait_for_timeout(800)
    btn = p.evaluate("()=>{const b=document.querySelector('#myfiles-bulk .myfiles-bulk-del');return b?{t:b.textContent,d:b.disabled}:null}")
    c.ok(btn and not btn["d"] and "(2)" in btn["t"], "V režimu výběru je tlačítko SMAZAT VYBRANÉ s počtem (%s)" % (btn["t"] if btn else "chybí"))
    p.evaluate("document.querySelector('#myfiles-bulk .myfiles-bulk-del').click()")
    p.wait_for_timeout(1500)
    r = p.evaluate("""async()=>({lib:myFilesLibrary.map(x=>x.name).sort(),db:(await myFilesDbGetAll()).length,
      pl:myPlaylists.find(x=>x.id==='tbulk').items.map(i=>i.title).sort(),q:customQueue.map(e=>e.title).sort(),
      h:JSON.parse(localStorage.getItem(HISTORY_KEY)||'[]').map(x=>x.title).sort(),blob:audio.src.indexOf('blob:')===0&&!audio.paused})""")
    left = ["Test A", "Test A", "Test D", "Test E"]
    c.ok(r["lib"] == left and r["db"] == 4, "Hromadné mazání smaže jen vybrané soubory (zbývá %d)" % r["db"])
    c.ok(r["pl"] == left and r["q"] == left and r["h"] == left, "Smazané soubory zmizí i z playlistu F, z My Playlist a z historie")
    c.ok(not r["blob"], "Právě hrající smazaný soubor se zastaví")

    p.evaluate("()=>{if(!myFilesSelectMode)toggleMyFilesSelectMode();selectAllMyFiles(true)}")
    p.evaluate("document.querySelector('#myfiles-bulk .myfiles-bulk-del').click()")
    p.wait_for_timeout(1500)
    r = p.evaluate("async()=>({lib:myFilesLibrary.length,db:(await myFilesDbGetAll()).length,q:customQueue.length,bar:document.getElementById('myfiles-bulk').children.length})")
    c.ok(r["lib"] == 0 and r["db"] == 0 and r["q"] == 0 and r["bar"] == 0, "☑ VŠE + SMAZAT VYBRANÉ smaže všechny vlastní soubory")
    c.ok(p.evaluate("document.querySelectorAll('#playlist li').length") >= 5, "Hlavní okno se potom vrátí na běžný playlist")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_own_backup(s, c):
    """Sekce F: záloha seznamů (malý .json) a obnovení na „druhém zařízení“."""
    def f(name, sec):
        return {"name": name, "mimeType": "audio/wav", "buffer": make_wav(sec, 8000)}
    A, B, C, D = f("Alfa.wav", 3), f("Beta.wav", 4), f("Gama.wav", 5), f("Delta.wav", 6)
    STATE = """()=>({pls:myPlaylists.map(pl=>pl.name+': '+pl.items.map(i=>i.type==='file'?((myFilesLibrary.find(x=>x.id===i.fileId)||{name:'??'}).name):'T').join(',')),
      folders:myFolders.map(fo=>fo.name+': '+myFilesLibrary.filter(x=>myFolderMap[x.id]===fo.id).map(x=>x.name).sort().join(',')),
      pending:document.getElementById('myfiles-pending').hidden?0:(JSON.parse(localStorage.getItem('synthlucida_myfiles_pending')||'{"files":[]}').files.length)})"""

    def upload(p, files):
        p.set_input_files("#myfiles-input", files)
        p.wait_for_function("!document.getElementById('myfiles-add-btn').disabled", timeout=20000)
        p.wait_for_timeout(400)

    # --- první zařízení: soubory, složka, dva playlisty (jeden název dvakrát), smíchané se SYNTHLUCIDA skladbami
    ctx, p, errs = s.page()
    if not p.evaluate("typeof exportMyFilesBackup==='function'&&!!document.getElementById('myfiles-backup-input')"):
        c.ok(False, "Player nemá zálohu seznamů (je to aktuální player.html?)")
        return
    p.evaluate("toggleMainFiles()")
    upload(p, [A, B, C, D])
    p.evaluate("""()=>{const id=n=>myFilesLibrary.find(x=>x.name===n).id;
      document.getElementById('myfiles-folder-name').value='Chill';createMyFolder();moveFilesToFolder([id('Alfa'),id('Beta')],myFolders[0].id);
      const T=(pl,i)=>({type:'track',playlist:pl,index:i,title:tracks[pl][i].title,src:tracks[pl][i].src});
      myPlaylists.push({id:'b1',name:'Večer',createdAt:1,items:[T('piano',1),{type:'file',fileId:id('Alfa'),title:'Alfa'},T('asmr',2),{type:'file',fileId:id('Gama'),title:'Gama'}]});
      myPlaylists.push({id:'b2',name:'Dup',createdAt:2,items:[{type:'file',fileId:id('Beta'),title:'Beta'},{type:'file',fileId:id('Delta'),title:'Delta'}]});
      myPlaylists.push({id:'b3',name:'Dup',createdAt:3,items:[{type:'file',fileId:id('Alfa'),title:'Alfa'}]});
      saveMyPlaylistsToStorage();renderMyFiles();}""")
    want = p.evaluate(STATE)
    with p.expect_download() as dl:
        p.evaluate("exportMyFilesBackup()")
    path = os.path.join(tempfile.gettempdir(), "testplayeru_zaloha.json")
    dl.value.save_as(path)
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    c.ok(size < 20000 and data.get("kind") == "synthlucida-myfiles-backup" and len(data.get("playlists", [])) == 3 and len(data.get("files", [])) == 4,
         "Záloha je malý soubor bez hudby (%d B, playlisty 3, soubory 4)" % size)
    ctx.close()

    # --- druhé zařízení: Gama už je nahraná, playlist „dup“ už existuje a jiný playlist musí zůstat
    ctx, p, errs2 = s.page()
    p.evaluate("toggleMainFiles()")
    upload(p, [C])
    p.evaluate("()=>{myPlaylists.push({id:'x1',name:'dup',createdAt:1,items:[]},{id:'x2',name:'Moje',createdAt:2,items:[]});saveMyPlaylistsToStorage();renderMyFiles()}")
    p.set_input_files("#myfiles-backup-input", path)
    p.wait_for_timeout(800)
    r = p.evaluate(STATE)
    c.ok(r["pending"] == 3, "Po obnovení se ukáže, kolik souborů ze zálohy ještě chybí (%s)" % r["pending"])
    p.reload()
    p.wait_for_timeout(1500)
    p.evaluate("toggleMainFiles()")
    p.wait_for_timeout(400)
    c.ok(p.evaluate(STATE)["pending"] == 3, "Čekání na chybějící soubory přežije nové načtení appky")
    upload(p, [D, B])
    upload(p, [A])
    r = p.evaluate(STATE)
    norm = lambda pls: sorted(x.lower() for x in pls)  # nahrazený playlist si nechá svůj název („dup“ zůstane malými písmeny)
    exp = norm(want["pls"] + ["Moje: "])
    c.ok(norm(r["pls"]) == exp, "Nahrané MP3 se samy zařadí do playlistů ve správném pořadí (%s)" % " | ".join(r["pls"]))
    c.ok(r["folders"] == want["folders"] and r["pending"] == 0, "Soubory se vrátí i do složek a rámeček „chybí“ zmizí")
    p.set_input_files("#myfiles-backup-input", path)
    p.wait_for_timeout(800)
    r2 = p.evaluate(STATE)
    c.ok(norm(r2["pls"]) == exp, "Opakované obnovení ze stejné zálohy nic nezdvojí")
    c.ok(p.evaluate("myFilesLibrary.length") == 4, "Obnovení nemaže ani nepřidává soubory v zařízení")

    # --- špatný a zlomyslný soubor
    bad = os.path.join(tempfile.gettempdir(), "testplayeru_spatna.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write('{"kind":"neco jineho"}')
    p.set_input_files("#myfiles-backup-input", bad)
    p.wait_for_timeout(500)
    c.ok(norm(p.evaluate(STATE)["pls"]) == exp, "Soubor, který není záloha, se odmítne a nic se nezmění")
    x = '<img src=x onerror="window.__pwned=1">'
    with open(bad, "w", encoding="utf-8") as fh:
        json.dump({"kind": "synthlucida-myfiles-backup", "folders": [x, None, 5], "files": [None, {"name": x, "size": 5, "folder": x}],
                   "playlists": [None, {"name": x, "items": [None, {"type": "track", "playlist": "piano", "index": 0, "title": x, "src": "javascript:window.__pwned=2"},
                                                         {"type": "track", "playlist": "piano", "index": 1, "title": "t", "src": "https://evil.example/a.mp3"}]}]}, fh)
    p.set_input_files("#myfiles-backup-input", bad)
    p.wait_for_timeout(800)
    r = p.evaluate("()=>({pw:window.__pwned||0,img:document.querySelectorAll('img[src=\"x\"]').length,src:myPlaylists[myPlaylists.length-1].items.map(i=>i.src||'')})")
    c.ok(not r["pw"] and not r["img"] and not any(r["src"]), "Kód ani cizí odkazy podstrčené v záloze se nespustí a nepoužijí")
    for fp in (path, bad):
        try:
            os.remove(fp)
        except Exception:
            pass
    allerr = errs + errs2
    c.ok(not allerr, "Bez chyby ve stránce" + (": " + allerr[0] if allerr else ""))


def t_queue_import(s, c):
    """My Playlist: export a import souboru .json - vlastní soubory ze sekce F se najdou i na jiném zařízení (podle názvu a velikosti)."""
    def f(name, sec):
        return {"name": name, "mimeType": "audio/wav", "buffer": make_wav(sec, 8000)}
    FILES = [f("Alfa.wav", 3), f("Beta.wav", 4), f("Gama.wav", 5)]
    # alert/confirm se zachytí (confirm = OK = „nahradit“), ať jde zkontrolovat, co appka uživateli řekla
    HOOK = "()=>{window.__al=[];window.alert=m=>{window.__al.push(String(m))};window.confirm=()=>true}"
    QSTATE = "()=>customQueue.map(e=>({t:e.title,f:e.fileId||null}))"
    LIB = "()=>myFilesLibrary.map(x=>({id:x.id,name:x.name}))"
    tmp = tempfile.gettempdir()
    paths = {k: os.path.join(tmp, "testplayeru_queue_%s.json" % k) for k in ("orig", "new", "old", "missing", "bad")}

    def upload(p, files):
        p.set_input_files("#myfiles-input", files)
        p.wait_for_function("!document.getElementById('myfiles-add-btn').disabled", timeout=20000)
        p.wait_for_timeout(400)

    def do_import(p, path):
        p.evaluate("()=>{window.__al=[]}")
        p.set_input_files("#queue-import-input", path)
        p.wait_for_timeout(1200)
        return p.evaluate("()=>window.__al.slice()")

    def own_ids_ok(p):
        """vlastní soubory ve frontě ukazují na soubory, které opravdu jsou v My Files tohoto zařízení, ve stejném pořadí"""
        q, lib = p.evaluate(QSTATE), p.evaluate(LIB)
        byname = {x["name"]: x["id"] for x in lib}
        own = [e for e in q if e["f"]]
        return [e["t"] for e in own] == ["Alfa", "Beta", "Gama"] and all(byname.get(e["t"]) == e["f"] for e in own)

    # --- první zařízení („telefon“): 3 vlastní soubory + 2 skladby SYNTHLUCIDA v My Playlist, export
    ctx, p, errs = s.page()
    if not p.evaluate("typeof importCustomQueueFile==='function'&&!!document.getElementById('queue-import-input')"):
        c.ok(False, "Player nemá import My Playlistu (je to aktuální player.html?)")
        return
    p.evaluate("toggleMainFiles()")
    upload(p, FILES)
    p.evaluate(HOOK)
    p.evaluate("""()=>{window.addToCustomQueue('asmr',0);window.addToCustomQueue('piano',1);
      ['Alfa','Beta','Gama'].forEach(n=>{const f=myFilesLibrary.find(x=>x.name===n);customQueue.push({title:f.name,src:'',playlist:'custom:file:'+f.id,index:0,fileId:f.id})});
      saveCustomQueue();renderCustomQueue();}""")
    p.wait_for_timeout(300)
    with p.expect_download() as dl:
        p.evaluate("exportCustomQueueFile()")
    dl.value.save_as(paths["orig"])
    with open(paths["orig"], "r", encoding="utf-8") as fh:
        data = json.load(fh)
    fe = [t for t in data.get("tracks", []) if t.get("fileId")]
    c.ok(len(data.get("tracks", [])) == 5 and len(fe) == 3 and all(t.get("name") and t.get("size") for t in fe),
         "Export My Playlistu má u vlastních souborů i název a velikost (soubory %d, s názvem a velikostí %d)" % (len(fe), len([t for t in fe if t.get("name") and t.get("size")])))
    c.ok(not any(str(t.get("src") or "").startswith("blob:") for t in data["tracks"]),
         "Export neobsahuje dočasné blob: adresy ani obsah souborů")

    # --- soubor ze stejného zařízení se načte jako dřív
    al = do_import(p, paths["orig"])
    c.ok(p.evaluate("customQueue.length") == 5 and own_ids_ok(p) and not al,
         "Import zálohy ze STEJNÉHO zařízení funguje jako dřív (5 skladeb, bez hlášky)")
    ctx.close()

    # --- zálohy „z jiného zařízení“: vlastní soubory mají cizí fileId (na každém zařízení je jiné)
    def variant(strip=False, extra=False):
        d = json.loads(json.dumps(data))
        for i, t in enumerate(d["tracks"]):
            if t.get("fileId"):
                t["fileId"] = "telefon-%d" % i
                t["playlist"] = "custom:file:" + t["fileId"]
                if strip:  # stará záloha z doby, kdy se název a velikost neukládaly
                    t.pop("name", None)
                    t.pop("size", None)
        if extra:
            d["tracks"].append({"title": "Nikdy", "playlist": "custom:file:telefon-x", "index": 0, "fileId": "telefon-x", "name": "Nikdy", "size": 123})
        return d
    for key, d in (("new", variant()), ("old", variant(strip=True)), ("missing", variant(extra=True))):
        with open(paths[key], "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    with open(paths["bad"], "w", encoding="utf-8") as fh:
        fh.write('{"neco":"jineho"}')

    # --- druhé zařízení („PC“): stejné soubory v sekci F, ale s jinými interními čísly
    ctx, p, errs2 = s.page()
    p.evaluate("toggleMainFiles()")
    upload(p, FILES)
    p.evaluate(HOOK)
    al = do_import(p, paths["new"])
    c.ok(p.evaluate("customQueue.length") == 5 and own_ids_ok(p) and not al,
         "Záloha z jiného zařízení: všech 5 skladeb se načte, soubory se najdou podle názvu a velikosti (%s)" % (al[0][:60] if al else "bez hlášky"))
    al = do_import(p, paths["old"])
    c.ok(p.evaluate("customQueue.length") == 5 and own_ids_ok(p) and not al,
         "Stará záloha bez názvu a velikosti (jen název skladby) se načte taky – přesně případ „8 souborů z F se nenašlo“")
    try:
        p.wait_for_function("customQueue.every(e=>!e.fileId||String(e.src).startsWith('blob:'))", timeout=6000)
        ok_src = True
    except Exception:
        ok_src = False
    c.ok(ok_src, "Načtené vlastní soubory dostanou platnou adresu k přehrání")
    al = do_import(p, paths["missing"])
    c.ok(p.evaluate("customQueue.length") == 5 and own_ids_ok(p) and len(al) == 1 and "1" in al[0],
         "Soubor, který na zařízení opravdu není, se přeskočí s hláškou, ostatní se načtou (%s)" % (al[0][:50] if al else "bez hlášky"))
    ctx.close()

    # --- třetí zařízení: žádné vlastní soubory -> načtou se jen skladby SYNTHLUCIDA a appka řekne, že 3 chybí
    ctx, p, errs3 = s.page()
    p.evaluate(HOOK)
    al = do_import(p, paths["new"])
    q = p.evaluate(QSTATE)
    c.ok(len(q) == 2 and not any(e["f"] for e in q) and len(al) == 1 and "3" in al[0],
         "Bez nahraných souborů se načtou jen skladby SYNTHLUCIDA a hláška řekne, že 3 chybí (%s)" % (al[0][:50] if al else "bez hlášky"))
    al = do_import(p, paths["bad"])
    c.ok(p.evaluate("customQueue.length") == 2 and len(al) == 1, "Soubor, který není playlist, se odmítne a My Playlist se nezmění")
    ctx.close()
    for fp in paths.values():
        try:
            os.remove(fp)
        except Exception:
            pass
    allerr = errs + errs2 + errs3
    c.ok(not allerr, "Bez chyby ve stránce" + (": " + allerr[0] if allerr else ""))


def t_total_time(s, c):
    """Celkový čas vedle názvu: playlist ze sekce F a pojmenovaná sekce v My Playlist."""
    ctx, p, errs = s.page()
    p.evaluate("toggleMainFiles()")
    p.set_input_files("#myfiles-input", [{"name": "Delsi.wav", "mimeType": "audio/wav", "buffer": make_wav(40, 8000)},
                                         {"name": "Kratsi.wav", "mimeType": "audio/wav", "buffer": make_wav(4, 8000)}])
    p.wait_for_function("myFilesLibrary.length===2&&!document.getElementById('myfiles-add-btn').disabled", timeout=20000)
    fmt = lambda sec: "%d:%02d" % (sec // 60, sec % 60)
    exp = p.evaluate("""()=>{const id=n=>myFilesLibrary.find(x=>x.name===n).id;
      myPlaylists.push({id:'tt',name:'TOP',createdAt:1,items:[{type:'track',playlist:'asmr',index:0,title:tracks.asmr[0].title,src:tracks.asmr[0].src},
        {type:'file',fileId:id('Delsi'),title:'Delsi'},{type:'file',fileId:id('Kratsi'),title:'Kratsi'}]});saveMyPlaylistsToStorage();
      return trackDuration(tracks.asmr[0])}""")
    if not exp:
        c.ok(False, "Player nezná délku první ASMR skladby – test nejde vyhodnotit")
        return
    # hned po spuštění klepnout na řazení podle délky: měření celkového času ho nesmí pokazit
    order = p.evaluate("async()=>{await playMyPlaylist('tt');await sortMyPlaylistByLength('tt');return myPlaylists.find(x=>x.id==='tt').items.map(i=>i.title)}")
    c.ok(order[:2] == ["Kratsi", "Delsi"], "Řazení PODLE DÉLKY hned po spuštění playlistu funguje správně (%s)" % ", ".join(order))
    try:
        p.wait_for_function("document.getElementById('pl-name-banner').textContent.indexOf('·')!==-1", timeout=15000)
    except Exception:
        pass
    banner = p.evaluate("document.getElementById('pl-name-banner').textContent")
    want = fmt(exp + 44)
    c.ok(banner.strip().endswith(want) and "TOP" in banner, "Playlist ze sekce F ukazuje celkový čas vedle názvu (%s, má být %s)" % (banner.strip(), want))
    p.evaluate("()=>{const pl=myPlaylists.find(x=>x.id==='tt');pl.items=pl.items.filter(i=>i.title!=='Delsi');saveMyPlaylistsToStorage();syncMyPlaylistToMainWindow('tt')}")
    p.wait_for_timeout(1500)
    banner = p.evaluate("document.getElementById('pl-name-banner').textContent")
    c.ok(banner.strip().endswith(fmt(exp + 4)), "Po odebrání skladby se čas sám přepočítá (%s)" % banner.strip())
    # sekce v My Playlist
    p.evaluate("""async()=>{customQueue.push({title:tracks.piano[1].title,src:tracks.piano[1].src,playlist:'piano',index:1,mood:'Na spaní'});
      customQueue.push({title:tracks.chill[0].title,src:tracks.chill[0].src,playlist:'chill',index:0});
      queueMoods=['Na spaní'];saveQueueMoods();saveCustomQueue();renderCustomQueue();toggleCustomQueue();playCustomQueueAt(0,'Na spaní')}""")
    p.wait_for_timeout(1500)
    r = p.evaluate("()=>({b:document.getElementById('pl-name-banner').textContent,d:trackDuration(tracks.piano[1])})")
    c.ok(r["d"] and r["b"].strip().endswith(fmt(r["d"])) and "Na spaní" in r["b"], "Sekce v My Playlist ukazuje celkový čas (%s)" % r["b"].strip())
    p.evaluate("playCustomQueueAt(1,null)")
    p.wait_for_timeout(1000)
    c.ok(p.evaluate("document.getElementById('pl-name-banner').hidden"), "Celý My Playlist bez sekce nad seznamem nic nového neukazuje")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_history(s, c):
    """Historie poslechu: přehrání z historie (i po novém otevření appky), vlastní soubory, F playlisty, ＋, limit, mazání."""
    ctx, p, errs = s.page()
    if not p.evaluate("typeof playFromHistoryEntry==='function'"):
        c.ok(False, "Player nemá opravené přehrávání z historie (je to aktuální player.html?)")
        return
    STATE = """()=>({playing:!!audio.getAttribute('src')&&!audio.paused,key:String(currentPlaylistKey),
      title:((customQueueActive?customQueue[customQueueIndex]:playlist[currentTrackIndex])||{}).title||'',
      open:document.getElementById('history-panel').classList.contains('visible'),
      toast:document.getElementById('app-toast-text').textContent})"""

    def click(part):
        p.evaluate("()=>{audio.pause();audio.removeAttribute('src');audio.load();openHistoryFromFiles()}")
        p.wait_for_timeout(250)
        found = p.evaluate("(t)=>{const el=[...document.querySelectorAll('#history-list .history-name')].find(e=>e.textContent.indexOf(t)!==-1);if(!el)return false;el.click();return true}", part)
        p.wait_for_timeout(1500)
        st = p.evaluate(STATE)
        st["found"] = found
        return st

    hist_len = lambda: p.evaluate("JSON.parse(localStorage.getItem(HISTORY_KEY)||'[]').length")

    # --- 1) běžná SYNTHLUCIDA skladba
    piano = p.evaluate("()=>{setPlaylist('piano',[...document.querySelectorAll('.btn-atmos')].find(b=>/piano/i.test(b.textContent)));playTrack(2);return tracks.piano[2].title}")
    p.wait_for_timeout(1000)
    p.evaluate("()=>setPlaylist('chill',[...document.querySelectorAll('.btn-atmos')].find(b=>/chill/i.test(b.textContent)))")
    r = click(displayable(p, piano))
    c.ok(r["found"] and r["playing"] and r["key"] == "piano" and r["title"] == piano and not r["open"],
         "Klepnutí na skladbu v historii ji pustí ve správném playlistu a historie se zavře")

    # --- 2) vlastní soubory a F playlist
    p.evaluate("toggleMainFiles()")
    p.set_input_files("#myfiles-input", [{"name": "Hist A.wav", "mimeType": "audio/wav", "buffer": make_wav(30, 8000)},
                                         {"name": "Hist B.wav", "mimeType": "audio/wav", "buffer": make_wav(20, 8000)}])
    p.wait_for_function("myFilesLibrary.length===2&&!document.getElementById('myfiles-add-btn').disabled", timeout=20000)
    nebula = p.evaluate("""async()=>{const id=n=>myFilesLibrary.find(f=>f.name===n).id;
      myPlaylists.push({id:'hpl',name:'HistPL',createdAt:1,items:[{type:'track',playlist:'piano',index:3,title:tracks.piano[3].title,src:tracks.piano[3].src},
        {type:'file',fileId:id('Hist B'),title:'Hist B'},{type:'file',fileId:id('Hist A'),title:'Hist A'}]});saveMyPlaylistsToStorage();
      await playMyFileNow(id('Hist A'));await new Promise(r=>setTimeout(r,700));
      await playMyPlaylist('hpl');await new Promise(r=>setTimeout(r,700));playTrack(1);await new Promise(r=>setTimeout(r,700));
      return tracks.piano[3].title}""")
    p.wait_for_timeout(500)
    r = click("Hist B")
    c.ok(r["playing"] and r["title"] == "Hist B", "Vlastní soubor z F playlistu jde pustit z historie")

    # --- 3) po novém otevření appky (seznamy F existují jen v paměti)
    p.reload()
    p.wait_for_timeout(1800)
    rA, rB, rN = click("Hist A"), click("Hist B"), click(displayable(p, nebula))
    c.ok(rA["playing"] and rA["title"] == "Hist A", "Po novém otevření appky jde z historie pustit samostatný vlastní soubor")
    c.ok(rB["playing"] and rB["title"] == "Hist B" and rB["key"] == "custom:hpl",
         "Po novém otevření appky se skladba z F playlistu pustí v celém playlistu od sebe")
    c.ok(rN["playing"] and rN["title"] == nebula, "Po novém otevření appky jde pustit i SYNTHLUCIDA skladba, která hrála v F playlistu")

    # --- 4) úpravy playlistu a souborů
    p.evaluate("()=>{const pl=myPlaylists.find(x=>x.id==='hpl');const it=pl.items.splice(1,1)[0];pl.items.push(it);saveMyPlaylistsToStorage()}")
    r = click("Hist B")
    c.ok(r["playing"] and r["title"] == "Hist B" and r["key"] == "custom:hpl", "Po přeházení skladeb v playlistu hraje z historie pořád ta správná")
    p.evaluate("()=>{const pl=myPlaylists.find(x=>x.id==='hpl');pl.items=pl.items.filter(i=>i.title!=='Hist A');saveMyPlaylistsToStorage()}")
    r = click("Hist A")
    c.ok(r["playing"] and r["title"] == "Hist A" and r["key"].startswith("custom:file:"), "Soubor odebraný z playlistu se z historie pustí samostatně")
    p.evaluate("()=>{myPlaylists=myPlaylists.filter(x=>x.id!=='hpl');saveMyPlaylistsToStorage()}")
    rN, rB = click(displayable(p, nebula)), click("Hist B")
    c.ok(rN["playing"] and rN["title"] == nebula and rN["key"] == "piano" and rB["playing"] and rB["title"] == "Hist B",
         "Po smazání F playlistu hraje SYNTHLUCIDA skladba z katalogu a soubor samostatně")

    # --- 5) tlačítko ＋ (do My Playlist)
    r = p.evaluate("""async()=>{openHistoryFromFiles();const li=[...document.querySelectorAll('#history-list li')].find(l=>l.textContent.indexOf('Hist B')!==-1);
      const btn=li.querySelector('.icon-btn');const before=customQueue.filter(q=>q.fileId).length;btn.click();await new Promise(r=>setTimeout(r,600));
      const mid=customQueue.filter(q=>q.fileId).length;btn.click();await new Promise(r=>setTimeout(r,400));
      return {dis:btn.disabled,before,mid,after:customQueue.filter(q=>q.fileId).length,toast:document.getElementById('app-toast-text').textContent}}""")
    c.ok(not r["dis"] and r["mid"] == r["before"] + 1 and r["after"] == r["mid"],
         "Tlačítko ＋ v historii přidá vlastní soubor do My Playlist jen jednou (%s)" % r["toast"])

    # --- 6) smazaný soubor
    p.evaluate("async()=>{const f=myFilesLibrary.find(x=>x.name==='Hist A');await deleteMyFile(f.id)}")
    p.wait_for_timeout(600)
    left = p.evaluate("JSON.parse(localStorage.getItem(HISTORY_KEY)||'[]').filter(h=>h.title==='Hist A').length")
    c.ok(left == 0, "Smazaný soubor zmizí i z historie")
    p.evaluate("()=>{const h=JSON.parse(localStorage.getItem(HISTORY_KEY)||'[]');h.unshift({title:'Uz neexistuje',playlist:'custom:file:nic',fileId:'nic',index:0,time:Date.now()});localStorage.setItem(HISTORY_KEY,JSON.stringify(h))}")
    r = click("Uz neexistuje")
    dis = p.evaluate("()=>{openHistoryFromFiles();const li=[...document.querySelectorAll('#history-list li')].find(l=>l.textContent.indexOf('Uz neexistuje')!==-1);return li.querySelector('.icon-btn').disabled}")
    c.ok(not r["playing"] and ("available" in r["toast"] or "dispozici" in r["toast"]) and dis,
         "U skladby, která už neexistuje, se ukáže hláška místo tichého nic a ＋ je zašedlé")

    # --- 7) export, limit a smazání
    with p.expect_download() as dl:
        p.evaluate("exportHistory()")
    c.ok(dl.value.suggested_filename.endswith(".txt"), "Historii jde stáhnout jako textový soubor")
    p.evaluate("()=>{const h=[];for(let i=0;i<130;i++)h.push({title:tracks.chill[i%tracks.chill.length].title,playlist:'chill',index:i%tracks.chill.length,time:Date.now()-i*1000});localStorage.setItem(HISTORY_KEY,JSON.stringify(h))}")
    p.evaluate("()=>{setPlaylist('asmr',[...document.querySelectorAll('.btn-atmos')].find(b=>/asmr/i.test(b.textContent)));playTrack(0)}")
    p.wait_for_timeout(1000)
    c.ok(hist_len() == 100, "Historie si drží nejvýš 100 posledních skladeb (%d)" % hist_len())
    p.evaluate("()=>{openHistoryFromFiles();clearHistory()}")
    p.wait_for_timeout(300)
    c.ok(hist_len() == 0 and p.evaluate("document.querySelectorAll('#history-list li').length") == 0, "VYMAZAT historii ji opravdu smaže")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def displayable(p, title):
    """Jak je název skladby vidět v historii (bez předpony SYNTHLUCIDA)."""
    return p.evaluate("(t)=>displayTrackTitle(t)", title)


def t_background(s, c):
    """Telefon se zhasnutým displejem: časovače stránky stojí - hudba musí i tak pokračovat další skladbou."""
    MOB = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"
    init = ("window.__hid=false;Object.defineProperty(document,'hidden',{get(){return window.__hid}});"
            "Object.defineProperty(document,'visibilityState',{get(){return window.__hid?'hidden':'visible'}});")
    def scenario(cf, hide, freeze, broken=False):
        ctx, p, errs = s.page(init=init, user_agent=MOB)
        if not p.evaluate("typeof crossfadeBailOut==='function'"):
            ctx.close()
            return None, ["Player nemá opravu přehrávání na pozadí (je to aktuální player.html?)"]
        p.evaluate("""(a)=>{setCrossfade(a.cf,null);setPlaylist('piano',[...document.querySelectorAll('.btn-atmos')].find(b=>/piano/i.test(b.textContent)));
          if(a.broken){tracks.piano[1].src='data:audio/mpeg;base64,AAAA';playlist=tracks.piano;}
          audio.defaultPlaybackRate=8;crossfadeAudio.defaultPlaybackRate=8;playTrack(0);audio.playbackRate=8}""", {"cf": cf, "broken": broken})
        p.wait_for_timeout(700)
        if hide:
            p.evaluate("()=>{window.__hid=true;document.dispatchEvent(new Event('visibilitychange'))}")
        if freeze:
            p.evaluate("()=>{window.setTimeout=()=>0;window.setInterval=()=>0}")  # jako uspaný telefon
        try:
            p.wait_for_function("currentTrackIndex>=%d&&!audio.paused&&audio.currentTime>1" % (2 if broken else 1), timeout=25000)
            ok = True
        except Exception:
            ok = False
        st = p.evaluate("()=>({i:currentTrackIndex,hraje:!audio.paused,t:Math.round(audio.currentTime),prolnuti:crossfadeActive})")
        ctx.close()
        return ok, st

    for name, args in [
        ("Prolnutí VYPNUTÉ, zhasnutý displej, uspané časovače: hraje další skladba", dict(cf=0, hide=True, freeze=True)),
        ("Prolnutí ZAPNUTÉ, zhasnutý displej: hraje další skladba", dict(cf=3, hide=True, freeze=False)),
        ("Prolnutí ZAPNUTÉ a zaseknuté (uspané časovače): skladba nezůstane stát", dict(cf=3, hide=False, freeze=True)),
        ("Nedostupná další skladba se zhasnutým displejem: přeskočí se a hraje se dál", dict(cf=0, hide=True, freeze=True, broken=True)),
    ]:
        ok, st = scenario(**args)
        if ok is None:
            c.ok(False, st[0])
            return
        c.ok(ok, name + ("" if ok else " (%s)" % st))

    # prolnutí s rozsvíceným displejem: původní skladba dohraje o chvilku dřív, než prolnutí doběhne
    # -> musí pokračovat TA nová skladba, která už hraje (dřív se usekla a pustila se jiná)
    ctx, p, errs_cf = s.page(init=init, user_agent=MOB)
    p.evaluate("()=>{setCrossfade(6,null);setPlaylist('chill',[...document.querySelectorAll('.btn-atmos')].find(b=>/chill/i.test(b.textContent)));shuffleEnabled=true;playTrack(0)}")
    p.wait_for_timeout(600)
    p.evaluate("()=>{audio.currentTime=Math.max(0,(audio.duration||60)-12);audio.playbackRate=1.6}")
    try:
        p.wait_for_function("crossfadeActive&&!crossfadeAudio.paused", timeout=25000)
        target = p.evaluate("crossfadeAudio.src")
        p.wait_for_function("audio.src===%r&&!crossfadeActive&&!audio.paused" % target, timeout=25000)
        cf_ok = True
    except Exception:
        cf_ok = False
    ctx.close()
    c.ok(cf_ok, "Prolnutí: když původní skladba dohraje dřív než prolnutí, pokračuje stejná nová skladba (bez useknutí a skoku na jinou)")

    # přetočení během prolnutí: prolnutí se zruší a posuvník platí pro skladbu, kterou vidíš (dřív nová skladba hrála dál)
    ctx, p, errs_sk = s.page()
    if p.evaluate("typeof window.userSeekTo==='function'"):
        p.evaluate("()=>{setCrossfade(15,null);setPlaylist('chill',[...document.querySelectorAll('.btn-atmos')].find(b=>/chill/i.test(b.textContent)));shuffleEnabled=false;playTrack(0)}")
        p.wait_for_timeout(1200)
        first = p.evaluate("audio.src")
        p.evaluate("()=>userSeekTo(audio.duration-12)")
        try:
            p.wait_for_function("crossfadeActive&&!crossfadeAudio.paused", timeout=10000)
            started = True
        except Exception:
            started = False
        p.evaluate("()=>userSeekTo(audio.duration*0.4)")
        p.wait_for_timeout(1000)
        r = p.evaluate("()=>({cf:crossfadeActive,cfp:crossfadeAudio.paused,src:audio.src,t:audio.currentTime,d:audio.duration,v:audio.volume})")
        sk_ok = started and (not r["cf"]) and r["cfp"] and r["src"] == first and abs(r["t"] - r["d"] * 0.4) < 3 and r["v"] > 0.05
        c.ok(sk_ok, "Přetočení během prolnutí: prolnutí se zruší a přetočí se skladba, kterou vidíš na posuvníku")
    else:
        c.ok(False, "Player nemá opravu přetáčení během prolnutí (je to aktuální player.html?)")
    ctx.close()

    # nečekaná pauza od systému těsně po rozjetí skladby (záznam z telefonu: "playing" a za 1 s "pause")
    ctx, p, errs0 = s.page(init=init, user_agent=MOB)
    p.evaluate("()=>{setPlaylist('piano',[...document.querySelectorAll('.btn-atmos')].find(b=>/piano/i.test(b.textContent)));playTrack(0)}")
    p.wait_for_timeout(800)
    p.evaluate("()=>{window.__hid=true;document.dispatchEvent(new Event('visibilitychange'));audio.pause()}")  # pauza od systému
    p.wait_for_timeout(1200)
    sys_ok = p.evaluate("()=>!audio.paused&&isPlaying")
    p.evaluate("()=>playBtn.click()")  # pauza od uživatele (tlačítko / zamčená obrazovka)
    p.wait_for_timeout(1200)
    user_ok = p.evaluate("()=>audio.paused&&!isPlaying")
    ctx.close()
    c.ok(sys_ok, "Nečekaná pauza od systému hned po rozjetí skladby (zhasnutý displej) se sama zruší a hraje se dál")
    c.ok(user_ok, "Pauza od uživatele se zhasnutým displejem zůstane pauzou")

    # "ticho na pozadí": běží jen na telefonu, se zhasnutým displejem a jen když hraje hudba
    ctx, p, errs0 = s.page(init=init, user_agent=MOB)
    if p.evaluate("typeof window.__slKeepAlive==='function'"):
        ka = "()=>{const k=window.__slKeepAlive();return !!k&&!k.paused}"
        p.evaluate("()=>{setPlaylist('piano',[...document.querySelectorAll('.btn-atmos')].find(b=>/piano/i.test(b.textContent)));playTrack(0)}")
        p.wait_for_timeout(700)
        a = p.evaluate(ka)
        p.evaluate("()=>{window.__hid=true;document.dispatchEvent(new Event('visibilitychange'))}")
        p.wait_for_timeout(500)
        b = p.evaluate(ka)
        p.evaluate("()=>playBtn.click()")
        p.wait_for_timeout(500)
        cc = p.evaluate(ka)
        p.evaluate("()=>playBtn.click()")
        p.wait_for_timeout(500)
        p.evaluate("()=>{window.__hid=false;document.dispatchEvent(new Event('visibilitychange'))}")
        p.wait_for_timeout(500)
        d = p.evaluate(ka)
        c.ok((not a) and b and (not cc) and (not d),
             "Ticho na pozadí (drží appce právo hrát mezi skladbami) běží jen se zhasnutým displejem a jen když hraje hudba")
    else:
        c.ok(False, "Player nemá ticho na pozadí (je to aktuální player.html?)")
    ctx.close()

    # vlastní soubor s obrázkem alba (ID3 štítek s obalem): Android ho bere jako video a se zhasnutým displejem
    # ho pozastaví -> pro přehrávání se obal vynechá, uložený soubor zůstane beze změny
    def id3_with_cover(audio):
        jpg = b"\xff\xd8\xff\xe0" + b"\0" * 3000 + b"\xff\xd9"
        frame_body = b"\0image/jpeg\0\x03cover\0" + jpg
        frame = b"APIC" + len(frame_body).to_bytes(4, "big") + b"\0\0" + frame_body
        n = len(frame)
        size = bytes([(n >> 21) & 0x7f, (n >> 14) & 0x7f, (n >> 7) & 0x7f, n & 0x7f])
        return b"ID3\x03\0\0" + size + frame + audio
    ctx, p, errs1 = s.page()
    p.evaluate("toggleMainFiles()")
    p.set_input_files("#myfiles-input", [{"name": "S obalem.mp3", "mimeType": "audio/mpeg", "buffer": id3_with_cover(make_wav(5, 8000))}])
    p.wait_for_function("myFilesLibrary.length===1&&!document.getElementById('myfiles-add-btn').disabled", timeout=20000)
    r = p.evaluate("""async()=>{const f=myFilesLibrary[0];const url=await getMyFileBlobUrl(f.id);
      const b=new Uint8Array(await (await fetch(url)).arrayBuffer());const rec=await myFilesDbGet(f.id);
      return {start:String.fromCharCode(b[0],b[1],b[2],b[3]),stored:rec.blob.size,size:f.size}}""")
    ctx.close()
    c.ok(r["start"] == "RIFF" and r["stored"] == r["size"],
         "Obrázek alba ve vlastním MP3 se při přehrávání vynechá (Android by soubor se zhasnutým displejem zastavil), uložený soubor zůstane celý")

    ctx, p, errs = s.page()
    p.evaluate("()=>{slPlayLog('test zaznamu');location.hash='#playlog'}")
    p.wait_for_timeout(500)
    shown = p.evaluate("()=>{const v=document.getElementById('sl-playlog-view');return v?v.textContent:''}")
    c.ok("test zaznamu" in shown, "Záznam přehrávání jde zobrazit přes player.html#playlog")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_last_source(s, c):
    """Po zavření a novém otevření appky se připraví naposledy přehrávaný seznam a skladba (i F a My Playlist)."""
    ctx, p, errs = s.page()
    if not p.evaluate("typeof saveLastTrackPosition==='function'"):
        c.ok(False, "Player nemá zapamatování posledního seznamu (je to aktuální player.html?)")
        return
    ST = """()=>({key:String(currentPlaylistKey),q:customQueueActive,mood:customQueueMoodPlay,
      title:((customQueueActive?customQueue[customQueueIndex]:playlist[currentTrackIndex])||{}).title||'',
      active:[...document.querySelectorAll('.btn-atmos.active')].map(b=>b.textContent.trim()).join(','),playing:!audio.paused})"""

    def reopen():
        p.evaluate("()=>{audio.pause();saveLastTrackPosition()}")
        p.reload()
        p.wait_for_timeout(2500)
        return p.evaluate(ST)

    p.evaluate("toggleMainFiles()")
    p.set_input_files("#myfiles-input", [{"name": "Last A.wav", "mimeType": "audio/wav", "buffer": make_wav(20, 8000)},
                                         {"name": "Last B.wav", "mimeType": "audio/wav", "buffer": make_wav(20, 8000)}])
    p.wait_for_function("myFilesLibrary.length===2&&!document.getElementById('myfiles-add-btn').disabled", timeout=20000)

    # F playlist
    p.evaluate("""async()=>{const id=n=>myFilesLibrary.find(f=>f.name===n).id;
      myPlaylists.push({id:'lp',name:'LastPL',createdAt:1,items:[{type:'track',playlist:'piano',index:2,title:tracks.piano[2].title,src:tracks.piano[2].src},
        {type:'file',fileId:id('Last A'),title:'Last A'},{type:'file',fileId:id('Last B'),title:'Last B'}]});saveMyPlaylistsToStorage();
      await playMyPlaylist('lp');await new Promise(r=>setTimeout(r,500));playTrack(2);await new Promise(r=>setTimeout(r,800))}""")
    r = reopen()
    c.ok(r["key"] == "custom:lp" and r["title"] == "Last B" and r["active"] == "" and not r["playing"],
         "F playlist: po novém otevření je připravená stejná skladba (bez autoplaye a bez svítícího ASMR)")

    # My Playlist se sekcí
    p.evaluate("""async()=>{customQueue.length=0;const f=myFilesLibrary.find(x=>x.name==='Last A');
      customQueue.push({title:tracks.chill[1].title,src:tracks.chill[1].src,playlist:'chill',index:1,mood:'Noc'});
      customQueue.push({title:'Last A',src:await getMyFileBlobUrl(f.id),playlist:'custom:file:'+f.id,index:0,fileId:f.id,mood:'Noc'});
      customQueue.push({title:tracks.piano[3].title,src:tracks.piano[3].src,playlist:'piano',index:3});
      queueMoods=['Noc'];saveQueueMoods();saveCustomQueue();renderCustomQueue();playCustomQueueAt(1,'Noc');await new Promise(r=>setTimeout(r,1000))}""")
    r = reopen()
    c.ok(r["q"] and r["title"] == "Last A" and r["mood"] == "Noc" and not r["playing"],
         "My Playlist: po novém otevření je připravená stejná skladba i vybraná sekce (%s, %s)" % (r["title"], r["mood"]))
    p.evaluate("()=>playBtn.click()")
    p.wait_for_timeout(1200)
    r = p.evaluate(ST)
    c.ok(r["q"] and r["playing"] and r["title"] == "Last A", "Po stisku ▶ hraje My Playlist dál od připravené skladby")

    # samostatný vlastní soubor
    p.evaluate("async()=>{const f=myFilesLibrary.find(x=>x.name==='Last B');await playMyFileNow(f.id);await new Promise(r=>setTimeout(r,800))}")
    r = reopen()
    c.ok(r["key"].startswith("custom:file:") and r["title"] == "Last B" and not r["q"], "Samostatně puštěný vlastní soubor se po novém otevření připraví znovu")

    # běžný playlist dál funguje
    p.evaluate("()=>{setPlaylist('chill',[...document.querySelectorAll('.btn-atmos')].find(b=>/chill/i.test(b.textContent)));playTrack(4)}")
    p.wait_for_timeout(800)
    want = p.evaluate("tracks.chill[4].title")
    r = reopen()
    c.ok(r["key"] == "chill" and r["title"] == want and r["active"] == "CHILL", "CHILL / PIANO / ASMR se po novém otevření připraví jako dřív")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


# --------------------------------------------------------------------------- #
#  11/2026: přání, motivační texty, fronta přehrávání, ASMR vrstva, GAIN, prolnutí, záloha F
# --------------------------------------------------------------------------- #
BTN_JS = "(k)=>[...document.querySelectorAll('.btn-atmos')].find(b=>b.getAttribute('onclick')&&b.getAttribute('onclick').indexOf(\"'\"+k+\"'\")!==-1)"


def fake_date_js(iso):
    """Init skript: hodiny stránky ukazují zadané místní datum a čas (a dál běží)."""
    return ("(()=>{const T=new Date(%r).getTime();const D0=Date;const off=T-D0.now();"
            "class FD extends D0{constructor(...a){if(a.length===0)super(D0.now()+off);else super(...a);}static now(){return D0.now()+off;}}"
            "window.Date=FD;})();" % iso)


def slogan_state(p):
    return p.evaluate("()=>({vis:document.getElementById('slogan-container').classList.contains('visible'),"
                      "txt:document.getElementById('slogan-container').innerText})")


def greet_texts(p, kind, year=None):
    """Texty přání z player.html (jde je změnit v Editoru playeru): kind = friday | advent | eve | newyear."""
    js = """(a)=>{try{if(a.k==='friday')return [FRIDAY_GREET_TEXT.en,FRIDAY_GREET_TEXT.cz];
      const h=HOLIDAY_TEXTS[a.k];return [].concat(h.en||[],h.cz||[])}catch(e){return []}}"""
    out = [t for t in (p.evaluate(js, {"k": kind}) or []) if isinstance(t, str) and t.strip()]
    if year is not None:
        out = [t.replace("{Y}", str(year)) for t in out]
    return out


def greet_ok(shown, texts, fallback):
    """Ukázané přání je jedno z textů v player.html (když je nejde přečíst, platí původní kontrola)."""
    norm = lambda t: " ".join(str(t).split()).lower()
    if not texts:
        return bool(re.search(fallback, shown, re.I))
    return any(norm(t) and (norm(t) in norm(shown) or norm(shown) in norm(t)) for t in texts) and norm(shown) != ""


def t_greetings(s, c):
    """Páteční / nedělní přání se ukazují i s vypnutými motivačními texty; Vánoce a Nový rok mají vlastní přání."""
    def look(iso, loads=1, wait=900):
        ctx, p, errs = s.page(init=fake_date_js(iso))
        p.wait_for_timeout(wait)
        shown = []
        r = slogan_state(p)
        if r["vis"]:
            shown.append(r["txt"])
        motiv = p.evaluate("motivationTextsEnabled")
        for _ in range(loads - 1):
            p.reload()
            p.wait_for_timeout(1500 + wait)
            r = slogan_state(p)
            if r["vis"]:
                shown.append(r["txt"])
        return ctx, p, errs, shown, motiv

    ctx, p, errs, shown, motiv = look("2026-11-27T12:00:00")  # obyčejný pátek
    if not p.evaluate("typeof holidayGreetingToday==='function'"):
        c.ok(False, "Player nemá sváteční přání (je to aktuální player.html?)")
        return
    c.ok(not motiv and len(shown) == 1 and greet_ok(shown[0], greet_texts(p, "friday"), "weekend|víkend"),
         "Páteční přání se ukáže i s vypnutými motivačními texty (MOTIVATION: OFF)")
    p.evaluate("toggleMotivationTexts()")  # zapnout a hned vypnout během přání
    p.evaluate("toggleMotivationTexts()")
    p.wait_for_timeout(200)
    c.ok(slogan_state(p)["vis"], "Vypnutí MOTIVATION během přání přání neschová")
    ctx.close()
    ctx, p, errs2, shown, _ = look("2026-11-22T18:00:00")
    c.ok(len(shown) == 1, "Nedělní přání (večer) se ukáže i s vypnutými motivačními texty")
    ctx.close()
    ctx, p, errs3, shown, _ = look("2026-11-22T10:00:00")
    c.ok(not shown, "Neděle dopoledne: žádné přání")
    ctx.close()
    ctx, p, errs4, shown, _ = look("2026-12-24T17:00:00", loads=5)
    tx = greet_texts(p, "eve")
    c.ok(len(shown) == 3 and all(greet_ok(t, tx, "Christmas|Vánoc|Štědr") for t in shown),
         "24. 12.: vánoční přání ke štědrovečerní večeři, nejvýš 3x za den (%dx)" % len(shown))
    ctx.close()
    ctx, p, errs5, shown, _ = look("2026-12-18T12:00:00", loads=3)
    c.ok(len(shown) == 1 and greet_ok(shown[0], greet_texts(p, "advent"), "Christmas|Vánoc"),
         "Týden před Vánoci (i v pátek): předvánoční přání jednou za den, páteční se neukáže")
    ctx.close()
    ctx, p, errs6, shown, _ = look("2027-01-01T12:00:00", loads=4)
    tx = greet_texts(p, "newyear", 2027)
    c.ok(len(shown) == 3 and all(greet_ok(t, tx, "2027") for t in shown), "1. 1.: novoroční přání (s letopočtem), nejvýš 3x")
    ctx.close()
    ctx, p, errs7, shown, _ = look("2027-01-04T12:00:00")
    c.ok(not shown, "4. 1.: žádné přání")
    ctx.close()
    allerr = errs + errs2 + errs3 + errs4 + errs5 + errs6 + errs7
    c.ok(not allerr, "Bez chyby ve stránce" + (": " + allerr[0] if allerr else ""))


def t_motiv_editor(s, c):
    """EXPLORE > SETUP > MOTIVAČNÍ TEXTY: vestavěné texty jdou vypnout (ne smazat), vlastní přidat a smazat."""
    ctx, p, errs = s.page()
    if not p.evaluate("typeof toggleMotivationEditor==='function'&&!!document.querySelector('#setup-window #motiv-editor')"):
        c.ok(False, "Player nemá úpravu motivačních textů v SETUP (je to aktuální player.html?)")
        return
    p.evaluate("toggleSetup()")
    p.wait_for_timeout(300)
    p.click("#motivation-edit-btn")
    p.wait_for_timeout(200)
    n_builtin = p.evaluate("document.querySelectorAll('#motiv-builtin-list .motiv-item').length")
    c.ok(n_builtin >= 20 and not p.evaluate("document.getElementById('motiv-editor').hidden"),
         "V SETUP se otevře úprava textů se všemi vestavěnými texty (%d)" % n_builtin)
    c.ok(p.evaluate("document.querySelectorAll('#motiv-builtin-list .motiv-del').length") == 0, "Vestavěné texty nejdou smazat (jen ZAP / VYP)")
    p.fill("#motiv-add-input", "Můj klidný text <img src=x onerror=alert(1)>")
    p.press("#motiv-add-input", "Enter")
    p.fill("#motiv-add-input", "můj klidný TEXT <img src=x onerror=alert(1)>")
    p.click("#motiv-add-btn")
    p.wait_for_timeout(200)
    r = p.evaluate("()=>({n:document.querySelectorAll('#motiv-custom-list .motiv-item').length,img:document.querySelectorAll('#motiv-editor img').length,"
                   "inList:currentSlogans().some(t=>t.indexOf('Můj klidný text')===0)})")
    c.ok(r["n"] == 1 and r["inList"], "Vlastní text se přidá (Enter i + PŘIDAT) a stejný podruhé ne")
    c.ok(r["img"] == 0, "Vložené HTML se zobrazí jen jako text (nic se nespustí)")
    first = p.evaluate("slogans[0]")
    p.evaluate("()=>document.querySelector('#motiv-builtin-list .motiv-item button').click()")
    p.wait_for_timeout(150)
    c.ok(p.evaluate("(f)=>currentSlogans().indexOf(f)===-1", first), "Vypnutý vestavěný text se přestane používat")
    p.reload()
    p.wait_for_timeout(1500)
    r = p.evaluate("(f)=>({off:currentSlogans().indexOf(f)===-1,own:currentSlogans().some(t=>t.indexOf('Můj klidný text')===0)})", first)
    c.ok(r["off"] and r["own"], "Úpravy se pamatují i po novém spuštění")
    p.evaluate("()=>{localStorage.setItem('sl-motivation-texts',JSON.stringify({off:slogans.map((_,i)=>i),custom:['Jediný']}));}")
    p.evaluate("toggleSetup()")
    p.wait_for_timeout(300)
    p.click("#motivation-edit-btn")
    p.wait_for_timeout(150)
    p.evaluate("()=>document.querySelector('#motiv-custom-list .motiv-del').click()")
    p.wait_for_timeout(150)
    c.ok(p.evaluate("currentSlogans().length===1&&currentSlogans()[0]==='Jediný'"), "Poslední zapnutý text nejde vypnout ani smazat (vždy zůstane aspoň jeden)")
    p.evaluate("enableAllMotivationTexts()")
    p.wait_for_timeout(100)
    c.ok(p.evaluate("currentSlogans().length===slogans.length+1"), "ZAPNOUT VŠE vrátí všechny vestavěné texty")
    p.evaluate("()=>{localStorage.setItem('sl-motivation-texts','{\"off\":[null,\"x\",9999],\"custom\":[5,null,\"  ok  \"]}')}")
    c.ok(p.evaluate("currentSlogans().length===slogans.length+1&&currentSlogans().indexOf('ok')!==-1"), "Poškozený záznam: nesmysly se ignorují")
    p.evaluate("toggleGuideLanguage()")
    p.wait_for_timeout(200)
    c.ok(re.search("MOTIVAČNÍ|MOTIVATIONAL", p.evaluate("document.getElementById('su-motiv-sec').textContent")), "Texty sekce se přeloží s jazykem")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def wait_next(p, timeout=30000):
    """Počká, až začne hrát jiná skladba než teď (bez rozběhnutého prolnutí)."""
    first = p.evaluate("audio.src")
    p.wait_for_function("(f)=>audio.src!==f&&!crossfadeActive&&!audio.paused&&audio.currentTime>0.2", arg=first, timeout=timeout)


def near_end(p, left=2.5, rate=2):
    p.evaluate("(a)=>{audio.currentTime=Math.max(0,(audio.duration||60)-a.left);audio.playbackRate=a.rate}", {"left": left, "rate": rate})


def t_asmr_view_next(s, c):
    """ASMR otevřené jen kvůli druhé vrstvě: další skladba pokračuje v seznamu, ze kterého hraje (ne z ASMR)."""
    for cf in (0, 3):
        ctx, p, errs = s.page()
        if not p.evaluate("typeof asmrViewForeignSource==='function'"):
            c.ok(False, "Player nemá opravu dalšího tracku při prohlížení ASMR (je to aktuální player.html?)")
            return
        p.evaluate("(cf)=>{const B=%s;setCrossfade(cf,null);shuffleEnabled=false;repeatEnabled=false;setPlaylist('piano',B('piano'));playTrack(1)}" % BTN_JS, cf)
        p.wait_for_timeout(1200)
        p.evaluate("()=>{const B=%s;setPlaylist('asmr',B('asmr'));pickLayerTrack(3,true)}" % BTN_JS)
        p.wait_for_timeout(500)
        near_end(p, left=(cf + 2.5), rate=2)
        try:
            wait_next(p)
            ok = True
        except Exception:
            ok = False
        r = p.evaluate("()=>({src:audio.src.split('/').pop(),want:tracks.piano[2].src.split('/').pop(),view:currentPlaylistKey,layer:layerActive&&!layerAudio.paused,ref:playingRef&&playingRef.key})")
        c.ok(ok and r["src"] == r["want"] and r["view"] == "asmr" and r["layer"] and r["ref"] == "piano",
             "Prolnutí %s: hraje další skladba z PIANO, okno zůstane na ASMR a druhá vrstva hraje dál%s" % ("ZAP" if cf else "VYP", "" if ok else " (%s)" % r))
        c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))
        ctx.close()


PQ_NUMS = "()=>[...document.querySelectorAll('#playlist li')].map((li,i)=>{const n=li.querySelector('.pq-num');return n?i+':'+n.textContent:null}).filter(Boolean).join(' ')"


def t_play_queue(s, c):
    """Fronta přehrávání (hrát jako další): režim fronty, čísla, pořadí, návrat do playlistu, okno, paměť, hledání."""
    ctx, p, errs = s.page()
    if not p.evaluate("typeof togglePlayQueueMode==='function'&&!!document.getElementById('pl-pq-btn')&&!!document.getElementById('pq-window')"):
        c.ok(False, "Player nemá frontu přehrávání (je to aktuální player.html?)")
        return
    c.ok(p.evaluate("()=>{const a=document.getElementById('pl-sort-btn').getBoundingClientRect(),b=document.getElementById('pl-pq-btn').getBoundingClientRect(),d=document.getElementById('pl-pqlist-btn').getBoundingClientRect();return b.top-a.bottom>=8&&d.top>b.bottom}"),
         "Tlačítka fronty jsou vlevo pod řazením, s mezerou")
    p.evaluate("()=>{const B=%s;setCrossfade(0,null);shuffleEnabled=false;repeatEnabled=false;setPlaylist('piano',B('piano'));playTrack(1)}" % BTN_JS)
    p.wait_for_timeout(1000)
    p.click("#pl-pq-btn")
    p.evaluate("()=>{const B=%s;setPlaylist('chill',B('chill'))}" % BTN_JS)
    p.wait_for_timeout(200)
    p.evaluate("()=>{const li=document.querySelectorAll('#playlist li');li[4].click();li[2].click();li[6].click();li[6].click()}")  # 6 přidat a zase vyndat
    p.wait_for_timeout(300)
    r = p.evaluate("()=>({nums:(%s)(),piano:audio.src.indexOf(tracks.piano[1].src.split('/').pop())!==-1,cnt:document.getElementById('pq-count').textContent})" % PQ_NUMS)
    c.ok(r["nums"] == "2:2 4:1" and r["piano"] and r["cnt"] == "2",
         "Režim fronty: klepnutí skladbu přidá (zvýraznění s pořadím), znovu klepnutí vyndá, nic se nespustí (%s)" % r["nums"])
    p.click("#pl-pq-btn")
    p.wait_for_timeout(200)
    c.ok(p.evaluate(PQ_NUMS) == "" and p.evaluate("document.getElementById('pq-count').textContent") == "2",
         "Vypnutí režimu fronty schová čísla u skladeb (fronta zůstane, počet je u tlačítka)")
    p.click("#pl-pq-btn")
    p.wait_for_timeout(200)
    c.ok(p.evaluate(PQ_NUMS) == "2:2 4:1", "Zapnutí režimu čísla zase ukáže")
    p.click("#pl-pq-btn")
    p.evaluate("()=>{const B=%s;setPlaylist('piano',B('piano'))}" % BTN_JS)
    seen = []
    for _ in range(3):
        near_end(p)
        wait_next(p)
        seen.append(p.evaluate("()=>playingRef.key+':'+playingRef.index"))
    c.ok(seen == ["chill:4", "chill:2", "piano:2"],
         "Po skončení skladby hraje fronta v pořadí přidání, pak se vrátí do PIANO za původní skladbu (%s)" % ", ".join(seen))
    c.ok(p.evaluate("pqItems.length===0&&document.getElementById('pq-count').hidden"), "Odehraná fronta je prázdná")
    # fronta z My Playlist + prolnutí: návrat do My Playlist za původní pozici
    p.evaluate("""()=>{customQueue.length=0;['chill','piano'].forEach(k=>[0,1,2].forEach(i=>customQueue.push({title:tracks[k][i].title,src:tracks[k][i].src,playlist:k,index:i})));
      saveCustomQueue();setCrossfade(3,null);playCustomQueueAt(1,null)}""")
    p.wait_for_timeout(900)
    p.evaluate("()=>{togglePlayQueueMode();document.querySelectorAll('#playlist li')[4].click();togglePlayQueueMode()}")
    near_end(p, left=5.5)
    wait_next(p)
    a = p.evaluate("()=>({q:customQueueActive,ref:playingRef.key+':'+playingRef.index})")
    near_end(p, left=5.5)
    wait_next(p)
    b = p.evaluate("()=>({q:customQueueActive,pos:customQueueIndex})")
    c.ok(not a["q"] and a["ref"] == "piano:1" and b["q"] and b["pos"] == 2,
         "My Playlist + prolnutí: zahraje skladba z fronty, pak My Playlist pokračuje za původní pozicí (%s / %s)" % (a, b))
    # okno fronty, paměť, VYMAZAT
    p.evaluate("()=>{setCrossfade(0,null);const B=%s;setPlaylist('chill',B('chill'));togglePlayQueueMode();[5,6,7].forEach(i=>document.querySelectorAll('#playlist li')[i].click());togglePlayQueueMode()}" % BTN_JS)
    p.click("#pl-pqlist-btn")
    p.wait_for_timeout(300)
    rows = "()=>[...document.querySelectorAll('#pq-list .pq-item-title')].map(e=>e.textContent)"
    a = p.evaluate(rows)
    p.evaluate("()=>document.querySelectorAll('#pq-list .pq-item')[2].querySelectorAll('button')[0].click()")  # ▲ u třetí
    b = p.evaluate(rows)
    p.evaluate("()=>{const it=document.querySelectorAll('#pq-list .pq-item')[0];it.querySelector('.pq-del').click()}")
    cc = p.evaluate(rows)
    c.ok(not p.evaluate("document.getElementById('pq-window').hidden") and len(a) == 3 and b == [a[0], a[2], a[1]] and cc == [a[2], a[1]],
         "Okno fronty: posun ▲▼ a odebrání ✖ fungují")
    p.reload()
    p.wait_for_timeout(1500)
    c.ok(p.evaluate("pqItems.length===2&&document.getElementById('pq-count').textContent==='2'"), "Fronta se pamatuje i po novém spuštění")
    # úplně nové spuštění bez připravené poslední skladby (jen fronta) -> ▶ začne frontou
    saved = p.evaluate("localStorage.getItem('sl-play-queue')")
    ctx2, p2, errs_q = s.page(init="try{localStorage.setItem('sl-play-queue',%s)}catch(e){}" % json.dumps(saved))
    p2.click("#btn-play")
    p2.wait_for_timeout(1500)
    c.ok(p2.evaluate("()=>!audio.paused&&audio.src.indexOf(tracks.chill[7].src.split('/').pop())!==-1"), "Když nic nehraje, ▶ začne první skladbou z fronty")
    c.ok(not errs_q, "Bez chyby ve stránce (nové spuštění)" + (": " + errs_q[0] if errs_q else ""))
    ctx2.close()
    p.evaluate("openPlayQueueWindow()")
    p.click("#pq-clear-btn")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("pqItems.length===0&&!!document.querySelector('#pq-list .pq-empty')&&localStorage.getItem('sl-play-queue')===null"), "VYMAZAT FRONTU frontu smaže (reset)")
    p.evaluate("closePlayQueueWindow()")
    # hledání v playlistu: ＋ přidá do fronty, ▶ přehraje (i v režimu fronty)
    p.evaluate("()=>{const B=%s;setPlaylist('piano',B('piano'))}" % BTN_JS)
    p.wait_for_timeout(200)
    p.click("#pl-find-btn")
    p.fill("#plfind-input", "piano")
    p.wait_for_timeout(200)
    p.evaluate("()=>document.querySelectorAll('#plfind-results li')[1].querySelector('.search-queue').click()")
    p.wait_for_timeout(200)
    r = p.evaluate("()=>({open:document.getElementById('plfind-panel').classList.contains('visible'),q:pqItems.length,btn:document.querySelectorAll('#plfind-results li')[1].querySelector('.search-queue').textContent,"
                   "name:document.querySelectorAll('#plfind-results li')[1].querySelector('.history-name').textContent,qt:(pqItems[0]||{}).title||''})")
    c.ok(r["open"] and r["q"] == 1 and r["btn"] == "1" and r["qt"].endswith(r["name"]),
         "Hledání v playlistu: ＋ přidá výsledek do fronty, okno zůstane otevřené a ukáže pořadí")
    p.evaluate("togglePlayQueueMode()")
    p.evaluate("()=>document.querySelectorAll('#plfind-results li')[2].querySelector('.search-play').click()")
    p.wait_for_timeout(800)
    c.ok(p.evaluate("()=>pqItems.length===1&&!audio.paused&&!document.getElementById('plfind-panel').classList.contains('visible')"),
         "▶ ve výsledku hledání skladbu přehraje i se zapnutým režimem fronty")
    p.evaluate("togglePlayQueueMode()")
    p.evaluate("toggleGuideLanguage()")
    p.evaluate("openPlayQueueWindow()")
    p.wait_for_timeout(200)
    c.ok(re.search("FRONTA|QUEUE", p.evaluate("document.getElementById('pq-title').textContent")) and
         p.evaluate("!!document.querySelector('[data-i18n=\"h_playqueue\"]')"), "Okno fronty je přeložené a fronta má sekci v návodu")
    c.ok(not p.evaluate(OVERFLOW_JS), "Okno fronty se vejde na telefon")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_queue_layer(s, c):
    """ASMR skladba ve frontě jde použít jako druhá vrstva (ASMR MIX) a vrstva hraje dál i v jiném seznamu."""
    ctx, p, errs = s.page()
    if not p.evaluate("typeof pqHasAsmr==='function'"):
        c.ok(False, "Player nemá druhou vrstvu z fronty (je to aktuální player.html?)")
        return
    p.evaluate("()=>{const B=%s;setPlaylist('piano',B('piano'));playTrack(0)}" % BTN_JS)
    p.wait_for_timeout(1000)
    c.ok(p.evaluate("document.getElementById('btn-layer').disabled"), "V PIANO bez ASMR ve frontě je ASMR MIX neaktivní")
    p.evaluate("()=>{const B=%s;setPlaylist('asmr',B('asmr'));togglePlayQueueMode();document.querySelectorAll('#playlist li')[2].click();togglePlayQueueMode();setPlaylist('piano',B('piano'))}" % BTN_JS)
    p.wait_for_timeout(300)
    c.ok(not p.evaluate("document.getElementById('btn-layer').disabled"), "S ASMR skladbou ve frontě je ASMR MIX aktivní i v PIANO")
    p.click("#btn-layer")
    p.wait_for_timeout(300)
    c.ok(not p.evaluate("document.getElementById('pq-window').hidden") and p.evaluate("document.querySelectorAll('#pq-list .pq-layer').length") == 1,
         "ASMR MIX otevře okno fronty, u ASMR skladby je tlačítko 🎧")
    p.evaluate("()=>document.querySelector('#pq-list .pq-layer').click()")
    p.wait_for_timeout(800)
    p.evaluate("()=>{closePlayQueueWindow();const B=%s;setPlaylist('chill',B('chill'))}" % BTN_JS)
    p.wait_for_timeout(1300)
    r = p.evaluate("()=>({on:layerActive,play:!layerAudio.paused,src:layerSrc.split('/').pop(),want:tracks.asmr[2].src.split('/').pop(),q:pqItems.length,knob:!document.getElementById('layer-group').classList.contains('knob-off')})")
    c.ok(r["on"] and r["play"] and r["src"] == r["want"] and r["q"] == 0 and r["knob"],
         "🎧 pustí skladbu jako druhou vrstvu (z fronty se odebere) a ta hraje dál i v jiném seznamu")
    p.click("#btn-layer")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("!layerActive&&layerAudio.paused"), "ASMR MIX vrstvu zase vypne")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_gain_crossfade(s, c):
    """GAIN 0.25x-4x (nikdy přes 100 %), prolnutí během ztišování časovačem, nenačtená další skladba při prolnutí."""
    ctx, p, errs = s.page()
    r = p.evaluate("()=>{const o=mainGainPos;const g=[0,0.5,1].map(x=>{mainGainPos=x;return +mainGainMul().toFixed(3)});volumeSlider.value=0.6;mainGainPos=1;const v=mainVol();mainGainPos=o;return {g,v}}")
    c.ok(r["g"] == [0.25, 1, 4] and r["v"] == 1, "GAIN zesiluje/zeslabuje 0.25x - 4x a hlasitost nikdy nepřekročí 100 %% (%s)" % r["g"])
    # časovač ztišuje i během prolnutí (dřív se nová skladba rozjela nahlas)
    p.evaluate("()=>{const B=%s;volumeSlider.value=0.6;mainGainPos=0.5;setCrossfade(6,null);shuffleEnabled=false;setPlaylist('piano',B('piano'));playTrack(0)}" % BTN_JS)
    p.wait_for_timeout(1000)
    p.evaluate("()=>{audio.currentTime=audio.duration-7;setTimer(15);timerEndsAt=Date.now()+50000}")  # ztišování běží, ještě dost nahlas
    samples = p.evaluate("""async()=>{const out=[];for(let i=0;i<36;i++){await new Promise(r=>setTimeout(r,250));
      if(crossfadeActive)out.push(Math.max(audio.volume,crossfadeAudio.volume)-(0.6*sleepFadeMul+0.03));}return out}""")
    c.ok(samples and max(samples[2:] or samples) <= 0.001, "Prolnutí během ztišování časovačem: hlasitost dál klesá (nepoposkočí)")
    p.evaluate("cancelTimer()")
    ctx.close()
    # nenačtená další skladba během prolnutí: současná dohraje nahlas (dřív se ztlumila do ticha)
    ctx, p, errs2 = s.page()
    p.evaluate("()=>{const B=%s;volumeSlider.value=0.6;setCrossfade(6,null);shuffleEnabled=false;setPlaylist('piano',B('piano'));tracks.piano[1].src='http://127.0.0.1:1/neni.mp3';playTrack(0)}" % BTN_JS)
    p.wait_for_timeout(1000)
    p.evaluate("()=>{audio.currentTime=audio.duration-8}")
    vols = p.evaluate("""async()=>{const out=[];const s0=audio.src;for(let i=0;i<24;i++){await new Promise(r=>setTimeout(r,250));if(audio.src===s0&&audio.currentTime>audio.duration-4)out.push(audio.volume);}return out}""")
    c.ok(vols and min(vols) > 0.4, "Další skladba se při prolnutí nenačte: současná dohraje nahlas, bez ticha")
    try:
        p.wait_for_function("()=>!audio.paused&&audio.src.indexOf(tracks.piano[2].src.split('/').pop())!==-1", timeout=20000)
        skip = True
    except Exception:
        skip = False
    c.ok(skip, "Pak se nedostupná skladba přeskočí a hraje další")
    allerr = errs + errs2
    c.ok(not allerr, "Bez chyby ve stránce" + (": " + allerr[0] if allerr else ""))


def t_backup_spaces(s, c):
    """Záloha F: soubor se dvěma mezerami v názvu se po obnovení spáruje (dřív zůstal navždy „chybějící“)."""
    ctx, p, errs = s.page()
    p.evaluate("toggleMainFiles()")
    p.set_input_files("#myfiles-input", [{"name": "Rain  Night.wav", "mimeType": "audio/wav", "buffer": make_wav(2, 8000)}])
    p.wait_for_function("!document.getElementById('myfiles-add-btn').disabled", timeout=20000)
    p.wait_for_timeout(400)
    p.evaluate("""()=>{const f=myFilesLibrary.find(x=>x.name==='Rain  Night');document.getElementById('myfiles-folder-name').value='Spaní';createMyFolder();moveFilesToFolder([f.id],myFolders[0].id);
      myPlaylists.push({id:'sp1',name:'Noc',createdAt:1,items:[{type:'file',fileId:f.id,title:f.name}]});saveMyPlaylistsToStorage();renderMyFiles()}""")
    with p.expect_download() as dl:
        p.evaluate("exportMyFilesBackup()")
    path = os.path.join(tempfile.gettempdir(), "testplayeru_mezery.json")
    dl.value.save_as(path)
    p.evaluate("()=>{myPlaylists.length=0;saveMyPlaylistsToStorage();myFolders.length=0;for(const k in myFolderMap)delete myFolderMap[k];saveMyFolders();renderMyFiles()}")
    p.set_input_files("#myfiles-backup-input", path)
    p.wait_for_timeout(1000)
    r = p.evaluate("()=>({items:(myPlaylists.find(x=>x.name==='Noc')||{items:[]}).items.length,folder:(myFolders.find(f=>f.id===fileFolderId((myFilesLibrary[0]||{}).id))||{}).name||'',"
                   "pending:localStorage.getItem('synthlucida_myfiles_pending'),box:document.getElementById('myfiles-pending').hidden})")
    c.ok(r["items"] == 1 and r["folder"] == "Spaní" and r["pending"] is None and r["box"],
         "Soubor „Rain  Night“ (2 mezery) se po obnovení zařadí do playlistu i složky a nečeká se na něj")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_robust(s, c):
    keys = sorted(set(re.findall(r"""['"]((?:sl-[a-z0-9-]+)|(?:synthlucida_[a-z0-9_]+))['"]""", s.env.player_src)))
    panels = ["", "toggleCustomQueue()", "toggleMainStats()", "toggleMainFiles()", "toggleSetup()", "playTrack(3)"]
    cases = [("zablokované úložiště prohlížeče", "Object.defineProperty(window,'localStorage',{get(){throw new DOMException('blocked','SecurityError')}})")]
    for name, val in (("poškozená data (nesmysl)", "{{{not json"), ("poškozená data (seznam)", '[null,5,{"a":1},"x"]'),
                      ("poškozená data (objekt)", '{"a":null,"items":5,"list":"x"}')):
        cases.append((name, "try{%s.forEach(k=>localStorage.setItem(k,%s))}catch(e){}" % (json.dumps(keys), json.dumps(val))))
    for name, init in cases:
        bad = []
        for fn in panels:
            ctx, p, errs = s.page(w=360, h=760, seen=False, init=init, wait=1800)
            if fn:
                try:
                    p.evaluate(fn)
                    p.wait_for_timeout(500)
                except Exception as e:
                    errs.append("%s: %s" % (fn, str(e)[:80]))
            rows = p.evaluate("document.querySelectorAll('#playlist li').length")
            if errs or rows < 5:
                bad.append((fn or "načtení") + ": " + (errs[0] if errs else "prázdný seznam"))
            ctx.close()
        c.ok(not bad, "Player nespadne: " + name + (" – " + " | ".join(bad[:2]) if bad else ""))


def t_layout(s, c):
    seed = """try{const q=[];for(let i=0;i<10;i++)q.push({title:'SYNTHLUCIDA - Velmi_dlouhy_nazev_bez_mezer_tracku_abcdefghij '+i,src:'%sv1.0/asmr1.mp3',playlist:'asmr',index:i});
      localStorage.setItem('synthlucida_custom_queue',JSON.stringify(q))}catch(e){}""" % ("http://localhost:%d/dl/" % s.env.p_gh)
    panels = ["", "toggleCustomQueue()", "toggleMainStats()", "toggleMainFiles()", "toggleSetup()", "togglePlFind()", "toggleMainGuide()", "cycleListSort()", "cycleListSort();cycleListSort()", "toggleCustomQueue();toggleQueueSelectMode()"]
    for w in (320, 375, 1280):
        bad = []
        for fn in panels:
            ctx, p, errs = s.page(w=w, h=760, init=seed, wait=1500)
            try:
                p.evaluate("document.fonts.ready")
            except Exception:
                pass
            if fn:
                p.evaluate(fn)
                p.wait_for_timeout(500)
            over = p.evaluate(OVERFLOW_JS)
            if over:
                bad.append((fn or "hlavní okno") + ": " + ", ".join(over[:2]))
            ctx.close()
        c.ok(not bad, "Šířka %d px: nic nepřetéká ven z obrazovky" % w + (" – " + " | ".join(bad[:2]) if bad else ""))


def t_i18n(s, c):
    src = s.env.player_src
    keys = set(re.findall(r'data-i18n(?:-placeholder|-aria)?="([^"]+)"', src))
    ctx, p, errs = s.page()
    # text bez překladu, který má pevný nápis přímo ve stránce (např. MICROSOFT STORE), je v pořádku - zůstane stejný v obou jazycích
    bad = p.evaluate("""(keys)=>keys.filter(k=>{const e=i18nText[k];
      if(!e){const el=document.querySelector('[data-i18n="'+k+'"],[data-i18n-placeholder="'+k+'"],[data-i18n-aria="'+k+'"]');
        return !(el&&(el.textContent.trim()||el.getAttribute('placeholder')||el.getAttribute('aria-label')))}
      return !String(e.en||'').trim()||!String(e.cz||'').trim()})""", sorted(keys))
    c.ok(not bad, "Všech %d textů má českou i anglickou verzi" % len(keys) + (" – chybí: " + ", ".join(bad[:6]) if bad else ""))
    btn_txt = p.evaluate("document.querySelector('.lang-toggle-btn') ? document.querySelector('.lang-toggle-btn').innerText : ''")
    p.evaluate("toggleGuideLanguage()")
    p.wait_for_timeout(300)
    for fn in ("toggleMainGuide()", "toggleSetup()", "toggleCustomQueue()", "toggleMainFiles()", "toggleMainStats()"):
        p.evaluate(fn)
        p.wait_for_timeout(250)
        p.evaluate(fn)
    r = p.evaluate("""()=>({lang:guideLang,empty:Array.from(document.querySelectorAll('[data-i18n]')).filter(e=>!e.textContent.trim()).map(e=>e.getAttribute('data-i18n')).slice(0,5),
      sample:(document.querySelector('[data-i18n="tour_card_btn"]')||{}).textContent||''})""")
    c.ok(r["lang"] != "en" or btn_txt == "", "Přepnutí jazyka funguje (teď: %s)" % r["lang"])
    c.ok(not r["empty"], "Po přepnutí jazyka není žádný text prázdný" + (": " + ", ".join(r["empty"]) if r["empty"] else ""))
    p.reload()
    p.wait_for_timeout(1500)
    c.ok(p.evaluate("guideLang") == r["lang"], "Zvolený jazyk se pamatuje i po novém načtení")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_timer(s, c):
    ctx, p, errs = s.page()
    p.evaluate("playTrack(2)")
    p.wait_for_timeout(1200)
    p.evaluate("setTimer(15)")
    p.wait_for_timeout(1300)
    r = p.evaluate("()=>({txt:document.getElementById('timer-display').innerText,active:!!document.querySelector('.btn-timer.active[data-timer-min=\"15\"]')})")
    c.ok(re.match(r"⏱ 1[45]:\d\d", r["txt"]) and r["active"], "Časovač 15 minut se spustí a odpočítává (%s)" % r["txt"])
    p.evaluate("cancelTimer()")
    p.wait_for_timeout(300)
    c.ok("Off" in p.evaluate("document.getElementById('timer-display').innerText"), "Křížek časovač zruší")
    v0 = p.evaluate("audio.volume")
    p.evaluate("()=>{setTimer(1);timerEndsAt=Date.now()+4000}")
    p.wait_for_timeout(2200)
    mid = p.evaluate("()=>({mul:sleepFadeMul,paused:audio.paused})")
    c.ok(mid["mul"] < 1 and not mid["paused"], "Před koncem se hudba plynule ztiší")
    p.wait_for_timeout(3500)
    r = p.evaluate("()=>({paused:audio.paused,mul:sleepFadeMul,vol:audio.volume,txt:document.getElementById('timer-display').innerText})")
    c.ok(r["paused"] and "Off" in r["txt"], "Po uplynutí času se hudba zastaví")
    c.ok(r["mul"] == 1 and abs(r["vol"] - v0) < 0.02, "Hlasitost se pak vrátí, aby další přehrání nezačalo v tichu")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_repeat(s, c):
    ctx, p, errs = s.page()
    p.evaluate("playTrack(4)")
    p.wait_for_timeout(1200)
    p.evaluate("toggleRepeat()")
    c.ok(p.evaluate("repeatEnabled && document.getElementById('btn-repeat').classList.contains('active')"), "Tlačítko opakování se zapne")
    p.evaluate("audio.currentTime=Math.max(0,audio.duration-0.6)")
    p.wait_for_timeout(2500)
    r = p.evaluate("()=>({i:currentTrackIndex,t:audio.currentTime,paused:audio.paused})")
    c.ok(r["i"] == 4 and r["t"] < 5 and not r["paused"], "S opakováním hraje po skončení stejná skladba znovu")
    p.evaluate("toggleShuffle()")
    r = p.evaluate("()=>({sh:shuffleEnabled,rep:repeatEnabled,b:document.getElementById('btn-repeat').classList.contains('active')})")
    c.ok(r["sh"] and not r["rep"] and not r["b"], "Zapnutí náhodného pořadí opakování vypne (nejdou zároveň)")
    seen = set()
    for _ in range(4):
        p.evaluate("playNextTrack()")
        p.wait_for_timeout(700)
        seen.add(p.evaluate("currentTrackIndex"))
    c.ok(len(seen) >= 3 and not p.evaluate("audio.paused"), "Náhodné pořadí hraje různé skladby (%d různé ze 4)" % len(seen))
    p.evaluate("toggleShuffle()")
    p.evaluate("audio.pause()")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_offline_app(s, c):
    s.env.reset()
    ctx, p, errs = s.page(sw=True)
    p.evaluate(CLEAR_AUDIO_CACHE_JS)
    p.evaluate(PIANO_JS)
    p.wait_for_timeout(600)
    p.evaluate("runOfflineDownload([2])")
    p.wait_for_timeout(4000)
    c.ok(p.evaluate("isTrackCached(tracks.piano[2].src)"), "Skladba se stáhne do offline")
    p.wait_for_timeout(1500)
    ctx.set_offline(True)
    try:
        p.reload()
        p.wait_for_timeout(2500)
        rows = p.evaluate("document.querySelectorAll('#playlist li').length")
        c.ok(rows > 5, "Player se načte i úplně bez internetu (%d skladeb v seznamu)" % rows)
        p.evaluate(PIANO_JS)
        p.wait_for_timeout(600)
        p.evaluate("playTrack(2)")
        p.wait_for_timeout(2500)
        c.ok(not p.evaluate("audio.paused") and p.evaluate("audio.currentTime") > 0.5, "Stažená skladba bez internetu hraje")
        bad = [e for e in errs if "Failed to fetch" not in e and "NetworkError" not in e]
        c.ok(not bad, "Bez chyby ve stránce" + (": " + bad[0] if bad else ""))
    finally:
        ctx.set_offline(False)


def t_sw_update(s, c):
    s.env.reset()
    orig = s.env.sw
    try:
        ctx, p, errs = s.page(sw=True)
        p.evaluate(CLEAR_AUDIO_CACHE_JS)
        p.evaluate(PIANO_JS)
        p.wait_for_timeout(600)
        p.evaluate("runOfflineDownload([3])")
        p.wait_for_timeout(4000)
        c.ok(p.evaluate("isTrackCached(tracks.piano[3].src)"), "Skladba je stažená offline")
        keys0 = p.evaluate("caches.keys()")
        m = re.search(r"synthlucida-app-v(\d+)", orig)
        if not m:
            c.ok(False, "V sw.js chybí číslo verze (synthlucida-app-vČÍSLO)")
            return
        newname = "synthlucida-app-v%d" % (int(m.group(1)) + 1)
        s.env.sw = orig.replace(m.group(0), newname) + "\n// update %s\n" % time.time()
        p.reload()
        p.wait_for_timeout(3000)
        p.reload()
        p.wait_for_timeout(2500)
        keys1 = p.evaluate("caches.keys()")
        c.ok(newname in keys1, "Nový sw.js se nainstaluje (%s)" % newname)
        c.ok(m.group(0) not in keys1 and m.group(0) in keys0, "Stará kopie aplikace se uklidí")
        c.ok(p.evaluate("isTrackCached(tracks.piano[3].src)"), "Stažené skladby po aktualizaci zůstanou")
        c.ok(p.evaluate("document.querySelectorAll('#playlist li').length") > 5 and not errs, "Player po aktualizaci normálně běží" + (": " + errs[0] if errs else ""))
    finally:
        s.env.sw = orig


def t_settings(s, c):
    ctx, p, errs = s.page()
    p.evaluate("()=>{const el=document.getElementById('volume-slider');el.value=0.42;el.dispatchEvent(new Event('input',{bubbles:true}))}")
    p.evaluate("()=>{setVisualTheme('space', document.querySelector(\".btn-theme[onclick*=\\\"'space'\\\"]\"));setCrossfade(6, document.querySelector('.btn-crossfade[data-cf=\"6\"]'));saveSettings()}")
    p.wait_for_timeout(400)
    p.reload()
    p.wait_for_timeout(1800)
    r = p.evaluate("()=>({vol:+document.getElementById('volume-slider').value,theme:document.body.classList.contains('theme-space'),cf:crossfadeDuration})")
    c.ok(abs(r["vol"] - 0.42) < 0.011, "Hlasitost se pamatuje (%.2f)" % r["vol"])
    c.ok(r["theme"], "Uložené barevné téma se po novém spuštění obnoví")
    c.ok(r["cf"] == 6, "Uložené prolínání skladeb se obnoví (%s s)" % r["cf"])
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_extras(s, c):
    ctx, p, errs = s.page()
    n = p.evaluate("""()=>{const b=Array.from(document.querySelectorAll('.btn-theme[onclick*="setVisualTheme"]'));b.forEach(x=>x.click());return b.length}""")
    p.wait_for_timeout(500)
    c.ok(n >= 20 and not errs, "Všech %d barevných témat jde přepnout bez chyby" % n)
    p.evaluate("startBreathing()")
    p.wait_for_timeout(1500)
    on = p.evaluate("breathingActive")
    p.evaluate("stopBreathing()")
    p.wait_for_timeout(500)
    c.ok(on and not p.evaluate("breathingActive") and not errs, "Dechové cvičení se spustí a zase vypne")
    p.evaluate("startBinaural()")
    p.wait_for_timeout(1500)
    on = p.evaluate("binauralActive")
    p.evaluate("stopBinaural()")
    p.wait_for_timeout(600)
    c.ok(on and not p.evaluate("binauralActive") and not errs, "Binaurální tóny se otevřou a zavřou")
    p.evaluate("playTrack(1)")
    p.wait_for_timeout(1200)
    p.evaluate("toggleHistory()")
    p.wait_for_timeout(300)
    title = p.evaluate("document.getElementById('track-title').textContent")
    hist = p.evaluate("document.getElementById('history-list').innerText")
    c.ok(title and title.split(" ")[0] in hist, "Historie poslechu ukazuje právě hranou skladbu")
    p.evaluate("toggleHistory()")
    other = p.evaluate("displayTrackTitle(tracks.piano[4].title)")
    p.evaluate("toggleSearch()")
    p.wait_for_timeout(300)
    p.fill("#search-input", other)
    p.wait_for_timeout(500)
    res = p.evaluate("document.getElementById('search-results').innerText")
    c.ok(other in res, "Hledání (lupa) najde skladbu i z jiného playlistu (%s)" % other)
    p.evaluate("toggleSearch()")
    p.evaluate("toggleMainStats()")
    p.wait_for_timeout(400)
    c.ok(len(p.evaluate("document.getElementById('main-stats').innerText")) > 40, "Statistiky (S) se zobrazí")
    p.evaluate("toggleMainStats()")
    p.evaluate("audio.pause()")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


MS_JS = """window.__msh={};try{const o=navigator.mediaSession.setActionHandler.bind(navigator.mediaSession);
navigator.mediaSession.setActionHandler=function(a,h){window.__msh[a]=h;return o(a,h)}}catch(e){}"""


def t_media_session(s, c):
    ctx, p, errs = s.page(init=MS_JS)
    have = p.evaluate("Object.keys(window.__msh||{}).sort()")
    c.ok(all(a in have for a in ("play", "pause", "nexttrack", "previoustrack")), "Ovládání ze zamčené obrazovky a sluchátek je nastavené (%s)" % ", ".join(have))
    p.evaluate("playTrack(2)")
    p.wait_for_timeout(1500)
    r = p.evaluate("()=>({t:(navigator.mediaSession.metadata||{}).title||'',shown:document.getElementById('track-title').textContent})")
    c.ok(r["t"] and r["t"].lower() == r["shown"].lower(), "Na zamčené obrazovce je správný název skladby (%s)" % r["t"])
    p.evaluate("window.__msh.nexttrack()")
    p.wait_for_timeout(1200)
    c.ok(p.evaluate("currentTrackIndex") != 2 and not p.evaluate("audio.paused"), "Tlačítko další skladba ze zamčené obrazovky funguje")
    p.evaluate("window.__msh.pause()")
    p.wait_for_timeout(500)
    c.ok(p.evaluate("audio.paused"), "Pauza ze zamčené obrazovky funguje")
    p.evaluate("window.__msh.play()")
    p.wait_for_timeout(900)
    c.ok(not p.evaluate("audio.paused"), "Play ze zamčené obrazovky funguje")
    p.evaluate("audio.pause()")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


# --------------------------------------------------------------------------- #
#  Chat a společné dýchání: falešný Firebase + "stroj času"
# --------------------------------------------------------------------------- #
# Chat i společné dýchání jedou přes Firebase. Prohlížeč má během testu zakázaný internet, takže se skutečný Firebase
# nikdy nenačte. Pro tyhle testy mu proto podstrčíme malou "falešnou" databázi v paměti stránky (nic se neposílá ven).
# Přes window.__fb pak testy hrají roli ostatních posluchačů (píšou zprávy, reagují, dýchají v místnosti...).
FAKE_FB_JS = r"""
(function(){
 if(window.__fb) return;
 var RST=window.setTimeout.bind(window);
 var offset=__OFFSET__, connected=__CONNECTED__, seq=0, user=null;
 var root={}, listeners=[], disc={}, fail={write:false,presence:false}, authCbs=[], pend=false;
 function now(){return Date.now()+offset}
 function split(p){return String(p||'').split('/').filter(Boolean)}
 function clone(v){return v===undefined?undefined:JSON.parse(JSON.stringify(v))}
 function getAt(p){var n=root,a=split(p);for(var i=0;i<a.length;i++){if(n==null||typeof n!=='object')return undefined;n=n[a[i]]}return n}
 function prune(a){for(var k=a.length-1;k>=1;k--){var path=a.slice(0,k),par=getAt(path.join('/'));
   if(par&&typeof par==='object'&&Object.keys(par).length===0){var gp=path.length===1?root:getAt(path.slice(0,-1).join('/'));delete gp[path[path.length-1]]}else break}}
 function setAt(p,v){var a=split(p);if(!a.length){root=(v==null?{}:v);return}
   var n=root;for(var i=0;i<a.length-1;i++){if(n[a[i]]==null||typeof n[a[i]]!=='object'){if(v==null)return;n[a[i]]={}}n=n[a[i]]}
   if(v==null)delete n[a[a.length-1]];else n[a[a.length-1]]=v;prune(a)}
 function resolve(v){if(v&&typeof v==='object'){if(v['.sv']==='timestamp')return now();var o=Array.isArray(v)?[]:{};for(var k in v)o[k]=resolve(v[k]);return o}return v}
 function cmp(a,b){return a<b?-1:(a>b?1:0)}
 function applyQuery(node,q){
   var ents=[];if(node&&typeof node==='object')Object.keys(node).forEach(function(k){ents.push([k,node[k]])});
   function ov(e){return q.orderBy==='child'?(e[1]&&typeof e[1]==='object'?e[1][q.child]:undefined):(q.orderBy==='value'?e[1]:e[0])}
   ents.sort(function(x,y){return cmp(ov(x),ov(y))||cmp(x[0],y[0])});
   if(q.start!==undefined)ents=ents.filter(function(e){var o=ov(e);return o!==undefined&&o>=q.start});
   if(q.end!==undefined)ents=ents.filter(function(e){var o=ov(e);return o!==undefined&&o<=q.end});
   if(q.limit)ents=ents.slice(-q.limit);
   var obj={},ord=[];ents.forEach(function(e){obj[e[0]]=e[1];ord.push(e[0])});return {obj:obj,ord:ord}}
 function Snap(path,qv,isNode){this._p=path;this.key=split(path).slice(-1)[0]||null;this._qv=qv;this.ref=new Ref(path)}
 Snap.prototype.val=function(){var v=this._qv.leaf!==undefined?this._qv.leaf:this._qv.obj;
   if(this._qv.leaf===undefined&&!this._qv.ord.length)return null;return clone(v)};
 Snap.prototype.exists=function(){return this.val()!==null};
 Snap.prototype.numChildren=function(){return this._qv.leaf!==undefined?0:this._qv.ord.length};
 Snap.prototype.forEach=function(fn){var qv=this._qv,self=this;if(qv.leaf!==undefined)return false;
   for(var i=0;i<qv.ord.length;i++){var k=qv.ord[i];var c=new Snap(self._p+'/'+k,{leaf:clone(qv.obj[k])});
     if(c._qv.leaf&&typeof c._qv.leaf==='object'){c._qv={obj:c._qv.leaf,ord:Object.keys(c._qv.leaf)}}
     if(fn(c)===true)return true}return false};
 function mkSnap(path,q){var node=getAt(path);
   if(node!==undefined&&(node===null||typeof node!=='object'))return new Snap(path,{leaf:node});
   var qv=applyQuery(node,q||{});return new Snap(path,qv)}
 function changed(){if(pend)return;pend=true;RST(function(){pend=false;listeners.slice().forEach(function(L){if(listeners.indexOf(L)>=0)evalL(L,false)})},0)}
 function evalL(L,first){
   if(L.path==='.info/connected'||L.path==='.info/serverTimeOffset')return;
   if(fail.presence&&L.path==='together'&&L.type==='value')return;
   var node=getAt(L.path);
   if(L.type==='value'){var s=JSON.stringify([L.q,node===undefined?null:node]);
     if(first||s!==L.last){L.last=s;L.cb(mkSnap(L.path,L.q))}return}
   var qv=applyQuery(node,L.q);
   qv.ord.forEach(function(k){var cs=JSON.stringify(qv.obj[k]);
     if(!(k in L.seen)){L.seen[k]=cs;if(L.type==='child_added')L.cb(childSnap(L.path,k,qv.obj[k]))}
     else if(L.seen[k]!==cs){L.seen[k]=cs;if(L.type==='child_changed')L.cb(childSnap(L.path,k,qv.obj[k]))}})}
 function childSnap(path,k,v){if(v&&typeof v==='object')return new Snap(path+'/'+k,{obj:clone(v),ord:Object.keys(v)});return new Snap(path+'/'+k,{leaf:v})}
 function write(fn){if(fail.write)return Promise.reject(new Error('PERMISSION_DENIED'));fn();changed();return Promise.resolve()}
 function Ref(path,q){this.path=path;this.key=split(path).slice(-1)[0]||null;this.q=q||{}}
 Ref.prototype.child=function(c){return new Ref(this.path+'/'+c)};
 Ref.prototype.push=function(v){var k='-K'+String(++seq).padStart(6,'0')+Math.random().toString(36).slice(2,5);var r=new Ref(this.path+'/'+k);
   if(v!==undefined){var pr=r.set(v);r.then=pr.then.bind(pr);r['catch']=pr['catch'].bind(pr)}return r};
 Ref.prototype.set=function(v){var p=this.path;return write(function(){setAt(p,resolve(clone(v)))})};
 Ref.prototype.remove=function(){var p=this.path;return write(function(){setAt(p,null)})};
 Ref.prototype.update=function(o){var p=this.path;return write(function(){for(var k in o){setAt((p?p+'/':'')+k,o[k]==null?null:resolve(clone(o[k])))}})};
 Ref.prototype.transaction=function(fn){var p=this.path;if(fail.write)return Promise.reject(new Error('PERMISSION_DENIED'));
   var cur=clone(getAt(p));var nv=fn(cur===undefined?null:cur);if(nv!==undefined)setAt(p,nv);changed();return Promise.resolve({committed:true,snapshot:mkSnap(p)})};
 Ref.prototype.onDisconnect=function(){var p=this.path;return {remove:function(){disc[p]=true;return Promise.resolve()},update:function(){disc[p]=true;return Promise.resolve()},cancel:function(){delete disc[p];return Promise.resolve()}}};
 function qref(r,o){var q={};for(var k in r.q)q[k]=r.q[k];for(var j in o)q[j]=o[j];return new Ref(r.path,q)}
 Ref.prototype.orderByChild=function(c){return qref(this,{orderBy:'child',child:c})};
 Ref.prototype.orderByValue=function(){return qref(this,{orderBy:'value'})};
 Ref.prototype.startAt=function(x){return qref(this,{start:x})};
 Ref.prototype.endAt=function(x){return qref(this,{end:x})};
 Ref.prototype.limitToLast=function(n){return qref(this,{limit:n})};
 Ref.prototype.on=function(type,cb,err){var L={path:this.path,q:this.q,type:type,cb:cb,err:err,last:undefined,seen:{}};listeners.push(L);
   RST(function(){if(listeners.indexOf(L)<0)return;
     if(L.path==='.info/connected'){cb(new Snap(L.path,{leaf:connected}));return}
     if(L.path==='.info/serverTimeOffset'){cb(new Snap(L.path,{leaf:offset}));return}
     if(fail.presence&&L.path==='together'&&type==='value'){if(err)err(new Error('PERMISSION_DENIED'));return}
     evalL(L,true)},0);return cb};
 Ref.prototype.off=function(type,cb){var p=this.path;listeners=listeners.filter(function(L){return !(L.path===p&&(!type||L.type===type)&&(!cb||L.cb===cb))})};
 Ref.prototype.once=function(type){var self=this;return new Promise(function(res){RST(function(){res(mkSnap(self.path,self.q))},0)})};
 var DB={ref:function(p){return new Ref(p||'')}};
 var AUTH={onAuthStateChanged:function(cb){authCbs.push(cb);RST(function(){cb(user)},0);return function(){}},
   signInWithEmailAndPassword:function(e,p){if(e==='admin@test.cz'&&p==='dobreheslo'){user={uid:'admin1',email:e};authCbs.forEach(function(f){f(user)});return Promise.resolve({user:user})}
     return Promise.reject(new Error('auth/invalid-credential'))},
   signOut:function(){user=null;authCbs.forEach(function(f){f(null)});return Promise.resolve()}};
 var apps=[];
 var dbfn=function(){return DB};dbfn.ServerValue={TIMESTAMP:{'.sv':'timestamp'}};
 window.firebase={apps:apps,
   initializeApp:function(cfg,name){var n=name||'[DEFAULT]';for(var i=0;i<apps.length;i++){if(apps[i].name===n)throw new Error('duplicate app')}
     var app={name:n,options:cfg,database:function(){return DB}};apps.push(app);return app},
   database:dbfn,auth:function(){return AUTH}};
 function infoFire(path,val){listeners.forEach(function(L){if(L.path===path)L.cb(new Snap(path,{leaf:val}))})}
 window.__fb={
  get:function(p){var v=getAt(p);return v===undefined?null:clone(v)},
  set:function(p,v){setAt(p,resolve(clone(v)));changed()},
  now:now,
  seed:function(text,o){o=o||{};var k='-K'+String(++seq).padStart(6,'0')+'s';var m={text:text,ts:now()-(o.ageMs||0)};if(o.isAdmin)m.isAdmin=true;setAt('messages/'+k,m);changed();return k},
  react:function(id,emoji,n){var p='reactions/'+id+'/'+emoji;setAt(p,(getAt(p)||0)+(n||1));changed()},
  others:function(n,ageMs){for(var i=0;i<n;i++){setAt('together/-O'+String(++seq).padStart(6,'0'),now()-(ageMs||0))}changed()},
  onlineOthers:function(n){for(var i=0;i<n;i++){setAt('presence/-P'+String(++seq).padStart(6,'0'),true)}changed()},
  setConnected:function(b){connected=!!b;infoFire('.info/connected',connected)},
  setOffset:function(ms){offset=ms;infoFire('.info/serverTimeOffset',offset)},
  denyPresence:function(b){fail.presence=(b!==false);if(fail.presence)listeners.filter(function(L){return L.path==='together'&&L.type==='value'}).forEach(function(L){if(L.err)L.err(new Error('PERMISSION_DENIED'))})},
  failWrites:function(b){fail.write=(b!==false)},
  listeners:function(prefix){return listeners.filter(function(L){return L.path.indexOf(prefix)===0}).length},
  disconnectPaths:function(){return Object.keys(disc)},
  user:function(){return user}
 };
})();
"""

# Stroj času: posune hodiny stránky (Date.now), případně je zrychlí nebo zafixuje na daný okamžik.
# Časovače (setTimeout/setInterval) se zrychlují stejně, takže "minuta dýchání" trvá jen pár vteřin.
TIME_JS = r"""
(function(){
 var S=__SPEED__, B=__BASE__, SH=__SHIFT__;
 var rn=Date.now.bind(Date), p0=performance.now(), t0=rn();
 var rst=window.setTimeout.bind(window), rsi=window.setInterval.bind(window);
 Date.now=function(){return (B!==null?B+(performance.now()-p0)*S:t0+(rn()-t0)*S)+SH};
 if(S!==1){window.setTimeout=function(f,ms){var a=Array.prototype.slice.call(arguments,2);return rst.apply(window,[f,(ms||0)/S].concat(a))};
  window.setInterval=function(f,ms){var a=Array.prototype.slice.call(arguments,2);return rsi.apply(window,[f,(ms||0)/S].concat(a))}}
})();
"""

# pro zkoušku tlačítka POZVAT: bez sdílení z telefonu, schránka se jen zapíše do proměnné
INVITE_JS = """try{Object.defineProperty(navigator,'share',{value:undefined,configurable:true,writable:true})}catch(e){}
try{Object.defineProperty(navigator,'clipboard',{value:{writeText:function(t){window.__clip=t;return Promise.resolve()}},configurable:true})}catch(e){}"""


def fb_init(offset=0, connected=True, speed=1, base=None, shift=0, extra=""):
    parts = []
    if speed != 1 or base is not None or shift:
        parts.append(TIME_JS.replace("__SPEED__", repr(float(speed))).replace("__BASE__", "null" if base is None else str(int(base)))
                     .replace("__SHIFT__", str(int(shift))))
    parts.append(FAKE_FB_JS.replace("__OFFSET__", str(int(offset))).replace("__CONNECTED__", "true" if connected else "false"))
    parts.append(extra)
    return "\n".join(parts)


def fb_page(s, wait=1300, w=390, h=800, **kw):
    """Stránka s falešným Firebase. Parametry: offset, connected, speed, base, shift, extra (+ to, co bere Session.page)."""
    keys = ("offset", "connected", "speed", "base", "shift", "extra")
    fk = {k: kw.pop(k) for k in keys if k in kw}
    return s.page(w=w, h=h, init=fb_init(**fk), wait=wait, **kw)


def set_lang(p, lang):
    p.evaluate("(l)=>{if(guideLang!==l)toggleGuideLanguage()}", lang)
    p.wait_for_timeout(250)


def open_chat_input(p):
    p.evaluate("()=>{const r=document.getElementById('community-input-row');if(!r.classList.contains('visible'))toggleCommunityInput(true)}")
    p.wait_for_timeout(150)


def chat_send(p, text, wait=500):
    open_chat_input(p)
    p.evaluate("(t)=>{document.getElementById('community-input').value=t;sendCommunityMessage()}", text)
    p.wait_for_timeout(wait)


def err_visible(p):
    return p.evaluate("document.getElementById('community-error').classList.contains('visible')")


def chat_msgs(p):
    return p.evaluate("__fb.get('messages')") or {}


def t_chat_offline(s, c):
    """Bez Firebase (jako když je chat nedostupný): nic nepadá a uživatel dostane srozumitelnou hlášku."""
    ctx, p, errs = s.page()
    c.ok(p.evaluate("typeof firebase") == "undefined", "Zkouška běží bez Firebase (nedostupný chat)")
    p.click("#community-toggle-btn")
    p.wait_for_timeout(250)
    c.ok(p.evaluate("document.getElementById('community-input-row').classList.contains('visible')"), "Tlačítko 💬 otevře pole pro zprávu")
    chat_send(p, "Ahoj", wait=250)
    c.ok(err_visible(p), "Odeslání bez připojení ukáže chybovou hlášku")
    c.ok(p.evaluate("document.getElementById('community-input').value") == "Ahoj", "Napsaný text zůstane v poli (nepřijde se o něj)")
    c.ok(p.evaluate("!localStorage.getItem('sl-last-community-msg-ts')"), "Neodeslaná zpráva nespustí hodinový/minutový limit")
    p.evaluate("toggleCommunityInput(false)")
    p.evaluate("toggleCommunityHistory(true)")
    p.wait_for_timeout(300)
    h = p.evaluate("()=>({vis:document.getElementById('community-history-panel').classList.contains('visible'),txt:document.getElementById('community-history-list').innerText.trim()})")
    c.ok(h["vis"] and h["txt"], "Historie zpráv se otevře a řekne, že je chat nedostupný (%s)" % h["txt"][:40])
    p.evaluate("toggleCommunityHistory(false)")
    p.wait_for_timeout(200)
    c.ok(not p.evaluate("document.getElementById('community-history-panel').classList.contains('visible')"), "Historie jde zavřít")
    c.ok(p.evaluate("document.getElementById('online-count-badge').textContent") == "–", "Počet posluchačů ukazuje pomlčku")
    # skrytý vstup pro admina (666 – kód jde změnit v Editoru playeru)
    code = PCFG.get("admin_code", "666")
    chat_send(p, code, wait=250)
    c.ok(p.evaluate("document.getElementById('community-admin-login-row').classList.contains('visible')"), "„%s“ otevře skrytá pole pro admina" % code)
    c.ok(p.evaluate("document.getElementById('community-input').value") == "", "„%s“ se nikdy neodešle jako zpráva" % code)
    p.evaluate("()=>{document.getElementById('community-admin-email').value='a@b.cz';document.getElementById('community-admin-password').value='x';adminLogin()}")
    p.wait_for_timeout(250)
    c.ok(err_visible(p), "Přihlášení admina bez připojení jen ukáže chybu")
    c.ok(not p.evaluate("!!window.__slIsAdmin"), "Admin se bez připojení nepřihlásí")
    p.evaluate("cancelAdminLogin()")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_chat(s, c):
    """Chat: odeslání, limity, zobrazení zprávy, reakce, počet posluchačů."""
    ctx, p, errs = fb_page(s, extra="__fb.onlineOthers(2);")
    set_lang(p, "en")
    c.ok(p.evaluate("document.getElementById('online-count-badge').textContent") == "3", "Počet posluchačů u 💬: já + 2 další = 3")
    p.evaluate("__fb.onlineOthers(1)")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("document.getElementById('online-count-badge').textContent") == "4", "Když se připojí další posluchač, číslo naskočí na 4")
    # --- odeslání ---
    p.click("#community-toggle-btn")
    p.wait_for_timeout(200)
    p.fill("#community-input", "Dobrou noc všem")
    p.press("#community-input", "Enter")
    p.wait_for_timeout(700)
    ms = chat_msgs(p)
    mid = next(iter(ms), None)
    m = ms.get(mid) or {}
    c.ok(len(ms) == 1 and m.get("text") == "Dobrou noc všem" and isinstance(m.get("ts"), (int, float)) and "isAdmin" not in m,
         "Zpráva se uloží (text + čas, bez příznaku admina)")
    c.ok(p.evaluate("document.getElementById('community-input').value") == "" and
         not p.evaluate("document.getElementById('community-input-row').classList.contains('visible')"), "Po odeslání se pole vyprázdní a schová")
    sl = p.evaluate("()=>{const e=document.getElementById('slogan-container');return {t:e.innerText,msg:e.classList.contains('community-msg'),adm:e.classList.contains('community-msg-admin'),vis:e.classList.contains('visible')}}")
    c.ok(sl["t"] == "Dobrou noc všem" and sl["msg"] and sl["vis"] and not sl["adm"], "Zpráva se zobrazí na hlavní obrazovce místo sloganu")
    rb = p.evaluate("()=>({vis:document.getElementById('community-reactions').classList.contains('visible'),n:document.querySelectorAll('#community-reactions .reaction-btn').length})")
    c.ok(rb["vis"] and rb["n"] == 6, "Pod zprávou je 6 tlačítek reakcí")
    # --- limity ---
    chat_send(p, "Druhá hned potom")
    c.ok(err_visible(p) and len(chat_msgs(p)) == 1, "Druhá zpráva do minuty se neodešle (limit 1 zpráva/min)")
    p.evaluate("()=>{localStorage.removeItem('sl-last-community-msg-ts');document.getElementById('community-error').classList.remove('visible')}")
    chat_send(p, "x" * 90, wait=700)
    lens = sorted(len(v.get("text", "")) for v in chat_msgs(p).values())
    c.ok(len(lens) == 2 and lens[-1] == 60, "Příliš dlouhý text se ořízne na 60 znaků (uloženo %s)" % (lens[-1] if lens else "-"))
    p.evaluate("()=>{localStorage.removeItem('sl-last-community-msg-ts');document.getElementById('community-error').classList.remove('visible');communityBannedWords.push('zakazane')}")
    chat_send(p, "tohle je ZAKAZANE slovo", wait=300)
    c.ok(err_visible(p) and len(chat_msgs(p)) == 2, "Zakázané slovo se neodešle (bez ohledu na velikost písmen)")
    p.evaluate("()=>{communityBannedWords.length=0;toggleCommunityInput(false)}")
    p.evaluate("()=>{localStorage.removeItem('sl-last-community-msg-ts');__fb.failWrites(true)}")
    chat_send(p, "nepůjde to", wait=500)
    c.ok(err_visible(p) and p.evaluate("!localStorage.getItem('sl-last-community-msg-ts')"), "Když zápis selže, ukáže se chyba a limit se nespustí")
    p.evaluate("__fb.failWrites(false)")
    # --- zpráva od někoho jiného + reakce ---
    oid = p.evaluate("__fb.seed('Pěkný večer')")
    p.wait_for_timeout(500)
    sl = p.evaluate("document.getElementById('slogan-container').innerText")
    c.ok(sl == "Pěkný večer", "Zpráva od jiného posluchače se objeví živě")
    p.evaluate("document.querySelectorAll('#community-reactions .reaction-btn')[0].click()")
    p.wait_for_timeout(500)
    r = p.evaluate("()=>({db:__fb.get('reactions/%s'),cnt:document.querySelector('#community-reactions .reaction-count').innerText,"
                   "done:document.querySelector('#community-reactions .reaction-btn').classList.contains('reacted'),ls:!!localStorage.getItem('sl-reacted-%s-🔥')})" % (oid, oid))
    c.ok(r["db"] == {"🔥": 1} and r["cnt"] == "1" and r["done"] and r["ls"], "Kliknutí na 🔥 zapíše reakci a zobrazí číslo 1")
    p.evaluate("document.querySelectorAll('#community-reactions .reaction-btn')[0].click()")
    p.wait_for_timeout(400)
    c.ok(p.evaluate("__fb.get('reactions/%s')" % oid) == {"🔥": 1}, "Stejným emoji jde na jednu zprávu reagovat jen jednou")
    p.evaluate("__fb.react('%s','❤️')" % oid)
    p.wait_for_timeout(400)
    cnts = p.evaluate("Array.from(document.querySelectorAll('#community-reactions .reaction-count')).map(x=>x.innerText).join()")
    c.ok(cnts.split(",")[:2] == ["1", "1"], "Reakce ostatních se promítnou do čísel hned (%s)" % cnts)
    # --- zpráva admina + bezpečnost textu ---
    p.evaluate("__fb.seed('NEWS z webu',{isAdmin:true})")
    p.wait_for_timeout(400)
    c.ok(p.evaluate("document.getElementById('slogan-container').classList.contains('community-msg-admin')"), "Zpráva admina je zvýrazněná")
    p.evaluate("""__fb.seed('<img src=x onerror="window.__xss=1"><b>tučně</b>')""")
    p.wait_for_timeout(500)
    x = p.evaluate("()=>({t:document.getElementById('slogan-container').innerText,tags:document.querySelectorAll('#slogan-container img,#slogan-container b').length,xss:!!window.__xss})")
    c.ok(x["tags"] == 0 and not x["xss"] and "<b>" in x["t"], "Zpráva s HTML kódem se ukáže jako obyčejný text (nic se nespustí)")
    # --- zpráva zmizí po 10 s ---
    p.wait_for_timeout(10300)
    gone = p.evaluate("()=>({vis:document.getElementById('slogan-container').classList.contains('community-msg'),bar:document.getElementById('community-reactions').classList.contains('visible')})")
    c.ok(not gone["vis"] and not gone["bar"], "Po 10 sekundách zpráva i reakce zmizí a vrátí se slogany")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_chat_history(s, c):
    """Chat: historie za 24 h, mazání starých zpráv, ADMIN zprávy, oznámení o nové reakci."""
    seeds = """window.__ids={};
      __ids.A=__fb.seed('Nedávná zpráva',{ageMs:30*60000});
      __ids.B=__fb.seed('Stará běžná zpráva',{ageMs:25*3600000});
      __ids.C=__fb.seed('Zpráva admina před 3 dny',{ageMs:3*86400000,isAdmin:true});
      __ids.D=__fb.seed('Admin po uplynutí 14 dnů',{ageMs:15*86400000,isAdmin:true});
      __ids.E=__fb.seed('Skoro 24 hodin stará',{ageMs:24*3600000-120000});
      __ids.F=__fb.seed('Těsně přes 24 hodin',{ageMs:24*3600000+120000});
      __fb.react(__ids.A,'❤️');__fb.react(__ids.B,'🔥');__fb.react(__ids.D,'🔥');"""
    ctx, p, errs = fb_page(s, extra=seeds)
    set_lang(p, "en")
    ids = p.evaluate("__ids")
    left = set(chat_msgs(p).keys())
    c.ok(left == {ids["A"], ids["C"], ids["E"], ids["F"]},
         "Po startu se smažou jen zprávy po životnosti (běžná po 24 h, admin po 14 dnech) – zbylo %d z 6" % len(left))
    c.ok(p.evaluate("__fb.get('reactions/%s')" % ids["B"]) is None and p.evaluate("__fb.get('reactions/%s')" % ids["D"]) is None and
         p.evaluate("__fb.get('reactions/%s')" % ids["A"]) == {"❤️": 1}, "Smazaným zprávám zmizí i reakce, ostatním zůstanou")
    c.ok(not p.evaluate("document.getElementById('slogan-container').classList.contains('community-msg')"), "Staré zprávy se po načtení appky znovu nepřehrávají")
    p.evaluate("toggleCommunityHistory(true)")
    p.wait_for_timeout(700)
    items = p.evaluate("""()=>Array.from(document.querySelectorAll('.community-history-item')).map(i=>({t:i.querySelector('.community-history-text').innerText.replace('ADMIN','').trim(),
      adm:i.classList.contains('is-admin'),badge:!!i.querySelector('.community-history-badge'),time:i.querySelector('.community-history-time').innerText}))""")
    c.ok([i["t"] for i in items] == ["Nedávná zpráva", "Skoro 24 hodin stará", "Zpráva admina před 3 dny"],
         "Historie ukáže zprávy z posledních 24 h a admin zprávu, od nejnovější (%d položky)" % len(items))
    c.ok(len(items) == 3 and items[2]["adm"] and items[2]["badge"] and not items[0]["adm"], "Admin zpráva má v historii štítek ADMIN")
    c.ok(items and items[0]["time"] == "30m ago", "U zprávy je stáří (%s)" % (items[0]["time"] if items else "-"))
    c.ok(p.evaluate("__fb.listeners('reactions/')") >= 3, "Dokud je historie otevřená, sleduje reakce u zpráv")
    cnt = p.evaluate("Array.from(document.querySelectorAll('.community-history-item')[0].querySelectorAll('.reaction-count')).map(x=>x.innerText).join()")
    c.ok(cnt.split(",")[:2] == ["0", "1"], "Počty reakcí v historii sedí (%s)" % cnt)
    p.evaluate("document.querySelectorAll('.community-history-item')[0].querySelector('.reaction-btn-sm').click()")
    p.wait_for_timeout(500)
    r = p.evaluate("()=>({db:__fb.get('reactions/%s'),done:document.querySelectorAll('.community-history-item')[0].querySelector('.reaction-btn-sm').classList.contains('reacted')})" % ids["A"])
    c.ok(r["db"] == {"❤️": 1, "🔥": 1} and r["done"], "Z historie jde na zprávu zpětně reagovat")
    p.click("#community-history-close-btn")
    p.wait_for_timeout(300)
    c.ok(not p.evaluate("document.getElementById('community-history-panel').classList.contains('visible')") and p.evaluate("__fb.listeners('reactions/')") == 0,
         "Po zavření historie se přestanou sledovat reakce")
    # oznámení o nové reakci (animace) – jen při zapnutých REMINDERS
    pop = "document.getElementById('history-reaction-toast').classList.contains('pop')"
    p.evaluate("remindersEnabled=false")
    p.evaluate("__fb.react('%s','👍')" % ids["A"])
    p.wait_for_timeout(500)
    c.ok(not p.evaluate(pop), "S vypnutými REMINDERS se animace nové reakce nespustí")
    p.evaluate("remindersEnabled=true")
    p.evaluate("__fb.react('%s','✨')" % ids["A"])
    p.wait_for_timeout(600)
    t = p.evaluate("()=>({pop:%s,e:(document.querySelector('#history-reaction-toast .toast-emoji')||{}).textContent,t:(document.querySelector('#history-reaction-toast .toast-text')||{}).textContent})" % pop)
    c.ok(t["pop"] and t["e"] == "✨" and t["t"] == "Nedávná zpráva", "Reakce někoho jiného se ukáže jako animace s textem zprávy (%s %s)" % (t["e"], t["t"]))
    p.wait_for_timeout(2500)
    c.ok(not p.evaluate(pop), "Animace po chvíli zmizí")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_chat_admin(s, c):
    """Chat: skryté přihlášení admina přes 666 (účet ověřuje Firebase, ne kód v appce)."""
    ctx, p, errs = fb_page(s)
    set_lang(p, "en")
    row = "document.getElementById('community-admin-login-row').classList.contains('visible')"
    logout = "document.getElementById('admin-logout-btn').classList.contains('visible')"
    code = PCFG.get("admin_code", "666")
    chat_send(p, code, wait=250)
    c.ok(p.evaluate(row) and len(chat_msgs(p)) == 0, "„%s“ otevře pole pro přihlášení a nic neodešle" % code)
    p.press("#community-admin-email", "Escape")
    p.wait_for_timeout(200)
    c.ok(not p.evaluate(row), "Esc pole pro přihlášení zavře")
    chat_send(p, code, wait=250)
    p.evaluate("()=>{document.getElementById('community-admin-email').value='admin@test.cz';document.getElementById('community-admin-password').value='spatne';adminLogin()}")
    p.wait_for_timeout(500)
    c.ok(err_visible(p) and not p.evaluate("!!window.__slIsAdmin") and p.evaluate(row) and p.evaluate("__fb.user()") is None, "Špatné heslo: chyba, nikdo se nepřihlásí")
    p.evaluate("()=>{document.getElementById('community-admin-password').value='dobreheslo';adminLogin()}")
    p.wait_for_timeout(500)
    c.ok(p.evaluate("!!window.__slIsAdmin") and not p.evaluate(row) and p.evaluate(logout), "Správné heslo: admin přihlášen, pole zmizí, objeví se odhlášení 🔓")
    c.ok(p.evaluate("document.getElementById('community-admin-password').value") == "", "Heslo se po přihlášení z pole smaže")
    chat_send(p, "Novinka pro všechny", wait=500)
    chat_send(p, "Ještě jedna novinka", wait=500)
    ms = list(chat_msgs(p).values())
    c.ok(len(ms) == 2 and all(m.get("isAdmin") is True for m in ms), "Admin může poslat víc zpráv po sobě (bez limitu) a jsou označené jako admin")
    c.ok(p.evaluate("document.getElementById('slogan-container').classList.contains('community-msg-admin')"), "Admin zpráva se ukáže zvýrazněně")
    p.click("#admin-logout-btn")
    p.wait_for_timeout(400)
    c.ok(not p.evaluate("!!window.__slIsAdmin") and not p.evaluate(logout) and p.evaluate("__fb.user()") is None, "Odhlášení vrátí běžný režim")
    p.evaluate("localStorage.removeItem('sl-last-community-msg-ts')")
    chat_send(p, "Zase obyčejná zpráva", wait=500)
    n_adm = sum(1 for m in chat_msgs(p).values() if m.get("isAdmin"))
    chat_send(p, "A hned další", wait=300)
    c.ok(len(chat_msgs(p)) == 3 and n_adm == 2 and err_visible(p), "Po odhlášení je zpráva zase obyčejná a platí limit 1 zpráva/min")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


# --------------------------------------------------------------------------- #
#  Společné dýchání (TOGETHER)
# --------------------------------------------------------------------------- #
from datetime import datetime, timezone, timedelta

TG = {  # anglické texty místnosti (test si jazyk přepne na EN)
    "wait": "Join on the next inhale", "in": "Inhale through the nose", "hold": "Hold your breath", "out": "Exhale through the mouth",
    "title": "BREATHE TOGETHER", "many": "Breathing together right now: ", "alone": "You're the only one here", "offline": "Offline", "connecting": "Connecting",
}
TG_STATE_JS = """()=>{const g=id=>document.getElementById(id);const o=g('together-orb').style.transform.match(/scale\\(([\\d.]+)\\)/);
  const ov=g('together-overlay');
  return {open:document.body.classList.contains('together-active'),shown:getComputedStyle(ov).display!=='none',
    count:g('together-count-text').textContent,phase:g('together-phase').textContent,cd:g('together-countdown').textContent,
    type:g('together-arrow').getAttribute('data-type'),scale:o?+o[1]:null,sw:g('together-stopwatch').textContent,
    dots:document.querySelectorAll('#together-dots .together-dot').length,me:document.querySelectorAll('#together-dots .together-dot.me').length,
    evening:g('together-evening-text').textContent,banner:g('together-evening-banner').hidden?'':g('together-evening-banner').textContent,
    breathing:document.body.classList.contains('breathing-active')}}"""


def tg_state(p):
    return p.evaluate(TG_STATE_JS)


def prague_ms(y, mo, d, h, mi, sec, summer=False):
    tz = timezone(timedelta(hours=2 if summer else 1))
    return int(datetime(y, mo, d, h, mi, sec, tzinfo=tz).timestamp() * 1000)


def tg_open(p, lang="en", wait=500):
    """Otevře BREATHE a v něm místnost společného dýchání (tlačítkem TOGETHER, jako uživatel)."""
    set_lang(p, lang)
    p.evaluate("startBreathing()")
    p.wait_for_timeout(1300)
    p.click("#breath-together-btn")
    p.wait_for_timeout(wait)


def tg_set_cycle(p, ms_in_cycle):
    """Posune hodiny serveru tak, aby byl rytmus právě v daném místě 19s cyklu."""
    p.evaluate("(t)=>__fb.setOffset(t-(Date.now()%19000))", ms_in_cycle)


def t_together_offline(s, c):
    """Společné dýchání bez Firebase: místnost se otevře, rytmus běží podle hodin, nic nepadá."""
    ctx, p, errs = s.page()
    set_lang(p, "en")
    p.evaluate("startBreathing()")
    p.wait_for_timeout(1200)
    c.ok(p.evaluate("typeof firebase") == "undefined", "Zkouška běží bez Firebase")
    c.ok(p.evaluate("document.getElementById('breath-together-count').hidden"), "Na tlačítku TOGETHER není žádný počet lidí")
    p.click("#breath-together-btn")
    p.wait_for_timeout(700)
    st = tg_state(p)
    c.ok(st["open"] and st["shown"], "Místnost se otevře i bez připojení")
    c.ok(st["count"].startswith(TG["offline"]), "Nahoře stojí, že je bez připojení (%s)" % st["count"][:30])
    # Rytmus jede podle hodin (19s cyklus). Když se místnost otevře těsně před začátkem nádechu, za 0,7 s čekání
    # už nádech začne a správně se ukáže "Inhale" - to není chyba (dřív to test náhodně hlásil zhruba 1× z 25).
    waiting = st["phase"] == TG["wait"] and st["cd"].isdigit() and 1 <= int(st["cd"]) <= 19
    just_started = st["phase"] == TG["in"] and st["cd"].isdigit() and 1 <= int(st["cd"]) <= 4
    c.ok(waiting or just_started, "Čeká se na další celý nádech (zbývá %s s)" % st["cd"])
    ok = 0
    n = 0
    for _ in range(6):
        v = p.evaluate("()=>({t:Date.now()%19000,type:document.getElementById('together-arrow').getAttribute('data-type')})")
        t = v["t"]
        if min(abs(t - 4000), abs(t - 11000), t, 19000 - t) < 350:
            p.wait_for_timeout(400)
            continue
        n += 1
        ok += v["type"] == ("in" if t < 4000 else ("hold" if t < 11000 else "out"))
        p.wait_for_timeout(700)
    c.ok(n >= 3 and ok == n, "Rytmus 4-7-8 běží podle místních hodin (nádech 4 s, zádrž 7 s, výdech 8 s)")
    p.keyboard.press("Escape")
    p.wait_for_timeout(400)
    st = tg_state(p)
    c.ok(not st["open"] and st["breathing"], "Esc zavře jen místnost, dechové cvičení pod ní zůstane")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_together(s, c):
    """Společné dýchání: počet lidí, tečky, odchod, odpojení, skrytá karta, chyby připojení."""
    ctx, p, errs = fb_page(s, extra="__fb.others(3);")
    set_lang(p, "en")
    p.evaluate("startBreathing()")
    p.wait_for_timeout(1500)
    entry = p.evaluate("()=>({hidden:document.getElementById('breath-together-count').hidden,txt:document.querySelector('#breath-together-count .tg-entry-count-text').textContent})")
    c.ok(not entry["hidden"] and entry["txt"] == tgt("entryCount", n=3), "Na tlačítku TOGETHER je vidět, kolik lidí právě dýchá (%s)" % entry["txt"])
    p.click("#breath-together-btn")
    p.wait_for_timeout(800)
    st = tg_state(p)
    c.ok(st["open"] and st["shown"], "Tlačítko TOGETHER otevře místnost")
    c.ok(p.evaluate("breathingPaused"), "Rozběhnuté dechové cvičení se při tom pozastaví")
    c.ok(st["count"] == tgt("many", n=4), "Ukazuje počet lidí včetně mě: %s" % st["count"])
    c.ok(st["dots"] == 4 and st["me"] == 1, "Na kruhu je 4 teček a jedna je moje")
    c.ok(len([k for k in (p.evaluate("__fb.get('together')") or {})]) == 4 and any(x.startswith("together/") for x in p.evaluate("__fb.disconnectPaths()")),
         "Zapíšu se do místnosti a mám nastavený úklid při výpadku spojení")
    p.evaluate("__fb.others(2)")
    p.wait_for_timeout(400)
    c.ok(tg_state(p)["count"] == tgt("many", n=6), "Když se připojí dva další, číslo naskočí na 6")
    p.evaluate("__fb.others(2, 4*3600*1000);__fb.others(1, 7*3600*1000)")
    p.wait_for_timeout(500)
    c.ok(tg_state(p)["count"] == tgt("many", n=6), "Záznamy starší než 3 hodiny se nepočítají")
    c.ok(len(p.evaluate("__fb.get('together')")) == 8, "Zapomenuté záznamy starší než 6 hodin se samy uklidí")
    # schovaná karta / zhasnutý displej
    hide = "()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>%s});document.dispatchEvent(new Event('visibilitychange'))}"
    p.evaluate(hide % "true")
    p.wait_for_timeout(400)
    c.ok(tg_state(p)["count"] == tgt("many", n=5), "Když mám stránku schovanou, nepočítám se mezi dýchající")
    p.evaluate(hide % "false")
    p.wait_for_timeout(500)
    c.ok(tg_state(p)["count"] == tgt("many", n=6), "Po návratu na stránku se zase počítám")
    p.click("#together-close-btn")
    p.wait_for_timeout(500)
    st = tg_state(p)
    c.ok(not st["open"] and st["breathing"], "✖ zavře místnost, dechové cvičení zůstane otevřené")
    c.ok(len(p.evaluate("__fb.get('together')")) == 7 and p.evaluate("__fb.listeners('together')") == 0,
         "Po odchodu zmizím z místnosti a přestanu sledovat ostatní")
    # přístup odepřen
    p.click("#breath-together-btn")
    p.wait_for_timeout(500)
    p.evaluate("__fb.denyPresence()")
    p.wait_for_timeout(400)
    c.ok(tg_state(p)["count"].startswith(TG["offline"]), "Když databáze počet nepustí, ukáže se „bez připojení“ a rytmus běží dál")
    p.keyboard.press("Escape")
    p.wait_for_timeout(300)
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))
    # ztráta spojení
    ctx.close()
    ctx, p, errs = fb_page(s, connected=False)
    tg_open(p)
    c.ok(tg_state(p)["count"].startswith(TG["connecting"]), "Při navazování spojení se ukáže „Connecting…“")
    p.wait_for_timeout(6800)
    c.ok(tg_state(p)["count"].startswith(TG["offline"]), "Když se spojení nepodaří déle než pár vteřin, ukáže se „bez připojení“")
    p.evaluate("__fb.setConnected(true)")
    p.wait_for_timeout(1500)
    c.ok(tg_state(p)["count"].startswith(TG["alone"]), "Po obnovení spojení se text sám vrátí (%s)" % tg_state(p)["count"][:30])
    c.ok(not errs, "Bez chyby ve stránce (výpadek spojení)" + (": " + errs[0] if errs else ""))


def t_together_sync(s, c):
    """Společné dýchání: všichni dýchají ve stejnou vteřinu podle času serveru, i když mají špatně hodiny."""
    ctx, p, errs = fb_page(s)
    tg_set_cycle(p, 0)  # hodiny serveru zatím neřešíme, jen aby byly "známé"
    set_lang(p, "en")
    p.evaluate("startBreathing()")
    p.wait_for_timeout(1000)
    tg_set_cycle(p, 14000)
    p.wait_for_timeout(100)
    p.evaluate("openTogether()")
    p.wait_for_timeout(500)
    st = tg_state(p)
    c.ok(st["phase"] == TG["wait"] and st["cd"].isdigit() and 3 <= int(st["cd"]) <= 5,
         "Uprostřed výdechu ostatních se čeká na další celý nádech (zbývá %s s)" % st["cd"])
    p.wait_for_function("()=>document.getElementById('together-phase').textContent!==%s" % json.dumps(TG["wait"]), timeout=9000)
    st = tg_state(p)
    c.ok(st["phase"] == TG["in"] and st["type"] == "in", "S dalším nádechem naskočím do rytmu ostatních (%s)" % st["phase"])
    p.wait_for_timeout(2300)
    c.ok(tg_state(p)["sw"] in ("00:02", "00:03"), "Stopky počítají, jak dlouho dýchám (%s)" % tg_state(p)["sw"])
    for target, kind, label in ((2000, "in", TG["in"]), (7000, "hold", TG["hold"]), (14500, "out", TG["out"])):
        tg_set_cycle(p, target)
        p.wait_for_timeout(350)
        st = tg_state(p)
        ok_scale = (0.7 < st["scale"] < 1.06) if kind == "in" else ((st["scale"] > 1.1) if kind == "hold" else (0.64 < st["scale"] < 1.1))
        c.ok(st["type"] == kind and st["phase"] == label and ok_scale,
             "V %.1f s cyklu serveru je %s (koule %.2f×, %s)" % (target / 1000.0, kind, st["scale"] or 0, label))
    ctx.close()
    # tři posluchači s různě špatnými hodinami v telefonu, server má čas správný
    pages = []
    for shift in (0, 7300, -4100):
        cx, pg, er = fb_page(s, shift=shift, offset=-shift, extra="__fb.others(1);")
        set_lang(pg, "en")
        pg.evaluate("startBreathing()")
        pg.wait_for_timeout(900)
        pg.evaluate("openTogether()")
        pages.append((pg, er, shift))
    for pg, _, _ in pages:  # počkat, až naskočí do rytmu (nejdéle jeden celý cyklus 19 s)
        pg.wait_for_function("()=>document.getElementById('together-phase').textContent!==%s" % json.dumps(TG["wait"]), timeout=22000)
    pages[0][0].wait_for_timeout(500)
    same_type = 0
    worst = 0.0
    cd_ok = 0
    rounds = 5
    for _ in range(rounds):
        sts = [tg_state(pg) for pg, _, _ in pages]
        same_type += len({x["type"] for x in sts}) == 1
        worst = max(worst, max(x["scale"] for x in sts) - min(x["scale"] for x in sts))
        cds = [int(x["cd"]) for x in sts if x["cd"].isdigit()]
        cd_ok += bool(cds) and max(cds) - min(cds) <= 1
        pages[0][0].wait_for_timeout(1700)
    c.ok(same_type >= rounds - 1, "Tři zařízení s hodinami posunutými o +7,3 s a −4,1 s mají vždy stejnou fázi (%d z %d měření)" % (same_type, rounds))
    c.ok(worst < 0.05, "Koule se u všech tří hýbe současně (největší rozdíl %.3f)" % worst)
    c.ok(cd_ok >= rounds - 1, "Odpočet vteřin je u všech stejný (±1 s)")
    c.ok(all(not er for _, er, _ in pages), "Bez chyby ve stránce (3 zařízení)")


def t_together_extras(s, c):
    """Společné dýchání: srdíčka, VIBE, tvar, POZVAT, kalendář (.ics), jazyk, odkaz #together, Esc."""
    ctx, p, errs = fb_page(s, extra=INVITE_JS + "__fb.others(2);", wait=1300)
    tg_open(p, "en", wait=700)
    p.wait_for_timeout(600)
    # srdíčka
    p.click("#together-heart-btn")
    p.wait_for_timeout(500)
    h = p.evaluate("()=>({n:document.querySelectorAll('#together-hearts .together-heart').length,db:__fb.get('togetherHearts/count'),"
                   "on:document.getElementById('together-heart-btn').classList.contains('tg-alt')})")
    c.ok(h["n"] == 1 and h["db"] == 1 and h["on"], "POSLAT KLID: vznikne srdíčko, počet se zvýší o 1 a tlačítko ukáže „poslané“")
    p.click("#together-heart-btn")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("__fb.get('togetherHearts/count')") == 1, "Dvakrát za sebou to nejde (pauza 3 s)")
    p.evaluate("__fb.set('togetherHearts/count', 4)")
    mx = 0
    for _ in range(8):
        p.wait_for_timeout(300)
        mx = max(mx, p.evaluate("document.querySelectorAll('#together-hearts .together-heart').length"))
    c.ok(mx >= 3, "Srdíčka od ostatních se ukážou (naráz až %d)" % mx)
    p.wait_for_timeout(1000)
    p.click("#together-heart-btn")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("__fb.get('togetherHearts/count')") == 5, "Po pauze jde poslat další srdíčko")
    # VIBE a tvar
    v0 = p.evaluate("vibrationEnabled")
    p.click("#together-vibe-btn")
    p.wait_for_timeout(200)
    v1 = p.evaluate("vibrationEnabled")
    p.click("#together-vibe-btn")
    p.wait_for_timeout(200)
    c.ok(v0 != v1 and p.evaluate("vibrationEnabled") == v0, "VIBE se přepíná zapnuto/vypnuto")
    sh0 = p.evaluate("document.body.classList.contains('shape-mandala')")
    p.click("#together-shape-btn")
    p.wait_for_timeout(250)
    sh1 = p.evaluate("document.body.classList.contains('shape-mandala')")
    p.click("#together-shape-btn")
    p.wait_for_timeout(250)
    c.ok(sh0 != sh1 and p.evaluate("document.body.classList.contains('shape-mandala')") == sh0, "Tvar uprostřed jde přepnout z koule na mandalu a zpět")
    # POZVAT
    p.click("#together-invite-btn")
    p.wait_for_timeout(400)
    clip = p.evaluate("window.__clip||''")
    c.ok("https://synthlucida.com/player.html#together" in clip and p.evaluate("document.getElementById('together-invite-btn').classList.contains('tg-alt')"),
         "POZVAT bez sdílení v telefonu zkopíruje odkaz a tlačítko ukáže „zkopírováno“")
    p.evaluate("navigator.share=function(d){window.__shared=d;return Promise.resolve()}")
    p.click("#together-invite-btn")
    p.wait_for_timeout(300)
    sh = p.evaluate("window.__shared||null")
    c.ok(sh and sh.get("url") == "https://synthlucida.com/player.html#together" and sh.get("title") and sh.get("text"), "S podporou sdílení se otevře sdílení s odkazem na místnost")
    # připomínka do kalendáře
    with p.expect_download() as dl:
        p.click(".together-link-btn")
    d = dl.value
    ics = open(d.path(), encoding="utf-8", newline="").read()
    c.ok(d.suggested_filename == TGL["en"]["icsFile"], "Stáhne se soubor do kalendáře (%s)" % d.suggested_filename)
    c.ok(all(x in ics for x in ("BEGIN:VCALENDAR", "BEGIN:VEVENT", "RRULE:FREQ=DAILY", "DURATION:PT10M", "BEGIN:VALARM", "TRIGGER:-PT5M", "END:VCALENDAR")) and
         re.search(r"DTSTART;TZID=Europe/Prague:\d{8}T210000", ics), "V kalendáři je denní událost ve 21:00 pražského času na 10 minut s upozorněním 5 minut předem")
    c.ok("\r\n" in ics and "URL:https://synthlucida.com/player.html#together" in ics, "Soubor má správné konce řádků a odkaz zpět do playeru")
    # jazyk
    p.evaluate("toggleGuideLanguage()")
    p.wait_for_timeout(500)
    cz = p.evaluate("()=>({title:document.getElementById('together-title').textContent,heart:document.querySelector('#together-heart-btn .tg-lbl-a').textContent,"
                    "count:document.getElementById('together-count-text').textContent,entry:document.querySelector('#breath-together-btn [data-tg=entryBtn]').textContent})")
    c.ok(cz["title"] == TGL["cz"]["title"] and TGL["cz"]["heartBtn"] in cz["heart"] and cz["entry"] == TGL["cz"]["entryBtn"]
         and any(cz["count"].startswith(tgpre(k, "cz", 12)) for k in ("many", "alone", "connecting", "offline")),
         "Po přepnutí do češtiny se přeloží celá místnost (%s)" % cz["title"])
    with p.expect_download() as dl:
        p.click(".together-link-btn")
    c.ok(dl.value.suggested_filename == TGL["cz"]["icsFile"], "Česká připomínka má český název souboru")
    p.keyboard.press("Escape")
    p.wait_for_timeout(400)
    st = tg_state(p)
    c.ok(not st["open"] and st["breathing"], "Esc zavře jen místnost")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))
    ctx.close()
    # odkaz #together otevře místnost rovnou
    ctx, p, errs = fb_page(s, extra="try{location.hash='together'}catch(e){}", wait=2200)
    st = tg_state(p)
    c.ok(st["open"] and st["breathing"], "Odkaz s #together (pozvánka, kalendář) otevře místnost rovnou")
    c.ok(not errs, "Bez chyby ve stránce (odkaz #together)" + (": " + errs[0] if errs else ""))


def t_together_evening(s, c):
    """Společné dýchání: večerní sezení ve 21:00 pražského času (i při letním/zimním času a v jiném časovém pásmu)."""
    def case(base, tz="Europe/Prague"):
        cx, pg, er = fb_page(s, base=base, timezone_id=tz, wait=900)
        set_lang(pg, "en")
        pg.evaluate("startBreathing()")
        pg.wait_for_timeout(1300)
        live_btn = pg.evaluate("document.getElementById('breath-together-btn').classList.contains('live')")
        pg.click("#breath-together-btn")
        pg.wait_for_timeout(700)
        st = tg_state(pg)
        st["live_btn"] = live_btn
        st["errs"] = er
        return cx, pg, st
    cx, pg, st = case(prague_ms(2026, 12, 15, 12, 0, 0))
    at = re.escape(tgt("timeAt"))
    c.ok(st["banner"] == "" and not st["live_btn"] and re.match(tgrx("eveningToday", time=at, **{"in": "(?:%s|%s)" % (
        re.escape(tgt("durH", h=9)), re.escape(tgt("durHMin", h=8, m=59)))}), st["evening"]),
         "Ve 12:00: „večerní sezení dnes ve 21:00 za 9 h“ (%s)" % st["evening"])
    c.ok(not st["errs"], "Bez chyby ve stránce (12:00)")
    cx.close()
    cx, pg, st = case(prague_ms(2026, 12, 15, 21, 3, 30))
    c.ok(re.match(tgrx("eveningLiveBanner", left=r"[56]:\d\d"), st["banner"]) and st["live_btn"] and st["evening"] == "",
         "Ve 21:03:30 běží sezení: nahoře pruh se zbývajícím časem, tlačítko TOGETHER svítí (%s)" % st["banner"])
    cx.close()
    cx, pg, st = case(prague_ms(2026, 12, 15, 21, 10, 30))
    c.ok(st["banner"] == "" and not st["live_btn"] and tgt("eveningTomorrow", time=tgt("timeAt")).split("{in}")[0].strip() in st["evening"], "Po 21:10 sezení skončí a nabídne se zítřek (%s)" % st["evening"][:45])
    with pg.expect_download() as dl:
        pg.click(".together-link-btn")
    ics = open(dl.value.path(), encoding="utf-8").read()
    c.ok("DTSTART;TZID=Europe/Prague:20261216T210000" in ics, "Připomínka do kalendáře začíná od zítřka (16. 12.) ve 21:00")
    cx.close()
    cx, pg, st = case(prague_ms(2026, 7, 15, 21, 5, 0, summer=True))
    c.ok(st["banner"].startswith(tgpre("eveningLiveBanner", n=200).strip()) and st["live_btn"], "V létě (21:05 letního času) sezení také běží – posun času se počítá správně")
    cx.close()
    cx, pg, st = case(prague_ms(2026, 7, 15, 20, 59, 20, summer=True))
    c.ok(st["banner"] == "" and re.match(tgrx("eveningToday", time=re.escape(tgt("timeAt")), **{"in": re.escape(tgt("durMin", m=1))}), st["evening"]), "V 20:59 je do sezení 1 minuta (%s)" % st["evening"])
    cx.close()
    cx, pg, st = case(prague_ms(2026, 12, 15, 12, 0, 0), tz="America/New_York")
    c.ok(all(x.strip() in st["evening"] for x in TGL["en"]["timeAtLocal"].split("{local}") if x.strip()), "V jiném časovém pásmu text říká i čas u uživatele (%s)" % st["evening"][:70])
    cx.close()


def t_together_stats(s, c):
    """Společné dýchání: stopky, denní statistika (lidé, minuty) a započtení do XP / progresu. Čas je zrychlený."""
    ctx, p, errs = fb_page(s, speed=8, extra="__fb.others(1);")
    set_lang(p, "en")
    p.evaluate("startBreathing()")
    p.wait_for_timeout(500)
    before = p.evaluate("stats.breathingSessions||0")
    p.evaluate("openTogether()")
    p.wait_for_function("()=>document.getElementById('together-phase').textContent!==%s" % json.dumps(TG["wait"]), timeout=8000)
    sw = "()=>document.getElementById('together-stopwatch').textContent"
    p.wait_for_function("()=>document.getElementById('together-stopwatch').textContent>='00:31'", timeout=15000)
    day = p.evaluate("Object.keys(__fb.get('togetherStats')||{})[0]")
    st = p.evaluate("__fb.get('togetherStats/%s')" % day) if day else None
    c.ok(st and st.get("people") == 1 and re.match(r"\d{4}-\d\d-\d\d$", day or ""), "Po 30 s dýchání se počítám do dnešní statistiky lidí (%s)" % st)
    c.ok(p.evaluate("localStorage.getItem('synthlucida_together_counted')") == day, "Dnešní započtení si zařízení pamatuje")
    p.wait_for_function("()=>document.getElementById('together-stopwatch').textContent>='01:02'", timeout=15000)
    st = p.evaluate("__fb.get('togetherStats/%s')" % day)
    c.ok(st.get("minutes", 0) >= 1 and st.get("people") == 1, "Každá celá minuta se přičte do společných minut (%s)" % st.get("minutes"))
    c.ok(re.match(tgrx("today", p="1", m=r"\d+"), p.evaluate("document.getElementById('together-today-text').textContent")),
         "V místnosti je vidět „dnes tu dýchalo lidí / společných minut“")
    p.click("#together-close-btn")
    p.wait_for_timeout(600)
    after = p.evaluate("stats.breathingSessions||0")
    c.ok(after == before + 1, "Minutové společné dýchání se započítá do statistik a XP (%d → %d)" % (before, after))
    p.click("#breath-together-btn")
    p.wait_for_function("()=>document.getElementById('together-phase').textContent!==%s" % json.dumps(TG["wait"]), timeout=8000)
    p.wait_for_function("()=>document.getElementById('together-stopwatch').textContent>='00:33'", timeout=15000)
    c.ok(p.evaluate("__fb.get('togetherStats/%s')" % day).get("people") == 1, "Ve stejný den se jeden člověk nepočítá dvakrát")
    p.click("#together-close-btn")
    p.wait_for_timeout(500)
    c.ok(p.evaluate("stats.breathingSessions||0") == after, "Krátké sezení (pod minutu) se do XP nepočítá")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def _probe(url, timeout=20):
    """True = soubor je na serveru, False = server řekl, že tam není (404/410), None = nepodařilo se zjistit."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers={"Range": "bytes=0-1", "User-Agent": "SynthlucidaTest/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in (200, 206)
    except urllib.error.HTTPError as e:
        return False if e.code in (404, 410) else None
    except Exception:
        return None


def t_online(s, c):
    from concurrent.futures import ThreadPoolExecutor
    urls = sorted(set(re.findall(r'src: "(%s[^"]+)"' % re.escape(GH_PREFIX), s.env.player_src)))
    m = re.search(r"const R2_BASE = '([^']*)';", s.env.sw_src)
    r2 = m.group(1) if m else ""
    jobs = [("GitHub", u, u) for u in urls]
    if r2:
        jobs += [("R2", u, r2 + u.rsplit("/", 1)[-1]) for u in urls]
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda j: (j[0], j[1], _probe(j[2])), jobs))
    for where in ("GitHub", "R2") if r2 else ("GitHub",):
        mine = [r for r in res if r[0] == where]
        found = sum(1 for w, u, ok in mine if ok is True)
        gone = sum(1 for w, u, ok in mine if ok is False)
        unknown = sum(1 for w, u, ok in mine if ok is None)
        if unknown == len(mine):
            c.ok(True, "%s: nepodařilo se ověřit (není internet?) – kontrola přeskočena" % where)
            continue
        # jen informace, test kvůli chybějící skladbě nikdy neselže
        c.ok(True, "%s: online – na serveru je teď %d skladeb (v playeru jich je %d)" % (where, found, len(mine)) +
             (", %d na serveru není" % gone if gone else "") + (", %d se nepodařilo ověřit" % unknown if unknown else ""))
    if not r2:
        c.ok(False, "V sw.js jsem nenašel adresu zálohy R2")


# --------------------------------------------------------------------------- #
#  ON AIR (co Dave právě poslouchá)
# --------------------------------------------------------------------------- #
ONAIR_LIVE_JS = """()=>{const g=id=>document.getElementById(id);return {btn:g('btn-onair').classList.contains('onair-live'),
  explore:g('atmos-toggle-explore').classList.contains('onair-live'),brand:!g('brand-onair').hidden}}"""
OWN_BLOB_JS = """async(a)=>{const r=await fetch(a.url);const bu=URL.createObjectURL(await r.blob());
  tracks['custom:file:onair1']=[{title:a.title,src:bu,fileId:'onair1'}];playlist=tracks['custom:file:onair1'];currentPlaylistKey='custom:file:onair1';playTrack(0)}"""


def onair_ready(p, c):
    if not p.evaluate("typeof openOnAir==='function'&&!!document.getElementById('btn-onair')&&!!document.getElementById('onair-window')"):
        c.ok(False, "Player nemá ON AIR (je to aktuální player.html?)")
        return False
    return True


def t_onair_admin(s, c):
    """ON AIR: přihlášený admin při hraní zapisuje název skladby, playlist, pauzu; úprava názvu; odhlášení."""
    ctx, p, errs = fb_page(s)
    if not onair_ready(p, c):
        return
    set_lang(p, "cz")
    p.evaluate("()=>{const B=%s;setCrossfade(0,null);shuffleEnabled=false;repeatEnabled=false;setPlaylist('piano',B('piano'));playTrack(2)}" % BTN_JS)
    p.wait_for_timeout(1200)
    c.ok(p.evaluate("__fb.get('onair')") is None, "Nepřihlášený uživatel do ON AIR nic nezapisuje")
    p.evaluate("()=>firebase.auth().signInWithEmailAndPassword('admin@test.cz','dobreheslo')")
    p.wait_for_timeout(900)
    r = p.evaluate("()=>({o:__fb.get('onair'),want:tracks.piano[2].title,disc:__fb.disconnectPaths()})")
    o = r["o"] or {}
    c.ok(o.get("playing") is True and o.get("title") == r["want"] and o.get("list") == "piano" and o.get("own") is False
         and isinstance(o.get("ts"), (int, float)) and isinstance(o.get("since"), (int, float)),
         "Admin po přihlášení zapíše: název skladby, hraje, playlist PIANO, čas (%s)" % o.get("title"))
    c.ok("onair" in r["disc"], "Během hraní je zapnutá pojistka: při ztrátě spojení ON AIR zhasne")
    lv = p.evaluate(ONAIR_LIVE_JS)
    c.ok(lv["btn"] and lv["explore"] and lv["brand"], "ON AIR svítí červeně v EXPLORE, tečka na EXPLORE a nápis u SYNTHLUCIDA")
    p.evaluate("()=>{const B=%s;setPlaylist('asmr',B('asmr'));playTrack(4)}" % BTN_JS)
    p.wait_for_timeout(1200)
    r = p.evaluate("()=>({o:__fb.get('onair'),want:tracks.asmr[4].title})")
    c.ok(r["o"]["title"] == r["want"] and r["o"]["list"] == "asmr" and r["o"]["playing"] is True, "Změna skladby (ASMR) se hned propíše i s playlistem")
    p.evaluate("()=>{const B=%s;setPlaylist('chill',B('chill'))}" % BTN_JS)
    p.wait_for_timeout(800)
    c.ok(p.evaluate("__fb.get('onair').title") == r["want"], "Jen otevřený jiný playlist (nic z něj nehraje) ON AIR nezmění")
    p.click("#btn-play")
    p.wait_for_timeout(900)
    r = p.evaluate("()=>({o:__fb.get('onair'),disc:__fb.disconnectPaths()})")
    c.ok(r["o"]["playing"] is False and not p.evaluate(ONAIR_LIVE_JS)["btn"] and "onair" not in r["disc"],
         "Pauza: ON AIR zhasne a pojistka se vypne")
    p.click("#btn-play")
    p.wait_for_timeout(900)
    c.ok(p.evaluate("__fb.get('onair').playing") is True, "Znovu ▶: ON AIR zase svítí")
    # vlastní soubor z F, název s mp3clan.com
    p.evaluate(OWN_BLOB_JS, {"url": s.env.r2_url + "onair_own.mp3", "title": "Enya_-_Only_Time_(mp3clan.com)"})
    p.wait_for_timeout(1300)
    o = p.evaluate("__fb.get('onair')")
    c.ok(o["title"] == "Enya - Only Time" and o["own"] is True and not o.get("list"),
         "Vlastní soubor: název bez „mp3clan.com“ a podtržítek, příznak vlastní, bez playlistu (%s)" % o["title"])
    # úprava názvu v okně (jen admin)
    p.evaluate("()=>{const B=%s;setPlaylist('piano',B('piano'));playTrack(2)}" % BTN_JS)
    p.wait_for_timeout(1200)
    since0 = p.evaluate("__fb.get('onair').since")
    p.wait_for_timeout(300)
    p.evaluate("openOnAir()")
    p.wait_for_timeout(600)
    r = p.evaluate("()=>({hid:document.getElementById('onair-admin').hidden,v:document.getElementById('onair-name').value,want:tracks.piano[2].title})")
    c.ok(not r["hid"] and r["v"] == r["want"], "Admin vidí v okně pole s názvem právě hrající skladby")
    p.fill("#onair-name", "Synthlucida Nebula Piano relaxing music")
    p.press("#onair-name", "Enter")
    p.wait_for_timeout(500)
    o = p.evaluate("__fb.get('onair')")
    c.ok(o["title"] == "Synthlucida Nebula Piano relaxing music" and o["list"] == "piano" and o["since"] == since0,
         "Upravený název se hned pošle (playlist i „hraje od“ zůstanou)")
    c.ok("Původní" in p.evaluate("document.getElementById('onair-name-hint').textContent"), "Pod polem je vidět původní název")
    p.evaluate("()=>playTrack(3)")
    p.wait_for_timeout(1200)
    r = p.evaluate("()=>({t:__fb.get('onair').title,v:document.getElementById('onair-name').value,want:tracks.piano[3].title})")
    c.ok(r["t"] == r["want"] and r["v"] == r["want"], "Další skladba má zase svůj název (pole se přepne samo)")
    p.evaluate("()=>playTrack(2)")
    p.wait_for_timeout(1200)
    c.ok(p.evaluate("__fb.get('onair').title") == "Synthlucida Nebula Piano relaxing music", "Upravená skladba použije uložený název i příště")
    p.click("#onair-name-reset")
    p.wait_for_timeout(500)
    c.ok(p.evaluate("__fb.get('onair').title") == p.evaluate("tracks.piano[2].title"), "PŮVODNÍ vrátí původní název")
    p.evaluate("closeOnAir()")
    # "jsem tu" každou minutu
    ts0 = p.evaluate("__fb.get('onair').ts")
    p.evaluate("()=>{const rn=Date.now.bind(Date);Date.now=()=>rn()+61000}")
    p.wait_for_timeout(1500)
    c.ok(p.evaluate("__fb.get('onair').ts") > ts0 + 50000, "Každou minutu pošle „jsem tu“ (jinak by ON AIR po 4 min zhasl)")
    # pravidla bez položky list
    p.evaluate("""()=>{const P=Object.getPrototypeOf(firebase.database().ref('x'));const o=P.update;window.__oUpd=o;
      P.update=function(d){if(this.path==='onair'&&d&&'list' in d)return Promise.reject(new Error('PERMISSION_DENIED'));return o.call(this,d)};playTrack(5)}""")
    p.wait_for_timeout(1200)
    r = p.evaluate("()=>({o:__fb.get('onair'),want:tracks.piano[5].title})")
    c.ok(r["o"]["title"] == r["want"] and r["o"]["playing"] is True, "Starší pravidla bez playlistu: ON AIR funguje dál (jen bez playlistu)")
    p.evaluate("()=>{Object.getPrototypeOf(firebase.database().ref('x')).update=window.__oUpd}")
    # výpadek spojení a návrat
    p.evaluate("()=>{__fb.setConnected(false);__fb.set('onair',Object.assign({},__fb.get('onair'),{playing:false}));__fb.setConnected(true)}")
    p.wait_for_timeout(600)
    c.ok(p.evaluate("__fb.get('onair').playing") is True, "Po výpadku sítě ON AIR hned zase svítí")
    p.evaluate("adminLogout()")
    p.wait_for_timeout(700)
    r = p.evaluate("()=>({o:__fb.get('onair'),u:__fb.user(),hid:document.getElementById('onair-admin').hidden})")
    c.ok(r["o"]["playing"] is False and r["u"] is None and r["hid"], "Odhlášení: ON AIR nejdřív zhasne, pak se odhlásí; pole pro název zmizí")
    p.evaluate("()=>playTrack(6)")
    p.wait_for_timeout(1000)
    c.ok(p.evaluate("__fb.get('onair').playing") is False, "Po odhlášení se už nic nezapisuje")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))
    # druhé zařízení: přihlášený admin, který nic nepustil, nesmí ON AIR přepsat
    ctx2, q, errs2 = fb_page(s)
    q.evaluate("()=>__fb.set('onair',{title:'Z jiného zařízení',playing:true,ts:__fb.now(),own:false})")
    q.evaluate("()=>firebase.auth().signInWithEmailAndPassword('admin@test.cz','dobreheslo')")
    q.wait_for_timeout(900)
    o = q.evaluate("__fb.get('onair')")
    c.ok(o["title"] == "Z jiného zařízení" and o["playing"] is True and "onair" not in q.evaluate("__fb.disconnectPaths()"),
         "Admin na druhém zařízení (nic nehraje) ON AIR z prvního nezhasne ani nepřepíše")
    q.evaluate("adminLogout()")
    q.wait_for_timeout(500)
    c.ok(q.evaluate("__fb.get('onair').playing") is True and not errs2, "Ani jeho odhlášení ON AIR nezhasne")


def t_onair_viewer(s, c):
    """ON AIR pro návštěvníky: svícení, okno s onair.html, Esc, jazyk, bez internetu; stránka onair.html sama."""
    ctx, p, errs = fb_page(s)
    if not onair_ready(p, c):
        return
    set_lang(p, "en")
    head = "()=>{const r=document.querySelector('.brand-wordmark').getBoundingClientRect();return [Math.round(r.top),Math.round(r.height)]}"
    h0 = p.evaluate(head)
    lv0 = p.evaluate(ONAIR_LIVE_JS)
    p.evaluate("()=>__fb.set('onair',{title:'SYNTHLUCIDA - Polaris',playing:true,ts:__fb.now(),since:__fb.now()-60000,own:false,list:'piano'})")
    p.wait_for_timeout(300)
    lv1 = p.evaluate(ONAIR_LIVE_JS)
    c.ok(not any(lv0.values()) and all(lv1.values()), "Zhasnuto → svítí, když Dave hraje (EXPLORE, tečka, nápis u SYNTHLUCIDA)")
    c.ok(p.evaluate(head) == h0, "Nápis ON AIR u SYNTHLUCIDA neposune nadpis")
    p.evaluate("()=>__fb.set('onair',{title:'SYNTHLUCIDA - Polaris',playing:true,ts:__fb.now()-5*60000,own:false})")
    p.wait_for_timeout(300)
    c.ok(not any(p.evaluate(ONAIR_LIVE_JS).values()), "Bez zprávy déle než 4 min ON AIR zhasne (zavřený player)")
    p.evaluate("()=>__fb.set('onair',{title:'SYNTHLUCIDA - Polaris',playing:true,ts:__fb.now(),own:false,list:'piano'})")
    p.evaluate("()=>{const t=document.getElementById('atmos-toggle-explore');if(t)t.click()}")
    p.wait_for_timeout(300)
    click_vis(p, "#btn-onair")
    p.wait_for_timeout(1500)
    src = p.evaluate("document.getElementById('onair-frame').getAttribute('src')") or ""
    c.ok(not p.evaluate("document.getElementById('onair-window').hidden") and "onair.html?embed=1&lang=en" in src,
         "Klepnutí na ON AIR otevře okno se stránkou onair.html")
    c.ok(p.evaluate("document.getElementById('onair-admin').hidden"), "Návštěvník nevidí úpravu názvu")
    if s.env.onair_src is None:
        c.ok(False, "Vedle player.html chybí onair.html – okno ON AIR by bylo prázdné")
    else:
        fr = p.frame(url=re.compile(r"onair\.html"))
        ok = False
        if fr:
            try:
                ok = fr.evaluate("document.body.classList.contains('embed')")
            except Exception:
                ok = False
        c.ok(ok, "V okně je onair.html ve zjednodušené podobě (bez hlavičky)")
        if fr:
            try:
                fr.click("body", position={"x": 20, "y": 20})
            except Exception:
                pass
    p.keyboard.press("Escape")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("document.getElementById('onair-window').hidden"), "Esc okno zavře (i po klepnutí dovnitř)")
    p.evaluate("openOnAir()")
    p.wait_for_timeout(300)
    set_lang(p, "cz")
    c.ok("lang=cz" in (p.evaluate("document.getElementById('onair-frame').getAttribute('src')") or "")
         and "Dave právě poslouchá" in p.evaluate("document.getElementById('btn-onair').title"),
         "Přepnutí jazyka: popisek i otevřené okno hned v novém jazyce")
    p.evaluate("closeOnAir()")
    ctx.set_offline(True)
    p.wait_for_timeout(200)
    p.evaluate("()=>{document.getElementById('onair-frame').removeAttribute('src');openOnAir()}")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("document.getElementById('onair-window').hidden") and "internet" in p.evaluate(TOAST_JS),
         "Bez internetu se okno neotevře, jen se ukáže hláška")
    ctx.set_offline(False)
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))
    if s.env.onair_src is None:
        return
    # samotná stránka onair.html
    for lang, txt in (("cz", {"live": "právě online", "from": "z playlistu PIANO", "last": "NAPOSLEDY HRÁLO", "yt": "Hledat na YouTube"}),
                      ("en", {"live": "online right now", "from": "from the PIANO playlist", "last": "LAST PLAYED", "yt": "Search on YouTube"})):
        ctx2, q, e2 = fb_page(s)
        q.goto(s.env.url.replace("player.html", "onair.html?lang=" + lang))
        q.wait_for_timeout(800)
        off = q.text_content("#status")
        q.evaluate("()=>__fb.set('onair',{title:'SYNTHLUCIDA - Polaris',playing:true,ts:__fb.now(),since:__fb.now()-60000,own:false,list:'piano'})")
        q.wait_for_timeout(400)
        r = q.evaluate("()=>({st:document.getElementById('status').textContent,t:document.getElementById('title').textContent,from:document.getElementById('from').textContent,yt:document.getElementById('yt').href,ytt:document.getElementById('yt-text').textContent})")
        c.ok(txt["live"] in r["st"] and r["t"] == "Polaris" and r["from"] == txt["from"] and r["ytt"] == txt["yt"]
             and r["yt"] == "https://www.youtube.com/results?search_query=SYNTHLUCIDA%20-%20Polaris",
             "onair.html (%s): Dave hraje – název, playlist a hledání na YouTube" % lang)
        q.evaluate("()=>__fb.set('onair',{title:'<img src=x onerror=window.__x=1> Song - www.mp3clan.com',playing:false,ts:__fb.now()-30*60000,own:true,list:'piano'})")
        q.wait_for_timeout(400)
        r = q.evaluate("()=>({lab:document.getElementById('label').textContent,from:document.getElementById('from').hidden,t:document.getElementById('title').textContent,img:document.querySelectorAll('#card img').length,x:window.__x})")
        c.ok(r["lab"] == txt["last"] and r["from"] and "mp3clan" not in r["t"].lower() and r["img"] == 0 and not r["x"],
             "onair.html (%s): naposledy hrálo, vlastní soubor bez playlistu, bez mp3clan, HTML se nespustí" % lang)
        ov = q.evaluate(OVERFLOW_JS)
        c.ok(off and not ov and not e2, "onair.html (%s): na telefonu nic nepřetéká, bez chyb" % lang + (" " + str(ov or e2) if (ov or e2) else ""))


# --------------------------------------------------------------------------- #
#  Testy z 28. 11. 2026: FILE MANAGER, chytrý shuffle, záloha nastavení, F (záloha, okénko),
#  plynulé prolnutí, notička u fronty, stažení za XP s DONATE
# --------------------------------------------------------------------------- #
FM_READY = "()=>{const f=document.getElementById('fm-frame');try{return !!(f&&f.contentWindow&&typeof f.contentWindow.fmRefresh==='function'&&f.contentDocument.getElementById('stat').textContent)}catch(e){return false}}"


def _wavf(name, sec=3):
    return {"name": name, "mimeType": "audio/wav", "buffer": make_wav(sec, 8000)}


def _upload_f(p, files):
    """Nahraje soubory přes sekci F a počká, až je hotovo."""
    p.set_input_files("#myfiles-input", files)
    p.wait_for_function("!document.getElementById('myfiles-add-btn').disabled&&!myFilesUploadBusy", timeout=30000)
    p.wait_for_timeout(400)


def _dialogs(p):
    """Zapisuje texty oken alert/confirm (potvrzuje je Session.page)."""
    msgs = []
    p.on("dialog", lambda d: msgs.append(d.message))
    return msgs


def t_file_manager(s, c):
    """FILE MANAGER (manager.html): jen na PC, soubory/složky/playlisty/My Playlist, obousměrná synchronizace, návod, CZ/EN, Esc."""
    if s.env.manager_src is None:
        c.ok(False, "Vedle player.html chybí manager.html (FILE MANAGER) – nahraj ho do stejné složky")
        return
    ctx, p, errs = s.page()  # telefon
    if not p.evaluate("typeof openFileManager==='function'&&!!window.SLFM&&!!document.getElementById('btn-fm')"):
        c.ok(False, "Player nemá FILE MANAGER (je to aktuální player.html?)")
        return
    r = p.evaluate("()=>{openFileManager();const o=document.getElementById('fm-overlay');return {vis:getComputedStyle(document.getElementById('btn-fm')).display!=='none',open:!!(o&&!o.hidden)}}")
    c.ok(not r["vis"] and not r["open"], "Na telefonu je FILE MANAGER v EXPLORE skrytý a nejde otevřít")
    ctx.close()

    ctx, p, errs2 = s.page(w=1366, h=860)
    c.ok(p.evaluate("getComputedStyle(document.getElementById('btn-fm')).display!=='none'"), "Na počítači je FILE MANAGER v EXPLORE vidět")
    set_lang(p, "cz")
    p.evaluate("openFileManager()")
    try:
        p.wait_for_function(FM_READY, timeout=15000)
        ready = True
    except Exception:
        ready = False
    c.ok(ready, "Okno FILE MANAGER se otevře a načte manager.html")
    if not ready:
        return
    fr = p.frame_locator("#fm-frame")
    fr.locator("#add-input").set_input_files([_wavf("Fm Alfa.wav"), _wavf("Fm Beta.wav", 4), _wavf("Fm Gama.wav", 5)])
    p.wait_for_function("()=>myFilesLibrary.length===3&&!myFilesUploadBusy", timeout=30000)
    p.wait_for_timeout(700)
    c.ok(fr.locator("#mid-body .row").count() >= 3 and p.evaluate("myFilesLibrary.length") == 3,
         "Soubory přidané v manažeru jsou v sekci F i v manažeru")
    r = p.evaluate("""()=>{const id=n=>myFilesLibrary.find(x=>x.name===n).id;
      const fo=SLFM.createFolder('Spaní');SLFM.moveFiles([id('Fm Alfa'),id('Fm Beta')],fo.id);
      const pl=SLFM.createPlaylist('Večer');
      SLFM.addToPlaylist(pl.id,[{k:'f',id:id('Fm Gama')},{k:'t',pl:'piano',i:0},{k:'f',id:id('Fm Alfa')}]);
      const again=SLFM.addToPlaylist(pl.id,[{k:'f',id:id('Fm Gama')}]);
      SLFM.reorderPlaylist(pl.id,[2,0,1]);
      const P=myPlaylists.find(x=>x.id===pl.id);
      return {folder:myFolders.some(f=>f.name==='Spaní')&&myFilesLibrary.filter(x=>myFolderMap[x.id]===fo.id).length,
        items:P.items.map(i=>i.type==='file'?(myFilesLibrary.find(x=>x.id===i.fileId)||{}).name:'T').join(','),again:again.added,id:pl.id}}""")
    c.ok(r["folder"] == 2, "Složka z manažeru je v sekci F a soubory jsou v ní")
    c.ok(r["items"] == "Fm Alfa,Fm Gama,T" and r["again"] == 0,
         "Playlist z manažeru: soubory i SYNTHLUCIDA skladby, nic dvakrát, změna pořadí se uloží (%s)" % r["items"])
    q = p.evaluate("""async()=>{customQueue.length=0;saveCustomQueue();const id=n=>myFilesLibrary.find(x=>x.name===n).id;
      SLFM.moodCreate('Klid');const a=await SLFM.queueAdd([{k:'f',id:id('Fm Beta')},{k:'t',pl:'chill',i:1}]);
      const b=await SLFM.queueAdd([{k:'t',pl:'chill',i:1}],'Klid');
      return {n:customQueue.length,mood:customQueue.filter(e=>e.mood==='Klid').length,a:a.added,saved:JSON.parse(localStorage.getItem('synthlucida_custom_queue')||'[]').length}}""")
    c.ok(q["n"] == 3 and q["mood"] == 1 and q["saved"] == 3, "My Playlist z manažeru: přidání, sekce, uloží se (%s)" % q)
    # player -> manažer
    p.evaluate("()=>{myPlaylists.push({id:'fmzz',name:'Z playeru',items:[],createdAt:1});saveMyPlaylistsToStorage();renderMyFiles()}")
    try:
        fr.locator("#tgt-select option", has_text="Z playeru").first.wait_for(state="attached", timeout=4000)
        synced = True
    except Exception:
        synced = False
    c.ok(synced, "Změna v playeru se sama objeví v manažeru")
    st = fr.locator("#stat").text_content()
    c.ok("3" in st and "My Playlist 3" in st, "Horní lišta manažeru ukazuje počty (%s)" % st)
    # návod + jazyk
    fr.locator("#btn-help").click()
    p.wait_for_timeout(250)
    h = fr.locator(".help h5").count()
    c.ok(fr.locator(".help").is_visible() and h >= 6, "? Návod otevře návod (%d částí)" % h)
    fr.locator(".help").press("Escape")
    p.wait_for_timeout(250)
    c.ok(fr.locator(".help").count() == 0 and p.evaluate("!document.getElementById('fm-overlay').hidden"),
         "Esc zavře jen návod, manažer zůstane otevřený")
    fr.locator("#btn-lang").click()
    p.wait_for_timeout(800)
    c.ok(p.evaluate("guideLang") == "en" and "Add audio" in fr.locator("#btn-add").text_content(),
         "Tlačítko CZ/EN přepne jazyk manažeru i playeru")
    src0 = p.evaluate("document.getElementById('fm-frame').getAttribute('src')")
    p.keyboard.press("Escape")
    p.wait_for_timeout(300)
    closed = p.evaluate("document.getElementById('fm-overlay').hidden")
    p.evaluate("openFileManager()")
    p.wait_for_timeout(500)
    c.ok(closed and p.evaluate("document.getElementById('fm-frame').getAttribute('src')") == src0,
         "Esc zavře manažer; znovu otevřený se nenačítá od začátku")
    # smazání souboru z manažeru = zmizí i z F, playlistů a My Playlist
    r = p.evaluate("""async()=>{const id=myFilesLibrary.find(x=>x.name==='Fm Beta').id;await SLFM.deleteFiles([id]);
      return {lib:myFilesLibrary.some(x=>x.name==='Fm Beta'),q:customQueue.some(e=>e.fileId===id)}}""")
    c.ok(not r["lib"] and not r["q"], "Smazání souboru v manažeru ho odstraní i z F a My Playlist")
    allerr = errs + errs2
    c.ok(not allerr, "Bez chyby ve stránce" + (": " + allerr[0] if allerr else ""))


def t_smart_shuffle(s, c):
    """Chytrý shuffle: paměť 50 skladeb z HISTORY, podle playlistu, oblíbené o trochu častěji, kostka, mini přehrávač."""
    hist = [{"title": "T%d" % (i % 60), "playlist": "piano", "index": 0, "time": 1700000000000 - i * 1000} for i in range(70)]
    init = "try{if(!sessionStorage.getItem('h')){sessionStorage.setItem('h','1');localStorage.setItem('synthlucida_history',%s)}}catch(e){}" % json.dumps(json.dumps(hist))
    ctx, p, errs = s.page(init=init)
    if not p.evaluate("typeof weightedShufflePick==='function'&&typeof countShufflePlay==='function'"):
        c.ok(False, "Player nemá chytrý shuffle (je to aktuální player.html?)")
        return
    r = p.evaluate("()=>({n:recentTitles.length,last:recentTitles[recentTitles.length-1],u:new Set(recentTitles).size})")
    c.ok(r["n"] == 50 and r["u"] == 50 and r["last"] == "T0", "Po startu si pamatuje posledních 50 skladeb z HISTORY (%s)" % r)
    r = p.evaluate("""()=>{const list=[];for(let i=0;i<20;i++)list.push({title:'L'+i});recentTitles=[];for(let i=0;i<14;i++)rememberRecentTrack('L'+i);
      const seen={};for(let k=0;k<3000;k++){const x=randomTrackIndex(list,19);seen[x]=1;}return Object.keys(seen).map(Number).sort((a,b)=>a-b)}""")
    c.ok(19 not in r and all(k < 4 or k >= 14 for k in r) and len(r) == 9,
         "20 skladeb: nevybere hrající ani 10 naposledy hraných z playlistu (%s)" % r)
    r = p.evaluate("""()=>{const list=[];for(let i=0;i<10;i++)list.push({title:'Q'+i});recentTitles=['X1','X2','X3','X4','X5','X6','Q1'];
      const seen={};for(let k=0;k<2000;k++){seen[randomTrackIndex(list,0)]=1;}return Object.keys(seen).length}""")
    c.ok(r == 8, "Skladby z jiných playlistů nevadí - vynechá jen nedávné z tohoto playlistu (%s možností z 9)" % r)
    r = p.evaluate("""()=>{const list=[];for(let i=0;i<5;i++)list.push({title:'W'+i});localStorage.removeItem('sl-shuffle-plays');shufflePlays=null;
      for(let k=0;k<40;k++)countShufflePlay('W0');const n=[0,0,0,0,0];for(let k=0;k<60000;k++)n[weightedShufflePick(list,[0,1,2,3,4])]++;
      return {ratio:n[0]/((n[1]+n[2]+n[3]+n[4])/4),saved:JSON.parse(localStorage.getItem('sl-shuffle-plays')).W0}}""")
    c.ok(1.35 < r["ratio"] < 1.65 and r["saved"] == 40, "Často poslouchaná skladba hraje jen o trochu častěji (%.2f×) a počet se uloží" % r["ratio"])
    r = p.evaluate("""()=>{localStorage.removeItem('sl-shuffle-plays');shufflePlays=null;currentTrackTitle='ZZ';pendingTrackXPAwarded=false;pendingTrackXPListenSeconds=0;
      addListenSeconds(10);const a=shufflePlayCount('ZZ');addListenSeconds(15);const b=shufflePlayCount('ZZ');addListenSeconds(30);return [a,b,shufflePlayCount('ZZ')]}""")
    c.ok(r == [0, 1, 1], "Poslech se započítá až po 20 s a jen jednou za skladbu (%s)" % r)
    r = p.evaluate("""()=>{localStorage.setItem('sl-shuffle-plays','{"__proto__":{"x":1},"a":"5","b":-3,"c":"abc"}');shufflePlays=null;
      countShufflePlay('__proto__');return [shufflePlayCount('a'),shufflePlayCount('b'),shufflePlayCount('c'),({}).x===undefined]}""")
    c.ok(r == [5, 0, 0, True], "Poškozené počty poslechů se bezpečně ignorují")
    r = p.evaluate("""()=>{localStorage.removeItem('sl-shuffle-plays');shufflePlays=null;const m={};for(let i=0;i<700;i++)m['k'+i]=3;
      localStorage.setItem('sl-shuffle-plays',JSON.stringify(m));shufflePlays=null;countShufflePlay('nova');const o=JSON.parse(localStorage.getItem('sl-shuffle-plays'));
      return {n:Object.keys(o).length,sum:Object.values(o).reduce((a,b)=>a+b,0)}}""")
    c.ok(r["n"] <= 600 and r["sum"] <= 3000, "Počty poslechů nerostou donekonečna (%s)" % r)
    p.evaluate("()=>{localStorage.removeItem('sl-shuffle-plays');shufflePlays=null;recentTitles=[];setCrossfade(0,null)}")
    picks = []
    for _ in range(6):
        p.evaluate("playRandomMix()")
        p.wait_for_timeout(300)
        picks.append(p.evaluate("currentPlaylistKey+':'+currentTrackIndex+':'+shuffleEnabled"))
    c.ok(len(set(picks)) == 6 and all(re.match(r"^(piano|chill):\d+:true$", x) for x in picks),
         "Kostka 6× po sobě: pokaždé jiná skladba z PIANO/CHILL a zapne shuffle (%s)" % ", ".join(picks))
    r = p.evaluate("()=>{const s=[];for(let k=0;k<40;k++)s.push(randomTrackIndex(tracks.asmr,-1));return s.every(i=>i>=0&&i<tracks.asmr.length&&!isSpecialTrack(tracks.asmr[i]))}")
    c.ok(r, "Start playlistu při zapnutém shuffle nevybere skladbu SPECIAL")
    r = p.evaluate("""async()=>{customQueue.length=0;tracks.piano.slice(0,6).forEach((t,i)=>customQueue.push({title:t.title,src:t.src,playlist:'piano',index:i}));saveCustomQueue();
      customQueueActive=false;slPlayback.selectList('queue');await new Promise(r=>setTimeout(r,400));const st=new Set();for(let k=0;k<40;k++)st.add(queueRandomStartPos());
      return {a:customQueueActive,n:st.size}}""")
    c.ok(r["a"] and r["n"] >= 3, "Mini přehrávač: MY PLAYLIST při shuffle začne náhodnou skladbou (%s)" % r)
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_settings_backup(s, c):
    """SETUP → ZÁLOHA NASTAVENÍ: export, obnova po smazání dat, škodlivý soubor, špatné soubory."""
    init = "try{if(!sessionStorage.getItem('seed')){sessionStorage.setItem('seed','1');localStorage.setItem('synthlucida_guide_lang','cz')}}catch(e){}"
    ctx, p, errs = s.page(init=init)
    msgs = _dialogs(p)
    if not p.evaluate("!!window.slSettingsBackup&&!!document.getElementById('su-set-export')"):
        c.ok(False, "Player nemá zálohu nastavení v SETUP (je to aktuální player.html?)")
        return
    p.evaluate("""()=>{localStorage.setItem('synthlucida_opt_blur','1');localStorage.setItem('sl-orbs-style','disco');
      customQueue.length=0;customQueue.push({title:'A',src:tracks.piano[0].src,playlist:'piano',index:0},{title:'Mine',src:'blob:x',fileId:'f1'});saveCustomQueue();
      localStorage.setItem('synthlucida_history',JSON.stringify([{title:'A',playlist:'piano'},{title:'Mine',playlist:'custom:file:f1',fileId:'f1'}]));
      localStorage.setItem('synthlucida_custom_playlists','[{"id":"1","name":"x","items":[]}]');localStorage.setItem('sl-onair-names','{"a":"b"}');toggleSetup()}""")
    p.wait_for_timeout(300)
    with p.expect_download() as dl:
        p.click("#su-set-export")
    path = os.path.join(tempfile.gettempdir(), "testplayeru_nastaveni.json")
    dl.value.save_as(path)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    d = data.get("data", {})
    q = json.loads(d.get("synthlucida_custom_queue", "[]"))
    c.ok(data.get("kind") == "synthlucida-player-settings" and d.get("synthlucida_opt_blur") == "1" and d.get("sl-orbs-style") == "disco",
         "Export uloží nastavení playeru (%d položek)" % len(d))
    c.ok(len(q) == 1 and len(json.loads(d.get("synthlucida_history", "[]"))) == 1 and "synthlucida_custom_playlists" not in d
         and "sl-onair-names" not in d and "sl-app-version-seen" not in d,
         "V exportu nejsou soubory z F, admin věci ani číslo verze")
    p.evaluate("()=>{toggleSetup();localStorage.clear()}")
    p.reload()
    p.wait_for_timeout(1500)
    before = p.evaluate("localStorage.getItem('sl-orbs-style')")
    p.evaluate("toggleSetup()")
    p.wait_for_timeout(200)
    with p.expect_navigation(timeout=15000):
        p.set_input_files("#su-set-file", path)
    p.wait_for_timeout(1500)
    r = p.evaluate("()=>[localStorage.getItem('sl-orbs-style'),localStorage.getItem('synthlucida_opt_blur'),customQueue.length]")
    c.ok(before is None and r == ["disco", "1", 1] and any("Load settings" in m or "Nahrát nastavení" in m for m in msgs),
         "Po smazání dat import vrátí nastavení (s potvrzením a novým načtením playeru)")
    evil = {"kind": "synthlucida-player-settings", "v": 1, "data": {
        "evil-key": "x", "sl-orbs-style": "<img src=x onerror=window.__pwned=1>disco",
        "synthlucida_settings": json.dumps({"theme": "javascript:window.__pwned=2", "a": "<script>x</script>", "__proto__": {"polluted": 1}, "deep": {"u": "url(https://evil.example)"}}),
        "synthlucida_history": 123}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(evil, fh)
    p.evaluate("toggleSetup()")
    p.wait_for_timeout(200)
    with p.expect_navigation(timeout=15000):
        p.set_input_files("#su-set-file", path)
    p.wait_for_timeout(1500)
    r = p.evaluate("()=>({evil:localStorage.getItem('evil-key'),orbs:localStorage.getItem('sl-orbs-style'),set:localStorage.getItem('synthlucida_settings'),pw:window.__pwned||0,pol:({}).polluted,img:document.querySelectorAll('img[src=\"x\"]').length})")
    c.ok(r["evil"] is None and "<" not in r["orbs"] and not re.search(r"javascript:|<|url\(|polluted", r["set"] or "") and not r["pw"] and r["pol"] is None and not r["img"],
         "Podstrčený kód ani cizí klíče se z importu nepoužijí")
    for obj, pat, name in (({"kind": "synthlucida-myfiles-backup"}, r"sekce F|section F", "záloha z F"), ({"type": "my-playlist", "tracks": []}, r"MY PLAYLIST", "export My Playlist"),
                           ({"hello": 1}, r"není záloha nastavení|not a SYNTHLUCIDA player settings", "jiný soubor")):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        del msgs[:]
        p.evaluate("()=>{const m=document.getElementById('setup-window');if(m.hidden)toggleSetup()}")
        p.set_input_files("#su-set-file", path)
        p.wait_for_timeout(1800)
        c.ok(any(re.search(pat, m) for m in msgs), "Špatný soubor (%s) → srozumitelná hláška, nic se nezmění" % name)
    try:
        os.remove(path)
    except Exception:
        pass
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_own_busy_backup(s, c):
    """Sekce F: okénko s kolečkem při přidávání více souborů, poznámka o cloudu, záloha s BOM / omylem vybraný jiný soubor."""
    ctx, p, errs = s.page()
    msgs = _dialogs(p)
    if not p.evaluate("!!document.getElementById('mf-busy')&&typeof readBackupFileText==='function'"):
        c.ok(False, "Player nemá okénko pro přidávání souborů (je to aktuální player.html?)")
        return
    p.evaluate("toggleMainFiles()")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("!!document.querySelector('.myfiles-cloud-note')&&document.querySelector('.myfiles-cloud-note').getBoundingClientRect().height>0"),
         "V sekci F je poznámka, že soubory z cloudu se načítají déle")
    p.evaluate("""()=>{window.__busy=0;new MutationObserver(()=>{if(!document.getElementById('mf-busy').hidden)window.__busy++}).observe(document.getElementById('mf-busy'),{attributes:true})}""")
    _upload_f(p, [_wavf("Busy %d.wav" % i, 2 + i) for i in range(4)])
    p.wait_for_timeout(3000)
    r = p.evaluate("()=>({seen:window.__busy,hidden:document.getElementById('mf-busy').hidden,n:myFilesLibrary.length})")
    c.ok(r["seen"] > 0 and r["hidden"] and r["n"] == 4, "Při přidávání 4 souborů se ukáže okénko s kolečkem a pak samo zmizí")
    # záloha F uložená s BOM a koncem řádků Windows
    with p.expect_download() as dl:
        p.evaluate("exportMyFilesBackup()")
    path = os.path.join(tempfile.gettempdir(), "testplayeru_bom.json")
    dl.value.save_as(path)
    with open(path, "r", encoding="utf-8") as fh:
        txt = fh.read()
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("﻿" + txt.replace("\n", "\r\n"))
    del msgs[:]
    p.set_input_files("#myfiles-backup-input", path)
    p.wait_for_timeout(1500)
    c.ok(any("Obnovit z této" in m or "Restore from this" in m for m in msgs) and not any("není záloha" in m or "is not a SYNTHLUCIDA" in m for m in msgs),
         "Záloha F upravená v Poznámkovém bloku (BOM, konce řádků Windows) jde obnovit")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"app": "SYNTHLUCIDA", "type": "my-playlist", "tracks": [{"title": "a", "src": "x"}]}, fh)
    del msgs[:]
    p.set_input_files("#myfiles-backup-input", path)
    p.wait_for_timeout(1500)
    c.ok(any("MY PLAYLIST" in m for m in msgs), "Omylem vybraný export My Playlist v F: řekne, kde ho obnovit")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"kind": "synthlucida-myfiles-backup", "folders": [], "files": [], "playlists": []}, fh)
    del msgs[:]
    p.set_input_files("#queue-import-input", path)
    p.wait_for_timeout(1500)
    c.ok(any("sekce F" in m or "from F" in m for m in msgs), "Omylem vybraná záloha F v importu My Playlist: řekne, kde ji obnovit")
    r = p.evaluate("""async()=>{let n=0;const real=new File(['{"a":1}'],'c.json');const f={name:'c.json',size:7,arrayBuffer:async()=>{if(n++<2)throw new DOMException('x','NotReadableError');return real.arrayBuffer()}};
      const t=await readBackupFileText(f);let m=0;const g={name:'d.json',size:7,arrayBuffer:async()=>{m++;throw new DOMException('x','NotReadableError')}};const u=await readBackupFileText(g);return {t,u,m}}""")
    c.ok(r["t"] == '{"a":1}' and r["u"] is None and r["m"] == 4, "Soubor z cloudu, který se napoprvé nepřečte, se zkusí načíst znovu")
    try:
        os.remove(path)
    except Exception:
        pass
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_crossfade_smooth(s, c):
    """Prolnutí: na konci se nová skladba předá do hlavního přehrávače bez zaškobrtnutí (obě kopie do pár ms, bez ztlumení)."""
    ctx, p, errs = s.page()
    if not p.evaluate("typeof CF_QUIET!=='undefined'"):
        c.ok(False, "Player nemá plynulé předání při prolnutí (je to aktuální player.html?)")
        return
    p.evaluate("()=>{const B=%s;setCrossfade(3,null);shuffleEnabled=false;repeatEnabled=false;setPlaylist('piano',B('piano'));playTrack(0)}" % BTN_JS)
    p.wait_for_timeout(1500)
    res = p.evaluate("""async()=>{const cf=crossfadeAudio,w=ms=>new Promise(r=>setTimeout(r,ms)),out=[];
      for(let n=0;n<3;n++){for(let i=0;i<100&&!(audio.duration>0);i++)await w(50);
        audio.currentTime=audio.duration-5;await w(200);
        let worst=0,samples=0,hand=0,muted=false,zero=false;const t0=Date.now();
        const iv=setInterval(()=>{if(cfHandoff)hand=1;if(hand&&crossfadeActive){if(audio.muted&&!soundMuted)muted=true;if(!audio.paused&&audio.volume===0)zero=true;}
          if(!audio.paused&&!cf.paused&&cf.src===audio.src&&audio.volume>0.002&&cf.volume>0.002){samples++;worst=Math.max(worst,Math.abs(audio.currentTime-cf.currentTime));}},2);
        while(Date.now()-t0<15000&&!(hand&&!crossfadeActive))await w(20);
        clearInterval(iv);out.push({ms:Math.round(worst*1000),samples,ok:hand&&!crossfadeActive&&!audio.paused,muted,zero});await w(1200);}
      return out}""")
    ms = sorted(x["ms"] for x in res)
    c.ok(all(x["ok"] for x in res), "3× prolnutí: nová skladba pokaždé převzata a hraje dál")
    c.ok(ms[1] <= 12, "Při předání hrají obě kopie skladby skoro přesně spolu (%s ms, dřív 30-80 ms)" % ", ".join(str(m) for m in [x["ms"] for x in res]))
    c.ok(not any(x["muted"] or x["zero"] for x in res), "Při předání se hlavní přehrávač neztlumí úplně (jinak se Chrome na chvilku zasekne)")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_pq_note_own_file(s, c):
    """Fronta přehrávání + vlastní soubor: točící se notička ukáže skladbu v normálním seznamu (ne seznam s jedinou skladbou)."""
    ctx, p, errs = s.page()
    if not p.evaluate("typeof pqOwnFileHome==='function'"):
        c.ok(False, "Player nemá opravenou notičku u fronty (je to aktuální player.html?)")
        return
    p.evaluate("toggleMainFiles()")
    _upload_f(p, [_wavf("Pq A.wav", 20), _wavf("Pq B.wav", 20), _wavf("Pq C.wav", 20)])
    p.evaluate("""async()=>{const id=n=>myFilesLibrary.find(x=>x.name===n).id;setCrossfade(0,null);shuffleEnabled=false;repeatEnabled=false;
      myPlaylists.push({id:'pqf',name:'Noc',createdAt:1,items:[{type:'file',fileId:id('Pq A'),title:'Pq A'},
        {type:'track',playlist:'chill',index:0,title:tracks.chill[0].title,src:tracks.chill[0].src},{type:'file',fileId:id('Pq B'),title:'Pq B'}]});
      saveMyPlaylistsToStorage();await playMyPlaylist('pqf');
      pqToggleEntry({t:'file',fileId:id('Pq B'),title:'Pq B'});pqToggleEntry({t:'file',fileId:id('Pq C'),title:'Pq C'})}""")
    p.wait_for_timeout(1000)
    near_end(p)
    wait_next(p)
    c.ok(p.evaluate("currentTrackTitle") == "Pq B" and not p.evaluate("document.getElementById('btn-back-queue').disabled"),
         "Z fronty hraje vlastní soubor a notička je aktivní")
    p.evaluate("backToMyPlaylist()")
    p.wait_for_timeout(700)
    r = p.evaluate("()=>({key:currentPlaylistKey,rows:document.querySelectorAll('#playlist li').length,act:(document.querySelector('#playlist li.active')||{textContent:''}).textContent})")
    c.ok(r["key"] == "custom:pqf" and r["rows"] == 3 and "Pq B" in r["act"],
         "Notička: soubor z playlistu F se ukáže v celém playlistu a je označený (%s)" % r)
    near_end(p)
    wait_next(p)
    p.evaluate("()=>{const x=document.getElementById('main-files');if(x.classList.contains('visible'))toggleMainFiles()}")
    p.evaluate("backToMyPlaylist()")
    p.wait_for_timeout(700)
    r = p.evaluate("()=>({t:currentTrackTitle,key:currentPlaylistKey,rows:document.querySelectorAll('#playlist li').length,f:document.getElementById('main-files').classList.contains('visible')})")
    c.ok(r["t"] == "Pq C" and r["key"] == "custom:pqf" and r["rows"] == 3 and r["f"],
         "Soubor, který není v žádném seznamu: okno zůstane, jak je, a otevře se sekce F (%s)" % r)
    near_end(p)
    wait_next(p)
    c.ok(p.evaluate("playingRef&&playingRef.key") == "custom:pqf", "Po frontě hraje dál playlist z F")
    c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))


def t_xp_download_window(s, c):
    """Stažení za XP (MY PLAYLIST, ASMR/PIANO/CHILL): okno s potvrzením a DONATE (odkaz z tlačítka ☕), XP až po potvrzení."""
    for w, lang in ((1280, "cz"), (360, "en")):
        ctx, p, errs = s.page(w=w, h=800)
        if not p.evaluate("!!document.getElementById('xpdl-window')&&typeof openXpDownloadWindow==='function'"):
            c.ok(False, "Player nemá okno pro stažení za XP (je to aktuální player.html?)")
            return
        set_lang(p, lang)
        p.evaluate("toggleMainFiles()")
        _upload_f(p, [_wavf("Xp Moje.wav")])
        p.evaluate("""async()=>{const x=document.getElementById('main-files');if(x.classList.contains('visible'))toggleMainFiles();
          stats.xpSpent=computeXP(stats)-DOWNLOAD_XP_STEP*3;saveStats();customQueue.length=0;
          [['piano',0],['chill',1]].forEach(([k,i])=>customQueue.push({title:tracks[k][i].title,src:tracks[k][i].src,playlist:k,index:i}));
          const f=myFilesLibrary[0];const url=await getMyFileBlobUrl(f.id);customQueue.push({title:f.name,src:url,playlist:'custom:file:'+f.id,index:0,fileId:f.id});
          saveCustomQueue();if(!document.getElementById('queue-panel').classList.contains('visible'))toggleCustomQueue();renderCustomQueue()}""")
        p.wait_for_timeout(300)
        DL = "(i)=>{const li=document.querySelectorAll('#queue-list > li')[i];Array.from(li.querySelectorAll('button')).find(b=>/⬇|🔒/.test(b.textContent)).click()}"
        xp0 = p.evaluate("computeAvailableXP(stats)")
        p.evaluate(DL, 0)
        p.wait_for_timeout(300)
        r = p.evaluate("()=>({vis:!document.getElementById('xpdl-window').hidden,don:document.getElementById('xpdl-donate').getAttribute('href'),main:document.querySelector('.main-donate-btn').getAttribute('href'),xp:computeAvailableXP(stats),t:document.getElementById('xpdl-text').textContent,go:document.getElementById('xpdl-go').textContent})")
        c.ok(r["vis"] and r["xp"] == xp0 and r["don"] == r["main"],
             "[%d px, %s] ⬇ otevře okno se STÁHNOUT a DONATE (odkaz z ☕), XP se zatím neodečtou" % (w, lang))
        c.ok(("Máte dost XP" in r["t"] and "STÁHNOUT TUTO" in r["go"]) if lang == "cz" else ("enough XP" in r["t"] and "DOWNLOAD THIS" in r["go"]),
             "Texty okna jsou v jazyce playeru (%s)" % lang)
        bad = p.evaluate("()=>{const W=document.documentElement.clientWidth;const o=[];document.querySelectorAll('#xpdl-window *').forEach(e=>{const r=e.getBoundingClientRect();if(r.width&&(r.right>W+0.5||r.left<-0.5))o.push(e.id||e.className)});return o}")
        c.ok(not bad, "Okno se vejde na šířku %d px" % w + (" – přetéká: " + ", ".join(bad[:3]) if bad else ""))
        p.keyboard.press("Escape")
        p.wait_for_timeout(200)
        c.ok(p.evaluate("document.getElementById('xpdl-window').hidden") and p.evaluate("computeAvailableXP(stats)") == xp0, "Esc okno zavře a nic neodečte")
        p.evaluate("document.querySelector('.main-donate-btn').setAttribute('href','https://example.org/jiny-odkaz')")
        p.evaluate(DL, 0)
        p.wait_for_timeout(200)
        c.ok(p.evaluate("document.getElementById('xpdl-donate').getAttribute('href')") == "https://example.org/jiny-odkaz",
             "Změněný odkaz tlačítka ☕ (Audio editor) platí i pro DONATE v okně")
        step = p.evaluate("DOWNLOAD_XP_STEP")
        with p.expect_download(timeout=15000) as dl:
            p.evaluate("()=>{const g=document.getElementById('xpdl-go');g.click();g.click()}")
        p.wait_for_timeout(400)
        r = p.evaluate("()=>({vis:!document.getElementById('xpdl-window').hidden,xp:computeAvailableXP(stats),unl:isDownloadUnlockedKey(stats,customQueue[0].src)})")
        c.ok(not r["vis"] and r["xp"] == xp0 - step and r["unl"] and dl.value.suggested_filename.lower().endswith(".mp3"),
             "STÁHNOUT TUTO SKLADBU stáhne MP3 a odečte XP jen jednou (i při dvojkliku)")
        with p.expect_download(timeout=15000):
            p.evaluate(DL, 0)
        p.wait_for_timeout(200)
        with p.expect_download(timeout=15000):
            p.evaluate(DL, 2)
        p.wait_for_timeout(200)
        c.ok(p.evaluate("document.getElementById('xpdl-window').hidden") and p.evaluate("computeAvailableXP(stats)") == xp0 - step,
             "Už zaplacená skladba i vlastní soubor z F se stáhnou rovnou, bez okna a bez XP")
        p.evaluate("()=>{stats.xpSpent=computeXP(stats);saveStats();renderCustomQueue()}")
        r = p.evaluate("()=>{const li=document.querySelectorAll('#queue-list > li')[1];const b=Array.from(li.querySelectorAll('button')).find(b=>/⬇|🔒/.test(b.textContent));return [b.textContent,b.disabled]}")
        c.ok(r == ["🔒", True], "Bez dost XP zůstává zámek 🔒")
        c.ok(not errs, "Bez chyby ve stránce" + (": " + errs[0] if errs else ""))
        ctx.close()


TESTS = [
    ("Kontrola souborů", t_static),
    ("Načtení playeru a okna", t_load),
    ("Přehrávání, pauza, hlasitost", t_play),
    ("Přepínání playlistů", t_switch),
    ("My Playlist", t_queue),
    ("Sekce v My Playlist", t_sections),
    ("Hledání v playlistu", t_find),
    ("Řazení seznamu", t_sort),
    ("Druhá vrstva ASMR", t_layer),
    ("Stahování offline", t_offline),
    ("Stahování právě hrající skladby", t_playing_download),
    ("Záloha R2 při výpadku GitHubu", t_r2),
    ("Stažení skladby za XP", t_xp_download),
    ("Stažení za XP nerozbije přehrávání", t_xp_no_break),
    ("Admin sekce a XP", t_admin),
    ("iPhone – zašedlé funkce", t_iphone),
    ("Průvodce pro nové návštěvníky", t_tour),
    ("Hláška o nové verzi", t_update),
    ("Skladba SPECIAL", t_special),
    ("Koule na pozadí", t_orbs),
    ("Vlastní soubory (sekce F)", t_own_files),
    ("Vlastní soubory – duplicity, průběh, hromadné mazání", t_own_bulk),
    ("Záloha a obnova seznamů (sekce F)", t_own_backup),
    ("My Playlist – export a import souboru (i z jiného zařízení)", t_queue_import),
    ("Celkový čas playlistu vedle názvu", t_total_time),
    ("Historie poslechu", t_history),
    ("Přehrávání se zhasnutým displejem", t_background),
    ("Zapamatování posledního seznamu (F, My Playlist)", t_last_source),
    ("Víkendová, nedělní a sváteční přání", t_greetings),
    ("Úprava motivačních textů (SETUP)", t_motiv_editor),
    ("ASMR otevřené kvůli vrstvě nepřepne další skladbu", t_asmr_view_next),
    ("Fronta přehrávání (hrát jako další)", t_play_queue),
    ("Fronta a druhá vrstva ASMR", t_queue_layer),
    ("GAIN a prolnutí (časovač, nenačtená skladba)", t_gain_crossfade),
    ("Záloha F – názvy s dvojitou mezerou", t_backup_spaces),
    ("ON AIR – admin zapisuje, co hraje (i úprava názvu)", t_onair_admin),
    ("ON AIR – svícení, okno a stránka onair.html", t_onair_viewer),
    ("Odolnost proti poškozeným datům", t_robust),
    ("Rozložení na malém a velkém displeji", t_layout),
    ("Texty v češtině i angličtině", t_i18n),
    ("Časovač usínání", t_timer),
    ("Opakování a náhodné pořadí", t_repeat),
    ("Aplikace bez internetu", t_offline_app),
    ("Aktualizace sw.js nesmaže stažené skladby", t_sw_update),
    ("Uložené nastavení", t_settings),
    ("Témata, dýchání, binaurální tóny, historie, hledání", t_extras),
    ("Ovládání ze zamčené obrazovky", t_media_session),
    ("Chat bez připojení (Firebase nedostupný)", t_chat_offline),
    ("Chat – odeslání zprávy, limity, zobrazení, reakce", t_chat),
    ("Chat – historie, mazání starých zpráv, upozornění na reakce", t_chat_history),
    ("Chat – přihlášení admina (666)", t_chat_admin),
    ("Společné dýchání bez připojení", t_together_offline),
    ("Společné dýchání – místnost, počet lidí, odchod", t_together),
    ("Společné dýchání – jeden rytmus pro všechny", t_together_sync),
    ("Společné dýchání – srdíčka, pozvánka, připomínka, jazyk", t_together_extras),
    ("Společné dýchání – večerní sezení ve 21:00", t_together_evening),
    ("Společné dýchání – statistiky a XP", t_together_stats),
    ("FILE MANAGER (jen počítač)", t_file_manager),
    ("Chytré náhodné přehrávání", t_smart_shuffle),
    ("Záloha nastavení (SETUP)", t_settings_backup),
    ("Sekce F – okénko při přidávání, čtení zálohy", t_own_busy_backup),
    ("Plynulé předání při prolnutí", t_crossfade_smooth),
    ("Fronta – notička u vlastního souboru", t_pq_note_own_file),
    ("Stažení za XP – okno s DONATE", t_xp_download_window),
    ("Skladby jsou opravdu na GitHubu a R2 (online)", t_online),
]


# --------------------------------------------------------------------------- #
#  Spousteni
# --------------------------------------------------------------------------- #
def launch_browser(pw):
    args = ["--autoplay-policy=no-user-gesture-required",
            # zadny pristup na internet: bez proxy a vsechna jmena krome localhostu se "nenajdou"
            "--no-proxy-server",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1"]
    tries = []
    custom = os.environ.get("TESTPLAYERU_BROWSER")
    if custom:
        tries.append(("vlastní prohlížeč", dict(executable_path=custom)))
    if os.environ.get("TESTPLAYERU_BUNDLED"):  # GitHub: vždy stejný přibalený Chromium (výsledky se nemění s verzí Chrome)
        tries.append(("Chromium", {}))
    else:
        tries += [("Microsoft Edge", dict(channel="msedge")), ("Google Chrome", dict(channel="chrome")), ("Chromium", {})]
    last = None
    for name, kw in tries:
        try:
            return pw.chromium.launch(headless=True, args=args, **kw), name
        except Exception as e:
            last = e
    raise RuntimeError("Nenašel jsem Microsoft Edge ani Google Chrome. Nainstaluj jeden z nich.\n(%s)" % str(last)[:200])


def run_tests(player_path, sw_path, on_event, stop_flag=None, only=None):
    """on_event(kind, data): 'info' text | 'start' (i, name) | 'done' (i, name, checks, seconds) | 'finish' summary."""
    from playwright.sync_api import sync_playwright
    env = Env(player_path, sw_path)
    configure(env.player_src)
    env.prepare()
    env.start()
    results = []
    try:
        with sync_playwright() as pw:
            browser, bname = launch_browser(pw)
            on_event("info", "Prohlížeč: %s (neviditelný, bez přístupu na internet)" % bname)
            sess = Session(env, browser)
            for i, (name, fn) in enumerate(TESTS):
                if stop_flag is not None and stop_flag.is_set():
                    break
                if only and i not in only:
                    continue
                on_event("start", (i, name))
                c = Checks()
                t0 = time.time()
                try:
                    fn(sess, c)
                except Exception as e:
                    msg = str(e).strip().split("\n")[0][:220]
                    c.ok(False, "Test se nepodařilo dokončit: " + msg)
                    c.detail = traceback.format_exc()
                finally:
                    sess.close_all()
                    env.reset()
                results.append((name, c))
                on_event("done", (i, name, c, time.time() - t0))
            try:
                browser.close()
            except Exception:
                pass
    finally:
        env.stop()
    passed = sum(1 for _, c in results if c.passed)
    on_event("finish", {"passed": passed, "total": len(results), "results": results})
    return results


def html_report(results, player_path, sw_path):
    esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    passed = sum(1 for _, c in results if c.passed)
    rows = []
    for name, c in results:
        rows.append('<h3 class="%s">%s %s</h3><ul>' % ("ok" if c.passed else "bad", "✓" if c.passed else "✗", esc(name)))
        for okk, text in c.items:
            rows.append('<li class="%s">%s %s</li>' % ("ok" if okk else "bad", "✓" if okk else "✗", esc(text)))
        rows.append("</ul>")
    return """<!doctype html><html lang="cs"><meta charset="utf-8"><title>Test playeru</title>
<style>body{font-family:Segoe UI,Arial,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;background:#14161d;color:#e8e8ee}
h1{font-size:22px}h3{margin:18px 0 4px;font-size:16px}ul{margin:0 0 8px 0;padding-left:22px}li{margin:2px 0;list-style:none}
.ok{color:#7fe08a}.bad{color:#ff7b7b}.sum{font-size:18px;padding:10px 14px;border-radius:8px;background:#1f2330}small{color:#9aa}</style>
<h1>SYNTHLUCIDA – Test playeru</h1><p class="sum %s">Prošlo %d z %d testů</p>
<p><small>%s<br>player.html: %s<br>sw.js: %s</small></p>%s</html>""" % (
        "ok" if passed == len(results) else "bad", passed, len(results), time.strftime("%d.%m.%Y %H:%M"),
        esc(player_path), esc(sw_path), "\n".join(rows))


def cli(argv):
    if len(argv) < 2:
        print("Pouziti: test_playeru.py --cli player.html sw.js [cisla testu]")
        return 2
    only = set(int(x) for x in argv[2:]) if len(argv) > 2 else None

    def ev(kind, d):
        if kind == "info":
            print(d)
        elif kind == "done":
            i, name, c, sec = d
            print("%s %2d. %s (%.0f s)" % ("OK  " if c.passed else "FAIL", i + 1, name, sec))
            for okk, text in c.items:
                if not okk or os.environ.get("TESTPLAYERU_VERBOSE"):
                    print("       %s %s" % ("+" if okk else "-", text))
        elif kind == "finish":
            print("\nProslo %d z %d testu" % (d["passed"], d["total"]))

    res = run_tests(argv[0], argv[1], ev, only=only)
    return 0 if all(c.passed for _, c in res) else 1


# --------------------------------------------------------------------------- #
#  Okno programu
# --------------------------------------------------------------------------- #
def gui(parent=None):
    """parent = ramec zalozky v SYNTHLUCIDA AUDIO EDITORU; bez nej vlastni okno jako driv."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import webbrowser

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}

    if parent is None:
        root = tk.Tk()
        root.title(APP_TITLE)
        root.geometry("860x640")
        root.minsize(700, 480)
    else:
        root = parent
    q = queue.Queue()
    state = {"running": False, "results": None, "stop": threading.Event()}

    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    v_player = tk.StringVar(value=cfg.get("player", ""))
    v_sw = tk.StringVar(value=cfg.get("sw", ""))

    def pick(var, title, pattern):
        start = os.path.dirname(var.get()) if var.get() else os.path.expanduser("~")
        path = filedialog.askopenfilename(title=title, initialdir=start, filetypes=[(pattern, pattern), ("Všechny soubory", "*.*")])
        if path:
            var.set(path)
            if var is v_player and not v_sw.get():
                guess = os.path.join(os.path.dirname(path), "sw.js")
                if os.path.exists(guess):
                    v_sw.set(guess)

    for row, (label, var, pattern) in enumerate((("player.html:", v_player, "*.html"), ("sw.js:", v_sw, "*.js"))):
        ttk.Label(top, text=label, width=12).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(top, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Vybrat…", command=lambda v=var, l=label, pt=pattern: pick(v, "Vyber " + l, pt)).grid(row=row, column=2)
    top.columnconfigure(1, weight=1)

    bar = ttk.Frame(root, padding=(10, 0, 10, 6))
    bar.pack(fill="x")
    btn_run = ttk.Button(bar, text="▶  Spustit testy")
    btn_run.pack(side="left")
    btn_stop = ttk.Button(bar, text="Zastavit", state="disabled")
    btn_stop.pack(side="left", padx=6)
    btn_rep = ttk.Button(bar, text="Otevřít zprávu", state="disabled")
    btn_rep.pack(side="left")
    v_sum = tk.StringVar(value="Vyber oba soubory a spusť testy. Trvá to pár minut.")
    lbl_sum = tk.Label(bar, textvariable=v_sum, anchor="w", font=("Segoe UI", 10, "bold"))
    lbl_sum.pack(side="left", padx=12)
    prog = ttk.Progressbar(root, maximum=len(TESTS))
    prog.pack(fill="x", padx=10)

    body = ttk.PanedWindow(root, orient="horizontal")
    body.pack(fill="both", expand=True, padx=10, pady=8)
    left = ttk.Frame(body)
    lst = tk.Listbox(left, activestyle="none", font=("Segoe UI", 10), width=42)
    sb = ttk.Scrollbar(left, command=lst.yview)
    lst.configure(yscrollcommand=sb.set)
    lst.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    right = ttk.Frame(body)
    txt = tk.Text(right, wrap="word", font=("Segoe UI", 10), state="disabled")
    sb2 = ttk.Scrollbar(right, command=txt.yview)
    txt.configure(yscrollcommand=sb2.set)
    txt.pack(side="left", fill="both", expand=True)
    sb2.pack(side="right", fill="y")
    txt.tag_configure("ok", foreground="#1a8f3a")
    txt.tag_configure("bad", foreground="#c62828")
    txt.tag_configure("h", font=("Segoe UI", 11, "bold"))
    body.add(left, weight=2)
    body.add(right, weight=3)

    checks_by_index = {}

    def fill_list():
        lst.delete(0, "end")
        for i, (name, _) in enumerate(TESTS):
            lst.insert("end", "    %2d. %s" % (i + 1, name))

    def show(i):
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        if i in checks_by_index:
            name, c = checks_by_index[i]
            txt.insert("end", name + "\n\n", "h")
            for okk, text in c.items:
                txt.insert("end", ("✓ " if okk else "✗ ") + text + "\n", "ok" if okk else "bad")
        elif 0 <= i < len(TESTS):
            txt.insert("end", TESTS[i][0] + "\n\n", "h")
            txt.insert("end", "Zatím neproběhl.")
        txt.configure(state="disabled")

    def on_select(_=None):
        sel = lst.curselection()
        if sel:
            show(sel[0])

    lst.bind("<<ListboxSelect>>", on_select)

    def worker(player, sw):
        try:
            only = os.environ.get("TESTPLAYERU_ONLY")  # jen pro vyvojare: "0,3,5"
            only = set(int(x) for x in only.split(",")) if only else None
            run_tests(player, sw, lambda k, d: q.put((k, d)), stop_flag=state["stop"], only=only)
        except Exception as e:
            q.put(("error", str(e)))

    def start():
        player, sw = v_player.get().strip(), v_sw.get().strip()
        if not os.path.isfile(player) or not os.path.isfile(sw):
            messagebox.showwarning(APP_TITLE, "Vyber prosím soubor player.html i sw.js.")
            return
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"player": player, "sw": sw}, f)
        except Exception:
            pass
        checks_by_index.clear()
        fill_list()
        show(-1)
        prog["value"] = 0
        state.update(running=True, results=None)
        state["stop"].clear()
        btn_run.configure(state="disabled")
        btn_stop.configure(state="normal")
        btn_rep.configure(state="disabled")
        lbl_sum.configure(fg="black")
        v_sum.set("Spouštím…")
        threading.Thread(target=worker, args=(player, sw), daemon=True).start()

    def finish_ui():
        state["running"] = False
        btn_run.configure(state="normal")
        btn_stop.configure(state="disabled")

    def poll():
        try:
            while True:
                kind, d = q.get_nowait()
                if kind == "info":
                    v_sum.set(d)
                elif kind == "start":
                    i, name = d
                    lst.delete(i)
                    lst.insert(i, "⏳ %2d. %s" % (i + 1, name))
                    lst.see(i)
                    v_sum.set("Běží: %s" % name)
                elif kind == "done":
                    i, name, c, sec = d
                    checks_by_index[i] = (name, c)
                    lst.delete(i)
                    lst.insert(i, "%s %2d. %s" % ("✓ " if c.passed else "✗ ", i + 1, name))
                    lst.itemconfig(i, foreground="#1a8f3a" if c.passed else "#c62828")
                    prog["value"] = len(checks_by_index)
                    if not c.passed or not lst.curselection():
                        lst.selection_clear(0, "end")
                        lst.selection_set(i)
                        show(i)
                elif kind == "finish":
                    state["results"] = d["results"]
                    allok = d["passed"] == d["total"] and d["total"] == len(TESTS)
                    if os.environ.get("TESTPLAYERU_AUTORUN"):  # jen pro vyvojare: samocinny beh okna
                        print("GUI:", d["passed"], "/", d["total"], "|", lst.get(0), "|", lst.get(1))
                        root.after(300, root.destroy)
                    v_sum.set(("✓ Vše prošlo (%d z %d) – můžeš nahrát na web" if allok else "Prošlo %d z %d – klepni na červené řádky") % (d["passed"], d["total"]))
                    lbl_sum.configure(fg="#1a8f3a" if allok else "#c62828")
                    btn_rep.configure(state="normal" if d["results"] else "disabled")
                    finish_ui()
                elif kind == "error":
                    v_sum.set("Testy se nepodařilo spustit")
                    lbl_sum.configure(fg="#c62828")
                    messagebox.showerror(APP_TITLE, d)
                    finish_ui()
        except queue.Empty:
            pass
        root.after(150, poll)

    def stop():
        state["stop"].set()
        v_sum.set("Zastavuji po dokončení právě běžícího testu…")

    def report():
        if not state["results"]:
            return
        path = os.path.join(tempfile.gettempdir(), "synthlucida_test_playeru.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_report(state["results"], v_player.get(), v_sw.get()))
        webbrowser.open("file:///" + path.replace("\\", "/"))

    btn_run.configure(command=start)
    btn_stop.configure(command=stop)
    btn_rep.configure(command=report)
    fill_list()
    root.after(150, poll)
    if os.environ.get("TESTPLAYERU_AUTORUN"):
        root.after(400, start)
    if parent is None:
        root.mainloop()
    return state


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.exit(cli(sys.argv[2:]))
    gui()
