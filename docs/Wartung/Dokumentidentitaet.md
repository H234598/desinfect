# Dokumentidentität und Manifeste

`document_identity(handle)` akzeptiert nur numerische RKI-Handles. Keine Suffix-Version bedeutet Version 1. `document_id` ist `rki-<prefix>-<nummer>-v<version>`; Versionen ab 2 verweisen mit `supersedes` auf Vorgänger.

`bitstream_identity(url)` akzeptiert nur HTTPS-URLs von `edoc.rki.de`, PDF-Pfade für RKI-Handles und optionalen positiven `sequence`-Parameter. `isAllowed=y` wird bei Kanonisierung entfernt; unbekannte, doppelte oder fragmentierte Query-Parameter werden abgewiesen. `bitstream_id` ist SHA-256 der kanonischen URL. Fehlende `sequence` bleibt `null`, nicht Version 1.

Vollständiges Publikationsdatum stammt aus RKI-Metadaten. Daraus folgen ISO-Woche, Kalendermonat und Jahr; Pfade richten sich nach diesem Datum. Bei gleichem Datei-SHA-256 ist niedrigste `bitstream_id` kanonisch; jede andere Bitstream-Manifestdatei setzt `same_content_as` explizit auf diese ID.

Schema-Versionen werden nur über registrierte Ein-Schritt-Migrationen aktualisiert. Vor Rückgabe und vor Schreiben validiert jeder Builder gegen `source-manifest` beziehungsweise `document-manifest`.

Prüfen:

```bash
.venv/bin/pytest -q tests/test_document_identity.py tests/test_paths.py tests/test_source_manifest.py
.venv/bin/python scripts/validate_schemas.py
```

Rollback: neuen Manifest-Entwurf verwerfen. Nie alte Dokumentversion löschen; spätere Version mit `superseded_by` verknüpfen.
