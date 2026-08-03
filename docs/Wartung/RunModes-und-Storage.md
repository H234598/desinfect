# RunModes, Storage und Backendmigration

## Modusvertrag

P04 trennt die drei Ausführungsmodi als Sicherheitsgrenzen:

| Modus | Erlaubt | Verboten |
|---|---|---|
| `plan` | lesen, validieren, JSON nach stdout | Datei-, Repository-, Git-, Status-, LFS-, Release- und Object-Writes |
| `materialize` | ausschließlich verifizierte Dateien unter explizitem `temp_root` | Repository, Index, Commit, `status.json` und Remote-Backends |
| `apply` | explizit im EffectLedger registrierte, deny-first validierte Effekte | nicht deklarierte oder backendfremde Effekte |

`SideEffectGuard` erfasst vor und nach dem Block:

- `HEAD`;
- Worktree einschließlich unversionierter Dateien;
- Git-Index-Diff;
- SHA-256 geschützter Statusdateien;
- Dateien unter `temp_root`.

Ein nicht zum Modus und Ledger passender Unterschied erzeugt `ModeViolation`.

## Storage Protocol

Alle Backends implementieren denselben Vertrag:

```python
exists(intent)
materialize(intent, temp_root, ledger)
export(reference, temp_root, ledger)
apply(prepared, ledger)
verify(reference)
list_references()
```

Die stabilen Datentypen sind:

- `StorageIntent`: Quellpfad, logischer Schlüssel, SHA-256, Größe, Sichtbarkeit und Rechtezustand;
- `PreparedObject`: verifiziertes Objekt unter `temp_root`;
- `StorageReference`: backendneutrale Referenz gemäß `schemas/storage-reference.schema.json`.

Unbekannte Backends und unbekannte TOML-Schlüssel brechen fail-closed ab. `config/storage.toml` legt Git LFS als initiales Backend fest.

## Rechte-Gate

Jede Payload-Operation benötigt eine aktuelle Entscheidung für das exakte Paar
aus `source_id` und `source_sha256`. Adapter laden die gepinnte Authority vor
dem nächsten Byteeffekt erneut; Legacy-Provenienz, fehlende Entscheidungen und
Revocations blockieren fail-closed. RKI-Rohmetadaten bleiben Evidenz und werden
nicht als Freigabe verwendet.

Policy, Zustandsmatrix und Reviewverfahren beschreibt
[Rechte und Lizenzen](../Rechte-und-Lizenzen.md). Bei einer Rücknahme gilt das
[Rights-Takedown-Runbook](../../runbooks/RIGHTS-TAKEDOWN.md), einschließlich
Pages-Rebuild und getrennter rechtlicher Behandlung der LFS-Historie.

## Git LFS

`.gitattributes` verfolgt nur:

```text
rki/Bulletins/**/*.pdf
rki/Bulletins/**/Markdown/**/*.md
rki/Bulletins/**/*.zip
```

Die Validierung prüft:

1. exakte Trackingregeln;
2. Pointerversion `https://git-lfs.github.com/spec/v1`;
3. kleingeschriebene SHA-256-OID;
4. nichtnegative Größe;
5. lokales Objekt unter `.git/lfs/objects/<aa>/<bb>/<oid>`;
6. tatsächliche Objektgröße und SHA-256;
7. Objekt-/Bytegrenzen je Lauf;
8. Warn- und Blockschwellen des Gesamtbestands.

Der LFS-Adapter schreibt Working-Tree-Dateien, führt aber weder `git add`, Commit, Push noch Netzwerktransfer aus. Diese Effekte folgen erst hinter P05/P12-Gates.

## Release- und Object-Ports

Die Remoteadapter importieren keine GitHub- oder Cloud-SDKs. Sie akzeptieren injizierte Ports mit:

```python
head(key)
put(key, source_path, sha256, size)
get(key, target_path)
list(prefix)
```

Dadurch sind Plan, Materialisierung, Publikation, Konflikte und Wiederaufnahme vollständig offline testbar. Ein fehlender Client ist ein Konfigurationsfehler.

## Migration

Die Migration ist nicht destruktiv und in drei Stufen getrennt:

1. `plan_migration()` vergleicht Referenzen nach `artifact_id`, SHA-256 und Größe und klassifiziert `copy|unchanged|conflict`.
2. `materialize_migration()` exportiert ausschließlich `copy`-Einträge unter `temp_root`.
3. `apply_migration()` veröffentlicht und verifiziert die vorbereiteten Objekte.

Konflikte blockieren vor dem ersten Export. Identische Ziele sind `unchanged`. Ein wiederholter Lauf wird zum No-op. Quellobjekte werden nie automatisch gelöscht.

## CLI

```bash
python3 -m scripts.rki_pipeline.storage_cli verify --repository-root .
python3 -m scripts.rki_pipeline.storage_cli plan \
  --source-repo /quelle --target-repo /ziel
python3 -m scripts.rki_pipeline.storage_cli materialize \
  --plan migration.json --source-repo /quelle \
  --temp-root /tmp/desinfect-migration --output prepared.json
python3 -m scripts.rki_pipeline.storage_cli apply \
  --plan migration.json --prepared prepared.json \
  --target-repo /ziel --confirm-apply
```

Die P04-CLI führt bewusst nur lokale LFS-Migrationsdrills aus. Produktive Release-/Object-Clients werden erst später mit expliziten Credentials und Betriebsgrenzen injiziert.

## Vollständiges Gate

```bash
python3 scripts/validate_p04_storage.py
python3 scripts/validate_rights_register.py
python3 -m pytest -q \
  tests/test_run_modes.py \
  tests/test_storage_contract.py \
  tests/test_storage_lfs.py \
  tests/test_storage_remote.py \
  tests/test_storage_migration.py
```

Die Architekturentscheidungen **ADR-003=A** und **ADR-014=B** bleiben unverändert. Insbesondere wird ein Rechtezustand nicht durch die Wahl eines Storage-Backends ersetzt und Analysevollständigkeit bleibt von öffentlicher Spiegelvollständigkeit getrennt.
