#!/usr/bin/env python3
"""
heureka_intl.py – seznam e-shopů z Heureka.cz + dohledání jejich mezinárodních domén.

Dvě fáze, každá se dá spustit zvlášť a obě jdou kdykoli přerušit a navázat (cache):

  1) python heureka_intl.py shops            -> shops.csv (název, slug, Heureka URL, doména)
  2) python heureka_intl.py intl shops.csv   -> intl_vysledky.csv (seřazeno podle počtu zahraničních domén)

Fáze 2 vezme jakékoli CSV/TXT s doménami v prvním sloupci (nebo sloupci "domena"),
takže ji jde pustit i na seznam obchodů ze Srovname.

Šetrnost vůči Heurece (fáze 1):
  - seznam obchodů se bere primárně ze sitemap (pár XML souborů místo tisíců stránek),
  - doména se odvodí ze slugu (obchody.heureka.cz/alza-cz/ -> alza.cz), detail obchodu
    se stahuje jen tam, kde odvozená doména neexistuje v DNS,
  - jedno vlákno, náhodná pauza 4–9 s, respektuje robots.txt (Disallow i Crawl-delay),
  - každá stažená stránka se ukládá do cache, nic se nestahuje dvakrát,
  - při 403/429 dlouhá pauza a po opakování skript sám skončí (zkuste to jindy),
  - limit požadavků na jedno spuštění (--max-requests).
Fáze 2 už na Heureku vůbec nechodí – stahuje jen úvodní stránky samotných e-shopů.

Stejný provozovatel (fáze 2): u hlavního webu i u každé nalezené zahraniční domény se načte
úvodní stránka + kontakt/obchodní podmínky/impressum a porovná se:
  ano            = shodné IČO, DIČ/VAT nebo název firmy (doložený stejný provozovatel)
  pravdepodobne  = shodné měřicí kódy (GTM/GA4/Ads/FB pixel), hreflang nebo vzájemné odkazy
  nejiste        = doména existuje, ale vazbu se nepodařilo doložit (ověřit ručně)

Menší a střední e-shopy: velké obchody s vlastním řešením (Alza, Mall, Notino… – seznam BIG_SHOPS)
se přeskakují a u každého e-shopu se zjistí platforma (Shoptet, Upgates, WooCommerce, Shopify…).
  python heureka_intl.py intl shops.csv --platformy shoptet,upgates,woocommerce,shopify,prestashop

Instalace:  pip install requests beautifulsoup4 lxml
"""
import argparse
import csv
import hashlib
import json
import os
import random
import re
import socket
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEUREKA = "https://obchody.heureka.cz"
CACHE_DIR = ".cache_heureka"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; heureka-intl-check/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.6",
}

# TLD, které se zkoušejí u každé značky (pořadí = priorita ve výstupu)
TLDS = ["cz", "sk", "pl", "hu", "ro", "bg", "hr", "si", "rs", "de", "at", "ch",
        "it", "es", "pt", "fr", "be", "nl", "lu", "ie", "co.uk", "dk", "se", "fi",
        "no", "ee", "lv", "lt", "gr", "ua", "eu", "com"]

# "Globální" TLD se do počtu zemí nezapočítávají (bývají to rozcestníky)
NON_COUNTRY = {"eu", "com", "net", "org", "shop", "store"}

PARKED_HINTS = re.compile(
    r"domain (is )?for sale|doména (je )?na prodej|this domain|parked|"
    r"sedo|dan\.com|afternic|godaddy|hugedomains|domena-na-prodej|"
    r"stránka ve výstavbě|under construction|coming soon|wedos|forpsi|"
    r"default web site page|apache2 .* default page|welcome to nginx",
    re.I)

MULTI_SUFFIX = {"co.uk", "com.pl", "com.ua", "co.hu", "com.hr"}


# --------------------------------------------------------------------------- utils
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def split_domain(host):
    """'www.shop.co.uk' -> ('shop', 'co.uk'); vrací (brand, tld)."""
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_SUFFIX:
        return parts[-3], ".".join(parts[-2:])
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return host, ""


def norm_domain(s):
    s = (s or "").strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).hostname or ""
    return host[4:] if host.startswith("www.") else host


def slug_to_domain(slug):
    """'alza-cz' -> 'alza.cz', 'e-shop-pro-psy-cz' -> 'e-shop-pro-psy.cz', 'shop-co-uk' -> 'shop.co.uk'."""
    slug = slug.strip("/").lower()
    for suf in sorted(MULTI_SUFFIX | set(TLDS) | {"net", "org", "info", "shop", "store", "biz"},
                      key=len, reverse=True):
        token = "-" + suf.replace(".", "-")
        if slug.endswith(token) and len(slug) > len(token):
            return slug[: -len(token)] + "." + suf
    return ""


def resolves(host):
    try:
        socket.setdefaulttimeout(5)
        socket.gethostbyname(host)
        return True
    except Exception:
        return False


def read_domains(path):
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        if "," in sample or ";" in sample:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")
            rows = list(csv.reader(f, dialect))
        else:
            rows = [[l.strip()] for l in f if l.strip()]
    if not rows:
        return out, None
    header = [h.strip().lower() for h in rows[0]]
    col = header.index("domena") if "domena" in header else 0
    start = 1 if ("domena" in header or "." not in rows[0][col]) else 0
    for r in rows[start:]:
        if len(r) > col:
            d = norm_domain(r[col])
            if d and "." in d:
                out.append((d, r))
    return out, rows[0] if start else None


# ------------------------------------------------------------ fáze 1: Heureka
class PoliteFetcher:
    """Jedno vlákno, náhodné pauzy, cache na disk, robots.txt, zastavení při blokaci."""

    def __init__(self, min_wait, max_wait, max_requests):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.min_wait, self.max_wait = min_wait, max_wait
        self.max_requests = max_requests
        self.count = 0
        self.blocks = 0
        self.last = 0.0
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.robots = urllib.robotparser.RobotFileParser()
        self.robots_text = ""

    def _fetch_robots(self, url):
        """Stáhne robots.txt; redirect na jinou cestu (např. /robots.txt/) ani HTML stránku nebere."""
        for _ in range(3):
            try:
                r = self.s.get(url, timeout=25, allow_redirects=False)
            except requests.RequestException as e:
                log("chyba sítě:", url, e)
                return None
            if r.status_code in (301, 302, 303, 307, 308):
                nxt = urljoin(url, r.headers.get("location", ""))
                if urlparse(nxt).path != "/robots.txt":   # přesměrování mimo robots.txt = soubor neexistuje
                    return None
                url = nxt
                continue
            ctype = r.headers.get("content-type", "").lower()
            if r.status_code != 200 or "html" in ctype or r.text.lstrip()[:1] == "<":
                return None
            return r.text
        return None

    def load_robots(self):
        # obchody.heureka.cz vlastní robots.txt nemá (/robots.txt -> /robots.txt/ -> 404 HTML),
        # proto záložně pravidla hlavní domény www.heureka.cz
        txt = ""
        for url in (HEUREKA + "/robots.txt", "https://www.heureka.cz/robots.txt"):
            txt = self._fetch_robots(url) or ""
            if txt:
                log(f"robots.txt: {url}")
                break
        else:
            log("robots.txt nenalezen – pokračuji s výchozími pauzami")
        self.robots_text = txt
        self.robots.parse(txt.splitlines())
        cd = self.robots.crawl_delay(HEADERS["User-Agent"]) or self.robots.crawl_delay("*")
        if cd and cd > self.min_wait:
            self.min_wait = float(cd)
            self.max_wait = max(self.max_wait, cd * 1.8)
            log(f"robots.txt Crawl-delay {cd}s – pauzy upraveny na {self.min_wait:.0f}–{self.max_wait:.0f}s")
        return [l.split(":", 1)[1].strip() for l in txt.splitlines()
                if l.lower().startswith("sitemap:")]

    def _cache_path(self, url):
        return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".html")

    def get(self, url, check_robots=True):
        cp = self._cache_path(url)
        if os.path.exists(cp):
            with open(cp, encoding="utf-8", errors="ignore") as f:
                return f.read()
        if check_robots and self.robots_text and not self.robots.can_fetch(HEADERS["User-Agent"], url):
            log("robots.txt zakazuje:", url)
            return None
        if self.count >= self.max_requests:
            raise SystemExit(f"Dosažen limit {self.max_requests} požadavků na spuštění. "
                             "Spusťte znovu později – naváže se z cache.")
        wait = random.uniform(self.min_wait, self.max_wait)
        if random.random() < 0.04:            # občas delší pauza jako člověk
            wait += random.uniform(20, 60)
        sleep_for = self.last + wait - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)
        try:
            r = self.s.get(url, timeout=25)
        except requests.RequestException as e:
            log("chyba sítě:", url, e)
            self.last = time.time()
            return None
        self.last = time.time()
        self.count += 1
        if r.status_code in (403, 429, 503):
            self.blocks += 1
            pause = 600 * self.blocks
            log(f"HTTP {r.status_code} na {url} – pauza {pause // 60} min")
            if self.blocks >= 3:
                raise SystemExit("Heureka opakovaně odmítá požadavky. Končím, zkuste to za pár hodin "
                                 "(hotová data jsou v cache).")
            time.sleep(pause)
            return None
        if r.status_code != 200:
            return None
        self.blocks = 0
        r.encoding = r.encoding or "utf-8"
        with open(cp, "w", encoding="utf-8") as f:
            f.write(r.text)
        return r.text


SHOP_URL_RE = re.compile(r"^https?://obchody\.heureka\.cz/([a-z0-9][a-z0-9\-]*[a-z0-9])/?$", re.I)


def parse_sitemap(xml):
    soup = BeautifulSoup(xml, "xml")
    sub = [l.get_text(strip=True) for l in soup.select("sitemap > loc")]
    urls = [l.get_text(strip=True) for l in soup.select("url > loc")]
    return sub, urls


def shops_from_listing(html, base):
    """Fallback: z výpisu kategorie vytáhne odkazy na detaily obchodů + další stránku."""
    soup = BeautifulSoup(html, "html.parser")
    shops, nxt = {}, None
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"]).split("#")[0].split("?")[0]
        m = SHOP_URL_RE.match(u)
        if m and slug_to_domain(m.group(1)):
            shops.setdefault(m.group(1), a.get_text(" ", strip=True)[:80])
    link = soup.find("a", rel="next") or soup.find("link", rel="next")
    if link and link.get("href"):
        nxt = urljoin(base, link["href"])
    return shops, nxt


def domain_from_detail(html):
    """Z detailu obchodu na Heurece najde skutečnou doménu e-shopu."""
    soup = BeautifulSoup(html, "html.parser")
    cands = []
    for a in soup.find_all("a", href=True):
        h = norm_domain(a["href"])
        if h and "heureka" not in h and "." in h and not h.endswith(("facebook.com", "google.com",
                                                                        "instagram.com", "youtube.com")):
            txt = a.get_text(" ", strip=True).lower()
            score = 2 if (h in txt or "obchod" in txt or "web" in txt) else 1
            cands.append((score, h))
    for t in soup.find_all(string=re.compile(r"\b[a-z0-9\-]+\.(cz|sk|com|eu)\b", re.I)):
        m = re.search(r"\b((?:www\.)?[a-z0-9\-]+\.(?:cz|sk|com|eu))\b", t, re.I)
        if m:
            cands.append((1, norm_domain(m.group(1))))
    cands.sort(reverse=True)
    return cands[0][1] if cands else ""


def cmd_shops(args):
    f = PoliteFetcher(args.min_wait, args.max_wait, args.max_requests)
    sitemaps = list(dict.fromkeys([HEUREKA + "/sitemap.xml"] + f.load_robots()))
    shops = {}

    # 1) sitemapy
    queue, seen = list(sitemaps), set()
    while queue:
        sm = queue.pop(0)
        if sm in seen or "heureka" not in sm:
            continue
        seen.add(sm)
        xml = f.get(sm, check_robots=False)
        if not xml:
            continue
        sub, urls = parse_sitemap(xml)
        # do pod-sitemap jdeme jen pokud se týkají obchodů
        queue += [s for s in sub if "obchod" in s.lower() or "shop" in s.lower() or "obchody.heureka" in s]
        for u in urls:
            m = SHOP_URL_RE.match(u)
            if m and slug_to_domain(m.group(1)):
                shops.setdefault(m.group(1), "")
    log(f"ze sitemap: {len(shops)} obchodů")

    # 2) fallback – výpisy kategorií (jen když sitemap nic nedala)
    if args.kategorie:
        shops = {}                      # při výběru kategorií sitemap (všechny obchody) nepoužijeme
    if len(shops) < 100:
        log("sitemap obchody neobsahuje, procházím výpis kategorií (pomaleji)")
        home = f.get(HEUREKA + "/") or ""
        soup = BeautifulSoup(home, "html.parser")
        cats = sorted({urljoin(HEUREKA, a["href"]).split("?")[0] for a in soup.find_all("a", href=True)
                       if urljoin(HEUREKA, a["href"]).startswith(HEUREKA + "/")
                       and not SHOP_URL_RE.match(urljoin(HEUREKA, a["href"]).split("?")[0])})
        if args.kategorie:
            want = [k.strip().lower() for k in args.kategorie.split(",") if k.strip()]
            cats = [c for c in cats if any(k in c.lower() for k in want)]
            log(f"vybrané kategorie: {len(cats)} ({', '.join(want)})")
        for cat in cats:
            url, pages = cat, 0
            while url and pages < args.max_pages:
                html = f.get(url)
                if not html:
                    break
                found, url = shops_from_listing(html, url)
                for k, v in found.items():
                    shops.setdefault(k, v)
                pages += 1
            log(f"{cat}: celkem {len(shops)} obchodů")

    # 3) doména ze slugu, detail jen u nejasných
    rows = []
    for i, (slug, name) in enumerate(sorted(shops.items()), 1):
        dom = slug_to_domain(slug)
        src = "slug"
        if not dom or not resolves(dom):
            html = f.get(f"{HEUREKA}/{slug}/")
            d2 = domain_from_detail(html) if html else ""
            if d2:
                dom, src = d2, "detail"
        rows.append({"nazev": name or dom, "slug": slug, "heureka_url": f"{HEUREKA}/{slug}/",
                     "domena": dom, "zdroj_domeny": src})
        if i % 500 == 0:
            log(f"zpracováno {i}/{len(shops)}")

    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["domena"])
        w.writeheader()
        w.writerows(rows)
    log(f"hotovo: {len(rows)} obchodů -> {args.out} (požadavků na Heureku: {f.count})")


# ------------------------------------------------------- fáze 2: zahraniční domény
# Stránky, kde e-shopy uvádějí provozovatele (IČO, DIČ, firma) – CZ/SK/PL/HU/DE/RO/... + EN
OPERATOR_PAGE_RE = re.compile(
    r"kontakt|obchodni-podminky|obchodne-podmienky|podminky|podmienky|o-nas|o-firme|o-spolecnosti|"
    r"impressum|imprint|contact|terms|agb|regulamin|o-nas|kapcsolat|impresszum|aszf|"
    r"termeni|contacte|uslovia|condizioni|contatti|condiciones|contacto|cgv|mentions-legales",
    re.I)

# DIČ / VAT ID – všechny státy EU (+ CH, NO, UK). Prefix je povinný -> málo falešných shod.
VAT_RE = re.compile(
    r"\b(CZ\d{8,10}|SK\d{10}|PL\d{10}|HU\d{8}|DE\d{9}|ATU\d{8}|RO\d{2,10}|BG\d{9,10}|HR\d{11}|"
    r"SI\d{8}|IT\d{11}|ES[A-Z0-9]\d{7}[A-Z0-9]|FR[A-Z0-9]{2}\d{9}|BE0?\d{9,10}|NL\d{9}B\d{2}|"
    r"LU\d{8}|IE\d{7}[A-Z]{1,2}|DK\d{8}|SE\d{12}|FI\d{8}|EE\d{9}|LV\d{11}|LT\d{9,12}|EL\d{9}|"
    r"PT\d{9}|CHE[\-\s]?\d{3}\.?\d{3}\.?\d{3}|GB\d{9})\b")
ICO_RE = re.compile(r"\bI[ČC]O?\s*[:.]?\s*(\d{2}\s?\d{3}\s?\d{3}|\d{8})\b")
LEGAL_FORMS = (r"s\.\s?r\.\s?o\.|spol\.\s?s\s?r\.\s?o\.|a\.\s?s\.|k\.\s?s\.|v\.\s?o\.\s?s\.|"
               r"sp\.\s?z\s?o\.\s?o\.|S\.A\.|Kft\.?|Zrt\.?|Bt\.|GmbH|AG|S\.R\.L\.|SRL|s\.r\.l\.|"
               r"d\.o\.o\.|EOOD|OOD|Ltd\.?|Limited|B\.V\.|BV|S\.L\.|SAS|SARL|SpA|S\.p\.A\.|UAB|SIA|OÜ")
# 1–4 slova začínající písmenem, hned za nimi právní forma (např. "Psí svět s.r.o.")
COMPANY_RE = re.compile(
    r"((?:[A-ZÁ-Ža-zá-ž][\w&\-\.\']*\s+){1,4}?(?:" + LEGAL_FORMS + r"))(?=[\s,;)<]|$)", re.U)
COMPANY_NOISE = re.compile(r"\b(sídlo|copyright|práva|rights|provozovatel|prevádzkovateľ|společnost|"
                           r"spoločnosť|firma|company|sprzedawca|üzemeltető|betreiber|operator)\b", re.I)
# Firmy, které na webech vystupují, ale nejsou provozovatelem (platformy, platby, dopravci…)
NOT_OPERATOR = re.compile(
    r"shoptet|upgates|eshop-rychle|webnode|wordpress|woocommerce|shopify|heureka|seznam|google|"
    r"comgate|gopay|thepay|global payments|adyen|stripe|paypal|packeta|zásilkovna|zasilkovna|ppl|dpd|"
    r"gls|geis|česká pošta|ceska posta|wedos|forpsi|active24|websupport|"
    r"trusted shops|ekomi|smartsupp|ecomail|mailchimp|cookiebot|onetrust",
    re.I)

# Rozpoznání e-shopové platformy (menší/střední e-shopy = krabicová řešení)
PLATFORMS = [
    ("Shoptet", re.compile(r"cdn\.myshoptet\.com|shoptet\.cz|/user/documents/|shoptet", re.I)),
    ("Upgates", re.compile(r"upgates", re.I)),
    ("Eshop-rychle", re.compile(r"eshop-rychle|ers\.cz/", re.I)),
    ("Webareal", re.compile(r"webareal", re.I)),
    ("ByznysWeb", re.compile(r"byznysweb", re.I)),
    ("FastCentrik", re.compile(r"fastcentrik", re.I)),
    ("Shopsys", re.compile(r"shopsys", re.I)),
    ("Shopify", re.compile(r"cdn\.shopify\.com|Shopify\.theme", re.I)),
    ("WooCommerce", re.compile(r"woocommerce", re.I)),
    ("PrestaShop", re.compile(r"prestashop", re.I)),
    ("Shopware", re.compile(r"shopware", re.I)),
    ("Magento", re.compile(r"Magento_|mage/cookies|var BLANK_URL|/static/version\d+/frontend/", re.I)),
    ("OpenCart", re.compile(r"route=product/|catalog/view/theme", re.I)),
    ("Wix", re.compile(r"wixstatic\.com|_wixCIDX", re.I)),
    ("Webnode", re.compile(r"webnode", re.I)),
    ("BigCommerce", re.compile(r"bigcommerce", re.I)),
]

# Velké e-shopy s vlastním řešením – nejsou cílovka, přeskočí se (lze vypnout --vcetne-velkych)
BIG_SHOPS = {
    "alza", "mall", "notino", "datart", "czc", "kasa", "tsbohemia", "mironet", "electroworld",
    "okay", "euronics", "planeo", "expert", "hornbach", "obi", "bauhaus", "ikea", "decathlon",
    "sportisimo", "pilulka", "drmax", "benu", "rohlik", "kosik", "zoot", "answear", "aboutyou",
    "zalando", "kaufland", "tchibo", "lidl", "dm", "rossmann", "knihydobrovsky", "martinus",
    "luxor", "megaknihy", "kytary", "bonami", "vivantis", "prozdravi", "mountfield", "hudy",
    "intersport", "a3sport", "4camping", "sizeer", "footshop", "fajn-brands", "lekarna", "mojelekarna",
    "allegro", "temu", "shein", "aliexpress", "amazon", "ebay", "booking", "conrad", "elektro-bauhaus",
    "smarty", "istyle", "mp3", "elkov", "siko", "xxxlutz", "jysk", "sconto", "mobelix", "asko",
    "decodoma", "nay", "nay-elektrodom", "andrea", "babyshop", "dracik", "pompo", "hracky-pompo",
    "pet-center", "zoohit", "zooplus", "bux", "sklik", "heureka", "glami", "mujrozpocet",
}
TRACKING_PATTERNS = {
    "gtm": re.compile(r"GTM-[A-Z0-9]{4,9}"),
    "ga4": re.compile(r"\bG-[A-Z0-9]{8,12}\b"),
    "aw": re.compile(r"AW-\d{6,12}"),
    "fb": re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{10,20})"),
}


def fetch_site(url, timeout=12):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return None, None
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return r.url, r.text[:800_000]
    except requests.RequestException:
        return None, None


def norm_company(name):
    n = re.sub(r"(?i)" + LEGAL_FORMS, " ", name)
    n = re.sub(r"[^\w]+", " ", n.lower(), flags=re.U)
    n = re.sub(r"\b(spol|s|r|o|a|the|company|společnost|spolecnost)\b", " ", n)
    return " ".join(n.split())


def page_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return soup.get_text(" ", strip=True)


def operator_ids(html):
    """Z HTML vytáhne IČO, DIČ/VAT a názvy firem s právní formou."""
    txt = page_text(html)
    ids = {"ico": set(), "vat": set(), "firma": set()}
    for m in ICO_RE.findall(txt):
        ids["ico"].add(re.sub(r"\s", "", m))
    for m in VAT_RE.findall(txt):
        v = re.sub(r"[\s\.\-]", "", m.upper())
        ids["vat"].add(v)
        if v.startswith(("CZ", "SK")) and len(v) == 10:      # CZ DIČ obsahuje IČO
            ids["ico"].add(v[2:])
    for m in COMPANY_RE.findall(txt):
        m = m.strip(" ,")
        # odřízne úvodní slova typu "Provozovatel:" / "Sídlo společnosti"
        words = m.split()
        while words and COMPANY_NOISE.search(words[0].strip(":")):
            words.pop(0)
        m = " ".join(words)
        if not m or NOT_OPERATOR.search(m) or len(m) > 70 or COMPANY_NOISE.search(m):
            continue
        n = norm_company(m)
        if len(n) >= 3:
            ids["firma"].add(n)
    return ids


def tracking_ids(html):
    return {k: set(rx.findall(html or "")) for k, rx in TRACKING_PATTERNS.items()}


def operator_pages(html, base, limit=2):
    """Najde odkazy na kontakt / obchodní podmínky / impressum na stejném webu."""
    soup = BeautifulSoup(html, "html.parser")
    host = norm_domain(base)
    out = []
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"]).split("#")[0]
        if norm_domain(u) != host or u.rstrip("/") == base.rstrip("/"):
            continue
        if OPERATOR_PAGE_RE.search(u) or OPERATOR_PAGE_RE.search(a.get_text(" ", strip=True)
                                                                   .lower().replace(" ", "-")):
            if u not in out:
                out.append(u)
    # kontakt/impressum mají přednost před dlouhými obchodními podmínkami
    out.sort(key=lambda u: 0 if re.search(r"kontakt|impressum|contact|kapcsolat|impresszum", u, re.I) else 1)
    return out[:limit]


def detect_platform(html):
    head = html[:400_000]
    for name, rx in PLATFORMS:
        if rx.search(head):
            return name
    return ""


class Site:
    """Načtený web + jeho otisk (provozovatel, měřicí kódy, odkazy)."""

    def __init__(self, host):
        self.host = host
        self.ok = False
        self.final_host = host
        self.html = ""
        self.ids = {"ico": set(), "vat": set(), "firma": set()}
        self.track = {}
        self.hreflang, self.links, self.title = set(), set(), ""
        self.platform = ""

    def load(self, operator_lookup=True):
        final, html = fetch_site("https://" + self.host)
        if not html:
            final, html = fetch_site("http://" + self.host)
        if not html:
            return self
        self.ok, self.html = True, html
        self.final = final
        self.final_host = norm_domain(final) or self.host
        self.track = tracking_ids(html)
        self.platform = detect_platform(html)
        self.hreflang, self.links, self.title = linked_hosts(html, final)
        self.ids = operator_ids(html)
        if operator_lookup and not (self.ids["ico"] or self.ids["vat"]):
            for u in operator_pages(html, final):
                _, h = fetch_site(u)
                if h:
                    more = operator_ids(h)
                    for k in self.ids:
                        self.ids[k] |= more[k]
                    if self.ids["ico"] or self.ids["vat"]:
                        break
        return self

    def operator_label(self):
        parts = sorted(self.ids["firma"])[:2] + sorted(self.ids["ico"])[:2] + sorted(self.ids["vat"])[:2]
        return ", ".join(parts)


def linked_hosts(html, base):
    """hreflang + všechny odkazy na jiné hosty (jazykové přepínače, patičky s vlajkami)."""
    soup = BeautifulSoup(html, "html.parser")
    hreflang, links = set(), set()
    for l in soup.find_all("link", href=True):
        rel = " ".join(l.get("rel") or []).lower()
        if "alternate" in rel and l.get("hreflang"):
            h = norm_domain(urljoin(base, l["href"]))
            if h:
                hreflang.add(h)
    for a in soup.find_all("a", href=True):
        h = norm_domain(urljoin(base, a["href"]))
        if h:
            links.add(h)
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:120]
    return hreflang, links, title


def compare(main, other):
    """Vrátí (úroveň, důvod). Úroveň: 'ano' = shodný provozovatel doložen IČO/DIČ/firmou,
    'pravdepodobne' = shodné měřicí kódy / vzájemné odkazy, '' = nic."""
    if main.ids["ico"] & other.ids["ico"]:
        return "ano", "stejné IČO " + ",".join(sorted(main.ids["ico"] & other.ids["ico"]))
    if main.ids["vat"] & other.ids["vat"]:
        return "ano", "stejné DIČ/VAT " + ",".join(sorted(main.ids["vat"] & other.ids["vat"]))
    shared = main.ids["firma"] & other.ids["firma"]
    if shared:
        return "ano", "stejná firma " + sorted(shared)[0]
    for k in ("gtm", "ga4", "aw", "fb"):
        common = main.track.get(k, set()) & other.track.get(k, set())
        if common:
            return "pravdepodobne", f"stejné {k.upper()} {sorted(common)[0]}"
    if main.final_host in other.hreflang or main.host in other.hreflang:
        return "pravdepodobne", "hreflang oboustranně"
    if main.final_host in other.links or main.host in other.links:
        return "pravdepodobne", "odkazuje zpět"
    return "", ""


def is_parked(html):
    return PARKED_HINTS.search(html[:20000]) or len(html) < 3000


def analyse(domain, tlds, deep, include_big=False, only_platforms=None):
    res = {"domena": domain, "stav": "", "titulek": "", "provozovatel": "", "platforma": "",
           "domeny": [], "nejiste": [], "pocet": 0, "pocet_zemi": 0,
           "pocet_stejny_provozovatel": 0, "zeme_stejny_provozovatel": 0}
    if not include_big and split_domain(domain)[0] in BIG_SHOPS:
        res["stav"] = "vyřazeno: velký e-shop"
        return res
    main = Site(domain).load()
    if not main.ok:
        res["stav"] = "web nenačten"
        return res
    base_host = main.final_host
    brand, own_tld = split_domain(base_host)
    res["titulek"], res["provozovatel"] = main.title, main.operator_label()
    res["platforma"] = main.platform or "vlastní / nerozpoznáno"
    if not include_big and brand in BIG_SHOPS:
        res["stav"] = "vyřazeno: velký e-shop"
        return res
    if only_platforms is not None and main.platform.lower() not in only_platforms:
        res["stav"] = "vyřazeno: platforma " + res["platforma"]
        return res

    cands = {}   # host -> zdroj nálezu

    def cand(host, why):
        b, t = split_domain(host)
        if host == base_host or (b == brand and t == own_tld) or not t:
            return
        cands.setdefault(host, why)

    for h in main.hreflang:                                  # a) hreflang
        cand(h, "hreflang")
    for h in main.links:                                     # b) přepínač zemí / patička
        b, t = split_domain(h)
        if b == brand and t != own_tld:
            cand(h, "odkaz z webu")
        elif deep and len(brand) >= 5 and brand in b and t in TLDS and t != own_tld:
            cand(h, "odkaz z webu (podobný název)")
    for t in tlds:                                           # c) značka na ostatních TLD
        if t == own_tld:
            continue
        host = f"{brand}.{t}"
        if host not in cands and (resolves(host) or resolves("www." + host)):
            cand(host, "stejný název")

    found = []   # (host, zdroj, úroveň, důvod)
    for host, src in cands.items():
        other = Site(host).load()
        if not other.ok:
            if src == "hreflang":                            # hreflang bez načtení – i tak silný signál
                found.append((host, src, "pravdepodobne", "jen hreflang (web nenačten)"))
            continue
        fh = other.final_host
        if fh == base_host or split_domain(fh) == (brand, own_tld):
            continue                                         # jen přesměrování na hlavní web
        if src == "stejný název" and is_parked(other.html):
            continue                                         # zaparkovaná / prázdná doména
        level, why = compare(main, other)
        if not level and src in ("hreflang", "odkaz z webu"):
            level, why = "pravdepodobne", src
        show = fh if split_domain(fh)[0] == split_domain(host)[0] else host
        if level:
            found.append((show, src, level, why))
        else:
            res["nejiste"].append(f"{host} [{other.operator_label() or 'provozovatel nezjištěn'}]")

    dedup = {}
    for h, src, lvl, why in found:
        if h not in dedup or (lvl == "ano" and dedup[h][1] != "ano"):
            dedup[h] = (src, lvl, why)
    order = lambda h: (0 if dedup[h][1] == "ano" else 1,
                       TLDS.index(split_domain(h)[1]) if split_domain(h)[1] in TLDS else 99)
    hosts = sorted(dedup, key=order)
    res["domeny"] = [{"domena": h, "zdroj": dedup[h][0], "stejny_provozovatel": dedup[h][1],
                      "duvod": dedup[h][2]} for h in hosts]
    same = [h for h in hosts if dedup[h][1] == "ano"]
    countries = lambda hs: len({split_domain(h)[1] for h in hs} - NON_COUNTRY - {own_tld})
    res["pocet"] = len(hosts)
    res["pocet_zemi"] = countries(hosts)
    res["pocet_stejny_provozovatel"] = len(same)
    res["zeme_stejny_provozovatel"] = countries(same)
    res["stav"] = "ok"
    return res


def cmd_intl(args):
    domains, header = read_domains(args.input)
    if args.limit:
        domains = domains[: args.limit]
    log(f"načteno {len(domains)} domén")
    cache_file = args.out + ".jsonl"
    done = {}
    if os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if "domeny" in r:                        # starý formát cache ignorujeme
                        done[r["domena"]] = r
                except Exception:
                    pass
        log(f"z předchozího běhu hotovo {len(done)}")
    todo = list(dict.fromkeys(d for d, _ in domains if d not in done))
    tlds = args.tlds.split(",") if args.tlds else TLDS
    with open(cache_file, "a", encoding="utf-8") as cf, ThreadPoolExecutor(args.workers) as ex:
        only = None
        if args.platformy:
            only = {"" if p.strip().lower() == "vlastni" else p.strip().lower()
                    for p in args.platformy.split(",") if p.strip()}
        futs = {ex.submit(analyse, d, tlds, args.deep, args.vcetne_velkych, only): d for d in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                r = fu.result()
            except Exception as e:
                r = {"domena": futs[fu], "stav": f"chyba: {e}", "domeny": [], "nejiste": []}
            done[r["domena"]] = r
            if not r.get("stav", "").startswith("vyřazeno"):   # vyřazené se při jiném filtru přepočítají
                cf.write(json.dumps(r, ensure_ascii=False) + "\n")
            cf.flush()
            if i % 50 == 0 or i == len(todo):
                log(f"{i}/{len(todo)}")

    extra_cols = header or []
    rows = []
    for d, orig in domains:
        r = done.get(d)
        if not r or r.get("stav") != "ok":
            continue
        n = r["pocet_stejny_provozovatel"] if args.same_operator else r["pocet"]
        if n < args.min:
            continue
        same = [x for x in r["domeny"] if x["stejny_provozovatel"] == "ano"]
        prob = [x for x in r["domeny"] if x["stejny_provozovatel"] != "ano"]
        row = {"domena": d,
               "provozovatel": r["provozovatel"],
               "platforma": r.get("platforma", ""),
               "zeme_stejny_provozovatel": r["zeme_stejny_provozovatel"],
               "pocet_stejny_provozovatel": r["pocet_stejny_provozovatel"],
               "pocet_zemi": r["pocet_zemi"],
               "pocet_zahranicnich_domen": r["pocet"],
               "domeny_stejny_provozovatel": " | ".join(f"{x['domena']} ({x['duvod']})" for x in same),
               "domeny_pravdepodobne": " | ".join(f"{x['domena']} ({x['duvod']})" for x in prob),
               "nejiste_overit_rucne": " | ".join(r["nejiste"]),
               "titulek_webu": r["titulek"]}
        for i, h in enumerate(extra_cols):
            if h and h.lower() != "domena" and i < len(orig):
                row["vstup_" + h] = orig[i]
        rows.append(row)
    stats = {}
    for d, _ in domains:
        st = (done.get(d) or {}).get("stav", "?")
        stats[st] = stats.get(st, 0) + 1
    log("stav domén: " + ", ".join(f"{k} {v}" for k, v in sorted(stats.items(), key=lambda x: -x[1])))
    rows.sort(key=lambda x: (x["zeme_stejny_provozovatel"], x["pocet_stejny_provozovatel"],
                             x["pocet_zemi"], x["pocet_zahranicnich_domen"]), reverse=True)
    if not rows:
        log("žádný e-shop nesplnil --min")
        return
    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    log(f"hotovo: {len(rows)} e-shopů -> {args.out}")


# --------------------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("shops", help="seznam obchodů z Heureka.cz")
    s.add_argument("--out", default="shops.csv")
    s.add_argument("--min-wait", type=float, default=4.0, help="min. pauza mezi požadavky (s)")
    s.add_argument("--max-wait", type=float, default=9.0, help="max. pauza mezi požadavky (s)")
    s.add_argument("--max-requests", type=int, default=1500, help="limit požadavků na jedno spuštění")
    s.add_argument("--max-pages", type=int, default=400, help="max. stránek na kategorii (fallback)")
    s.add_argument("--kategorie", default="",
                   help="jen vybrané kategorie – části URL oddělené čárkou, např. elektronika,sport,zvirata")
    s.set_defaults(func=cmd_shops)

    i = sub.add_parser("intl", help="dohledání zahraničních domén")
    i.add_argument("input", help="CSV/TXT s doménami (sloupec 'domena' nebo první sloupec)")
    i.add_argument("--out", default="intl_vysledky.csv")
    i.add_argument("--min", type=int, default=1, help="min. počet zahraničních domén ve výstupu")
    i.add_argument("--workers", type=int, default=12, help="paralelní vlákna (různé weby)")
    i.add_argument("--tlds", default="", help="vlastní seznam TLD, např. sk,pl,hu,de,at,ro")
    i.add_argument("--deep", action="store_true", help="hledat i podobné názvy (víc falešných shod)")
    i.add_argument("--same-operator", action="store_true",
                   help="--min počítat jen z domén s doloženým stejným provozovatelem (IČO/DIČ/firma)")
    i.add_argument("--limit", type=int, default=0, help="zpracovat jen prvních N domén (test)")
    i.add_argument("--vcetne-velkych", action="store_true",
                   help="nevyřazovat velké e-shopy (Alza, Mall, Notino… viz BIG_SHOPS)")
    i.add_argument("--platformy", default="",
                   help="jen e-shopy na těchto platformách, např. shoptet,upgates,woocommerce,shopify,prestashop "
                        "('vlastni' = nerozpoznaná platforma)")
    i.set_defaults(func=cmd_intl)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
