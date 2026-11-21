"""Kaufland.de / Kaufland.cz přes Seller API v2 (vyžaduje prodejní účet).

Dokumentace: https://sellerapi.kaufland.com/?page=rest-api
Podpis: HMAC-SHA256(secret, METHOD\\nURI\\nBODY\\nTIMESTAMP) jako hex.

POZOR – neověřeno naostro: veřejná dokumentace popisuje endpoint GET /buybox
("konkurence na jednom EAN"), ale jeho parametry a tvar odpovědi nejsou veřejně
čitelné. Adaptér proto zkouší:
  1) GET /v2/buybox?ean=…&storefront=de|cz
  2) GET /v2/products/ean/{ean}?storefront=…&embedded=units   (záloha)
a z odpovědi tolerantně vytáhne nabídky (pole s price / shipping_rate / seller).
Při prvním ostrém běhu spusťte s --debug a pošlete mi vzorek JSON, parser doladím.

Doprava: pole shipping_rate je doprava v rámci daného storefrontu (de = do Německa, cz = do ČR).

Ověřeno naostro (11/2026): /buybox vrací 404, /products/ean/{ean}?embedded=units funguje,
ceny i doprava jsou v centech, prodejce je v units[].seller.pseudonym. id_unit je vyplněné
u všech nabídek, vlastní nabídky se proto poznávají jen podle VLASTNI_PRODEJCI.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from .base import Adapter, Nabidka, Vysledek, cislo

BASE = "https://sellerapi.kaufland.com/v2"
MENA = {"de": "EUR", "cz": "CZK", "sk": "EUR", "pl": "PLN", "at": "EUR"}
WEB = {"de": "https://www.kaufland.de", "cz": "https://www.kaufland.cz"}


def podpis(metoda, uri, body, timestamp, secret):
    zprava = "\n".join([metoda, uri, body, str(timestamp)])
    return hmac.new(secret.encode(), zprava.encode(), hashlib.sha256).hexdigest()


class Kaufland(Adapter):
    pauza = 0.2   # limit API je 111 req/s na prodejce, jsme hluboko pod ním

    def __init__(self, storefront, client_key, secret_key, **kw):
        self.storefront = storefront
        self.klic = f"kaufland_{storefront}"
        super().__init__(**kw)
        self.client_key = client_key
        self.secret_key = secret_key
        # ceny ve v2 API jsou v centech (int); lze přepnout po ověření na živých datech
        self.ceny_v_centech = True

    def _get(self, cesta, params):
        url = f"{BASE}{cesta}" + (f"?{urlencode(params)}" if params else "")
        ts = int(time.time())
        r = self.pozadavek("GET", url, headers={
            "Accept": "application/json",
            "Shop-Client-Key": self.client_key,
            "Shop-Timestamp": str(ts),
            "Shop-Signature": podpis("GET", url, "", ts, self.secret_key),
        })
        return r

    def hledej(self, ean):
        # /buybox naostro vrací 404, proto jen jeden dotaz na EAN
        pokusy = [
            (f"/products/ean/{ean}", {"storefront": self.storefront, "embedded": "units"}),
        ]
        posledni_chyba = ""
        for cesta, params in pokusy:
            r = self._get(cesta, params)
            if r.status_code == 404:
                posledni_chyba = f"{cesta}: 404"
                continue
            if r.status_code != 200:
                posledni_chyba = f"{cesta}: HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (401, 403):
                    break  # špatné klíče – nemá smysl zkoušet dál
                continue
            d = r.json()
            self.uloz_debug(ean, {"endpoint": cesta, "odpoved": d})
            nabidky = self.parsuj(d)
            if nabidky:
                return Vysledek("ok", nabidky)
            return Vysledek("nenalezeno", zprava=f"{cesta}: bez nabídek")
        if posledni_chyba.endswith(": 404"):
            return Vysledek("nenalezeno")
        return Vysledek("chyba", zprava=posledni_chyba)

    # --- parsování -----------------------------------------------------------
    def _castka(self, v):
        if isinstance(v, dict):  # např. {"amount": 1999, "currency": "EUR"}
            v = v.get("amount", v.get("value"))
        c = cislo(v)
        if c is None:
            return None
        return round(c / 100, 2) if self.ceny_v_centech and float(c).is_integer() else c

    def _units(self, d):
        """Najde v libovolně vnořené odpovědi slovníky, které vypadají jako nabídka."""
        if isinstance(d, dict):
            if any(k in d for k in ("price", "listing_price", "amount")) and \
               any(k in d for k in ("seller", "shipping_rate", "id_unit", "buybox_rank", "fulfillment_type")):
                yield d
                return
            for v in d.values():
                yield from self._units(v)
        elif isinstance(d, list):
            for v in d:
                yield from self._units(v)

    def parsuj(self, d):
        out = []
        titul = ""
        data = d.get("data", d) if isinstance(d, dict) else d
        url_produktu = ""
        if isinstance(data, dict):
            titul = data.get("title", "")
            id_product = data.get("id_product")
            url_produktu = data.get("url", "")
        else:
            id_product = None
        for u in self._units(d):
            seller = u.get("seller", "")
            if isinstance(seller, dict):
                seller = seller.get("name") or seller.get("pseudonym") or ""
            if self.je_vlastni(seller):
                continue
            cena = self._castka(u.get("price", u.get("listing_price")))
            if cena is None:
                continue
            idp = u.get("id_product") or id_product
            out.append(Nabidka(
                titul=u.get("title", titul),
                cena=cena,
                mena=MENA.get(self.storefront, ""),
                doprava=self._castka(u.get("shipping_rate")),
                prodejce=seller,
                stav=u.get("condition", ""),
                url=url_produktu or (f"{WEB.get(self.storefront, '')}/product/{idp}/" if idp else ""),
            ))
        return out
