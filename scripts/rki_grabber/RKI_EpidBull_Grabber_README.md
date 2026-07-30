# RKI Epidemiologisches Bulletin – modularer Grabber

Der Grabber nutzt ausschließlich den offiziellen Publikationsserver `https://edoc.rki.de`:

- Gesamtausgaben/Jahrgänge: `/handle/176904/10`
- Einzelartikel: `/handle/176904/45`

P03 erhält die fachliche Logik des ursprünglichen 817-zeiligen Grabbers, trennt sie aber in reine Parser, einen injizierbaren HTTP-Transport, einen gehärteten Downloader, eine importierbare API und eine dünne Kompatibilitäts-CLI.

## Installation

```bash
python3 -m pip install -r requirements.txt
```

Für Entwicklung und Tests:

```bash
python3 -m pip install -r requirements-test.txt
```

## Sichere kleine Vorschau

`--dry-run` liest Sammlungs- und Item-Metadaten, lädt aber keine PDFs und erzeugt standardmäßig keine Ausgabedateien. Das strukturierte Ergebnis wird nach stdout geschrieben:

```bash
python3 -m scripts.rki_grabber.rki_epidbull_grabber \
  --scope issues \
  --from-year 1994 \
  --to-year 1996 \
  --dry-run \
  --max-items 3
```

Ein expliziter Ergebnisbericht kann außerhalb des kanonischen Archivbaums geschrieben werden:

```bash
python3 -m scripts.rki_grabber.rki_epidbull_grabber \
  --scope issues \
  --from-year 1994 \
  --to-year 1996 \
  --dry-run \
  --max-items 3 \
  --result-json build/grabber-plan.json
```

## Materialisierender Lauf

Ein Lauf ohne `--dry-run` lädt PDFs in das angegebene Verzeichnis und schreibt atomar:

- `result.json` als kanonischen strukturierten Vertrag,
- `manifest.jsonl`, `manifest.csv` und `run-info.json` als Kompatibilitätsausgaben,
- PDFs unter relativen, handlebasierten Pfaden.

```bash
python3 -m scripts.rki_grabber.rki_epidbull_grabber \
  --scope issues \
  --from-year 1994 \
  --to-year 1996 \
  --max-items 3 \
  --output rki-epidbull-pilot
```

Ein produktiver RKI-Abruf oder historischer Backfill gehört ausdrücklich nicht zu P03. Er folgt erst nach RunMode-, Storage-, Rechte-, Pipeline- und Pilotgates.

## Python-API

```python
from scripts.rki_grabber.api import grab
from scripts.rki_grabber.models import GrabberRequest, Scope

result = grab(
    GrabberRequest(
        scope=Scope.ISSUES,
        from_year=1994,
        to_year=1996,
        dry_run=True,
        max_items=3,
    )
)
print(result.to_dict())
```

CLI, spätere Pipeline und Backfill verwenden dieselbe `grab()`-API und denselben `grabber-result`-Vertrag.

## Sicherheitsgrenzen

- nur HTTPS und feste Hosts aus `config/rki-source.toml`;
- keine Credentials, fremden Ports oder fremden Redirectziele;
- `robots.txt` wird bei aktiviertem Schutz fail-closed ausgewertet;
- serielle, verzögerte Requests mit `Retry-After`-fähigem Retryadapter;
- harte Grenzen für HTML-, Robots- und PDF-Bytes;
- MIME-, `%PDF-`- und `%%EOF`-Prüfung;
- optionaler RKI-MD5-Abgleich und immer SHA-256;
- descriptor-relative `.part`-Dateien, `fsync` und atomarer Austausch;
- keine Symlinkkomponente und kein Root-Escape;
- Unit- und Fixturetests laufen ohne Netzwerk.

`--no-robots` bleibt als bewusst sichtbarer Kompatibilitätsschalter erhalten. Er darf nur nach gesonderter Freigabe verwendet werden und ändert nichts an Host-, Redirect-, Rate- oder Größenlimits.

## Exitcodes

| Code | Bedeutung |
|---:|---|
| 0 | vollständig erfolgreich |
| 2 | validierte Teilfehler; übrige Items wurden verarbeitet |
| 3 | sicher blockiert, beispielsweise durch Robots-Regeln |
| 4 | Konfigurations-, Eingabe- oder Sicherheitsfehler vor einem regulären Lauf |

## Validierung

```bash
python3 scripts/validate_p03_grabber.py
python3 -m scripts.rki_grabber.rki_epidbull_grabber --help
python3 -m pytest -q tests/test_rki_grabber.py tests/test_rki_parser.py \
  tests/test_rki_http.py tests/test_download_security.py tests/test_grabber_api.py
```

Die ausführliche Architektur- und Betriebsbeschreibung steht in `docs/Wartung/RKI-Grabber.md`; die öffentliche API ist in `docs/API.md` dokumentiert.
