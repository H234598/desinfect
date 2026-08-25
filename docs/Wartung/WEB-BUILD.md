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

## 7. Nicht Teil von P10.2

P10.3-Navigation, Wartungsicon und Tabellen fehlen absichtlich. Dieses Runbook
konfiguriert oder deployt keine GitHub Pages und trifft keine Custom-Domain-
Entscheidung.
