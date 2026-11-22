#!/usr/bin/env python3
"""
ceny_ean.py – nejnižší ceny konkurence podle EAN na eBay.de, Kaufland.de/.cz a Allegro.pl/.cz.

Použití:
  python ceny_ean.py eany.csv                  # celá dávka, výstup ceny_<týden>.xlsx
  python ceny_ean.py eany.xlsx --jen ebay_de,kaufland_de
  python ceny_ean.py 4006381333931             # jeden EAN, výpis do terminálu
  python ceny_ean.py eany.csv --debug          # uloží surové odpovědi API do debug/

Vstup: CSV nebo XLSX. EAN se bere ze sloupce "ean" (jinak z prvního sloupce),
ostatní sloupce (kód, nákupní cena…) se přenesou do výstupu.

Doprava se počítá podle domény: ebay_de/kaufland_de → Německo, kaufland_cz/allegro_cz → ČR,
allegro_pl → Polsko.

Navazování: výsledky se ukládají průběžně do data/ceny.sqlite pod označením běhu
(výchozí = kalendářní týden, např. 2026-W47). Přerušená dávka po novém spuštění
pokračuje jen chybějícími EAN; --znovu vynutí nové dotazy.

Klíče k API v souboru .env (viz .env.example).
Instalace:  pip install requests openpyxl
"""
import argparse
import csv
import datetime as dt
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from mp import MARKETPLACY, ZEME_DOPRAVY
from mp.allegro import Allegro
from mp.ebay import EbayDE
from mp.kaufland import Kaufland
from mp.kaufland_insight import nacti_insight
from zisk import Parametry

ADRESAR = os.path.dirname(os.path.abspath(__file__))
DB_CESTA = os.path.join(ADRESAR, "data", "ceny.sqlite")
CNB_URL = ("https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/"
           "kurzy-devizoveho-trhu/denni_kurz.txt")


# --- konfigurace -------------------------------------------------------------
def nacti_env(cesta=os.path.join(ADRESAR, ".env")):
    if not os.path.exists(cesta) and os.path.exists(cesta + ".txt"):
        cesta += ".txt"   # Poznámkový blok rád přidá .txt
    if os.path.exists(cesta):
        with open(cesta, encoding="utf-8-sig") as f:   # -sig: snese BOM z Poznámkového bloku
            for radek in f:
                radek = radek.strip()
                if radek and not radek.startswith("#") and "=" in radek:
                    k, v = radek.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def vytvor_adaptery(klice, debug_dir):
    vlastni = os.environ.get("VLASTNI_PRODEJCI", "").split(",")
    spol = {"vlastni_prodejci": vlastni, "debug_dir": debug_dir}
    out, chybi = {}, []
    for k in klice:
        if k == "ebay_de":
            if os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"):
                out[k] = EbayDE(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"], **spol)
            else:
                chybi.append(f"{k} (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)")
        elif k.startswith("kaufland_"):
            if os.environ.get("KAUFLAND_CLIENT_KEY") and os.environ.get("KAUFLAND_SECRET_KEY"):
                out[k] = Kaufland(k.split("_")[1], os.environ["KAUFLAND_CLIENT_KEY"],
                                  os.environ["KAUFLAND_SECRET_KEY"], **spol)
            else:
                chybi.append(f"{k} (KAUFLAND_CLIENT_KEY / KAUFLAND_SECRET_KEY)")
        elif k.startswith("allegro_"):
            out[k] = Allegro(k.split("_")[1], **spol)
    return out, chybi


# --- vstup -------------------------------------------------------------------
from mp.base import ean_platny  # noqa: E402


def normalizuj_ean(v):
    s = str(v or "").strip()
    if s.endswith(".0"):          # Excel uloží EAN jako číslo
        s = s[:-2]
    return "".join(ch for ch in s if ch.isdigit())


def nacti_vstup(cesta):
    """Vrátí (seznam slovníků s klíčem 'ean', názvy dalších sloupců)."""
    if cesta.lower().endswith(".xls"):
        sys.exit("Starý formát .xls neumím načíst – v Excelu ho prosím uložte jako .xlsx nebo .csv.")
    if cesta.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        ws = load_workbook(cesta, read_only=True, data_only=True).active
        radky = [["" if c is None else c for c in r] for r in ws.iter_rows(values_only=True)]
    else:
        with open(cesta, encoding="utf-8-sig", newline="") as f:
            ukazka = f.read(4096)
            f.seek(0)
            try:
                dialekt = csv.Sniffer().sniff(ukazka, delimiters=",;\t")
            except csv.Error:
                dialekt = csv.excel
            radky = list(csv.reader(f, dialekt))
    radky = [r for r in radky if any(str(c).strip() for c in r)]
    if not radky:
        return [], []
    hlavicka = [str(c).strip() for c in radky[0]]
    if normalizuj_ean(hlavicka[0]) and ean_platny(normalizuj_ean(hlavicka[0])):
        hlavicka = ["ean"] + [f"sloupec{i}" for i in range(2, len(radky[0]) + 1)]
        data = radky
    else:
        data = radky[1:]
    mala = [h.lower() for h in hlavicka]
    # sloupec "ean", jinak první sloupec, jehož název obsahuje "ean" / "gtin" / "čárový kód", jinak první
    i_ean = mala.index("ean") if "ean" in mala else next(
        (i for i, h in enumerate(mala) if "ean" in h or "gtin" in h or "čárov" in h or "carov" in h), 0)
    dalsi = [h for i, h in enumerate(hlavicka) if i != i_ean]
    out, videno = [], set()
    for r in data:
        ean = normalizuj_ean(r[i_ean] if i_ean < len(r) else "")
        if not ean or ean in videno:
            continue
        videno.add(ean)
        zaznam = {"ean": ean}
        for i, h in enumerate(hlavicka):
            if i != i_ean:
                zaznam[h] = r[i] if i < len(r) else ""
        out.append(zaznam)
    return out, dalsi


# --- databáze ----------------------------------------------------------------
class Databaze:
    def __init__(self, cesta=DB_CESTA):
        os.makedirs(os.path.dirname(cesta), exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(cesta, check_same_thread=False)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS vysledek (
            beh TEXT, ean TEXT, marketplace TEXT, status TEXT, zprava TEXT,
            cena REAL, doprava REAL, celkem REAL, mena TEXT, pocet INTEGER,
            prodejce TEXT, titul TEXT, url TEXT, cas TEXT,
            PRIMARY KEY (beh, ean, marketplace));
        CREATE TABLE IF NOT EXISTS kurz (datum TEXT, mena TEXT, czk REAL, PRIMARY KEY (datum, mena));
        """)

    def hotove(self, beh, marketplace):
        with self.lock:
            return {r[0] for r in self.db.execute(
                "SELECT ean FROM vysledek WHERE beh=? AND marketplace=? AND status IN ('ok','nenalezeno')",
                (beh, marketplace))}

    def uloz(self, beh, ean, mp, vys):
        n = vys.nejlevnejsi
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO vysledek VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (beh, ean, mp, vys.status, vys.zprava,
                 n.cena if n else None, n.doprava if n else None, n.celkem if n else None,
                 n.mena if n else None, len(vys.nabidky), n.prodejce if n else None,
                 (n.titul if n else None) or vys.titul or None, n.url if n else None,
                 dt.datetime.now().isoformat(timespec="seconds")))
            self.db.commit()

    def vysledky(self, beh):
        with self.lock:
            cur = self.db.execute("SELECT * FROM vysledek WHERE beh=?", (beh,))
            sloupce = [c[0] for c in cur.description]
            return {(r[1], r[2]): dict(zip(sloupce, r)) for r in cur}

    def predchozi_beh(self, beh):
        with self.lock:
            r = self.db.execute("SELECT MAX(beh) FROM vysledek WHERE beh < ?", (beh,)).fetchone()
            return r[0] if r else None


# --- měny (kurzy ČNB) ----------------------------------------------------------
def kurzy_cnb(db):
    dnes = dt.date.today().isoformat()
    with db.lock:
        ulozene = dict(db.db.execute("SELECT mena, czk FROM kurz WHERE datum=?", (dnes,)).fetchall())
    if ulozene:
        return ulozene
    kurzy = {"CZK": 1.0}
    try:
        r = requests.get(CNB_URL, timeout=20)
        r.raise_for_status()
        for radek in r.text.splitlines()[2:]:
            p = radek.split("|")
            if len(p) == 5:
                kurzy[p[3]] = float(p[4].replace(",", ".")) / float(p[2])
    except (requests.RequestException, ValueError) as e:
        print(f"! Kurzy ČNB se nepodařilo stáhnout ({e}), použiji poslední uložené.", file=sys.stderr)
        with db.lock:
            r = db.db.execute("SELECT MAX(datum) FROM kurz").fetchone()
            if r and r[0]:
                return dict(db.db.execute("SELECT mena, czk FROM kurz WHERE datum=?", (r[0],)).fetchall())
        return kurzy
    with db.lock:
        db.db.executemany("INSERT OR REPLACE INTO kurz VALUES (?,?,?)",
                          [(dnes, m, k) for m, k in kurzy.items()])
        db.db.commit()
    return kurzy


def prevod(castka, mena, cil, kurzy):
    if castka is None or mena not in kurzy or cil not in kurzy:
        return None
    return round(castka * kurzy[mena] / kurzy[cil], 2)


# --- běh -----------------------------------------------------------------------
def zpracuj_marketplace(adapter, eany, beh, db, znovu, prubeh):
    hotove = set() if znovu else db.hotove(beh, adapter.klic)
    fronta = [e for e in eany if e not in hotove]
    for i, ean in enumerate(fronta, 1):
        try:
            vys = adapter.hledej(ean)
        except Exception as e:  # jeden špatný EAN nesmí shodit dávku
            from mp import Vysledek
            vys = Vysledek("chyba", zprava=f"{type(e).__name__}: {e}"[:300])
        if getattr(adapter, "vycerpano", False):
            print(f"! {adapter.klic}: denní limit API je vyčerpaný – zbývá {len(fronta) - i + 1} EAN. "
                  f"Spusťte stejný příkaz zítra, doplní se jen chybějící.", file=sys.stderr)
            break
        db.uloz(beh, ean, adapter.klic, vys)
        prubeh(adapter.klic, i, len(fronta), ean, vys)
    return len(fronta)


NAZVY_MP = {"ebay_de": "eBay.de", "kaufland_de": "Kaufland.de", "kaufland_cz": "Kaufland.cz",
            "allegro_pl": "Allegro.pl", "allegro_cz": "Allegro.cz"}


def prehled(wb, vstup, klice, vys, kurzy=None, insight=None, param=None, dalsi_sloupce=()):
    """První list: EAN, název, kategorie, nákupní cena; pro každý web cena, zisk, marže
    a sloupec "objednat ks" se vzorci pro nákup a zisk za objednané množství; souhrn objednávky;
    údaje z Kaufland Assortment Insight."""
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter as L
    from zisk import urci_kategorii
    kurzy = kurzy or {}
    insight = insight or {}
    sl_nakup = param.najdi_sloupec(dalsi_sloupce) if param else None
    sl_kat = param.najdi_sloupec_kategorie(dalsi_sloupce) if param else None
    objednavka = bool(sl_nakup)

    # --- rozvržení sloupců ---
    hlavicka = ["EAN", "Název produktu", "Kategorie (provize)"]
    if objednavka:
        hlavicka.append("Nákupní cena bez DPH EUR")
    c_nakup = len(hlavicka) if objednavka else None
    sloupce = {}   # mp -> {nazev_pole: index sloupce (1-based)}
    for k in klice:
        m = NAZVY_MP.get(k, k)
        pole = ["cena", "doprava", "celkem", "měna", "celkem EUR"]
        if objednavka:
            pole += ["tržba bez DPH EUR", "provize %", "zisk/ks EUR", "marže %"]
        pole += ["prodejce"]
        if objednavka:
            pole += ["OBJEDNAT ks", "nákup celkem EUR", "zisk celkem EUR"]
        sloupce[k] = {}
        for p_ in pole:
            hlavicka.append(f"{m} {p_}")
            sloupce[k][p_] = len(hlavicka)
    souhrn = {}
    if objednavka:
        for p_ in ("Objednat celkem ks", "Nákup celkem EUR", "Očekávaný zisk celkem EUR", "Očekávaná marže %"):
            hlavicka.append(p_)
            souhrn[p_] = len(hlavicka)
    s_insight = bool(insight)
    if s_insight:
        hlavicka += ["Kaufland pořadí prodejnosti", "Kaufland bestseller", "Poptávka mimo Kaufland",
                     "Buy Box aktuálně EUR", "Buy Box Ø 30 dní EUR", "Buy Box Ø 90 dní EUR"]

    ws = wb.create_sheet("Přehled", 0)
    ws.append(hlavicka)
    modra, zluta = PatternFill("solid", fgColor="DDEBF7"), PatternFill("solid", fgColor="FFF2CC")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = modra
    for k in klice:
        if "OBJEDNAT ks" in sloupce[k]:
            ws.cell(1, sloupce[k]["OBJEDNAT ks"]).fill = zluta
    cervena = Font(color="C00000")

    for z in vstup:
        ean = z["ean"]
        r = ws.max_row + 1
        ins = insight.get(ean) or {}
        nazev = next((vys[(ean, k)]["titul"] for k in klice
                      if vys.get((ean, k)) and vys[(ean, k)]["titul"]), "") or (ins.get("item_name") or "")
        if not nazev:   # název ze vstupního souboru, pokud tam je
            nazev = next((str(v) for h, v in z.items()
                          if h.lower() in ("nazev", "název", "name", "produkt", "popis") and v), "")
        kat, zdroj = urci_kategorii(z.get(sl_kat) if sl_kat else None, ins.get("category"), nazev)
        radek = {1: ean, 2: nazev, 3: f"{'parfém' if kat == 'parfem' else 'kosmetika'} ({zdroj})"}
        nakup = None
        if objednavka:
            nakup = param.nakup_eur(z.get(sl_nakup), kurzy)
            radek[c_nakup] = round(nakup, 2) if nakup is not None else None
        zaporne = []
        for k in klice:
            sl = sloupce[k]
            v = vys.get((ean, k))
            if v and v["status"] == "ok":
                castka = v["celkem"] if v["celkem"] is not None else v["cena"]
                eur = prevod(castka, v["mena"], "EUR", kurzy)
                radek.update({sl["cena"]: v["cena"], sl["doprava"]: v["doprava"], sl["celkem"]: v["celkem"],
                              sl["měna"]: v["mena"], sl["celkem EUR"]: eur, sl["prodejce"]: v["prodejce"]})
                if objednavka:
                    zisk, marze = param.spocti(k, eur, nakup, kat)
                    radek.update({sl["tržba bez DPH EUR"]: param.trzba(k, eur),
                                  sl["provize %"]: param.provize(k, kat),
                                  sl["zisk/ks EUR"]: zisk, sl["marže %"]: marze})
                    if zisk is not None and zisk < 0:
                        zaporne += [sl["zisk/ks EUR"], sl["marže %"]]
            else:
                radek[sl["cena"]] = v["status"] if v else ""
            if objednavka:
                ks = f"{L(sl['OBJEDNAT ks'])}{r}"
                radek[sl["nákup celkem EUR"]] = f'=IF(AND(N({ks})>0,ISNUMBER({L(c_nakup)}{r})),{ks}*{L(c_nakup)}{r},"")'
                radek[sl["zisk celkem EUR"]] = f'=IF(AND(N({ks})>0,ISNUMBER({L(sl["zisk/ks EUR"])}{r})),{ks}*{L(sl["zisk/ks EUR"])}{r},"")'
        if objednavka:
            ks_bunky = [f"{L(sloupce[k]['OBJEDNAT ks'])}{r}" for k in klice]
            nak = [f"{L(sloupce[k]['nákup celkem EUR'])}{r}" for k in klice]
            zis = [f"{L(sloupce[k]['zisk celkem EUR'])}{r}" for k in klice]
            trz = "+".join(f"N({L(sloupce[k]['OBJEDNAT ks'])}{r})*N({L(sloupce[k]['tržba bez DPH EUR'])}{r})" for k in klice)
            radek[souhrn["Objednat celkem ks"]] = f"=SUM({','.join(ks_bunky)})"
            radek[souhrn["Nákup celkem EUR"]] = f"=SUM({','.join(nak)})"
            radek[souhrn["Očekávaný zisk celkem EUR"]] = f"=SUM({','.join(zis)})"
            zc = f"{L(souhrn['Očekávaný zisk celkem EUR'])}{r}"
            radek[souhrn["Očekávaná marže %"]] = f'=IF(({trz})>0,ROUND({zc}/({trz})*100,1),"")'
        if s_insight:
            def eur100(c):
                return round(c / 100, 2) if isinstance(c, (int, float)) else None
            base = len(hlavicka) - 5
            for i, val in enumerate([ins.get("sales_rank"),
                                     {True: "ano", False: "ne"}.get(ins.get("kaufland_bestseller")),
                                     ins.get("external_demand_indicator"),
                                     eur100(ins.get("buybox_price_current")),
                                     eur100(ins.get("buybox_price_l_30_day")),
                                     eur100(ins.get("buybox_price_l_90_day"))]):
                radek[base + i] = val
        for col, val in radek.items():
            ws.cell(r, col, val)
        for col in zaporne:
            ws.cell(r, col).font = cervena
        for k in klice:
            if objednavka:
                ws.cell(r, sloupce[k]["OBJEDNAT ks"]).fill = zluta

    # řádek součtů objednávky
    if objednavka and vstup:
        prvni, posledni, r = 2, ws.max_row, ws.max_row + 1
        ws.cell(r, 1, "CELKEM").font = Font(bold=True)
        sumy = [sloupce[k][p_] for k in klice for p_ in ("OBJEDNAT ks", "nákup celkem EUR", "zisk celkem EUR")]
        sumy += [souhrn[p_] for p_ in ("Objednat celkem ks", "Nákup celkem EUR", "Očekávaný zisk celkem EUR")]
        for col in sumy:
            c = ws.cell(r, col, f"=SUM({L(col)}{prvni}:{L(col)}{posledni})")
            c.font = Font(bold=True)
        # SUMPRODUCT s oddělenými poli bere text/prázdné buňky jako 0
        trz = "+".join(f"SUMPRODUCT({L(sloupce[k]['OBJEDNAT ks'])}{prvni}:{L(sloupce[k]['OBJEDNAT ks'])}{posledni},"
                       f"{L(sloupce[k]['tržba bez DPH EUR'])}{prvni}:{L(sloupce[k]['tržba bez DPH EUR'])}{posledni})"
                       for k in klice)
        zc = f"{L(souhrn['Očekávaný zisk celkem EUR'])}{r}"
        c = ws.cell(r, souhrn["Očekávaná marže %"], f'=IFERROR(ROUND({zc}/({trz})*100,1),"")')
        c.font = Font(bold=True)

    ws.freeze_panes = "D2"
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 50
    ws.column_dimensions["C"].width = 22
    for i in range(4, len(hlavicka) + 1):
        ws.column_dimensions[L(i)].width = 14

    if param:
        wp = wb.create_sheet("Parametry výpočtu", 1)
        wp.append(["parametr", "hodnota", "nastavení v .env"])
        for c in wp[1]:
            c.font = Font(bold=True)
        wp.append(["Sloupec s nákupní cenou ve vstupu", sl_nakup or "NENALEZEN – zisk se nepočítá", "SLOUPEC_NAKUP"])
        wp.append(["Sloupec s kategorií ve vstupu", sl_kat or "není – kategorie z Kauflandu / názvu", ""])
        for rr in param.radky_popisu():
            wp.append(rr)
        wp.append([])
        for t in ("Prodejní cena = nejnižší cena konkurence vč. dopravy, přepočtená na EUR kurzem ČNB.",
                  "Tržba bez DPH = prodejní cena / (1 + DPH země prodeje).",
                  "Zisk/ks = tržba bez DPH − prodejní cena × provize % − fixní poplatek − nákup bez DPH − vaše doprava.",
                  "Marže % = zisk / tržba bez DPH.",
                  "Provize podle kategorie: parfém / kosmetika – určeno ze sloupce kategorie ve vstupu, "
                  "jinak z kategorie Kauflandu, jinak podle názvu (Parfum, Eau de…, EDP, EDT…).",
                  "Do žlutých sloupců OBJEDNAT ks zapište množství – nákup, zisk a marže za objednávku se přepočítají.",
                  "Hodnoty označené (výchozí) nejsou nastavené v .env."):
            wp.append([t])
        wp.column_dimensions["A"].width = 70
        wp.column_dimensions["B"].width = 22
        wp.column_dimensions["C"].width = 28


def export_xlsx(cesta, vstup, dalsi_sloupce, klice, vys, predchozi, kurzy, insight=None, param=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Ceny"
    pole = [("cena", "cena"), ("doprava", "doprava"), ("celkem", "celkem"), ("mena", "měna"),
            ("czk", "celkem CZK"), ("eur", "celkem EUR"), ("zmena", "změna vs. minule %"),
            ("pocet", "nabídek"), ("prodejce", "prodejce"), ("url", "odkaz"), ("status", "stav")]
    h1 = ["EAN"] + dalsi_sloupce + ["nejlevnější celkem CZK", "kde"]
    h2 = [""] * len(h1)
    for k in klice:
        for i, (_, nazev) in enumerate(pole):
            h1.append(f"{k} (doprava {ZEME_DOPRAVY[k]})" if i == 0 else "")
            h2.append(nazev)
    ws.append(h1)
    ws.append(h2)
    tucne = Font(bold=True)
    for radek in (1, 2):
        for c in ws[radek]:
            c.font = tucne
            c.fill = PatternFill("solid", fgColor="DDEBF7")
    zacatek = len(dalsi_sloupce) + 4   # EAN, další sloupce, nejlevnější, kde, pak marketplacy
    for j in range(len(klice)):
        s = zacatek + j * len(pole)
        ws.merge_cells(start_row=1, start_column=s, end_row=1, end_column=s + len(pole) - 1)
        ws.cell(1, s).alignment = Alignment(horizontal="center")

    problemy = []
    for z in vstup:
        ean = z["ean"]
        radek = [ean] + [z.get(s, "") for s in dalsi_sloupce]
        bunky, nejlepsi = [], (None, "")
        for k in klice:
            v = vys.get((ean, k))
            if not v:
                bunky += [""] * len(pole)
                continue
            castka = v["celkem"] if v["celkem"] is not None else v["cena"]
            czk = prevod(castka, v["mena"], "CZK", kurzy)
            eur = prevod(castka, v["mena"], "EUR", kurzy)
            zmena = None
            p = predchozi.get((ean, k))
            if p and v["celkem"] and p.get("celkem") and p.get("mena") == v["mena"]:
                zmena = round((v["celkem"] - p["celkem"]) / p["celkem"] * 100, 1)
            if czk is not None and (nejlepsi[0] is None or czk < nejlepsi[0]):
                nejlepsi = (czk, k)
            stav = v["status"] if v["status"] != "ok" or v["doprava"] is not None else "ok (doprava neznámá)"
            bunky += [v["cena"], v["doprava"], v["celkem"], v["mena"], czk, eur, zmena,
                      v["pocet"], v["prodejce"], v["url"], stav]
            if v["status"] in ("chyba", "nenalezeno"):
                problemy.append([ean, k, v["status"], v["zprava"]])
        ws.append(radek + [nejlepsi[0], nejlepsi[1]] + bunky)
        for j, k in enumerate(klice):
            c = ws.cell(ws.max_row, zacatek + j * len(pole) + pole.index(("url", "odkaz")))
            if c.value:
                c.hyperlink = c.value
                c.value = "odkaz"
                c.style = "Hyperlink"

    ws.freeze_panes = ws.cell(3, 2)
    for i in range(1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(i)].width = 15
    ws.column_dimensions["A"].width = 16

    w2 = wb.create_sheet("Nenalezeno a chyby")
    w2.append(["EAN", "marketplace", "stav", "zpráva"])
    for c in w2[1]:
        c.font = tucne
    for p in problemy:
        w2.append(p)
    w2.column_dimensions["A"].width = 16
    w2.column_dimensions["B"].width = 14
    w2.column_dimensions["D"].width = 80

    prehled(wb, vstup, klice, vys, kurzy, insight, param, dalsi_sloupce)

    w3 = wb.create_sheet("Kurzy ČNB")
    w3.append(["měna", "CZK za 1 jednotku"])
    for m in ("EUR", "PLN"):
        if m in kurzy:
            w3.append([m, round(kurzy[m], 4)])
    wb.save(cesta)


def tisk_jednoho(ean, adaptery):
    for k, a in adaptery.items():
        try:
            v = a.hledej(ean)
        except Exception as e:
            print(f"{k:12} CHYBA: {e}")
            continue
        n = v.nejlevnejsi
        if n:
            dop = "?" if n.doprava is None else f"{n.doprava:.2f}"
            print(f"{k:12} {n.cena:>9.2f} + {dop:>6} {n.mena}  ({len(v.nabidky)} nab.)  "
                  f"{n.prodejce}  {n.url}")
        else:
            print(f"{k:12} {v.status} {v.zprava}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vstup", help="CSV/XLSX se sloupcem ean, nebo jeden EAN")
    ap.add_argument("--jen", help="jen vybrané marketplacy, např. ebay_de,kaufland_cz "
                                  f"(výchozí: {','.join(MARKETPLACY)})")
    ap.add_argument("--beh", help="označení běhu (výchozí: kalendářní týden, např. 2026-W47)")
    ap.add_argument("--znovu", action="store_true", help="nenavazovat, dotázat se znovu na vše")
    ap.add_argument("--vystup", help="cesta k výstupnímu XLSX (výchozí ceny_<beh>.xlsx)")
    ap.add_argument("--debug", action="store_true", help="ukládat surové odpovědi API do debug/")
    ap.add_argument("--bez-insight", action="store_true", help="nestahovat Kaufland Assortment Insight")
    a = ap.parse_args()

    nacti_env()
    klice = [k.strip() for k in a.jen.split(",")] if a.jen else MARKETPLACY
    nezname = [k for k in klice if k not in MARKETPLACY]
    if nezname:
        ap.error(f"neznámý marketplace: {', '.join(nezname)}")
    adaptery, chybi = vytvor_adaptery(klice, os.path.join(ADRESAR, "debug") if a.debug else None)
    for c in chybi:
        print(f"! Přeskakuji {c} – chybí klíče v {os.path.join(ADRESAR, '.env')}", file=sys.stderr)

    if not os.path.exists(a.vstup) and normalizuj_ean(a.vstup) == a.vstup.strip():
        tisk_jednoho(a.vstup.strip(), adaptery)
        return

    vstup, dalsi = nacti_vstup(a.vstup)
    if not vstup:
        sys.exit("Ve vstupu nejsou žádné EAN.")
    spatne = [z["ean"] for z in vstup if not ean_platny(z["ean"])]
    if spatne:
        print(f"! {len(spatne)} EAN má neplatnou kontrolní číslici – Kaufland je přeskočí, eBay je zkusí "
              f"hledat jako text. Např.: {', '.join(spatne[:5])}", file=sys.stderr)

    iso = dt.date.today().isocalendar()
    beh = a.beh or f"{iso[0]}-W{iso[1]:02d}"
    db = Databaze()
    eany = [z["ean"] for z in vstup]
    print(f"Běh {beh}: {len(eany)} EAN × {len(adaptery)} marketplaců")

    tisk_lock = threading.Lock()
    pocty = {k: [0, 0] for k in adaptery}   # ok, problém

    def prubeh(k, i, n, ean, vys):
        with tisk_lock:
            pocty[k][0 if vys.status == "ok" else 1] += 1
            if i % 50 == 0 or i == n or vys.status == "chyba":
                extra = f"  [{ean}: {vys.zprava[:80]}]" if vys.status == "chyba" else ""
                print(f"  {k:12} {i}/{n}  nalezeno {pocty[k][0]}, bez ceny {pocty[k][1]}{extra}")

    # každý marketplace ve vlastním vlákně – pomalý web nezdržuje ostatní
    with ThreadPoolExecutor(max_workers=max(1, len(adaptery))) as ex:
        list(ex.map(lambda ad: zpracuj_marketplace(ad, eany, beh, db, a.znovu, prubeh), adaptery.values()))

    kurzy = kurzy_cnb(db)
    insight = {}
    if not a.bez_insight and os.environ.get("KAUFLAND_CLIENT_KEY") and \
            any(k.startswith("kaufland_") for k in adaptery):
        try:
            insight = nacti_insight(db, os.environ["KAUFLAND_CLIENT_KEY"], os.environ["KAUFLAND_SECRET_KEY"], eany)
            print(f"Assortment Insight: údaje pro {len(insight)} z {len(eany)} EAN")
        except Exception as e:
            print(f"! Assortment Insight se nepodařilo načíst: {e}", file=sys.stderr)
    param = Parametry()
    if not param.najdi_sloupec(dalsi):
        print("! Ve vstupu není sloupec s nákupní cenou (např. 'nákupní cena') – zisk a marže se nespočítají. "
              f"Sloupce ve vstupu: {', '.join(dalsi) or '(žádné)'}. Název lze nastavit v .env: SLOUPEC_NAKUP=…",
              file=sys.stderr)
    pred = db.predchozi_beh(beh)
    vystup = a.vystup or os.path.join(ADRESAR, f"ceny_{beh}.xlsx")
    export_xlsx(vystup, vstup, dalsi, list(adaptery), db.vysledky(beh), db.vysledky(pred) if pred else {},
                kurzy, insight, param)
    print(f"Hotovo: {vystup}" + (f" (porovnáno s během {pred})" if pred else ""))


if __name__ == "__main__":
    main()
