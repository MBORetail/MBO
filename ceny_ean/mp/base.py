"""Společné typy a pomocné funkce pro adaptéry marketplaců."""
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

USER_AGENT = "ceny-ean/1.0 (interni nastroj pro porovnani cen)"

# Kam se počítá doprava: každá doména = doprava do své země.
ZEME_DOPRAVY = {
    "ebay_de": "DE",
    "kaufland_de": "DE",
    "kaufland_cz": "CZ",
    "allegro_pl": "PL",
    "allegro_cz": "CZ",
}


@dataclass
class Nabidka:
    titul: str
    cena: float                      # cena zboží bez dopravy
    mena: str
    doprava: Optional[float] = None  # None = neznámá (např. eBay "CALCULATED")
    prodejce: str = ""
    stav: str = ""
    url: str = ""

    @property
    def celkem(self) -> Optional[float]:
        return None if self.doprava is None else round(self.cena + self.doprava, 2)

    def razeni(self):
        # nabídky se známou dopravou mají přednost před neznámou
        return (self.doprava is None, self.celkem if self.celkem is not None else self.cena)


@dataclass
class Vysledek:
    status: str                      # ok | nenalezeno | chyba | nepodporovano
    nabidky: list = field(default_factory=list)
    zprava: str = ""
    titul: str = ""                  # název produktu, i když nemá nabídky

    @property
    def nejlevnejsi(self) -> Optional[Nabidka]:
        return min(self.nabidky, key=Nabidka.razeni) if self.nabidky else None


class Adapter:
    """Základ adaptéru. Potomek implementuje hledej(ean) -> Vysledek."""
    klic = ""          # např. "ebay_de"
    pauza = 0.0        # minimální rozestup mezi dotazy v sekundách

    def __init__(self, vlastni_prodejci=(), debug_dir=None):
        self.vlastni = {p.strip().lower() for p in vlastni_prodejci if p.strip()}
        self.debug_dir = debug_dir
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._posledni = 0.0

    @property
    def zeme(self):
        return ZEME_DOPRAVY[self.klic]

    def je_vlastni(self, prodejce: str) -> bool:
        return prodejce.strip().lower() in self.vlastni

    def pockej(self):
        zbyva = self.pauza - (time.monotonic() - self._posledni)
        if zbyva > 0:
            time.sleep(zbyva)
        self._posledni = time.monotonic()

    def uloz_debug(self, ean, data):
        if not self.debug_dir:
            return
        os.makedirs(self.debug_dir, exist_ok=True)
        with open(os.path.join(self.debug_dir, f"{self.klic}_{ean}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    vycerpano = False   # True = denní limit API vyčerpán, další dotazy nemají smysl

    def pozadavek(self, metoda, url, pokusy=4, opakovat_429=True, **kw):
        """HTTP s opakováním při 429/5xx a výpadku spojení."""
        kw.setdefault("timeout", 30)
        for pokus in range(pokusy):
            self.pockej()
            try:
                r = self.session.request(metoda, url, **kw)
            except requests.RequestException:
                if pokus == pokusy - 1:
                    raise
                time.sleep(2 ** pokus * 2)
                continue
            if r.status_code == 429 and not opakovat_429:
                return r
            if r.status_code == 429 or r.status_code >= 500:
                if pokus == pokusy - 1:
                    return r
                cekej = int(r.headers.get("Retry-After", 0) or 0) or 2 ** pokus * 5
                time.sleep(min(cekej, 120))
                continue
            return r
        return r

    def hledej(self, ean: str) -> Vysledek:
        raise NotImplementedError


def ean_platny(ean: str) -> bool:
    """Kontrolní číslice EAN-8 / UPC-12 / EAN-13 / GTIN-14."""
    if not ean.isdigit() or len(ean) not in (8, 12, 13, 14):
        return False
    cisla = [int(c) for c in ean]
    soucet = sum(c * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(cisla[:-1])))
    return (10 - soucet % 10) % 10 == cisla[-1]


def cislo(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None
