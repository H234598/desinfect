# Anforderungsregister

Die Ausgangsanforderungen bleiben über den SHA-256-Fingerabdruck des bereitgestellten Plans und stabile IDs unveränderlich referenzierbar.

- `config/plan-source.json`: Dateiname, Größe und SHA-256 der vollständigen externen Langfassung sowie Pfad und Bytehash der kanonischen Steuerungsdatei.
- `docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md`: kanonisch gepflegte Reihenfolge aller 60 Arbeitspakete.
- `requirement-index.json`: lückenlose 40 MUSS- und 169 V2-IDs.
- `must-register.json`: exakte Kurztexte der MUSS-Anforderungen plus eindeutige Zuordnungsregeln zu Phase, Zielpfad, blockierendem Test und Abnahme.
- `v2-register.json`: Präfixregeln für alle V2-Zeilen; der Validator löst jede der 169 IDs genau einer Regel zu.

`python3 scripts/validate_requirements.py` berechnet den SHA-256 der kanonischen Steuerungsdatei neu, vergleicht ihn mit `config/plan-source.json` und prüft danach alle Register gegen den eingefrorenen Ursprungsfingerabdruck.

Eine Regel darf keine ID doppelt abdecken. Eine nicht abgedeckte ID, eine leere Test-/Pfadangabe, eine Drift der Steuerungsdatei oder eine Abweichung von **ADR-003=A** beziehungsweise **ADR-014=B** blockiert die Baseline.
