
# Automatische Schreibpfade

Die Policy `config/automatic-write-paths.toml` arbeitet deny-first. Ein Pfad muss in der Allowlist liegen und darf keine Denyregel treffen. Nicht aufgeführte Pfade sind ebenfalls verboten.

Automatisch erlaubt sind ausschließlich die spätere RKI-Datenebene, generierte Contentdaten, `research/corpus-readiness.json` und `status.json`. Infrastruktur, Schemas, Code, Tests, Workflows, LFS-Regeln, Rechte- und Taxonomieregister bleiben geschützt.

Symlinks, Gitlinks/Submodule, doppelte Operationen sowie Unicode-/Casefold-Kollisionen blockieren vor dem Commit. `.github/CODEOWNERS` weist `@H234598` als initialen Owner aus und nennt die kritischen Pfade zusätzlich ausdrücklich.

## CI-Vertrag „Variante B“

Die aktuelle Baseline bleibt vollständig read-only. Der blockierende Validator
`scripts/validate_ci_mutation_safety.py` bereitet lediglich spätere, ausdrücklich
freigegebene Schreibpfade vor. Jeder einzelne Workflow-Schritt mit `git commit`
oder `git push` muss dann:

- bei leerem staged Diff erfolgreich ohne Commit und Push enden;
- `git status --short` und einen staged `--name-status`- oder `--stat`-Diff
  protokollieren;
- fragmentierte Payloads mit berechneter und erwarteter SHA-256, Größe und
  SHA-256 jedes Fragments sowie einer bestmöglichen Archivliste diagnostizieren;
- Ist- und Soll-Prüfsumme tatsächlich vergleichen und bei einer Abweichung mit
  einem von null verschiedenen Exitcode enden;
- Sicherheits-Audits blockierend belassen und darf sie weder durch ein- oder
  mehrzeilige Shell-Bypässe noch per `continue-on-error` entkräften.

Die Negativtests schließen ausdrücklich aus, dass ein abgesicherter Writer einen
zweiten ungesicherten Writer im selben Workflow maskiert. Diese Regeln ergänzen
die deny-first Pfadpolicy. Sie gewähren weder zusätzliche Tokenrechte noch
erlauben sie einen bislang verbotenen Schreibpfad.
