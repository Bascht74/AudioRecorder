#!/usr/bin/env python3
"""
audiorec Statusanzeige für das interne DSI-Display des Raspberry Pi 5.

Liest /run/audiorec/status.json und zeigt den Zustand bildschirmfüllend an —
für beliebig viele gleichzeitig aufnehmende Geräte, jeweils in einem eigenen
Block mit VU-Anzeige. Skaliert automatisch auf die Auflösung des Displays.

Bedienung per Touch: zwei Flächen unten, beide mit Rückfrage.

Läuft im Vollbild und lässt sich absichtlich nicht per Touch beenden —
das Gerät hat keine Tastatur, und ein versehentlich geschlossenes Fenster
fiele mitten im Abend niemandem auf. Beenden von aussen:
    pkill -f status_display.py
"""

import configparser
import json
import subprocess
import sys
import time
import traceback
import tkinter as tk
from pathlib import Path

sys.path.insert(0, "/opt/audiorec")
try:
    import piwatch
except ImportError:
    piwatch = None

STATUS_PATH = Path("/run/audiorec/status.json")
ANSWER_PATH = Path("/run/audiorec/input/answer.json")
REFRESH_MS = 250           # Takt der Anzeige – die VU soll dem Signal folgen
STALE_AFTER_S = 8          # ab wann der Status als veraltet gilt
CONFIRM_S = 5.0            # Bedenkzeit der Rückfrage
CODE_TIMEOUT_S = 30.0      # Zifferneingabe schliesst sich von selbst
CONFIG_PATH = Path("/etc/audiorec/audiorec.conf")
FILE_ROWS = 3              # Dateizeilen je Gerät (bei vielen Geräten weniger)

# ---- VU-Anzeige ----
METER_MIN = -60.0          # unteres Ende der Skala
METER_WARN = -6.0          # ab hier gelb
METER_CLIP = -1.0          # ab hier rot
HOLD_FALL = 20.0           # Spitzenmarke fällt in dB je Sekunde
HOLD_GRID = (0.0, -6.0, -20.0, -40.0)

# Schwellen fuer die Systemwarnungen (Spiegel von piwatch)
TEMP_WARN = 75.0
TEMP_KRITISCH = 80.0
DIRTY_WARN_MB = 250.0
CPU_WARN = 85.0

BG = "#101014"
FG = "#e8e8ec"
# Nebentext. Bewusst deutlich heller als das übliche Grau: das Display
# steht schräg unter Streiflicht, und um 23 Uhr liest niemand mehr #8a8a96.
DIM = "#b9bac6"
HELL = "#ffffff"           # Fußzeile – Systemwerte und Uhr
BOX = "#1a1a20"
BTN_BG = "#2a2a30"
TRACK = "#32323c"          # unbeleuchteter Teil eines Kanalbalkens
HOLD_FG = "#f1f5f9"
KANAL_NR = "#9a9ba8"       # Kanalnummern unter den Balken
GITTER_HELL = "#4a4a58"    # Skalenlinien 0 und -6 dBFS
GITTER_DUNKEL = "#3a3a46"  # Skalenlinien -20 und -40 dBFS

# ---------------------------------------------------------------------------
# Zwei getrennte Paletten.
#
# ZUSTAND: was der Recorder gerade tut. AUFNAHME ist rot – die Farbe, die
# an jedem Aufnahmegerät dafuer steht. Damit FEHLER daneben nicht
# untergeht, bekommt er einen roten Balken statt nur roter Schrift.
#
# BEWERTUNG: ob ein einzelner Wert in Ordnung ist. Hier bleibt gruen das
# Gute – sonst saehe die ganze Anzeige alarmiert aus, obwohl alles laeuft.
# ---------------------------------------------------------------------------
COLORS = {
    "RECORDING": "#f43f5e",    # rot – es wird aufgenommen
    "WAITING":   "#eab308",    # gelb – bereit, wartet auf ein Gerät
    "STARTING":  "#38bdf8",    # blau – Übergang
    "STOPPING":  "#38bdf8",    # blau – Übergang, Dateien werden geschlossen
    "ERROR":     "#ffffff",    # weiß auf rotem Balken, siehe STATE_BG
    "STOPPED":   "#a8a9b4",
    "STALE":     "#ffffff",
}

# Hintergrund der grossen Zustandszeile (leer = wie der Rest)
STATE_BG = {"ERROR": "#7f1d1d", "STALE": "#7f1d1d"}

GUT = "#22c55e"        # Wert in Ordnung
ACHTUNG = "#eab308"    # Wert grenzwertig
SCHLECHT = "#ef4444"   # Wert nicht in Ordnung

LABELS = {
    "RECORDING": "AUFNAHME",
    "WAITING":   "WARTE",
    "STARTING":  "STARTE",
    "STOPPING":  "SCHLIESST",
    "ERROR":     "FEHLER",
    "STOPPED":   "GESTOPPT",
    "STALE":     "KEIN DIENST",
}


def hms(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds // 3600:d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def de(value, digits=2):
    """Zahl mit deutschem Dezimalkomma."""
    return f"{value:.{digits}f}".replace(".", ",")


class Display:
    def __init__(self, root):
        self.root = root
        root.title("audiorec")
        root.configure(bg=BG)
        root.config(cursor="none")

        w, h = root.winfo_screenwidth(), root.winfo_screenheight()

        # ---- Vollbild, und zwar hartnäckig ----
        # Unter Wayland/XWayland setzt der Compositor -fullscreen beim
        # Mappen des Fensters gelegentlich zurück, dann steht die Anzeige
        # als Fenster da. Deshalb: Geometrie mitgeben, gleich mehrfach
        # nachsetzen und in tick() dauerhaft überwachen.
        root.geometry(f"{w}x{h}+0+0")
        self._vollbild_setzen()
        for verzoegerung in (200, 800, 2500):
            root.after(verzoegerung, self._vollbild_setzen)

        # Beenden ist bewusst nicht vorgesehen: das Gerät hat keine
        # Tastatur, und ein versehentlich geschlossenes Fenster fällt
        # mitten im Abend niemandem auf. Notausgang für die Werkbank ist
        # Strg-Alt-Q; im Betrieb hilft über SSH:
        #     pkill -f status_display.py
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        root.bind("<Control-Alt-q>", lambda e: root.destroy())
        u = min(w, h)
        # Hochformat (Touch Display 2: 720x1280) hat reichlich Hoehe, das
        # aeltere 7"-Display (800x480) nicht. Danach richtet sich, wie viel
        # Platz Kopfbereich und VU-Anzeige bekommen duerfen.
        self.tall = h >= w * 1.2

        self.f_huge = max(24, int(u * 0.105))
        self.f_big = max(16, int(u * 0.058))
        self.f_mid = max(11, int(u * 0.036))
        # Die beiden kleinen Groessen zusaetzlich an der BREITE deckeln:
        # die Datenzeilen sind Festbreitenschrift und muessen ganz
        # hineinpassen. Faustwert ~0,8 px Zeichenbreite je Punkt.
        self.f_small = max(10, min(int(u * 0.029), int(w / 44)))
        self.f_file = max(9, min(int(u * 0.026), int(w / 50)))
        # Kanalnummern unter den VU-Balken. Bei 32 Kanälen auf 720 px ist
        # ein Balken 22 px breit – die Ziffer muss da hineinpassen.
        self.f_kanal = max(8, min(int(u * 0.019), int(w / 52)))
        pad = int(u * 0.018)
        self.pad = pad
        self.meter_h = max(22, int(u * 0.062))
        self.screen_w = w

        # ---- Kopf ----
        self.state_lbl = tk.Label(root, text="…", bg=BG, fg=FG,
                                  font=("DejaVu Sans", self.f_huge, "bold"))
        self.state_lbl.pack(pady=(pad, 0))

        self.time_lbl = tk.Label(root, text="0:00:00", bg=BG, fg=FG,
                                 font=("DejaVu Sans Mono", self.f_big, "bold"))
        self.time_lbl.pack()

        self.msg_lbl = tk.Label(root, text="", bg=BG, fg=DIM, wraplength=int(w * 0.94),
                                font=("DejaVu Sans", self.f_mid))
        self.msg_lbl.pack(pady=(pad // 3, pad // 3))

        # ---- Ziel und Platz ----
        self.target_lbl = tk.Label(root, text="", bg=BG, fg=DIM, anchor="w",
                                   wraplength=int(w * 0.94),
                                   font=("DejaVu Sans Mono", self.f_small))
        self.target_lbl.pack(fill="x", padx=pad)

        # ---- Rückfrage USB-Stick ----
        self.ask_frame = tk.Frame(root, bg="#1e2a12", padx=pad, pady=pad // 2,
                                  highlightbackground=ACHTUNG,
                                  highlightthickness=2)
        self.ask_lbl = tk.Label(self.ask_frame, text="", bg="#1e2a12", fg=FG,
                                anchor="w", justify="left",
                                wraplength=int(w * 0.90),
                                font=("DejaVu Sans", self.f_small, "bold"))
        self.ask_lbl.pack(fill="x", pady=(0, pad // 3))

        btns = tk.Frame(self.ask_frame, bg="#1e2a12")
        btns.pack(anchor="w")
        self.yes_btn = tk.Label(btns, text="   Ja   ", bg="#15803d", fg="#fff",
                                font=("DejaVu Sans", self.f_small, "bold"),
                                padx=int(u * 0.02), pady=int(u * 0.016))
        self.yes_btn.pack(side="left")
        self.yes_btn.bind("<Button-1>", lambda e: self.answer_usb("yes"))
        tk.Frame(btns, bg="#1e2a12", width=int(u * 0.02)).pack(side="left")
        self.no_btn = tk.Label(btns, text="   Nein   ", bg=BTN_BG, fg=FG,
                               font=("DejaVu Sans", self.f_small, "bold"),
                               padx=int(u * 0.02), pady=int(u * 0.016))
        self.no_btn.pack(side="left")
        self.no_btn.bind("<Button-1>", lambda e: self.answer_usb("no"))

        self._code_soll = ""
        self._code_eingabe = ""
        self._code_aktion = lambda: None
        self._code_bis = 0.0

        self._ask_shown = False
        self._ask_path = ""
        self._head_n = -1
        self._vollbild_t = 0.0
        self._tick_fehler = 0
        self._block_fehler = 0

        # ---- Geräteblöcke ----
        # Angelegt wird der Rahmen hier, gepackt aber erst NACH der
        # Fussleiste: sonst frisst er als einziger Bereich mit expand=True
        # den ganzen Platz und schiebt die Touch-Tasten aus dem Bild.
        self.blocks_frame = tk.Frame(root, bg=BG)
        self.blocks = []          # wächst nach Bedarf, ein Block je Gerät

        # ---- Fussleiste ----
        # Auf schmalen Displays passen Tasten, Uhr und Systemwerte nicht in
        # eine Zeile – dann ueberdecken sie sich gegenseitig. Deshalb dort
        # zwei Zeilen: oben die Werte, unten die Tasten. Die Tasten bleiben
        # so gross, wie sie fuer Finger sein muessen.
        bar = tk.Frame(root, bg=BG)
        bar.pack(side="bottom", fill="x", padx=pad, pady=(pad // 3, pad // 3))

        # Meldungen des Pi selbst (Drosselung, Temperatur). Ganz unten,
        # direkt ueber den Tasten – dort steht alles Geraeteuebergreifende
        # beisammen. Wird nur eingeblendet, wenn es etwas zu melden gibt.
        self.warn_lbl = tk.Label(bar, text="", bg="#3b1212", fg="#fecaca",
                                 anchor="w", justify="left",
                                 wraplength=int(w * 0.92), padx=pad // 2,
                                 pady=pad // 5,
                                 font=("DejaVu Sans", self.f_small, "bold"))
        self._warn_shown = False

        if w < 900:
            infozeile = tk.Frame(bar, bg=BG)
            infozeile.pack(fill="x")
            tastenzeile = tk.Frame(bar, bg=BG)
            tastenzeile.pack(fill="x", pady=(pad // 4, 0))
            self.bar_erste_zeile = infozeile
        else:
            infozeile = tk.Frame(bar, bg=BG)
            infozeile.pack(fill="x")
            tastenzeile = infozeile
            self.bar_erste_zeile = infozeile

        self._rec_confirm_until = 0.0
        self._pwr_confirm_until = 0.0
        self._service_alive = True

        self.rec_btn = tk.Label(tastenzeile, text=" … ", bg=BTN_BG, fg=FG,
                                font=("DejaVu Sans", self.f_small, "bold"),
                                padx=int(u * 0.022), pady=int(u * 0.018))
        self.rec_btn.pack(side="left")
        self.rec_btn.bind("<Button-1>", self.on_rec)

        tk.Frame(tastenzeile, bg=BG, width=int(u * 0.025)).pack(side="left")

        self.power_btn = tk.Label(tastenzeile, text=" Herunterfahren ", bg=BTN_BG, fg=FG,
                                  font=("DejaVu Sans", self.f_small, "bold"),
                                  padx=int(u * 0.022), pady=int(u * 0.018))
        self.power_btn.pack(side="left")
        self.power_btn.bind("<Button-1>", self.on_power)

        self.clock_lbl = tk.Label(infozeile, text="", bg=BG, fg=DIM,
                                  font=("DejaVu Sans Mono", self.f_small))
        self.clock_lbl.pack(side="right")

        # Temperatur und Drosselung: bei sieben Stunden Dauerlast die
        # aussagekraeftigeren Werte. Wird selten abgefragt, das kostet
        # jedes Mal einen Prozessstart.
        self.sys_lbl = tk.Label(infozeile, text="", bg=BG, fg=DIM,
                                font=("DejaVu Sans Mono", self.f_file))
        self.sys_lbl.pack(side="right", padx=(0, int(u * 0.03)))
        self._sys_t = 0.0
        self._ziel_pfad = None

        # Erst jetzt – die Fussleiste hat ihren Platz bereits reserviert.
        self.blocks_frame.pack(fill="both", expand=True, padx=pad,
                               pady=(pad // 3, 0))

        self.code_frame = self._make_code_pad()   # bleibt bis zum Bedarf verborgen

        self.tick()

    # ------------------------------------------------ Aufbau

    def _make_block(self):
        f = tk.Frame(self.blocks_frame, bg=BOX, padx=self.pad // 2, pady=self.pad // 3)
        # wraplength: lieber umbrechen als rechts abschneiden. So bleibt die
        # Anzeige auf jeder Displaygroesse vollstaendig lesbar, ohne dass die
        # Schriftgroesse geraten werden muss.
        wl = self.screen_w - 3 * self.pad
        head = tk.Label(f, text="", bg=BOX, fg=FG, anchor="w", justify="left",
                        wraplength=wl,
                        font=("DejaVu Sans", self.f_small, "bold"))
        head.pack(fill="x")
        rate = tk.Label(f, text="", bg=BOX, fg=FG, anchor="w", justify="left",
                        wraplength=wl,
                        font=("DejaVu Sans Mono", self.f_small, "bold"))
        rate.pack(fill="x")
        level = tk.Label(f, text="", bg=BOX, fg=DIM, anchor="w", justify="left",
                         wraplength=wl,
                         font=("DejaVu Sans Mono", self.f_file))
        level.pack(fill="x")
        meter = tk.Canvas(f, bg=BOX, height=self.meter_h,
                          highlightthickness=0, bd=0)
        # Im Hochformat darf die VU-Anzeige den freien Platz unten
        # ausfuellen – dann sind die Balken auch aus zwei Metern lesbar.
        meter.pack(fill="both", expand=self.tall,
                   pady=(self.pad // 5, self.pad // 5))
        # Nicht benoetigte Zeilen werden spaeter ausgeblendet – ein leeres
        # Label belegt sonst trotzdem seine Zeilenhoehe.
        files = []
        for _ in range(FILE_ROWS):
            lbl = tk.Label(f, text="", bg=BOX, fg=DIM, anchor="w",
                           font=("DejaVu Sans Mono", self.f_file))
            lbl.pack(fill="x")
            files.append(lbl)
        return {"frame": f, "head": head, "rate": rate, "level": level,
                "meter": meter, "files": files, "shown": False,
                "nch": 0, "mw": 0, "mh": 0, "xs": [], "bars": [],
                "holds": [], "cols": [], "hold": [], "hold_t": 0.0}

    # ------------------------------------------------ Vollbild

    def _vollbild_setzen(self):
        """Vollbild erzwingen. Fehler werden geschluckt — die Anzeige darf
        daran nicht sterben, sie ist im Zweifel als Fenster brauchbar."""
        try:
            self.root.attributes("-fullscreen", True)
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass

    def _vollbild_pruefen(self):
        """Einmal je Sekunde nachsehen, ob es noch Vollbild ist."""
        now = time.time()
        if now - self._vollbild_t < 1.0:
            return
        self._vollbild_t = now
        try:
            if not self.root.attributes("-fullscreen"):
                self._vollbild_setzen()
        except tk.TclError:
            pass

    # ------------------------------------------------ VU-Anzeige

    def _db_y(self, db, h):
        """dBFS auf eine Bildschirmzeile umrechnen (unten leise, oben laut)."""
        frac = (db - METER_MIN) / (0.0 - METER_MIN)
        frac = 0.0 if frac < 0.0 else (1.0 if frac > 1.0 else frac)
        return (h - 1) - frac * (h - 2)

    def _meter_size(self, cv):
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 20:
            w = max(240, self.screen_w - 3 * self.pad)
        if h < 12:
            h = self.meter_h
        return w, h

    def _build_meter(self, block, n):
        """Balken einmalig anlegen; danach werden nur noch Koordinaten gesetzt.

        Die Kanalnummern werden IN die Zeichenflaeche gesetzt, nicht in ein
        eigenes Label darunter: nur so stehen sie zwangslaeufig genau unter
        ihrem Balken. Ein Label mit Festbreitenschrift wuerde bei krummen
        Kanalzahlen auseinanderlaufen.
        """
        cv = block["meter"]
        cv.delete("all")
        w, h = self._meter_size(cv)
        # Unterer Streifen fuer die Ziffern; der Rest gehoert den Balken.
        lab_h = self.f_kanal + 6
        bar_h = max(12, h - lab_h)
        gap = 1.0 if n > 20 else 2.0
        slot = w / float(n)

        xs = []
        for i in range(n):
            x0 = i * slot + gap / 2.0
            x1 = (i + 1) * slot - gap / 2.0
            if x1 - x0 < 1.0:
                x1 = x0 + 1.0
            xs.append((x0, x1))
            cv.create_rectangle(x0, 0, x1, bar_h, fill=TRACK, outline="")

        # Skalenlinien liegen ueber dem dunklen Untergrund, aber unter den
        # Balken – so bleibt die Skala im unbeleuchteten Bereich sichtbar.
        for db in HOLD_GRID:
            y = self._db_y(db, bar_h)
            cv.create_line(0, y, w, y,
                           fill=GITTER_HELL if db >= -6.0 else GITTER_DUNKEL)

        bars = [cv.create_rectangle(x0, bar_h, x1, bar_h, fill=GUT,
                                    outline="") for x0, x1 in xs]
        holds = [cv.create_line(x0, bar_h, x1, bar_h, fill=HOLD_FG, width=2)
                 for x0, x1 in xs]

        # Kanalnummern: je Balken die letzte Ziffer. Kanal 1 und jeder
        # Zehner stehen hell und fett – das sind die Anker zum Abzaehlen,
        # den Rest ergaenzt das Auge von selbst.
        ymid = bar_h + lab_h / 2.0
        for i, (x0, x1) in enumerate(xs):
            nr = i + 1
            marke = nr == 1 or nr % 10 == 0
            cv.create_text((x0 + x1) / 2.0, ymid, text=str(nr % 10),
                           fill=FG if marke else KANAL_NR,
                           font=("DejaVu Sans", self.f_kanal,
                                 "bold" if marke else "normal"))

        block.update(nch=n, mw=w, mh=h, mbar=bar_h, xs=xs, bars=bars,
                     holds=holds, cols=[""] * n, hold=[METER_MIN] * n,
                     hold_t=time.time())

    def _clear_meter(self, block):
        if block["nch"]:
            block["meter"].delete("all")
            block.update(nch=0, mw=0, mh=0, mbar=0, xs=[], bars=[], holds=[],
                         cols=[], hold=[])

    def draw_meter(self, block, dev):
        cv = block["meter"]
        dbs = dev.get("channel_dbfs") or []
        # Waehrend der USB-Rueckfrage weicht die VU-Anzeige – sonst schiebt
        # der Kasten den Block auf kleinen Displays aus dem Bild. Die
        # Pegelzeile darueber bleibt stehen, die Information geht nicht weg.
        if self._ask_shown and not self.tall:
            if block["nch"]:
                self._clear_meter(block)
                cv.pack_forget()
            return
        if not cv.winfo_ismapped():
            opts = {"fill": "both", "expand": True,
                    "pady": (self.pad // 5, self.pad // 5)}
            # vor der ersten sichtbaren Dateizeile einordnen; sind alle
            # ausgeblendet, kommt die Anzeige einfach ans Ende
            first = next((l for l in block["files"] if l.winfo_ismapped()), None)
            if first is not None:
                opts["before"] = first
            cv.pack(**opts)
        if dev.get("state") != "RECORDING" or not dbs:
            self._clear_meter(block)
            return

        n = len(dbs)
        w, h = self._meter_size(cv)
        if (n != block["nch"] or abs(w - block["mw"]) > 4
                or abs(h - block["mh"]) > 2):
            self._build_meter(block, n)
        # Die Balken enden ueber dem Ziffernstreifen, nicht am Rand.
        bar_h = block["mbar"]

        now = time.time()
        dt = now - block["hold_t"]
        block["hold_t"] = now
        decay = HOLD_FALL * dt if 0.0 < dt < 5.0 else 0.0

        hold, xs, bars, holds, cols = (block["hold"], block["xs"],
                                       block["bars"], block["holds"],
                                       block["cols"])
        for i in range(n):
            db = dbs[i]
            x0, x1 = xs[i]
            y = self._db_y(db, bar_h)
            cv.coords(bars[i], x0, y, x1, bar_h)

            if db >= METER_CLIP:
                col = SCHLECHT
            elif db >= METER_WARN:
                col = ACHTUNG
            elif db > METER_MIN:
                col = GUT
            else:
                col = TRACK
            if cols[i] != col:
                cv.itemconfig(bars[i], fill=col)
                cols[i] = col

            hv = db if db > hold[i] - decay else hold[i] - decay
            if hv < METER_MIN:
                hv = METER_MIN
            hold[i] = hv
            yh = self._db_y(hv, bar_h) if hv > METER_MIN else bar_h + 4
            cv.coords(holds[i], x0, yh, x1, yh)

    # ------------------------------------------------ Touch

    @staticmethod
    def _run(cmd):
        try:
            subprocess.Popen(cmd)
            return True
        except OSError:
            return False

    def update_rec_btn(self, state):
        alive = state not in ("STOPPED", "STALE")
        self._service_alive = alive
        # Waehrend des Schliessens ist Warten die einzige richtige Handlung.
        if state == "STOPPING":
            self._rec_confirm_until = 0.0
            self.rec_btn.config(text=" Schließt … bitte warten ",
                                bg=SCHLECHT, fg="#fff")
            return
        if time.time() < self._rec_confirm_until:
            return
        if alive:
            self.rec_btn.config(text=" Aufnahme stoppen ", bg=BTN_BG, fg=FG)
        else:
            self.rec_btn.config(text=" Aufnahme starten ", bg="#14532d", fg="#dcfce7")

    def wirklich_stoppen(self):
        self._rec_confirm_until = 0.0
        self.rec_btn.config(text=" Stoppt … ", bg=SCHLECHT, fg="#fff")
        self._run(["sudo", "systemctl", "stop", "audiorec"])

    def on_rec(self, _event=None):
        if not self._service_alive:
            self.rec_btn.config(text=" Startet … ", bg=COLORS["STARTING"], fg="#04202e")
            self._run(["sudo", "systemctl", "start", "audiorec"])
            return
        code = self.code_aus_konfig("stop_code")
        if code:
            self.code_oeffnen("Aufnahme stoppen", code, self.wirklich_stoppen)
            return
        if time.time() < self._rec_confirm_until:
            self.wirklich_stoppen()
            return
        self._rec_confirm_until = time.time() + CONFIRM_S
        self.rec_btn.config(text=" Wirklich stoppen? ", bg=SCHLECHT, fg="#fff")

    def answer_usb(self, value):
        """Antwort für den Dienst hinterlegen."""
        try:
            ANSWER_PATH.parent.mkdir(parents=True, exist_ok=True)
            ANSWER_PATH.write_text(json.dumps(
                {"usb": value, "path": self._ask_path, "ts": time.time()}))
        except OSError as e:
            self.ask_lbl.config(text=f"Antwort nicht speicherbar: {e}")
            return
        self.ask_lbl.config(text="Übernommen …")
        self.hide_ask()

    def show_ask(self, path, recording):
        text = f"USB-Stick gefunden:  {path}\n\nAls Ziel für die Aufnahmen verwenden?"
        if recording:
            text += ("\n\n⚠  Dafür wird die laufende Aufnahme kurz gestoppt "
                     "und auf dem Stick neu gestartet.")
        self.ask_lbl.config(text=text)
        self._ask_path = path
        if not self._ask_shown:
            self.ask_frame.pack(fill="x", padx=self.pad, pady=(self.pad // 3, 0),
                                before=self.blocks_frame)
            self._ask_shown = True

    def hide_ask(self):
        if self._ask_shown:
            self.ask_frame.pack_forget()
            self._ask_shown = False

    def reset_power_btn(self):
        self._pwr_confirm_until = 0.0
        self.power_btn.config(text=" Herunterfahren ", bg=BTN_BG, fg=FG)

    def on_power(self, _event=None):
        code = self.code_aus_konfig("shutdown_code")
        if code:
            self.code_oeffnen("Herunterfahren", code,
                              self.wirklich_herunterfahren)
            return
        if time.time() < self._pwr_confirm_until:
            self.wirklich_herunterfahren()
            return
        self._pwr_confirm_until = time.time() + CONFIRM_S
        self.power_btn.config(text=" Wirklich? Nochmal tippen ",
                              bg=SCHLECHT, fg="#fff")

    def wirklich_herunterfahren(self):
        self.power_btn.config(text=" Fährt herunter … ", bg=SCHLECHT, fg="#fff")
        self.root.update_idletasks()
        self._run(["sudo", "shutdown", "-h", "now"])

    # ------------------------------------------------ Zifferneingabe

    @staticmethod
    def code_aus_konfig(schluessel):
        """Zugangscode frisch aus der Konfiguration holen.

        Bewusst bei jedem Tastendruck statt einmal beim Start: so wirkt
        eine Aenderung in der Konfiguration sofort, ohne dass die Anzeige
        neu gestartet werden muss. Leer = keine Abfrage.

        Kein Sicherheitsmerkmal — der Code steht im Klartext in der Datei.
        Er schuetzt gegen Versehen und neugierige Finger.
        """
        try:
            c = configparser.ConfigParser(interpolation=None)
            if not c.read(CONFIG_PATH):
                return ""
            return c.get("display", schluessel, fallback="").strip()
        except (configparser.Error, OSError, UnicodeDecodeError):
            return ""

    def _make_code_pad(self):
        """Vollflächiges Tastenfeld. Liegt per place() über allem anderen,
        damit das übrige Layout unberührt bleibt."""
        u = min(self.screen_w, self.root.winfo_screenheight())
        f = tk.Frame(self.root, bg="#08080b")

        self.code_titel = tk.Label(f, text="", bg="#08080b", fg=FG,
                                   font=("DejaVu Sans", self.f_big, "bold"))
        self.code_titel.pack(pady=(int(u * 0.05), 0))

        self.code_dots = tk.Label(f, text="", bg="#08080b", fg=ACHTUNG,
                                  font=("DejaVu Sans Mono", self.f_big, "bold"))
        self.code_dots.pack(pady=(int(u * 0.02), 0))

        self.code_msg = tk.Label(f, text="", bg="#08080b", fg=DIM,
                                 font=("DejaVu Sans", self.f_small))
        self.code_msg.pack(pady=(int(u * 0.01), int(u * 0.03)))

        raster = tk.Frame(f, bg="#08080b")
        raster.pack(expand=True)   # senkrecht mittig, gut erreichbar
        tasten = [("1", 0, 0), ("2", 0, 1), ("3", 0, 2),
                  ("4", 1, 0), ("5", 1, 1), ("6", 1, 2),
                  ("7", 2, 0), ("8", 2, 1), ("9", 2, 2),
                  ("C", 3, 0), ("0", 3, 1), ("×", 3, 2)]
        gross = max(20, int(u * 0.055))
        for text, zeile, spalte in tasten:
            farbe = {"C": "#3f3f46", "×": "#7f1d1d"}.get(text, BTN_BG)
            b = tk.Label(raster, text=text, bg=farbe, fg=FG, width=3,
                         font=("DejaVu Sans", gross, "bold"),
                         padx=int(u * 0.012), pady=int(u * 0.012))
            b.grid(row=zeile, column=spalte, padx=int(u * 0.012),
                   pady=int(u * 0.012), sticky="nsew")
            b.bind("<Button-1>", lambda e, t=text: self.code_taste(t))
        return f

    def code_oeffnen(self, titel, code, aktion):
        self._code_soll = str(code)
        self._code_aktion = aktion
        self._code_eingabe = ""
        self._code_bis = time.time() + CODE_TIMEOUT_S
        self.code_titel.config(text=titel)
        self.code_msg.config(text=f"Code eingeben ({len(self._code_soll)} Ziffern)",
                             fg=DIM)
        self.code_punkte()
        self.code_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.code_frame.lift()

    def code_schliessen(self):
        self._code_soll = ""
        self._code_eingabe = ""
        self._code_bis = 0.0
        self.code_frame.place_forget()
        self.reset_power_btn()

    def code_punkte(self):
        offen = len(self._code_soll) - len(self._code_eingabe)
        self.code_dots.config(text="  ".join(["●"] * len(self._code_eingabe)
                                             + ["○"] * max(0, offen)))

    def code_taste(self, t):
        self._code_bis = time.time() + CODE_TIMEOUT_S
        if t == "×":
            self.code_schliessen()
            return
        if t == "C":
            self._code_eingabe = ""
            self.code_msg.config(text="Code eingeben", fg=DIM)
            self.code_punkte()
            return
        if len(self._code_eingabe) >= len(self._code_soll):
            return
        self._code_eingabe += t
        self.code_punkte()
        if len(self._code_eingabe) < len(self._code_soll):
            return
        if self._code_eingabe == self._code_soll:
            self.code_msg.config(text="Code richtig …", fg=GUT)
            self.root.update_idletasks()
            aktion = self._code_aktion
            self.code_schliessen()
            aktion()
        else:
            self._code_eingabe = ""
            self.code_msg.config(text="Falscher Code", fg=SCHLECHT)
            self.code_punkte()

    # ------------------------------------------------ Daten

    def read_status(self):
        try:
            data = json.loads(STATUS_PATH.read_text())
        except (OSError, ValueError):
            return None
        if (data.get("state") != "STOPPED"
                and time.time() - data.get("updated", 0) > STALE_AFTER_S):
            data["state"] = "STALE"
            data["message"] = "Dienst antwortet nicht – läuft audiorec.service?"
        return data

    def ensure_blocks(self, count):
        """So viele Blöcke bereitstellen, wie Geräte da sind."""
        while len(self.blocks) < count:
            self.blocks.append(self._make_block())

    def fit_head(self, n):
        """Kopfbereich an die Zahl der Geräte anpassen.

        Bei einem Gerät darf der Zustand gross dastehen. Sobald zwei Pulte
        laufen, wird der Platz unten gebraucht – sonst rutschen VU-Anzeige
        und Touch-Tasten aus dem Bild. Betrifft besonders das alte
        7"-Display mit 800x480; im Hochformat 720x1280 ist Platz genug.
        """
        if n == self._head_n:
            return
        self._head_n = n
        big = (n <= 1) or self.tall
        self.state_lbl.config(font=("DejaVu Sans",
                                    self.f_huge if big else self.f_big, "bold"))
        self.time_lbl.config(font=("DejaVu Sans Mono",
                                   self.f_big if big else self.f_mid, "bold"))
        self.msg_lbl.config(font=("DejaVu Sans",
                                  self.f_mid if big else self.f_small))

    def render_block(self, block, dev, rows=FILE_ROWS):
        # Freien Platz bekommt immer die VU-Anzeige. Ist keiner da, aendert
        # expand nichts – Tk verteilt nur, was uebrig bleibt.
        block["frame"].pack(fill="both", expand=True, pady=(0, self.pad // 3))
        block["meter"].pack_configure(expand=True)
        block["shown"] = True

        ch, rate, fmt = dev.get("channels", 0), dev.get("rate", 0), dev.get("format", "")
        head = f"{dev.get('card_name', '?')}   {dev.get('device', '')}"
        if ch:
            head += f"   {ch} Kan · {de(rate / 1000, 1)} kHz · {fmt}"
        # Wie lang ein Stück wird. Steht bewusst hier oben bei den übrigen
        # Aufnahmedaten: der Dienst kürzt diesen Wert selbst, wenn die
        # Datei sonst über die 2-GiB-Grenze von arecord liefe. Weicht er
        # vom eingestellten ab, steht der eingestellte in Klammern dahinter
        # — dann sieht man auf einen Blick, dass gekürzt wurde.
        mft = dev.get("max_file_time_s", 0)
        if mft:
            wunsch = dev.get("max_file_time_wunsch", 0)
            head += (f" · {mft} s/Datei"
                     if not wunsch or wunsch == mft
                     else f" · {mft} s/Datei (statt {wunsch})")
        block["head"].config(text=head)

        if dev.get("state") == "RECORDING":
            r, exp = dev.get("rate_mb_s", 0.0), dev.get("expected_mb_s", 0.0)
            ok = exp <= 0 or r >= exp * 0.85
            txt = f"{de(r)}/{de(exp)} MB/s"
            if not ok:
                txt += " ZU NIEDRIG"
            txt += (f" · {dev.get('files', 0)} Dateien"
                    f" · {de(dev.get('bytes', 0) / 1e9)} GB"
                    f" · {hms(dev.get('elapsed_s', 0))}")
            neu = dev.get("restarts", 0)
            if neu:
                txt += f"  ⚠ {neu}× neu gestartet"
            # Aussetzer meldet arecord selbst ("overrun"). Sie kosten
            # Bruchteile einer Sekunde, hoerbar als Knacken – aber sie
            # sind der Vorbote eines Datentraegers, der nicht mitkommt.
            aus = dev.get("xruns", 0)
            if aus:
                txt += f"  ⚠ {aus}× Aussetzer"
            block["rate"].config(
                text=txt,
                fg=SCHLECHT if not ok else (ACHTUNG if (neu or aus) else GUT))
        else:
            block["rate"].config(text=dev.get("last_error", "kein Signal"),
                                 fg=SCHLECHT)

        # Pegel – der Beleg, dass echtes Audio ankommt und nicht nur Nullen
        peak, active = dev.get("peak_dbfs"), dev.get("active_channels")
        if dev.get("state") == "RECORDING" and peak is not None:
            mx, clips = dev.get("max_dbfs"), dev.get("clip_windows", 0)
            win_min = max(1, int(dev.get("max_window_s", 900) / 60))
            # Die Zeile muss in EINE Zeile passen, sonst bricht sie um und
            # verschiebt alles darunter. Deshalb entfallen bei einem Zusatz
            # ("STILLE", "3× Anschlag") die Werte, die dann ohnehin nichts
            # mehr aussagen: bei Stille sind Maximum und Mittel bedeutungslos,
            # bei Übersteuerung ist das Mittel uninteressant.
            avg = dev.get("avg_dbfs")
            if active == 0:
                txt = f"Pegel {de(peak, 1)} dBFS · {active}/{ch} aktiv · STILLE"
                col = SCHLECHT
            elif clips:
                txt = f"Pegel {de(peak, 1)}"
                if mx is not None:
                    txt += f" · max{win_min} {de(mx, 1)}"
                txt += f" dBFS · {active}/{ch} · {clips}× Anschlag"
                col = ACHTUNG
            else:
                txt = f"Pegel {de(peak, 1)}"
                if mx is not None:
                    txt += f" · max/{win_min}min {de(mx, 1)}"
                if avg is not None:
                    txt += f" · Ø {de(avg, 1)}"
                txt += f" dBFS · {active}/{ch} aktiv"
                col = GUT
            block["level"].config(text=txt, fg=col)
        else:
            block["level"].config(text="", fg=DIM)

        self.draw_meter(block, dev)

        recent = dev.get("recent_files", [])[-rows:] if rows > 0 else []
        for i, lbl in enumerate(block["files"]):
            if i < rows:
                if not lbl.winfo_ismapped():
                    lbl.pack(fill="x")
            elif lbl.winfo_ismapped():
                lbl.pack_forget()
            if i < len(recent):
                f = recent[i]
                size = de(f.get("bytes", 0) / 1e9)
                if f.get("active"):
                    lbl.config(text=f"● {f['name']}   {size} GB", fg=GUT)
                else:
                    lbl.config(text=f"  {f['name']}   {size} GB", fg=DIM)
            else:
                lbl.config(text="")

    def hide_block(self, block):
        if block["shown"]:
            block["frame"].pack_forget()
            block["shown"] = False
        self._clear_meter(block)

    # ------------------------------------------------ Schleife

    def sys_pruefen(self, d=None):
        """Systemwerte anzeigen.

        Gemessen wird im Dienst – er liefert sie in status.json unter
        "system" mit. Nur wenn dort nichts steht (aeltere Dienstversion),
        misst die Anzeige selbst.
        """
        if d is None:
            d = {}
        if not d:
            if piwatch is None or time.time() - self._sys_t < 5.0:
                return
            self._sys_t = time.time()
            d = piwatch.alles(max_alter_s=0, ziel=self._ziel_pfad)
            d["warnung"] = piwatch.kritisch(d)
            d["vorwarnung"] = piwatch.vorwarnung(d)
        krit = bool(d.get("warnung"))
        vor = bool(d.get("vorwarnung"))

        # In der Fussleiste ist nur Platz fuer das Nötigste
        teile = []
        if d.get("temp_c") is not None:
            teile.append(f"{d['temp_c']:.0f}°C")
        if d.get("cpu") is not None:
            teile.append(f"CPU {d['cpu']:.0f}%")
        if d.get("dirty_mb") is not None:
            teile.append(f"Puffer {d['dirty_mb']:.0f}MB")
        if d.get("disk") and d.get("disk_last") is not None:
            teile.append(f"{d['disk']} {d['disk_last']:.0f}%")
        if d.get("watt") is not None and self.screen_w >= 900:
            teile.append(f"{d['watt']:.1f}W")
        kurz = " · ".join(teile)
        if krit:
            kurz = ("⚠ " + kurz) if kurz else "⚠"
        self.sys_lbl.config(
            text=kurz,
            fg=SCHLECHT if krit else (ACHTUNG if vor else HELL))

        # Der Klartext bekommt eine eigene Zeile – aber nur, wenn es
        # etwas zu sagen gibt.
        if d.get("fehler"):
            text = f"Systemwerte nicht messbar: {d['fehler']}"
        elif d.get("throttle_jetzt"):
            text = "Pi meldet JETZT: " + ", ".join(d["throttle_jetzt"])
        elif d.get("throttle_frueher"):
            text = ("Pi hat seit dem Einschalten gemeldet: "
                    + ", ".join(d["throttle_frueher"]))
        elif d.get("dirty_mb") is not None and d["dirty_mb"] >= DIRTY_WARN_MB:
            text = (f"Schreibstau: {d['dirty_mb']:.0f} MB warten auf den "
                    f"Datenträger — er kommt nicht hinterher")
        elif d.get("cpu") is not None and d["cpu"] >= CPU_WARN:
            text = f"CPU am Anschlag: {d['cpu']:.0f} %"
        elif d.get("temp_c") is not None and d["temp_c"] >= TEMP_WARN:
            text = (f"Pi wird warm: {d['temp_c']:.0f} °C — ab "
                    f"{TEMP_KRITISCH:.0f} °C drosselt er")
        else:
            text = ""
        if text:
            if not self._warn_shown:
                self.warn_lbl.pack(fill="x", pady=(0, self.pad // 4),
                                   before=self.bar_erste_zeile)
                self._warn_shown = True
            self.warn_lbl.config(text=text)
        elif self._warn_shown:
            self.warn_lbl.pack_forget()
            self._warn_shown = False

    def tick(self):
        """Aussenhuelle des Anzeigetakts.

        Der eigentliche Aufbau steckt in _tick(). Hier steht nur der
        Schutzwall darum — und der ist wichtiger, als er aussieht:

        Wirft eine Tk-Rueckrufmethode eine Ausnahme, gibt Tkinter sie auf
        der Fehlerausgabe aus und ruft die Methode NICHT WIEDER AUF. Die
        Anzeige bliebe dann stehen — mit dem letzten gezeichneten Bild:
        AUFNAHME, eine mitlaufend aussehende Zeit, plausible Pegel. Genau
        das Bild, bei dem niemand nachsieht.

        Deshalb: der naechste Takt wird IMMER eingeplant, und ein Fehler
        wird sichtbar gemacht statt verschluckt.
        """
        try:
            self._tick()
            self._tick_fehler = 0
        except Exception as e:                     # noqa: BLE001
            self._tick_fehler = getattr(self, "_tick_fehler", 0) + 1
            if self._tick_fehler <= 3:
                traceback.print_exc()
            try:
                self.state_lbl.config(text="ANZEIGE", fg="#ffffff",
                                      bg=STATE_BG["ERROR"])
                self.msg_lbl.config(
                    text=f"Fehler in der Anzeige ({type(e).__name__}: {e}). "
                         "Der Dienst läuft davon unabhängig weiter — "
                         "über SSH prüfen mit:  audiorec-status")
            except tk.TclError:
                pass
        finally:
            try:
                self.root.after(REFRESH_MS, self.tick)
            except tk.TclError:
                pass

    def _tick(self):
        self._vollbild_pruefen()
        if self._code_bis and time.time() > self._code_bis:
            self.code_schliessen()
        self.clock_lbl.config(text=time.strftime(
            "%d.%m.%Y  %H:%M:%S" if self.screen_w >= 900 else "%H:%M:%S"))
        if self._pwr_confirm_until and time.time() > self._pwr_confirm_until:
            self.reset_power_btn()
        if self._rec_confirm_until and time.time() > self._rec_confirm_until:
            self._rec_confirm_until = 0.0

        d = self.read_status()
        if d is None:
            self.state_lbl.config(text="KEIN STATUS", fg="#ffffff",
                                  bg=STATE_BG["ERROR"])
            self.msg_lbl.config(text=f"{STATUS_PATH} nicht lesbar")
            self.update_rec_btn("STALE")
            self.hide_ask()
            for b in self.blocks:
                self.hide_block(b)
            return

        # Uhr gelb, solange die Zeit nicht per Netz gestellt ist – die
        # Dateinamen sind Zeitstempel, ein falsches Datum faellt sonst
        # erst beim Sichten auf.
        self.clock_lbl.config(
            fg=HELL if d.get("clock_synced", True) else ACHTUNG)

        self.sys_pruefen(d.get("system") or {})

        state = d.get("state", "STOPPED")
        self.update_rec_btn(state)
        self.state_lbl.config(text=LABELS.get(state, state),
                              fg=COLORS.get(state, DIM),
                              bg=STATE_BG.get(state, BG))

        devices = d.get("devices", [])
        elapsed = max((dev.get("elapsed_s", 0) for dev in devices), default=0)
        self.time_lbl.config(text=hms(elapsed),
                             fg=FG if state == "RECORDING" else DIM)
        self.msg_lbl.config(text=d.get("message", ""))

        # Ziel, Platz, Restlaufzeit
        free, total = d.get("disk_free_gb", 0.0), d.get("disk_total_gb", 0.0)
        left = d.get("remaining_h", 0.0)
        target = d.get("target", "")
        self._ziel_pfad = target or None
        line = f"Ziel  {target}"
        if d.get("target_is_usb"):
            line += "  [USB]"
        line += f"      {free:.0f} von {total:.0f} GB frei"
        if left > 0:
            # Neben der Dauer die Uhrzeit: "reicht 9:24 h" muss man
            # umrechnen, "voll um 04:57" nicht.
            voll = time.localtime(time.time() + left * 3600)
            line += (f"      reicht {int(left)}:{int((left % 1) * 60):02d} h"
                     f" · voll um {time.strftime('%H:%M', voll)}")
        low = (total > 0 and free / total < 0.12) or (0 < left < 2)
        self.target_lbl.config(text=line,
                               fg=SCHLECHT if low else
                               (COLORS["STARTING"] if d.get("target_is_usb") else DIM))

        candidate = d.get("usb_candidate", "")
        if candidate:
            self.show_ask(candidate, d.get("recording_active", False))
        else:
            self.hide_ask()

        # Bei mehreren Geräten die Dateiliste kürzen, damit VU-Anzeige und
        # Touch-Tasten trotzdem auf den Bildschirm passen. Im Hochformat
        # ist Hoehe genug da – dort bleiben alle drei Zeilen stehen, also
        # eine halbe Stunde Verlauf bei zehnminuetigen Dateien.
        n = len(devices)
        if self.tall:
            rows = FILE_ROWS if n <= 2 else (2 if n == 3 else 1)
        else:
            rows = 2 if n <= 1 else (1 if n == 2 else 0)
        self.fit_head(n)
        self.ensure_blocks(n)
        # Je Gerät einzeln abgesichert: ein unerwarteter Wert in einem Block
        # darf nicht die Anzeige des anderen Pults mitreissen.
        for i, block in enumerate(self.blocks):
            if i >= len(devices):
                self.hide_block(block)
                continue
            try:
                self.render_block(block, devices[i], rows)
            except Exception as e:                 # noqa: BLE001
                # Eigener Zaehler: _tick_fehler wird bei jedem geglueckten
                # Takt zurueckgesetzt, sonst stuende der Fehlerbericht
                # viermal je Sekunde im Journal.
                self._block_fehler += 1
                if self._block_fehler <= 3:
                    traceback.print_exc()
                block["head"].config(
                    text=f"{devices[i].get('card_name', '?')} — "
                         f"Anzeige gestört ({type(e).__name__})", fg=SCHLECHT)


def main():
    root = tk.Tk()
    Display(root)
    root.mainloop()


if __name__ == "__main__":
    main()
