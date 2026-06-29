#!/usr/bin/env python3
"""
streetview_manager.py  –  StreetView Street Manager v1.0
Selbst-installierend, kein manuelles pip nötig.
Linux-tauglich (Ubuntu/Debian/Arch). Erfordert nur Python 3.8+ und python3-tk.

Starten mit:
    python3 streetview_manager.py

Abhängigkeiten werden automatisch per pip installiert.
"""

# ─── SELF-INSTALL ─────────────────────────────────────────────────────────────
import sys
import subprocess
import importlib

REQUIRED = {"requests": "requests", "PIL": "Pillow"}

def _ensure_deps():
    missing = []
    for imp, pkg in REQUIRED.items():
        try:
            importlib.import_module(imp)
        except ModuleNotFoundError:
            missing.append(pkg)
    if missing:
        print(f"[INSTALL] Fehlende Pakete werden installiert: {missing}")
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "--quiet", *missing
        ])
        print("[INSTALL] Fertig.")

_ensure_deps()

def _ensure_streetview_dl():
    try:
        result = subprocess.run(
            ["streetview-dl", "--version"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"[INSTALL] streetview-dl vorhanden: {result.stdout.strip()}")
            return
    except FileNotFoundError:
        pass
    print("[INSTALL] streetview-dl wird installiert ...")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "--quiet", "streetview-dl"
    ])
    print("[INSTALL] streetview-dl installiert.")

_ensure_streetview_dl()

# ─── STANDARD IMPORTS ─────────────────────────────────────────────────────────
import json
import logging
import math
import os
import queue
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import (
    Tk, StringVar, IntVar, DoubleVar, BooleanVar,
    END, filedialog, messagebox, scrolledtext
)
from tkinter import ttk

import requests

# ─── LOGGING SETUP ────────────────────────────────────────────────────────────
APP_DIR  = Path(__file__).resolve().parent
LOG_DIR  = APP_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATE_DIR = APP_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)

LOG_FILE  = LOG_DIR / f"session_{datetime.now():%Y%m%d_%H%M%S}.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  [%(levelname)-7s]  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sv_manager")
log.info("Session gestartet. Log: %s", LOG_FILE)

# ─── CONSTANTS ────────────────────────────────────────────────────────────────
MONTH_KEY  = datetime.now().strftime("%Y-%m")
STATE_FILE = STATE_DIR / f"quota_{MONTH_KEY}.json"

TILES_PER_QUALITY = {"low": 32, "medium": 128, "high": 512}
QUERY_COST        = 2   # konservative Schätzung pro Query-Aufruf

PALETTE = {
    "bg":        "#1c1b1a",
    "surface":   "#242321",
    "surface2":  "#2b2a28",
    "border":    "#3a3835",
    "text":      "#e4e2de",
    "muted":     "#8f8d87",
    "primary":   "#4f98a3",
    "success":   "#6daa45",
    "warning":   "#c97b36",
    "error":     "#d15a5a",
    "entry_bg":  "#1e1d1b",
}

# ─── QUOTA TRACKER ────────────────────────────────────────────────────────────
class QuotaTracker:
    """Lokaler, persistenter Tile-Request-Zähler pro Kalendermonat."""

    def __init__(self):
        self.limit        = 100_000
        self.stop_percent = 80
        self.used         = 0
        self._lock        = threading.Lock()
        self.load()

    @property
    def stop_limit(self) -> int:
        return int(self.limit * self.stop_percent / 100)

    @property
    def remaining(self) -> int:
        return max(0, self.stop_limit - self.used)

    @property
    def percent_used(self) -> float:
        return round(self.used / max(1, self.limit) * 100, 1)

    def can_spend(self, amount: int) -> bool:
        with self._lock:
            return self.used + amount <= self.stop_limit

    def add(self, amount: int):
        with self._lock:
            self.used += amount
        self.save()
        log.debug("Quota +%d → %d / %d (stop at %d)",
                  amount, self.used, self.limit, self.stop_limit)

    def reset_month(self):
        with self._lock:
            self.used = 0
        self.save()
        log.info("Quota-Zähler auf 0 zurückgesetzt.")

    def load(self):
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text())
                if d.get("month") == MONTH_KEY:
                    self.used         = int(d.get("used",         self.used))
                    self.limit        = int(d.get("limit",        self.limit))
                    self.stop_percent = int(d.get("stop_percent", self.stop_percent))
                    log.info("Quota geladen: %d / %d (stop %d%%)",
                             self.used, self.limit, self.stop_percent)
                else:
                    log.info("Neuer Monat – Quota-Zähler beginnt bei 0.")
            except Exception as exc:
                log.warning("Quota-Datei Lesefehler: %s", exc)

    def save(self):
        try:
            STATE_FILE.write_text(json.dumps({
                "month":        MONTH_KEY,
                "used":         self.used,
                "limit":        self.limit,
                "stop_percent": self.stop_percent,
                "updated":      datetime.now().isoformat(timespec="seconds"),
            }, indent=2))
        except Exception as exc:
            log.error("Quota speichern fehlgeschlagen: %s", exc)


# ─── GEO HELPERS ──────────────────────────────────────────────────────────────
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Abstand in Metern zwischen zwei GPS-Koordinaten."""
    R   = 6_371_000
    p1  = math.radians(lat1)
    p2  = math.radians(lat2)
    dp  = math.radians(lat2 - lat1)
    dl  = math.radians(lon2 - lon1)
    a   = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def interpolate(coords: list, every_m: float) -> list:
    """Erzeugt dichte Probepunkte entlang einer Linie."""
    if len(coords) < 2:
        return coords
    out, carry = [coords[0]], 0.0
    for i in range(1, len(coords)):
        la1, lo1 = coords[i - 1]
        la2, lo2 = coords[i]
        seg = haversine(la1, lo1, la2, lo2)
        if seg == 0:
            continue
        d = carry
        while d + every_m <= seg:
            d += every_m
            r = d / seg
            out.append((la1 + (la2 - la1) * r, lo1 + (lo2 - lo1) * r))
        carry = seg - d
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    log.debug(
        "Interpolation: %d Punkte aus %d Koordinaten (alle %.0f m)",
        len(out), len(coords), every_m
    )
    return out


def geocode_street(street: str, city: str) -> list:
    """Straßengeometrie von OpenStreetMap/Nominatim holen (kostenlos, kein Key nötig)."""
    query = f"{street}, {city}"
    log.info("Geokodierung: %s", query)
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "json", "polygon_geojson": 1, "limit": 1},
        headers={"User-Agent": "streetview-street-manager/1.0"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    log.debug("Nominatim: %d Ergebnis(se) für '%s'", len(data), query)
    if not data:
        raise RuntimeError(f"Straße nicht gefunden: {query}")
    geo  = data[0].get("geojson", {})
    gtyp = geo.get("type")
    c    = geo.get("coordinates", [])
    log.info("Geometrie-Typ: %s  Roh-Punkte: %d", gtyp, len(c))
    if gtyp == "LineString":
        return [(lat, lon) for lon, lat in c]
    if gtyp == "MultiLineString":
        pts = []
        for line in c:
            pts += [(lat, lon) for lon, lat in line]
        return pts
    # Fallback: Bounding-Box-Mittelpunkt
    bb = data[0].get("boundingbox")
    if bb:
        return [((float(bb[0]) + float(bb[1])) / 2,
                 (float(bb[2]) + float(bb[3])) / 2)]
    raise RuntimeError("Keine verwertbare Straßengeometrie gefunden.")


def check_api_key(key: str) -> tuple:
    """Testet den Google API-Key gegen die Map Tiles API."""
    log.info("API-Key wird getestet (Länge: %d)...", len(key))
    if not key or len(key) < 20:
        return False, "API-Key ist leer oder zu kurz."
    url = (
        "https://tile.googleapis.com/v1/streetview/tiles/0/0/0"
        f"?session=&key={key}"
    )
    try:
        r = requests.get(url, timeout=12)
        log.debug("API-Test HTTP %d", r.status_code)
        body = r.text[:400]
        log.debug("API-Test Body: %s", body)
        if r.status_code == 200:
            return True, "API-Key gültig — Map Tiles API erreichbar."
        if "API_KEY_INVALID" in body or "keyInvalid" in body:
            return False, f"API-Key ungültig (Google: keyInvalid).\n{body}"
        if "REQUEST_DENIED" in body or "accessNotConfigured" in body:
            return False, (
                "Map Tiles API nicht aktiviert oder Billing fehlt.\n"
                "→ console.cloud.google.com → APIs → Map Tiles API aktivieren.\n" + body
            )
        if r.status_code == 401:
            return False, "HTTP 401 – Key ungültig oder nicht autorisiert."
        if r.status_code in (400, 403):
            return True, f"HTTP {r.status_code} – Key erkannt (kein harter Fehler). Prüfe Billing."
        return False, f"Unerwarteter HTTP-Status: {r.status_code}\n{body}"
    except requests.exceptions.ConnectionError as exc:
        return False, f"Keine Netzwerkverbindung: {exc}"
    except Exception as exc:
        log.exception("API-Test Exception")
        return False, f"Netzwerkfehler: {exc}"


# ─── HAUPTANWENDUNG ───────────────────────────────────────────────────────────
class App:
    """Tkinter-GUI für den StreetView Street Manager."""

    def __init__(self, root: Tk):
        self.root      = root
        self.root.title("StreetView Street Manager")
        self.root.geometry("1400x900")
        self.root.minsize(1100, 700)
        self.q             = queue.Queue()
        self.stop_flag     = threading.Event()
        self.quota         = QuotaTracker()
        self.pano_ids: set = set()
        self.panos: list   = []
        self.url_file      = None

        self._init_vars()
        self._apply_theme()
        self._build_ui()
        self._refresh_quota_display()
        self.root.after(120, self._poll_queue)
        log.info("GUI bereit.")

    # ── VARIABLEN ─────────────────────────────────────────────────────────────
    def _init_vars(self):
        self.v_street     = StringVar(value="Berger Straße")
        self.v_city       = StringVar(value="Frankfurt am Main")
        self.v_output     = StringVar(value=str(APP_DIR / "downloads"))
        self.v_api_key    = StringVar(value=os.environ.get("GOOGLE_MAPS_API_KEY", ""))
        self.v_quality    = StringVar(value="medium")
        self.v_sample     = IntVar(value=5)
        self.v_radius     = IntVar(value=8)
        self.v_max_res    = IntVar(value=5)
        self.v_pause      = DoubleVar(value=0.2)
        self.v_limit      = IntVar(value=self.quota.limit)
        self.v_stop_pct   = IntVar(value=self.quota.stop_percent)
        self.v_used       = IntVar(value=self.quota.used)
        self.v_historical = BooleanVar(value=False)
        self.v_status     = StringVar(value="Bereit")

    # ── DARK THEME ────────────────────────────────────────────────────────────
    def _apply_theme(self):
        P = PALETTE
        self.root.configure(bg=P["bg"])
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".",
            background=P["bg"], foreground=P["text"],
            font=("Segoe UI", 10)
        )
        s.configure("TFrame",     background=P["bg"])
        s.configure("TLabel",     background=P["bg"], foreground=P["text"])
        s.configure("TLabelframe",
            background=P["bg"], foreground=P["primary"],
            relief="flat", borderwidth=1
        )
        s.configure("TLabelframe.Label",
            background=P["bg"], foreground=P["primary"],
            font=("Segoe UI", 10, "bold")
        )
        s.configure("TNotebook",  background=P["surface"], borderwidth=0)
        s.configure("TNotebook.Tab",
            background=P["surface2"], foreground=P["muted"],
            padding=(16, 9), font=("Segoe UI", 10)
        )
        s.map("TNotebook.Tab",
              background=[("selected", P["primary"])],
              foreground=[("selected", "#ffffff")])
        s.configure("TEntry",
            fieldbackground=P["entry_bg"], foreground=P["text"],
            insertcolor=P["text"], borderwidth=1, relief="flat"
        )
        s.configure("TCombobox",
            fieldbackground=P["entry_bg"], foreground=P["text"],
            selectbackground=P["primary"], borderwidth=1, relief="flat"
        )
        s.map("TCombobox", fieldbackground=[("readonly", P["entry_bg"])])
        s.configure("TSpinbox",
            fieldbackground=P["entry_bg"], foreground=P["text"],
            arrowcolor=P["primary"], borderwidth=1, relief="flat"
        )
        s.configure("TCheckbutton", background=P["bg"], foreground=P["text"])
        s.configure("TButton",
            background=P["surface2"], foreground=P["text"],
            padding=(12, 8), relief="flat", borderwidth=0
        )
        s.map("TButton",
              background=[("active", P["border"])],
              foreground=[("active", P["text"])])
        s.configure("Primary.TButton",
            background=P["primary"], foreground="#ffffff"
        )
        s.map("Primary.TButton",
              background=[("active", "#3a7f8a")])
        s.configure("Danger.TButton",
            background=P["error"], foreground="#ffffff"
        )
        s.map("Danger.TButton",
              background=[("active", "#b04040")])
        s.configure("Success.TButton",
            background=P["success"], foreground="#ffffff"
        )
        s.map("Success.TButton",
              background=[("active", "#598c36")])
        s.configure("TProgressbar",
            troughcolor=P["surface2"], background=P["primary"],
            borderwidth=0, thickness=9
        )
        s.configure("Muted.TLabel",
            background=P["bg"], foreground=P["muted"],
            font=("Segoe UI", 9)
        )
        s.configure("Header.TLabel",
            background=P["bg"], foreground=P["text"],
            font=("Segoe UI", 17, "bold")
        )
        s.configure("Sub.TLabel",
            background=P["bg"], foreground=P["muted"],
            font=("Segoe UI", 10)
        )
        s.configure("Status.TLabel",
            background=P["bg"], foreground=P["primary"],
            font=("Segoe UI", 10, "bold")
        )
        s.configure("OK.TLabel",   background=P["bg"], foreground=P["success"])
        s.configure("Warn.TLabel", background=P["bg"], foreground=P["warning"])
        s.configure("Error.TLabel",background=P["bg"], foreground=P["error"])

    # ── UI AUFBAU ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        P = PALETTE
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        # Titel-Header
        hdr = ttk.Frame(outer)
        hdr.pack(fill="x", pady=(0, 14))
        ttk.Label(
            hdr, text="StreetView Street Manager",
            style="Header.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            hdr,
            text="Vollständiger Straßen-Panorama-Export  ·  Quota-Schutz  ·  API-Key-Test  ·  Debug-Logging",
            style="Sub.TLabel"
        ).pack(anchor="w", pady=(3, 0))

        # Quota-Statusleiste
        qbar = ttk.Frame(outer)
        qbar.pack(fill="x", pady=(0, 10))
        self.lbl_used   = ttk.Label(qbar, text="Verbraucht: 0",  style="Muted.TLabel")
        self.lbl_stop   = ttk.Label(qbar, text="Stop bei: 0",    style="Muted.TLabel")
        self.lbl_remain = ttk.Label(qbar, text="Verfügbar: 0",   style="Muted.TLabel")
        self.lbl_status = ttk.Label(qbar, textvariable=self.v_status, style="Status.TLabel")
        for w in (self.lbl_used, self.lbl_stop, self.lbl_remain, self.lbl_status):
            w.pack(side="left", padx=(0, 22))
        self.progress = ttk.Progressbar(qbar, length=220, mode="determinate")
        self.progress.pack(side="left", padx=(0, 8))
        self.lbl_pct = ttk.Label(qbar, text="0 %", style="Muted.TLabel")
        self.lbl_pct.pack(side="left")

        # Notebook (Tabs)
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)

        self._tab_project(nb)
        self._tab_run(nb)
        self._tab_options(nb)
        self._tab_quota(nb)
        self._tab_api(nb)
        self._tab_log(nb)

    # ── TAB: Projekt ──────────────────────────────────────────────────────────
    def _tab_project(self, nb):
        frm = ttk.Frame(nb, padding=22)
        nb.add(frm, text="  Projekt  ")
        frm.columnconfigure(1, weight=1)

        self._lf_row(frm, 0, "Straße",        self.v_street, "z. B.  Berger Straße")
        self._lf_row(frm, 1, "Ort / Stadt",   self.v_city,   "z. B.  Frankfurt am Main")
        self._lf_row(frm, 2, "Ausgabeordner", self.v_output, "Bilder und URL-Datei werden hier gespeichert")
        ttk.Button(
            frm, text="Ordner auswählen …",
            command=self._choose_output
        ).grid(row=2, column=2, padx=(10, 0), sticky="w")

        info = ttk.LabelFrame(frm, text="Ablauf", padding=14)
        info.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(22, 0))
        steps = [
            "1.  Straße über OpenStreetMap geokodieren und Liniengeometrie laden.",
            "2.  Punkte entlang der Linie samplen (Standard: alle 5 m).",
            "3.  Für jeden Punkt streetview-dl query aufrufen → echte Panorama-IDs sammeln.",
            "4.  Alle pano_id-Werte deduplizieren → jede Aufnahme wird nur einmal heruntergeladen.",
            "5.  Tile-Requests werden lokal mitgeschätzt und der Lauf stoppt bei deinem Limit (Standard: 80 %).",
        ]
        for i, step in enumerate(steps):
            ttk.Label(info, text=step, style="Muted.TLabel").grid(
                row=i, column=0, sticky="w", pady=2
            )

    # ── TAB: Ausführen ────────────────────────────────────────────────────────
    def _tab_run(self, nb):
        frm = ttk.Frame(nb, padding=22)
        nb.add(frm, text="  Ausführen  ")
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)

        acts = ttk.Frame(frm)
        acts.grid(row=0, column=0, sticky="w")
        ttk.Button(
            acts, text="▶  1. Straße analysieren",
            style="Primary.TButton", command=self._start_scan
        ).pack(side="left", padx=(0, 10))
        ttk.Button(
            acts, text="⬇  2. Download starten",
            style="Success.TButton", command=self._start_download
        ).pack(side="left", padx=(0, 10))
        ttk.Button(
            acts, text="⏹  Stopp",
            style="Danger.TButton", command=self._request_stop
        ).pack(side="left")

        self.lbl_summary = ttk.Label(
            frm, text="Noch kein Lauf gestartet.",
            style="Muted.TLabel", wraplength=1200
        )
        self.lbl_summary.grid(row=1, column=0, sticky="w", pady=(12, 0))

        # Ergebnis-Tabelle
        rf = ttk.LabelFrame(frm, text="Gefundene Panoramen", padding=10)
        rf.grid(row=2, column=0, sticky="nsew", pady=(16, 0))
        rf.columnconfigure(0, weight=1)
        rf.rowconfigure(0, weight=1)

        cols = ("pano_id", "lat", "lng", "date")
        self.tree = ttk.Treeview(rf, columns=cols, show="headings", height=16)
        for col, width, head in zip(
            cols, (300, 110, 110, 100),
            ("Pano-ID", "Latitude", "Longitude", "Datum")
        ):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=width, anchor="w")
        sb = ttk.Scrollbar(rf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

    # ── TAB: Optionen ─────────────────────────────────────────────────────────
    def _tab_options(self, nb):
        frm = ttk.Frame(nb, padding=22)
        nb.add(frm, text="  Optionen  ")
        frm.columnconfigure(1, weight=1)

        self._lf_spin(
            frm, 0, "Sampling-Abstand (m)", self.v_sample, 1, 200,
            "5 m = vollständig & dicht  ·  30 m = schnell, lückenhaft"
        )
        self._lf_spin(
            frm, 1, "Suchradius pro Punkt (m)", self.v_radius, 1, 100,
            "Klein halten (5–15 m) → weniger Duplikate bei dichtem Sampling"
        )
        self._lf_spin(
            frm, 2, "Max. Treffer pro Punkt", self.v_max_res, 1, 20,
            "3–5 reicht für normale Straßen"
        )

        ttk.Label(frm, text="Bildqualität").grid(
            row=3, column=0, sticky="w", pady=8, padx=(0, 12)
        )
        cb = ttk.Combobox(
            frm, textvariable=self.v_quality,
            values=["low", "medium", "high"],
            state="readonly", width=18
        )
        cb.grid(row=3, column=1, sticky="w")
        ttk.Label(
            frm,
            text="low = 32 Tiles/Pano  ·  medium = 128  ·  high = 512",
            style="Muted.TLabel"
        ).grid(row=3, column=2, sticky="w", padx=(12, 0))

        self._lf_spin(
            frm, 4, "Pause zw. Queries (s)", self.v_pause,
            0.05, 5.0, "0.2 s schont die API und verhindert Rate-Limits",
            incr=0.05
        )

        ttk.Checkbutton(
            frm,
            text="Historische Aufnahmen ebenfalls laden"
                 " (erhöht Quota-Bedarf deutlich – ca. 2.5× mehr Tiles)",
            variable=self.v_historical
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(16, 0))

    # ── TAB: Quota ────────────────────────────────────────────────────────────
    def _tab_quota(self, nb):
        frm = ttk.Frame(nb, padding=22)
        nb.add(frm, text="  Quota  ")
        frm.columnconfigure(1, weight=1)

        self._lf_spin(
            frm, 0, "Monatliches Limit (Tiles)", self.v_limit,
            1_000, 1_000_000,
            "Google: 100.000 Tiles/Monat kostenlos (Stand 2024)",
            incr=1000
        )
        self._lf_spin(
            frm, 1, "Stopp bei Prozent (%)", self.v_stop_pct,
            1, 100,
            "80 % = sicherer Puffer · 100 % = kein Puffer",
            incr=1
        )
        self._lf_spin(
            frm, 2, "Bereits verbraucht (Tiles)", self.v_used,
            0, 1_000_000,
            "Auf deinen echten Cloud Console-Stand setzen",
            incr=100
        )

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(16, 0))
        ttk.Button(
            btn_row, text="Einstellungen speichern & anwenden",
            style="Primary.TButton", command=self._save_quota
        ).pack(side="left", padx=(0, 12))
        ttk.Button(
            btn_row, text="Monatszähler auf 0 zurücksetzen",
            style="Danger.TButton", command=self._reset_quota
        ).pack(side="left")

        note = ttk.LabelFrame(frm, text="Hinweis zur Messgenauigkeit", padding=14)
        note.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(24, 0))
        msg = (
            "Google bietet keine direkte Live-Abfrage des tatsächlichen Monatsverbrauchs "
            "über die Map Tiles API an. Das würde eine separate Cloud-Monitoring- und "
            "Billing-API-Einrichtung erfordern. Dieses Tool schätzt den Verbrauch daher "
            "lokal und konservativ:\n"
            "  • Query-Aufrufe: je ~2 Tiles\n"
            "  • Download low: 32 Tiles/Pano  ·  medium: 128  ·  high: 512\n"
            "Wenn du den echten Stand aus der Google Cloud Console kennst, trage ihn "
            "manuell in 'Bereits verbraucht' ein."
        )
        ttk.Label(
            note, text=msg, style="Muted.TLabel",
            wraplength=900, justify="left"
        ).pack(anchor="w")

    # ── TAB: API-Key ──────────────────────────────────────────────────────────
    def _tab_api(self, nb):
        frm = ttk.Frame(nb, padding=22)
        nb.add(frm, text="  API-Key  ")
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Google Maps API-Key").grid(
            row=0, column=0, sticky="w", pady=8, padx=(0, 12)
        )
        self.entry_key = ttk.Entry(
            frm, textvariable=self.v_api_key, width=64, show="•"
        )
        self.entry_key.grid(row=0, column=1, sticky="ew")

        def _toggle_show():
            cur = self.entry_key.cget("show")
            self.entry_key.config(show="" if cur == "•" else "•")

        ttk.Checkbutton(
            frm, text="Anzeigen", command=_toggle_show
        ).grid(row=0, column=2, padx=(10, 0))

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(
            btn_row,
            text="Speichern & als GOOGLE_MAPS_API_KEY setzen",
            style="Primary.TButton",
            command=self._save_api_key
        ).pack(side="left", padx=(0, 12))
        ttk.Button(
            btn_row, text="Key testen",
            command=self._test_api_key
        ).pack(side="left")

        self.lbl_api_result = ttk.Label(
            frm, text="", wraplength=900
        )
        self.lbl_api_result.grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(14, 0)
        )

        guide = ttk.LabelFrame(frm, text="Google API-Key einrichten (Schritt für Schritt)", padding=14)
        guide.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(22, 0))
        steps = [
            "1.  Google Cloud Console öffnen: https://console.cloud.google.com",
            "2.  Neues Projekt erstellen (oder bestehendes Projekt auswählen).",
            "3.  APIs & Dienste  →  Bibliothek  →  'Map Tiles API' suchen und aktivieren.",
            "4.  APIs & Dienste  →  Anmeldedaten  →  '+ Anmeldedaten erstellen'  →  API-Schlüssel.",
            "5.  Abrechnung (Billing) aktivieren: Konto verknüpfen (Kreditkarte, wird nicht belastet solange < 100.000 Tiles/Monat).",
            "6.  Optional: Key einschränken auf 'Map Tiles API' für mehr Sicherheit.",
            "7.  Key oben eintragen, auf 'Speichern' klicken und dann 'Key testen' drücken.",
        ]
        for i, step in enumerate(steps):
            ttk.Label(guide, text=step, style="Muted.TLabel").grid(
                row=i, column=0, sticky="w", pady=3
            )

    # ── TAB: Debug-Log ────────────────────────────────────────────────────────
    def _tab_log(self, nb):
        frm = ttk.Frame(nb, padding=14)
        nb.add(frm, text="  Debug-Log  ")
        frm.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)

        self.log_widget = scrolledtext.ScrolledText(
            frm,
            wrap="word",
            bg="#111110",
            fg="#b0afa9",
            insertbackground="#b0afa9",
            font=("Monospace", 9),
            relief="flat",
        )
        self.log_widget.grid(row=0, column=0, sticky="nsew")

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(
            btn_row, text="Log leeren",
            command=lambda: self.log_widget.delete("1.0", END)
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            btn_row, text=f"Log-Datei öffnen ({LOG_FILE.name})",
            command=lambda: subprocess.Popen(["xdg-open", str(LOG_FILE)])
        ).pack(side="left")
        ttk.Label(
            btn_row,
            text=f"  Datei: {LOG_FILE}",
            style="Muted.TLabel"
        ).pack(side="left", padx=(12, 0))

    # ── HILFS-METHODEN ────────────────────────────────────────────────────────
    def _lf_row(self, parent, row, label, var, help_text=""):
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", pady=8, padx=(0, 12)
        )
        ttk.Entry(parent, textvariable=var).grid(
            row=row, column=1, sticky="ew"
        )
        if help_text:
            ttk.Label(parent, text=help_text, style="Muted.TLabel").grid(
                row=row, column=2, sticky="w", padx=(12, 0)
            )

    def _lf_spin(self, parent, row, label, var, frm_v, to_v,
                 help_text="", incr=1):
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", pady=8, padx=(0, 12)
        )
        ttk.Spinbox(
            parent, from_=frm_v, to=to_v,
            textvariable=var, increment=incr, width=14
        ).grid(row=row, column=1, sticky="w")
        if help_text:
            ttk.Label(parent, text=help_text, style="Muted.TLabel").grid(
                row=row, column=2, sticky="w", padx=(12, 0)
            )

    def _choose_output(self):
        d = filedialog.askdirectory(initialdir=self.v_output.get() or str(APP_DIR))
        if d:
            self.v_output.set(d)
            log.debug("Ausgabeordner: %s", d)

    def _gui_log(self, text: str):
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_widget.insert(END, f"{ts}  {text}\n")
            self.log_widget.see(END)
        except Exception:
            pass

    def _update_summary(self, text: str):
        try:
            self.lbl_summary.config(text=text)
        except Exception:
            pass

    def _refresh_quota_display(self):
        q = self.quota
        self.lbl_used.config(text=f"Verbraucht: {q.used:,}")
        self.lbl_stop.config(text=f"Stop bei: {q.stop_limit:,}")
        self.lbl_remain.config(text=f"Verfügbar: {q.remaining:,}")
        pct = q.percent_used
        self.progress["value"] = min(100, pct)
        style = "OK.TLabel" if pct < 60 else ("Warn.TLabel" if pct < q.stop_percent else "Error.TLabel")
        self.lbl_pct.config(text=f"{pct} %", style=style)

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._gui_log(msg[1])
                elif kind == "status":
                    self.v_status.set(msg[1])
                elif kind == "summary":
                    self._update_summary(msg[1])
                elif kind == "quota":
                    self._refresh_quota_display()
                elif kind == "tree_add":
                    p = msg[1]
                    self.tree.insert(
                        "", "end",
                        values=(
                            p["pano_id"],
                            f"{p['lat']:.6f}",
                            f"{p['lng']:.6f}",
                            p.get("date", "?")
                        )
                    )
                elif kind == "done":
                    messagebox.showinfo("Fertig", msg[1])
                elif kind == "error":
                    messagebox.showerror("Fehler", msg[1])
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    # ── QUOTA AKTIONEN ────────────────────────────────────────────────────────
    def _save_quota(self):
        self.quota.limit        = int(self.v_limit.get())
        self.quota.stop_percent = int(self.v_stop_pct.get())
        self.quota.used         = int(self.v_used.get())
        self.quota.save()
        self._refresh_quota_display()
        self._gui_log("[Quota] Einstellungen gespeichert.")
        log.info(
            "Quota gespeichert: limit=%d  stop=%d%%  used=%d",
            self.quota.limit, self.quota.stop_percent, self.quota.used
        )

    def _reset_quota(self):
        if messagebox.askyesno(
            "Zurücksetzen?",
            "Monatszähler wirklich auf 0 setzen?\n"
            "Das ist sinnvoll zum Monatsbeginn."
        ):
            self.quota.reset_month()
            self.v_used.set(0)
            self._refresh_quota_display()
            self._gui_log("[Quota] Zähler auf 0 zurückgesetzt.")

    # ── API-KEY AKTIONEN ──────────────────────────────────────────────────────
    def _save_api_key(self):
        key = self.v_api_key.get().strip()
        os.environ["GOOGLE_MAPS_API_KEY"] = key
        self._gui_log(
            f"[API] GOOGLE_MAPS_API_KEY gesetzt (Länge: {len(key)} Zeichen)."
        )
        log.info("API-Key als Env-Variable gesetzt (Länge: %d).", len(key))
        messagebox.showinfo(
            "Gespeichert",
            "API-Key als Umgebungsvariable für diese Sitzung gesetzt.\n\n"
            "Für dauerhafte Speicherung füge folgendes in ~/.bashrc ein:\n\n"
            f'export GOOGLE_MAPS_API_KEY="{key}"'
        )

    def _test_api_key(self):
        key = self.v_api_key.get().strip()
        os.environ["GOOGLE_MAPS_API_KEY"] = key
        self.lbl_api_result.config(
            text="Verbinde mit Google Map Tiles API …",
            style="Muted.TLabel"
        )
        self._gui_log(f"[API] Teste Key (Länge: {len(key)}) …")

        def _run():
            ok, msg = check_api_key(key)
            style  = "OK.TLabel" if ok else "Error.TLabel"
            prefix = "✓" if ok else "✗"
            self.lbl_api_result.config(text=f"{prefix}  {msg}", style=style)
            self._gui_log(f"[API] {'OK' if ok else 'FEHLER'}: {msg}")
            log.info("API-Key-Test Ergebnis: ok=%s  msg=%s", ok, msg)

        threading.Thread(target=_run, daemon=True).start()

    # ── STOPP ─────────────────────────────────────────────────────────────────
    def _request_stop(self):
        self.stop_flag.set()
        self.v_status.set("Stopp angefordert …")
        self._gui_log("[Stopp] Stopp-Signal gesendet.")
        log.info("Stopp-Signal gesetzt.")

    # ── SCAN ──────────────────────────────────────────────────────────────────
    def _start_scan(self):
        self.stop_flag.clear()
        self.pano_ids.clear()
        self.panos.clear()
        self.url_file = None
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._gui_log("[Scan] Neuer Lauf gestartet.")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            street  = self.v_street.get().strip()
            city    = self.v_city.get().strip()
            sample  = int(self.v_sample.get())
            radius  = int(self.v_radius.get())
            max_res = int(self.v_max_res.get())
            pause   = float(self.v_pause.get())

            self.q.put(("status",  "Straße analysieren …"))
            self.q.put(("summary", f"Geokodiere: {street}, {city} …"))
            self.q.put(("log",     f"[Scan] Straße: {street}, {city}"))
            self.q.put(("log",     f"[Scan] Sampling: alle {sample} m | Suchradius: {radius} m | max. Treffer/Punkt: {max_res}"))
            self.q.put(("log",     f"[Scan] Pause: {pause} s | Qualität: {self.v_quality.get()}"))

            coords = geocode_street(street, city)
            self.q.put(("log", f"[Scan] Geometrie: {len(coords)} Koordinatenpunkte aus OSM"))

            points = interpolate(coords, sample)
            total  = len(points)
            self.q.put(("log",     f"[Scan] Sample-Punkte: {total}"))
            self.q.put(("summary", f"Suche Panoramen auf {total} Punkten …"))

            dup_streak = 0
            for idx, (lat, lng) in enumerate(points, 1):
                if self.stop_flag.is_set():
                    self.q.put(("log",    "[Scan] Abgebrochen durch Nutzer."))
                    self.q.put(("status", "Abgebrochen"))
                    break

                if not self.quota.can_spend(QUERY_COST):
                    self.q.put(("log",
                        f"[Quota] Stopp-Limit erreicht: {self.quota.used:,} / "
                        f"{self.quota.stop_limit:,} Tiles. Lauf beendet."
                    ))
                    self.q.put(("status", "Quota-Stopp"))
                    break

                self.quota.add(QUERY_COST)
                self.q.put(("quota",))

                self.q.put(("status", f"Punkt {idx} / {total}"))
                self.q.put(("log",
                    f"[{idx:>5}/{total}]  lat={lat:.6f}  lng={lng:.6f}"
                ))

                cmd = [
                    "streetview-dl", "query",
                    "--lat", str(lat),
                    "--lng", str(lng),
                    "--radius", str(radius),
                    "--max-results", str(max_res),
                    "--json",
                ]
                log.debug("CMD: %s", " ".join(cmd))

                result = subprocess.run(
                    cmd, capture_output=True, text=True
                )
                log.debug("RETURNCODE: %d", result.returncode)
                if result.stderr.strip():
                    log.debug("STDERR: %s", result.stderr.strip())

                if result.returncode != 0:
                    err = (result.stderr.strip() or result.stdout.strip())[:160]
                    self.q.put(("log", f"  → Kein Treffer / Fehler: {err}"))
                    time.sleep(pause)
                    continue

                try:
                    data = json.loads(result.stdout)
                except json.JSONDecodeError:
                    self.q.put(("log",
                        f"  → JSON-Parsefehler. Raw: {result.stdout[:200]}"
                    ))
                    log.warning("JSON-Fehler: %s", result.stdout[:200])
                    time.sleep(pause)
                    continue

                panos_here = data.get("panoramas", [])
                log.debug("Treffer roh: %d", len(panos_here))
                new_count = 0
                for p in panos_here:
                    pid = p.get("pano_id")
                    if pid and pid not in self.pano_ids:
                        self.pano_ids.add(pid)
                        entry = {
                            "pano_id": pid,
                            "lat":     p.get("lat", lat),
                            "lng":     p.get("lng", lng),
                            "date":    p.get("date", "?"),
                        }
                        self.panos.append(entry)
                        new_count += 1
                        self.q.put(("tree_add", entry))

                dup_streak = (dup_streak + 1) if new_count == 0 else 0
                self.q.put(("log",
                    f"  → Treffer: {len(panos_here):2}  |  neu: {new_count:2}  |  "
                    f"gesamt eindeutig: {len(self.pano_ids):4}  |  dup-streak: {dup_streak}"
                ))

                if dup_streak >= 6:
                    self.q.put(("log",
                        "  → 6× hintereinander keine neuen pano_ids. "
                        "Straßenabschnitt wahrscheinlich vollständig abgedeckt."
                    ))
                    dup_streak = 0

                time.sleep(pause)

            # ── URL-Datei schreiben ────────────────────────────────────────
            if self.panos:
                outdir = Path(self.v_output.get())
                outdir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^\w]", "_", f"{street}_{city}").lower()
                url_path = outdir / f"streetview_urls_{safe}.txt"
                with url_path.open("w") as fh:
                    for p in self.panos:
                        fh.write(
                            f"https://www.google.com/maps/@{p['lat']},{p['lng']},"
                            f"3a,75y,0h,90t/data=!3m7!1e1!3m5!1s{p['pano_id']}!\n"
                        )
                self.url_file = url_path
                quality = self.v_quality.get().split()[0]
                tiles_est = len(self.panos) * TILES_PER_QUALITY.get(quality, 128)
                summary = (
                    f"Analyse fertig: {len(self.panos)} eindeutige Panoramen gefunden.  "
                    f"URL-Datei: {url_path.name}  |  "
                    f"Geschätzter Download-Bedarf: {tiles_est:,} Tiles."
                )
                self.q.put(("summary", summary))
                self.q.put(("log",     f"[Scan] {summary}"))
                self.q.put(("log",     f"[Scan] URL-Datei: {url_path}"))
                self.q.put(("status",  "Analyse fertig — Download starten!"))
                self.q.put(("done",    summary))
            else:
                self.q.put(("summary", "Keine Panoramen gefunden."))
                self.q.put(("status",  "Keine Treffer"))
                self.q.put(("log",     "[Scan] Kein einziges Panorama gefunden. Straße/Ort prüfen."))

        except Exception:
            tb = traceback.format_exc()
            log.error("Scan-Worker Exception:\n%s", tb)
            self.q.put(("log",   f"[FEHLER]\n{tb}"))
            self.q.put(("error", tb))
            self.q.put(("status", "Fehler"))

    # ── DOWNLOAD ──────────────────────────────────────────────────────────────
    def _start_download(self):
        if not self.url_file or not Path(self.url_file).exists():
            messagebox.showwarning(
                "Hinweis",
                "Bitte zuerst eine Straße analysieren (Schritt 1).\n"
                "Erst danach steht die URL-Datei für den Download bereit."
            )
            return
        self.stop_flag.clear()
        self._gui_log("[Download] Neuer Download-Lauf gestartet.")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        try:
            quality = self.v_quality.get().split()[0]
            tiles_each  = TILES_PER_QUALITY.get(quality, 128)
            tiles_total = len(self.panos) * tiles_each
            if self.v_historical.get():
                tiles_total = int(tiles_total * 2.5)

            self.q.put(("log",
                f"[Download] {len(self.panos)} Panoramen  |  "
                f"Qualität: {quality}  |  ~{tiles_total:,} Tiles geschätzt"
            ))

            if not self.quota.can_spend(tiles_total):
                raise RuntimeError(
                    f"Download würde die Quota überschreiten.\n"
                    f"Bedarf: {tiles_total:,} Tiles  |  "
                    f"Verfügbar bis Stop: {self.quota.remaining:,} Tiles"
                )

            outdir = Path(self.v_output.get())
            outdir.mkdir(parents=True, exist_ok=True)
            self.q.put(("status", "Download läuft …"))

            cmd = [
                "streetview-dl",
                "--batch",      str(self.url_file),
                "--output-dir", str(outdir),
                "--quality",    quality,
                "--verbose",
            ]
            log.info("Download-CMD: %s", " ".join(cmd))
            self.q.put(("log", f"[Download] CMD: {' '.join(cmd)}"))

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.q.put(("log", f"  {line}"))
                    log.debug("sv-dl out: %s", line)
                if self.stop_flag.is_set():
                    proc.terminate()
                    self.q.put(("log",    "[Download] Prozess beendet durch Stopp."))
                    self.q.put(("status", "Abgebrochen"))
                    return
            proc.wait()
            log.info("streetview-dl Exitcode: %d", proc.returncode)

            self.quota.add(tiles_total)
            self.q.put(("quota",))
            self.q.put(("status", "Download fertig"))
            done = (
                f"Download abgeschlossen! {len(self.panos)} Panoramen. "
                f"Dateien liegen in: {outdir}"
            )
            self.q.put(("summary", done))
            self.q.put(("log",     f"[Download] {done}"))
            self.q.put(("done",    done))

        except Exception:
            tb = traceback.format_exc()
            log.error("Download-Worker Exception:\n%s", tb)
            self.q.put(("log",   f"[FEHLER]\n{tb}"))
            self.q.put(("error", tb))
            self.q.put(("status", "Fehler"))


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = Tk()
    App(root)
    root.mainloop()
