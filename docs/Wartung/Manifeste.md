# Manifestkataloge

P06 führt Quellen, Dokumente, Konvertierungen und Storage-Referenzen als kleinen,
normalen Git-Bestand. Validierung benötigt weder PDF-/Markdown-Artefakte noch
einen Git-LFS-Fetch.

## Layout

Ein Snapshot besitzt genau diese Dateien:

```text
rki/Bulletins/Manifeste/
├── Quellen/manifest.jsonl
├── Dokumente/manifest.jsonl
├── Konvertierungen/manifest.jsonl
├── Storage/manifest.jsonl
└── catalog.json
```

JSONL-Zeilen sind kanonisches UTF-8-JSON, nach Primäridentität sortiert und mit
LF abgeschlossen. `catalog.json` bindet pro Sammlung Pfad, Typ, Datensatzanzahl,
Bytezahl und SHA-256. SHA-256 bezeichnet immer Artefaktbytes. Bei Git LFS enthält
`storage_object_id` deshalb `sha256:<artifact sha256>`; Pointertext ist keine
Artefaktidentität.

## Referenzvertrag

`build_manifest_graph(..., authorizer=...)` validiert jeden Datensatz zuerst
gegen seinen registrierten Vertrag (`source-manifest` 1.2, übrige P06-Manifeste
1.1) und akzeptiert nur `provenance_state=current`.
Authorizer löst jede Quelle frisch gegen kanonisches Rechte-Register und Policy
auf; jede Storage-Referenz wird erneut autorisiert. Danach gelten:

- Quelle und Dokument teilen exakte `source_id`, `bitstream_id`, Version,
  Publikationsdatum und kanonische Repositorypfade. Quell- und Bitstream-URL
  müssen auf denselben RKI-Handle zeigen.
- Gleicher Inhalt verwendet kleinste `bitstream_id` als kanonisches Aliasziel.
- Dokument-Supersession ist reziprok und zyklenfrei.
- Konvertierung bindet Dokument, Bitstream und Quell-SHA; höchstens eine
  persistierte Konvertierung je Dokument/Bitstream besitzt eine Storagekante.
- PDF-Storage bindet Quell-SHA und PDF-Pfad. Markdown-Storage bindet
  `conversion_id`, Output-SHA und Markdown-Pfad.
- Byteidentische Aliasquellen dürfen Storage-Objekte deduplizieren; dieselbe
  Backend-/Objekt-ID mit widersprüchlichen Hashes blockiert.
- Storage übernimmt `decision_sha256` und Rechtezustand unverändert aus Quelle.
  Öffentliche Referenzen benötigen `visibility=public`.

Fehlende Konvertierung oder Storagekante ist zulässiger Fälligkeitszustand,
solange kein Manifest eine nicht auflösbare Kante behauptet. Explizite dangling
IDs, Drift, Orphans, doppelte Identitäten und Pfadkollisionen blockieren.

## Materialisierung

`materialize_manifest_catalog(graph, temp_root=..., ledger=...,
authorizer=...)` verlangt passenden `RunMode.MATERIALIZE`-Ledger und frischen
Rechte-Authorizer. Sichtbare Effekte sind ausschließlich `TEMP_FILE` unter
`temp_root`. Ein exklusives Verzeichnis-Lock serialisiert konkurrierende
Publisher. Aufbau erfolgt vollständig descriptor-relativ in Sentinel-geschütztem
Staging; Veröffentlichung ersetzt gesamten Snapshot atomar. Kein Append-in-place.

Identische Zielbytes ergeben No-op: keine Ledger-Events, keine geänderten mtimes.
Schlägt Aufbau oder Vorabvalidierung fehl, bleibt alter Snapshot erhalten. Ein
vorhandener unmarkierter oder ungültiger Zielbaum wird nicht überschrieben.

## Striktes Lesen und Betrieb

`load_manifest_catalog()` liest über gehaltene Verzeichnisdeskriptoren. Symlinks,
unregistrierte Dateien, BOM, Leerzeilen, doppelte JSON-Schlüssel, `NaN`/Infinity,
nichtkanonisches JSON, Größenüberschreitungen sowie Katalog-/Graphdrift werden
fail-closed abgelehnt.

Prüfung:

```bash
python3 scripts/validate_manifests.py --root tests/fixtures/manifests
python3 -m pytest -q tests/test_manifests.py
python3 scripts/validate_fixture_manifest.py
```

Fehlerbehebung: gesamten letzten gültigen Snapshot wiederherstellen und erneut
validieren. Nie einzelne JSONL-Zeilen anhängen, korrupte Referenzen still löschen
oder LFS-Artefakte als Reparaturmaßnahme entfernen.
