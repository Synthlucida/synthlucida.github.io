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
import datetime
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

    return """<!doctype html>
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
    with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_page(entry, history))

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
