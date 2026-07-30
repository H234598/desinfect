# P04 RunMode- und Storage-Design

## Status und Freigabegrundlage

Dieses Design konkretisiert die bereits freigegebene Phase P04 des Implementierungsplans V3. Es verändert weder die Phasenreihenfolge noch die gesperrten Entscheidungen **ADR-003=A** und **ADR-014=B**.

## Ziel

P04 führt vier zusammenhängende Sicherheits- und Speicherbausteine ein:

1. strikt getrennte Modi `plan`, `materialize` und `apply`;
2. einen Seiteneffektwächter, der verbotene Änderungen nachweisbar erkennt;
3. ein echtes, injizierbares Storage Protocol mit `lfs|release|object`;
4. Git-LFS-Verifikation, Budgetkontrolle und eine idempotente Backendmigration.

## Architektur

### RunMode und Effects

`scripts/rki_pipeline/run_modes.py` definiert:

- `RunMode(StrEnum)`: `PLAN`, `MATERIALIZE`, `APPLY`;
- `EffectKind(StrEnum)`: Repositorydatei, Git-Index, Git-Commit, LFS, Release, Objektspeicher und Status;
- `EffectEvent`: ein unveränderliches, serialisierbares Ereignis;
- `EffectLedger`: protokolliert beabsichtigte und tatsächlich ausgeführte Effekte;
- `SideEffectGuard`: vergleicht Git-/Datei-Snapshots und das Ledger mit dem Modusvertrag.

Der Modusvertrag lautet:

- `plan`: keinerlei persistente oder temporäre Dateischreibvorgänge durch Pipelinekomponenten und keinerlei Remoteeffekte;
- `materialize`: ausschließlich Schreibvorgänge unter einem expliziten `temp_root`; keine Repository-, Index-, Commit-, LFS-, Release-, Objekt- oder Statuseffekte;
- `apply`: persistente Effekte nur nach expliziter Ledgerregistrierung und nach deny-first Pfadvalidierung.

Der Wächter erfasst `HEAD`, Worktree-/Indexstatus, den SHA-256 geschützter Statusdateien und Remote-Spies. Abweichungen brechen fail-closed ab.

### Storage Protocol

`scripts/rki_pipeline/storage/base.py` definiert stabile Datentypen:

- `StorageBackend(StrEnum)`: `LFS`, `RELEASE`, `OBJECT`;
- `StorageIntent`: Quellpfad, logischer Schlüssel, SHA-256, Größe, Sichtbarkeit und Rechtezustand;
- `PreparedObject`: materialisierte Datei unter `temp_root` samt unveränderlicher Metadaten;
- `StorageReference`: backendneutrale Referenz gemäß `schemas/storage-reference.schema.json`;
- `StorageAdapter(Protocol)`: `exists`, `materialize`, `apply`, `verify` und `list_references`.

`storage/factory.py` lädt `config/storage.toml` streng typisiert. Unbekannte Backends, unbekannte Schlüssel und inkonsistente Limits brechen ab.

### Adapter

- `LfsStorageAdapter`: arbeitet ausschließlich in einem injizierten Repositoryroot, erzeugt kanonische Zielpfade, prüft `.gitattributes`, Git-LFS-Pointer und lokale LFS-Objekte. Er führt selbst keinen Commit oder Push aus.
- `ReleaseStorageAdapter`: verwendet ausschließlich einen injizierten `ReleaseClient`-Port. Tests nutzen einen In-Memory-Client; es gibt in P04 keinen GitHub-Netzwerkaufruf.
- `ObjectStorageAdapter`: verwendet ausschließlich einen injizierten `ObjectClient`-Port mit unveränderlicher Namespacegrenze. Tests nutzen einen In-Memory-Client.

Alle Adapter erzeugen dasselbe `StorageReference`-Format. Website, Katalog und Downloadcode müssen deshalb keine Backend-spezifischen URLs erraten.

### Git LFS und Budget

`.gitattributes` verfolgt ausschließlich die kanonischen Archivpfade für PDFs, vollständige Quell-Markdowns und ZIP-Dateien. `storage/lfs.py` prüft:

- exakte Trackingregeln;
- Pointerversion, OID und Größe;
- Existenz und SHA-256 des lokalen `.git/lfs/objects/<aa>/<bb>/<oid>`-Objekts;
- Objektanzahl und Bytegrenzen je Lauf sowie Warn-/Blockschwellen des Gesamtbestands.

Ein LFS-Pointer ohne Objekt, ein Objekt mit falschem Hash oder ein Budgetüberlauf blockiert `apply`.

### Backendmigration

`storage/migrate.py` trennt ebenfalls:

- `plan`: liest Referenzen und erzeugt einen deterministischen Migrationsplan;
- `materialize`: kopiert/verifiziert ausschließlich unter `temp_root`;
- `apply`: veröffentlicht über den Zieladapter und verifiziert jede Referenz erneut.

Bereits identische Zielobjekte werden als `unchanged` geführt. Ein zweiter Lauf mit demselben Input ist ein No-op. Quellobjekte werden niemals automatisch gelöscht.

## Fehler- und Sicherheitsmodell

- unbekannter Modus oder Backend: fail-closed;
- Traversal, Symlink, Gitlink oder portable Pfadkollision: blockiert durch P01/P02-Primitiven;
- Hash-/Größenabweichung: blockiert vor Veröffentlichung;
- Remoteadapter ohne injizierten Client: Konfigurationsfehler;
- teilweise Migration: strukturierter Fehler, keine Quelllöschung, Wiederaufnahme anhand verifizierter Referenzen;
- `plan` und `materialize` dürfen `status.json`, Repository, Index oder Remote-Spies nicht verändern.

## Tests

- `tests/test_run_modes.py`: Modusmatrix, Git-/Statussnapshot, TempRoot-Grenze, Ledger und negative Effekte;
- `tests/test_storage_contract.py`: strikte Konfiguration, Protocol-Verhalten, backendneutrale Referenzen;
- `tests/test_storage_lfs.py`: Trackingregeln, Pointer, Objektintegrität und Budgets;
- `tests/test_storage_remote.py`: Release-/Object-Spies ohne Netzwerk;
- `tests/test_storage_migration.py`: Plan/Materialize/Apply, Fehlerfortsetzung und Idempotenz;
- `scripts/validate_p04_storage.py`: vollständiges P04-Gate.

## Abgrenzung

P04 erstellt keine Bot-Commits, keinen Dispatcher, keinen Scheduler, keinen GitHub-Release und kein echtes Objektstorage. Diese Seiteneffekte folgen erst in P05/P12/P17 hinter den hier definierten Ports und Gates.
