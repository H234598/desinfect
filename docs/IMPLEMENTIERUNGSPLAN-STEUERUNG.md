---
title: Implementierungsplan V3 – Steuerung
status: P08.2 abgeschlossen; P09.1 als nächste Phase
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
- **P05:** PR #12, Merge `b1e6b0fa417b1ea879fe373795e320b1950970ba`
- **P05-Gate:** geprüfter Head `d9e1c5b39cc7fb714ba61d089119bfa5b81c080b`; GitHub Actions `30784217751`, CodeRabbit und qlty erfolgreich; alle zehn Reviewthreads aufgelöst; 249 Pytest-, 9 Unittest- und 2 Node-Tests erfolgreich; PR #8 wurde nach Replacement-Kommentar `5162329352` geschlossen und nicht gemergt.
- **P06.1:** PR #14, Merge `c477be9ea08b338d0d981bb052abd023b7f10a87`
- **P06.2:** PR #15, Merge `0e4fe01624d45b750f8a8dd4abf3b5d160e7e46e`
- **P06.3:** PR #16, Merge `54da0963f1b7b0458ae7bb4a0311a02a32e7649e`
- **P06.4:** PR #17, Merge `71f8a58b5c737ad5034da55aad908b7bd2b91080`
- **P06-Gate:** geprüfte Heads `9c38fb5`, `3b1d4bc`, `5a04bee` und `ce27bf1`; GitHub Actions `30798406250`, `30814619838`, `30831150430`, `30838403022` sowie Nach-Merge-Lauf `30838723565` erfolgreich; CodeRabbit und qlty erfolgreich; alle 23 Reviewthreads aufgelöst; 782 Pytest-, 9 Unittest- und 2 Node-Tests erfolgreich; finaler Security-Diff-Scan ohne Finding.
- **P07.1:** PR #19, Merge `cb378bec67ccacd3d3b426e6a8661eeb06ddfe08`
- **P07.1-Gate:** geprüfter Head `5ce347e`; GitHub Actions `30855764141` sowie Nach-Merge-Lauf `30855957537` erfolgreich; CodeRabbit und qlty erfolgreich; beide Reviewthreads aufgelöst; 892 Pytest-, 9 Unittest- und 2 Node-Tests erfolgreich; deterministischer Doppel-CLI-Smoke bytegleich.
- **P07.2:** PR #21, Merge `5cf540d6d9918ae0cab1eb5b2dcfcc6cad521e61`
- **P07.2-Gate:** geprüfter Head `490d5a2`; GitHub Actions `30875762790` sowie Nach-Merge-Lauf `30875875161` erfolgreich; CodeRabbit und qlty erfolgreich; alle fünf Reviewthreads aufgelöst; 1014 Pytest-, 9 Unittest- und 2 Node-Tests erfolgreich; materialisierter Archiv-Smoke und Aggregationsplan erfolgreich.
- **P07.3:** PR #23, Merge `cf756854964cd4be1b092bb19bfd61c9a3e0ac1e`
- **P07.3-Gate:** geprüfter Head `96de454`; GitHub Actions `30893991569` sowie Nach-Merge-Lauf `30894298026` erfolgreich; CodeRabbit und qlty erfolgreich; alle acht Reviewthreads aufgelöst; 1167 Pytest-, 9 Unittest- und 2 Node-Tests erfolgreich; Offline-Reconciliation in `plan` und `materialize` erfolgreich.
- **P08.1:** PR #25, Merge `6fccb8d0ce42f27dd5dca9f47ff2cd169323b0c0`
- **P08.1-Gate:** geprüfter Head `54a6d1e`; GitHub Actions `30901827480` sowie Nach-Merge-Lauf `30902077612` erfolgreich; CodeRabbit und qlty erfolgreich; alle drei Reviewthreads aufgelöst; 1210 Pytest-, 9 Unittest- und 2 Node-Tests sowie Wachhund-CLI im Planmodus erfolgreich.
- **P08.2:** PR #27, Merge `12ac906cdf9982cd91d0bcab5a5e1d783a765ddf`
- **P08.2-Gate:** geprüfter Head `f60a2fc`; GitHub Actions `30910544137` sowie Nach-Merge-Lauf `30911539579` erfolgreich; qlty erfolgreich; CodeRabbit-Hauptreview erfolgreich und finales inkrementelles Walkthrough ohne neue Aktion; einziger Reviewthread aufgelöst; 1327 Pytest-, 9 Unittest- und 2 Node-Tests erfolgreich.
- **Fortschritt:** 29 von 60 Arbeitspaketen umgesetzt; 31 offen; 0 in Arbeit; 0 im Review; 0 blockiert.

## Nächste Phase P09.1

P08.2 ist umgesetzt. P09.1 bleibt `offen`, bis Implementierungsbranch und planmäßige Evidenz vorliegen. P09.1 schafft das Cloudflare-Worker-/Durable-Object-Projektfundament.

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
- [x] **P05.1** Fälligkeitsberechnung und Catch-up _(umgesetzt, PR #12, Merge `b1e6b0fa417b1ea879fe373795e320b1950970ba`)_
- [x] **P05.2** Transaktionaler Pipeline-Orchestrator _(umgesetzt, PR #12, Merge `b1e6b0fa417b1ea879fe373795e320b1950970ba`)_
- [x] **P05.3** GitHub-App-Token, Commit und Push _(umgesetzt, PR #12, Merge `b1e6b0fa417b1ea879fe373795e320b1950970ba`)_
- [x] **P05.4** GitHub-Workflows: Dispatcher, Pipeline und Backfill _(umgesetzt, PR #12, Merge `b1e6b0fa417b1ea879fe373795e320b1950970ba`)_
- [x] **P06.1** Stabile Dokument-IDs, Pfade und Quellmanifest _(umgesetzt, PR #14, Merge `c477be9ea08b338d0d981bb052abd023b7f10a87`)_
- [x] **P06.2** Rechte- und Lizenzpolicy _(umgesetzt, PR #15, Merge `0e4fe01624d45b750f8a8dd4abf3b5d160e7e46e`)_
- [x] **P06.3** PDF-Validierung und Konvertierung _(umgesetzt, PR #16, Merge `54da0963f1b7b0458ae7bb4a0311a02a32e7649e`)_
- [x] **P06.4** Dokument-, Konvertierungs- und Storage-Manifeste _(umgesetzt, PR #17, Merge `71f8a58b5c737ad5034da55aad908b7bd2b91080`)_
- [x] **P07.1** Deterministischer ZIP-Builder _(umgesetzt, PR #19, Merge `cb378bec67ccacd3d3b426e6a8661eeb06ddfe08`)_
- [x] **P07.2** Wochen-, Monats-, Jahresarchive und Nachzügler _(umgesetzt, PR #21, Merge `5cf540d6d9918ae0cab1eb5b2dcfcc6cad521e61`)_
- [x] **P07.3** Quartals-Reconciliation _(umgesetzt, PR #23, Merge `cf756854964cd4be1b092bb19bfd61c9a3e0ac1e`)_
- [x] **P08.1** Interner Wachhund und getrennte Uhren _(umgesetzt, PR #25, Merge `6fccb8d0ce42f27dd5dca9f47ff2cd169323b0c0`)_
- [x] **P08.2** Job Summary, Diagnoseartefakte und Rolling Issue _(umgesetzt, PR #27, Merge `12ac906cdf9982cd91d0bcab5a5e1d783a765ddf`)_
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
