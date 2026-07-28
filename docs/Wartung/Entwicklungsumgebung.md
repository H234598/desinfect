# Entwicklungsumgebung

P01 legt das reproduzierbare Paketfundament fest:

- Python `>=3.12` mit exakt gepinnten direkten und aufgelösten transitiven
  Lockdateien;
- Buildbackend `setuptools==83.0.0`;
- Node.js 24 und npm 11;
- `npm ci` als einziger CI-Installationsweg;
- `pytest`, `unittest`, `compileall`, Lock- und Fixturevalidatoren als
  blockierende Prüfungen.

Die `.in`-Dateien halten die direkten Absichten fest. Die gleichnamigen `.txt`-
Dateien werden aus einem frischen Python-3.12-Resolverbericht erzeugt und müssen
kanonisch sortiert sein. `scripts/validate_dependency_locks.py` verhindert Drift
zwischen `pyproject.toml`, Intentdateien und Locks.

Die Architekturentscheidungen **ADR-003=A** und **ADR-014=B** werden durch diese
Paketarbeiten nicht verändert.
