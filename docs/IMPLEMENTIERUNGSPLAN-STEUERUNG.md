---
title: Implementierungsplan V3 – Steuerung
status: P04 abgeschlossen; P05 als nächste Phase
source_plan_sha256: aa50863cde1313a7039691b4ca596c1ab498d0fab0008da324de5cb69f12ffc4
repository: H234598/desinfect
branch: main
pull_request: null
---

# Implementierungsplan V3 – Steuerung

Diese Datei pflegt den aktiven Umsetzungszustand der vollständig bereitgestellten Langfassung. Die externe Langfassung ist durch Dateiname, Bytezahl und SHA-256 in `config/plan-source.json` eingefroren. Diese Steuerungsdatei ist die kanonische Repositoryprojektion; ihr Bytehash wird bei jedem Baselinelauf neu berechnet. Ihre 40 MUSS- und 169 V2-IDs sind über die Register unter `docs/requirements/` vollständig einer Umsetzungsregel zugeordnet.

> [!important]
> **ADR-003 = A** und **ADR-014 = B** sind unveränderliche Invarianten. Die Baseline-Validatoren prüfen Revisionspolicy, ADR-Register, ADR-Dateien, Anforderungsregister, den eingefrorenen Ursprungsfingerabdruck und den Hash dieser kanonischen Steuerungsdatei gegeneinander. Die Antworten bleiben auch bei rechtssicherer Präzisierung ihrer Ausführung unverändert.

## Abgeschlossene Phasen

- **P00:** PR #1, Merge `c4996105f6d683c2c4d342df6ee43b74dbcb64a6`
- **P01:** PR #3, Merge `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2`
- **P02:** PR #5, Merge `947b2ba86792d5a84e0f2fd972cfbe554c156afc`
- **P03:** PR #7, Merge `e180b20788072bba840e655d493bac73c7f1a3ee`
- **P03-Gate:** geprüfter Head `71943a05fe0f6a2a013f1794e64601b32a44d079`; GitHub Actions `30584133252`, CodeRabbit und qlty erfolgreich; alle zehn Reviewthreads aufgelöst.
- **P04:** PR #10, Merge `b7148bb362425bc6f5a0d30b27a78539ec3acc75`
- **P04-Gate:** geprüfter Head `62ef771edaba96d5e3212d1525164367d3e46dbe`; GitHub Actions `30665523318`, CodeRabbit und qlty erfolgreich; alle 19 Reviewthreads aufgelöst; 171 Pytest-, 9 Unittest- und 2 Node-Tests erfolgreich.
- **Fortschritt:** 16 von 60 Arbeitspaketen umgesetzt; 44 offen; 0 in Arbeit; 0 im Review; 0 blockiert.

## Nächste Phase P05

P05.1 bis P05.4 bleiben `offen`, bis der eigene Implementierungsbranch und die planmäßige Evidenz vorliegen. P05 führt Fälligkeitsberechnung und Catch-up, den transaktionalen Pipeline-Orchestrator, GitHub-App-Token mit Commit/Push sowie Dispatcher-, Pipeline- und Backfill-Workflows ein.

## Arbeitspakete

- [x] **P00.1** Revisionsblatt und Analysefreeze _(umgesetzt, PR #1, Merge `c4996105f6d683c2c4d342df6ee43b74dbcb64a6`)_
- [x] **P00.2** Anforderungs- und Entscheidungstraceability _(umgesetzt, PR #1, Merge `c4996105f6d683c2c4d342df6ee43b74dbcb64a6`)_
- [x] **P00.3** Fortschritts- und Evidenzvertrag _(umgesetzt, PR #1, Merge `c4996105f6d683c2c4d342df6ee43b74dbcb64a6`)_
- [x] **P01.1** Python-/Node-Paketfundament _(umgesetzt, PR #3, Merge `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2`)_
- [x] **P01.2** Sichere Datei-, Hash- und Stagingprimitive _(umgesetzt, PR #3, Merge `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2`)_
- [x] **P01.3** Offline-Fixtures und Testdatenpolicy _(umgesetzt, PR #3, Merge `4fc4aca667ce1b7a9529cc49e4e81fc373f75da2`)_
- [x] **P02.1** Schemafamilie und Versionsstrategie _(umgesetzt, PR #5, Merge `947b2ba86792d5a84e0f2fd972cfbe554c156afc`)_
- [x] **P02.2** Öffentlicher Status und Lauf-/Recovery-Modell _(umgesetzt, PR #5, Merge `947b2ba86792d5a84e0f2fd972cfbe554c156afc`)_
- [x] **P02.3** Automatische Schreibpfad-Policy _(umgesetzt, PR #5, Merge `947b2ba86792d5a84e0f2fd972cfbe554c156afc`)_
- [x] **P03.1** Grabber in Parser, HTTP und Orchestrierung schneiden _(umgesetzt, PR #7, Merge `e180b20788072bba840e655d493bac73c7f1a3ee`)_
- [x] **P03.2** Netzwerk-, Robots- und Downloadhärtung _(umgesetzt, PR #7, Merge `e180b20788072bba840e655d493bac73c7f1a3ee`)_
- [x] **P03.3** Stabile CLI, API und Resultvertrag _(umgesetzt, PR #7, Merge `e180b20788072bba840e655d493bac73c7f1a3ee`)_
- [x] **P04.1** RunMode und Seiteneffektwächter _(umgesetzt, PR #10, Merge `b7148bb362425bc6f5a0d30b27a78539ec3acc75`)_
- [x] **P04.2** Storage Protocol und echte Adapter _(umgesetzt, PR #10, Merge `b7148bb362425bc6f5a0d30b27a78539ec3acc75`)_
- [x] **P04.3** Git-LFS-Tracking, Objekt- und Budgetprüfung _(umgesetzt, PR #10, Merge `b7148bb362425bc6f5a0d30b27a78539ec3acc75`)_
- [x] **P04.4** Backend-Migrationswerkzeug _(umgesetzt, PR #10, Merge `b7148bb362425bc6f5a0d30b27a78539ec3acc75`)_
- [ ] **P05.1** Fälligkeitsberechnung und Catch-up
- [ ] **P05.2** Transaktionaler Pipeline-Orchestrator
- [ ] **P05.3** GitHub-App-Token, Commit und Push
- [ ] **P05.4** GitHub-Workflows: Dispatcher, Pipeline und Backfill
- [ ] **P06.1** Stabile Dokument-IDs, Pfade und Quellmanifest
- [ ] **P06.2** Rechte- und Lizenzpolicy
- [ ] **P06.3** PDF-Validierung und Konvertierung
- [ ] **P06.4** Dokument-, Konvertierungs- und Storage-Manifeste
- [ ] **P07.1** Deterministischer ZIP-Builder
- [ ] **P07.2** Wochen-, Monats-, Jahresarchive und Nachzügler
- [ ] **P07.3** Quartals-Reconciliation
- [ ] **P08.1** Interner Wachhund und getrennte Uhren
- [ ] **P08.2** Job Summary, Diagnoseartefakte und Rolling Issue
- [ ] **P09.1** Cloudflare-Worker-/DO-Projektfundament
- [ ] **P09.2** GitHub-App-JWT und feste API-Operationen
- [ ] **P09.3** DO-Sperre, Idempotenz, Alarm und Nachkontrolle
- [ ] **P09.4** Cloudflare-Deploy und Betriebsgrenzen
- [ ] **P10.1** Contentmodell, Wikilinks und Callouts
- [ ] **P10.2** Atomarer Webbuild und MkDocs Strict
- [ ] **P10.3** Informationsarchitektur, Wartungsicon und Tabellen
- [ ] **P10.4** Backendneutrale Downloads, Status- und Rechtshinweise
- [ ] **P11.1** Read-only PR-Validierung mit Diagnosen
- [ ] **P11.2** Pages Build/Deploy aus `site/`
- [ ] **P11.3** Supply-Chain, CODEOWNERS und Branchschutz
- [ ] **P11.4** Browser-, Accessibility- und Security-Smokes
- [ ] **P12.1** Offline-End-to-End-Pilot
- [ ] **P12.2** Kleiner echter RKI-Plan-/Materialize-Pilot
- [ ] **P12.3** Backend-Migrationsprobe und kontrollierter Apply
- [ ] **P13.1** Rechte-Audit und Quelleninventar 1994–2020
- [ ] **P13.2** Batch-Backfill plan→materialize→apply
- [ ] **P13.3** Reconciliation und Readiness bis 2020
- [ ] **P14.1** Quellennahe Merkmalsextraktion
- [ ] **P14.2** Korpusanalyse und Taxonomievorschlag
- [ ] **P15.1** Fachreview und versionierte Taxonomiefreigabe
- [ ] **P15.2** Deterministischer Mapper und Kategorieseiten
- [ ] **P16.1** Deep-Research-Importvertrag und Quellenvalidierung
- [ ] **P16.2** Anleitungs- und Bulletinseiten
- [ ] **P16.3** Historische, medizinische und rechtliche Einordnung
- [ ] **P17.1** Backfill 2021 bis aktuelles Jahr
- [ ] **P17.2** Produktivierung Dispatcher und externer Wächter
- [ ] **P17.3** Reconciliation-, Restore-, Kapazitäts- und Rotationsbetrieb
- [ ] **P18.1** Dokument- und Navigationsgraph
- [ ] **P18.2** Downloadzentrum und Offline-/Obsidian-Paket
- [ ] **P18.3** Promptdrift, Performance- und Wartungsmigrationsgates
