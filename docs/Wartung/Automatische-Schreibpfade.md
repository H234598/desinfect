
# Automatische Schreibpfade

Die Policy `config/automatic-write-paths.toml` arbeitet deny-first. Ein Pfad muss in der Allowlist liegen und darf keine Denyregel treffen. Nicht aufgeführte Pfade sind ebenfalls verboten.

Automatisch erlaubt sind ausschließlich die spätere RKI-Datenebene, generierte Contentdaten, `research/corpus-readiness.json` und `status.json`. Infrastruktur, Schemas, Code, Tests, Workflows, LFS-Regeln, Rechte- und Taxonomieregister bleiben geschützt.

Symlinks, Gitlinks/Submodule, doppelte Operationen sowie Unicode-/Casefold-Kollisionen blockieren vor dem Commit. `.github/CODEOWNERS` weist `@H234598` als initialen Owner aus und nennt die kritischen Pfade zusätzlich ausdrücklich.
