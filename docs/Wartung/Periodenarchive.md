# Periodenarchive

`aggregate` plant und materialisiert deterministische Wochen-, Monats- und Jahresarchive. Es arbeitet nur mit validiertem Manifestgraphen und bereits autorisierten lokalen Objekten. Repository-Apply, Commit und Push bleiben bei P05.

## Periodenabschluss

Periodengrenzen gelten in `Europe/Berlin`. Woche ist ISO-Woche Montag bis Sonntag, Monat und Jahr sind lokale Kalenderperioden. Der stabile `source_date_epoch` ist UTC für den lokalen, exklusiven Folgetag um 00:00 Uhr. Damit bleiben Winterzeit, Sommerzeit und DST-Wechsel korrekt; Laufzeit beeinflusst kein Archiv.

RKI-Korpus beginnt 1996; betriebliche Periodenauswahl beginnt daher 1996. Zusätzlich weist `PeriodRef` jeden negativen Schluss-Epoch sofort ab. Epoch null bleibt konservative technische Untergrenze und liegt vor allen RKI-Dokumenten.

P05 liefert fällige (`due`) Perioden. Spät eingetroffene Dokumente ergänzen diese Auswahl als Vereinigung mit ihren betroffenen Wochen, Monaten und Jahren. Nur vollständig geschlossene Perioden werden verarbeitet. Eine Nachlieferung baut genau ihre historische Woche, ihren Monat und ihr Jahr neu, keine spekulative Catch-up-Periode.

## Layout

- Woche: `rki/Bulletins/Monate/<Montag-YYYY>/<Montag-MM>/ZIP/Wochen/RKI-Einzelartikel-<YYYY-MM-DD>_bis_<YYYY-MM-DD>-PDF|Markdown/`.
- Monat: `rki/Bulletins/Monate/YYYY/MM/ZIP/RKI-Einzelartikel-YYYY-MM-PDF|Markdown/` und `rki/Bulletins/Monate/YYYY/MM/Markdown/index.md`.
- Jahr: `rki/Bulletins/Jahre/YYYY/ZIP/RKI-Einzelartikel-YYYY-PDF|Markdown/`.
- Periodenmanifest: `rki/Bulletins/Manifeste/Archive/<week|month|year>/<period>.json`.

Wochen liegen nach ihrem Montag. Monatsindizes verlinken nur überlappende Wochenbundle, die im aktuellen Plan entstehen oder aus einem vorhandenen Wochenmanifest erneut nach Schema, Sidecar, ZIP, SHA-256, Größe und Identität validiert wurden. Ein fehlendes Wochenmanifest erzeugt keinen theoretischen Link. Behauptet ein vorhandenes überlappendes Manifest ein fehlendes oder beschädigtes Bundle, bricht Monatsmaterialisierung geschlossen ab und lässt vorherige Publikation unverändert.

Jedes Bundle enthält `archive.zip` und `archive-manifest.json`. Leere PDF- oder Markdown-Mengen erzeugen kein Bundle. ZIPs enthalten nur Dokument-Payloads, nie andere ZIPs.

## Manifeste und Backends

Periodenmanifeste enthalten Berlin-Grenzen, `source_date_epoch`, geordnete aktuelle Dokument-/Bitstreamversionen, nullable DOI, Archive, Checksummen und Eingabe-Fingerprint. Identität und Eindeutigkeit verwenden `(document_id, bitstream_id)`; mehrere Bitstreams dürfen dasselbe Dokument und dieselbe Quelle aliasieren. Referenzen verwenden Artefakt-IDs und repository-relative logische Pfade, keine Backend-URLs oder Objektschlüssel. P04-Storage-Referenzen lösen diese backend-neutral auf.

Monatsindex-Metadaten escapen HTML-/Tabellensyntax sowie Backslash und eckige Klammern. Titel in Link- oder Bildsyntax bleiben nicht klickbarer Text.

## Betrieb und Recovery

`plan` schreibt nichts ins Repository und gibt kanonisches JSON auf stdout aus. `materialize` schreibt nur unter einem temporären Root. Betriebssystemfehler erscheinen im CLI als feste Meldung ohne Hostpfad. Identische Eingaben sind No-op: keine Produktänderung und keine neuen Ledger-Ereignisse; `test_materialize_noop_preserves_tree_mtimes_and_outer_ledger` prüft dies gegen dieselbe persistente Produktwurzel. Vor Veröffentlichung schlägt jede Rechte-, Integritäts- oder Manifestprüfung fehl und entfernt Staging; vorherige valide Produkte bleiben erhalten. Beschädigte geplante Produkte werden bei erneuter Materialisierung ersetzt.

Bei einem fehlgeschlagenen Lauf: Ursache in Rechte- oder Manifestvertrag korrigieren, danach denselben `plan` erneut prüfen und `materialize` wiederholen. Kein manueller Repository-Write aus diesem Schritt. P05 führt erst nach erfolgreichem plan/materialize/validate die transaktionale Apply-Grenze aus; erst dort sind Repository-Änderung, Commit oder Push erlaubt.

```bash
python -m scripts.rki_pipeline.cli aggregate --as-of 2026-01-01T05:00:00Z --mode plan
python -m scripts.rki_pipeline.cli aggregate --as-of 2026-01-01T05:00:00Z --mode materialize
```
