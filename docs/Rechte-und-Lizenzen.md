# Rechte und Lizenzen

## Autoritative Entscheidung

Volltexte, PDFs und ZIP-Dateien dürfen nur mit einer aktuellen, manuell
reviewten Entscheidung aus `research/rights-register.yml` materialisiert,
exportiert oder veröffentlicht werden. Autorität besitzt ausschließlich der
exakte Revisionsschlüssel aus `source_id`, kanonischer `bitstream_url`,
kanonischer `bitstream_id` (`version_or_bitstream`) und `source_sha256`. Eine
Freigabe nur für Handle-ID, Serie, Host, Jahr, URL-Muster oder Quellhash ist
unzulässig. Änderung eines der vier Felder setzt die Freigabe zurück.

RKI-Rohmetadaten wie Lizenz-URL, Rechtehinweis und Open-Access-Angabe bleiben
prüfbare Evidenz, sind aber keine Autorität für eine Veröffentlichung. Fehlt
der exakte Registereintrag, gilt `unknown` mit Modus `origin_link` und ohne
Payloadaktion. Grundlage oder Gesetzestext allein erteilt keine Freigabe.

## Modi und Einzelaktionen

| Publikationsmodus | Zulässige Wirkung |
|---|---|
| `remove_all` | keine öffentliche Präsenz; Takedown |
| `origin_link` | nur kanonischer RKI-Originallink |
| `source_only` | Quellenmetadaten ohne Payload |
| `materialized` | nur ausdrücklich freigegebene Einzelaktionen |

Geschlossene Einzelaktionen sind `fetch`, `cache`, `hash`, `ocr`,
`extract_text`, `thumbnail`, `index_text` und `publish`. `materialized`
erfordert Zustand `approved`; jede Wirkung benötigt ihre eigene Aktion.
`publish` erfordert zusätzlich `components_state=cleared` und vollständige
Attribution: Urheber, Attributionsparteien, Copyright-, Lizenz- und
Disclaimerhinweis, kanonische Lizenz- und Origin-URL sowie frühere und aktuelle
Änderungshinweise. Die Matrix ist in `config/rights-policy.toml`
festgeschrieben. Backend, Dateityp oder öffentliche Erreichbarkeit erweitern
sie nicht.

Geschlossene Rechtezustände bleiben `approved`, `internal_only`,
`metadata_only`, `unknown` und `takedown`. Geschlossene Sichtbarkeiten bleiben
`public`, `repository_authorized`, `internal` und `restricted`; Sichtbarkeit
erteilt keine zusätzliche Einzelaktion.

## Review und Entscheidungsprovenienz

Jede Freigabe oder Einschränkung benötigt eine manuelle rechtliche Prüfung.
Reviewer dokumentieren Grundlage, `reviewed_by`, UTC-Zeitpunkt und die exakt
geprüften Quellbytes. Abweichende Dokumentbedingungen und Rechte Dritter sind
gesondert zu berücksichtigen.

`decision_sha256` bindet Policyversion, Vierfachschlüssel, Zustand, Modus,
sortierte Aktionen, Komponentenstatus, vollständige Attribution, Grundlage
und Reviewdaten deterministisch aneinander. Der Hash macht Drift sichtbar; er
ist keine Signatur, keine Identitätsbestätigung und kein Ersatz für das Review.

## Governance und Laufzeitgrenze

Der kanonische Registerpfad ist in der `RightsAuthority` gepinnt. Jeder
payloadfähige Schritt lädt ihn vor dem nächsten Byteeffekt erneut und prüft
die konkrete Aktion. Reine Lookups wie `exists`, Skip-Prüfungen und
Manifestvalidierung erzeugen keine Schein-Autorisierung.
Dadurch blockiert eine zwischen Materialisierung und Apply eingetragene
Revocation sofort; ein alter Zustand oder `decision_sha256` wird abgewiesen.

`CODEOWNERS` weist das Register einem menschlichen Owner zu. Die Policy für
automatische Schreibpfade verweigert Änderungen an
`research/rights-register.yml`; Änderungen erfolgen nur als reviewter PR.
Beide blockierenden Workflows führen aus:

```bash
python3 scripts/validate_rights_register.py
```

Methodische Details stehen unter [Methodik/Rechte](Methodik/Rechte.md). Bei
einer Rücknahme gilt das [Rights-Takedown-Runbook](../runbooks/RIGHTS-TAKEDOWN.md).
