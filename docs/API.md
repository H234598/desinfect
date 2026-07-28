# Python-API des RKI-Grabbers

## Stabiler Einstiegspunkt

```python
from scripts.rki_grabber.api import grab
from scripts.rki_grabber.models import GrabberRequest, GrabberResult, Scope
```

```python
request = GrabberRequest(
    scope=Scope.ISSUES,
    from_year=1994,
    to_year=1996,
    dry_run=True,
    max_items=3,
)
result: GrabberResult = grab(request)
```

`grab()` ist der gemeinsame Einstiegspunkt für CLI, spätere Pipeline und Backfill. Modulimporte führen weder Netzwerkzugriffe, Dateischreibvorgänge noch Prozessabbrüche aus.

## Request

`GrabberRequest` validiert Jahresbereich, Treffergrenze, Delay, Timeout und Größenlimits vor einem Lauf. Kontaktangabe und Outputpfade gehören nicht zur öffentlichen Resultprojektion.

Wichtige Felder:

- `scope`: `issues|articles|all`;
- `from_year`, `to_year`;
- `dry_run`;
- `max_items`;
- `force`;
- `output_root`, optional `result_path`;
- injizierbare Netzwerkgrenzen und Robotsentscheidung.

## Result

`GrabberResult.to_dict()` liefert ausschließlich JSON-kompatible Werte und validiert gegen `schemas/grabber-result.schema.json`. `exit_code` ist stabil:

- `0`: success,
- `2`: partial,
- `3`: blocked,
- `4`: failed/configuration.

Die einzelnen `ArtifactRecord`-Objekte enthalten versionierte Dokument- und Quellen-IDs, Quelle, Publikationsdatum, Roh-Rechtefelder, PDF-Referenz, relativen Zielpfad, State, Prüfsummen, ETag/Last-Modified und strukturierte Fehlerdaten.

## Testports

`grab()` akzeptiert einen `HttpTransport` und eine `now`-Funktion. Dadurch laufen Parser-, Transport-, API- und Downloadregressionen vollständig offline und deterministisch.

## Persistenz

`write_result()` und `write_legacy_outputs()` sind explizite Funktionen. Ein Dry-run ruft sie standardmäßig nicht auf. Materialisierende Ausgaben verwenden die sicheren atomaren IO-Primitiven aus P01.
