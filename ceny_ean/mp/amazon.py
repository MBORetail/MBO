"""Amazon (amazon.de a další EU) přes Selling Partner API pod vaším prodejním účtem.

Zdroj: oficiální modely API https://github.com/amzn/selling-partner-api-models
  - přihlášení: Login with Amazon (refresh token) -> hlavička x-amz-access-token
  - katalog:   GET  /catalog/2022-04-01/items?identifiers=<až 20 EAN>&identifiersType=EAN
               (název, značka, pořadí prodejnosti = BSR)            limit 2 dotazy/s
  - ceny:      POST /batches/products/pricing/v0/itemOffers  (až 20 ASIN v dávce)
               nabídky vč. dopravy, Buy Box, MyOffer = vaše nabídka  limit 0,1 dávky/s
  - poplatky:  POST /products/fees/v0/feesEstimate  (až 20 ASIN)
               skutečné poplatky Amazonu (provize, u FBA i fulfillment)  limit 0,5 dotazu/s
Doprava: ceny jsou z daného marketplace (amazon.de = doručení v Německu).
"""
import json
import time

from .base import Adapter, Nabidka, Vysledek, cislo, ean_platny

LWA_URL = "https://api.amazon.com/auth/o2/token"
HOST_EU = "https://sellingpartnerapi-eu.amazon.com"
MARKETPLACE_ID = {
    "de": "A1PA6795UKMFR9", "fr": "A13V1IB3VIYZZH", "it": "APJ6JRA9NG5V4", "es": "A1RKKUPIHCS9HS",
    "nl": "A1805IZSGTT6HS", "pl": "A1C3SOZRARQ6R3", "se": "A2NODRKZP88ZB9", "be": "AMEN7PMS3EDWL",
}
MENA = {"de": "EUR", "fr": "EUR", "it": "EUR", "es": "EUR", "nl": "EUR", "be": "EUR", "pl": "PLN", "se": "SEK"}
DAVKA = 20


class Amazon(Adapter):
    def __init__(self, zeme, client_id, client_secret, refresh_token, fba=False, **kw):
        self.zeme_kod = zeme
        self.klic = f"amazon_{zeme}"
        super().__init__(**kw)
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.fba = fba
        self.mp_id = MARKETPLACE_ID[zeme]
        self._token, self._token_do = None, 0.0
        self._posledni_op = {}

    # --- infrastruktura --------------------------------------------------------
    def token(self):
        if self._token and time.time() < self._token_do - 60:
            return self._token
        r = self.session.post(LWA_URL, data={
            "grant_type": "refresh_token", "refresh_token": self.refresh_token,
            "client_id": self.client_id, "client_secret": self.client_secret}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Amazon odmítl přihlášení (HTTP {r.status_code}): {r.text[:200]}")
        d = r.json()
        self._token, self._token_do = d["access_token"], time.time() + int(d.get("expires_in", 3600))
        return self._token

    def _tempo(self, op, za_sekundu):
        """Dodrží limit konkrétní operace (každá má vlastní)."""
        zbyva = 1.0 / za_sekundu - (time.monotonic() - self._posledni_op.get(op, 0.0))
        if zbyva > 0:
            time.sleep(zbyva)
        self._posledni_op[op] = time.monotonic()

    def _api(self, metoda, cesta, op, za_sekundu, **kw):
        self._tempo(op, za_sekundu)
        r = self.pozadavek(metoda, HOST_EU + cesta, headers={
            "x-amz-access-token": self.token(), "Accept": "application/json",
            "Content-Type": "application/json"}, **kw)
        if r.status_code == 403 and "token" in r.text.lower():
            self._token = None
        return r

    # --- jednotlivé kroky -----------------------------------------------------------
    def katalog(self, eany):
        """{ean: {"asin", "titul", "znacka", "bsr", "bsr_kategorie"}}"""
        r = self._api("GET", "/catalog/2022-04-01/items", "katalog", 1.8, params={
            "identifiers": ",".join(eany), "identifiersType": "EAN", "marketplaceIds": self.mp_id,
            "includedData": "summaries,salesRanks,identifiers", "pageSize": 20})
        if r.status_code != 200:
            raise RuntimeError(f"katalog HTTP {r.status_code}: {r.text[:200]}")
        out = {}
        hledane = set(eany)
        for it in r.json().get("items", []):
            eany_polozky = set()
            for blok in it.get("identifiers") or []:
                for i in blok.get("identifiers") or []:
                    if i.get("identifierType") in ("EAN", "GTIN", "UPC"):
                        hodnota = str(i.get("identifier", ""))
                        for var in {hodnota, hodnota.zfill(13), hodnota.lstrip("0")}:
                            if var in hledane:
                                eany_polozky.add(var)
            summ = next((s for s in it.get("summaries") or [] if s.get("marketplaceId") == self.mp_id),
                        (it.get("summaries") or [{}])[0])
            bsr, kat = None, ""
            for sr in it.get("salesRanks") or []:
                if sr.get("marketplaceId") != self.mp_id:
                    continue
                # hlavní kategorie (displayGroupRanks) má přednost, jinak nejlepší podkategorie
                for skup in ("displayGroupRanks", "classificationRanks"):
                    ranks = sr.get(skup) or []
                    if ranks and bsr is None:
                        top = min(ranks, key=lambda x: x.get("rank", 10**9))
                        bsr, kat = top.get("rank"), top.get("title", "")
            zaznam = {"asin": it.get("asin"), "titul": summ.get("itemName", ""), "znacka": summ.get("brand", ""),
                      "bsr": bsr, "bsr_kategorie": kat}
            for e in eany_polozky:
                # jeden EAN může mít víc ASIN – bereme ten s lepším (nižším) BSR
                staty = out.get(e)
                if not staty or (bsr is not None and (staty["bsr"] is None or bsr < staty["bsr"])):
                    out[e] = zaznam
        return out

    def nabidky(self, asiny):
        """{asin: (seznam Nabidka, souhrn dict)}"""
        body = {"requests": [{"uri": f"/products/pricing/v0/items/{a}/offers", "method": "GET",
                              "MarketplaceId": self.mp_id, "ItemCondition": "New"} for a in asiny]}
        r = self._api("POST", "/batches/products/pricing/v0/itemOffers", "ceny", 0.1, data=json.dumps(body))
        if r.status_code != 200:
            raise RuntimeError(f"ceny HTTP {r.status_code}: {r.text[:200]}")
        out = {}
        for resp in r.json().get("responses", []):
            asin = (resp.get("request") or {}).get("Asin")
            payload = (resp.get("body") or {}).get("payload") or {}
            asin = asin or payload.get("ASIN")
            if not asin:
                continue
            nab = []
            for o in payload.get("Offers") or []:
                if o.get("MyOffer"):
                    continue          # vaše vlastní nabídka
                cena = cislo((o.get("ListingPrice") or {}).get("Amount"))
                if cena is None:
                    continue
                nab.append(Nabidka(
                    titul="", cena=cena, mena=(o.get("ListingPrice") or {}).get("CurrencyCode", MENA[self.zeme_kod]),
                    doprava=cislo((o.get("Shipping") or {}).get("Amount")) or 0.0,
                    prodejce=("FBA " if o.get("IsFulfilledByAmazon") else "") + (o.get("SellerId") or ""),
                    stav="Buy Box" if o.get("IsBuyBoxWinner") else "",
                    url=f"https://www.amazon.{'de' if self.zeme_kod == 'de' else self.zeme_kod}/dp/{asin}"))
            summ = payload.get("Summary") or {}
            bb = next((cislo((b.get("LandedPrice") or {}).get("Amount")) for b in summ.get("BuyBoxPrices") or []
                       if b.get("condition", "New").lower() == "new"), None)
            out[asin] = (nab, {"buybox": bb, "pocet_nabidek": summ.get("TotalOfferCount")})
        return out

    def poplatky(self, ceny):
        """ceny: {asin: (cena, doprava)} -> {asin: poplatky celkem}"""
        if not ceny:
            return {}
        body = [{"IdType": "ASIN", "IdValue": a, "FeesEstimateRequest": {
                    "MarketplaceId": self.mp_id, "IsAmazonFulfilled": self.fba, "Identifier": a,
                    "PriceToEstimateFees": {
                        "ListingPrice": {"CurrencyCode": MENA[self.zeme_kod], "Amount": round(c, 2)},
                        "Shipping": {"CurrencyCode": MENA[self.zeme_kod], "Amount": round(d or 0, 2)}}}}
                for a, (c, d) in ceny.items()]
        r = self._api("POST", "/products/fees/v0/feesEstimate", "poplatky", 0.45, data=json.dumps(body))
        if r.status_code != 200:
            return {}
        out = {}
        for res in r.json() if isinstance(r.json(), list) else []:
            if res.get("Status") != "Success":
                continue
            asin = (res.get("FeesEstimateIdentifier") or {}).get("IdValue")
            total = cislo((((res.get("FeesEstimate") or {}).get("TotalFeesEstimate")) or {}).get("Amount"))
            if asin and total is not None:
                out[asin] = total
        return out

    # --- dávka pro skript -------------------------------------------------------------
    def hledej_davku(self, eany):
        """{ean: Vysledek} pro až 20 EAN."""
        vysl = {}
        platne = [e for e in eany if ean_platny(e)]
        for e in eany:
            if e not in platne:
                vysl[e] = Vysledek("nenalezeno", zprava="neplatný EAN (chybná kontrolní číslice)")
        if not platne:
            return vysl
        kat = self.katalog(platne)
        asiny = list(dict.fromkeys(k["asin"] for k in kat.values() if k.get("asin")))
        ceny = self.nabidky(asiny) if asiny else {}
        k_poplatkum = {}
        for e in platne:
            k = kat.get(e)
            if not k:
                vysl[e] = Vysledek("nenalezeno", zprava="EAN není v katalogu Amazonu")
                continue
            nab, summ = ceny.get(k["asin"], ([], {}))
            for n in nab:
                n.titul = k["titul"]
            extra = {"asin": k["asin"], "bsr": k["bsr"], "bsr_kategorie": k["bsr_kategorie"],
                     "buybox": summ.get("buybox"), "pocet_nabidek_celkem": summ.get("pocet_nabidek")}
            if nab:
                v = Vysledek("ok", nab, titul=k["titul"], extra=extra)
                n = v.nejlevnejsi
                k_poplatkum[k["asin"]] = (n.cena, n.doprava)
            else:
                v = Vysledek("nenalezeno", zprava="bez nabídek konkurence", titul=k["titul"], extra=extra)
            vysl[e] = v
        try:
            fees = self.poplatky(k_poplatkum)
        except Exception:
            fees = {}
        for v in vysl.values():
            if v.extra.get("asin") in fees:
                v.extra["poplatky"] = fees[v.extra["asin"]]
        return vysl

    def hledej(self, ean):
        return self.hledej_davku([ean])[ean]
