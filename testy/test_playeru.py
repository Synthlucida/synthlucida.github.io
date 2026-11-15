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
    c.ok(u["dis"] and u["locked"] and u["seg"] == "AUTO*,GITHUB", "Volba R2 je bez admina zamčená a zdroj je AUTO")
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
    ("Odolnost proti poškozeným datům", t_robust),
    ("Rozložení na malém a velkém displeji", t_layout),
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
