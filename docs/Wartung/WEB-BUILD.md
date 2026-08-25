# Reproduzierbarer Webbuild

Dieses Runbook baut die MkDocs-Ausgabe strikt und reproduzierbar. Befehle laufen
im Repository-Stamm. `build/docs/` und `site/` sind zwei getrennt atomar
publizierte, durch `.desinfect-generated` markierte Ziele. Der Gesamtlauf ist
keine gemeinsame Transaktion über beide Ziele.

## 1. Voraussetzungen

Benötigt werden Python 3.12 sowie exakt `requirements-test.txt` und
`requirements-docs.txt`. Installation ist nur in einer repo-lokalen isolierten
Python-Umgebung erlaubt. Nie Home- oder globale Python-Umgebung verändern. Keine
Abhängigkeit ergänzen oder ungepinnt installieren.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
test "$VIRTUAL_ENV" = "$PWD/.venv"
python3 --version
python3 -m pip install --no-cache-dir \
  -r requirements-test.txt -r requirements-docs.txt
python3 scripts/validate_dependency_locks.py
python3 -c 'import material, mkdocs, pytest, yaml; print(mkdocs.__version__)'
python3 -m pip check
```

`python3 -m pip check` ist Pflicht. Ein Konflikt wird nicht ignoriert; Build erst
nach Wiederherstellung der gelockten Umgebung starten.

## 2. Normaler Strict-Build

Epoche stammt aus aktuellem Commit und wird unverändert an MkDocs weitergereicht.

```bash
SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD) \
  python3 scripts/web/build_site.py --check --strict \
  --site-url https://h234598.github.io/desinfect/
```

Normalbuild publiziert zuerst `build/docs/`, danach `site/`. Jede Phase ersetzt
nur ihr eigenes Ziel atomar. Scheitert die Docs-Phase, bleiben alte Docs und alte
Site aktiv. Scheitert nach erfolgreicher Docs-Publikation der MkDocs-Build, die
Konfigurations- oder Site-Symlinkprüfung oder die spätere Quellhashprüfung,
bleiben neue vollständige Docs neben alter vollständiger Site bestehen. Kein
veröffentlichter Zielbaum ist unvollständig.

## 3. Sichere Vorschau einer ausgewählten Seite

```bash
SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD) \
  python3 scripts/web/build_site.py --check --dry-run --strict \
  --only index.md \
  --site-url https://h234598.github.io/desinfect/
```

Dry-run verwendet wegwerfbare Vorschauen. Weder `build/docs/` noch `site/` aus
diesem Lauf ist deploybare Ausgabe. Vorschau niemals an Pages übergeben.

## 4. Ausgabekontrollen

```bash
test -f site/index.html
test -f site/404.html
find site -type l -print -quit
SOURCE_DATE_EPOCH=1700000000 \
  python3 -m pytest -q tests/web/test_build_docs.py \
  -k 'reproducible or invalid_epoch'
```

`find` muss leer bleiben. Tests hashen Quellen vor und nach zwei unabhängigen
Vollbuilds und vergleichen sortierte SHA-256-Abbildungen aller regulären
`site/`-Dateien.

## 5. Fehlerpfad

Docs-Phasenfehler veröffentlichen nichts und behalten beide alten vollständigen
Ziele. Nach erfolgreicher Docs-Publikation können Fehler im MkDocs-Build, in der
Konfigurations- oder Site-Symlinkprüfung sowie bei der späteren
Quellhashprüfung neue vollständige `build/docs/` neben alter vollständiger
`site/` hinterlassen. Beide Ziele sind getrennte atomare Transaktionen; kein
unvollständiger Baum wird veröffentlicht. Quellpfade nicht manuell löschen.

Stale Backups zuerst inspizieren:

```bash
find . -maxdepth 3 -type d \
  \( -name '.site.backup' -o -name '.docs.backup' \) -print
test -f site/.desinfect-generated
test -f build/docs/.desinfect-generated
```

Nur nach Abgleich von Fehlermeldung, Zielidentität und Sentinel erneut mit
`--force` bauen:

```bash
SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD) \
  python3 scripts/web/build_site.py --check --strict --force \
  --site-url https://h234598.github.io/desinfect/
```

## 6. Recovery

Pages-Deployment stoppen und alten vollständigen `site/`-Baum behalten. Blieb
wegen fehlgeschlagener Bereinigung ein validiertes Backup zurück, zuerst
Staging-Fehlerkontext und Sentinel prüfen:

```bash
test ! -d .site.backup || test -f .site.backup/.desinfect-generated
test ! -d build/.docs.backup || test -f build/.docs.backup/.desinfect-generated
```

Danach ausschließlich Sentinel-geprüfte Stagingtransaktion mit `--force` wie im
Fehlerpfad verwenden. Kein `rm -rf`, kein manuelles Umbenennen, kein Löschen von
`content/`, `config/`, `mkdocs.yml` oder `web/`. Pages Build/Deploy aus `site/`
gehört zu P11.2. Festlegung der GitHub-Pages-Site-URL gehört zu P14. Eine Custom
Domain benötigt eine separate Adminentscheidung.

## 7. P10.3-Navigation und Tabellendaten

P10.3 ist Teil des Strict-Builds. Der Vollbuild erzeugt `site/index.html`,
`site/Tabelle/index.html`, `site/WARTUNG/index.html` und die neun verlinkten Seiten unter
`site/Wartung/`. Die Lesernavigation enthält keinen Projekt-/Wartungspunkt. Werkzeuglink und
Wartungshub bleiben trotzdem lokale, strikt geprüfte Linkziele.

Tabellen verwenden diese kanonischen Quellen:

- `status.json` für Gate und Zustandsmaschine;
- optional `research/corpus-readiness.json` und `research/taxonomy.yml` gemäß Gatezustand;
- optional `content/generated-data/corpus-table.json` für Korpuszeilen;
- `content/generated-data/anleitungen.json` nur im vollständig freigegebenen Zustand.

Die Eingaben werden begrenzt gelesen, schema-validiert und vor jeder Veröffentlichung
gegeneinander geprüft. Öffentliche Projektionen liegen danach ausschließlich unter
`build/docs/assets/data/`; rohe `content/generated-data/`-Pfade werden nicht kopiert.
Vollständige Tabellenzeilen stehen zugleich serverseitig in `build/docs/Tabelle.md` und
`site/Tabelle/index.html`. Browser-JavaScript ist keine Datenquelle.

## 8. Zulässiger Vor-Gate- und Fehlerzustand

Ein noch nicht erfülltes Taxonomie-Gate ist kein Buildfehler. Ohne Korpusprojektion zeigt die
Seite den Text „Noch keine validierten Dokumentmanifeste“, Caption und alle 14 Korpusspalten.
Bei Gate `false`, Readiness-/Taxonomie-Review oder Proposal bleibt die Anleitungstabelle
ausgeblendet. Erst `approved` mit vollständiger Readiness, freigegebener Taxonomie und
passender Anleitungsprojektion veröffentlicht sie.

Widersprüchliche Gatewerte, verfrühte Taxonomie-/Anleitungsdateien, fehlende Pflichtdateien
im freigegebenen Zustand, Schema-/Versionsdrift, unvollständige Evidenz, unbekannte
Kategorien oder Grenzwertverletzungen brechen fail-closed ab. Nicht durch Umbenennen,
Löschen oder Ändern kanonischer Quellen „reparieren“. Zuerst Fehlermeldung und die vier
Gate-Eingaben prüfen:

```bash
python3 scripts/web/build_site.py --check --dry-run --strict \
  --only Tabelle.md \
  --site-url https://h234598.github.io/desinfect/
python3 -m pytest -q tests/web/test_build_tables.py tests/web/test_build_docs.py
git status --short
```

Der Teilpreview baut nur die ausgewählte Seite in Wegwerfverzeichnissen und publiziert
nichts. `git status --short` muss danach für kanonische Quellen leer bleiben.

## 9. Atomizität, Rollback und Diagnose

Docs und Site bleiben zwei getrennte atomare Veröffentlichungen. `build/docs/` wird zuerst
vollständig gestaged, validiert und ersetzt. Danach baut MkDocs aus einem festgehaltenen
Docs-Baum eine vollständige Site und ersetzt `site/` atomar. Ein später Sitefehler rollt
nicht die bereits vollständigen neuen Docs zurück; die vorherige vollständige Site bleibt
aktiv. Ein Fehler innerhalb einer Stagingtransaktion stellt das jeweilige sentinel-geprüfte
alte Ziel wieder her.

Diagnose bleibt read-only gegenüber `content/`, `config/`, `web/`, `mkdocs.yml`,
`status.json` und `research/`. Erlaubt sind Logs, Hashvergleiche, `git diff`,
sentinel-geprüfte Zielinspektion und der Dry-run. Keine Source-Mutation, kein manuelles
Verschieben veröffentlichter Bäume und kein Löschen eines Backups. `--force` erst nach
Identitäts- und Sentinelprüfung wie in Abschnitt 5 verwenden.

## 10. Abgrenzung

Der Build konfiguriert oder deployt keine GitHub Pages und trifft keine Custom-Domain-
Entscheidung. Echte Browser-, Axe- und 390-Pixel-Abnahme gehört zu P11.4. P09.4-Live-
Deployment und dessen separater 301-Blocker werden durch P10.3 nicht verändert.
