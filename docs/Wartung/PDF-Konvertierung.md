# PDF-Validierung und Konvertierung

## Betriebsgrenze

P06 materialisiert ausschließlich abgeleitete Dateien unter einem expliziten
`temp_root`. Original-PDF, Repository, Git-Index, LFS und Remotes bleiben
unverändert. Erlaubter RunMode ist nur `materialize`; sichtbare Effekte sind nur
`temp_file`.

```bash
python -m scripts.rki_pipeline.cli convert \
  --fixture tests/fixtures/pdf/text.pdf \
  --mode materialize
```

Die JSON-Ausgabe nennt `temp_root`, Markdown, Manifest, Qualität, Fingerprint und
Effekte. Ohne `--temp-root` entsteht ein persistenter privater Pfad unter dem
System-Tempverzeichnis. Für Wiederaufnahme oder gezielte Bereinigung denselben
Pfad explizit setzen.

Exitcodes:

- `0`: `converted` oder byteidentisch geprüftes `skipped_unchanged`;
- `2`: ungültiger Aufruf, Fixture-Drift oder fehlende Rechtefreigabe;
- `3`: Validierungs-, Tool-, Runtime- oder Konvertierungsfehler;
- `4`: OCR fehlt oder Ergebnis benötigt Review.

## Geprüfter Paket- und Lizenzsatz

| Rolle | Debian/Ubuntu-Paket | Werkzeuge/Daten | Upstream-Lizenz |
|---|---|---|---|
| Pflicht | `poppler-utils` | `pdfinfo`, `pdftotext` | GPL-2.0-or-later |
| OCR, optional | `poppler-utils` | `pdftoppm` | GPL-2.0-or-later |
| OCR, optional | `tesseract-ocr` | `tesseract` | Apache-2.0 |
| OCR, optional | `tesseract-ocr-deu`, `tesseract-ocr-eng` | exakt `deu.traineddata`, `eng.traineddata` | Apache-2.0 |

Paket-Copyrightdateien und Distributionshinweise bleiben für Freigabe und
Weitergabe maßgeblich. CI installiert nur Poppler. Tesseract bleibt bewusst
optional; fehlende OCR-Bestandteile erzeugen sichtbar `needs_review`, keinen
stillen Erfolg.

## Feste Konvertierung

Vor Konvertierung werden reguläre Datei, Größe, `%PDF-`, terminales `%%EOF`,
SHA-256, Parseröffnung, Seitenzahl und Verschlüsselung geprüft. Textausgabe nutzt
`pdftotext -layout -enc UTF-8 -eol unix`. Jede PDF-Seite erhält genau einen Marker:

```html
<!-- rki-page: N -->
```

Qualität ist nur `good`, wenn jede Seite mindestens 40 sichtbare Zeichen enthält,
keine Seite leer ist, Seitenzahlen übereinstimmen und höchstens ein Prozent der
Zeichen Unicode-Ersatzzeichen sind. Sonst läuft optional OCR pro Seite mit 300
DPI, Graustufen, `deu+eng`, PSM 3 und OEM 1. OCR-Ergebnis bleibt immer
`needs_review`.

## Ressourcengrenzen

- Quelle: 256 MiB;
- Seiten: 2.000;
- Raster: 100.000.000 Pixel;
- Wand- und CPU-Zeit je Toollauf: 120 Sekunden;
- Adressraum: 2 GiB; offene Dateien: 256;
- erzeugte Einzeldatei, stdout und Gesamtausgabe: jeweils 512 MiB;
- stderr: 1 MiB; Conversion Manifest: 1 MiB;
- Tool-Arbeitsbaum: 64 Ebenen und 4.096 Einträge.

Toolprozesse laufen ohne Shell, mit fester Umgebung, festen Argumentlisten,
Prozessgruppen- und Bytegrenzen sowie privaten markierten Arbeitsverzeichnissen.

## Fingerprint und Wiederaufnahme

Fingerprint bindet mindestens:

- Source-SHA und Rechteentscheidung;
- alle Optionen, Qualitätswerte und Ressourcengrenzen;
- Toolname, Version, Executable-SHA, Argumentvorlage und Umgebung;
- Plattform, libc sowie SHA-256 aller verwendeten Shared Libraries und
  fontconfig-sichtbaren Fonts;
- bei OCR die SHA-256 der privat kopierten deutschen und englischen Tessdata.

Ein vorhandenes Bundle wird nur übersprungen, wenn Manifest, Output, Größen,
Hashes, Qualität und OCR-Zustand erneut vollständig passen. Runtime- oder
Toolchain-Drift erzeugt einen anderen Fingerprint.

## Rollback und Review

Markdown und Manifest entstehen vollständig im privaten Staging. Veröffentlichung
erfolgt als zusammengehöriges create-if-absent-Bundle; vorhandene Ziele werden
nicht überschrieben. Fehler vor Veröffentlichung entfernen nur eigene
Stagingdateien. Signalunterbrechungen zwischen Bundle-Publikation und Ledger-
Commit bleiben als nachvollziehbares, erneut prüfbares Bundle erhalten. Quelle
bleibt immer byteidentisch.

`failed` verwirft abgeleitete Stagingdaten. `needs_review` verlangt fachliche
Sichtprüfung von Seitenmarkern, OCR-Text und Manifest, bevor ein späterer
`apply`-Schritt zulässig wäre. P06 selbst besitzt keinen `apply`-Pfad.

## Prüfungen

```bash
python -m pytest -q \
  tests/test_pdf_validation.py \
  tests/test_conversion.py \
  tests/test_conversion_service.py \
  tests/test_conversion_ocr.py \
  tests/test_cli_entrypoints.py
python scripts/validate_fixture_manifest.py
python scripts/validate_rights_register.py
```
