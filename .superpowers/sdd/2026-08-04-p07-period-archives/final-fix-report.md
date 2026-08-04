# P07.2 Final Fix Report

## Ergebnis

Finale Review-Welle schließt Alias-/DOI-Identität, historische Wochenlinks,
Markdown-Injection, durable Staging-Publikation und geforderte kleinere
Vertragslücken. Kein Push, kein Repository-Apply.

## Commits

- `191713f` — `fix(p07): preserve alias and DOI identity`
- `ae72501` — `fix(p07): retain validated weekly links`
- `2e8822f` — `fix(p07): pin published directory through commit`
- `7672035` — `docs(p07): sync final archive contracts`
- `ab7adce` — `fix(p07): remove unused weekly local`

## Behobene Punkte

- `PeriodDocument.bitstream_id` läuft durch Planfingerprint, Index,
  Periodenmanifest, ZIP und Materialisierung. Eindeutigkeit ist
  `(document_id, bitstream_id)`; gleiche `source_id` bleibt für valide Aliase
  erlaubt, echte Paar-Kollisionen blockieren.
- `ArtifactRecord.doi` läuft über `source-manifest` 1.2.0 und Manifestgraph bis
  Monatsindex. Direkte Migrationen von 1.0.0/1.1.0 ergänzen `doi: null`;
  Produktionsfixture und Katalog sind auf 1.2.0 aktualisiert.
- Month-only rebuild lädt über gehaltene Staging-FDs nur überlappende,
  schema-/Sidecar-/ZIP-/SHA-/Größen-validierte Wochenreferenzen. Fehlendes
  Manifest erzeugt keinen Link; fehlendes oder korruptes behauptetes Bundle
  bricht geschlossen ab und erhält alte Publikation.
- Monatsrenderer konsumiert sortierte, unveränderliche und gefingerprintete
  aktuelle plus historische Wochenreferenzen.
- Markdown-Zellen escapen Backslash und eckige Klammern zusätzlich zu HTML,
  Pipe und Zeilenumbrüchen. Link-/Bildtitel bleiben inert.
- Negative Schluss-Epochen scheitern in `PeriodRef`; RKI-Betriebsminimum 1996
  und konservative technische Epoch-Untergrenze sind dokumentiert.
- Aggregate-CLI gibt bei `OSError` nur feste pfadfreie Meldung aus.
- `staged_directory` trägt exakt denselben offenen Publikations-FD von Rename
  durch Validator, Ownership, Commit/Rollback und Cleanup. Parent-`fsync` und
  `publication_committed=True` liegen innerhalb Signal-Guard.
- Parent-weite nonblocking `flock` und Geschwisterziel-Fail-fast sind getestet
  und dokumentiert.
- Plan, Design und Wartungsdokumentation spiegeln finale Verträge.

## RED/GREEN-Evidenz

- Alias/DOI-Fokus: 242 passed; Schema-, Fixture- und Manifestvalidatoren grün.
- Historische Wochenlinks: gültiger Carry-forward zunächst RED; danach
  month-only PDF/Markdown-Linkerhalt grün. Korruptes und fehlendes behauptetes
  Wochenbundle fail-closed grün.
- Markdown-Injection: 2 RED, danach 2 grün.
- Epoch/CLI: 2 RED, danach grün.
- Staging-Signalreihenfolge und exakter FD zunächst RED; kompletter
  `tests/test_staging.py` danach 21 passed.
- Kombinierter Staging/Conversion/Period/Archive-Fokus: 242 passed.
- Finaler Vertragsfokus: 379 passed.

## Abnahme

- `python3 -m pytest -q` — 999 passed in 42.51s auf finalem Codezustand.
- `python3 -m unittest discover -s tests -p 'test_*.py'` — 9 passed.
- Baseline-, P01–P05-, Rechte-, Manifest-, Fixture-, Schema-, CI- und
  Requirements-Validatoren — alle grün.
- `python3 -m compileall -q scripts tests` — grün.
- `ruff check scripts tests` — grün.
- `npm test` — 2 passed.
- `git diff --check 8d06c7d..HEAD` — grün.

## Offene Punkte

Keine im beauftragten Final-Fix-Scope.
