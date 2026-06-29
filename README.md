# StreetView Street Manager

> Vollständiger Google Street View Straßenexport für Linux  
> Dichte Abtastung · pano_id-Deduplizierung · lokaler Quota-Schutz · API-Key-Test · selbst-installierend · moderne Tkinter-GUI

---

## Was dieses Tool macht

Dieses Tool lädt **alle** Google Street View Panoramen einer ganzen Straße herunter – nicht nur einen zufälligen Punkt, sondern jeden einzelnen Panorama-Datenpunkt, den Google für diese Straße aufgenommen hat.

**Ablauf intern:**

1. Straße über OpenStreetMap/Nominatim geokodieren → Liniengeometrie holen
2. Dichte Probepunkte entlang der Straße samplen (Standard: alle 5 m)
3. Für jeden Punkt `streetview-dl query` aufrufen → echte `pano_id` sammeln
4. Alle `pano_id`-Werte deduplizieren → jede Aufnahme nur einmal herunterladen
5. Tile-Requests lokal mitschätzen und automatisch bei deinem Quota-Limit stoppen

---

## Systemvoraussetzungen

```bash
# Ubuntu / Debian
sudo apt install python3 python3-pip python3-tk

# Arch Linux
sudo pacman -S python python-pip tk

# Fedora / RHEL
sudo dnf install python3 python3-pip python3-tkinter
```

> **Das war's.** Alles andere installiert das Skript automatisch.

---

## Installation & Start

```bash
# 1. Repository klonen
git clone https://github.com/basecore/streetview-street-manager.git
cd streetview-street-manager

# 2. Direkt starten – kein pip, kein venv nötig
python3 streetview_manager.py
```

Beim ersten Start installiert das Skript automatisch:
- `requests`
- `Pillow`
- `streetview-dl`

---

## Google Maps API-Key einrichten

Das Tool benötigt einen Google Maps API-Key mit aktivierter **Map Tiles API**.

### Schritt für Schritt

1. **Google Cloud Console öffnen:**  
   https://console.cloud.google.com

2. **Projekt erstellen** (oder bestehendes wählen)  
   → Oben in der Leiste → "Projekt auswählen" → "Neues Projekt"

3. **Map Tiles API aktivieren:**  
   → APIs & Dienste → Bibliothek → Suche: `Map Tiles API` → Aktivieren

4. **API-Schlüssel erstellen:**  
   → APIs & Dienste → Anmeldedaten → `+ Anmeldedaten erstellen` → `API-Schlüssel`

5. **Abrechnung (Billing) aktivieren:**  
   → Navigation → Abrechnung → Konto verknüpfen  
   ⚠️ Kreditkarte wird benötigt, aber **unter 100.000 Tiles/Monat ist alles kostenlos**.

6. **Optional – Key einschränken:**  
   → Anmeldedaten → Key auswählen → Einschränkung: `Map Tiles API`

7. **Key im Tool eintragen:**  
   → Tab `API-Key` → Key einfügen → `Speichern` → `Key testen`

### Alternativ: Umgebungsvariable setzen

```bash
export GOOGLE_MAPS_API_KEY="AIza..."
python3 streetview_manager.py
```

Für dauerhafte Speicherung in `~/.bashrc`:

```bash
echo 'export GOOGLE_MAPS_API_KEY="AIza..."' >> ~/.bashrc
source ~/.bashrc
```

---

## Tabs in der GUI

| Tab | Inhalt |
|---|---|
| **Projekt** | Straße, Ort, Ausgabeordner |
| **Ausführen** | Analyse starten · Download starten · Stopp · Live-Tabelle gefundener Panoramen |
| **Optionen** | Sampling-Abstand, Suchradius, Qualität, Pause, Historische Bilder |
| **Quota** | Monatslimit, Stop-Prozent, manueller Startwert, Reset |
| **API-Key** | Key eingeben, anzeigen/ausblenden, testen, Anleitung |
| **Debug-Log** | Vollständiges Verbose-Logging aller Aktionen, CMD-Strings, HTTP-Codes |

---

## Quota und Billing

Google bietet keine direkte Live-Abfrage des tatsächlichen Tile-Verbrauchs über die Map Tiles API an. Das Tool schätzt den Verbrauch daher **lokal und konservativ**:

| Qualität | Tiles pro Panorama |
|---|---|
| `low` | 32 |
| `medium` | 128 |
| `high` | 512 |
| Query-Aufruf | ~2 |

**Stopp-Mechanismus:**
- Standardmäßig Stopp bei **80 %** von **100.000 Tiles/Monat**
- Der Zähler wird in `state/quota_YYYY-MM.json` gespeichert
- Wenn du den echten Stand aus der Google Cloud Console kennst → Tab `Quota` → `Bereits verbraucht` setzen

---

## Optionen im Detail

| Option | Standard | Erklärung |
|---|---|---|
| Sampling-Abstand | 5 m | Abstand zwischen Messpunkten entlang der Straße |
| Suchradius | 8 m | Radius um jeden Punkt für die Panorama-Suche |
| Max. Treffer/Punkt | 5 | Max. Panoramen pro Query-Aufruf |
| Qualität | medium | Bildqualität beim Download |
| Pause | 0,2 s | Wartezeit zwischen Query-Aufrufen |
| Historisch | aus | Auch ältere Aufnahmen laden (2–3× mehr Quota) |

---

## Dateistruktur nach dem Lauf

```
streetview-street-manager/
├── streetview_manager.py       ← Das Hauptskript
├── README.md
├── downloads/                  ← Heruntergeladene Bilder
│   ├── streetview_urls_berger_strasse_frankfurt.txt
│   └── streetview_XXXXX.jpg
├── state/
│   └── quota_2026-06.json      ← Monatlicher Quota-Zähler
└── logs/
    └── session_20260629_...log ← Vollständiges Debug-Log
```

---

## Häufige Fehler

### `streetview-dl: command not found`
→ Wird automatisch installiert. Falls nicht: `pip3 install streetview-dl`

### `Straße nicht gefunden`
→ Namen prüfen. Beispiel: `Berger Straße` + `Frankfurt am Main` (nicht nur Straßenname)

### `Map Tiles API nicht aktiviert`
→ Google Cloud Console → APIs & Dienste → Map Tiles API aktivieren + Billing einrichten

### `Quota-Stopp` zu früh
→ Tab Quota → `Monatliches Limit` erhöhen oder `Stop bei Prozent` erhöhen

### Keine Bilder obwohl Panoramen gefunden
→ Meist API-Key-Problem. Tab `API-Key` → `Key testen`

---

## Abhängigkeiten

| Paket | Version | Zweck |
|---|---|---|
| `streetview-dl` | aktuell | Panorama-Query und -Download |
| `requests` | ≥2.28 | HTTP für Nominatim + API-Test |
| `Pillow` | ≥9.0 | Bildverarbeitung (von streetview-dl genutzt) |
| `tkinter` | Systempaket | GUI |

---

## Lizenz

MIT License – freie Nutzung, Weitergabe und Anpassung.
