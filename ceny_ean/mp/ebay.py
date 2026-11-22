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


LIMIT = "denní limit eBay API vyčerpán – dotáže se při dalším spuštění"


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

    def _hledej(self, **dotaz):
        return self.pozadavek(
            "GET", SEARCH_URL, opakovat_429=False,   # 429 u eBay = vyčerpaný denní limit, opakování ho jen pálí
            headers={
                "Authorization": f"Bearer {self.token()}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_DE",
                "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DDE%2Czip%3D10115",
                "Accept-Language": "de-DE",
            },
            params={
                **dotaz,
                "filter": "conditions:{NEW},buyingOptions:{FIXED_PRICE},deliveryCountry:DE",
                "sort": "price",
                "limit": 50,
            },
        )

    def hledej(self, ean):
        # 1) přesně podle katalogového GTIN, 2) EAN jako hledaný text (nabídky mimo katalog eBay)
        if self.vycerpano:
            return Vysledek("chyba", zprava=LIMIT)
        for dotaz in ({"gtin": ean}, {"q": ean}):
            r = self._hledej(**dotaz)
            if r.status_code == 429:
                self.vycerpano = True
                return Vysledek("chyba", zprava=LIMIT)
            if r.status_code == 401:
                self._token = None
            if r.status_code != 200:
                return Vysledek("chyba", zprava=f"HTTP {r.status_code}: {r.text[:200]}")
            d = r.json()
            self.uloz_debug(ean, {"dotaz": dotaz, "odpoved": d})
            if d.get("itemSummaries"):
                nabidky = self.parsuj(d)
                if nabidky:
                    return Vysledek("ok", nabidky)
                return Vysledek("nenalezeno", zprava="jen vlastní nabídky",
                                titul=d["itemSummaries"][0].get("title", ""))
        return Vysledek("nenalezeno")

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
