# Deterministisches Archivformat P07.1

P07.1 erzeugt rechtegebundene, byteidentische ZIP-Bundles aus bereits materialisierten
`PreparedObject`-Werten. Es liest keine Netzdienste und entpackt keine Archive.

## Bundle und interne Dateien

Ein materialisiertes Bundle enthält genau den Generated-Root-Sentinel, `archive.zip` und
das kanonische `archive-manifest.json`. Der Sidecar folgt Schema `archive-manifest` 1.0.0
und bindet Archiv-ID, Periode, Art, sortierte Payloadpfade, Eingabefingerprint und
SHA-256 der ZIP-Datei. `storage_reference` bleibt in P07.1 `null`.

`archive.zip` enthält exakt folgende Metadaten und die lexikalisch sortierten,
kanonischen Payloadpfade:

- `MANIFEST.json`: kanonisches JSON mit Formatversion, Archividentität, Periode, Art,
  Sichtbarkeit, normalisierter ZIP-Zeit, Eingabefingerprint und Pfad/Bytes/SHA-256 je
  Payload.
- `README.md`: lesbare Wiederholung der gebundenen Archividentität.
- `SHA256SUMS.txt`: eine sortierte SHA-256-Zeile je Payload; Metadaten sind wegen der
  sonst selbstreferenziellen Prüfsumme nicht enthalten.

Metadaten- und Payloadmitglieder stehen gemeinsam in lexikalischer Reihenfolge.
Absolute Pfade, `..`, Backslashes, nicht-NFC-normalisierte Namen, portable
Groß-/Kleinschreibungs-Kollisionen, reservierte Metadatennamen und verschachtelte ZIPs
sind unzulässig.

## Feste ZIP-Metadaten

Alle Mitglieder verwenden `ZIP_STORED`, Unix-Erzeugersystem 3, regulären Dateimodus
`0644`, ZIP-Version 2.0 und leere Extra-/Kommentarfelder. ZIP64 ist deaktiviert.
`SOURCE_DATE_EPOCH` wird als UTC interpretiert, auf 1980-01-01 bis
2107-12-31T23:59:58 begrenzt und auf eine gerade Sekunde abgerundet.

`ZIP_STORED` vermeidet von zlib- und Runner-Versionen abhängige Bytes; PDFs gewinnen
durch erneute Kompression meist wenig. Die Decke sind größere Markdownarchive. Wechsel
zu einem gepinnten deterministischen Kompressor erfordert gemessenen Speicherbedarf,
neue Archivformatversion und Migrationstest.

## Eingabefingerprint

Der Eingabefingerprint ist SHA-256 über kanonisches JSON mit:

- Archivformatversion;
- Archiv-ID, Periode und Art;
- Sichtbarkeit;
- normalisierter ZIP-Zeit;
- sortierten Payloaddatensätzen aus Pfad, Bytezahl und SHA-256.

Vor Build und No-op wird jedes `PreparedObject` gegen die frisch geladene
`RightsStorageAuthorizer`-Entscheidung autorisiert. Die Quelldatei muss unter ihrem
expliziten `temp_root` liegen und weiterhin exakt zu Größe und SHA-256 passen.

## Sicherheitslimits und Validierung

Standardlimits:

- höchstens 10.000 Payloadmitglieder plus drei interne Metadatenmitglieder;
- höchstens 256 MiB pro Mitglied;
- höchstens 4 GiB gesamte unkomprimierte Mitgliedsdaten;
- höchstens 4 GiB ZIP-Dateigröße;
- höchstens Kompressionsverhältnis 100:1.

Validierung erfolgt ohne Extraktion. Sie prüft Dateityp und unveränderte Dateiidentität,
SHA-256, CRC, lokale und zentrale ZIP-Header, Reihenfolge, Flags, Modus, Zeit,
Kompressionsmethode, Namen und alle Limits. Danach werden `MANIFEST.json`,
`SHA256SUMS.txt`, `README.md`, Payloadgrößen und Payload-SHA-256 gegeneinander sowie
gegen erwarteten Eingabefingerprint und erwartete Ausgabe-SHA-256 geprüft. Doppelte
JSON-Schlüssel, nichtendliche Zahlen, nichtkanonisches JSON, Symlinks, Geräte,
Verzeichnisse, Verschlüsselung, unbekannte Dateien und Schemaabweichungen brechen
fail-closed ab.

## No-op und Rollback

Ein vorhandenes Bundle ist nur dann No-op, wenn aktuelle Rechte weiter gelten, der
Sidecar exakt zum erwarteten Fingerprint passt und Sidecar, ZIP sowie alle Payloads die
vollständige Validierung bestehen. Dann bleiben mtimes und Ledger unverändert.
Veraltete oder beschädigte reguläre Bundles werden vollständig neu gebaut.

Neubau erfolgt in einem Generated-Staging-Verzeichnis unter dem expliziten
`temp_root`. Nach vollständiger Validierung von ZIP und Sidecar werden zwei
`TEMP_FILE`-Ereignisse vorläufig aufgezeichnet, solange der Staging-Kontext noch offen
ist. Erst dessen erfolgreicher Abschluss ersetzt das Ziel atomar. Jeder Fehler beim
Aufzeichnen oder Veröffentlichen entfernt die Stagingdaten, kürzt die vorläufigen
Ledger-Ereignisse und bewahrt das vorherige veröffentlichte Bundle.

## Offline-Smoke

```bash
python -m scripts.rki_pipeline.cli build-archive --fixture pilot --mode materialize
```

Nur `--fixture pilot --mode materialize` ist zulässig. Der Befehl legt autorisierte,
synthetische Eingabe und Bundle in einem `TemporaryDirectory` an, materialisiert und
validiert die ZIP erneut, gibt kanonisches JSON mit `changed`, `input_fingerprint`,
`output_sha256` und `bytes` aus und entfernt danach das gesamte Temporärverzeichnis.
Unbekannte Fixtures oder Modi enden mit Status 2. Der Befehl schreibt weder
Repositorydateien noch Backendobjekte.

## Betriebsgrenze

P07.1 wählt keine Wochen-, Monats- oder Jahresperioden aus und veröffentlicht weder in
Repositorypfade noch Storage-Backends. Planung, periodische Zielpfade,
Repository-/Backend-Publikation und spätere Aggregation gehören P07.2; Reconciliation
gehört P07.3.
