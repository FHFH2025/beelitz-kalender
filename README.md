# Automatisch aktualisierter Beelitz-Veranstaltungskalender

Dieses kleine GitHub-Pages-Projekt liest den EventON-Kalender unter

https://beelitz.de/veranstaltungen/

mit einem Headless-Browser aus und veröffentlicht daraus eine abonnierbare
`beelitz.ics`. Die Aktualisierung läuft alle sechs Stunden.

## Einrichten

1. Bei GitHub ein neues **öffentliches** Repository anlegen, zum Beispiel
   `beelitz-kalender`.
2. Den Inhalt dieses ZIP-Archivs in die oberste Ebene des Repositorys hochladen.
   Wichtig: Der Ordner `.github` muss mit hochgeladen werden.
3. Unter **Settings > Pages** bei **Build and deployment > Source**
   **GitHub Actions** auswählen.
4. Unter **Actions** den Workflow **Beelitz-Kalender aktualisieren** öffnen und
   einmal über **Run workflow** starten.
5. Nach erfolgreichem Lauf ist der Kalender typischerweise erreichbar unter:

   `https://DEIN-GITHUB-NAME.github.io/beelitz-kalender/beelitz.ics`

## Auf dem iPhone abonnieren

Unter iOS 26:

1. Kalender-App öffnen.
2. Unten **Kalender** antippen.
3. **Hinzufügen > Kalenderabonnement hinzufügen** wählen.
4. Die GitHub-Pages-Adresse der `beelitz.ics` eintragen.
5. Als Account **iCloud** wählen und speichern.

Ein importierter Download ist nur eine Momentaufnahme. Nur das Abonnement über
die HTTPS-Adresse erhält spätere Änderungen.

## Technische Hinweise

- Quelle und Ziel verwenden die Zeitzone `Europe/Berlin`.
- Der Scraper versucht 18 Monate ab dem aktuell angezeigten Monat einzulesen.
- Die ICS-UIDs bleiben stabil, damit geänderte Termine aktualisiert und nicht
  jedes Mal dupliziert werden.
- Wenn die Stadt das HTML oder die EventON-Navigation grundlegend ändert, kann
  der Workflow fehlschlagen. Er überschreibt die bestehende Kalenderdatei dann
  nicht mit einer leeren Datei.
- GitHub Pages und GitHub Actions unterliegen den jeweils geltenden GitHub-
  Bedingungen und Nutzungslimits.
