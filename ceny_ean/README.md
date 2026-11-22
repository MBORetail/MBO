# ceny_ean – ceny konkurence podle EAN

Pro seznam EAN zjistí nejnižší cenu nového zboží **včetně dopravy do země domény**:

| klíč | web | doprava do | zdroj | stav |
|---|---|---|---|---|
| `ebay_de` | ebay.de | DE | oficiální Browse API | hotovo |
| `kaufland_de` | kaufland.de | DE | Kaufland Seller API (váš prodejní účet) | hotovo, tvar odpovědi ověřit na prvním běhu (`--debug`) |
| `kaufland_cz` | kaufland.cz | CZ | Kaufland Seller API | totéž |
| `allegro_pl` | allegro.pl | PL | – | API ceny konkurence nevrací, zdroj se teprve volí |
| `allegro_cz` | allegro.cz | CZ | – | totéž |
| `amazon_de` | amazon.de | DE | Amazon Selling Partner API (váš prodejní účet) | hotovo, ověřit naostro; BSR, Buy Box a skutečné poplatky Amazonu |

## Instalace

```bash
pip install requests openpyxl
cp .env.example .env   # doplnit klíče
```

## Použití

```bash
python ceny_ean.py eany.csv                       # dávka -> ceny_2026-W47.xlsx
python ceny_ean.py eany.xlsx --jen ebay_de        # jen vybrané weby
python ceny_ean.py 4006381333931                  # jeden EAN do terminálu
python ceny_ean.py eany.csv --debug               # surové odpovědi API do debug/
python ceny_ean.py eany.csv --jen-export          # jen Excel z uložených výsledků, bez dotazů
```

* Vstup: CSV (`,` `;` nebo tab) nebo XLSX. EAN ze sloupce `ean`, jinak z prvního sloupce. Další sloupce se přenesou do výstupu.
* Výsledky se průběžně ukládají do `data/ceny.sqlite` pod označením běhu (kalendářní týden). Přerušený běh po novém spuštění pokračuje. `--znovu` vynutí nové dotazy.
* Výstup XLSX: list **Ceny** (řádek na EAN, pro každý web cena, doprava, celkem, přepočet na CZK/EUR podle ČNB, změna proti minulému týdnu, počet nabídek, prodejce, odkaz), list **Nenalezeno a chyby** a list **Kurzy ČNB**.
* **Zisk a marže:** pokud má vstup sloupec s nákupní cenou (`nákupní cena`, `nakup`, `NC`, nebo název v `SLOUPEC_NAKUP`), spočítá se pro každý web zisk na kus v EUR a hrubá marže v %. Prodejní cena = nejnižší cena konkurence vč. dopravy; odečte se DPH země prodeje, provize marketplace (% + fix), nákupní cena bez DPH a vaše doprava. Sazby se nastavují v `.env` (viz `.env.example`), použité hodnoty jsou ve výstupu na listu **Parametry výpočtu**.
* **Kaufland Assortment Insight:** pořadí prodejnosti, bestseller, poptávka mimo Kaufland a průměrné ceny Buy Boxu (30/90 dní). Celý seznam (~300 tis. produktů) se stahuje jednou denně; obsahuje jen produkty, které Kaufland doporučuje do sortimentu, a jen Kaufland.de. Vypnout: `--bez-insight`.
* **eBay odhad prodejů/měsíc:** po zjištění cen projde skript u každého EAN až `EBAY_PRODEJE_TOP` (výchozí 3) nejlevnějších nabídek a sečte prodané kusy / měsíce od vystavení nabídky. Orientační údaj; spotřebuje jen zbytek denního limitu eBay API, chybějící doplní další běh.
* `VLASTNI_PRODEJCI` v `.env` vyřadí vaše vlastní nabídky (porovnává se jméno prodejce, čárkou oddělený seznam pro všechny weby).

## Týdenní spouštění (cron, pondělí 5:00)

```
0 5 * * 1  cd /cesta/ceny_ean && python3 ceny_ean.py eany.csv >> data/log.txt 2>&1
```

## Testy

```bash
python -m unittest discover -s tests
```
