# RKI Epidemiologisches Bulletin – Downloader

Der Downloader nutzt den offiziellen Publikationsserver des Robert Koch-Instituts:

- **Gesamtausgaben/Jahrgänge:** `https://edoc.rki.de/handle/176904/10`
- **Einzelartikel:** `https://edoc.rki.de/handle/176904/45`

Das RKI weist im Gesamtausgaben-Archiv darauf hin, dass nach Abschluss des Jahres 2020 dort keine weiteren Gesamtausgaben veröffentlicht werden. Einige Gesamtausgaben von 2021 sind dennoch vorhanden. Für neuere Inhalte dient daher zusätzlich die Einzelartikel-Sammlung.

## Installation

```bash
python -m pip install requests beautifulsoup4
```

## Für die ursprüngliche Recherche: 1994 bis 1996

Zuerst einen kleinen Netz- und Parsertest mit drei Item-Seiten ausführen:

```bash
python rki_epidbull_grabber.py \
  --scope issues \
  --from-year 1994 \
  --to-year 1996 \
  --dry-run \
  --max-items 3 \
  --output rki-epidbull-test
```

Danach den gesamten Zeitraum nur erfassen, ohne PDFs herunterzuladen:

```bash
python rki_epidbull_grabber.py \
  --scope issues \
  --from-year 1994 \
  --to-year 1996 \
  --dry-run \
  --output rki-epidbull-1994-1996
```

Danach herunterladen:

```bash
python rki_epidbull_grabber.py \
  --scope issues \
  --from-year 1994 \
  --to-year 1996 \
  --output rki-epidbull-1994-1996
```

## Alle verfügbaren Gesamtausgaben

```bash
python rki_epidbull_grabber.py --scope issues --output rki-epidbull-gesamtausgaben
```

## Gesamtausgaben plus Einzelartikel

```bash
python rki_epidbull_grabber.py \
  --scope all \
  --from-year 1994 \
  --to-year 2026 \
  --output rki-epidbull-alles
```

Dabei können Inhalte doppelt vorkommen: einmal als vollständige Ausgabe und einmal als Einzelartikel.

## Verhalten

- Standardmäßig **ein HTTP-Abruf nach dem anderen** mit mindestens **1,25 Sekunden Abstand**.
- Prüft `robots.txt`, soweit diese abrufbar ist.
- Wiederaufnahme: Bereits vorhandene, gültige PDFs werden nicht erneut geladen.
- Verifiziert PDF-Magic-Bytes und, soweit auf der Item-Seite vorhanden, die RKI-MD5-Prüfsumme.
- Erstellt zusätzlich eine SHA-256-Prüfsumme.
- Schreibt laufend `manifest.jsonl` und am Ende `manifest.csv` sowie `run-info.json`.
- Fehler an einzelnen Dokumenten werden protokolliert; der Lauf setzt die übrigen Dokumente fort.

Eine Kontaktadresse im User-Agent ist bei umfangreichen Abrufen höflich:

```bash
python rki_epidbull_grabber.py \
  --scope issues \
  --contact name@example.org
```

## Wichtige Optionen

```text
--scope issues|articles|all
--from-year JAHR
--to-year JAHR
--output VERZEICHNIS
--dry-run
--max-items ANZAHL
--force
--delay SEKUNDEN
--contact E-MAIL
--verbose
```

`--no-robots` ist absichtlich vorhanden, aber nicht empfohlen. Es sollte nur eingesetzt werden, wenn die Zugriffsfreigabe anderweitig geprüft wurde. Die Drosselung bleibt trotzdem aktiv.

## Erwartete Ordnerstruktur

```text
rki-epidbull/
├── issues/
│   ├── 1994/
│   ├── 1995/
│   └── ...
├── articles/
│   └── ...
├── manifest.csv
├── manifest.jsonl
└── run-info.json
```

Die Dateinamen enthalten Titel, Item-Handle und ursprünglichen PDF-Dateinamen, damit gleichnamige Dateien nicht überschrieben werden.
