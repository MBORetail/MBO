"""Offline testy (bez sítě a bez klíčů): python -m unittest discover -s tests"""
import csv
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ceny_ean as ce  # noqa: E402
from mp import Nabidka, Vysledek  # noqa: E402
from mp.ebay import EbayDE  # noqa: E402
from mp.kaufland import Kaufland, podpis  # noqa: E402


class Odpoved:
    def __init__(self, status, data):
        self.status_code, self._d, self.headers, self.text = status, data, {}, str(data)

    def json(self):
        return self._d


class TestEan(unittest.TestCase):
    def test_kontrolni_soucet(self):
        self.assertTrue(ce.ean_platny("4006381333931"))
        self.assertFalse(ce.ean_platny("4006381333932"))
        self.assertTrue(ce.ean_platny("96385074"))

    def test_normalizace(self):
        self.assertEqual(ce.normalizuj_ean(4006381333931.0), "4006381333931")
        self.assertEqual(ce.normalizuj_ean(" 4006381333931 "), "4006381333931")

    def test_vstup_csv_se_strednikem(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write("kod;EAN;nakup\nA1;4006381333931;100\nA2;4006381333931;100\nA3;96385074;50\n")
        vstup, dalsi = ce.nacti_vstup(f.name)
        os.unlink(f.name)
        self.assertEqual([z["ean"] for z in vstup], ["4006381333931", "96385074"])
        self.assertEqual(dalsi, ["kod", "nakup"])
        self.assertEqual(vstup[0]["kod"], "A1")

    def test_vstup_bez_hlavicky(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("4006381333931\n96385074\n")
        vstup, _ = ce.nacti_vstup(f.name)
        os.unlink(f.name)
        self.assertEqual(len(vstup), 2)


class TestEbay(unittest.TestCase):
    def test_parsovani_a_vlastni_prodejce(self):
        a = EbayDE("id", "secret", vlastni_prodejci=["mbo-shop"])
        a._token, a._token_do = "T", 1e12
        data = {"itemSummaries": [
            {"title": "Stift", "price": {"value": "9.99", "currency": "EUR"},
             "shippingOptions": [{"shippingCost": {"value": "4.90", "currency": "EUR"}}],
             "seller": {"username": "haendler1"}, "condition": "Neu", "itemWebUrl": "u1"},
            {"title": "Stift billig", "price": {"value": "8.50", "currency": "EUR"},
             "shippingOptions": [{"shippingCostType": "CALCULATED"}],
             "seller": {"username": "haendler2"}, "itemWebUrl": "u2"},
            {"title": "Vlastni", "price": {"value": "5.00", "currency": "EUR"},
             "shippingOptions": [{"shippingCost": {"value": "0", "currency": "EUR"}}],
             "seller": {"username": "MBO-shop"}, "itemWebUrl": "u3"},
        ]}
        with mock.patch.object(a.session, "request", return_value=Odpoved(200, data)) as req:
            v = a.hledej("4006381333931")
        hdr = req.call_args.kwargs["headers"]
        self.assertEqual(hdr["X-EBAY-C-MARKETPLACE-ID"], "EBAY_DE")
        self.assertIn("deliveryCountry:DE", req.call_args.kwargs["params"]["filter"])
        self.assertEqual(v.status, "ok")
        self.assertEqual(len(v.nabidky), 2)            # vlastní nabídka vyřazena
        self.assertEqual(v.nejlevnejsi.url, "u1")      # známá doprava má přednost
        self.assertEqual(v.nejlevnejsi.celkem, 14.89)

    def test_nenalezeno(self):
        a = EbayDE("id", "secret")
        a._token, a._token_do = "T", 1e12
        with mock.patch.object(a.session, "request", return_value=Odpoved(200, {"total": 0})):
            self.assertEqual(a.hledej("4006381333931").status, "nenalezeno")


class TestKaufland(unittest.TestCase):
    def test_podpis_je_hex_sha256(self):
        s = podpis("GET", "https://sellerapi.kaufland.com/v2/buybox?ean=1", "", 1411055926, "x" * 64)
        self.assertEqual(len(s), 64)
        self.assertEqual(s, s.lower())

    def test_products_ean_units(self):
        a = Kaufland("cz", "k" * 32, "s" * 64, vlastni_prodejci=["Muj obchod"])
        data = {"data": {"title": "Fixa", "id_product": 123, "units": [
            {"seller": {"pseudonym": "Konkurent A"}, "price": 24900, "shipping_rate": 7900, "id_unit": 7},
            {"seller": "Konkurent B", "price": 22900, "shipping_rate": 12900, "buybox_rank": 2},
            {"seller": {"pseudonym": "Muj obchod"}, "price": 19900, "shipping_rate": 0, "id_unit": 5},
        ]}}
        with mock.patch.object(a.session, "request", return_value=Odpoved(200, data)) as req:
            v = a.hledej("4006381333931")
        self.assertIn("/v2/products/ean/4006381333931?storefront=cz&embedded=units", req.call_args.args[1])
        self.assertEqual(v.status, "ok")
        self.assertEqual(len(v.nabidky), 2)
        n = v.nejlevnejsi
        self.assertEqual((n.prodejce, n.cena, n.doprava, n.celkem, n.mena), ("Konkurent A", 249.0, 79.0, 328.0, "CZK"))
        self.assertEqual(n.url, "https://www.kaufland.cz/product/123/")


class FalesnyAdapter:
    def __init__(self, klic, ceny):
        self.klic, self.ceny = klic, ceny

    def hledej(self, ean):
        if ean not in self.ceny:
            return Vysledek("nenalezeno")
        c, d, m = self.ceny[ean]
        return Vysledek("ok", [Nabidka("x", c, m, d, "prodejce", "", "https://example.com")])


class TestCelyBeh(unittest.TestCase):
    def test_db_navazani_a_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ce.Databaze(os.path.join(tmp, "t.sqlite"))
            kurzy = {"CZK": 1.0, "EUR": 25.0, "PLN": 5.8}
            eb = FalesnyAdapter("ebay_de", {"4006381333931": (10.0, 5.0, "EUR")})
            kc = FalesnyAdapter("kaufland_cz", {"4006381333931": (300.0, 79.0, "CZK")})
            eany = ["4006381333931", "96385074"]
            nic = lambda *a: None  # noqa: E731
            self.assertEqual(ce.zpracuj_marketplace(eb, eany, "2026-W46", db, False, nic), 2)
            ce.zpracuj_marketplace(kc, eany, "2026-W46", db, False, nic)
            # druhé spuštění téhož běhu už nic nedotazuje
            self.assertEqual(ce.zpracuj_marketplace(eb, eany, "2026-W46", db, False, nic), 0)
            eb.ceny["4006381333931"] = (12.0, 5.0, "EUR")
            ce.zpracuj_marketplace(eb, eany, "2026-W47", db, False, nic)
            ce.zpracuj_marketplace(kc, eany, "2026-W47", db, False, nic)
            self.assertEqual(db.predchozi_beh("2026-W47"), "2026-W46")

            out = os.path.join(tmp, "o.xlsx")
            vstup = [{"ean": e, "kod": f"K{i}"} for i, e in enumerate(eany)]
            ce.export_xlsx(out, vstup, ["kod"], ["ebay_de", "kaufland_cz"],
                           db.vysledky("2026-W47"), db.vysledky("2026-W46"), kurzy)
            from openpyxl import load_workbook
            wb = load_workbook(out)
            ws = wb["Ceny"]
            r = [c.value for c in ws[3]]
            self.assertEqual(r[0], "4006381333931")
            self.assertEqual(r[2], 379.0)          # nejlevnější: Kaufland CZ 379 Kč < eBay 17 € = 425 Kč
            self.assertEqual(r[3], "kaufland_cz")
            self.assertEqual(r[4:11], [12.0, 5.0, 17.0, "EUR", 425.0, 17.0, 13.3])  # změna (17-15)/15
            self.assertEqual(wb["Nenalezeno a chyby"].max_row, 3)
            self.assertEqual(ws.cell(1, 4).value, "kde")
            self.assertEqual(ws.cell(1, 5).value, "ebay_de (doprava DE)")
            self.assertEqual(r[12], "prodejce")
            self.assertEqual(ws.cell(3, 14).hyperlink.target, "https://example.com")


if __name__ == "__main__":
    unittest.main()
