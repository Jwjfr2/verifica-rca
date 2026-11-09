"""Client for the Portale dell'Automobilista public "ServiziAlCittadino" portlets.

    python rca.py GA123BC                                  # RCA, Autoveicolo (tipo A)
    python rca.py --tipo M AB12345                         # RCA, Motoveicolo
    python rca.py --ciclomotore --cic 1234567              # RCA, ciclomotore by CIC
    python rca.py --service classe-ambientale GA123BC      # Euro class + CO2
    python rca.py --service neopatentati GA123BC           # driveable by a new driver?
    python rca.py --proxy http://user:pass@host:port GA123BC
    python batch.py --service classe-ambientale < plates.txt

Every one of these services is a Liferay portlet from the same WAR: the page carries a
POST form with a `captcha` field and an `<img class="captcha">` resource, the captcha
answer is 4 digits stored in the JSESSIONID-bound session, a wrong code neither rotates
nor consumes it, and a successful one does. The client therefore parses the form (action,
field names, captcha URL) instead of hard-coding each service.

Apple Vision reads the captcha (`vision_ocr`); ~25 ms per image, and it abstains rather
than guessing on the noisy variants, in which case the image is simply re-fetched.
"""
import argparse
import html as htmllib
import re
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
import time
from typing import NamedTuple

from curl_cffi import requests as cr

import vision_ocr
from vision_ocr import is_blank

BASE = "https://www.ilportaledellautomobilista.it"
SERVICES = {
    "rca": "/web/portale-automobilista/ext/verifica-copertura-rc",
    "classe-ambientale": "/web/portale-automobilista/ext/verifica-classe-ambientale-veicolo",
    "neopatentati": "/web/portale-automobilista/ext/neopatentati",
}
HEADERS = {"accept-language": "it-IT,it;q=0.9,en;q=0.8"}
CAPTCHA_PATH = "/tmp/rca_captcha.png"
VISION_MIN_CONF = 0.6
TIPO = {"A": "Autoveicolo", "M": "Motoveicolo", "R": "Rimorchio", "N": "Macchina Operatrice Trainata",
        "P": "Macchina Operatrice Semovente", "U": "Macchina Agricola Semovente ad 1 asse",
        "X": "Macchina Agricola Operatrice Trainata", "T": "Rimorchio Agricolo",
        "S": "Macchina Agricola Semovente", "V": "Macchina Agricola Operatrice",
        "C": "Ciclomotore",
        "E": "Autoveicolo e Rimorchio (targa prova)", "F": "Motoveicolo e Ciclomotore (targa prova)",
        "G": "Macchina Agricola (targa prova)", "H": "Macchina Operatrice (targa prova)",
        "CD": "Corpo Diplomatico", "CC": "Corpo Consolare", "EE": "Escursionisti Esteri"}


class Blocked(RuntimeError):
    """The WAF answered 403 (rate limit / IP block)."""


class FormInfo(NamedTuple):
    action: str
    captcha_url: str
    texts: tuple
    selects: tuple
    button: str


def parse_form(html):
    """Describe the POST form that carries a captcha field, or None."""
    for m in re.finditer(r"<form[^>]*>[\s\S]*?</form>", html):
        seg = m.group(0)
        if 'name="captcha"' not in seg:
            continue
        action = re.search(r'action="([^"]*)"', seg).group(1).replace("&amp;", "&")
        cap = re.search(r'<img[^>]*class="captcha"[^>]*src="([^"]*)"', seg, re.S)
        texts = re.findall(r'<input[^>]*type="text"[^>]*name="([^"]+)"', seg) or \
            re.findall(r'<input[^>]*name="([^"]+)"[^>]*type="text"', seg)
        selects = re.findall(r'<select[^>]*name="([^"]+)"', seg)
        buttons = [b for b in re.findall(r'<input[^>]*type="submit"[^>]*name="([^"]+)"', seg)
                   if b != "indietro"]
        return FormInfo(action,
                        cap.group(1).replace("&amp;", "&") if cap else "",
                        tuple(dict.fromkeys(texts)), tuple(selects), buttons[0] if buttons else "")
    return None


def parse_chooser(html):
    """Action URL of the type-chooser form (veicolo / ciclomotore buttons), or None."""
    for m in re.finditer(r"<form[^>]*>[\s\S]*?</form>", html):
        seg = m.group(0)
        if 'name="veicolo"' in seg or 'name="ciclomotore"' in seg:
            return re.search(r'action="([^"]*)"', seg).group(1).replace("&amp;", "&")
    return None


def form_errors(page):
    return [(k, htmllib.unescape(re.sub(r"<[^>]+>", " ", v)).strip())
            for k, v in re.findall(r'<span id="([\w.]+)" class="errore-desc">(.*?)</span>', page, re.S)]


def portlet_fragment(html):
    i = html.find('<div id="elenco-1"')
    if i < 0:
        return ""
    j = html.find("END IW BODY", i)
    return html[i:j if j > i else i + 8000]


def portlet_message(html):
    m = re.search(r'<div class="messaggio"[^>]*>(.*?)</div>', portlet_fragment(html), re.S)
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()) if m else ""


def result_dict(html):
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", portlet_fragment(html), re.S):
        cells = [htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip())
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
    if len(rows) < 2:
        return None
    return [dict(zip(rows[0], r)) for r in rows[1:]]


class Client:
    def __init__(self, proxy=None, timeout=30):
        self.proxy = proxy
        self.timeout = timeout
        self.s = cr.Session(impersonate="chrome",
                            proxies={"http": proxy, "https": proxy} if proxy else None)
        self._forms = {}

    def _request(self, method, url, data=None):
        headers = dict(HEADERS)
        if data is not None:
            headers["content-type"] = "application/x-www-form-urlencoded"
        r = self.s.request(method, url, headers=headers, data=data, timeout=self.timeout)
        if r.status_code == 403:
            raise Blocked(r.text[:200])
        r.raise_for_status()
        return r

    def form(self, service, veicolo=True):
        """Parsed form for a service (cached: one page GET per process)."""
        key = (service, veicolo)
        if key in self._forms:
            return self._forms[key]
        url = BASE + SERVICES[service]
        html = self._request("GET", url).text
        info = parse_form(html)
        # RCA opens on a type chooser; the other services render their form directly
        if info is None:
            url2 = parse_chooser(html) or url
            choice = {"veicolo": "Veicolo"} if veicolo else {"ciclomotore": "Ciclomotore"}
            html = self._request("POST", url2, choice).text
            info = parse_form(html)
        self._forms[key] = info
        return info

    def fetch_captcha(self, info):
        r = self.s.get(info.captcha_url, headers={**HEADERS, "accept": "image/*,*/*;q=0.8"},
                       timeout=self.timeout)
        if r.status_code == 403:
            raise Blocked(r.text[:200])
        r.raise_for_status()
        open(CAPTCHA_PATH, "wb").write(r.content)
        return CAPTCHA_PATH

    def submit(self, info, code, targa="", cic="", tipo="A"):
        data = {}
        for name in info.texts:
            if name == "targa":
                data[name] = targa
            elif name == "numeroCIC":
                data[name] = cic
            elif name == "captcha":
                data[name] = code
            else:
                data[name] = ""
        for name in info.selects:
            if name == "tipoVeicolo":
                data[name] = tipo
        if info.button:
            data[info.button] = "Ricerca"
        try:
            r = self._request("POST", info.action, data)
        except Blocked:
            raise
        except Exception as e:                      # transient 4xx/5xx: count as a miss
            return None, [(type(e).__name__, str(e))], False
        errs = form_errors(r.text)
        return r, errs, not any(k.startswith("captcha") for k, _ in errs)


class Portal:
    """One HTTP session per service.

    A captcha challenge is bound to its JSESSIONID, so sharing one session across services
    would make them fight over the same image; separate sessions let all services run at
    the same time instead (each has its own challenge).
    """

    def __init__(self, proxy=None):
        self.proxy = proxy
        self.clients = {}
        self._lock = threading.Lock()

    def client(self, service):
        with self._lock:
            if service not in self.clients:
                self.clients[service] = Client(proxy=self.proxy)
            return self.clients[service]

    def close(self):
        self.clients.clear()


def _reading(path):
    word, conf = vision_ocr.read(path)
    return (word, conf) if word and conf >= VISION_MIN_CONF else (None, 0.0)


def query(targa="", cic="", tipo="A", service="rca", proxy=None, max_captchas=15,
          max_posts=6, client=None):
    """Solve one captcha and submit one lookup. Returns a dict with the outcome."""
    if service not in SERVICES:
        raise ValueError(f"unknown service {service!r}; known: {', '.join(SERVICES)}")
    veicolo = not cic
    t0 = time.time()
    client = client or Client(proxy=proxy)
    info = client.form(service, veicolo)
    if info is None:
        raise RuntimeError(f"{service}: no captcha form found on the page")
    fetches = posts = 0
    last_errs = []
    for _ in range(max_captchas):
        path = client.fetch_captcha(info)
        fetches += 1
        if is_blank(path):
            continue
        word, conf = _reading(path)
        if not word:
            continue
        r, errs, ok = client.submit(info, word, targa, cic, tipo)
        posts += 1
        last_errs = errs
        if ok:
            return {"ok": True, "captcha": word, "confidence": conf, "fetches": fetches,
                    "posts": posts, "errors": errs, "rows": result_dict(r.text),
                    "message": portlet_message(r.text), "service": service,
                    "seconds": round(time.time() - t0, 1), "proxy": proxy,
                    "targa": targa, "cic": cic, "tipo": TIPO.get(tipo, tipo)}
        if posts >= max_posts:
            break
    return {"ok": False, "captcha": None, "fetches": fetches, "posts": posts,
            "errors": last_errs, "message": "", "seconds": round(time.time() - t0, 1),
            "proxy": proxy, "service": service}


def query_all(targa="", cic="", tipo="A", proxy=None, portal=None, max_captchas=15,
              max_posts=6, parallel=True):
    """Everything the anonymous portal exposes for one vehicle, in one call.

    The three portlets are queried concurrently on separate sessions (one captcha each),
    so wall time is the slowest of the three instead of their sum.
    """
    portal = portal or Portal(proxy)
    threading.Thread(target=vision_ocr.prime, daemon=True).start()
    svc_tipo = "C" if cic else tipo          # a CIC lookup is a moped
    targets = []
    for service in SERVICES:
        if service != "rca" and not targa:
            continue                          # emission class / neopatentati are plate-only
        if service == "neopatentati" and (cic or tipo != "A"):
            continue                          # no type selector: category B cars only
        targets.append(service)

    def run(service):
        return service, query(targa, cic if service == "rca" else "",
                              tipo if service == "rca" else svc_tipo,
                              service=service, client=portal.client(service),
                              max_captchas=max_captchas, max_posts=max_posts)

    results = {}
    if parallel and len(targets) > 1:
        with ThreadPoolExecutor(max_workers=len(targets)) as ex:
            for service, res in ex.map(run, targets):
                results[service] = res
    else:
        for service in targets:
            name, res = run(service)
            results[name] = res

    fields, message, errors, notes = {}, "", [], []
    for service, res in results.items():
        if not res["ok"]:
            errors.append(f"{service}: no result")
            notes.append(f"{service}: no result")
            continue
        for row in res["rows"] or []:
            for k, v in row.items():
                if v and k not in fields:
                    fields[k] = v
        if res["message"] and not message:
            message = res["message"]
        for _, val in res["errors"]:
            notes.append(f"{service}: {val}")
    return {"ok": bool(fields), "fields": fields, "message": message, "errors": errors,
            "notes": notes, "services": results, "targa": targa, "cic": cic,
            "tipo": TIPO.get(tipo, tipo), "proxy": proxy}


def format_line(result):
    parts = [f"{k}: {v}" for k, v in result["fields"].items() if v]
    if result["message"]:
        parts.append(result["message"])
    parts.extend(result.get("notes", []))
    return " | ".join(parts) if parts else "no result"

def main(argv=None):
    ap = argparse.ArgumentParser(description="Portale dell'Automobilista lookups (captcha auto-solved)")
    ap.add_argument("targa", nargs="?", default="")
    ap.add_argument("--cic", default="", help="numero CIC (ciclomotore)")
    ap.add_argument("--ciclomotore", action="store_true")
    ap.add_argument("--tipo", default="A", help="tipoVeicolo: A=auto, M=moto, R=rimorchio, C=ciclomotore, ...")
    ap.add_argument("--proxy", default=None, help="http://user:pass@host:port")
    ap.add_argument("--proxy-file", default=None, help="one proxy per line; rotated on a WAF block")
    ap.add_argument("--max-captchas", type=int, default=15,
                    help="give up after N images (about 40%% of them are unreadable)")
    args = ap.parse_args(argv)

    proxies = [args.proxy] if args.proxy else []
    if args.proxy_file:
        proxies += [l.strip() for l in open(args.proxy_file) if l.strip() and not l.startswith("#")]
    proxies = proxies or [None]

    cic = args.cic
    for proxy in proxies:
        try:
            res = query_all(args.targa, cic, args.tipo, proxy=proxy,
                            max_captchas=args.max_captchas)
        except Blocked as e:
            print(f"proxy {proxy or 'direct'}: WAF block ({str(e)[:60]}...); rotating")
            continue
        except Exception as e:
            print(f"proxy {proxy or 'direct'}: {type(e).__name__}: {str(e)[:80]}; rotating")
            continue
        print(format_line(res))
        return 0 if res["ok"] else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
