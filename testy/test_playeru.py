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
            ctx.add_init_script(SEEN_JS)
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
    x = p.evaluate("()=>({avail:computeAvailableXP(stats),level:xpLevelInfo(computeXP(stats)).level})")
    c.ok(x["avail"] == 1000 and x["level"] >= 6, "Admin dostane 1000 XP na vyzkoušení (úroveň %s)" % x["level"])
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
    ctx, p, errs = s.page(seen=False, wait=2800)
    r = p.evaluate("()=>({sp:!document.getElementById('support-popup').hidden,hint:!document.getElementById('tour-hint').hidden})")
    c.ok(r["sp"] and not r["hint"], "První návštěva: okno s poděkováním, bublina průvodce čeká")
    p.click("#sp-ok")
    p.wait_for_timeout(1800)
    r = p.evaluate("()=>({hint:!document.getElementById('tour-hint').hidden,blink:document.querySelector('.main-guide-btn').classList.contains('tour-attention')})")
    c.ok(r["hint"] and r["blink"], "Pak se ukáže bublina „Jsi tu poprvé?“ a bliká ⓘ")
    p.click("#tour-hint-go")
    p.wait_for_timeout(600)
    n = p.evaluate("document.getElementById('tour-step').textContent")
    c.ok(re.match(r"1 / \d+", n or ""), "Průvodce se spustí (krok %s)" % n)
    seen_layer = False
    for _ in range(14):
        if "#btn-layer" and re.search("LAYER|VRSTVA", p.evaluate("document.getElementById('tour-card').innerText"), re.I):
            seen_layer = True
            break
        p.keyboard.press("ArrowRight")
        p.wait_for_timeout(400)
    c.ok(seen_layer, "Průvodce má krok o druhé ASMR vrstvě (i pod vlastní hudbou)")
    p.keyboard.press("Escape")
    p.wait_for_timeout(300)
    c.ok(p.evaluate("document.getElementById('tour').hidden"), "Esc průvodce zavře")
    c.ok(p.evaluate("!!document.querySelector('.guide-layer-tip .guide-tip-text') && document.querySelector('.guide-layer-tip .guide-tip-text').textContent.length>80"),
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

    ctx, p, errs = s.page()
    p.evaluate("()=>{slPlayLog('test zaznamu');location.hash='#playlog'}")
    p.wait_for_timeout(500)
    shown = p.evaluate("()=>{const v=document.getElementById('sl-playlog-view');return v?v.textContent:''}")
    c.ok("test zaznamu" in shown, "Záznam přehrávání jde zobrazit přes player.html#playlog")
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
        missing = [u.rsplit("/", 1)[-1] for w, u, ok in mine if ok is False]
        unknown = [u for w, u, ok in mine if ok is None]
        if len(unknown) == len(mine):
            c.ok(True, "%s: nepodařilo se ověřit (není internet?) – kontrola přeskočena" % where)
            continue
        if missing:
            n = len(missing)
            word = "skladba CHYBÍ" if n == 1 else ("skladby CHYBÍ" if 2 <= n <= 4 else "skladeb CHYBÍ")
            c.ok(False, "%s: na serveru %d %s (nebo má jinak napsaný název): %s" % (where, n, word, ", ".join(missing[:8])))
        else:
            c.ok(True, "%s: všech %d skladeb je opravdu na serveru" % (where, len(mine) - len(unknown)) +
                 (" (%d se nepodařilo ověřit)" % len(unknown) if unknown else ""))
    if not r2:
        c.ok(False, "V sw.js jsem nenašel adresu zálohy R2")


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
def gui():
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

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("860x640")
    root.minsize(700, 480)
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
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.exit(cli(sys.argv[2:]))
    gui()
