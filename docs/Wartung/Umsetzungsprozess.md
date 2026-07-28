# Umsetzungsprozess

1. Arbeitspaket auf `in_arbeit` setzen und Branch dokumentieren.
2. Änderungen und lokale Tests durchführen.
3. Nach Eröffnung eines PRs Status auf `im_review` setzen und PR-Nummer erfassen.
4. Erst nach Merge, grünen Required Checks und Abnahme auf `umgesetzt` setzen.
5. `python scripts/validate_baseline.py` ausführen.

Für `umgesetzt` sind mindestens PR-Nummer, Merge-SHA, CI-Lauf, Testbefehle, Abnahmedatum und Abnehmer erforderlich. ADR-003=A und ADR-014=B bleiben dauerhaft gesperrt.
