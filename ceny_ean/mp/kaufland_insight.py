"""Kaufland Assortment Insight – pořadí prodejnosti, bestseller, poptávka a průměrné ceny Buy Boxu.

Dokumentace: https://sellerapi.kaufland.com/?page=assortment-insight
  GET /v2/assortment-coverage/insights?offset=…&limit=…   (max. 5000 položek na dotaz)

Endpoint nefiltruje podle EAN – vrací celý seznam doporučených produktů (řádově 300 tis.),
proto se stáhne jednou denně celý, uloží do SQLite a páruje se podle EAN.
Pokrývá jen produkty z tohoto seznamu a jen EUR (Kaufland.de). Měsíční prodeje v kusech neobsahuje.
"""
import json
import time

from .kaufland import BASE, Kaufland

POLE = ["item_name", "brand", "category", "sales_rank", "kaufland_bestseller",
        "external_demand_indicator", "external_rank", "buybox_price_current",
        "buybox_price_l_30_day", "buybox_price_l_90_day", "popular_product_bad_priced"]


class AssortmentInsight(Kaufland):
    def __init__(self, client_key, secret_key, **kw):
        super().__init__("de", client_key, secret_key, **kw)
        self.klic = "kaufland_insight"

    def stahni_vse(self, prubeh=print, limit=5000):
        """Generátor položek přes všechny stránky."""
        offset, celkem = 0, None
        params_extra = {}
        while celkem is None or offset < celkem:
            r = self._get("/assortment-coverage/insights", {"offset": offset, "limit": limit, **params_extra})
            if r.status_code == 400 and not params_extra:
                params_extra = {"storefront": "de"}   # pro případ, že je storefront povinný
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Assortment Insight: HTTP {r.status_code}: {r.text[:200]}")
            d = r.json()
            data = d.get("data") or []
            celkem = (d.get("pagination") or {}).get("total", offset + len(data))
            yield from data
            offset += len(data) or limit
            if not data:
                break
            prubeh(f"  Assortment Insight: {min(offset, celkem)}/{celkem}")


def nacti_insight(db, client_key, secret_key, eany, max_stari_hodin=24, prubeh=print):
    """Vrátí {ean: {pole…}} pro zadané EAN. Celý seznam se stahuje nejvýš jednou za max_stari_hodin."""
    with db.lock:
        db.db.execute("CREATE TABLE IF NOT EXISTS insight (ean TEXT PRIMARY KEY, data TEXT)")
        db.db.execute("CREATE TABLE IF NOT EXISTS insight_stav (klic TEXT PRIMARY KEY, cas REAL)")
        r = db.db.execute("SELECT cas FROM insight_stav WHERE klic='stazeno'").fetchone()
    if not r or time.time() - r[0] > max_stari_hodin * 3600:
        prubeh("Stahuji Kaufland Assortment Insight (jednou denně, chvíli to trvá)…")
        ai = AssortmentInsight(client_key, secret_key)
        radky = [(it.get("ean"), json.dumps({k: it.get(k) for k in POLE}, ensure_ascii=False))
                 for it in ai.stahni_vse(prubeh) if it.get("ean")]
        with db.lock:
            db.db.execute("DELETE FROM insight")
            db.db.executemany("INSERT OR REPLACE INTO insight VALUES (?,?)", radky)
            db.db.execute("INSERT OR REPLACE INTO insight_stav VALUES ('stazeno', ?)", (time.time(),))
            db.db.commit()
        prubeh(f"  Assortment Insight: uloženo {len(radky)} produktů")
    out = {}
    with db.lock:
        for ean in eany:
            # Insight používá EAN-13; vstup může mít jinou délku (UPC-12, GTIN-14)
            for var in {ean, ean.zfill(13), ean.lstrip("0").zfill(13)}:
                row = db.db.execute("SELECT data FROM insight WHERE ean=?", (var,)).fetchone()
                if row:
                    out[ean] = json.loads(row[0])
                    break
    return out
