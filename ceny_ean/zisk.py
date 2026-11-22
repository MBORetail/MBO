"""Výpočet zisku na položku a hrubé marže.

Prodejní cena = nejnižší cena konkurence včetně dopravy (tj. cena, za kterou byste museli prodávat,
abyste byli nejlevnější). Pro každý marketplace:

  tržba bez DPH   = prodejní cena / (1 + DPH země)
  poplatek        = prodejní cena × provize % + fixní poplatek     (marketplacy počítají z ceny vč. DPH a dopravy)
  náklady         = nákupní cena bez DPH + vlastní náklady na dopravu
  zisk            = tržba bez DPH − poplatek − náklady
  marže %         = zisk / tržba bez DPH × 100

Vše v EUR (přepočet kurzem ČNB). Sazby a poplatky se nastavují v .env (viz VYCHOZI níže).
"""
import os

# klíč v .env: (výchozí hodnota, popis)
VYCHOZI = {
    "DPH_DE": (19.0, "DPH Německo %"),
    "DPH_CZ": (21.0, "DPH Česko %"),
    "DPH_PL": (23.0, "DPH Polsko %"),
    "PROVIZE_KAUFLAND_PARFEM": (10.0, "Kaufland (DE i CZ) provize % – parfémy"),
    "PROVIZE_KAUFLAND_KOSMETIKA": (13.0, "Kaufland (DE i CZ) provize % – kosmetika"),
    "PROVIZE_EBAY_PARFEM": (11.0, "eBay.de provize % – parfémy"),
    "PROVIZE_EBAY_KOSMETIKA": (12.0, "eBay.de provize % – kosmetika"),
    "PROVIZE_ALLEGRO_PARFEM": (10.0, "Allegro provize % – parfémy (ODHAD)"),
    "PROVIZE_ALLEGRO_KOSMETIKA": (10.0, "Allegro provize % – kosmetika (ODHAD)"),
    "FIX_EBAY_DE": (0.35, "eBay.de fixní poplatek za objednávku EUR"),
    "FIX_KAUFLAND_DE": (0.0, "Kaufland.de fixní poplatek za položku EUR"),
    "FIX_KAUFLAND_CZ": (0.0, "Kaufland.cz fixní poplatek za položku EUR"),
    "FIX_ALLEGRO_PL": (0.0, "Allegro.pl fixní poplatek EUR"),
    "FIX_ALLEGRO_CZ": (0.0, "Allegro.cz fixní poplatek EUR"),
    "DOPRAVA_DE": (0.0, "vaše náklady na dopravu 1 ks do DE, EUR"),
    "DOPRAVA_CZ": (0.0, "vaše náklady na dopravu 1 ks do CZ, EUR"),
    "DOPRAVA_PL": (0.0, "vaše náklady na dopravu 1 ks do PL, EUR"),
}
# Kategorie pro provize: "parfem" nebo "kosmetika".
# Pořadí: sloupec "kategorie" ve vstupu > kategorie z Kaufland Assortment Insight > klíčová slova v názvu.
SLOVA_PARFEM = ("parfum", "parfüm", "parfém", "perfum", "eau de", "edp", "edt", "cologne", "duft",
                "fragrance", "extrait", "body mist", "körperspray", "deo parfum", "toaletní voda", "parfémová voda")
NAZVY_SLOUPCE_KATEGORIE = ("kategorie", "category", "kategorie zbozi", "kategorie zboží", "typ")


def urci_kategorii(vstup_hodnota=None, insight_kategorie=None, titul=None):
    """Vrátí (kategorie, zdroj)."""
    if vstup_hodnota and str(vstup_hodnota).strip():
        v = str(vstup_hodnota).lower()
        return ("parfem" if any(w in v for w in ("parf", "perf", "duft", "vůn", "vun")) else "kosmetika"), "vstup"
    for text, zdroj in ((insight_kategorie, "Kaufland"), (titul, "název")):
        if text:
            t = " " + str(text).lower() + " "
            if any(w in t for w in SLOVA_PARFEM):
                return "parfem", zdroj
    return "kosmetika", "výchozí"


NAZVY_SLOUPCE_NAKUP = ("nakupni cena", "nákupní cena", "nakup", "nákup", "nakupni", "nákupní",
                       "nc", "purchase price", "einkaufspreis", "cost", "cena nakup", "cena nákup")


def _f(v):
    try:
        return float(str(v).replace(",", ".").replace(" ", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


class Parametry:
    def __init__(self):
        self.hodnoty = {}
        self.z_env = set()
        for k, (vych, _) in VYCHOZI.items():
            v = _f(os.environ.get(k))
            if v is not None:
                self.z_env.add(k)
            self.hodnoty[k] = vych if v is None else v
        self.nakup_mena = (os.environ.get("NAKUP_MENA") or "CZK").strip().upper()
        self.nakup_s_dph = (os.environ.get("NAKUP_S_DPH") or "ne").strip().lower() in ("ano", "1", "true", "yes")
        self.dph_nakup = _f(os.environ.get("DPH_NAKUP")) or 21.0
        self.sloupec_nakup = (os.environ.get("SLOUPEC_NAKUP") or "").strip()

    def najdi_sloupec(self, dalsi_sloupce):
        if self.sloupec_nakup:
            return next((s for s in dalsi_sloupce if s.strip().lower() == self.sloupec_nakup.lower()), None)
        return next((s for s in dalsi_sloupce if s.strip().lower() in NAZVY_SLOUPCE_NAKUP), None)

    def nakup_eur(self, hodnota, kurzy):
        c = _f(hodnota)
        if c is None:
            return None
        if self.nakup_s_dph:
            c = c / (1 + self.dph_nakup / 100)
        if self.nakup_mena not in kurzy or "EUR" not in kurzy:
            return None
        return c * kurzy[self.nakup_mena] / kurzy["EUR"]

    def najdi_sloupec_kategorie(self, dalsi_sloupce):
        return next((s for s in dalsi_sloupce if s.strip().lower() in NAZVY_SLOUPCE_KATEGORIE), None)

    def provize(self, mp, kategorie):
        zaklad = mp.split("_")[0].upper()          # EBAY / KAUFLAND / ALLEGRO
        return self.hodnoty.get(f"PROVIZE_{zaklad}_{kategorie.upper()}", 0.0)

    def trzba(self, mp, prodej_eur):
        if prodej_eur is None:
            return None
        dph = self.hodnoty.get(f"DPH_{mp.split('_')[1].upper()}", 0.0)
        return round(prodej_eur / (1 + dph / 100), 2)

    def spocti(self, mp, prodej_eur, nakup_eur, kategorie="kosmetika"):
        """Vrátí (zisk EUR, marže %) nebo (None, None)."""
        if prodej_eur is None or nakup_eur is None:
            return None, None
        zeme = mp.split("_")[1].upper()
        dph = self.hodnoty.get(f"DPH_{zeme}", 0.0)
        provize = self.provize(mp, kategorie)
        fix = self.hodnoty.get(f"FIX_{mp.upper()}", 0.0)
        doprava = self.hodnoty.get(f"DOPRAVA_{zeme}", 0.0)
        trzba = prodej_eur / (1 + dph / 100)
        poplatek = prodej_eur * provize / 100 + fix
        zisk = trzba - poplatek - nakup_eur - doprava
        return round(zisk, 2), (round(zisk / trzba * 100, 1) if trzba else None)

    def radky_popisu(self):
        out = [["Nákupní cena: měna", self.nakup_mena, "NAKUP_MENA"],
               ["Nákupní cena je s DPH", "ano" if self.nakup_s_dph else "ne", "NAKUP_S_DPH"]]
        for k, (_, popis) in VYCHOZI.items():
            out.append([popis, self.hodnoty[k], k + ("" if k in self.z_env else "  (výchozí)")])
        return out
