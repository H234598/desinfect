---
title: "GitHub Actions und Dispatcher"
role: "maintenance"
---

# GitHub Actions und Dispatcher

Workflows unter `.github/workflows/` validieren und koordinieren Pipeline-Läufe. Dispatcher planen
nur erlaubte Arbeit; automatische Schreibziele bleiben auf die in
`config/automatic-write-paths.toml` freigegebenen Pfade begrenzt.
