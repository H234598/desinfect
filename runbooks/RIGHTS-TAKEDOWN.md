# Rights Takedown

## Auslöser

Dieses Runbook gilt bei Rechtewiderruf, belastbarer Beschwerde, fehlerhafter
Freigabe oder einer Entscheidung mit Zustand `takedown`. Das technische Gate
blockiert erst, nachdem die aktuelle `takedown`-Entscheidung gemergt wurde. Bis
dahin müssen Betreiber die betroffenen Pipeline- und Publikationsläufe sofort
pausieren.

## Sofortmaßnahmen

1. Betroffene geplante und manuelle Publikationsläufe sofort pausieren und den
   Freeze mit Zeitpunkt und Verantwortlichen protokollieren.
2. Vorfall, betroffene `source_id`, `source_sha256`, `decision_sha256`,
   Storage-Referenzen, URLs und Zeitpunkt protokollieren. Keine Evidenz löschen.
3. Manuelle rechtliche Prüfung einleiten. Nur ein menschlich reviewter PR darf
   `research/rights-register.yml` auf `takedown` setzen.
4. Register und Tests prüfen:

   ```bash
   python3 scripts/validate_rights_register.py
   python3 -m pytest -q tests/test_rights_policy.py tests/test_storage_contract.py
   ```

5. Im nächsten Site-Build alle betroffenen Artefakt- und Downloadreferenzen
   deaktivieren. RKI-Originallink, Quellmetadaten, Hashes und
   Provenienzinformationen bleiben im Inventar erhalten.
6. GitHub Pages neu bauen und veröffentlichen. Danach öffentlich prüfen, dass
   weder Downloadlink noch Backend-URL oder eingebetteter Volltext erreichbar
   ist; Cache-/Deploy-Ergebnis und Commit-SHA dokumentieren.
7. Reconciliation ausführen und betroffene Manifeste, öffentliche
   Spiegelvollständigkeit und Statusprojektion prüfen.

## LFS und Backendhistorie

Arbeitsbaum- und Site-Referenzen werden entfernt oder deaktiviert. Die
LFS-Historie und bereits veröffentlichte Backendobjekte sind ein separater
rechtlicher und betrieblicher Vorgang: nicht automatisch löschen, keine
History-Rewrites und keine Remote-Löschung ohne dokumentierte Freigabe,
Retention-Prüfung und Recovery-Plan. Erforderliche Entfernung wird mit Hosting,
Repository-Owner und rechtlicher Prüfung koordiniert und separat belegt.

## Verifikation

- aktueller Registereintrag ist `takedown` und Validator ist grün;
- payloadfähige Storage-Operationen scheitern mit aktueller Authority;
- nächster Pages-Build enthält keine Artefakt- oder Downloadreferenz;
- RKI-Originallink und Quellmetadaten bleiben sichtbar;
- Reconciliation meldet keine aktive öffentliche Referenz;
- Vorfall, Reviewer, Commits, Workflow-Runs und externe Maßnahmen sind erfasst.

## Rollback einer Fehlklassifikation

Rollback bedeutet keine ungeprüfte Wiederveröffentlichung. Erst eine neue
manuelle rechtliche Prüfung darf einen neuen Zustand mit neuem
`decision_sha256` freigeben. Danach Registervalidator, Storage-Tests,
Reconciliation und Pages-Build erneut ausführen. Bei rein technischem
Buildfehler bleibt `takedown` aktiv; nur der fehlerhafte Site-/Referenzcommit
wird zurückgenommen.
