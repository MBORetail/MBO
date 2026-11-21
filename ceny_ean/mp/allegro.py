"""Allegro.pl / Allegro.cz – zatím bez zdroje dat.

Stav oficiálního API (ověřeno v dokumentaci developer.allegro.pl, 11/2026):
  - veřejné vyhledávání nabídek (GET /offers/listing) v aktuální specifikaci API není,
  - katalog produktů podle EAN (GET /sale/products?phrase=<EAN>&mode=GTIN) existuje,
    ale vrací jen produkt, ne ceny konkurence,
  - ceny cizích nabídek ani jejich dopravu žádný endpoint nevrací.

Ceny konkurence tedy půjdou získat jen ze samotného webu (silná ochrana proti botům)
nebo přes placenou službu se scraperem. Až bude rozhodnuto, doplní se sem metoda hledej().
"""
from .base import Adapter, Vysledek


class Allegro(Adapter):
    def __init__(self, zeme, **kw):
        self.klic = f"allegro_{zeme}"
        super().__init__(**kw)

    def hledej(self, ean):
        return Vysledek("nepodporovano", zprava="Allegro: zdroj cen konkurence zatím není zvolen")
