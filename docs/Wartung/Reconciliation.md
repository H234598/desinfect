# Reconciliation

P07.3 prüft den veröffentlichten RKI-Bestand deterministisch. Sie diagnostiziert
Drift; sie repariert und publiziert nichts. P05 bleibt Scheduler und
Transaktionsgrenze. P07.1/P07.2 bleiben Autorität für Archivbytes und
Periodenmanifeste.

## Autoritäten und Prüfreihenfolge

`plan_reconciliation` verwendet diese Autoritäten in fester Reihenfolge:

1. Remote-Metadatensnapshot: aktueller Handle, Version, URLs, ETag,
   Last-Modified, Rechte-Metadaten und optionaler Content-Hash.
2. P06-Quellen-, Dokument- und Konvertierungsmanifeste: kanonische Identität,
   aktuelle nicht-supersedierte Quelle und Provenienz.
3. P04-Storage-Referenzen und Adapter: Backend, Objekt, Pfad, Größe, SHA-256,
   Sichtbarkeit sowie LFS-Pointer und -Objekt.
4. P06-Rechteauthority und kanonisches Register: aktuelle
   `decision_sha256`, Zustand und Veröffentlichungseignung.
5. P07-Periodenmanifest- und Archivvalidatoren: Woche, Monat, Jahr,
   Bundle-Sidecar, ZIP, Checksummen, Fingerprint und Mitgliedsidentitäten.

Zeit, Bereich, Top-Level-Typen, Remotesnapshot und Periodenroot werden zuerst
vollständig validiert. Danach wird der Manifestgraph auf aktuelle,
nicht-supersedierte Dokumente im angeforderten Jahresbereich eingeschränkt;
zugehörige Quellen, Konvertierungen, Storage-Referenzen und Remoteeinträge
folgen dieser Auswahl. Erst dann ruft die Komposition Remotequellen, Storage,
Rechte und Perioden/Archive in dieser Reihenfolge auf. Ungültige Eingaben
brechen damit vor Loadern oder Adaptern fail-closed ab. Doppelte Findings aus
Komponenten brechen beim Zusammenführen ebenfalls fail-closed ab.

## Findings und Zählung

| Code | Bedeutung | Zähler |
|---|---|---|
| `new` | Remote-Quelle/Bitstream fehlt im lokalen Manifestgraphen. | `missing_local` |
| `changed` | Metadaten, Bytes, Pointer, Manifest oder Archiv driften. | `changed` |
| `missing_remote` | Aktuelle lokale Quelle fehlt im Remotesnapshot. | `missing_remote` |
| `missing_local` | Erforderliches Objekt, lokaler Pfad, Manifest oder Bundle fehlt. | `missing_local` |
| `orphan` | Backendreferenz/-objekt ist nicht aus dem Manifestgraphen erreichbar. | `orphan` |
| `rights_changed` | Aktuelle Rechteentscheidung oder Veröffentlichungseignung driften. | `rights_changed` |
| `ok` | Eine vollständig geprüfte aktuelle Quelle/Bitstream hat kein offenes Finding. | `ok` |

Jedes nicht-`ok`-Finding erhöht `unresolved` um eins. `new` besitzt keinen
gleichnamigen Aggregatzähler: Es erhöht `missing_local`. Reihenfolge ist Code,
Subject-Kind, Subject-ID, relativer Pfad. Quellenbezogene Subjects verwenden
`<source_id>#<bitstream_id>`. Orphans mit ausgewähltem Graph-Owner verwenden
ebenfalls diese ID; ein Inventareintrag ohne Graph-Owner verwendet seine
Artefakt-ID. Nur im vollständigen Manifestgraph nachgewiesene, aber aus dem
Jahresbereich ausgeschlossene Inventareinträge werden ignoriert.

## Remote- und lokale Prüfung

Remote und lokale aktuelle Quellen verbinden sich exakt über
`(source_id, bitstream_id)`. Kandidatbytes werden nur bei byte-relevantem Drift
einer bekannten Remotequelle geladen: Version, Quell- oder Bitstream-URL,
Bitstreamidentität, ETag, Last-Modified oder gelieferter SHA-256. Abweichendes
Publikationsdatum oder abweichende Rechte-Evidenz bleibt Metadaten-`changed` und
lädt keine Bytes. Stabile Metadaten rufen ebenfalls keinen Kandidatenloader auf:
Der Spy-Test
`test_remote_metadata_avoids_blind_candidate_load` belegt dieses
No-Blind-Download-Verhalten. Auch passende Kandidatbytes heben ein
Metadaten-`changed` nicht auf.

Storageprüfung verwendet ausschließlich den passenden P04-Adapter. Für LFS
prüft dieser Pointer und lokales Objekt mit Pfad, Größe und SHA-256; kein
direkter LFS-Scan ersetzt den Adapter. Aufrufer müssen eine frisch aus dem
kanonischen Register geladene Rechteauthority bereitstellen. Für jedes aktuelle
Dokument mit Datum müssen valide Woche, Monat und Jahr sowie ihre Archive
vollständig sein. Ohne vollständiges Datum ist nur die Jahresprüfung zulässig;
Woche oder Monat werden nicht erfunden.

## Berichte, Wasserstand und Recovery

Der Report erfüllt `reconciliation-report` Schema `1.0.0`. Ein erfolgreicher
Lauf materialisiert ausschließlich unter temporärem Root nach:

```text
rki/Bulletins/Manifeste/Reconciliation/reconciliation-YYYYMMDDTHHMMSSZ.json
```

Er enthält Bereich, UTC-`as_of`, Zähler, Schlussfolgerung und SHA-256 der
kanonischen Bytes von `Quellen/manifest.jsonl`. Berichte sind unveränderliche
Historie: identische Bytes sind No-op; abweichende Bytes am selben Namen
blockieren. `success` setzt `successful_at` auf `as_of`; nur ein nicht-null
Wasserstand darf später durch P05 übernommen werden. `blocked` besitzt
`successful_at=null`, schreibt keinen kanonischen Report und erzeugt keine
persistenten Ledger-Effekte.

Bei `blocked` bleiben Findings transiente Diagnose. Ursache im zuständigen
Manifest-, Rechte-, Storage- oder P07-Vertrag korrigieren, danach neu planen
und materialisieren. Rollback stellt letzten gültigen Zustand wieder her oder
nimmt nur den fehlerhaften P05/P07-Publikationsschritt zurück. ZIPs,
Periodenmanifeste und Reconciliationreports nie manuell editieren.

## Betrieb

Offline-Drill mit kanonischer Fixture:

```bash
python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode plan
python3 -m scripts.rki_pipeline.cli reconcile --fixture tests/fixtures/reconciliation --mode materialize
```

`plan` ist read-only und gibt kanonische JSON-Evidenz aus. `materialize`
schreibt nur in seinen isolierten temporären Root. `apply` wird abgewiesen.

Nichtziele: keine automatische Reparatur, kein `apply`, kein Backfill und kein
P13-Readiness-Gate. Reconciliation lädt keine stabilen Quellen blind, schreibt
kein kanonisches Repository und löscht keine Remote- oder Orphanobjekte.
