# Kanonische Planprovenienz

Der vom Nutzer bereitgestellte Implementierungsplan V3 wird über Dateiname, Bytezahl und SHA-256 in `config/plan-source.json` unveränderlich identifiziert. Die vollständige Langfassung bleibt die externe Ursprungsquelle dieses Umsetzungsauftrags; sie wird nicht als schwer wartbare Binärkopie dupliziert.

Die im Repository kanonisch gepflegte Ausführungssteuerung ist `docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md`. Ihr Bytehash wird ebenfalls in `config/plan-source.json` geführt und von `python3 scripts/validate_requirements.py` berechnet. Jede Änderung an Reihenfolge, Paketbestand oder gesperrten Entscheidungen erfordert damit eine bewusste Provenienzaktualisierung.

Unveränderliche Entscheidungen:

- **ADR-003 = A**
- **ADR-014 = B**
