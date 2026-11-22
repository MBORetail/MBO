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
    "PROVIZE_EBAY_DE": (12.0, "eBay.de provize % z ceny vč. dopravy – ODHAD, upravte podle kategorie"),
    "FIX_EBAY_DE": (0.35, "eBay.de fixní poplatek za objednávku EUR"),
    "PROVIZE_KAUFLAND_DE": (12.0, "Kaufland.de provize % – ODHAD, upravte podle kategorie"),
    "FIX_KAUFLAND_DE": (0.0, "Kaufland.de fixní poplatek za položku EUR"),
    "PROVIZE_KAUFLAND_CZ": (12.0, "Kaufland.cz provize % – ODHAD, upravte podle kategorie"),
    "FIX_KAUFLAND_CZ": (0.0, "Kaufland.cz fixní poplatek za položku EUR"),
    "PROVIZE_ALLEGRO_PL": (10.0, "Allegro.pl provize % – ODHAD"),
    "FIX_ALLEGRO_PL": (0.0, "Allegro.pl fixní poplatek EUR"),
    "PROVIZE_ALLEGRO_CZ": (10.0, "Allegro.cz provize % – ODHAD"),
    "FIX_ALLEGRO_CZ": (0.0, "Allegro.cz fixní poplatek EUR"),
    "DOPRAVA_DE": (0.0, "vaše náklady na dopravu 1 ks do DE, EUR"),
    "DOPRAVA_CZ": (0.0, "vaše náklady na dopravu 1 ks do CZ, EUR"),
    "DOPRAVA_PL": (0.0, "vaše náklady na dopravu 1 ks do PL, EUR"),
}
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

    def spocti(self, mp, prodej_eur, nakup_eur):
        """Vrátí (zisk EUR, marže %) nebo (None, None)."""
        if prodej_eur is None or nakup_eur is None:
            return None, None
        zeme = mp.split("_")[1].upper()
        dph = self.hodnoty.get(f"DPH_{zeme}", 0.0)
        provize = self.hodnoty.get(f"PROVIZE_{mp.upper()}", 0.0)
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
