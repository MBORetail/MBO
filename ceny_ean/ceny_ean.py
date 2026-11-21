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
def ean_platny(ean):
    if not ean.isdigit() or len(ean) not in (8, 12, 13, 14):
        return False
    cisla = [int(c) for c in ean]
    soucet = sum(c * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(cisla[:-1])))
    return (10 - soucet % 10) % 10 == cisla[-1]


def normalizuj_ean(v):
    s = str(v or "").strip()
    if s.endswith(".0"):          # Excel uloží EAN jako číslo
        s = s[:-2]
    return "".join(ch for ch in s if ch.isdigit())


def nacti_vstup(cesta):
    """Vrátí (seznam slovníků s klíčem 'ean', názvy dalších sloupců)."""
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
    i_ean = mala.index("ean") if "ean" in mala else 0
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
                 n.titul if n else None, n.url if n else None,
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
        db.uloz(beh, ean, adapter.klic, vys)
        prubeh(adapter.klic, i, len(fronta), ean, vys)
    return len(fronta)


def export_xlsx(cesta, vstup, dalsi_sloupce, klice, vys, predchozi, kurzy):
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

    w3 = wb.create_sheet("Kurzy ČNB")
    w3.append(["měna", "CZK za 1 jednotku"])
    for m in ("EUR", "PLN"):
        if m in kurzy:
            w3.append([m, round(kurzy[m], 4)])
    wb.save(cesta)


def tisk_jednoho(ean, adaptery):
    for k, a in adaptery.items():
        v = a.hledej(ean)
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
        print(f"! {len(spatne)} EAN má neplatný kontrolní součet (dotáži se i tak), "
              f"např. {', '.join(spatne[:5])}", file=sys.stderr)

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
    pred = db.predchozi_beh(beh)
    vystup = a.vystup or os.path.join(ADRESAR, f"ceny_{beh}.xlsx")
    export_xlsx(vystup, vstup, dalsi, list(adaptery), db.vysledky(beh), db.vysledky(pred) if pred else {}, kurzy)
    print(f"Hotovo: {vystup}" + (f" (porovnáno s během {pred})" if pred else ""))


if __name__ == "__main__":
    main()
