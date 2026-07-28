---
title: Implementierungsplan V3 – Steuerung
status: Umsetzung begonnen
source_plan_sha256: aa50863cde1313a7039691b4ca596c1ab498d0fab0008da324de5cb69f12ffc4
repository: H234598/desinfect
branch: agent/p00-governance-baseline
pull_request: 1
---

# Implementierungsplan V3 – Steuerung

Diese Datei pflegt den aktiven Umsetzungszustand der vollständig bereitgestellten Langfassung. Die Langfassung bleibt durch ihren SHA-256-Fingerabdruck eindeutig eingefroren; ihre 40 MUSS- und 169 V2-IDs sind über die Register unter `docs/requirements/` vollständig einer Umsetzungsregel zugeordnet.

> [!important]
> **ADR-003 = A** und **ADR-014 = B** sind unveränderliche Invarianten. Die Baseline-Validatoren prüfen Revisionspolicy, ADR-Register, ADR-Dateien und Anforderungsregister gegeneinander.

## Aktueller Durchlauf

- **PR:** #1
- **Branch:** `agent/p00-governance-baseline`
- **P00.1 Revisionsblatt und Analysefreeze:** im Review
- **P00.2 Anforderungs- und Entscheidungstraceability:** im Review
- **P00.3 Fortschritts- und Evidenzvertrag:** im Review
- Plan-Checkboxen bleiben bis Merge, grüner CI und vollständiger Evidenz offen.

## Arbeitspakete

- [ ] **P00.1** Revisionsblatt und Analysefreeze _(im Review, PR #1)_
- [ ] **P00.2** Anforderungs- und Entscheidungstraceability _(im Review, PR #1)_
- [ ] **P00.3** Fortschritts- und Evidenzvertrag _(im Review, PR #1)_
- [ ] **P01.1** Python-/Node-Paketfundament
- [ ] **P01.2** Sichere Datei-, Hash- und Stagingprimitive
- [ ] **P01.3** Offline-Fixtures und Testdatenpolicy
- [ ] **P02.1** Schemafamilie und Versionsstrategie
- [ ] **P02.2** Öffentlicher Status und Lauf-/Recovery-Modell
- [ ] **P02.3** Automatische Schreibpfad-Policy
- [ ] **P03.1** Grabber in Parser, HTTP und Orchestrierung schneiden
- [ ] **P03.2** Netzwerk-, Robots- und Downloadhärtung
- [ ] **P03.3** Stabile CLI, API und Resultvertrag
- [ ] **P04.1** RunMode und Seiteneffektwächter
- [ ] **P04.2** Storage Protocol und echte Adapter
- [ ] **P04.3** Git-LFS-Tracking, Objekt- und Budgetprüfung
- [ ] **P04.4** Backend-Migrationswerkzeug
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
