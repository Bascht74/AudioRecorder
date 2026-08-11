#!/usr/bin/env python3
"""
audiorec — automatischer Mehrspur-Recorder für Raspberry Pi 5

Erkennt angeschlossene USB-Audio-Interfaces, startet für JEDES eine eigene
Aufnahme und schreibt laufend einen Status nach /run/audiorec/status.json,
den das Display-Programm anzeigt.

Beliebig viele Geräte gleichzeitig sind vorgesehen: jedes bekommt einen
eigenen Ordner und einen eigenen arecord-Prozess und läuft unabhängig
weiter, wenn ein anderes ausfällt.

Aufnahme-Engine ist arecord aus alsa-utils — bewusst gewählt, weil es
weniger bewegliche Teile hat als ffmpeg und Dateisplitting nativ kann.

Konfiguration: /etc/audiorec/audiorec.conf
"""

import array
import collections
import configparser
import json
import math
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import piwatch
except ImportError:
    piwatch = None

CONFIG_PATH = os.environ.get("AUDIOREC_CONFIG", "/etc/audiorec/audiorec.conf")
STATUS_DIR = Path("/run/audiorec")
STATUS_PATH = STATUS_DIR / "status.json"
# Das Display legt hier seine Antwort auf die USB-Rueckfrage ab
INPUT_DIR = STATUS_DIR / "input"
ANSWER_PATH = INPUT_DIR / "answer.json"

# systemd-timesyncd legt diese Datei an, sobald die Uhr per Netz gestellt
# wurde. Der Pi 5 hat zwar eine RTC, behaelt die Zeit ohne Knopfzelle am
# J5-Anschluss aber nicht ueber das Ausschalten hinweg – und die
# Dateinamen sind Zeitstempel.
TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")

# Wie viele der zuletzt geschriebenen Dateien je Gerät im Status stehen
RECENT_LIMIT = 4

# Bytes pro Sample je ALSA-Format
FORMAT_BYTES = {"S16_LE": 2, "S24_3LE": 3, "S24_LE": 4, "S32_LE": 4}

# Vollausschlag je Format. S24_LE belegt 4 Byte, nutzt davon aber nur
# 24 Bit – der Vollausschlag ist deshalb 2^23, nicht 2^31.
FULL_SCALE_FMT = {"S16_LE": 2 ** 15, "S24_3LE": 2 ** 23,
                  "S24_LE": 2 ** 23, "S32_LE": 2 ** 31}

# Typcode fuer 32-Bit-Ganzzahlen – auf ARM64 ist das "i", sicherheitshalber
# wird die Breite geprueft.
I32 = "i" if array.array("i").itemsize == 4 else "l"

WAV_HEADER = 44            # Standard-RIFF-Kopf, den arecord schreibt
PEAK_FRAMES = 2000         # Fenster fuer die Pegelmessung (~42 ms bei 48 kHz)
SILENCE_DBFS = -60.0       # darunter gilt ein Kanal als still
FLOOR_DBFS = -99.0         # Anzeigewert fuer "nur Nullen"
CLIP_DBFS = -0.1           # ab hier gilt ein Messfenster als "am Anschlag"

# ---------------------------------------------------------------------------
# Groesste WAV-Datei, die arecord schreibt.
#
# NICHT 4 GiB, wie man beim RIFF-Format erwarten wuerde: alsa-utils traegt
# in seiner Tabelle fmt_rec_table fuer WAVE 2147483648LL ein, also 2 GiB.
# Nachgelesen in aplay/aplay.c des alsa-utils-Quelltextes.
#
# Bei 32 Kanaelen ist die Grenze deutlich vor den konfigurierten 600 s
# erreicht:  S24_3LE nach 7:46 min,  S32_LE nach 5:49 min. Deshalb rechnet
# der Dienst die Dateilaenge beim Start selbst nach und kuerzt sie, wenn
# noetig. 1,8 GiB laesst genug Luft, dass die Grenze nie erreicht wird –
# und damit ist auch die Frage vom Tisch, wie arecord an der Grenze
# reagiert (sauberer Dateiwechsel oder Abbruch), die sich aus dem
# Quelltext nicht zweifelsfrei beantworten liess.
# ---------------------------------------------------------------------------
SAFE_WAV_BYTES = 1932735283        # 1,8 GiB = 90 % der harten Grenze

# Harte Reserve auf dem Zieldatentraeger. min_free_gb aus der Konfiguration
# ist ein Planwert; hier geht es darum, ob ueberhaupt noch Platz ist.
#
# Unterhalb dieser Grenze werden laufende Aufnahmen GEORDNET BEENDET,
# statt sie in den vollen Datentraeger laufen zu lassen. Der Unterschied:
# beim geordneten Beenden schliesst arecord den WAV-Kopf mit der richtigen
# Laenge. Laeuft die Platte dagegen wirklich voll, bekommt arecord beim
# Schreiben einen Fehler und die letzte Datei behaelt den vorlaeufigen
# Kopf – im schlimmsten Fall ist sie gar nicht mehr lesbar. Ein sauber
# geschlossenes Stueck, das zwei Minuten frueher endet, ist allemal
# besser als eine kaputte letzte Datei.
HARD_MIN_FREE_GB = 1.0

# So oft hintereinander muss "voll" gemessen werden, bevor tatsaechlich
# gestoppt wird. Bei poll_interval_s = 0,5 sind das rund drei Sekunden.
#
# Der Grund: shutil.disk_usage kann einmal fehlschlagen oder einen
# unsinnigen Wert liefern. Ein einzelner solcher Ausrutscher darf keine
# laufende Aufnahme beenden – das waere schlimmer als das Problem.
VOLL_BESTAETIGUNGEN = 6

running = True


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def handle_signal(signum, _frame):
    global running
    log(f"Signal {signum} empfangen – beende sauber.")
    running = False


def safe_name(text):
    """Kartenname, der als Ordnername taugt."""
    return re.sub(r"[^A-Za-z0-9_-]+", "", text) or "audio"


def gb(wert):
    """GB-Angabe, die auch kleine Reste noch zeigt ("0 GB" waere hier
    genau die Auskunft, auf die es ankommt – und sie waere falsch)."""
    if wert is None:
        return "?"
    return f"{wert:.2f}" if wert < 10 else f"{wert:.0f}"


# ---------------------------------------------------------------------------


class SysMonitor:
    """Misst Temperatur, Takt, CPU und Schreibpuffer in einem eigenen Faden.

    Bewusst nicht in der Hauptschleife: vcgencmd ist ein eigener Prozess
    und kann im ungluecklichen Fall Sekunden brauchen. Die Schleife
    schreibt aber alle 0,5 s den Status fuer die VU-Anzeige – die darf
    davon nichts merken.
    """

    def __init__(self, intervall=5.0):
        self.intervall = intervall
        self._wert = {}
        self._ziel = None
        self._lock = threading.Lock()
        self._faden = None

    def start(self):
        if piwatch is None or self._faden:
            return
        self._faden = threading.Thread(target=self._schleife, daemon=True)
        self._faden.start()

    def ziel_setzen(self, pfad):
        with self._lock:
            self._ziel = str(pfad) if pfad else None

    def _schleife(self):
        # Bezugspunkte setzen, bevor der erste Wert veroeffentlicht wird:
        # die CPU-Auslastung braucht zwei Messpunkte, und das Zielmedium
        # steht erst fest, wenn die Hauptschleife einmal durch ist.
        try:
            piwatch.cpu_prozent()
        except Exception:            # noqa: BLE001
            pass
        time.sleep(1.5)

        while running:
            with self._lock:
                ziel = self._ziel
            try:
                d = piwatch.alles(max_alter_s=0, ziel=ziel)
                d["warnung"] = piwatch.kritisch(d)
                d["vorwarnung"] = piwatch.vorwarnung(d)
                d["klartext"] = piwatch.zeile(d)
            except Exception as e:            # noqa: BLE001
                d = {"fehler": str(e)}
            with self._lock:
                self._wert = d
            time.sleep(self.intervall)

    def snapshot(self):
        with self._lock:
            return dict(self._wert)


class DeviceSession:
    """Eine Aufnahme für genau ein Gerät."""

    def __init__(self, cfg, idx, card_name):
        self.cfg = cfg
        self.idx = idx
        self.card_name = card_name
        self.device = f"hw:{idx},0"
        self.proc = None
        self.session_dir = None
        self.started_at = None
        self.params = {}
        self.last_error = ""
        self._last_bytes = 0
        self._last_bytes_t = 0.0
        self._rate_mb_s = 0.0
        self._last_total = 0
        self._last_growth_t = 0.0
        self._levels = {}
        self._last_level_t = 0.0
        # Meldungen von arecord. Werden staendig weggelesen – siehe
        # _drain_starten(). Ohne das laeuft die Pipe bei einem Geraet, das
        # Aussetzer meldet, nach Stunden voll und arecord bleibt beim
        # Schreiben auf stderr stehen: die Aufnahme haengt, ohne dass ein
        # Prozess abstuerzt.
        self._errlines = collections.deque(maxlen=50)
        self._drain = None
        self.xruns = 0
        # Gleitendes Fenster der Spitzenwerte: (Zeit, dBFS).
        # Bewusst kein Maximum seit Aufnahmebeginn – sonst haengt eine
        # einzelne Uebersteuerung um 18:30 Uhr um 00:30 Uhr noch in der
        # Anzeige und sagt nichts mehr ueber die aktuelle Lage.
        self._peak_hist = collections.deque()

    # ---------- Gerätefähigkeiten ----------

    def probe_params(self):
        try:
            r = subprocess.run(
                ["arecord", "-D", self.device, "--dump-hw-params", "-d", "1"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self.last_error = f"hw-params: {e}"
            return {}

        out = (r.stderr or "") + (r.stdout or "")
        params = {}

        m = re.search(r"^FORMAT:\s*(.+)$", out, re.M)
        if m:
            params["formats"] = m.group(1).split()

        m = re.search(r"^CHANNELS:\s*(.+)$", out, re.M)
        if m:
            vals = re.findall(r"\d+", m.group(1))
            params["channels_max"] = max(int(v) for v in vals) if vals else None

        return params

    def format_candidates(self, probed):
        """Formate in der Reihenfolge, in der sie versucht werden.

        Bewusst eine LISTE, nicht ein einzelnes Format: schlaegt die
        Geraeteabfrage fehl (Zeitueberschreitung, Geraet kurz belegt),
        weiss der Dienst nicht, was das Pult kann. Frueher waehlte er dann
        blind das erste Wunschformat – kann das Geraet es nicht, scheitert
        JEDE Kanalzahl mit derselben Meldung, und die Aufnahme kaeme nie
        zustande. Mit einer Liste geht er weiter zum naechsten Format.
        """
        want = [f.strip() for f in
                self.cfg.get("recording", "format_preference",
                             fallback="S24_3LE, S32_LE, S16_LE").split(",")
                if f.strip()]
        avail = probed.get("formats")
        if avail:
            passend = [f for f in want if f in avail]
            # Bietet das Geraet nichts aus der Wunschliste, das nehmen,
            # was es meldet – lieber ein ungewohntes Format als keine
            # Aufnahme.
            return passend or [f for f in avail if f in FORMAT_BYTES] or ["S32_LE"]
        return want or ["S32_LE"]

    def channel_candidates(self, probed):
        """Kanalzahlen, die der Reihe nach versucht werden.

        auto -> nimmt, was das Gerät anbietet. Schlägt die Abfrage fehl,
        wird absteigend probiert, bis eine Zahl startet.
        """
        raw = self.cfg.get("recording", "channels", fallback="auto").strip().lower()
        max_ch = probed.get("channels_max")

        if raw in ("auto", "max", "alle", "0", ""):
            return [max_ch] if max_ch else [32, 24, 16, 8, 4, 2]
        try:
            want = int(raw)
        except ValueError:
            return [max_ch] if max_ch else [32, 16, 8, 2]
        return [min(want, max_ch)] if max_ch else [want]

    # ---------- Start und Stopp ----------

    @staticmethod
    def sichere_dateilaenge(wunsch, fmt, ch, rate):
        """Dateilaenge, bei der die WAV sicher unter der 2-GiB-Grenze bleibt.

        arecord bricht eine WAV bei 2 GiB ab (siehe SAFE_WAV_BYTES). Bei
        32 Kanaelen ist das lange vor den konfigurierten 600 s erreicht.

        Rueckgabe: (zu verwendende Laenge, Groessengrenze in Sekunden).
        Die zweite Zahl ist 0, wenn der Wunschwert unveraendert passt –
        daran erkennt der Aufrufer, ob er etwas zu melden hat.

        Sonderfaelle, die hier bewusst mit abgedeckt sind:

          * wunsch = 0 oder negativ. Fuer arecord hiesse 0 "keine
            Zeitgrenze" – die Datei liefe dann geradewegs in die 2-GiB-
            Grenze. Deshalb wird auch dieser Fall auf die Groessengrenze
            gezogen.
          * sehr hohe Kanalzahlen, bei denen selbst 30 s zu lang waeren.
            Praktisch nicht erreichbar, aber die Rechnung soll auch dort
            stimmen und nicht an einer Untergrenze scheitern.

        Ein unbekanntes Format wird mit 4 Byte je Abtastwert gerechnet,
        also mit dem breitesten – im Zweifel lieber zu kurze Dateien.
        """
        byte_s = FORMAT_BYTES.get(fmt, 4) * max(1, ch) * max(1, rate)
        grenze = max(1, int(SAFE_WAV_BYTES // byte_s))
        if 0 < wunsch <= grenze:
            return wunsch, 0
        # Auf glatte 30 s abrunden, damit im Display eine runde Zahl steht.
        # Wird daraus 0 (winzige Grenze), bleibt die Grenze selbst stehen.
        return ((grenze // 30) * 30) or grenze, grenze

    def _drain_starten(self):
        """stderr von arecord fortlaufend leerlesen.

        Zwei Gruende: die Pipe darf nicht volllaufen (sonst blockiert
        arecord), und die letzten Zeilen sind bei einem Abbruch die
        einzige Auskunft darueber, was schiefging.
        """
        fh = self.proc.stderr
        if fh is None:
            return

        def lesen(fh, dq, sess):
            try:
                for zeile in fh:
                    s = zeile.decode(errors="replace").strip()
                    if not s:
                        continue
                    dq.append(s)
                    low = s.lower()
                    if "overrun" in low or "xrun" in low or "underrun" in low:
                        sess.xruns += 1
            except (OSError, ValueError):
                pass
            finally:
                try:
                    fh.close()
                except (OSError, ValueError):
                    pass

        self._drain = threading.Thread(target=lesen, daemon=True,
                                       args=(fh, self._errlines, self))
        self._drain.start()

    def _letzter_fehler(self, vorgabe):
        """Letzte brauchbare arecord-Zeile, ohne die Startmeldung."""
        if self._drain:
            self._drain.join(2.0)
        for s in reversed(self._errlines):
            if not s.startswith("Recording WAVE"):
                return s
        return vorgabe

    def start(self, rec_root):
        probed = self.probe_params()
        formate = self.format_candidates(probed)
        rate = self.cfg.getint("recording", "rate", fallback=48000)
        candidates = [c for c in self.channel_candidates(probed) if c and c > 0]
        log(f"{self.device} ({self.card_name}): {rate} Hz, "
            f"Formate {formate}, Kanalzahlen {candidates}")

        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        prefix = self.cfg.get("recording", "session_prefix", fallback="gig")
        session = rec_root / f"{prefix}_{safe_name(self.card_name)}_{stamp}"
        try:
            session.mkdir(parents=True, exist_ok=False)
            self._chown(session)
        except OSError as e:
            self.last_error = f"Zielordner nicht anlegbar: {e}"
            return False

        name_pat = self.cfg.get("recording", "filename_pattern",
                                fallback="r_%y%m%d_%H%M%S.wav")
        pattern = str(session / name_pat)
        wunsch_zeit = self.cfg.getint("recording", "max_file_time_s",
                                      fallback=600)
        extra = self.cfg.get("recording", "arecord_extra", fallback="").split()

        for fmt in formate:
            for ch in candidates:
                # Dateilaenge so waehlen, dass die 2-GiB-Grenze von arecord
                # nie erreicht wird. Haengt von Kanalzahl UND Format ab,
                # muss also hier in der Schleife stehen.
                max_file_time, grenze = self.sichere_dateilaenge(
                    wunsch_zeit, fmt, ch, rate)
                if grenze:
                    log(f"{self.card_name}: {ch} Kanäle in {fmt} füllen "
                        f"{SAFE_WAV_BYTES / 2**30:.2f} GiB nach {grenze} s "
                        f"(arecord bricht eine WAV bei 2 GiB ab) – Dateilänge "
                        f"von {wunsch_zeit} s auf {max_file_time} s gekürzt")

                cmd = [
                    "arecord", "-D", self.device,
                    "-f", fmt, "-c", str(ch), "-r", str(rate),
                    "--max-file-time", str(max_file_time),
                    "--use-strftime", "-t", "wav",
                ] + extra + [pattern]

                log("Starte: " + " ".join(cmd))
                self._errlines.clear()
                self._drain = None
                self.xruns = 0
                try:
                    self.proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        start_new_session=True,
                    )
                except OSError as e:
                    self.last_error = str(e)
                    self.proc = None
                    continue
                self._drain_starten()

                # Stirbt arecord sofort, passt Kanalzahl oder Format nicht
                time.sleep(2.0)
                if self.proc.poll() is None:
                    self.session_dir = session
                    self.started_at = time.time()
                    self.params = {"format": fmt, "channels": ch, "rate": rate,
                                   "max_file_time": max_file_time,
                                   "max_file_time_wunsch": wunsch_zeit}
                    self._last_bytes = 0
                    self._last_bytes_t = time.time()
                    self._last_level_t = 0.0
                    self._levels = {}
                    self._peak_hist.clear()
                    self.last_error = ""
                    self._write_session_note()
                    log(f"Aufnahme läuft: {session} ({fmt}, {ch} Kanäle, "
                        f"{rate} Hz, {max_file_time} s je Datei"
                        f"{' – gekürzt' if grenze else ''})")
                    return True

                self.last_error = self._letzter_fehler("unbekannt")
                log(f"{self.card_name}: {fmt} mit {ch} Kanälen abgelehnt "
                    f"– {self.last_error}")
                self.proc = None

        try:
            session.rmdir()
        except OSError:
            pass
        self.last_error = self.last_error or "kein Format/keine Kanalzahl angenommen"
        return False

    def _warte(self, sekunden, tick):
        """Auf das Ende von arecord warten, dabei den Status frisch halten.

        Wichtig fuer die Anzeige: das Schliessen darf 25 Sekunden dauern,
        und eine Statusdatei, die 8 Sekunden alt ist, gilt als veraltet.
        Ohne dieses Nachfassen erschiene mitten im geordneten Beenden ein
        rotes "KEIN DIENST" — genau dann, wenn jemand davorsteht und auf
        "GESTOPPT" wartet.
        """
        ende = time.time() + sekunden
        while time.time() < ende:
            if self.proc.poll() is not None:
                return True
            time.sleep(0.25)
            if tick:
                try:
                    tick()
                except Exception:            # noqa: BLE001
                    pass
        return self.proc.poll() is not None

    def stop(self, tick=None):
        if not self.proc:
            return
        log(f"Stoppe {self.card_name}.")
        try:
            self.proc.send_signal(signal.SIGINT)   # arecord schliesst die WAV sauber
            if not self._warte(15, tick):
                log(f"{self.card_name}: reagiert nicht auf SIGINT – SIGTERM")
                self.proc.terminate()
                if not self._warte(10, tick):
                    log(f"{self.card_name}: auch das nicht – SIGKILL. "
                        "Die letzte Datei kann einen unvollständigen Kopf haben.")
                    self.proc.kill()
                    self._warte(5, tick)
        except Exception as e:  # noqa: BLE001
            log(f"Fehler beim Stoppen: {e}")
        if self.xruns:
            log(f"{self.card_name}: {self.xruns} Meldung(en) über Aussetzer "
                "während dieser Aufnahme")
        if self._drain:
            self._drain.join(2.0)
        self.proc = None

    def died(self):
        """True, wenn arecord unerwartet beendet wurde."""
        if not self.proc or self.proc.poll() is None:
            return False
        self.last_error = self._letzter_fehler("arecord beendet")
        self.proc = None
        return True

    def _write_session_note(self):
        """Legt eine kleine Textdatei neben die Aufnahme.

        Damit steht spaeter im Ordner, was gemessen wurde – wichtig, weil
        eine WAV zwar Kanalzahl und Rate mitbringt, aber nicht verraet,
        an welchem Pult sie entstanden ist.
        """
        if not self.session_dir:
            return
        p = self.params
        ch = p.get("channels", 0)
        ordner = self.session_dir.name
        try:
            note = self.session_dir / "aufnahme.txt"
            note.write_text(
                f"Gerät       : {self.card_name}\n"
                f"ALSA-Device : {self.device}\n"
                f"Start       : {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"Format      : {p.get('format', '')}\n"
                f"Kanäle      : {ch}\n"
                f"Abtastrate  : {p.get('rate', 0)} Hz\n"
                f"Dateilänge  : {p.get('max_file_time', 0)} s\n"
                "\n"
                "Die Stücke heißen r_JJMMTT_HHMMSS.wav; der Name ist die\n"
                "Startzeit des jeweiligen Stücks. Alphabetisch sortiert sind\n"
                "sie zugleich chronologisch – auch über Mitternacht hinweg.\n"
                "\n"
                "ZUSAMMENBINDEN UND IN EINZELSPUREN ZERLEGEN (am Mac):\n"
                "\n"
                "  1. spuren.txt in diesem Ordner ausfüllen – eine Zeile je\n"
                "     Kanal. Leere Zeile = Spur behält nur ihre Nummer.\n"
                f"  2. ./split-tracks.sh \"{ordner}\"\n"
                "\n"
                f"Ergebnis: {ch} Mono-Dateien im Unterordner spuren/, benannt\n"
                "nach der Vorlage. In Logic alle zusammen markieren und ins\n"
                "Arrangement ziehen – Logic legt je Datei eine Spur an.\n"
                "\n"
                "Die Namen lassen sich auch nachträglich setzen, das dauert\n"
                "Sekunden:  ./split-tracks.sh --umbenennen <spuren-Ordner> spuren.txt\n"
                "\n"
                "ZWEI PULTE AUF EINER ZEITACHSE:\n"
                "\n"
                "Jedes Gerät wandelt mit seinem eigenen Quarz. Zwei Aufnahmen\n"
                "laufen deshalb über einen langen Abend auseinander – bei\n"
                "250 ppm sind das rund 6 Sekunden auf 7 Stunden. Das ist keine\n"
                "Lücke in der Aufnahme, sondern der Unterschied der Taktquellen.\n"
                "\n"
                "  audiorec-check <Elternordner>\n"
                "\n"
                "rechnet den Versatz aus und nennt ihn in ppm. Korrigiert wird\n"
                "am Mac mit sox (brew install sox):\n"
                "\n"
                "  sox eingang.wav -b 24 ausgang.wav speed 1.000267 rate -v -s 48000\n"
                "\n"
                "Der Faktor ist Länge A geteilt durch Länge B, gemessen zwischen\n"
                "denselben zwei Ereignissen in beiden Aufnahmen. Nachgemessen:\n"
                "Frequenzgang unverändert, Rauschen 1,7 dB mehr bei -133 dBFS,\n"
                "Tonhöhe 0,46 Cent. Nichts davon ist hörbar.\n"
                "Wer Stück für Stück arbeitet und jeden Titel einzeln ausrichtet,\n"
                "kann das ganze Thema ignorieren.\n",
                encoding="utf-8")
            self._chown(note)
        except OSError:
            pass

        # Namensvorlage gleich mitliefern – mit der richtigen Kanalzahl.
        # So kann sie noch am Abend ausgefuellt werden, und das
        # Zerlege-Skript findet sie spaeter von selbst.
        try:
            vorlage = self.session_dir / "spuren.txt"
            kopf = (
                f"# Spurnamen für {self.card_name} — eine Zeile je Kanal, in der\n"
                f"# Reihenfolge der Aufnahme ({ch} Kanäle, {p.get('rate', 0)} Hz,\n"
                f"# {p.get('format', '')}). Leere Zeile = Spur behält nur ihre Nummer.\n"
                "# Zeilen mit # werden übersprungen.\n"
                "#\n"
                "# Aus Kanal 7 mit dem Eintrag 'Gitarre li' wird:\n"
                f"#     {safe_name(self.card_name)}_07_Gitarre li.wav\n"
                "#\n")
            vorlage.write_text(kopf + "\n" * ch, encoding="utf-8")
            self._chown(vorlage)
        except OSError:
            pass

    def _chown(self, path):
        user = self.cfg.get("recording", "owner", fallback="")
        if not user:
            return
        try:
            info = pwd.getpwnam(user)
            os.chown(path, info.pw_uid, info.pw_gid)
        except (KeyError, OSError):
            pass

    # ---------- Zahlen ----------

    def expected_mb_s(self):
        b = FORMAT_BYTES.get(self.params.get("format", ""), 4)
        return b * self.params.get("channels", 0) * self.params.get("rate", 0) / 1e6

    def stats(self):
        if not self.session_dir or not self.session_dir.is_dir():
            return 0, 0, []
        files = sorted(self.session_dir.glob("*.wav"))
        total, infos = 0, []
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                continue
            total += size
            infos.append({"name": f.name, "bytes": size})
        return len(files), total, infos[-RECENT_LIMIT:]

    def analyse_levels(self):
        """Spitzenpegel je Kanal aus dem Ende der laufenden Datei.

        Liest ein kurzes Fenster am Dateiende und rechnet den Betrag der
        Samples in dBFS um. Damit laesst sich unterscheiden, ob wirklich
        Audio ankommt oder nur Nullen geschrieben werden – und das Display
        kann daraus eine VU-Anzeige je Kanal zeichnen.

        Die Auswertung laeuft bewusst ueber array/max/min statt ueber eine
        Python-Schleife: bei 32 Kanaelen sind das sonst zehntausende
        Einzelzugriffe pro Messung. So bleibt es auch bei zwei Geraeten
        und zwei Messungen je Sekunde im einstelligen Prozentbereich.
        """
        fmt = self.params.get("format", "")
        ch = self.params.get("channels", 0)
        width = FORMAT_BYTES.get(fmt, 0)
        if not (self.proc and self.session_dir and ch and width):
            return {}

        files = sorted(self.session_dir.glob("*.wav"))
        if not files:
            return {}
        path = files[-1]
        block = ch * width
        want = PEAK_FRAMES * block

        try:
            size = path.stat().st_size
            if size < WAV_HEADER + block * 64:
                return {}
            start = max(WAV_HEADER, size - want)
            start = WAV_HEADER + ((start - WAV_HEADER) // block) * block
            with path.open("rb") as f:
                f.seek(start)
                data = f.read(want)
        except OSError:
            return {}

        frames = len(data) // block
        if frames < 8:
            return {}
        data = data[:frames * block]

        # Auf ein Array mit festem Elementtyp bringen. Bei 3 Byte pro Sample
        # faellt das niederwertigste Byte weg – das entspricht exakt einer
        # 16-Bit-Verkuerzung und reicht bis hinunter zu -96 dBFS.
        try:
            if width == 3:
                buf = bytearray(data)
                del buf[0::3]
                samples = array.array("h", bytes(buf))
                full = 2 ** 15
            elif width == 2:
                samples = array.array("h", data)
                full = 2 ** 15
            else:
                samples = array.array(I32, data)
                full = FULL_SCALE_FMT.get(fmt, 2 ** 31)
        except (ValueError, OverflowError):
            return {}
        if len(samples) < ch * 8:
            return {}

        dbs = []
        for c in range(ch):
            col = samples[c::ch]
            peak = max(max(col), -min(col))
            if peak >= full:
                dbs.append(0.0)
            elif peak > 0:
                db = 20.0 * math.log10(peak / full)
                # Nach unten klemmen. Bei S32_LE entspricht ein einzelnes
                # gesetztes Bit rechnerisch -186 dBFS – ein exakter, aber
                # sinnloser Wert: die Geraete liefern 24 Nutzbits, alles
                # darunter ist Muell im ungenutzten Rest des 32-Bit-Worts.
                # Solche Zahlen wuerden auch den Mittelwert verzerren.
                dbs.append(db if db > FLOOR_DBFS else FLOOR_DBFS)
            else:
                dbs.append(FLOOR_DBFS)

        schwelle = self.cfg.getfloat("recording", "silence_dbfs",
                                     fallback=SILENCE_DBFS)
        active = sum(1 for d in dbs if d > schwelle)
        return {"peak_dbfs": round(max(dbs), 1),
                "avg_dbfs": round(sum(dbs) / len(dbs), 1),
                "active_channels": active,
                "channel_dbfs": [round(d, 1) for d in dbs]}

    def stalled(self, timeout):
        """True, wenn die Datei seit timeout Sekunden nicht mehr waechst."""
        return (self.proc is not None and timeout > 0
                and self._last_growth_t > 0
                and time.time() - self._last_growth_t > timeout)

    def snapshot(self):
        count, total, recent = self.stats()
        recording = self.proc is not None
        if recent and recording:
            recent[-1]["active"] = True

        now = time.time()
        if total > self._last_total:
            self._last_total = total
            self._last_growth_t = now
        elif self._last_growth_t == 0.0 and recording:
            self._last_growth_t = now

        if recording and now - self._last_bytes_t >= 3.0:
            self._rate_mb_s = max(0.0, (total - self._last_bytes)
                                  / (now - self._last_bytes_t) / 1e6)
            self._last_bytes, self._last_bytes_t = total, now

        # Pegel deutlich haeufiger als die Datenrate – die VU-Anzeige soll
        # dem Signal folgen, nicht alle drei Sekunden zucken.
        lvl_iv = self.cfg.getfloat("recording", "level_interval_s", fallback=0.5)
        if recording and lvl_iv > 0 and now - self._last_level_t >= lvl_iv:
            self._last_level_t = now
            self._levels = self.analyse_levels()
            p = self._levels.get("peak_dbfs")
            if p is not None:
                self._peak_hist.append((now, p))
        elif not recording:
            self._levels = {}
            self._peak_hist.clear()

        win = self.cfg.getfloat("recording", "max_window_s", fallback=900.0)
        while self._peak_hist and now - self._peak_hist[0][0] > win:
            self._peak_hist.popleft()
        max_dbfs, clips = None, 0
        for _, p in self._peak_hist:
            if max_dbfs is None or p > max_dbfs:
                max_dbfs = p
            if p >= CLIP_DBFS:
                clips += 1

        return {
            "card_name": self.card_name,
            "device": self.device,
            "state": "RECORDING" if recording else "ERROR",
            "format": self.params.get("format", ""),
            "channels": self.params.get("channels", 0),
            "rate": self.params.get("rate", 0),
            "session_dir": str(self.session_dir) if self.session_dir else "",
            "elapsed_s": int(now - self.started_at) if self.started_at else 0,
            "files": count,
            "bytes": total,
            "recent_files": recent,
            "rate_mb_s": round(self._rate_mb_s if recording else 0.0, 2),
            "expected_mb_s": round(self.expected_mb_s() if recording else 0.0, 2),
            "peak_dbfs": self._levels.get("peak_dbfs"),
            "avg_dbfs": self._levels.get("avg_dbfs"),
            "active_channels": self._levels.get("active_channels"),
            "channel_dbfs": self._levels.get("channel_dbfs", []),
            "max_dbfs": max_dbfs,
            "clip_windows": clips,
            "max_window_s": int(win),
            "max_file_time_s": self.params.get("max_file_time", 0),
            "max_file_time_wunsch": self.params.get("max_file_time_wunsch", 0),
            "xruns": self.xruns,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------


class Recorder:
    """Verwaltet beliebig viele gleichzeitige Geräte-Aufnahmen."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sessions = {}          # card_name -> DeviceSession (laufend)
        self.failures = {}          # card_name -> {"error": str, "retry_at": float}
        self.state = "STARTING"
        self.message = "Initialisierung"
        self.blocker = ""
        self.active_root = self.fallback_root
        self.target_is_usb = False
        self.usb_candidate = None      # gefunden, aber noch nicht bestaetigt
        self.started_at = time.time()
        self.hold_until = 0.0          # Ende der Startkarenz
        self._too_small = set()        # schon gemeldete, zu kleine Datentraeger
        self._voll_gemeldet = set()    # schon gemeldete, volle Datentraeger
        self._voll_zaehler = 0         # Bestaetigungen fuer "Datentraeger voll"
        self.sysmon = SysMonitor(
            cfg.getfloat("recording", "sys_interval_s", fallback=5.0))
        self.sysmon.start()
        # Wie oft ein Geraet gestartet wurde. Ab dem zweiten Mal bedeutet
        # jeder Start eine Luecke in der Aufnahme – und einen weiteren
        # Ordner. Ohne Zaehler faellt das um 23 Uhr niemandem mehr auf.
        self.starts = {}
        self.last_restart = {}
        self.usb_accepted = None       # vom Benutzer bestaetigter Pfad
        self.usb_declined = set()      # abgelehnte Pfade
        self._prepare_input_dir()

    def _prepare_input_dir(self):
        """Ordner, in den die Anzeige (als Benutzer) schreiben darf."""
        try:
            INPUT_DIR.mkdir(parents=True, exist_ok=True)
            user = self.cfg.get("recording", "owner", fallback="")
            if user:
                info = pwd.getpwnam(user)
                os.chown(INPUT_DIR, info.pw_uid, info.pw_gid)
            os.chmod(INPUT_DIR, 0o755)
        except (OSError, KeyError) as e:
            log(f"Eingabeordner nicht nutzbar: {e}")

    def read_answer(self):
        """Antwort der Anzeige einlesen und die Datei danach entfernen."""
        try:
            data = json.loads(ANSWER_PATH.read_text())
        except (OSError, ValueError):
            return None
        try:
            ANSWER_PATH.unlink()
        except OSError:
            pass
        return data

    @property
    def fallback_root(self):
        """Fester Zielordner, wenn kein USB-Datenträger da ist."""
        return Path(self.cfg.get("recording", "target_dir",
                                 fallback="/home/pi/rec"))

    @property
    def match_patterns(self):
        raw = self.cfg.get("device", "match", fallback="")
        return [p.strip().lower() for p in raw.split(",") if p.strip()]

    @property
    def ignore_patterns(self):
        raw = self.cfg.get("device", "ignore", fallback="vc4hdmi,Headphones")
        return [p.strip().lower() for p in raw.split(",") if p.strip()]

    @property
    def max_devices(self):
        """0 oder kleiner = unbegrenzt."""
        return self.cfg.getint("device", "max_devices", fallback=0)

    @property
    def retry_delay(self):
        return self.cfg.getfloat("device", "retry_delay_s", fallback=10.0)

    def find_usb_target(self):
        """Erster beschreibbarer, eingehängter Wechseldatenträger – oder None.

        Raspberry Pi OS haengt USB-Sticks im Desktop-Betrieb automatisch
        unter /media/<benutzer>/<label> ein.
        """
        root = Path(self.cfg.get("recording", "usb_mount_root", fallback="/media"))
        if not root.is_dir():
            return None

        # /media/<benutzer>/<label>  und  /media/<label>
        candidates = []
        try:
            for lvl1 in sorted(root.iterdir()):
                if not lvl1.is_dir():
                    continue
                if os.path.ismount(lvl1):
                    candidates.append(lvl1)
                    continue
                try:
                    for lvl2 in sorted(lvl1.iterdir()):
                        if lvl2.is_dir() and os.path.ismount(lvl2):
                            candidates.append(lvl2)
                except OSError:
                    pass
        except OSError:
            return None

        # Der einmal genommene Datentraeger hat Vorrang – sonst uebernimmt
        # ein spaeter eingesteckter Stick, dessen Name alphabetisch vorn
        # liegt, die Fuehrung und das Ziel wechselt mitten im Betrieb.
        if self.usb_accepted:
            candidates.sort(key=lambda p: str(p) != self.usb_accepted)

        need = self.cfg.getfloat("recording", "min_free_gb", fallback=0.0)
        zu_klein = []          # beschreibbar, aber unter dem Planwert
        for vol in candidates:
            if not os.access(vol, os.W_OK):
                continue
            try:
                frei = shutil.disk_usage(vol).free / 1e9
            except OSError:
                continue
            # Volle Datentraeger kommen gar nicht in Frage – ohne Ausnahme,
            # auch nicht fuer den zuvor gewaehlten. Sonst waehlte der Dienst
            # nach dem Beenden wegen Vollstands genau denselben wieder aus
            # und liefe im Kreis.
            if frei < HARD_MIN_FREE_GB:
                if str(vol) not in self._voll_gemeldet:
                    self._voll_gemeldet.add(str(vol))
                    log(f"{vol} uebergangen: voll (nur {gb(frei)} GB frei)")
                continue
            self._voll_gemeldet.discard(str(vol))
            # Zu kleine Datentraeger nachrangig behandeln – sonst macht ein
            # versehentlich eingesteckter 8-GB-Stick die Aufnahme kaputt,
            # statt nur uebergangen zu werden.
            if str(vol) != self.usb_accepted and frei < need:
                zu_klein.append((frei, vol))
                if str(vol) not in self._too_small:
                    self._too_small.add(str(vol))
                    log(f"{vol} zunaechst uebergangen: {gb(frei)} GB frei, "
                        f"Planwert {need:.0f} GB")
                continue
            return vol

        # Keiner erfuellt den Planwert. Statt still auf die SD-Karte
        # zurueckzufallen, den groessten nehmen – aber nur, wenn dort mehr
        # Platz ist als im Ersatzordner.
        #
        # Der Grund: min_free_gb ist ein Planwert. Wird er zu hoch gesetzt
        # (200 bei einer 1-TB-Platte, auf der noch Material liegt), waere
        # sonst die SD-Karte das Ziel – und die ist um Groessenordnungen
        # kleiner. Ein zu hoch gesetzter Wert darf die Aufnahme nicht auf
        # den schlechteren Datentraeger schicken.
        if zu_klein:
            frei, vol = max(zu_klein)
            try:
                frei_ersatz = shutil.disk_usage(self.fallback_root).free / 1e9
            except OSError:
                frei_ersatz = 0.0
            if frei > frei_ersatz:
                log(f"Kein Datentraeger erreicht {need:.0f} GB. Nehme "
                    f"trotzdem {vol} ({frei:.0f} GB frei) – der Ersatzordner "
                    f"hat nur {frei_ersatz:.0f} GB.")
                return vol
        return None

    def resolve_target(self):
        """Setzt active_root, target_is_usb und ggf. usb_candidate."""
        # ------------------------------------------------------------------
        # Solange irgendein Geraet aufnimmt, wird das Ziel NICHT mehr
        # angefasst. Jede Aenderung wuerde die laufenden Aufnahmen stoppen
        # und in einem neuen Ordner fortsetzen.
        #
        # Ohne diese Sperre reicht ein zweiter Datentraeger, der um 22 Uhr
        # zum Kopieren eingesteckt wird: haengt er alphabetisch vor dem
        # bisherigen Ziel, liefert find_usb_target() ihn zurueck – und der
        # Abend laeuft ploetzlich auf einem 32-GB-Stick weiter oder faellt
        # auf die SD-Karte zurueck.
        #
        # Faellt der Datentraeger wirklich aus, stirbt arecord; die Session
        # wird abgeraeumt, self.sessions ist leer, und der naechste Durchlauf
        # sucht regulaer ein neues Ziel.
        # ------------------------------------------------------------------
        if self.sessions:
            self.usb_candidate = None
            return

        mode = self.cfg.get("recording", "usb_target",
                            fallback="auto").strip().lower()
        usb = self.find_usb_target() if mode != "no" else None
        self.usb_candidate = None

        # Stick weg? Dann alle Merker zuruecksetzen
        if usb is None:
            if self.usb_accepted:
                log("USB-Ziel entfernt – zurueck auf " + str(self.fallback_root))
            self.usb_accepted = None
            self.usb_declined.clear()
        else:
            key = str(usb)
            # Hier ist sicher, dass keine Aufnahme laeuft (siehe oben).
            auto_ok = mode == "auto"
            if auto_ok or key == self.usb_accepted:
                sub = self.cfg.get("recording", "usb_subdir", fallback="audiorec")
                root = usb / sub if sub else usb
                try:
                    root.mkdir(parents=True, exist_ok=True)
                    self.usb_accepted = key
                    self.active_root, self.target_is_usb = root, True
                    return
                except OSError as e:
                    log(f"USB-Ziel {root} nicht nutzbar: {e}")
                    self.usb_declined.add(key)
            elif key not in self.usb_declined:
                self.usb_candidate = key      # Display fragt nach

        self.active_root, self.target_is_usb = self.fallback_root, False

    def handle_answer(self):
        """Antwort auf die USB-Rueckfrage verarbeiten."""
        ans = self.read_answer()
        if not ans:
            return
        path = ans.get("path", "")
        if ans.get("usb") == "yes":
            log(f"USB-Ziel bestaetigt: {path}")
            self.usb_accepted = path
            self.usb_declined.discard(path)
        elif ans.get("usb") == "no":
            log(f"USB-Ziel abgelehnt: {path}")
            self.usb_declined.add(path)

    # ---------- Geräte ----------

    def find_cards(self):
        """Alle passenden aufnahmefähigen Karten als [(index, name), ...]."""
        try:
            text = Path("/proc/asound/cards").read_text()
        except OSError:
            return []

        found = []
        # Zeilenformat: " 1 [Mixer          ]: USB-Audio - Mixer"
        for line in text.splitlines():
            m = re.match(r"\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(.*)", line)
            if not m:
                continue
            idx, short, desc = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
            hay = f"{short} {desc}".lower()

            if any(p in hay for p in self.ignore_patterns):
                continue
            if self.match_patterns and not any(p in hay for p in self.match_patterns):
                continue
            if not Path(f"/proc/asound/card{idx}/pcm0c").exists():
                continue
            found.append((idx, short))
        limit = self.max_devices
        return found[:limit] if limit > 0 else found

    # ---------- Ziel ----------

    def free_gb(self):
        """Freier Platz in GB – oder None, wenn er sich nicht messen laesst.

        Bewusst None und nicht 0.0: eine 0 waere von "Datentraeger voll"
        nicht zu unterscheiden, und daran haengt jetzt das Beenden
        laufender Aufnahmen.
        """
        try:
            return shutil.disk_usage(self.active_root).free / 1e9
        except OSError:
            return None

    def check_target(self):
        """Rückgabe: Grund, warum nicht aufgenommen werden kann – oder ''.

        min_free_gb steht hier BEWUSST NICHT.

        Der Wert entscheidet in find_usb_target() darueber, welcher
        Datentraeger genommen wird – ein versehentlich eingesteckter kleiner
        Stick wird dort uebergangen. Als Abbruchbedingung waere er dagegen
        eine Falle in beide Richtungen:

          * mit min_free_gb = 200 und einer 512-GB-Platte faellt der freie
            Platz im Laufe des Abends unter die Schwelle – ab da koennte
            sich kein Geraet nach einem Wackler mehr neu starten, obwohl
            noch 190 GB frei sind;
          * ist der Ersatzordner auf der SD-Karte kleiner als der Planwert,
            wuerde ueberhaupt nicht aufgenommen.

        Eine unvollstaendige Aufnahme ist immer besser als gar keine.
        Deshalb bricht hier nur die harte Restreserve ab; der Planwert wird
        als Warnung angezeigt (platz_warnung).
        """
        # fallback=False, weil target_dir ueblicherweise ein Ordner auf der
        # SD-Karte ist. Mit True (dem frueheren Wert) wuerde eine
        # unvollstaendige Konfigurationsdatei dazu fuehren, dass gar nicht
        # aufgenommen wird.
        if (not self.target_is_usb
                and self.cfg.getboolean("recording", "require_mountpoint",
                                        fallback=False)
                and not os.path.ismount(self.active_root)):
            return f"Datenträger nicht eingehängt ({self.active_root})"
        if not self.active_root.is_dir():
            return f"Zielordner fehlt ({self.active_root})"
        free = self.free_gb()
        if free is not None and free < HARD_MIN_FREE_GB:
            return (f"Datenträger voll: nur noch {free:.2f} GB frei "
                    f"({self.active_root})")
        return ""

    def platz_warnung(self):
        """Hinweis, wenn der Platz knapp wird – ohne die Aufnahme zu bremsen."""
        need = self.cfg.getfloat("recording", "min_free_gb", fallback=0.0)
        free = self.free_gb()
        if need > 0 and free is not None and free < need:
            return f"nur noch {free:.0f} GB frei (Planwert {need:.0f} GB)"
        return ""

    def pruefe_voll(self):
        """Laufende Aufnahmen geordnet beenden, wenn der Datentraeger voll ist.

        Ohne das liefe die Aufnahme bis zum letzten freien Byte. arecord
        bekaeme dann beim Schreiben einen Fehler, und die gerade offene
        Datei behielte den vorlaeufigen Kopf mit falscher Laengenangabe.
        Hier wird stattdessen SIGINT geschickt, solange noch Platz zum
        Schreiben des Kopfes da ist.

        Gemessen wird mehrfach (VOLL_BESTAETIGUNGEN), bevor gehandelt wird:
        ein einzelner Messfehler darf keine Aufnahme beenden.
        """
        if not self.sessions:
            self._voll_zaehler = 0
            return
        frei = self.free_gb()
        if frei is None or frei >= HARD_MIN_FREE_GB:
            if self._voll_zaehler:
                log(f"{self.active_root}: wieder ueber der Grenze, "
                    "Aufnahme laeuft weiter")
            self._voll_zaehler = 0
            return

        self._voll_zaehler += 1
        if self._voll_zaehler < VOLL_BESTAETIGUNGEN:
            log(f"{self.active_root}: nur noch {frei:.2f} GB frei "
                f"({self._voll_zaehler}/{VOLL_BESTAETIGUNGEN})")
            return

        log(f"{self.active_root} ist voll ({frei:.2f} GB frei) – "
            f"{len(self.sessions)} Aufnahme(n) werden jetzt geordnet "
            "geschlossen, damit die WAV-Köpfe stimmen.")
        self.state = "STOPPING"
        self.message = "Datenträger voll – Aufnahmen werden geschlossen"
        self.write_status()
        for name in list(self.sessions):
            self.sessions.pop(name).stop(tick=self.write_status)
        self.failures.clear()
        self._voll_zaehler = 0
        # Ab jetzt ist der Datentraeger auch fuer die Zielsuche tabu
        # (find_usb_target uebergeht alles unter HARD_MIN_FREE_GB). Gibt es
        # einen anderen mit Platz, laeuft die Aufnahme dort weiter; sonst
        # bleibt es beim Fehlerzustand.
        self.blocker = self.check_target()

    # ---------- Status ----------

    def write_status(self):
        devices = []
        for name, sess in self.sessions.items():
            snap = sess.snapshot()
            snap["restarts"] = max(0, self.starts.get(name, 1) - 1)
            snap["last_restart_s"] = (
                int(time.time() - self.last_restart[name])
                if name in self.last_restart else None)
            devices.append(snap)
        expected_sum = sum(d["expected_mb_s"] for d in devices)

        free_gb = total_gb = remaining_h = 0.0
        try:
            du = shutil.disk_usage(self.active_root)
            free_gb, total_gb = du.free / 1e9, du.total / 1e9
            if expected_sum > 0:
                remaining_h = (du.free / (expected_sum * 1e6)) / 3600.0
        except OSError:
            pass

        data = {
            "state": self.state,
            "message": self.message,
            "blocker": self.blocker,
            "target": str(self.active_root),
            "target_is_usb": self.target_is_usb,
            "usb_candidate": self.usb_candidate or "",
            "recording_active": any(s.proc for s in self.sessions.values()),
            "devices": devices,
            "device_count": len(devices),
            "expected_total_mb_s": round(expected_sum, 2),
            "disk_free_gb": round(free_gb, 1),
            "disk_total_gb": round(total_gb, 1),
            "remaining_h": round(remaining_h, 1),
            "clock_synced": TIMESYNC_FLAG.exists(),
            "system": self.sysmon.snapshot(),
            "updated": time.time(),
        }
        try:
            STATUS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
            tmp.replace(STATUS_PATH)
            os.chmod(STATUS_PATH, 0o644)
        except OSError as e:
            log(f"Status nicht schreibbar: {e}")

    def start_hold(self):
        """Kurz warten, bevor die allererste Aufnahme beginnt.

        Zwei Faelle, beide nur relevant, solange noch gar nichts laeuft:

        1. Der Datentraeger ist beim Hochfahren schon angesteckt, aber der
           Desktop hat ihn noch nicht eingehaengt. Ohne Karenz liefe die
           Aufnahme auf dem Ersatzziel an und wuerde Sekunden spaeter
           abgebrochen, sobald der Datentraeger auftaucht.
        2. Im Modus "ask" ist die Rueckfrage noch unbeantwortet.

        Laeuft die Karenz ab, faengt der Dienst auf dem Ersatzziel an –
        lieber ein Ordner zu viel als eine fehlende Aufnahme, wenn niemand
        am Display steht. Rueckgabe: (halten, Grund, Restsekunden).
        """
        if self.sessions:
            self.hold_until = 0.0
            return False, "", 0

        if self.usb_candidate:
            grace = self.cfg.getfloat("recording", "usb_ask_grace_s", fallback=90.0)
            grund = "bitte Ziel bestätigen"
        else:
            grace = self.cfg.getfloat("recording", "startup_grace_s", fallback=60.0)
            # nur unmittelbar nach dem Dienststart, nicht dauerhaft
            if time.time() - self.started_at > grace:
                self.hold_until = 0.0
                return False, "", 0

            offen = []
            if not self.target_is_usb:
                offen.append("externen Datenträger")
            # Die Dateinamen sind Zeitstempel. Startet die Aufnahme, bevor
            # der Pi die Uhr per Netz gestellt hat, heisst die erste Datei
            # r_700101_010000.wav – und ein Zeitsprung mitten in der
            # Aufnahme bringt die Sortierung durcheinander.
            if self.cfg.getboolean("recording", "wait_for_clock",
                                   fallback=True) and not TIMESYNC_FLAG.exists():
                offen.append("Zeitabgleich")
            if not offen:
                self.hold_until = 0.0
                return False, "", 0
            grund = "warte auf " + " und ".join(offen)

        if grace <= 0:
            return False, "", 0
        if not self.hold_until:
            self.hold_until = time.time() + grace
        rest = self.hold_until - time.time()
        if rest <= 0:
            return False, "", 0
        return True, grund, int(rest) + 1

    def refresh_state(self, present, hold=(False, "", 0)):
        recording = [s for s in self.sessions.values() if s.proc]
        holding, grund, rest = hold

        if holding:
            self.state = "STARTING"
            vorn = f"{len(present)} Gerät(e) bereit · " if present else ""
            self.message = f"{vorn}{grund} — Start in {rest} s"
        elif self.blocker and not recording:
            self.state = "ERROR"
            self.message = self.blocker
            if present:
                self.message += " · bereit: " + ", ".join(n for _, n in present)
        elif self.failures:
            self.state = "ERROR"
            self.message = "; ".join(f"{n}: {f['error']}"
                                     for n, f in self.failures.items())
            if recording:
                self.message += f" · {len(recording)} Gerät(e) laufen weiter"
        elif recording:
            # Eine laufende Aufnahme bleibt AUFNAHME. Alles andere waere
            # irrefuehrend: ein roter FEHLER-Balken um 23 Uhr laesst jemanden
            # eingreifen, obwohl alles mitgeschnitten wird.
            self.state = "RECORDING"
            self.message = (f"{len(recording)} Geräte nehmen auf"
                            if len(recording) > 1 else "Aufnahme läuft")
            warnung = self.blocker or self.platz_warnung()
            if warnung:
                self.message += " · " + warnung
        else:
            self.state = "WAITING"
            self.message = "Warte auf Audio-Interface"
            warnung = self.platz_warnung()
            if warnung:
                self.message += " · " + warnung

    # ---------- Hauptschleife ----------

    def run(self):
        poll = self.cfg.getfloat("device", "poll_interval_s", fallback=2.0)

        while running:
            present = self.find_cards()
            present_names = {name for _, name in present}

            self.handle_answer()

            prev_root = self.active_root
            self.resolve_target()
            if self.active_root != prev_root:
                log(f"Ziel gewechselt: {self.active_root}"
                    f"{' (USB)' if self.target_is_usb else ''}")
                if self.sessions:
                    log("Laufende Aufnahmen werden auf dem neuen Ziel neu gestartet.")
                    for name in list(self.sessions):
                        self.sessions.pop(name).stop()
                    self.failures.clear()

            self.blocker = self.check_target()
            # Voller Datentraeger: laufende Aufnahmen beenden, solange das
            # Schliessen der WAV-Koepfe noch gelingt.
            self.pruefe_voll()
            self.sysmon.ziel_setzen(self.active_root)

            # 1. Verschwundene Geräte beenden – VOR der Absturzprüfung, sonst
            #    meldet das Abziehen kurz einen Fehler, obwohl nichts kaputt ist
            for name in list(self.sessions):
                if name not in present_names:
                    log(f"{name} verschwunden – stoppe.")
                    self.sessions.pop(name).stop()
            for name in list(self.failures):
                if name not in present_names:
                    self.failures.pop(name)

            # 2a. Stillstand erkennen: Prozess lebt, aber die Datei waechst
            #     nicht mehr. Ein echter Aufhaenger, den Restart=always nicht
            #     bemerken wuerde.
            stall = self.cfg.getfloat("device", "stall_timeout_s", fallback=30.0)
            for name, sess in list(self.sessions.items()):
                if sess.stalled(stall):
                    log(f"{name}: seit {stall:.0f}s keine neuen Daten – Neustart")
                    sess.stop()
                    self.sessions.pop(name)
                    self.failures[name] = {
                        "error": "Aufnahme stand still – neu gestartet",
                        "retry_at": time.time() + 2}

            # 2b. Abgestürzte Prozesse einsammeln und zur Wiederholung vormerken
            for name, sess in list(self.sessions.items()):
                if sess.died():
                    log(f"{name}: arecord beendet – {sess.last_error}")
                    self.sessions.pop(name)
                    self.failures[name] = {"error": sess.last_error,
                                           "retry_at": time.time() + self.retry_delay}

            # 3. Neue oder wieder fällige Geräte starten
            hold = self.start_hold()
            if not self.blocker and not hold[0]:
                now = time.time()
                for idx, name in present:
                    if name in self.sessions:
                        continue
                    fail = self.failures.get(name)
                    if fail and now < fail["retry_at"]:
                        continue
                    sess = DeviceSession(self.cfg, idx, name)
                    # Waehrend des Starts steht der Zustand kurz still.
                    # Laeuft dabei schon eine Aufnahme, bleibt AUFNAHME
                    # stehen – sonst flackert die Anzeige jedes Mal auf
                    # STARTE, wenn ein zweites Pult dazukommt oder ein
                    # Geraet nach einem Fehler erneut versucht wird.
                    laufend = [s for s in self.sessions.values() if s.proc]
                    if laufend:
                        self.state = "RECORDING"
                        self.message = (f"{len(laufend)} Gerät(e) nehmen auf"
                                        f" · starte {name} …")
                    else:
                        self.state = "STARTING"
                        self.message = f"Gerät gefunden: {name}"
                    self.write_status()
                    if sess.start(self.active_root):
                        self.sessions[name] = sess
                        self.failures.pop(name, None)
                        self.starts[name] = self.starts.get(name, 0) + 1
                        if self.starts[name] > 1:
                            self.last_restart[name] = time.time()
                            log(f"{name}: Neustart Nr. {self.starts[name] - 1}"
                                f" – es gibt jetzt {self.starts[name]} Ordner")
                    else:
                        log(f"{name}: Start fehlgeschlagen – {sess.last_error}")
                        self.failures[name] = {
                            "error": sess.last_error,
                            "retry_at": time.time() + self.retry_delay}

            self.refresh_state(present, hold)
            self.write_status()
            time.sleep(poll)

        # Geordnetes Beenden. Waehrenddessen wird der Status weiter
        # geschrieben (siehe DeviceSession._warte), damit die Anzeige
        # nicht faelschlich "KEIN DIENST" meldet.
        self.state = "STOPPING"
        self.message = "Aufnahmen werden geschlossen – bitte warten"
        self.write_status()
        for sess in self.sessions.values():
            sess.stop(tick=self.write_status)
            self.write_status()
        self.sessions.clear()
        self.state = "STOPPED"
        self.message = "Dienst beendet – Dateien sind sauber geschlossen"
        self.write_status()


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # interpolation=None ist zwingend: filename_pattern enthaelt
    # strftime-Platzhalter wie %y%m%d. Mit der Standard-Interpolation
    # deutet configparser das Prozentzeichen als eigene Syntax und wirft
    # beim Auslesen eine InterpolationSyntaxError – mitten im Start der
    # Aufnahme.
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(CONFIG_PATH):
        log(f"Konfiguration nicht gefunden: {CONFIG_PATH}")
        sys.exit(1)

    Recorder(cfg).run()


if __name__ == "__main__":
    main()
