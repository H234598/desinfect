
# Automatische Schreibpfade

Die Policy `config/automatic-write-paths.toml` arbeitet deny-first. Ein Pfad muss in der Allowlist liegen und darf keine Denyregel treffen. Nicht aufgeführte Pfade sind ebenfalls verboten.

Automatisch erlaubt sind ausschließlich die spätere RKI-Datenebene, generierte Contentdaten, `research/corpus-readiness.json` und `status.json`. Infrastruktur, Schemas, Code, Tests, Workflows, LFS-Regeln, Rechte- und Taxonomieregister bleiben geschützt.

Symlinks, Gitlinks/Submodule, doppelte Operationen sowie Unicode-/Casefold-Kollisionen blockieren vor dem Commit. `.github/CODEOWNERS` weist `@H234598` als initialen Owner aus und nennt die kritischen Pfade zusätzlich ausdrücklich.

## GitHub App `Wachhund`

Automatische Repositoryschreibvorgänge benötigen eine GitHub App namens `Wachhund` mit der Repositoryberechtigung `Contents: Read and write`. Die App wird ausschließlich auf `H234598/desinfect` installiert. Im Repository müssen eingerichtet sein:

- Variable `WACHHUND_APP_CLIENT_ID`: Client-ID der App;
- Actions-Secret `WACHHUND_APP_PRIVATE_KEY`: privater Schlüssel der App.

Dispatcher, Backfill und Pipeline behalten auf Workflowebene `Contents: read`. Erst nach erfolgreicher Transaktion und globaler Validierung fordert die Pipeline für `H234598/desinfect` ein Installationstoken mit `Contents: write` an. Fehlende App-Konfiguration blockiert den Schreibpfad; ein write-fähiger `GITHUB_TOKEN` dient ausdrücklich nicht als Fallback.

## Transaktionaler Writer

Alle fälligen Aufgaben eines Laufs teilen eine Transaktion, eine globale Validierung und höchstens einen Commit. Ein leerer Dispatchplan startet keine Pipeline; eine Transaktion oder ein Staging ohne tatsächliche Änderung endet als No-op ohne Commit und Push.

Regulärer Lauf und Backfill verwenden die gemeinsame, nicht abbrechende Concurrency-Gruppe `desinfect-repository-writer` mit `cancel-in-progress: false`. Vor dem Push wird `origin/main` erneut mit dem geplanten Basis-Commit verglichen. Bei Drift wird nicht gepusht und niemals erzwungen; der vollständige Dispatcher- oder Backfill-Lauf muss gegen den aktuellen `main`-Stand wiederholt werden.
