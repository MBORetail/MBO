"""eBay.de přes oficiální Browse API.

Dokumentace: https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search
Token: client credentials (aplikační), platí 2 h, obnovuje se automaticky.
Doprava: počítá se do Německa (deliveryCountry:DE + kontext uživatele v DE).
"""
import base64
import time

from .base import Adapter, Nabidka, Vysledek, cislo

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


class ChybaPrihlaseni(Exception):
    pass


class EbayDE(Adapter):
    klic = "ebay_de"
    pauza = 0.3

    def __init__(self, client_id, client_secret, **kw):
        super().__init__(**kw)
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self._token = None
        self._token_do = 0.0

    def token(self):
        if self._token and time.time() < self._token_do - 60:
            return self._token
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = self.session.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=30,
        )
        if r.status_code != 200:
            try:
                popis = r.json().get("error_description") or r.json().get("error") or ""
            except ValueError:
                popis = r.text[:200]
            rada = ""
            if "SBX" in self.client_id.upper():
                rada = " Používáte SANDBOX klíče (obsahují SBX) – potřebujete klíče ze sloupce Production."
            elif r.status_code == 401:
                rada = (" Zkontrolujte, že EBAY_CLIENT_ID je App ID a EBAY_CLIENT_SECRET je Cert ID z produkčního"
                        " keysetu a že je u keysetu vyřízený krok Marketplace Account Deletion (výjimka).")
            raise ChybaPrihlaseni(f"eBay odmítl přihlášení (HTTP {r.status_code}: {popis}).{rada}")
        d = r.json()
        self._token = d["access_token"]
        self._token_do = time.time() + int(d.get("expires_in", 7200))
        return self._token

    def hledej(self, ean):
        r = self.pozadavek(
            "GET", SEARCH_URL,
            headers={
                "Authorization": f"Bearer {self.token()}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_DE",
                "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DDE%2Czip%3D10115",
                "Accept-Language": "de-DE",
            },
            params={
                "gtin": ean,
                "filter": "conditions:{NEW},buyingOptions:{FIXED_PRICE},deliveryCountry:DE",
                "sort": "price",
                "limit": 50,
            },
        )
        if r.status_code == 401:
            self._token = None
        if r.status_code != 200:
            return Vysledek("chyba", zprava=f"HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        self.uloz_debug(ean, d)
        return Vysledek("ok", self.parsuj(d)) if d.get("itemSummaries") else Vysledek("nenalezeno")

    def parsuj(self, d):
        out = []
        for it in d.get("itemSummaries", []):
            prodejce = (it.get("seller") or {}).get("username", "")
            if self.je_vlastni(prodejce):
                continue
            cena = cislo((it.get("price") or {}).get("value"))
            if cena is None:
                continue
            doprava = None
            for so in it.get("shippingOptions") or []:
                c = cislo((so.get("shippingCost") or {}).get("value"))
                if c is not None and (doprava is None or c < doprava):
                    doprava = c
            out.append(Nabidka(
                titul=it.get("title", ""),
                cena=cena,
                mena=(it.get("price") or {}).get("currency", "EUR"),
                doprava=doprava,
                prodejce=prodejce,
                stav=it.get("condition", ""),
                url=it.get("itemWebUrl", ""),
            ))
        return out
