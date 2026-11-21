# ceny_ean – ceny konkurence podle EAN

Pro seznam EAN zjistí nejnižší cenu nového zboží **včetně dopravy do země domény**:

| klíč | web | doprava do | zdroj | stav |
|---|---|---|---|---|
| `ebay_de` | ebay.de | DE | oficiální Browse API | hotovo |
| `kaufland_de` | kaufland.de | DE | Kaufland Seller API (váš prodejní účet) | hotovo, tvar odpovědi ověřit na prvním běhu (`--debug`) |
| `kaufland_cz` | kaufland.cz | CZ | Kaufland Seller API | totéž |
| `allegro_pl` | allegro.pl | PL | – | API ceny konkurence nevrací, zdroj se teprve volí |
| `allegro_cz` | allegro.cz | CZ | – | totéž |

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
```

* Vstup: CSV (`,` `;` nebo tab) nebo XLSX. EAN ze sloupce `ean`, jinak z prvního sloupce. Další sloupce se přenesou do výstupu.
* Výsledky se průběžně ukládají do `data/ceny.sqlite` pod označením běhu (kalendářní týden). Přerušený běh po novém spuštění pokračuje. `--znovu` vynutí nové dotazy.
* Výstup XLSX: list **Ceny** (řádek na EAN, pro každý web cena, doprava, celkem, přepočet na CZK/EUR podle ČNB, změna proti minulému týdnu, počet nabídek, prodejce, odkaz), list **Nenalezeno a chyby** a list **Kurzy ČNB**.
* `VLASTNI_PRODEJCI` v `.env` vyřadí vaše vlastní nabídky. U Kauflandu se vlastní nabídky poznají i automaticky.

## Týdenní spouštění (cron, pondělí 5:00)

```
0 5 * * 1  cd /cesta/ceny_ean && python3 ceny_ean.py eany.csv >> data/log.txt 2>&1
```

## Testy

```bash
python -m unittest discover -s tests
```
