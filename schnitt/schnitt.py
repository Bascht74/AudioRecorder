#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
schnitt.py — Mehrspur-Mitschnitte in Stücke und Einzelspuren zerlegen.

Aus fortlaufenden Mehrkanal-WAVs, wie sie ein Recorder über Stunden
schreibt, werden einzelne Mono- und Stereodateien je Stück geschnitten:
gleicher Anfang, gleiche Länge, ein Ordner je Stück, sortierbare Namen,
BWF-Zeitstempel. Zwei Geräte mit eigenem Quarz werden dabei auf eine
gemeinsame Zeitachse gezogen.

Betriebsarten
    kuerzen      alles außerhalb des Zeitraums aussortieren
    karte        einmal alles messen -> karte.json, karte.html (Wärmebild)
                 und stuecke-vorschlag.txt
    sync         Taktversatz zweier Geräte messen -> sync.json
    pegel        zeigen, welche Spuren in einem Zeitfenster belegt sind
    paare        messen, welche Spuren dasselbe Signal tragen
    summe        prüfen, ob eine Spur die Summe mehrerer anderer ist
    schnitt      ein Stück schneiden
    stuecke      viele Stücke aus einer Textdatei schneiden
    anschlag     zählen, wie oft der Vollausschlag berührt wird
    umbenennen   Spurnamen nachtragen, nachdem man reingehört hat

Braucht: python3, ffmpeg, numpy.  sox nur für die Driftkorrektur.
"""

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

VERSION = "4.3"

# Darunter liegt bei 24 Bit kein Nutzsignal mehr, nur Wandlerrauschen.
STILL_DBFS = -80.0
STILLE_DB = -200.0          # Ersatzwert für "-inf"
DB_SKALA = 10               # in karte.json wird dB * 10 als ganze Zahl abgelegt

AUSGABE_FORMATE = {
    "s16": ("pcm_s16le", 2),
    "s24": ("pcm_s24le", 3),
    "s32": ("pcm_s32le", 4),
}


# --------------------------------------------------------------- Ausgabe

def sag(*teile):
    print(*teile)
    sys.stdout.flush()


def warnung(text):
    sag("  ! " + text)


_GESAGT = set()


def warnung_einmal(text):
    """Dieselbe Warnung nicht dutzendfach wiederholen."""
    if text not in _GESAGT:
        _GESAGT.add(text)
        warnung(text)


def abbruch(text):
    sys.stderr.write("FEHLER: " + text + "\n")
    sys.exit(1)


# --------------------------------------------------------------- Zeiten

def parse_dauer(text):
    """'6:30' = 6 min 30 s · '1:20:00' = 1 h 20 min · '120' = 120 min ·
    '90s' = 90 s · '2h' = 2 h.  Minuten dürfen über 60 liegen."""
    s = str(text).strip().lower().replace(",", ".")
    if not s:
        raise ValueError("leere Dauer")

    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hms])", s)
    if m:
        wert = float(m.group(1))
        return wert * {"h": 3600.0, "m": 60.0, "s": 1.0}[m.group(2)]

    teile = s.split(":")
    if not all(re.fullmatch(r"\d+(?:\.\d+)?", t) for t in teile if t != ""):
        raise ValueError("Dauer '%s' nicht verstanden" % text)
    zahlen = [float(t) if t else 0.0 for t in teile]
    if len(zahlen) == 1:
        return zahlen[0] * 60.0                      # nackte Zahl = Minuten
    if len(zahlen) == 2:
        return zahlen[0] * 60.0 + zahlen[1]          # mm:ss
    if len(zahlen) == 3:
        return zahlen[0] * 3600.0 + zahlen[1] * 60.0 + zahlen[2]
    raise ValueError("Dauer '%s' hat zu viele Doppelpunkte" % text)


def parse_uhrzeit(text):
    """'19:00' oder '19:00:30' oder '19:00:30.5' -> Sekunden seit Mitternacht."""
    s = str(text).strip().replace(",", ".")
    teile = s.split(":")
    if len(teile) not in (2, 3):
        raise ValueError("Uhrzeit '%s' nicht verstanden (HH:MM[:SS])" % text)
    try:
        h = int(teile[0])
        mi = int(teile[1])
        se = float(teile[2]) if len(teile) == 3 else 0.0
    except ValueError:
        raise ValueError("Uhrzeit '%s' nicht verstanden" % text)
    if not (0 <= h < 24 and 0 <= mi < 60 and 0 <= se < 60):
        raise ValueError("Uhrzeit '%s' liegt außerhalb des Tages" % text)
    return h * 3600.0 + mi * 60.0 + se


def dauer_text(sekunden):
    """4.2 -> '00m04s' · 252 -> '04m12s' · 3900 -> '1h05m00s'"""
    ganz = int(round(sekunden))
    h, rest = divmod(ganz, 3600)
    mi, se = divmod(rest, 60)
    if h:
        return "%dh%02dm%02ds" % (h, mi, se)
    return "%02dm%02ds" % (mi, se)


def dauer_hms(sekunden):
    """252 -> '4:12' · 3900 -> '1:05:00' — die Schreibweise für stuecke.txt."""
    ganz = int(round(sekunden))
    h, rest = divmod(ganz, 3600)
    mi, se = divmod(rest, 60)
    return "%d:%02d:%02d" % (h, mi, se) if h else "%d:%02d" % (mi, se)


def uhr_text(dt):
    return dt.strftime("%H:%M:%S")


def ppm_text(wert):
    if wert is None:
        return "?"
    if wert >= 10:
        return "%.0f" % wert
    return "%.2f" % wert if wert < 1 else "%.1f" % wert


# ----------------------------------------------------------------- WAV

def wav_kopf(pfad):
    """Kopfdaten einer WAV lesen — auch WAVE_FORMAT_EXTENSIBLE und RF64.

    Die Länge wird aus der tatsächlichen Dateigröße bestimmt, nicht aus dem
    Kopf: bricht arecord unsauber ab, steht im Kopf eine falsche Zahl.
    """
    groesse_datei = os.path.getsize(pfad)
    with open(pfad, "rb") as f:
        kopf = f.read(12)
        if len(kopf) < 12 or kopf[0:4] not in (b"RIFF", b"RF64") \
                or kopf[8:12] != b"WAVE":
            raise ValueError("%s ist keine WAV-Datei" % pfad)
        ist_rf64 = kopf[0:4] == b"RF64"
        ds64 = None
        info = {"pfad": str(pfad)}
        while True:
            k = f.read(8)
            if len(k) < 8:
                break
            kid = k[0:4]
            klen = struct.unpack("<I", k[4:8])[0]
            pos = f.tell()
            if kid == b"ds64":
                b = f.read(min(klen, 28))
                if len(b) >= 16:
                    ds64 = struct.unpack("<Q", b[8:16])[0]
            elif kid == b"fmt ":
                b = f.read(min(klen, 40))
                tag, ch, rate, _brate, align, bits = struct.unpack("<HHIIHH", b[0:16])
                if tag == 0xFFFE and len(b) >= 26:
                    bits_gueltig = struct.unpack("<H", b[18:20])[0]
                    if bits_gueltig:
                        bits = bits_gueltig
                info.update(tag=tag, kanaele=ch, rate=rate, align=align, bits=bits)
            elif kid == b"data":
                info["offset"] = pos
                echt = groesse_datei - pos
                erklaert = klen
                if ist_rf64 and klen == 0xFFFFFFFF and ds64 is not None:
                    erklaert = ds64
                if erklaert in (0, 0xFFFFFFFF) or erklaert > echt:
                    erklaert = echt
                info["daten"] = max(0, erklaert)
                break
            f.seek(pos + klen + (klen & 1))
    for pflicht in ("kanaele", "rate", "align", "offset"):
        if pflicht not in info:
            raise ValueError("%s: unvollständiger WAV-Kopf" % pfad)
    if info["align"] <= 0:
        raise ValueError("%s: Blockgröße 0 im Kopf" % pfad)
    info["frames"] = info["daten"] // info["align"]
    info["bytes_je_probe"] = info["align"] // max(1, info["kanaele"])
    return info


def s32_ist_24bit(pfad, info, proben=400000):
    """Prüft, ob bei 32-Bit-Material das unterste Byte immer 0 ist.

    Ist es das, steckt in der 32-Bit-Datei ein reines 24-Bit-Signal — dann
    ist die Ausgabe in 24 Bit verlustfrei und keine Wandlung.
    Rückgabe: (geprüfte Proben, Anzahl mit gesetztem untersten Byte).
    """
    if info.get("bytes_je_probe") != 4:
        return (0, 0)
    ab = info["offset"] + (info["frames"] // 2) * info["align"]
    menge = min(proben * 4, info["daten"] // 2)
    menge -= menge % 4
    if menge <= 0:
        return (0, 0)
    with open(pfad, "rb") as f:
        f.seek(ab)
        roh = f.read(menge)
    treffer = 0
    for i in range(0, len(roh), 4):
        if roh[i]:
            treffer += 1
    return (len(roh) // 4, treffer)


# -------------------------------------------------------------- Geräte

DATEI_MUSTER = re.compile(r"^r_(\d{6})_(\d{6})\.wav$")
ORDNER_MUSTER = re.compile(r"^(?:gig|.+?)_(.+)_(\d{4}-\d{2}-\d{2})_(\d{6})$")


class Abschnitt(object):
    """Ein Aufnahmeordner: lückenlose Folge von Dateien auf einer Zeitachse."""

    def __init__(self, ordner):
        self.ordner = Path(ordner)
        self.dateien = []          # dicts: pfad, wanduhr, frames, pos
        self.rate = 0
        self.kanaele = 0
        self.bits = 0
        self.align = 0
        self.frames = 0
        self.notiz = {}
        self._laden()

    def _laden(self):
        namen = sorted(p for p in self.ordner.iterdir()
                       if DATEI_MUSTER.match(p.name))
        if not namen:
            raise ValueError("%s enthält keine r_*.wav" % self.ordner)
        pos = 0
        for p in namen:
            m = DATEI_MUSTER.match(p.name)
            try:
                wanduhr = datetime.strptime(m.group(1) + m.group(2), "%y%m%d%H%M%S")
            except ValueError:
                raise ValueError("Dateiname ohne gültige Zeit: %s" % p)
            info = wav_kopf(p)
            if not self.rate:
                self.rate = info["rate"]
                self.kanaele = info["kanaele"]
                self.bits = info["bits"]
                self.align = info["align"]
            elif (info["rate"], info["kanaele"], info["align"]) != \
                    (self.rate, self.kanaele, self.align):
                raise ValueError("%s hat andere Parameter als die erste Datei "
                                 "des Ordners" % p)
            self.dateien.append({"pfad": p, "wanduhr": wanduhr,
                                 "frames": info["frames"], "pos": pos,
                                 "offset": info["offset"]})
            pos += info["frames"]
        self.frames = pos
        notiz = self.ordner / "aufnahme.txt"
        if notiz.exists():
            for zeile in notiz.read_text(encoding="utf-8", errors="replace").splitlines():
                if ":" in zeile:
                    k, _, v = zeile.partition(":")
                    k = k.strip().lower()
                    if k in ("format", "kanäle", "kanaele", "abtastrate",
                             "dateilänge", "dateilaenge", "gerät", "geraet", "start"):
                        self.notiz[k] = v.strip()

    # ---- Zeitachse -----------------------------------------------------

    @property
    def beginn(self):
        return self.dateien[0]["wanduhr"]

    @property
    def ende_wanduhr(self):
        letzte = self.dateien[-1]
        return letzte["wanduhr"] + timedelta(seconds=letzte["frames"] / self.rate)

    def dauer_s(self):
        return self.frames / float(self.rate)

    def luecken(self):
        """Abweichung zwischen Dateinamenszeit und aufsummiertem Audio.

        Baut sich das gleichmäßig auf, ist es Taktversatz. Springt es an einer
        Stelle, ist dort eine Lücke.
        """
        raus = []
        for d in self.dateien[1:]:
            erwartet = self.beginn + timedelta(seconds=d["pos"] / self.rate)
            raus.append((d["pfad"].name, (d["wanduhr"] - erwartet).total_seconds()))
        return raus

    def takt(self):
        """(Faktor Audio/Wanduhr, Unsicherheit in ppm) aus den Dateinamen.

        Die letzte Datei zählt nicht mit: ihre Länge hängt davon ab, wann
        gestoppt wurde. Auflösung der Dateinamen: 1 s.
        """
        if len(self.dateien) < 2:
            return (None, None)
        spanne = (self.dateien[-1]["wanduhr"] - self.beginn).total_seconds()
        audio = self.dateien[-1]["pos"] / float(self.rate)
        if spanne <= 0:
            return (None, None)
        return (audio / spanne, 1.0e6 / spanne)

    def pos_von_wanduhr(self, t):
        """Sample-Position in diesem Abschnitt für eine Uhrzeit (oder None)."""
        if t < self.beginn or t > self.ende_wanduhr:
            return None
        treffer = self.dateien[0]
        for d in self.dateien:
            if d["wanduhr"] <= t:
                treffer = d
            else:
                break
        versatz = (t - treffer["wanduhr"]).total_seconds()
        p = treffer["pos"] + int(round(versatz * self.rate))
        return max(0, min(self.frames, p))

    def wanduhr_von_pos(self, p):
        treffer = self.dateien[0]
        for d in self.dateien:
            if d["pos"] <= p:
                treffer = d
            else:
                break
        return treffer["wanduhr"] + timedelta(
            seconds=(p - treffer["pos"]) / float(self.rate))

    def ausserhalb(self, von, bis):
        """Dateien, die das Fenster [von, bis] überhaupt nicht berühren.

        Als Ende einer Datei gilt der Beginn der nächsten — das ist die
        Wanduhrzeit und damit dasselbe Maß wie das Fenster. Nur bei der
        letzten Datei wird die Audiolänge genommen.
        """
        raus = []
        for i, d in enumerate(self.dateien):
            anfang = d["wanduhr"]
            if i + 1 < len(self.dateien):
                ende = self.dateien[i + 1]["wanduhr"]
            else:
                ende = anfang + timedelta(seconds=d["frames"] / float(self.rate))
            if ende <= von or anfang >= bis:
                raus.append(d)
        return raus

    def konkat(self, q, m):
        """Dateiliste, die [q, q+m) überdeckt, plus Position von q darin."""
        if q < 0 or m <= 0 or q + m > self.frames:
            return None
        erste = 0
        for i, d in enumerate(self.dateien):
            if d["pos"] <= q:
                erste = i
            else:
                break
        letzte = erste
        for i in range(erste, len(self.dateien)):
            letzte = i
            d = self.dateien[i]
            if d["pos"] + d["frames"] >= q + m:
                break
        dateien = [d["pfad"] for d in self.dateien[erste:letzte + 1]]
        return (dateien, q - self.dateien[erste]["pos"])


class Geraet(object):
    """Ein Pult: ein oder mehrere Aufnahmeordner."""

    def __init__(self, tag, abschnitte):
        self.tag = tag
        self.abschnitte = sorted(abschnitte, key=lambda a: a.beginn)
        a0 = self.abschnitte[0]
        self.rate = a0.rate
        self.kanaele = a0.kanaele
        self.bits = a0.bits
        self.align = a0.align

    @property
    def beginn(self):
        return self.abschnitte[0].beginn

    @property
    def ende(self):
        return self.abschnitte[-1].ende_wanduhr

    def dauer_s(self):
        return sum(a.dauer_s() for a in self.abschnitte)

    def takt(self):
        """Gewichteter Takt über alle Abschnitte."""
        gesamt_audio = 0.0
        gesamt_wand = 0.0
        for a in self.abschnitte:
            if len(a.dateien) < 2:
                continue
            gesamt_wand += (a.dateien[-1]["wanduhr"] - a.beginn).total_seconds()
            gesamt_audio += a.dateien[-1]["pos"] / float(a.rate)
        if gesamt_wand <= 0:
            return (None, None)
        return (gesamt_audio / gesamt_wand, 1.0e6 / gesamt_wand)

    def finde(self, t):
        """(Abschnitt, Position) für eine Uhrzeit — oder (None, None)."""
        for a in self.abschnitte:
            p = a.pos_von_wanduhr(t)
            if p is not None:
                return (a, p)
        return (None, None)


def tag_aus_ordner(name):
    m = ORDNER_MUSTER.match(name)
    roh = m.group(1) if m else name
    roh = re.sub(r"[^A-Za-z0-9]+", "", roh).upper()
    return roh or name.upper()


def geraete_finden(pfade):
    """Aufnahmeordner einsammeln und nach Gerät gruppieren."""
    def hat_aufnahmen(pfad):
        # Auf dem Mac sind Ordner wie Schreibtisch oder Dokumente gesperrt;
        # das darf die Suche nicht abbrechen.
        try:
            return any(DATEI_MUSTER.match(x.name) for x in pfad.iterdir()
                       if x.is_file())
        except (OSError, PermissionError):
            return False

    ordner = []
    for p in pfade:
        p = Path(p).expanduser()
        if not p.exists():
            abbruch("Ordner nicht gefunden: %s" % p)
        if hat_aufnahmen(p):
            ordner.append(p)
        else:
            try:
                unter = sorted(p.iterdir())
            except (OSError, PermissionError) as e:
                abbruch("%s ist nicht lesbar: %s" % (p, e))
            for u in unter:
                try:
                    ist_ordner = u.is_dir()
                except OSError:
                    continue
                if ist_ordner and hat_aufnahmen(u):
                    ordner.append(u)
    if not ordner:
        abbruch("keine Aufnahmeordner mit r_*.wav gefunden")

    nach_tag = {}
    for o in ordner:
        try:
            a = Abschnitt(o)
        except ValueError as e:
            warnung(str(e))
            continue
        nach_tag.setdefault(tag_aus_ordner(o.name), []).append(a)
    if not nach_tag:
        abbruch("kein lesbarer Aufnahmeordner")
    return [Geraet(t, a) for t, a in sorted(nach_tag.items())]


# ------------------------------------------------------------- Werkzeuge

def hat(programm):
    return shutil.which(programm) is not None


def pruefe_ffmpeg():
    if not hat("ffmpeg"):
        abbruch("ffmpeg nicht gefunden.  Am Mac:  brew install ffmpeg")


def konkat_datei(dateien, ordner):
    pfad = Path(ordner) / "konkat.txt"
    with open(pfad, "w", encoding="utf-8") as f:
        for d in dateien:
            f.write("file '%s'\n" % str(Path(d).resolve()).replace("'", "'\\''"))
    return pfad


def lauf(cmd, eingabe=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          input=eingabe)


# ------------------------------------------------------------- Messen

ASTATS_ZEILE = re.compile(r"^lavfi\.astats\.(\d+)\.(Peak_level|RMS_level)=(\S+)$")


def blockpegel(abschnitt, q, m, block_frames, fortschritt=None):
    """Spitzen- und Effektivpegel je Kanal und Block über [q, q+m).

    Ein einziger Durchlauf durch das Material; die Rechnung macht ffmpeg.
    Rückgabe: (peak, rms) — je eine Liste [kanal][block] in dBFS.
    """
    ziel = abschnitt.konkat(q, m)
    if ziel is None:
        return (None, None)
    dateien, versatz = ziel
    ganze_s = versatz // abschnitt.rate
    rest = versatz - ganze_s * abschnitt.rate

    tmp = tempfile.mkdtemp(prefix="schnitt-")
    try:
        liste = konkat_datei(dateien, tmp)
        kette = (
            "atrim=start_sample=%d:end_sample=%d,asetpts=N-STARTPTS,"
            "asetnsamples=n=%d:p=0,"
            "astats=metadata=1:reset=1:measure_perchannel=Peak_level+RMS_level:"
            "measure_overall=none,"
            "ametadata=mode=print:file=-"
            % (rest, rest + m, block_frames)
        )
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
               "-ss", str(int(ganze_s)),
               "-t", "%.3f" % ((rest + m) / float(abschnitt.rate) + 1.0),
               "-f", "concat", "-safe", "0", "-i", str(liste),
               "-af", kette, "-f", "null", "-"]

        peak = [[] for _ in range(abschnitt.kanaele)]
        rms = [[] for _ in range(abschnitt.kanaele)]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, bufsize=1 << 20)
        bloecke_soll = max(1, int(math.ceil(m / float(block_frames))))
        gezaehlt = 0
        letzte_meldung = time.time()
        for roh in p.stdout:
            zeile = roh.decode("utf-8", "replace").strip()
            if zeile.startswith("frame:"):
                gezaehlt += 1
                if fortschritt and time.time() - letzte_meldung > 5.0:
                    letzte_meldung = time.time()
                    fortschritt(gezaehlt, bloecke_soll)
                continue
            mm = ASTATS_ZEILE.match(zeile)
            if not mm:
                continue
            kanal = int(mm.group(1)) - 1
            if kanal >= abschnitt.kanaele:
                continue
            try:
                wert = float(mm.group(3))
            except ValueError:
                wert = STILLE_DB
            if not math.isfinite(wert):
                wert = STILLE_DB
            (peak if mm.group(2) == "Peak_level" else rms)[kanal].append(wert)
        p.stdout.close()
        stderr = p.stderr.read().decode("utf-8", "replace")
        p.wait()
        if p.returncode != 0:
            abbruch("ffmpeg beim Messen abgebrochen:\n" + stderr.strip())
        if fortschritt:
            fortschritt(gezaehlt, bloecke_soll)
        return (peak, rms)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ Beurteilen

class Spur(object):
    def __init__(self, kanal, spitze, effektiv, aktiv_s, dauer_s):
        self.kanal = kanal              # 1-basiert
        self.spitze = spitze
        self.effektiv = effektiv
        self.aktiv_s = aktiv_s
        self.dauer_s = dauer_s
        self.belegt = False
        self.verdacht = False
        self.naehe = None               # dB zur lautesten Spur im selben Moment
        self.spitze_bei = None          # Sekunden nach Fensterbeginn
        self.anschlag = False           # Spitze bei 0 dBFS

    @property
    def aktiv_anteil(self):
        return self.aktiv_s / self.dauer_s if self.dauer_s > 0 else 0.0


def beurteile(peak, rms, block_s, schwelle, abstand):
    """Aus Blockpegeln je Kanal entscheiden: belegt / Übersprech-Verdacht.

    BELEGT entscheidet der **Spitzenpegel über das ganze Fenster**. Ein
    einziger Tom-Schlag von einer Sekunde in einem Fünf-Minuten-Stück reicht
    also aus — der Durchschnitt spielt keine Rolle.

    ÜBERSPRECHEN wird **Block für Block** beurteilt, nicht über das ganze
    Fenster: Es zählt, ob die Spur irgendwann einmal nahe an die lauteste
    Spur desselben Moments herankommt. Ein Tom-Mikro tut das im Moment des
    Schlags; eine Spur, die nur das Übersprechen der anderen einfängt, bleibt
    zu jedem Zeitpunkt gleich weit darunter. Über das ganze Fenster gerechnet
    wären beide nicht zu unterscheiden.
    """
    spuren = []
    n = max((len(r) for r in peak), default=0)
    # Lautester Pegel je Zeitblock über alle Spuren
    moment = [STILLE_DB] * n
    for reihe in peak:
        for b, x in enumerate(reihe):
            if x > moment[b]:
                moment[b] = x

    for i, reihe in enumerate(peak):
        if not reihe:
            spuren.append(Spur(i + 1, STILLE_DB, STILLE_DB, 0.0, 0.0))
            continue
        spitze = max(reihe)
        aktiv = sum(1 for x in reihe if x >= schwelle) * block_s
        r = rms[i] if i < len(rms) and rms[i] else []
        # Effektivpegel nur über die aktiven Blöcke: sonst drückt die Stille
        # zwischen den Einsätzen den Wert beliebig weit nach unten.
        laut = [x for j, x in enumerate(r) if j < len(reihe) and reihe[j] >= schwelle]
        if laut:
            effektiv = 10.0 * math.log10(
                sum(10.0 ** (x / 10.0) for x in laut if math.isfinite(x)) / len(laut)
                + 1e-30)
        else:
            effektiv = max(r) if r else STILLE_DB
        s = Spur(i + 1, spitze, effektiv, aktiv, len(reihe) * block_s)
        s.spitze_bei = reihe.index(spitze) * block_s
        naehe = None
        for b, x in enumerate(reihe):
            if x < schwelle or b >= n:
                continue
            d = x - moment[b]
            if naehe is None or d > naehe:
                naehe = d
        s.naehe = naehe
        spuren.append(s)

    for s in spuren:
        s.belegt = s.spitze >= schwelle
        s.verdacht = bool(s.belegt and s.naehe is not None
                          and s.naehe <= -abstand)
        # Spitze am oberen Anschlag: das Pult hat womöglich digital
        # übersteuert. Sicher ist das nicht — ein Signal darf auch
        # zufällig knapp unter Vollaussteuerung landen.
        s.anschlag = bool(s.belegt and s.spitze >= -0.1)
    return spuren


def spuren_tabelle(tag, spuren, schwelle, abstand):
    zeilen = ["  %-4s %8s %8s %9s %7s %8s  %s"
              % ("Kan", "Spitze", "Effektiv", "lauteste", "aktiv", "Nähe",
                 "Urteil")]
    for s in spuren:
        if s.spitze <= STILLE_DB + 1 and not s.belegt:
            urteil = "still"
        elif not s.belegt:
            urteil = "unter Schwelle"
        elif s.verdacht:
            urteil = "BELEGT – Übersprechen? (nie näher als %.0f dB an die " \
                     "lauteste Spur)" % (-s.naehe)
        else:
            urteil = "BELEGT"
        if s.anschlag:
            urteil += " · Spitze am Anschlag"
        zeilen.append("  %-4d %7.1f  %7.1f  %8s %6ss %7s  %s"
                      % (s.kanal, s.spitze, s.effektiv,
                         dauer_text(s.spitze_bei) if s.spitze_bei is not None else "-",
                         ("%.2f" % s.aktiv_s if 0 < s.aktiv_s < 10
                          else "%.0f" % s.aktiv_s),
                         "%+.0f dB" % s.naehe if s.naehe is not None else "-",
                         urteil))
    kopf = ("%s — belegt ab %.0f dBFS Spitze; Übersprech-Verdacht, wenn die "
            "Spur zu keinem Zeitpunkt näher als %.0f dB an die lauteste Spur "
            "desselben Moments herankommt\n"
            "  (\"lauteste\" = Zeitpunkt der lautesten Stelle, gerechnet ab "
            "Fensterbeginn)" % (tag, schwelle, abstand))
    return kopf + "\n" + "\n".join(zeilen)


# ----------------------------------------------------------- Spurauswahl

def knapp_darunter(spuren, schwelle, spanne=15.0):
    """Spuren, die es fast über die Schwelle geschafft hätten.

    Damit keine leise Spur unbemerkt verlorengeht — bei Perkussion, die nur
    einmal im Stück anspricht, ist der Abstand zur Schwelle schnell knapp.
    """
    return [s.kanal for s in spuren
            if not s.belegt and schwelle - spanne <= s.spitze < schwelle]


def parse_spuren(text, geraete):
    """'PULT1:1-18,24; PULT2:1-8'  ->  ({'PULT1': [...]}, {'PULT1': {24}})

    Der zweite Rückgabewert sind die AUSDRÜCKLICH einzeln genannten Spuren.
    Sie werden immer ausgegeben — auch wenn sie in der Namensdatei mit '-'
    abgewählt sind oder unter der Stilleschwelle liegen. Ein Bereich wie
    1-32 zählt nicht als ausdrücklich.
    """
    if not text or text.strip().lower() == "auto":
        return (None, {})
    tags = dict((g.tag, g) for g in geraete)
    raus = {}
    ausdruecklich = {}
    for block in text.split(";"):
        block = block.strip()
        if not block:
            continue
        if ":" not in block:
            raise ValueError("'%s': erwartet wird GERÄT:Spuren" % block)
        tag, _, liste = block.partition(":")
        tag = re.sub(r"[^A-Za-z0-9]+", "", tag).upper()
        if tag not in tags:
            raise ValueError("Gerät '%s' gibt es nicht (bekannt: %s)"
                             % (tag, ", ".join(sorted(tags))))
        g = tags[tag]
        kanaele = []
        genannt = ausdruecklich.setdefault(tag, set())
        for teil in liste.split(","):
            teil = teil.strip().lower()
            if not teil:
                continue
            if teil in ("alle", "all", "*"):
                kanaele.extend(range(1, g.kanaele + 1))
                continue
            m = re.fullmatch(r"(\d+)\s*\+\s*(\d+)", teil)
            if m:                      # 31+32 = ein Stereopaar in EINER Datei
                a_, b_ = int(m.group(1)), int(m.group(2))
                kanaele.append((a_, b_))
                genannt.update((a_, b_))
                continue
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", teil)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if a > b:
                    a, b = b, a
                kanaele.extend(range(a, b + 1))
                continue
            if not teil.isdigit():
                raise ValueError("'%s' ist keine Spurangabe" % teil)
            kanaele.append(int(teil))
            genannt.add(int(teil))
        schlecht = [k for k in flach(kanaele) if k < 1 or k > g.kanaele]
        if schlecht:
            raise ValueError("%s hat nur %d Spuren, verlangt wurde %s"
                             % (tag, g.kanaele, schlecht))
        # Reihenfolge erhalten, Doppelte entfernen — bei Stereopaaren wäre
        # ein simples sorted() sinnlos.
        gesehen = set()
        sauber_liste = []
        for k in kanaele:
            if k not in gesehen:
                gesehen.add(k)
                sauber_liste.append(k)
        raus[tag] = sauber_liste
    return (raus, ausdruecklich)


def flach(kanaele):
    """[1, (31, 32)] -> [1, 31, 32]"""
    aus = []
    for k in kanaele:
        aus.extend(k if isinstance(k, tuple) else [k])
    return aus


def kanal_text(k):
    return "%02d+%02d" % k if isinstance(k, tuple) else "%02d" % k


def erste(k):
    return k[0] if isinstance(k, tuple) else k


# ------------------------------------------------------------ Synchronität

class Sync(object):
    """Abbildung zwischen den Zeitachsen zweier Geräte."""

    def __init__(self, referenz, faktoren, verfahren, unsicherheit_ppm,
                 anker=None, kurve=None, paare=None):
        self.referenz = referenz
        self.faktoren = faktoren          # tag -> Samples je Referenz-Sample
        self.verfahren = verfahren
        self.unsicherheit_ppm = unsicherheit_ppm
        self.anker = anker or {}          # tag -> (ref_sample, geraet_sample)
        # tag -> [[ref_sample, geraet_sample], …] — die einzelnen Messpunkte.
        # Der Takt der Pulte ist NICHT konstant: die Quarze wandern mit der
        # Temperatur. Eine Ausgleichsgerade lässt deshalb einige Millisekunden
        # stehen. Zwischen den Messpunkten zu interpolieren beseitigt das.
        self.kurve = kurve or {}
        self.paare = paare or {}          # tag -> [ref_kanal, geraet_kanal]

    def faktor(self, tag):
        return self.faktoren.get(tag, 1.0)

    def abbildung(self, tag, p):
        """(Sample im Gerät, örtlicher Faktor) für ein Referenz-Sample p."""
        punkte = self.kurve.get(tag)
        if not punkte or len(punkte) < 2:
            anker = self.anker.get(tag)
            f = self.faktor(tag)
            if anker:
                return (int(round(anker[1] + (p - anker[0]) * f)), f)
            return (None, f)
        i = 0
        for j in range(len(punkte) - 1):
            if punkte[j][0] <= p:
                i = j
        # Außerhalb der Messpunkte wird mit der Steigung des Randstücks
        # weitergerechnet — mehr weiß die Messung dort nicht.
        p1, q1 = punkte[i]
        p2, q2 = punkte[i + 1]
        if p2 == p1:
            return (int(round(q1)), self.faktor(tag))
        f = (q2 - q1) / float(p2 - p1)
        return (int(round(q1 + (p - p1) * f)), f)

    @staticmethod
    def aus_datei(pfad):
        d = json.loads(Path(pfad).read_text(encoding="utf-8"))
        return Sync(d["referenz"], d["faktoren"], d["verfahren"],
                    d.get("unsicherheit_ppm"), d.get("anker"),
                    d.get("kurve"), d.get("paare"))

    def speichern(self, pfad):
        Path(pfad).write_text(json.dumps({
            "referenz": self.referenz,
            "faktoren": self.faktoren,
            "verfahren": self.verfahren,
            "unsicherheit_ppm": self.unsicherheit_ppm,
            "anker": self.anker,
            "paare": self.paare,
            "kurve": self.kurve,
            "erzeugt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2, ensure_ascii=False), encoding="utf-8")


def sync_aus_dateinamen(geraete, referenz):
    """Grobe Taktmessung: Audiolänge gegen Uhrzeit in den Dateinamen."""
    takte = {}
    unsicher = 0.0
    for g in geraete:
        t, u = g.takt()
        takte[g.tag] = t
        if u:
            unsicher = max(unsicher, u)
    bezug = takte.get(referenz)
    faktoren = {}
    for tag, t in takte.items():
        if bezug and t:
            faktoren[tag] = t / bezug
        else:
            faktoren[tag] = 1.0
    return Sync(referenz, faktoren, "dateinamen",
                round(unsicher * 1.5, 1) if unsicher else None)


# -------------------------------------------------------------- Schnitt

def bext_optionen(start_wanduhr, rate, beschreibung, historie):
    """BWF-Zeitstempel: Samples seit Mitternacht des Aufnahmetags."""
    seit_mitternacht = (start_wanduhr.hour * 3600 + start_wanduhr.minute * 60
                        + start_wanduhr.second)
    proben = int(round((seit_mitternacht + start_wanduhr.microsecond / 1e6) * rate))
    return [
        "-write_bext", "1",
        "-metadata", "time_reference=%d" % proben,
        "-metadata", "origination_date=%s" % start_wanduhr.strftime("%Y-%m-%d"),
        "-metadata", "origination_time=%s" % start_wanduhr.strftime("%H:%M:%S"),
        "-metadata", "description=%s" % beschreibung[:255],
        "-metadata", "originator=schnitt.py %s" % VERSION,
        "-metadata", "coding_history=%s" % historie,
    ]


def sauber(text):
    """Der Name bleibt, wie er geschrieben wurde.

    macOS trägt Leerzeichen, '&' und Apostroph ohne Weiteres. Ersetzt wird
    nur, was ein Dateisystem wirklich stört: Schrägstriche, Doppelpunkt
    (den zeigt der Finder als '/'), Steuerzeichen. Führende Punkte machen
    die Datei unsichtbar, Leerzeichen am Rand verschwinden lautlos —
    beides fällt weg.

    'Hunter & Me - Seven'  ->  'Hunter & Me - Seven'
    'AC/DC: Live'          ->  'AC-DC - Live'
    """
    t = re.sub(r"[\x00-\x1f\x7f]+", "", text)
    t = t.replace("/", "-").replace("\\", "-")
    t = re.sub(r"\s*:\s*", " - ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip().strip(".").strip() or "ohne-Namen"


def dateiname(tag, kanal, spitze, dauer_s, verdacht, name=None,
              mit_pegel=False):
    """z. B. PULT1-07_Snare.wav — kurz, damit Logic den Namen zeigt.

    Pegel und Dauer stehen ohnehin in der stueck.txt; mit --mit-pegel
    kommen sie wie früher in den Dateinamen:
    PULT1-07_Snare_-08dB_04m12s.wav
    Übersprech-Verdacht hängt hinten als _ueberspr an.
    """
    kern = "%s-%s" % (tag, kanal_text(kanal))
    if name:
        kern += "_" + sauber(name)
    if mit_pegel:
        kern += "_%+03ddB_%s" % (int(round(spitze)), dauer_text(dauer_s))
    if verdacht:
        kern += "_ueberspr"
    return kern + ".wav"


def lade_namen(pfad):
    """Textdatei 'PULT1-07 = Snare' -> {'PULT1-07': 'Snare'}"""
    if not pfad:
        return {}
    p = Path(pfad).expanduser()
    if not p.exists():
        abbruch("Namensdatei nicht gefunden: %s" % p)
    raus = {}
    for z in p.read_text(encoding="utf-8").splitlines():
        z = z.split("#")[0].strip()
        if not z or "=" not in z:
            continue
        k, _, v = z.partition("=")
        k = re.sub(r"\s+", "", k.strip()).upper()
        m = re.fullmatch(r"([A-Z0-9]+)[-_]0*(\d+)\+0*(\d+)", k)
        if m:                      # Stereopaar: PULT2-31+32 = MainOut
            # Ein leerer Name ist erlaubt: dann gilt nur die Paarung.
            raus["%s-%d+%d" % (m.group(1), int(m.group(2)), int(m.group(3)))] \
                = v.strip()
            continue
        m = re.fullmatch(r"([A-Z0-9]+)[-_]0*(\d+)", k)
        if m and v.strip():
            raus["%s-%d" % (m.group(1), int(m.group(2)))] = v.strip()
    return raus


AUSLASSEN = {"-", "--", "aus", "weg", "nein", "nicht", "x"}


def auslassen(namen, tag):
    """Spuren, die in der Namensdatei mit '-' abgewählt wurden."""
    raus = set()
    for k, v in (namen or {}).items():
        if v.strip().lower() not in AUSLASSEN:
            continue
        m = re.fullmatch(r"([A-Z0-9]+)-(\d+)(?:\+(\d+))?", k)
        if m and m.group(1) == tag.upper():
            raus.add(int(m.group(2)))
            if m.group(3):
                raus.add(int(m.group(3)))
    return raus


def paare_aus_namen(namen, tag):
    """Welche Spurpaare hat die Namensdatei für dieses Gerät erklärt?"""
    aus = []
    for k in (namen or {}):
        m = re.fullmatch(r"([A-Z0-9]+)-(\d+)\+(\d+)", k)
        if m and m.group(1) == tag.upper():
            aus.append((int(m.group(2)), int(m.group(3))))
    return aus


def paare_anwenden(kanaele, tag, namen):
    """Aus [5,6,9,12,13] und der Erklärung 5+6, 12+13 wird [(5,6),9,(12,13)].

    Nur wenn BEIDE Spuren des Paares belegt sind; sonst bleibt die eine mono.
    """
    offen = list(kanaele)
    aus = []
    paare = paare_aus_namen(namen, tag)
    for k in kanaele:
        if k not in offen:
            continue
        partner = None
        for a, b in paare:
            if k == a and b in offen:
                partner = b
                break
            if k == b and a in offen:
                partner = a
                break
        if partner is None:
            aus.append(k)
            offen.remove(k)
        else:
            lo, hi = (k, partner) if k < partner else (partner, k)
            aus.append((lo, hi))
            offen.remove(k)
            offen.remove(partner)
    return aus


def schneide_geraet(geraet, abschnitt, q, m_quelle, n_ziel, kanaele, spuren,
                    faktor, ziel_ordner, ausgabe_format, start_wanduhr,
                    trocken=False, namen=None, mit_pegel=False):
    """Ein Gerät -> viele Mono-Dateien.  Ein Durchlauf über die Quelle.

    q, m_quelle in Samples des Geräts; n_ziel in Samples der Referenz.
    faktor = Samples des Geräts je Referenz-Sample (1.0 = kein Versatz).
    """
    ziel = abschnitt.konkat(q, m_quelle)
    if ziel is None:
        return ([], "Fenster liegt außerhalb der Aufnahme von %s" % geraet.tag)
    dateien, versatz = ziel
    ganze_s = versatz // abschnitt.rate
    rest = versatz - ganze_s * abschnitt.rate

    codec, breite = AUSGABE_FORMATE[ausgabe_format]
    drift = abs(faktor - 1.0) > 1e-9

    tmp = tempfile.mkdtemp(prefix="schnitt-")
    erzeugt = []
    try:
        liste = konkat_datei(dateien, tmp)

        # Schritt 1: Fenster sample-genau ausschneiden.
        kette1 = ("atrim=start_sample=%d:end_sample=%d,asetpts=N-STARTPTS"
                  % (rest, rest + m_quelle))
        vorne = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
                 "-ss", str(int(ganze_s)),
                 "-t", "%.3f" % ((rest + m_quelle) / float(abschnitt.rate) + 1.0),
                 "-f", "concat", "-safe", "0", "-i", str(liste)]

        # Schritt 2 (nur bei Drift): sox rechnet den Takt um.
        # Schritt 3: auf genau n_ziel Samples bringen und in Mono zerlegen.
        teile = []
        for idx, k in enumerate(kanaele):
            if isinstance(k, tuple):
                teile.append("[s%d]pan=stereo|c0=c%d|c1=c%d[o%d]"
                             % (idx, k[0] - 1, k[1] - 1, idx))
            else:
                teile.append("[s%d]pan=mono|c0=c%d[o%d]" % (idx, k - 1, idx))
        kette2 = ("apad=whole_len=%d,atrim=end_sample=%d,asetpts=N-STARTPTS,"
                  "asplit=%d%s;%s"
                  % (n_ziel, n_ziel, len(kanaele),
                     "".join("[s%d]" % i for i in range(len(kanaele))),
                     ";".join(teile)))

        ausgaben = []
        for idx, k in enumerate(kanaele):
            s = spuren[erste(k) - 1]
            if isinstance(k, tuple):
                s2 = spuren[k[1] - 1]
                s = s if s.spitze >= s2.spitze else s2
            klar = (namen or {}).get("%s-%s" % (geraet.tag, kanal_text(k)
                                                 .lstrip("0").replace("+0", "+")))
            if klar is None and isinstance(k, tuple):
                klar = (namen or {}).get("%s-%d+%d" % (geraet.tag, k[0], k[1]))
            if klar is None:
                klar = (namen or {}).get("%s-%d" % (geraet.tag, erste(k)))
            if klar is not None and klar.strip().lower() in AUSLASSEN:
                klar = None
            name = dateiname(geraet.tag, k, s.spitze,
                             n_ziel / float(abschnitt.rate), s.verdacht, klar,
                             mit_pegel)
            pfad = Path(ziel_ordner) / name
            beschreibung = ("%s Spur %s%s | %s | Spitze %.1f dBFS%s"
                            % (geraet.tag, kanal_text(k),
                               " " + klar if klar else "",
                               uhr_text(start_wanduhr), s.spitze,
                               " | Uebersprech-Verdacht" if s.verdacht else ""))
            historie = ("A=PCM,F=%d,W=%d,M=%s,T=schnitt.py %s%s"
                        % (abschnitt.rate, breite * 8,
                           "stereo" if isinstance(k, tuple) else "mono", VERSION,
                           ",speed=%.9f" % faktor if drift else ""))
            ausgaben += (["-map", "[o%d]" % idx, "-c:a", codec]
                         + bext_optionen(start_wanduhr, abschnitt.rate,
                                         beschreibung, historie)
                         + ["-rf64", "auto", str(pfad)])
            erzeugt.append(pfad)

        if drift:
            if not hat("sox"):
                return ([], "sox fehlt, Driftkorrektur nicht möglich "
                            "(brew install sox) — oder --ohne-drift benutzen")
            # Rohdaten statt WAV durch die Rohre: sonst muesste sox eine
            # Laenge in einen Kopf schreiben, den es nicht mehr erreichen
            # kann, und beide Seiten warnen darueber.
            roh = ["-t", "raw", "-r", str(abschnitt.rate), "-e",
                   "signed-integer", "-b", "32", "-c", str(abschnitt.kanaele)]
            cmd_a = vorne + ["-af", kette1, "-f", "s32le", "-"]
            cmd_b = ["sox", "-V1"] + roh + ["-"] + roh + ["-",
                     "speed", "%.9f" % faktor, "rate", "-v", "-s",
                     str(abschnitt.rate)]
            cmd_c = (["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
                      "-f", "s32le", "-ar", str(abschnitt.rate),
                      "-ac", str(abschnitt.kanaele), "-i", "-",
                      "-filter_complex", kette2] + ausgaben)
            if trocken:
                return (erzeugt, " | ".join(" ".join(c) for c in (cmd_a, cmd_b, cmd_c)))
            pa = subprocess.Popen(cmd_a, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            pb = subprocess.Popen(cmd_b, stdin=pa.stdout,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pa.stdout.close()
            pc = subprocess.Popen(cmd_c, stdin=pb.stdout, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            pb.stdout.close()
            aus_c = pc.communicate()[1].decode("utf-8", "replace")
            aus_b = pb.stderr.read().decode("utf-8", "replace")
            aus_a = pa.stderr.read().decode("utf-8", "replace")
            pa.wait()
            pb.wait()
            if pa.returncode or pb.returncode or pc.returncode:
                return ([], "Kette ffmpeg|sox|ffmpeg abgebrochen:\n"
                            + "\n".join(x.strip() for x in (aus_a, aus_b, aus_c) if x.strip()))
        else:
            cmd = vorne + ["-filter_complex", kette1 + "," + kette2] + ausgaben
            if trocken:
                return (erzeugt, " ".join(cmd))
            e = lauf(cmd)
            if e.returncode != 0:
                return ([], "ffmpeg abgebrochen:\n"
                            + e.stderr.decode("utf-8", "replace").strip())
        return (erzeugt, None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- Karte

def karte_bauen(geraete, block_s, ziel):
    daten = {"version": 1, "block_s": block_s, "erzeugt":
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "geraete": {}}
    for g in geraete:
        block_frames = max(1, int(round(block_s * g.rate)))
        sag("  %s: %d Kanäle, %d Hz, %d Bit, %s bis %s (%.2f h)"
            % (g.tag, g.kanaele, g.rate, g.bits, uhr_text(g.beginn),
               uhr_text(g.ende), g.dauer_s() / 3600.0))
        alle_peak = [[] for _ in range(g.kanaele)]
        alle_rms = [[] for _ in range(g.kanaele)]
        for a in g.abschnitte:
            def zeige(ist, soll, _t=g.tag):
                sag("      %s: %d von %d Blöcken (%.0f %%)"
                    % (_t, ist, soll, 100.0 * ist / max(1, soll)))
            peak, rms = blockpegel(a, 0, a.frames, block_frames, zeige)
            if peak is None:
                continue
            for k in range(g.kanaele):
                alle_peak[k].extend(peak[k] if k < len(peak) else [])
                alle_rms[k].extend(rms[k] if k < len(rms) else [])
        pruef = s32_ist_24bit(g.abschnitte[0].dateien[0]["pfad"],
                              wav_kopf(g.abschnitte[0].dateien[0]["pfad"]))
        daten["geraete"][g.tag] = {
            "ordner": [str(a.ordner) for a in g.abschnitte],
            "rate": g.rate, "kanaele": g.kanaele, "bits": g.bits,
            "beginn": g.beginn.strftime("%Y-%m-%d %H:%M:%S"),
            "ende": g.ende.strftime("%Y-%m-%d %H:%M:%S"),
            "dauer_s": round(g.dauer_s(), 3),
            "takt": g.takt()[0],
            "takt_unsicherheit_ppm": g.takt()[1],
            "abschnitte": [{"ordner": a.ordner.name,
                            "beginn": a.beginn.strftime("%Y-%m-%d %H:%M:%S"),
                            "dateien": len(a.dateien),
                            "frames": a.frames} for a in g.abschnitte],
            "unterstes_byte": {"geprueft": pruef[0], "gesetzt": pruef[1]},
            "peak": [[int(round(x * DB_SKALA)) for x in reihe] for reihe in alle_peak],
            "rms": [[int(round(x * DB_SKALA)) for x in reihe] for reihe in alle_rms],
        }
    Path(ziel).write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")
    return daten


def karte_laden(pfad):
    d = json.loads(Path(pfad).read_text(encoding="utf-8"))
    return d


def karte_fenster(karte, geraet, von_dt, dauer_s):
    """Blockpegel aus der Karte für ein Zeitfenster ausschneiden.

    Die Blöcke sind nach Sample-Position abgelegt, nicht nach Uhrzeit — über
    einen langen Abend laufen beide um Sekunden auseinander. Deshalb wird die
    Uhrzeit über dieselbe Abbildung umgerechnet, die auch der Schnitt benutzt.
    """
    eintrag = karte["geraete"].get(geraet.tag)
    if not eintrag:
        return (None, None)
    block_s = karte["block_s"]
    abschnitt, q = geraet.finde(von_dt)
    if abschnitt is None:
        return (None, None)
    vorher = 0
    for a in geraet.abschnitte:
        if a is abschnitt:
            break
        vorher += a.frames
    sekunden = (vorher + q) / float(geraet.rate)
    ab = max(0, int(math.floor(sekunden / block_s)))
    bis = int(math.ceil((sekunden + dauer_s) / block_s))
    if bis <= ab:
        return (None, None)
    peak = [[x / float(DB_SKALA) for x in reihe[ab:bis]] for reihe in eintrag["peak"]]
    rms = [[x / float(DB_SKALA) for x in reihe[ab:bis]] for reihe in eintrag["rms"]]
    if not peak or not peak[0]:
        return (None, None)
    return (peak, rms)


def farbe(db):
    """dBFS -> Hintergrundfarbe für das Wärmebild."""
    if db <= -70:
        return "#12121a"
    x = max(0.0, min(1.0, (db + 70.0) / 70.0))
    r = int(20 + 235 * min(1.0, x * 1.6))
    gr = int(20 + 200 * max(0.0, min(1.0, (x - 0.25) * 1.5)))
    b = int(40 + 60 * max(0.0, 1.0 - x * 2))
    return "#%02x%02x%02x" % (r, gr, b)


def karte_html(karte, ziel, raster_s=60.0, schwelle=-50.0, stuecke=None):
    block_s = karte["block_s"]
    je = max(1, int(round(raster_s / block_s)))
    teile = ["""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<title>Karte des Abends</title><style>
body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#15151c;
     color:#e8e8ef;margin:0;padding:18px}
h1{font-size:19px;margin:0 0 4px} h2{font-size:15px;margin:22px 0 6px}
p{font-size:12.5px;color:#a9a9bb;margin:3px 0}
table{border-collapse:collapse;margin:8px 0 2px}
td,th{padding:0;font-size:9px}
th.k{width:44px;text-align:right;padding-right:6px;color:#b9bac6;font-weight:normal}
td.z{width:5px;height:11px;border-right:1px solid #15151c}
tr.belegt th.k{color:#fff;font-weight:bold}
td.st{height:13px;border-right:1px solid #15151c}
td.st.an{background:#2f6fb5;color:#fff;font-size:8px;text-align:center}
.zeit td{color:#7d7d90;font-size:9px;height:14px;text-align:left;
         white-space:nowrap;vertical-align:top}
.leg{margin-top:10px;font-size:11.5px;color:#a9a9bb}
.leg span{display:inline-block;width:16px;height:11px;margin:0 2px -1px 10px}
</style></head><body>
<h1>Karte des Abends</h1>"""]
    teile.append('<p>Erzeugt %s · ein Kästchen = %.0f s · Farbe = Spitzenpegel'
                 ' im Kästchen</p>' % (karte.get("erzeugt", ""), raster_s))

    for tag, g in sorted(karte["geraete"].items()):
        beginn = datetime.strptime(g["beginn"], "%Y-%m-%d %H:%M:%S")
        peak = g["peak"]
        n = max((len(r) for r in peak), default=0)
        spalten = int(math.ceil(n / float(je))) if n else 0
        teile.append("<h2>%s — %d Kanäle, %d Hz, %d Bit</h2>"
                     % (tag, g["kanaele"], g["rate"], g["bits"]))
        teile.append('<p>%s bis %s · %.2f h</p>'
                     % (g["beginn"][11:], g["ende"][11:], g["dauer_s"] / 3600.0))
        teile.append("<table>")
        # Zeitleiste
        teile.append('<tr class="zeit"><td></td>')
        for c in range(spalten):
            t = beginn + timedelta(seconds=c * je * block_s)
            teile.append("<td>%s</td>" % (t.strftime("%H:%M") if c % 5 == 0 else ""))
        teile.append("</tr>")
        # Erkannte Stuecke als Streifen
        if stuecke:
            teile.append('<tr><th class="k">Stück</th>')
            for c in range(spalten):
                t0 = beginn + timedelta(seconds=c * je * block_s)
                t1 = t0 + timedelta(seconds=je * block_s)
                nr = 0
                for i, (sv, sd) in enumerate(stuecke, 1):
                    if sv < t1 and sv + timedelta(seconds=sd) > t0:
                        nr = i
                        break
                teile.append('<td class="st%s">%s</td>'
                             % (" an" if nr else "",
                                str(nr) if nr and (c == 0 or not (
                                    stuecke[nr - 1][0] < t0)) else ""))
            teile.append("</tr>")
        for k in range(g["kanaele"]):
            reihe = peak[k] if k < len(peak) else []
            spitze = max(reihe) / float(DB_SKALA) if reihe else STILLE_DB
            belegt = spitze >= schwelle
            teile.append('<tr class="%s"><th class="k">%d</th>'
                         % ("belegt" if belegt else "", k + 1))
            for c in range(spalten):
                stueck = reihe[c * je:(c + 1) * je]
                db = max(stueck) / float(DB_SKALA) if stueck else STILLE_DB
                teile.append('<td class="z" style="background:%s"></td>' % farbe(db))
            teile.append("</tr>")
        teile.append("</table>")
    teile.append('<p class="leg">Pegel:'
                 + "".join('<span style="background:%s"></span>%d dB' % (farbe(d), d)
                           for d in (-70, -50, -35, -20, -10, 0))
                 + "</p></body></html>")
    Path(ziel).write_text("".join(teile), encoding="utf-8")


def stuecke_vorschlagen(karte, schwelle, pause_s, mindest_s, vorlauf, nachlauf,
                        dyn=20.0, anteil=0.35, immer=0.7, mindestspuren=2,
                        bericht=None):
    """Stücke finden — nicht am Pegel allein, sondern daran, wie viele
    Spuren gleichzeitig arbeiten.

    Ein Publikums- oder Raummikrofon ist den ganzen Abend über laut; ein
    reiner Pegelvergleich hält deshalb den kompletten Abend für ein Stück.
    Spielt dagegen eine Band, sind viele Spuren gleichzeitig aktiv.
    """
    block_s = karte["block_s"]
    masken = []                     # (Beginn, Liste von 0/1, Name)
    dauerlaeufer = []
    for tag, g in sorted(karte["geraete"].items()):
        beginn = datetime.strptime(g["beginn"], "%Y-%m-%d %H:%M:%S")
        for k, reihe in enumerate(g["peak"], 1):
            if not reihe:
                continue
            hoch = max(reihe) / float(DB_SKALA)
            if hoch < schwelle:
                continue
            grenze = max(schwelle, hoch - dyn) * DB_SKALA
            maske = [1 if x >= grenze else 0 for x in reihe]
            wach = sum(maske) / float(len(maske))
            # Spuren, die praktisch den ganzen Abend laufen — Publikumsmikros,
            # Summen, Main Out — sagen nichts darüber aus, WANN ein Stück
            # läuft. Sie werden gezählt, aber nicht zur Entscheidung benutzt.
            if wach > immer:
                dauerlaeufer.append("%s-%d" % (tag, k))
                continue
            masken.append((beginn, maske))
    if bericht is not None:
        bericht.extend([len(masken), dauerlaeufer])
    if not masken:
        return []

    start = min(b for b, _ in masken)
    ende = max(b + timedelta(seconds=len(m) * block_s) for b, m in masken)
    n = int(math.ceil((ende - start).total_seconds() / block_s))
    zaehler = [0] * n
    for b, m in masken:
        versatz = int(round((b - start).total_seconds() / block_s))
        for i, x in enumerate(m):
            j = versatz + i
            if x and 0 <= j < n:
                zaehler[j] += 1

    noetig = max(mindestspuren, int(math.ceil(anteil * len(masken))))
    laut = [z >= noetig for z in zaehler]
    pause_bloecke = max(1, int(round(pause_s / block_s)))
    stuecke = []
    i = 0
    while i < n:
        if not laut[i]:
            i += 1
            continue
        j = i
        ruhe = 0
        k = i
        while k < n:
            if laut[k]:
                j = k
                ruhe = 0
            else:
                ruhe += 1
                if ruhe >= pause_bloecke:
                    break
            k += 1
        if (j - i + 1) * block_s >= mindest_s:
            von = start + timedelta(seconds=max(0.0, i * block_s - vorlauf))
            dauer = (j - i + 1) * block_s + vorlauf + nachlauf
            stuecke.append((von, dauer))
        i = k + 1
    return stuecke


# ------------------------------------------------------- Kreuzkorrelation

def _numpy():
    try:
        import numpy
        return numpy
    except ImportError:
        return None


def lies_mono(abschnitt, q, m, kanal, ziel_rate):
    """Ein Kanal als Fließkomma-Liste, heruntergetaktet auf ziel_rate."""
    np = _numpy()
    ziel = abschnitt.konkat(q, m)
    if ziel is None:
        return None
    dateien, versatz = ziel
    ganze_s = versatz // abschnitt.rate
    rest = versatz - ganze_s * abschnitt.rate
    tmp = tempfile.mkdtemp(prefix="schnitt-")
    try:
        liste = konkat_datei(dateien, tmp)
        # Kein resampler=soxr: für eine Korrelation ist die Qualität des
        # Herunterrechnens gleichgültig, und nicht jede ffmpeg-Fassung hat
        # libsoxr eingebaut.
        kette = ("atrim=start_sample=%d:end_sample=%d,asetpts=N-STARTPTS,"
                 "pan=mono|c0=c%d,aresample=%d"
                 % (rest, rest + m, kanal - 1, ziel_rate))
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
               "-ss", str(int(ganze_s)),
               "-t", "%.3f" % ((rest + m) / float(abschnitt.rate) + 1.0),
               "-f", "concat", "-safe", "0", "-i", str(liste),
               "-af", kette, "-f", "f32le", "-"]
        e = lauf(cmd)
        if e.returncode != 0:
            text = e.stderr.decode("utf-8", "replace").strip()
            warnung_einmal("ffmpeg konnte eine Einzelspur nicht lesen: %s"
                           % (text.splitlines()[-1] if text else "ohne Meldung"))
            return None
        werte = np.frombuffer(e.stdout, dtype="<f4").astype("float64")
        if len(werte) < 100:
            warnung_einmal("ffmpeg lieferte für eine Einzelspur nur %d Proben "
                           "— erwartet waren rund %d"
                           % (len(werte), int(m / float(abschnitt.rate)
                                              * ziel_rate)))
        return werte
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def lies_alle(abschnitt, q, m, ziel_rate):
    """Alle Kanäle eines Fensters in EINEM Durchlauf, heruntergetaktet.

    Ein Aufruf je Kanal würde das Material n-mal durch ffmpeg schicken; bei
    20 Spuren und 20 Fenstern sind das schnell hundert Gigabyte.
    """
    np = _numpy()
    ziel = abschnitt.konkat(q, m)
    if ziel is None:
        return None
    dateien, versatz = ziel
    ganze_s = versatz // abschnitt.rate
    rest = versatz - ganze_s * abschnitt.rate
    tmp = tempfile.mkdtemp(prefix="schnitt-")
    try:
        liste = konkat_datei(dateien, tmp)
        kette = ("atrim=start_sample=%d:end_sample=%d,asetpts=N-STARTPTS,"
                 "aresample=%d" % (rest, rest + m, ziel_rate))
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
               "-ss", str(int(ganze_s)),
               "-t", "%.3f" % ((rest + m) / float(abschnitt.rate) + 1.0),
               "-f", "concat", "-safe", "0", "-i", str(liste),
               "-af", kette, "-f", "f32le", "-"]
        e = lauf(cmd)
        if e.returncode != 0:
            text = e.stderr.decode("utf-8", "replace").strip()
            warnung_einmal("ffmpeg konnte das Fenster nicht lesen: %s"
                           % (text.splitlines()[-1] if text else "ohne Meldung"))
            return None
        roh = np.frombuffer(e.stdout, dtype="<f4")
        ch = abschnitt.kanaele
        n = len(roh) // ch
        if n < 100:
            return None
        return roh[:n * ch].reshape(n, ch).astype("float64")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def korreliere(a, b, np, max_versatz):
    """Bester Versatz von b gegen a (in Samples) und Güte 0..1."""
    n = int(2 ** math.ceil(math.log2(len(a) + len(b))))
    a = a - a.mean()
    b = b - b.mean()
    na = math.sqrt(float((a * a).sum()))
    nb = math.sqrt(float((b * b).sum()))
    if na <= 0 or nb <= 0:
        return (0, 0.0)
    A = np.fft.rfft(a, n)
    B = np.fft.rfft(b, n)
    r = np.fft.irfft(A * np.conj(B), n)
    r = np.concatenate((r[-max_versatz:], r[:max_versatz + 1]))
    i = int(np.argmax(np.abs(r)))
    guete = float(abs(r[i]) / (na * nb))
    return (i - max_versatz, guete)


# ------------------------------------------------------------ Befehle

def gemeinsame_geraete(args):
    pruefe_ffmpeg()
    geraete = geraete_finden(args.ordner)
    if args.referenz:
        tag = re.sub(r"[^A-Za-z0-9]+", "", args.referenz).upper()
        if tag not in [g.tag for g in geraete]:
            abbruch("Referenzgerät '%s' nicht gefunden (bekannt: %s)"
                    % (tag, ", ".join(g.tag for g in geraete)))
        geraete.sort(key=lambda g: 0 if g.tag == tag else 1)
    return geraete


def hole_sync(args, geraete):
    ref = geraete[0].tag
    pfad = Path(args.ziel) / "sync.json"
    if getattr(args, "ohne_drift", False):
        return Sync(ref, dict((g.tag, 1.0) for g in geraete), "abgeschaltet", None)
    if pfad.exists():
        try:
            s = Sync.aus_datei(pfad)
            if s.referenz == ref:
                return s
            warnung("sync.json hat eine andere Referenz (%s) — wird ignoriert"
                    % s.referenz)
        except (ValueError, KeyError) as e:
            warnung("sync.json unlesbar (%s) — Rückfall auf die Dateinamen" % e)
    elif len(geraete) > 1:
        s = sync_sicherstellen(args, geraete)
        if s is not None:
            return s
        warnung("%s fehlt — Zuordnung nur über die Dateinamen, also auf "
                "1 s genau." % pfad)
    return sync_aus_dateinamen(geraete, ref)


def fenster_bestimmen(args, geraete):
    """(Start-Uhrzeit als datetime, Dauer in Sekunden)"""
    sek = parse_uhrzeit(args.von)
    if args.bis and args.dauer:
        abbruch("--dauer und --bis schließen sich aus")
    if args.bis:
        sek_bis = parse_uhrzeit(args.bis)
        dauer = sek_bis - sek
        if dauer <= 0:
            dauer += 24 * 3600.0
    elif args.dauer:
        dauer = parse_dauer(args.dauer)
    else:
        abbruch("--dauer oder --bis fehlt")
    tag0 = geraete[0].beginn
    von = tag0.replace(hour=0, minute=0, second=0, microsecond=0) + \
        timedelta(seconds=sek)
    if von < tag0 - timedelta(hours=1):
        von += timedelta(days=1)         # nach Mitternacht
    return (von, dauer)


def analysiere_fenster(g, von, dauer, args, karte):
    """Spuren-Beurteilung für ein Gerät und Fenster (aus Karte oder frisch)."""
    if karte and g.tag in karte.get("geraete", {}):
        peak, rms = karte_fenster(karte, g, von, dauer)
        if peak:
            return (beurteile(peak, rms, karte["block_s"], args.schwelle,
                              args.abstand), "Karte")
    a, q = g.finde(von)
    if a is None:
        return (None, None)
    m = min(int(round(dauer * a.rate)), a.frames - q)
    if m <= 0:
        return (None, None)
    block_frames = max(1, int(round(args.block * a.rate)))
    peak, rms = blockpegel(a, q, m, block_frames)
    if peak is None:
        return (None, None)
    return (beurteile(peak, rms, args.block, args.schwelle, args.abstand),
            "Messung")


def karte_passt(karte, geraete):
    """Gehört die gespeicherte Karte noch zu den Dateien auf der Platte?

    Wird zwischen zwei Läufen gekürzt, oder kommt ein Ordner dazu, stimmen
    die Blocknummern nicht mehr: die Karte legt die Blöcke nach
    Sample-Position ab, und jede Uhrzeit landet dann auf einem falschen
    Block. Das fällt nicht auf — es kommen einfach die Pegel einer anderen
    Stelle des Abends heraus, und die Spurauswahl wird still falsch.
    Zurück kommt None, wenn alles passt, sonst der Grund.
    """
    if not karte:
        return None
    for g in geraete:
        e = (karte.get("geraete") or {}).get(g.tag)
        if not e:
            return "%s kommt in der Karte nicht vor" % g.tag
        alt = e.get("abschnitte") or []
        if len(alt) != len(g.abschnitte):
            return ("%s hat jetzt %d Aufnahmeordner, in der Karte stehen %d"
                    % (g.tag, len(g.abschnitte), len(alt)))
        for a, b_ in zip(g.abschnitte, alt):
            if len(a.dateien) != b_.get("dateien"):
                return ("%s/%s: jetzt %d Dateien, in der Karte %s"
                        % (g.tag, a.ordner.name, len(a.dateien),
                           b_.get("dateien")))
            if a.frames != b_.get("frames"):
                return ("%s/%s: jetzt %d Frames, in der Karte %s"
                        % (g.tag, a.ordner.name, a.frames, b_.get("frames")))
    return None


def karte_sicherstellen(args, geraete):
    """karte.json laden — und neu bauen, wenn sie fehlt oder nicht passt."""
    ziel = Path(args.ziel)
    kp = ziel / "karte.json"
    frisch = getattr(args, "frisch", False)
    grund = None
    if kp.exists() and not frisch:
        karte = karte_laden(kp)
        grund = karte_passt(karte, geraete)
        if grund is None:
            return karte
        warnung("die Karte passt nicht mehr zu den Dateien: %s" % grund)
        warnung("Sie wird neu gemessen — sonst kämen die Pegel einer "
                "anderen Stelle des Abends heraus.")
    if getattr(args, "kein_auto", False):
        if grund:
            warnung("mit --kein-auto bleibt die unpassende Karte stehen; "
                    "die Spurauswahl kann falsch sein")
            return karte_laden(kp)
        return None
    ziel.mkdir(parents=True, exist_ok=True)
    sag("")
    if frisch and kp.exists():
        sag("%s wird neu gemessen (--frisch, einige Minuten)." % kp)
    elif grund is None:
        sag("%s fehlt — wird einmalig angelegt (einige Minuten, --kein-auto "
            "schaltet es ab)." % kp)
    karte = karte_bauen(geraete, args.block, kp)
    sag("Karte angelegt: %s" % kp)
    sag("")
    return karte


def sync_sicherstellen(args, geraete):
    """sync.json besorgen — notfalls selbst messen."""
    if len(geraete) < 2 or getattr(args, "kein_auto", False):
        return None
    sag("")
    sag("sync.json fehlt — Taktversatz wird einmalig gemessen "
        "(--kein-auto schaltet es ab).")
    a = sync_args(args)
    try:
        return sync_messen(a, geraete)
    except SystemExit:
        raise
    except Exception as e:
        warnung("Die Messung ist gescheitert (%s) — es bleibt bei der "
                "Zuordnung über die Dateinamen." % e)
        return None


def cmd_karte(args):
    geraete = gemeinsame_geraete(args)
    ziel = Path(args.ziel)
    ziel.mkdir(parents=True, exist_ok=True)
    kp = ziel / "karte.json"
    if kp.exists() and not args.frisch:
        karte = karte_laden(kp)
        fehlt = [g.tag for g in geraete if g.tag not in karte.get("geraete", {})]
        if fehlt:
            warnung("in der vorhandenen Messung fehlen: %s — es wird neu "
                    "gemessen" % ", ".join(fehlt))
            karte = None
        else:
            sag("Vorhandene Messung wird benutzt (%s, Raster %.2f s)."
                % (kp, karte["block_s"]))
            sag("Nur die Auswertung wird neu gerechnet — für eine neue Messung")
            sag("den Befehl mit --frisch aufrufen.")
    else:
        karte = None
    neu_gemessen = karte is None
    if neu_gemessen:
        sag("Messe den ganzen Abend durch — das dauert, es wird alles gelesen.")
        karte = karte_bauen(geraete, args.block, kp)
    bericht = []
    stuecke = stuecke_vorschlagen(karte, args.stueck_schwelle, args.pause,
                                  args.mindest, args.vorlauf, args.nachlauf,
                                  args.dyn, args.anteil, args.immer,
                                  args.mindestspuren, bericht)
    karte_html(karte, ziel / "karte.html", args.raster, args.schwelle, stuecke)

    sag("")
    sag("Geräte")
    for g in geraete:
        t, u = g.takt()
        n = g.abschnitte[0].notiz
        sag("  %-8s %2d Kanäle · %d Hz · %d Bit%s"
            % (g.tag, g.kanaele, g.rate, g.bits,
               " · laut aufnahme.txt: %s" % n.get("format", "") if n.get("format") else ""))
        sag("           %s bis %s (%.2f h), %d Datei(en) in %d Ordner(n)"
            % (uhr_text(g.beginn), uhr_text(g.ende), g.dauer_s() / 3600.0,
               sum(len(a.dateien) for a in g.abschnitte), len(g.abschnitte)))
        if t:
            sag("           Takt gegen die Uhr des Pi: %+.1f ppm (±%.0f ppm)"
                % ((t - 1.0) * 1e6, u))
        d = karte["geraete"][g.tag]["unterstes_byte"]
        if d["geprueft"]:
            if d["gesetzt"] == 0:
                sag("           unterstes Byte immer 0 → in der 32-Bit-Datei "
                    "steckt echtes 24-Bit-Material; Ausgabe in 24 Bit ist "
                    "verlustfrei")
            else:
                sag("           unterstes Byte in %d von %d Proben gesetzt → "
                    "echtes 32-Bit-Material, 24 Bit würde runden"
                    % (d["gesetzt"], d["geprueft"]))
        for a in g.abschnitte:
            spruenge = [(n_, v) for n_, v in a.luecken() if abs(v) > 2.0]
            if spruenge:
                warnung("%s: %d Datei(en) weichen um mehr als 2 s von der "
                        "Zeitachse ab, größte Abweichung %+.1f s"
                        % (a.ordner.name, len(spruenge),
                           max(v for _, v in spruenge)))

    ref = geraete[0].tag
    sync = sync_aus_dateinamen(geraete, ref)
    sag("")
    sag("Taktversatz zwischen den Geräten (aus Dateinamen, Referenz %s)" % ref)
    for g in geraete[1:]:
        f = sync.faktor(g.tag)
        sag("  %-8s %+.1f ppm — nach 1 h %+.2f s, nach 4,5 h %+.2f s  (±%s ppm)"
            % (g.tag, (f - 1.0) * 1e6, (f - 1.0) * 3600, (f - 1.0) * 16200,
               ppm_text(sync.unsicherheit_ppm)))
    sag("  Genau wird das erst mit 'schnitt.py sync' — dafür muss dasselbe")
    sag("  Signal in beiden Aufnahmen liegen.")

    vorschlag = ziel / "stuecke-vorschlag.txt"
    zeilen = ["# Vorschlag aus der Karte — Zeiten prüfen und Namen eintragen.",
              "# Spalten: Startzeit  Dauer  Name",
              "# Dauer: 6:30 = 6 min 30 s · 1:20:00 = 1 h 20 min · 120 = 120 min",
              "# Eine Dauer von '-' heißt: bis zum Beginn der nächsten Zeile.",
              "#"]
    for i, (von, dauer) in enumerate(stuecke, 1):
        zeilen.append("%-10s %-9s %02d Stueck" % (uhr_text(von),
                                                  dauer_hms(dauer), i))
    vorschlag.write_text("\n".join(zeilen) + "\n", encoding="utf-8")

    sag("")
    if bericht:
        sag("Stückerkennung: %d Spuren zählen mit." % bericht[0])
        if bericht[1]:
            sag("  Nicht mitgezählt (über %.0f %% des Abends aktiv): %s"
                % (args.immer * 100, ", ".join(bericht[1])))
        sag("  Ein Stück läuft ab %d gleichzeitig aktiven Spuren "
            "(--mindestspuren, --anteil, --immer)."
            % max(args.mindestspuren,
                  int(math.ceil(args.anteil * bericht[0]))))
    sag("Gefundene laute Abschnitte: %d (ab %.0f dBFS, Pause ab %.0f s, "
        "mindestens %.0f s)" % (len(stuecke), args.stueck_schwelle, args.pause,
                                args.mindest))
    luecken = []
    for i, (von, dauer) in enumerate(stuecke, 1):
        marke = ""
        if i < len(stuecke):
            pause = (stuecke[i][0] - von).total_seconds() - dauer
            luecken.append(pause)
            if pause < 90:
                marke = "   <- nur %s Pause bis zum nächsten" % dauer_text(pause)
        sag("  %2d  %s  %s%s" % (i, uhr_text(von), dauer_text(dauer), marke))
    eng = sum(1 for p in luecken if p < 90)
    if eng:
        sag("")
        sag("%d Abschnitte liegen unter 90 s auseinander. Zum Zusammenfassen "
            "(misst nicht neu):" % eng)
        sag("  python3 schnitt.py karte %s --ziel %s --pause 60"
            % (" ".join(str(o) for o in args.ordner), args.ziel))
    sag("")
    sag("Geschrieben:")
    if neu_gemessen:
        sag("  %s" % kp)
    sag("  %s" % (ziel / "karte.html"))
    sag("  %s" % vorschlag)


def cmd_pegel(args):
    geraete = gemeinsame_geraete(args)
    von, dauer = fenster_bestimmen(args, geraete)
    karte = karte_sicherstellen(args, geraete)
    sag("Fenster: %s + %s  (bis %s)"
        % (uhr_text(von), dauer_text(dauer), uhr_text(von + timedelta(seconds=dauer))))
    sag("")
    for g in geraete:
        spuren, quelle = analysiere_fenster(g, von, dauer, args, karte)
        if spuren is None:
            warnung("%s: das Fenster liegt außerhalb der Aufnahme" % g.tag)
            continue
        sag(spuren_tabelle("%s (%s)" % (g.tag, quelle), spuren,
                           args.schwelle, args.abstand))
        belegt = [s.kanal for s in spuren if s.belegt]
        verdacht = [s.kanal for s in spuren if s.verdacht]
        sag("  -> belegt: %s" % (kurzliste(belegt)))
        if verdacht:
            sag("  -> Übersprech-Verdacht: %s" % kurzliste(verdacht))
        anschlag = [s.kanal for s in spuren if s.anschlag]
        if anschlag:
            sag("  -> Spitze am Anschlag (0 dBFS): %s — auf Übersteuerung "
                "prüfen" % kurzliste(anschlag))
        knapp = knapp_darunter(spuren, args.schwelle)
        if knapp:
            sag("  -> knapp unter der Schwelle: %s — mit --schwelle %.0f kämen "
                "sie dazu" % (kurzliste(knapp), args.schwelle - 15))
        sag("")


def cmd_schnitt(args):
    geraete = gemeinsame_geraete(args)
    von, dauer = fenster_bestimmen(args, geraete)
    ein_stueck(args, geraete, von, dauer, args.name)


def ein_stueck(args, geraete, von, dauer, name):
    ziel_wurzel = Path(args.ziel)
    ziel_wurzel.mkdir(parents=True, exist_ok=True)
    karte = karte_sicherstellen(args, geraete)
    sync = hole_sync(args, geraete)
    ref = geraete[0]
    namen = lade_namen(getattr(args, "namen", None))

    ordner_name = von.strftime("%H%M")
    if name:
        ordner_name += "_" + sauber(name)
    ziel = ziel_wurzel / ordner_name
    if not args.trocken:
        ziel.mkdir(parents=True, exist_ok=True)

    n_ziel = int(round(dauer * ref.rate))
    sag("%s  %s + %s  ->  %s"
        % (name or "(ohne Namen)", uhr_text(von), dauer_text(dauer), ziel.name))
    if sync.verfahren != "abgeschaltet":
        sag("  Zeitachse: Referenz %s, Verfahren '%s'%s"
            % (sync.referenz, sync.verfahren,
               ", Unsicherheit ±%s ppm" % ppm_text(sync.unsicherheit_ppm)
               if sync.unsicherheit_ppm else ""))

    wahl, ausdruecklich = None, {}
    if args.spuren:
        try:
            wahl, ausdruecklich = parse_spuren(args.spuren, geraete)
        except ValueError as e:
            abbruch(str(e))

    bericht = ["Stück      : %s" % (name or "(ohne Namen)"),
               "Beginn     : %s" % von.strftime("%Y-%m-%d %H:%M:%S"),
               "Dauer      : %s (%.3f s, %d Samples)" % (dauer_text(dauer), dauer, n_ziel),
               "Referenz   : %s" % ref.tag,
               "Sync       : %s" % sync.verfahren, ""]
    gesamt = 0
    for g in geraete:
        faktor = 1.0 if g.tag == ref.tag else sync.faktor(g.tag)
        if args.ohne_drift:
            faktor = 1.0
        spuren, quelle = analysiere_fenster(g, von, dauer, args, karte)
        if spuren is None:
            warnung("%s: Fenster liegt außerhalb der Aufnahme — übersprungen" % g.tag)
            bericht.append("%s: außerhalb der Aufnahme" % g.tag)
            continue
        # ---- Auswahl der Spuren -------------------------------------
        # Regeln, in dieser Reihenfolge:
        #  1. Digital stumme Spuren kommen NIE — sie enthalten nichts.
        #  2. Die Namensdatei ist führend: '-' wählt ab.
        #  3. Wer eine Spur AUSDRÜCKLICH einzeln nennt (nicht als Bereich),
        #     bekommt sie trotzdem — auch abgewählt, auch leise.
        #  4. Für alles Übrige entscheidet die Stilleerkennung.
        if wahl is not None:
            grund = wahl.get(g.tag, [])
        else:
            grund = list(range(1, g.kanaele + 1))
        genannt = ausdruecklich.get(g.tag, set())

        nach_kanal = dict((s.kanal, s) for s in spuren)
        stumm = set(k for k, s in nach_kanal.items() if s.spitze <= args.stille)
        abgewaehlt = auslassen(namen, g.tag) - genannt
        leise = set(k for k, s in nach_kanal.items() if not s.belegt) - genannt
        if args.alle:
            leise = set()

        def bleibt(k):
            return not (k in stumm or k in abgewaehlt or k in leise)

        gefiltert = []
        for k in grund:
            if isinstance(k, tuple):
                teile = [x for x in k if bleibt(x)]
                if len(teile) == 2:
                    gefiltert.append(k)
                elif teile:
                    gefiltert.append(teile[0])
            elif bleibt(k):
                gefiltert.append(k)

        raus_stumm = sorted(set(flach(grund)) & stumm)
        raus_ab = sorted(set(flach(grund)) & abgewaehlt)
        raus_leise = sorted((set(flach(grund)) & leise) - stumm - abgewaehlt)
        if raus_stumm:
            sag("           digital stumm, nie ausgegeben: %s"
                % kurzliste(raus_stumm))
        if raus_ab:
            sag("           in der Namensdatei abgewählt: %s"
                % kurzliste(raus_ab))
        if raus_leise:
            sag("           unter der Schwelle (%.0f dBFS): %s"
                % (args.schwelle, kurzliste(raus_leise)))

        vorher = list(gefiltert)
        kanaele = paare_anwenden(gefiltert, g.tag, namen)
        neu_gepaart = [k for k in kanaele
                       if isinstance(k, tuple) and k not in vorher]
        if neu_gepaart:
            sag("           aus der Namensdatei zusammengefasst: %s"
                % ", ".join("%d+%d" % k for k in neu_gepaart))
        if not kanaele:
            sag("  %-8s keine belegte Spur im Fenster" % g.tag)
            bericht.append("%s: keine belegte Spur" % g.tag)
            continue

        # Startposition und Quell-Länge in der Zeitachse dieses Geräts
        a, q = g.finde(von)
        if a is None:
            warnung("%s: Fenster liegt außerhalb der Aufnahme" % g.tag)
            continue
        if g.tag != ref.tag and len(g.abschnitte) == 1 \
                and len(ref.abschnitte) == 1 and not args.ohne_drift:
            a_ref, p_ref = ref.finde(von)
            if a_ref is not None:
                neu_q, ortlich = sync.abbildung(g.tag, p_ref)
                if neu_q is not None:
                    if abs(neu_q - q) > 3 * a.rate:
                        warnung("%s: die Synchronisation verlangt einen Sprung "
                                "von %.1f s — das ist unglaubwürdig, sie wird "
                                "für dieses Stück übergangen"
                                % (g.tag, (neu_q - q) / float(a.rate)))
                    else:
                        q = neu_q
                        faktor = ortlich
        m_quelle = int(round(n_ziel * faktor))
        if q + m_quelle > a.frames:
            m_quelle = a.frames - q
            warnung("%s: die Aufnahme endet vor dem Fenster — %s fehlen"
                    % (g.tag, dauer_text((n_ziel * faktor - m_quelle) / float(a.rate))))
        offen = flach(kanaele)
        verdaechtig = [s.kanal for s in spuren if s.verdacht and s.kanal in offen]
        sag("  %-8s %2d Datei(en): %s%s"
            % (g.tag, len(kanaele), kurzliste(offen),
               "  (Verdacht: %s)" % kurzliste(verdaechtig) if verdaechtig else ""))
        if wahl is None and not args.alle:
            knapp = knapp_darunter(spuren, args.schwelle)
            if knapp:
                sag("           knapp unter der Schwelle und deshalb NICHT "
                    "dabei: %s" % kurzliste(knapp))
        if abs(faktor - 1.0) > 1e-9:
            sag("           Driftkorrektur %+.1f ppm über sox" % ((faktor - 1.0) * 1e6))

        erzeugt, fehler = schneide_geraet(
            g, a, q, m_quelle, n_ziel, kanaele, spuren, faktor, ziel,
            args.format, von, trocken=args.trocken, namen=namen,
            mit_pegel=getattr(args, "mit_pegel", False))
        if fehler and not args.trocken:
            warnung("%s: %s" % (g.tag, fehler))
            bericht.append("%s: FEHLER %s" % (g.tag, fehler))
            continue
        gesamt += len(erzeugt)
        bericht.append("%s (Pegel aus: %s, Faktor %.9f)" % (g.tag, quelle, faktor))
        bericht.append(spuren_tabelle(g.tag, spuren, args.schwelle, args.abstand))
        bericht.append("")
        if args.trocken:
            # Der vollständige ffmpeg-Aufruf ist bei dreißig Spuren eine
            # Textwand. Im Trockenlauf zählt, WAS entstünde.
            if getattr(args, "zeige_befehl", False):
                sag("           %s" % fehler)
            else:
                for f in erzeugt:
                    sag("           %s" % Path(f).name)
        elif erzeugt and not getattr(args, "kein_anschlag", False):
            # Die Dateien sind eben erst geschrieben und liegen noch im
            # Zwischenspeicher des Systems — das Nachzählen kostet daher
            # kaum Zeit.
            zeilen = anschlag_zeilen(erzeugt)
            if zeilen:
                sag("           Vollausschlag berührt:")
                bericht.append("Vollausschlag berührt:")
                for z in zeilen:
                    sag("             %s" % z)
                    bericht.append("  " + z)
                bericht.append("")
            else:
                sag("           kein Vollausschlag")
                bericht.append("kein Vollausschlag")
                bericht.append("")

    if not args.trocken:
        (ziel / "stueck.txt").write_text("\n".join(bericht) + "\n", encoding="utf-8")
        sag("  %d Dateien in %s" % (gesamt, ziel))
    return gesamt


def kurzliste(zahlen):
    """[1,2,3,7,8] -> '1-3, 7-8'"""
    if not zahlen:
        return "-"
    zahlen = sorted(zahlen)
    teile = []
    a = b = zahlen[0]
    for x in zahlen[1:]:
        if x == b + 1:
            b = x
        else:
            teile.append(str(a) if a == b else "%d-%d" % (a, b))
            a = b = x
    teile.append(str(a) if a == b else "%d-%d" % (a, b))
    return ", ".join(teile)


def dauer_oder_endzeit(feld, von, tag0, hoechstens=7200.0):
    """Dauer ODER Endzeit erkennen.  -> (Dauer in s, war_es_eine_Endzeit)

    '6:30' ist eine Dauer, '19:11:30' eine Uhrzeit. Unterschieden wird an
    der Zahl der Doppelpunkte UND daran, ob die Uhrzeit nach dem Beginn
    liegt und ein sinnvolles Stück ergibt. Wer sichergehen will, schreibt
    ein '>' davor: '>19:11:30'.
    """
    roh = feld.strip()
    strikt = False
    for vorsatz in (">", "bis:", "bis", "="):
        if roh.lower().startswith(vorsatz):
            roh = roh[len(vorsatz):].strip()
            strikt = True
            break
    while roh.startswith(":"):
        roh = roh[1:]
        strikt = True
    if strikt or roh.count(":") == 2:
        sek = None
        try:
            sek = parse_uhrzeit(roh)
        except ValueError:
            if strikt:
                raise ValueError("'%s' ist keine gültige Uhrzeit" % feld)
        if sek is not None:
            ende = tag0 + timedelta(seconds=sek)
            if ende <= von:
                ende += timedelta(days=1)
            d = (ende - von).total_seconds()
            if strikt:
                if d <= 0:
                    raise ValueError("Endzeit '%s' liegt nicht nach dem Beginn"
                                     % feld)
                return (d, True)
            if 0 < d <= hoechstens:
                return (d, True)
    return (parse_dauer(feld), False)


def cmd_stuecke(args):
    geraete = gemeinsame_geraete(args)
    zeilen = Path(args.datei).expanduser().read_text(encoding="utf-8").splitlines()
    eintraege = []
    for nr, z in enumerate(zeilen, 1):
        z = z.split("#")[0].strip()
        if not z:
            continue
        teile = z.split(None, 2)
        if len(teile) < 2:
            abbruch("Zeile %d: erwartet werden Startzeit, Dauer und Name" % nr)
        eintraege.append((nr, teile[0], teile[1],
                          teile[2].strip() if len(teile) > 2 else ""))
    if not eintraege:
        abbruch("%s enthält keine Stücke" % args.datei)

    tag0 = geraete[0].beginn.replace(hour=0, minute=0, second=0, microsecond=0)
    fertig = []
    for i, (nr, zeit, dauer_roh, name) in enumerate(eintraege):
        try:
            sek = parse_uhrzeit(zeit)
        except ValueError as e:
            abbruch("Zeile %d: %s" % (nr, e))
        von = tag0 + timedelta(seconds=sek)
        if von < geraete[0].beginn - timedelta(hours=1):
            von += timedelta(days=1)
        endzeit = False
        if dauer_roh.strip() == "-":
            if i + 1 >= len(eintraege):
                abbruch("Zeile %d: '-' geht nicht in der letzten Zeile" % nr)
            sek2 = parse_uhrzeit(eintraege[i + 1][1])
            naechste = tag0 + timedelta(seconds=sek2)
            if naechste <= von:
                naechste += timedelta(days=1)
            dauer = (naechste - von).total_seconds()
            endzeit = True
        else:
            try:
                dauer, endzeit = dauer_oder_endzeit(
                    dauer_roh, von, tag0, args.hoechstens)
            except ValueError as e:
                abbruch("Zeile %d: %s" % (nr, e))
        if dauer <= 0:
            abbruch("Zeile %d: Dauer 0" % nr)
        fertig.append((von, dauer, name, endzeit))

    sag("%d Stücke aus %s" % (len(fertig), args.datei))
    sag("")
    sag("  Nr  Start      Dauer      Ende       Angabe   Name")
    for i, (von, dauer, name, endzeit) in enumerate(fertig, 1):
        sag("  %2d  %s   %-9s  %s   %-7s  %s"
            % (i, uhr_text(von), dauer_text(dauer),
               uhr_text(von + timedelta(seconds=dauer)),
               "Endzeit" if endzeit else "Dauer", name))
    ueberlappt = [i for i in range(len(fertig) - 1)
                  if fertig[i][0] + timedelta(seconds=fertig[i][1])
                  > fertig[i + 1][0]]
    for i in ueberlappt:
        warnung("Stück %d reicht in Stück %d hinein — Absicht?" % (i + 1, i + 2))
    sag("")
    gesamt = 0
    for von, dauer, name, _e in fertig:
        gesamt += ein_stueck(args, geraete, von, dauer, name)
        sag("")
    sag("Fertig: %d Dateien." % gesamt)


def laute_zeitpunkte(karte, tags, anzahl, fenster_s, schwelle):
    """Zeitpunkte, an denen alle genannten Geräte gleichzeitig Signal haben."""
    if not karte:
        return []
    block_s = karte["block_s"]
    huellen = []
    for tag in tags:
        g = karte["geraete"].get(tag)
        if not g:
            return []
        beginn = datetime.strptime(g["beginn"], "%Y-%m-%d %H:%M:%S")
        n = max((len(r) for r in g["peak"]), default=0)
        h = [STILLE_DB] * n
        for reihe in g["peak"]:
            for i, x in enumerate(reihe):
                v = x / float(DB_SKALA)
                if v > h[i]:
                    h[i] = v
        huellen.append((beginn, h))
    start = max(b for b, _ in huellen)
    ende = min(b + timedelta(seconds=len(h) * block_s) for b, h in huellen)
    n = int((ende - start).total_seconds() / block_s)
    if n <= 0:
        return []
    noetig = max(1, int(math.ceil(fenster_s / block_s)))
    gut = []
    for i in range(n - noetig):
        ok = True
        for b, h in huellen:
            versatz = int(round((start - b).total_seconds() / block_s))
            stueck = h[versatz + i:versatz + i + noetig]
            if not stueck or min(stueck) < schwelle:
                ok = False
                break
        if ok:
            gut.append(i)
    if not gut:
        return []
    raus = []
    for j in range(anzahl):
        k = gut[int(round(j * (len(gut) - 1) / max(1, anzahl - 1)))]
        t = start + timedelta(seconds=k * block_s)
        if not raus or (t - raus[-1]).total_seconds() > fenster_s:
            raus.append(t)
    return raus


SYNC_VORGABEN = {"fenster": 30.0, "punkte": 12, "arbeitsrate": 8000,
                 "suchweite": 3.0, "guete": 0.08, "signal": -40.0,
                 "rand": 30.0, "paar": None, "maxppm": 60.0}


def sync_args(args):
    """Fehlende sync-Einstellungen mit Vorgaben auffüllen."""
    import copy
    a = copy.copy(args)
    for k, v in SYNC_VORGABEN.items():
        if not hasattr(a, k):
            setattr(a, k, v)
    return a


def cmd_sync(args):
    geraete = gemeinsame_geraete(args)
    sync_messen(args, geraete)


def sync_messen(args, geraete):
    if len(geraete) < 2:
        abbruch("sync braucht mindestens zwei Geräte")
    np = _numpy()
    ziel = Path(args.ziel)
    ziel.mkdir(parents=True, exist_ok=True)
    ref = geraete[0]
    grob = sync_aus_dateinamen(geraete, ref.tag)

    sag("Grobmessung aus den Dateinamen (Referenz %s):" % ref.tag)
    for g in geraete[1:]:
        sag("  %-8s %+.1f ppm  (±%s ppm)"
            % (g.tag, (grob.faktor(g.tag) - 1.0) * 1e6,
               ppm_text(grob.unsicherheit_ppm)))

    if np is None:
        warnung("numpy fehlt — die genaue Messung braucht es.")
        warnung("Am Mac:  pip3 install --user numpy")
        grob.speichern(ziel / "sync.json")
        sag("sync.json mit der Grobmessung geschrieben.")
        return

    karte = None
    kp = ziel / "karte.json"
    if kp.exists():
        karte = karte_laden(kp)

    ar = args.arbeitsrate
    fenster_s = args.fenster
    faktoren = dict(grob.faktoren)
    anker = {}
    kurve = {}
    paare = {}
    verfahren = "dateinamen"
    unsicher = grob.unsicherheit_ppm
    vorgabe = parse_paar(args.paar, geraete)

    for g in geraete[1:]:
        sag("")
        sag("%s gegen %s" % (g.tag, ref.tag))
        if len(ref.abschnitte) > 1 or len(g.abschnitte) > 1:
            warnung("mindestens ein Gerät hat mehrere Aufnahmeordner. Der "
                    "Anker gilt nur innerhalb eines Ordners — die genaue "
                    "Messung wird übersprungen, es bleibt bei der Grobmessung.")
            continue
        von = max(ref.beginn, g.beginn) + timedelta(seconds=args.rand)
        bis = min(ref.ende, g.ende) - timedelta(seconds=args.rand + fenster_s)
        if bis <= von:
            warnung("kein gemeinsamer Zeitbereich")
            continue
        spanne = (bis - von).total_seconds()

        zeiten = [t for t in laute_zeitpunkte(karte, [ref.tag, g.tag],
                                              args.punkte, fenster_s,
                                              args.signal)
                  if von <= t <= bis]
        if zeiten:
            sag("  %d Messpunkte aus der Karte (dort haben beide Signal)"
                % len(zeiten))
        else:
            zeiten = [von + timedelta(seconds=spanne * (i / float(args.punkte - 1)))
                      for i in range(args.punkte)]
            if karte:
                warnung("in der Karte keine gemeinsam lauten Stellen gefunden — "
                        "die Messpunkte werden gleichmäßig verteilt")

        fest_ref = vorgabe.get(ref.tag)
        fest_g = vorgabe.get(g.tag)
        paar = None
        if fest_ref and fest_g:
            paar = (fest_ref, fest_g, 1.0)
            sag("  vorgegeben: %s Spur %d  <->  %s Spur %d"
                % (ref.tag, fest_ref, g.tag, fest_g))
        protokoll = []
        for t in zeiten:
            if paar:
                break
            paar = suche_paar(ref, g, t, fenster_s, ar, np, args.guete,
                              fest_ref, fest_g, protokoll)
            if paar:
                sag("  Paar gefunden bei %s: %s Spur %d <-> %s Spur %d, Güte %.2f"
                    % (uhr_text(t), ref.tag, paar[0], g.tag, paar[1], paar[2]))
        if not paar:
            warnung("kein gemeinsames Signal gefunden — es bleibt bei der "
                    "Grobmessung aus den Dateinamen")
            if protokoll:
                protokoll.sort(key=lambda x: -x[0])
                sag("  Geprüft wurden %d Spurpaare. Die besten davon:"
                    % len(protokoll))
                gesehen = set()
                gezeigt = 0
                for guete, k1, k2, versatz, zeit in protokoll:
                    if (k1, k2) in gesehen:
                        continue
                    gesehen.add((k1, k2))
                    sag("    %s %-2d <-> %s %-2d   Güte %.3f   Versatz %+8.1f ms"
                        "   (%s)" % (ref.tag, k1, g.tag, k2, guete,
                                     versatz * 1000.0, uhr_text(zeit)))
                    gezeigt += 1
                    if gezeigt >= 8:
                        break
                sag("  Verlangt waren %.2f (--guete). Ist ein Paar dabei, das "
                    "stimmen müsste," % args.guete)
                sag("  gib es mit --paar \"%s:N; %s:M\" vor und senke "
                    "--guete entsprechend." % (ref.tag, g.tag))
                sag("  Ein längeres Messfenster hilft oft: --fenster 30")
            continue

        k_ref, k_g = paar[0], paar[1]
        messungen = []
        for t in zeiten:
            v = versatz_messen(ref, g, t, fenster_s, k_ref, k_g, ar, np,
                               args.suchweite)
            if v is None:
                continue
            marke = "" if v[3] >= args.guete else "   (zu schwach, verworfen)"
            sag("  %s  Versatz %+8.2f ms   Güte %.3f%s"
                % (uhr_text(t), v[2] * 1000.0, v[3], marke))
            if v[3] >= args.guete:
                messungen.append(v + (t,))

        # Eine hohe Güte schützt NICHT davor, dass die Korrelation auf einen
        # Nachbarschlag springt — Musik ist periodisch. Solche Punkte fallen
        # nur dadurch auf, dass sie aus der Reihe tanzen. Ein Quarz wandert um
        # wenige ppm, nicht um hunderte; alles andere ist ein Messfehler.
        verworfen = []
        for _runde in range(6):
            if len(messungen) < 4:
                break
            pp = np.array([m[0] for m in messungen], dtype="float64")
            qq = np.array([m[1] for m in messungen], dtype="float64")
            if pp.max() - pp.min() <= 0:
                break
            st = (((pp - pp.mean()) * (qq - qq.mean())).sum()
                  / ((pp - pp.mean()) ** 2).sum())
            rest = qq - (qq.mean() + st * (pp - pp.mean()))
            # Um den MEDIAN der Residuen herum prüfen, nicht um null: ein
            # einzelner Ausreißer kippt die Gerade, dann liegen plötzlich
            # alle Punkte daneben und man würde die falschen verwerfen.
            mitte_r = float(np.median(rest))
            mad = float(np.median(np.abs(rest - mitte_r))) * 1.4826
            grenze = max(3.0 * mad, 0.005 * g.rate)
            behalten = [m for m, r in zip(messungen, rest)
                        if abs(r - mitte_r) <= grenze]
            raus = [m for m, r in zip(messungen, rest)
                    if abs(r - mitte_r) > grenze]
            if not raus or len(behalten) < 3:
                break
            verworfen.extend(raus)
            messungen = behalten
        for m in sorted(verworfen, key=lambda m: m[4]):
            sag("  %s  verworfen: %+.2f ms tanzt aus der Reihe (Güte war %.3f)"
                % (uhr_text(m[4]), m[2] * 1000.0, m[3]))
        if verworfen:
            sag("  %d Punkt(e) verworfen — bei periodischer Musik kann die "
                "Korrelation um" % len(verworfen))
            sag("  einen Schlag danebenliegen und dabei gut aussehen.")
        if len(messungen) < 2:
            warnung("weniger als zwei brauchbare Messungen — Faktor bleibt "
                    "aus den Dateinamen")
            continue

        p = np.array([m[0] for m in messungen], dtype="float64")
        q = np.array([m[1] for m in messungen], dtype="float64")
        if p.max() - p.min() < 60 * ref.rate:
            warnung("Messpunkte liegen zu dicht beieinander (unter einer "
                    "Minute) — Faktor bleibt aus den Dateinamen")
            continue
        faktor = float(((p - p.mean()) * (q - q.mean())).sum()
                       / ((p - p.mean()) ** 2).sum())
        rest = q - (q.mean() + faktor * (p - p.mean()))
        streu_ms = float(np.sqrt((rest ** 2).mean())) / g.rate * 1000.0
        streu_ppm = float(np.sqrt((rest ** 2).mean())) / (p.max() - p.min()) * 1e6

        grenze = (grob.unsicherheit_ppm or 1000.0) * 3.0 + 50.0
        weg = abs(faktor - grob.faktor(g.tag)) * 1e6
        if weg > grenze:
            warnung("Ergebnis %+.2f ppm weicht um %.0f ppm von der Grobmessung "
                    "ab (erlaubt wären %.0f) — das ist unglaubwürdig, "
                    "die Grobmessung bleibt stehen"
                    % ((faktor - 1.0) * 1e6, weg, grenze))
            continue

        # Letzte Sicherung: die örtlichen Steigungen der Kurve müssen
        # physikalisch möglich sein. Sind sie es nicht, ist irgendein Punkt
        # noch falsch — dann lieber die Gerade nehmen als eine krumme Kurve.
        punkte_ok = [[int(m[0]), int(m[1])] for m in messungen]
        schlimmste = 0.0
        for j in range(len(punkte_ok) - 1):
            d = punkte_ok[j + 1][0] - punkte_ok[j][0]
            if d > 0:
                s_ = ((punkte_ok[j + 1][1] - punkte_ok[j][1]) / float(d)
                      - 1.0) * 1e6
                schlimmste = max(schlimmste, abs(s_))
        faktoren[g.tag] = faktor
        anker[g.tag] = [int(messungen[0][0]), int(messungen[0][1])]
        if schlimmste > args.maxppm:
            warnung("örtliche Steigung von bis zu %.0f ppm — mehr als die "
                    "erlaubten %.0f. Ein Messpunkt ist noch falsch; es wird "
                    "die Ausgleichsgerade benutzt statt der Kurve."
                    % (schlimmste, args.maxppm))
            kurve[g.tag] = []
        else:
            kurve[g.tag] = punkte_ok
        paare[g.tag] = [int(k_ref), int(k_g)]
        verfahren = "korrelation"
        unsicher = round(max(streu_ppm, 0.05), 2)
        rest = []
        for j in range(len(messungen) - 1):
            p1, q1 = messungen[j][0], messungen[j][1]
            p2, q2 = messungen[j + 1][0], messungen[j + 1][1]
            if p2 > p1:
                rest.append(((q2 - q1) / float(p2 - p1) - 1.0) * 1e6)
        if rest:
            sag("  Örtlicher Takt zwischen den Messpunkten: %+.2f bis %+.2f ppm"
                % (min(rest), max(rest)))
            if max(rest) - min(rest) > 1.0:
                sag("  Der Takt ist nicht konstant (Temperatur) — beim "
                    "Schneiden wird zwischen")
                sag("  den Messpunkten interpoliert.")
        sag("  Ergebnis aus %d Punkten: %+.2f ppm   (grob war %+.1f ppm)"
            % (len(messungen), (faktor - 1.0) * 1e6,
               (grob.faktor(g.tag) - 1.0) * 1e6))
        sag("  Streuung um die Ausgleichsgerade: %.2f ms  =  %.2f ppm"
            % (streu_ms, streu_ppm))
        sag("  Anker: %s Sample %d entspricht %s Sample %d"
            % (ref.tag, anker[g.tag][0], g.tag, anker[g.tag][1]))

    s = Sync(ref.tag, faktoren, verfahren, unsicher, anker, kurve, paare)
    s.speichern(ziel / "sync.json")
    sag("")
    sag("Geschrieben: %s" % (ziel / "sync.json"))
    return s


def parse_paar(text, geraete):
    """'PULT1:12; PULT2:5' -> {'PULT1': 12, 'PULT2': 5}"""
    if not text:
        return {}
    tags = dict((g.tag, g) for g in geraete)
    raus = {}
    for block in re.split(r"[;,]", text):
        block = block.strip()
        if not block:
            continue
        if ":" not in block:
            abbruch("--paar: '%s' — erwartet wird GERÄT:Spur" % block)
        tag, _, k = block.partition(":")
        tag = re.sub(r"[^A-Za-z0-9]+", "", tag).upper()
        if tag not in tags:
            abbruch("--paar: Gerät '%s' gibt es nicht (bekannt: %s)"
                    % (tag, ", ".join(sorted(tags))))
        if not k.strip().isdigit():
            abbruch("--paar: '%s' ist keine Spurnummer" % k)
        n = int(k.strip())
        if not (1 <= n <= tags[tag].kanaele):
            abbruch("--paar: %s hat nur %d Spuren" % (tag, tags[tag].kanaele))
        raus[tag] = n
    return raus


def suche_paar(ref, g, t, fenster_s, ar, np, mindest_guete,
               fest_ref=None, fest_g=None, protokoll=None):
    """Welche Spur von ref passt zu welcher Spur von g?"""
    a_ref, p_ref = ref.finde(t)
    a_g, p_g = g.finde(t)
    if a_ref is None or a_g is None:
        return None
    m_ref = min(int(fenster_s * a_ref.rate), a_ref.frames - p_ref)
    m_g = min(int(fenster_s * a_g.rate), a_g.frames - p_g)
    if m_ref <= 0 or m_g <= 0:
        return None
    if fest_ref:
        s = lies_mono(a_ref, p_ref, m_ref, fest_ref, ar)
        kandidaten_ref = [(fest_ref, s)] if s is not None else []
    else:
        kandidaten_ref = laute_kanaele(a_ref, p_ref, m_ref, np, ar)
    if fest_g:
        s = lies_mono(a_g, p_g, m_g, fest_g, ar)
        kandidaten_g = [(fest_g, s)] if s is not None else []
    else:
        kandidaten_g = laute_kanaele(a_g, p_g, m_g, np, ar)
    if not kandidaten_ref or not kandidaten_g:
        if protokoll is not None:
            warnung_einmal(
                "Es konnten keine Spuren zum Vergleichen eingelesen werden "
                "(%s: %d, %s: %d). Ohne Spuren gibt es nichts zu korrelieren "
                "— die Ursache liegt beim Einlesen, nicht bei der Ähnlichkeit "
                "der Signale. Siehe die ffmpeg-Meldung oben."
                % (ref.tag, len(kandidaten_ref), g.tag, len(kandidaten_g)))
        return None
    max_versatz = int(2.0 * ar)
    bestes = None
    for k1, s1 in kandidaten_ref:
        for k2, s2 in kandidaten_g:
            n = min(len(s1), len(s2))
            v, guete = korreliere(s1[:n], s2[:n], np, max_versatz)
            if protokoll is not None:
                protokoll.append((guete, k1, k2, -v / float(ar), t))
            if bestes is None or guete > bestes[2]:
                bestes = (k1, k2, guete)
    if bestes and bestes[2] >= mindest_guete:
        return bestes
    return None


def laute_kanaele(a, p, m, np, ar, hoechstens=8):
    """Die lautesten Kanäle eines Fensters, heruntergetaktet."""
    block = max(1, int(0.5 * a.rate))
    peak, _rms = blockpegel(a, p, m, block)
    if peak is None:
        return []
    rang = sorted(((max(r) if r else STILLE_DB, i + 1)
                   for i, r in enumerate(peak)), reverse=True)
    raus = []
    for pegel, k in rang[:hoechstens]:
        if pegel < -60:
            break
        s = lies_mono(a, p, m, k, ar)
        if s is not None and len(s) > 100:
            raus.append((k, s))
    if not raus and rang and rang[0][0] >= -60:
        warnung_einmal("die lautesten Spuren dieses Fensters (%s) ließen sich "
                       "nicht einlesen — siehe die ffmpeg-Meldung darüber"
                       % ", ".join(str(k) for _p, k in rang[:3]))
    return raus


def versatz_messen(ref, g, t, fenster_s, k_ref, k_g, ar, np, suchweite_s):
    a_ref, p_ref = ref.finde(t)
    a_g, p_g = g.finde(t)
    if a_ref is None or a_g is None:
        return None
    m_ref = min(int(fenster_s * a_ref.rate), a_ref.frames - p_ref)
    m_g = min(int(fenster_s * a_g.rate), a_g.frames - p_g)
    if m_ref <= 0 or m_g <= 0:
        return None
    s1 = lies_mono(a_ref, p_ref, m_ref, k_ref, ar)
    s2 = lies_mono(a_g, p_g, m_g, k_g, ar)
    if s1 is None or s2 is None:
        return None
    n = min(len(s1), len(s2))
    v, guete = korreliere(s1[:n], s2[:n], np, int(suchweite_s * ar))
    # v = Versatz in Arbeitsrate-Samples: g liegt um -v später als ref.
    q = p_g + int(round(-v * (a_g.rate / float(ar))))
    # Über das Messfenster hinweg läuft der Takt auseinander; die Korrelation
    # mittelt darüber. Der gemessene Versatz gilt deshalb für die MITTE des
    # Fensters, nicht für seinen Anfang. Ohne diese Verschiebung bleibt ein
    # fester Fehler von rund einer halben Fensterdrift stehen.
    mitte = m_ref // 2
    return (p_ref + mitte, q + mitte, -v / float(ar), guete)


def cmd_kuerzen(args):
    geraete = geraete_finden(args.ordner)
    if args.referenz:
        tag = re.sub(r"[^A-Za-z0-9]+", "", args.referenz).upper()
        geraete.sort(key=lambda g: 0 if g.tag == tag else 1)
    von, dauer = fenster_bestimmen(args, geraete)
    puffer = timedelta(seconds=args.puffer)
    von_p = von - puffer
    bis_p = von + timedelta(seconds=dauer) + puffer

    sag("Behalten wird alles zwischen %s und %s"
        % (von_p.strftime("%Y-%m-%d %H:%M:%S"), bis_p.strftime("%Y-%m-%d %H:%M:%S")))
    sag("(gewünschtes Fenster %s + %s, dazu %s Puffer auf jeder Seite)"
        % (uhr_text(von), dauer_text(dauer), dauer_text(args.puffer)))
    sag("")

    gesamt_weg = 0
    gesamt_bleibt = 0
    plan = []
    for g in geraete:
        for a in g.abschnitte:
            weg = a.ausserhalb(von_p, bis_p)
            namen_weg = set(d["pfad"] for d in weg)
            bleibt = [d for d in a.dateien if d["pfad"] not in namen_weg]
            b_weg = sum(os.path.getsize(d["pfad"]) for d in weg)
            b_bleibt = sum(os.path.getsize(d["pfad"]) for d in bleibt)
            gesamt_weg += b_weg
            gesamt_bleibt += b_bleibt
            sag("%s — %s" % (g.tag, a.ordner.name))
            sag("  vorhanden : %3d Dateien, %s bis %s, %.1f GB"
                % (len(a.dateien), uhr_text(a.beginn), uhr_text(a.ende_wanduhr),
                   (b_weg + b_bleibt) / 1e9))
            if not bleibt:
                warnung("mit diesem Fenster bliebe NICHTS übrig — Zeiten prüfen")
                continue
            sag("  bleibt    : %3d Dateien, %s bis %s, %.1f GB"
                % (len(bleibt), uhr_text(bleibt[0]["wanduhr"]),
                   uhr_text(bleibt[-1]["wanduhr"]), b_bleibt / 1e9))
            sag("  weg       : %3d Dateien, %.1f GB%s"
                % (len(weg), b_weg / 1e9,
                   "  (%s … %s)" % (weg[0]["pfad"].name, weg[-1]["pfad"].name)
                   if weg else ""))
            # Lücken-Prüfung: die verbleibenden Dateien müssen lückenlos sein
            i_bleibt = [a.dateien.index(d) for d in bleibt]
            if i_bleibt and i_bleibt != list(range(i_bleibt[0], i_bleibt[-1] + 1)):
                warnung("die verbleibenden Dateien wären NICHT zusammenhängend "
                        "— hier wird nichts angefasst")
                continue
            plan.append((a, weg))
            sag("")

    sag("Zusammen: %.1f GB bleiben, %.1f GB fallen weg."
        % (gesamt_bleibt / 1e9, gesamt_weg / 1e9))

    if not args.verschieben and not args.loeschen:
        sag("")
        sag("Das war nur die Vorschau — es wurde nichts angefasst.")
        sag("Zum Ausführen denselben Befehl mit  --verschieben  (in den")
        sag("Unterordner _weg, umkehrbar)  oder  --loeschen  (endgültig).")
        return

    for a, weg in plan:
        if not weg:
            continue
        if args.loeschen:
            for d in weg:
                d["pfad"].unlink()
            sag("%s: %d Dateien gelöscht." % (a.ordner.name, len(weg)))
        else:
            ziel = a.ordner / "_weg"
            ziel.mkdir(exist_ok=True)
            for d in weg:
                d["pfad"].rename(ziel / d["pfad"].name)
            sag("%s: %d Dateien nach _weg/ verschoben." % (a.ordner.name, len(weg)))
    if args.verschieben:
        sag("")
        sag("Erst löschen, wenn 'karte' und 'sync' sauber durchgelaufen sind.")


# Alte Schreibweise mit Pegel und Dauer:  PULT1-07_Snare_-08dB_04m12s.wav
NAME_MUSTER = re.compile(
    r"^(?P<spur>[A-Za-z0-9]+-\d+(?:\+\d+)?)_(?P<mitte>.*?)"
    r"(?P<rest>[+-]\d+dB_.*)$")
# Kurze Schreibweise:  PULT1-07_Snare.wav  ·  PULT1-03_ueberspr.wav
NAME_MUSTER_KURZ = re.compile(
    r"^(?P<spur>[A-Za-z0-9]+-\d+(?:\+\d+)?)(?:_(?P<mitte>.*?))??"
    r"(?P<rest>(?:_ueberspr)?\.wav)$")


def taugliche_fenster(karte, geraet, kanaele, von, spanne, fenster,
                      anzahl, schwelle):
    """Messfenster suchen, in denen möglichst viele Spuren Signal führen.

    Die Karte weiß das schon — sie muss nicht dafür neu gelesen werden.
    Zurück kommen bis zu `anzahl` möglichst gleichmäßig verteilte Fenster.
    Ohne Karte gibt es eine leere Liste, dann wird das feste Raster benutzt.
    """
    eintrag = (karte or {}).get("geraete", {}).get(geraet.tag)
    if not eintrag:
        return []
    block_s = karte["block_s"]
    rms = eintrag.get("rms") or []
    vorher = {}
    summe = 0
    for a in geraet.abschnitte:
        vorher[id(a)] = summe
        summe += a.frames

    def bloecke(t):
        a, q = geraet.finde(t)
        if a is None:
            return None
        ab = int((vorher[id(a)] + q) / float(geraet.rate) / block_s)
        return (ab, ab + max(1, int(fenster / block_s)))

    schritt = max(fenster / 2.0, 5.0)
    kandidaten = []
    n = int(spanne / schritt) + 1
    for i in range(n):
        t = von + timedelta(seconds=i * schritt)
        if (t - von).total_seconds() + fenster > spanne:
            break
        gr = bloecke(t)
        if gr is None:
            continue
        ab, bis = gr
        # Nicht ALLE Spuren müssen laut sein — bei zwanzig Kanälen gibt es
        # keinen Moment, in dem alle gleichzeitig spielen. Die Hälfte genügt;
        # was im einzelnen Fenster fehlt, fällt dort ohnehin einzeln weg.
        laut = 0
        for k in kanaele:
            reihe = rms[k - 1] if k - 1 < len(rms) else []
            teil = reihe[ab:bis]
            if not teil:
                continue
            e = sum(10.0 ** (x / float(DB_SKALA) / 10.0) for x in teil) / len(teil)
            if 10.0 * math.log10(e + 1e-30) >= schwelle:
                laut += 1
        if laut >= max(2, int(math.ceil(0.5 * len(kanaele)))):
            kandidaten.append(t)
    if not kandidaten:
        return []
    raus = []
    for j in range(anzahl):
        k = kandidaten[int(round(j * (len(kandidaten) - 1)
                                 / max(1, anzahl - 1)))]
        if not raus or (k - raus[-1]).total_seconds() >= fenster:
            raus.append(k)
    return raus


def fenster_rangliste(karte, geraet, kanaele, von, spanne, fenster):
    """Alle möglichen Messfenster, bewertet nach der LEISESTEN der Spuren.

    Für ein einzelnes Paar gesucht: die Stelle im Abend, an der beide Spuren
    am meisten tragen. Eine Zuspielung, die nur zweimal am Abend läuft,
    findet man über ein gleichmäßiges Raster nie — über diese Rangliste
    schon. Zurück kommt eine nach Pegel absteigend sortierte Liste
    [(zeit, pegel_dbfs), ...].
    """
    eintrag = (karte or {}).get("geraete", {}).get(geraet.tag)
    if not eintrag:
        return []
    block_s = karte["block_s"]
    rms = eintrag.get("rms") or []
    vorher = {}
    summe = 0
    for a in geraet.abschnitte:
        vorher[id(a)] = summe
        summe += a.frames
    schritt = max(fenster / 2.0, 5.0)
    raus = []
    for i in range(int(spanne / schritt) + 1):
        t = von + timedelta(seconds=i * schritt)
        if (t - von).total_seconds() + fenster > spanne:
            break
        a, q = geraet.finde(t)
        if a is None:
            continue
        ab = int((vorher[id(a)] + q) / float(geraet.rate) / block_s)
        bis = ab + max(1, int(fenster / block_s))
        p = []
        for k in kanaele:
            reihe = rms[k - 1] if k - 1 < len(rms) else []
            teil = reihe[ab:bis]
            if not teil:
                p = []
                break
            e = sum(10.0 ** (x / float(DB_SKALA) / 10.0)
                    for x in teil) / len(teil)
            p.append(10.0 * math.log10(e + 1e-30))
        if p:
            raus.append((t, min(p)))
    raus.sort(key=lambda x: -x[1])
    return raus


def waehle_getrennt(rangliste, anzahl, abstand):
    """Die besten Einträge, die sich zeitlich nicht überlappen."""
    raus = []
    for t, p in rangliste:
        if all(abs((t - u).total_seconds()) >= abstand for u, _ in raus):
            raus.append((t, p))
        if len(raus) >= anzahl:
            break
    return raus


def huellkurve(karte, geraet, kanal):
    """Pegelverlauf einer Spur über den Abend, in dB, aus der Karte."""
    eintrag = (karte or {}).get("geraete", {}).get(geraet.tag)
    if not eintrag:
        return None
    rms = eintrag.get("rms") or []
    if kanal - 1 >= len(rms):
        return None
    return [x / float(DB_SKALA) for x in rms[kanal - 1]]


def paar_urteil(wellenform, huelle, args, verschoben=None, versatz_ms=None):
    """Gehören zwei benachbarte Spuren zusammen?

    Zurück kommt einer von vier Texten:
      "Paar"          — gemessen und sicher
      "Paar?"         — Anzeichen, aber die Messung trägt nicht
      "eigenständig"  — gemessen, gehören nicht zusammen
      "nicht messbar" — es gab kein Fenster mit Signal

    Der Pegelverlauf allein genügt nicht: zwei verschiedene Gesangsmikros
    derselben Band werden zusammen laut und zusammen leise, ohne ein
    Stereopaar zu sein. Die Wellenform allein genügt auch nicht: zwei
    auseinanderstehende Raummikros korrelieren bei Versatz null nur schwach,
    gehören aber zusammen — dafür ist die Suche über die Laufzeit da.
    """
    hoch = huelle is not None and huelle >= args.huelle
    if wellenform is None:
        return "Paar?" if hoch else "nicht messbar"
    if abs(wellenform) >= args.aehnlich:
        return "Paar"
    if hoch and abs(wellenform) >= args.nachbar:
        return "Paar"
    # Raummikrofone: dieselbe Quelle, aber mit Laufzeit dazwischen. Bei
    # Versatz null sieht man davon wenig, bei passender Verschiebung viel.
    if (hoch and verschoben is not None and versatz_ms is not None
            and abs(verschoben) >= args.aehnlich
            and abs(versatz_ms) <= args.laufzeit):
        return "Paar"
    return "eigenständig"


def max_korrelation(np, x, y, hoechster_versatz):
    """Höchste Ähnlichkeit, wenn man y gegen x verschieben darf.

    x und y müssen auf Länge 1 normiert sein. Zurück kommt
    (wert, versatz_in_proben). Positiver Versatz heißt: y kommt später.
    """
    n = min(len(x), len(y))
    if n < 64 or hoechster_versatz < 1:
        return None, 0
    hoechster_versatz = min(hoechster_versatz, n - 1)
    laenge = 1
    while laenge < 2 * n:
        laenge *= 2
    fx = np.fft.rfft(x[:n], laenge)
    fy = np.fft.rfft(y[:n], laenge)
    r = np.fft.irfft(fx * np.conj(fy), laenge)
    v = hoechster_versatz
    teil = np.concatenate((r[laenge - v:], r[:v + 1]))
    i = int(np.argmax(np.abs(teil)))
    return float(teil[i]), i - v


def huellen_aehnlich(np, a, b, boden=-70.0):
    """Wie ähnlich sind zwei Pegelverläufe?

    Zwei auseinanderstehende Raummikrofone haben eine niedrige Wellenform-
    Ähnlichkeit — dieselbe Quelle erreicht sie ja mit unterschiedlicher
    Laufzeit. Ihre PEGELVERLÄUFE sind aber fast deckungsgleich: sie werden
    zusammen laut und zusammen leise. Genau daran erkennt man ein Paar.
    """
    if not a or not b:
        return None
    n = min(len(a), len(b))
    x = np.array(a[:n], dtype="float64")
    y = np.array(b[:n], dtype="float64")
    gilt = (x > boden) | (y > boden)
    if gilt.sum() < 50:
        return None
    x = x[gilt]
    y = y[gilt]
    x = x - x.mean()
    y = y - y.mean()
    nx, ny = float(np.linalg.norm(x)), float(np.linalg.norm(y))
    if nx <= 0 or ny <= 0:
        return None
    return float(np.dot(x, y) / (nx * ny))


def cmd_summe(args):
    """Ist eine Spur die Summe mehrerer anderer?

    Gesucht werden Gewichte, mit denen sich die Zielspur aus den genannten
    Quellen zusammensetzen lässt (kleinste Quadrate). Entscheidend ist
    danach nicht die Ähnlichkeit, sondern was ÜBRIG BLEIBT: liegt der Rest
    30 dB unter dem Original, ist die Spur die Summe. Bleiben nur 6 dB,
    hat sie mit den Quellen wenig zu tun.
    """
    geraete = gemeinsame_geraete(args)
    np = _numpy()
    if np is None:
        abbruch("dieser Befehl braucht numpy:  pip3 install --user numpy")
    von, spanne = fenster_bestimmen(args, geraete)
    karte = karte_sicherstellen(args, geraete)
    if not args.spur or not args.aus:
        abbruch('--spur und --aus werden gebraucht, z. B. '
                '--spur "PULT1:5" --aus "PULT1:1,2,4,6"')
    z_wahl = parse_spuren(args.spur, geraete)[0]
    q_wahl = parse_spuren(args.aus, geraete)[0]
    tags = [g.tag for g in geraete if g.tag in z_wahl]
    if not tags:
        abbruch("die Zielspur gehört zu keinem gefundenen Gerät")
    g = [x for x in geraete if x.tag == tags[0]][0]
    ziel_k = sorted(set(flach(z_wahl[g.tag])))
    if len(ziel_k) != 1:
        abbruch("--spur muss genau EINE Spur nennen")
    ziel_k = ziel_k[0]
    quellen = [k for k in sorted(set(flach(q_wahl.get(g.tag, []))))
               if k != ziel_k]
    if len(quellen) < 2:
        abbruch("--aus braucht mindestens zwei Spuren desselben Geräts")

    fenster = min(args.fenster, spanne)
    anzahl = max(1, args.punkte)
    sag("%s-%02d gegen %s"
        % (g.tag, ziel_k, ", ".join("%02d" % k for k in quellen)))
    sag("Spanne: %s bis %s"
        % (uhr_text(von), uhr_text(von + timedelta(seconds=spanne))))

    alle_k = [ziel_k] + quellen
    zeiten = taugliche_fenster(karte, g, alle_k, von, spanne, fenster,
                               anzahl, args.leise)
    if not zeiten:
        rang = fenster_rangliste(karte, g, alle_k, von, spanne, fenster)
        zeiten = [x[0] for x in waehle_getrennt(rang, anzahl, fenster)]
    if not zeiten:
        zeiten = [von]

    reste, gewichte, einzeln, versaetze = [], [], {}, {}
    daten = []          # (y, X) je Fenster, für einen zweiten Durchgang
    benutzt = 0
    hoechster_versatz = int(round(args.laufzeit / 1000.0 * args.arbeitsrate))
    for t_ in zeiten:
        a_, q_ = g.finde(t_)
        if a_ is None:
            continue
        m = min(int(round(fenster * a_.rate)), a_.frames - q_)
        if m <= 0:
            continue
        roh = lies_alle(a_, q_, m, args.arbeitsrate)
        if roh is None or max(alle_k) > roh.shape[1]:
            continue
        y = roh[:, ziel_k - 1].astype("float64")
        y = y - y.mean()
        ny = float(np.linalg.norm(y))
        if ny <= 0 or ny / math.sqrt(len(y)) < 10.0 ** (args.leise / 20.0):
            continue
        X = np.stack([roh[:, k - 1].astype("float64") for k in quellen], 1)
        X = X - X.mean(0)
        if not np.isfinite(X).all():
            continue
        daten.append((y.astype("float32"), X.astype("float32")))
        w, _res, _rk, _sv = np.linalg.lstsq(X, y, rcond=None)
        r = y - X.dot(w)
        reste.append(20.0 * math.log10(max(float(np.linalg.norm(r)), 1e-30)
                                       / ny))
        gewichte.append(w)
        # Was schafft jede Quelle für sich allein?
        for i, k in enumerate(quellen):
            x = X[:, i]
            nx = float(np.linalg.norm(x))
            if nx <= 0:
                continue
            rr = y - x * (float(np.dot(x, y)) / (nx * nx))
            einzeln.setdefault(k, []).append(
                20.0 * math.log10(max(float(np.linalg.norm(rr)), 1e-30) / ny))
            wert, vers = max_korrelation(np, x / nx, y / ny,
                                         hoechster_versatz)
            if wert is not None:
                versaetze.setdefault(k, []).append(
                    vers * 1000.0 / float(args.arbeitsrate))
        benutzt += 1
    if not reste:
        abbruch("in keinem Fenster genug Signal — anderen Zeitraum wählen "
                "oder --leise senken")

    rest_db = float(np.median(reste))
    w_med = np.median(np.stack(gewichte, 0), 0)

    # Zeigen ALLE Quellen denselben Versatz, ist die Zielspur womöglich die
    # verzögerte Summe — ein Raummikro etwa hört dieselben Trommeln, nur
    # später. Dann lohnt ein zweiter Durchgang mit ausgeglichener Laufzeit.
    nach_versatz = None
    ms_alle = [float(np.median(versaetze[k])) for k in quellen
               if versaetze.get(k)]
    if (rest_db > -12.0 and len(ms_alle) == len(quellen)
            and max(ms_alle) - min(ms_alle) <= 2.0
            and abs(float(np.median(ms_alle))) >= 0.5):
        d0 = abs(int(round(float(np.median(ms_alle)) / 1000.0
                           * args.arbeitsrate)))
        # Welche Richtung stimmt, entscheidet die Messung, nicht das
        # Vorzeichen einer Kreuzkorrelation: beide werden gerechnet.
        for d in (d0, -d0):
            if d == 0:
                continue
            zweite = []
            for y_, X_ in daten:
                if d > 0:
                    yv, Xv = y_[d:], X_[:-d]      # Zielspur später
                else:
                    yv, Xv = y_[:d], X_[-d:]      # Zielspur früher
                if len(yv) < 1000:
                    continue
                ny = float(np.linalg.norm(yv))
                if ny <= 0:
                    continue
                w2, _a, _b, _c = np.linalg.lstsq(
                    Xv.astype("float64"), yv.astype("float64"), rcond=None)
                r2 = yv.astype("float64") - Xv.astype("float64").dot(w2)
                zweite.append((20.0 * math.log10(
                    max(float(np.linalg.norm(r2)), 1e-30) / ny), w2))
            if not zweite:
                continue
            rr = float(np.median([x[0] for x in zweite]))
            if nach_versatz is None or rr < nach_versatz[1]:
                nach_versatz = (d * 1000.0 / float(args.arbeitsrate), rr,
                                np.median(np.stack([x[1] for x in zweite], 0),
                                          0))

    sag("")
    sag("Gemessen in %d Fenstern à %s" % (benutzt, dauer_text(fenster)))
    sag("")
    sag("  Quelle   Gewicht   in dB    allein bleibt   Versatz")
    for i, k in enumerate(quellen):
        gw = float(w_med[i])
        db = 20.0 * math.log10(abs(gw)) if abs(gw) > 1e-9 else -120.0
        al = float(np.median(einzeln[k])) if einzeln.get(k) else 0.0
        vs = float(np.median(versaetze[k])) if versaetze.get(k) else None
        sag("  %6d %9.3f %8.1f %13.1f dB   %s"
            % (k, gw, db, al,
               "-" if vs is None else "%+.1f ms" % vs))
    sag("")
    erklaert = max(0.0, 1.0 - 10.0 ** (rest_db / 10.0)) * 100.0
    sag("  Rest nach Abzug aller Quellen: %.1f dB unter der Zielspur "
        "(%.1f %% erklärt)" % (rest_db, erklaert))
    if len(reste) > 1:
        sag("  (über die Fenster: %.1f bis %.1f dB)"
            % (min(reste), max(reste)))
    if nach_versatz is not None:
        ms, rest2, w2 = nach_versatz
        sag("")
        sag("  Die Zielspur liegt %+.1f ms gegenüber den Quellen — mit "
            "Laufzeitausgleich gerechnet:" % ms)
        sag("  Rest %.1f dB (%.1f %% erklärt), Gewichte %s"
            % (rest2, max(0.0, 1.0 - 10.0 ** (rest2 / 10.0)) * 100.0,
               ", ".join("%02d: %.2f" % (k, float(w2[i]))
                         for i, k in enumerate(quellen))))
        if rest2 < rest_db - 6.0:
            sag("  Das erklärt deutlich mehr: die Zielspur trägt dasselbe "
                "Signal, nur später.")
            sag("  Nach einem Pult-Summenkanal sieht das nicht aus — eher "
                "nach einem Mikrofon,")
            sag("  das dieselben Quellen aus %.1f m Entfernung hört."
                % (abs(ms) / 1000.0 * 343.0))
            rest_db = rest2
    sag("")
    if rest_db <= -30.0:
        sag("  Urteil: %s-%02d IST die Summe dieser Spuren." % (g.tag, ziel_k))
        sag("  Was übrig bleibt, liegt %.0f dB darunter — das ist nichts mehr."
            % -rest_db)
    elif rest_db <= -12.0:
        sag("  Urteil: %s-%02d enthält diese Spuren, aber nicht nur sie."
            % (g.tag, ziel_k))
        sag("  Es fehlt noch etwas — eine weitere Quelle, ein Effekt oder "
            "eine Bearbeitung.")
    elif rest_db <= -2.0:
        sag("  Urteil: %s-%02d enthält diese Spuren zu %.0f %% der Energie — "
            "der Rest ist etwas anderes." % (g.tag, ziel_k, erklaert))
    else:
        sag("  Urteil: %s-%02d ist NICHT aus diesen Spuren zusammengesetzt."
            % (g.tag, ziel_k))
    sag("")
    sag("Lesehilfe: 'allein bleibt' ist der Rest, wenn NUR diese eine Quelle")
    sag("abgezogen wird. Steht dort fast 0 dB, trägt sie kaum etwas bei.")
    sag("Ein Versatz deutlich über 0 ms spricht gegen eine Pultsumme: die")
    sag("wird intern gerechnet und ist probengenau.")


def cmd_paare(args):
    """Welche Spuren tragen dasselbe Signal? Misst statt zu raten.

    Gemessen wird an mehreren Stellen über die angegebene Spanne verteilt.
    Ein einzelnes Fenster kann täuschen: dort, wo gerade nur ein Instrument
    spielt, sehen zwei Spuren schnell gleich aus.
    """
    geraete = gemeinsame_geraete(args)
    np = _numpy()
    if np is None:
        abbruch("dieser Befehl braucht numpy:  pip3 install --user numpy")
    von, spanne = fenster_bestimmen(args, geraete)
    karte = karte_sicherstellen(args, geraete)
    wahl = parse_spuren(args.spuren, geraete)[0] if args.spuren else None
    vorschlaege = []

    fenster = min(args.fenster, spanne)
    anzahl = max(1, args.punkte)
    if anzahl > 1 and spanne <= fenster:
        anzahl = 1
    sag("Spanne: %s bis %s — bis zu %d Fenster à %s"
        % (uhr_text(von), uhr_text(von + timedelta(seconds=spanne)),
           anzahl, dauer_text(fenster)))

    for g in geraete:
        if wahl is not None and g.tag not in wahl:
            continue
        # Kanäle einmal festlegen, über die ganze Spanne
        if wahl is not None:
            kan = sorted(set(flach(wahl[g.tag])))
        else:
            spuren, _q = analysiere_fenster(g, von, spanne, args, karte)
            kan = [s.kanal for s in spuren if s.belegt] if spuren else []
        if len(kan) < 2:
            continue
        if len(kan) > args.hoechstens:
            warnung("%s: %d Spuren sind zu viele, es werden die ersten %d "
                    "geprüft — mit --spuren eingrenzen"
                    % (g.tag, len(kan), args.hoechstens))
            kan = kan[:args.hoechstens]

        # Fenster so wählen, dass möglichst viele der verglichenen Spuren
        # dort Signal führen — statt ein festes Raster zu nehmen und die
        # Hälfte davon hinterher als zu leise zu verwerfen. Was hier
        # durchfällt, bekommt weiter unten eigene Fenster.
        # Wer den ganzen Abend still ist, darf die Suche nicht verwässern:
        # sonst ist "die Hälfte" bei --spuren 1-32 nie zu erreichen und es
        # bleibt beim festen Raster.
        aktive = []
        for k in kan:
            hk = huellkurve(karte, g, k) or []
            if not hk or max(hk) > STILL_DBFS:
                aktive.append(k)
        if len(aktive) < 2:
            aktive = list(kan)
        zeiten = taugliche_fenster(karte, g, aktive, von, spanne, fenster,
                                   anzahl, args.leise)
        if zeiten:
            sag("")
            sag("  %s: %d Fenster gewählt, in denen mindestens die Hälfte "
                "der %d Spuren mit Signal laut ist."
                % (g.tag, len(zeiten), len(aktive)))
            if len(zeiten) < anzahl:
                sag("     Mehr gibt der Abend nicht her — %d waren verlangt."
                    % anzahl)
        else:
            if anzahl == 1:
                zeiten = [von]
            else:
                schritt = (spanne - fenster) / float(anzahl - 1)
                zeiten = [von + timedelta(seconds=schritt * i)
                          for i in range(anzahl)]
            if karte:
                warnung("%s: keine Stelle gefunden, an der genug Spuren "
                        "gleichzeitig Signal führen — festes Raster, stille "
                        "Fenster fallen dann einzeln weg" % g.tag)

        werte = {}          # (i, j) -> Liste von Werten bei Versatz null
        vers = {}           # (i, i+1) -> Liste von (bester Wert, ms)
        benutzt = 0
        hoechster_versatz = int(round(args.laufzeit / 1000.0
                                      * args.arbeitsrate))
        zu_leise = dict((k, 0) for k in kan)
        pegel = dict((k, []) for k in kan)
        for t in zeiten:
            a, q = g.finde(t)
            if a is None:
                continue
            m = min(int(round(fenster * a.rate)), a.frames - q)
            if m <= 0:
                continue
            alle = lies_alle(a, q, m, args.arbeitsrate)
            if alle is None:
                continue
            sig = {}
            for k in kan:
                if k > alle.shape[1]:
                    continue
                s = alle[:, k - 1]
                s = s - s.mean()
                norm = float(np.linalg.norm(s))
                eff = norm / math.sqrt(len(s))
                pegel[k].append(20.0 * math.log10(eff) if eff > 0 else -200.0)
                # Ein Fenster ohne nennenswertes Signal taugt nicht: dort
                # misst man Rauschen gegen Rauschen, und zwei verwandte
                # Spuren sehen sich dann zufällig ähnlich oder auch nicht.
                if eff < 10.0 ** (args.leise / 20.0):
                    zu_leise[k] += 1
                    continue
                sig[k] = s / norm
            if len(sig) < 2:
                continue
            benutzt += 1
            for i in sig:
                for j in sig:
                    if i < j:
                        werte.setdefault((i, j), []).append(
                            float(np.dot(sig[i], sig[j])))
            # Nur für BENACHBARTE Spuren zusätzlich mit Laufzeitausgleich:
            # zwei auseinanderstehende Raummikros hören dasselbe Signal,
            # aber zeitversetzt. Bei Versatz null sieht man davon wenig.
            for i in sig:
                if i + 1 in sig:
                    w_, v_ = max_korrelation(np, sig[i], sig[i + 1],
                                             hoechster_versatz)
                    if w_ is not None:
                        vers.setdefault((i, i + 1), []).append(
                            (w_, v_ * 1000.0 / float(args.arbeitsrate)))
        still_grenze = args.leise - 30.0
        stumm = [k for k in kan if zu_leise[k]]
        if stumm:
            sag("")
            sag("  %s — Fenster ohne nennenswertes Signal (unter %.0f dBFS "
                "effektiv):" % (g.tag, args.leise))
            leer = []
            for k in stumm:
                mp = float(np.median(pegel[k])) if pegel[k] else -200.0
                if mp <= -199.0 or mp < still_grenze:
                    leer.append(k)
                    continue
                sag("     Spur %-3d %2d von %d Fenstern übergangen "
                    "(Mittelpegel %.1f dBFS)"
                    % (k, zu_leise[k], len(zeiten), mp))
            if leer:
                sag("     Spur %s in allen Fenstern unter %.0f dBFS"
                    % (kurzliste(leer), still_grenze))

        # ---- Nachmessung mit eigenen Fenstern ------------------------
        # Wer in ALLEN Fenstern zu leise war, bekommt eigene: die Stellen
        # im Abend, an denen genau diese beiden Spuren am lautesten sind.
        # Ein Zuspieler, der nur zweimal läuft, taucht im gleichmäßigen
        # Raster sonst nie auf.
        unsicher = set()
        aktiv_satz = set(aktive)
        offene = [(k, k + 1) for k in kan
                  if k + 1 in kan and (k, k + 1) not in werte
                  and (k in aktiv_satz and k + 1 in aktiv_satz)]
        if offene and args.nachmessen and karte:
            sag("")
            sag("  Nachmessung — eigene Fenster für Paare ohne Messwert:")
            for i, j in offene:
                rang = fenster_rangliste(karte, g, [i, j], von, spanne,
                                         fenster)
                stellen = waehle_getrennt(rang, min(4, max(2, anzahl)),
                                          fenster)
                if not stellen:
                    sag("     %2d+%-2d  die Karte kennt keine Stelle" % (i, j))
                    continue
                bester = stellen[0][1]
                if bester < still_grenze:
                    sag("     %2d+%-2d  über die ganze Spanne still "
                        "(%.0f dBFS)" % (i, j, bester))
                    continue
                # Schwelle auf das absenken, was diese Spuren überhaupt
                # hergeben — sonst fällt wieder jedes Fenster weg.
                grenze = min(args.leise, bester - 6.0)
                w, vv = [], []
                for t, _p in stellen:
                    a, q = g.finde(t)
                    if a is None:
                        continue
                    m = min(int(round(fenster * a.rate)), a.frames - q)
                    if m <= 0:
                        continue
                    alle = lies_alle(a, q, m, args.arbeitsrate)
                    if alle is None or j > alle.shape[1]:
                        continue
                    sig = {}
                    for k in (i, j):
                        s = alle[:, k - 1]
                        s = s - s.mean()
                        norm = float(np.linalg.norm(s))
                        if norm <= 0:
                            continue
                        if norm / math.sqrt(len(s)) < 10.0 ** (grenze / 20.0):
                            continue
                        sig[k] = s / norm
                    if len(sig) < 2:
                        continue
                    w.append(float(np.dot(sig[i], sig[j])))
                    w_, v_ = max_korrelation(np, sig[i], sig[j],
                                             hoechster_versatz)
                    if w_ is not None:
                        vv.append((w_, v_ * 1000.0 / float(args.arbeitsrate)))
                if not w:
                    sag("     %2d+%-2d  auch dort kein verwertbares Signal "
                        "(bester Pegel %.1f dBFS)" % (i, j, bester))
                    continue
                werte[(i, j)] = w
                if vv:
                    vers[(i, j)] = vv
                if bester < args.leise:
                    unsicher.add((i, j))
                sag("     %2d+%-2d  %d Fenster ab %s, bester Pegel %.1f dBFS "
                    "→ Ähnlichkeit %.2f%s"
                    % (i, j, len(w), uhr_text(stellen[0][0]), bester,
                       float(np.median(w)),
                       "   (leise — unsicher)" if bester < args.leise else ""))

        if not werte:
            warnung("%s: in keinem Fenster genug Signal — anderen Zeitraum "
                    "wählen oder --leise senken" % g.tag)
            continue

        def mittel(i, j):
            v = werte.get((i, j) if i < j else (j, i))
            return None if not v else float(np.median(v))

        sag("")
        sag("%s — Ähnlichkeit über %d Fenster (Mittelwert; 1,00 = dasselbe "
            "Signal)" % (g.tag, benutzt))
        sag("      " + "".join("%7d" % k for k in kan))
        for i in kan:
            zeile = []
            for j in kan:
                v = None if i == j else mittel(i, j)
                zeile.append("      -" if v is None else "%7.2f" % v)
            sag("  %3d " % i + "".join(zeile))

        # Die Korrelation allein täuscht: 0,998 klingt nach "identisch",
        # heißt aber, dass der Differenzanteil bei -24 dB liegt — und das
        # hört man als Stereobreite. Entscheidend ist deshalb der Pegel der
        # Differenz nach Pegelangleich:  |L-R|^2 = 2 - 2r.
        def diff_db(r_):
            d = 2.0 - 2.0 * abs(r_)
            return 10.0 * math.log10(d) if d > 1e-12 else -120.0

        sag("")
        sag("  Spur   Spur   Ähnlichkeit   Differenz   Urteil")
        gleich, paar = [], []
        for (i, j), v in sorted(werte.items()):
            med = float(np.median(v))
            db = diff_db(med)
            if abs(med) < args.aehnlich:
                continue
            if db <= args.mono:
                urteil = "identisch — mono, eine Spur genügt"
                gleich.append((i, j, med, min(v), max(v), len(v)))
            elif db <= -18.0:
                urteil = "schmales Stereobild — beide behalten"
                paar.append((i, j, med, min(v), max(v), len(v)))
            elif db <= -5.0:
                urteil = "deutliches Stereobild — beide behalten"
                paar.append((i, j, med, min(v), max(v), len(v)))
            else:
                urteil = "gemeinsame Quelle, aber weit auseinander"
                paar.append((i, j, med, min(v), max(v), len(v)))
            sag("  %4d   %4d   %10.4f   %+7.1f dB   %s"
                % (i, j, med, db, urteil))
            lo, hi = min(v), max(v)
            if hi - lo > 0.15:
                sag("                    schwankt zwischen %.3f und %.3f über "
                    "die Fenster" % (lo, hi))

        entartet = set()
        treffer = []
        for i, j, _m, _l, _h, _n in gleich:
            entartet.add(i)
            entartet.add(j)

        # Ist eine Spur die Mitte zweier anderer?  Nur dort prüfen, wo alle
        # drei Spuren miteinander verwandt sind — und nur, wenn sie nicht
        # ohnehin identisch sind: die Summe zweier gleicher Signale ist
        # wieder dasselbe Signal, der Test antwortet dann zwangsläufig mit 1.
        def med(i, j):
            """Gemessene Ähnlichkeit — None heißt: nicht gemessen."""
            v = werte.get((i, j) if i < j else (j, i))
            return float(np.median(v)) if v else None

        def med0(i, j):
            v = med(i, j)
            return 0.0 if v is None else v

        def med_vers(i, j):
            """Beste Ähnlichkeit mit Laufzeitausgleich und der Versatz."""
            v = vers.get((i, j) if i < j else (j, i))
            if not v:
                return None, None
            return (float(np.median([a for a, _b in v])),
                    float(np.median([b for _a, b in v])))

        dreier = []
        for k in kan:
            for i in kan:
                for j in kan:
                    if i >= j or k in (i, j):
                        continue
                    if i in entartet or j in entartet or k in entartet:
                        continue
                    if min(abs(med0(i, j)), abs(med0(k, i)),
                           abs(med0(k, j))) < args.aehnlich:
                        continue
                    dreier.append((k, i, j))
        if entartet:
            sag("  Mitte-Test für %s übersprungen (identische Spuren)."
                % kurzliste(sorted(entartet)))

        if dreier and len(dreier) <= 60:
            probe = zeiten[:min(len(zeiten), 5)]
            gesammelt = {}
            for t in probe:
                a, q = g.finde(t)
                if a is None:
                    continue
                m = min(int(round(fenster * a.rate)), a.frames - q)
                if m <= 0:
                    continue
                noetig = set()
                for k, i, j in dreier:
                    noetig.update((k, i, j))
                alle = lies_alle(a, q, m, args.arbeitsrate)
                if alle is None:
                    continue
                sig = {}
                for k in sorted(noetig):
                    if k > alle.shape[1]:
                        continue
                    s = alle[:, k - 1]
                    s = s - s.mean()
                    norm = float(np.linalg.norm(s))
                    if norm / math.sqrt(len(s)) < 10.0 ** (args.leise / 20.0):
                        continue
                    sig[k] = s / norm
                for k, i, j in dreier:
                    if k in sig and i in sig and j in sig:
                        s = sig[i] + sig[j]
                        nn = float(np.linalg.norm(s))
                        if nn > 0:
                            gesammelt.setdefault((k, i, j), []).append(
                                float(np.dot(sig[k], s / nn)))
            # Bei einem SCHMALEN Stereobild besteht auch der falsche
            # Kandidat den Summentest: L gegen M+R kommt dann auf 0,998,
            # die echte Mitte auf 1,000. Deshalb zwei Bedingungen mehr:
            # je Dreiergruppe zählt nur der beste Wert, und das genannte
            # L/R-Paar muss die NIEDRIGSTE Korrelation der Gruppe haben —
            # links und rechts sind sich immer unähnlicher als jedes von
            # beiden der Mitte.
            roh = []
            for (k, i, j), v in gesammelt.items():
                mm = float(np.median(v))
                if mm >= 0.995:
                    roh.append((mm, k, i, j, min(v), max(v), len(v)))
            roh.sort(reverse=True)
            treffer = []
            gesehen = set()
            for e in roh:
                mm, k, i, j = e[0], e[1], e[2], e[3]
                gruppe = frozenset((k, i, j))
                if gruppe in gesehen:
                    continue
                if abs(med0(i, j)) >= min(abs(med0(k, i)), abs(med0(k, j))):
                    continue
                gesehen.add(gruppe)
                treffer.append(e)
            if treffer:
                sag("")
                mm, k, i, j, lo, hi, n = treffer[0]
                sag("  Spur %d ist die MITTE von %d und %d (%.4f über %d "
                    "Fenster) — %d und %d sind" % (k, i, j, mm, n, i, j))
                sag("  links und rechts, %d ist entbehrlich." % k)

        if not gleich and not paar:
            sag("  Keine auffälligen Verwandtschaften — die Spuren tragen "
                "verschiedene Quellen.")

        # ---- Vorschlag für die Namensdatei ---------------------------
        # Nur BENACHBARTE Spuren werden gepaart. Zwei weit auseinander
        # liegende Kanäle können zufällig verwandt aussehen (etwa Summe und
        # Main Out); ein Stereopaar liegt am Pult praktisch immer nebeneinander.
        # Hüllkurven benachbarter Spuren vergleichen — das entscheidet
        # bei weit auseinanderstehenden Mikrofonen, wo die Wellenform-
        # Ähnlichkeit naturgemäß niedrig ist.
        huellen = {}
        for k in kan:
            huellen[k] = huellkurve(karte, g, k)
        nachbarn = {}
        for k in kan:
            if k + 1 not in kan:
                continue
            if k not in aktiv_satz or k + 1 not in aktiv_satz:
                continue        # eine der beiden war den ganzen Abend still
            hh = huellen_aehnlich(np, huellen.get(k), huellen.get(k + 1))
            ww = med(k, k + 1)
            vw, vms = med_vers(k, k + 1)
            urteil = paar_urteil(ww, hh, args, vw, vms)
            if urteil == "Paar" and (k, k + 1) in unsicher:
                urteil = "Paar?"
            nachbarn[k] = (ww, hh, vw, vms, urteil)
        if nachbarn:
            sag("")
            sag("  Benachbarte Spuren — gehören sie zusammen?")
            sag("  Spuren   Wellenform   Pegelverlauf   mit Laufzeit   Urteil")
            for k in sorted(nachbarn):
                ww, hh, vw, vms, urteil = nachbarn[k]
                sag("  %2d+%-2d   %10s   %12s   %14s   %s"
                    % (k, k + 1,
                       "-" if ww is None else "%.2f" % ww,
                       "-" if hh is None else "%.2f" % hh,
                       "-" if vw is None else "%.2f bei %+.0f ms" % (vw, vms),
                       urteil))
            sag("  Paar? heißt: Anzeichen ja, Messung trägt nicht — selbst "
                "reinhören.")
            # Hoch korreliert, aber erst nach Verschieben, und der
            # Pegelverlauf spricht dagegen: kann ein Raumpaar sein, kann
            # auch bloßes Übersprechen sein. Nicht automatisch paaren,
            # aber auch nicht verschweigen.
            for k in sorted(nachbarn):
                ww, hh, vw, vms, urteil = nachbarn[k]
                if (urteil == "eigenständig" and vw is not None
                        and abs(vw) >= args.aehnlich
                        and abs(vw) > abs(ww) + 0.2):
                    sag("  %d+%d sieht erst mit %.0f ms Versatz ähnlich aus "
                        "(%.2f), der Pegelverlauf" % (k, k + 1, abs(vms),
                                                      abs(vw)))
                    sag("  spricht dagegen (%s) — falls das Raummikros sind, "
                        "selbst prüfen."
                        % ("-" if hh is None else "%.2f" % hh))

        # Welche Spur ist die Mitte eines Paares? Die wird gleich abgewählt.
        mitten = {}
        for m_, k_, i_, j_, _l, _h, _n in treffer[:1]:
            mitten[k_] = (i_, j_)

        if True:
            zeilen = ["# %s — aus der Messung vom %s" % (g.tag,
                      datetime.now().strftime("%Y-%m-%d %H:%M")),
                      "# Namen rechts vom = ergänzen. Ein leerer Name ist",
                      "# erlaubt: dann gilt nur die Paarung."]
            offen = list(kan)
            still_raus = []
            while offen:
                k = offen.pop(0)
                if offen and offen[0] == k + 1 and k in nachbarn:
                    ww, hh, vw, vms, urteil = nachbarn[k]
                    if urteil in ("Paar", "Paar?"):
                        partner = offen.pop(0)
                        if ww is None:
                            zeilen.append("%s-%02d+%02d =            "
                                          "# Paar? nur aus dem Pegelverlauf "
                                          "(%.2f) — zu leise zum Nachmessen"
                                          % (g.tag, k, partner, hh))
                            continue
                        d_ = 2.0 - 2.0 * abs(ww)
                        db_ = 10.0 * math.log10(d_) if d_ > 1e-12 else -120.0
                        if db_ <= args.mono:
                            zeilen.append("%s-%02d = " % (g.tag, k))
                            zeilen.append("%s-%02d = -            "
                                          "# gleich wie %02d (%.0f dB)%s"
                                          % (g.tag, partner, k, db_,
                                             ", leise gemessen"
                                             if urteil == "Paar?" else ""))
                            continue
                        # Steht das Paar nur über die Laufzeit, ist die
                        # Differenz nach 2-2r ohne Aussage — dann nennt der
                        # Kommentar die Laufzeit statt einer Scheinzahl.
                        ueber_laufzeit = (abs(ww) < args.aehnlich
                                          and vw is not None
                                          and abs(vw) >= args.aehnlich)
                        if ueber_laufzeit:
                            zeilen.append("%s-%02d+%02d =            "
                                          "# %s: Raummikros, %.0f ms "
                                          "auseinander (Ähnlichkeit dann "
                                          "%.2f), Pegelverlauf %s"
                                          % (g.tag, k, partner, urteil,
                                             abs(vms), abs(vw),
                                             "-" if hh is None
                                             else "%.2f" % hh))
                            continue
                        zeilen.append("%s-%02d+%02d =            "
                                      "# %s: Wellenform %.2f, Pegelverlauf "
                                      "%s, Differenz %.0f dB"
                                      % (g.tag, k, partner, urteil, ww,
                                         "-" if hh is None else "%.2f" % hh,
                                         db_))
                        continue
                if k in mitten:
                    i_, j_ = mitten[k]
                    zeilen.append("%s-%02d = -            "
                                  "# Mitte von %02d und %02d, aus dem Paar "
                                  "wieder herstellbar" % (g.tag, k, i_, j_))
                    continue
                # War DIESE Spur (nicht ihr Nachbar) in jedem Fenster zu
                # leise? Dann sagt der Kommentar, woran es lag — und was
                # digital still ist, wird gleich abgewählt.
                gemessen = any(k in ij for ij in werte)
                proben = len(pegel.get(k) or [])
                if not gemessen and proben and zu_leise.get(k, 0) >= proben:
                    mp = float(np.median(pegel[k]))
                    # Abgewählt wird nur, was den GANZEN Abend still war.
                    # "In meinen Fenstern leise" heißt noch lange nicht
                    # leer — eine Moderation spricht eben nur zweimal.
                    hk = huellen.get(k) or []
                    hoch = max(hk) if hk else None
                    if hoch is not None and hoch < STILL_DBFS:
                        # Was den ganzen Abend still war, gehört nicht in
                        # die Namensdatei — es kommt ohnehin nie heraus.
                        still_raus.append(k)
                    elif hoch is not None:
                        zeilen.append("%s-%02d =            "
                                      "# in den Messfenstern %s, Höchstwert "
                                      "des Abends %.0f dBFS"
                                      % (g.tag, k,
                                         "ohne Signal" if mp <= -199.0
                                         else "zu leise (%.0f dBFS)" % mp,
                                         hoch))
                    else:
                        zeilen.append("%s-%02d =            "
                                      "# zu leise zum Messen (%.0f dBFS)"
                                      % (g.tag, k, mp))
                    continue
                zeilen.append("%s-%02d = " % (g.tag, k))
            if still_raus:
                zeilen.append("# nicht aufgeführt, weil den ganzen Abend "
                              "still: %s %s" % (g.tag, kurzliste(still_raus)))

            vorschlaege.extend(zeilen)
            vorschlaege.append("")
    if vorschlaege:
        sag("")
        sag("--- Vorschlag für die Namensdatei " + "-" * 40)
        for z in vorschlaege:
            sag(z)
        sag("-" * 74)
        if args.vorschlag:
            ziel = Path(args.vorschlag).expanduser()
            if not ziel.is_absolute() and getattr(args, "ziel", None):
                # Ohne Pfadangabe neben die anderen Ergebnisse legen,
                # nicht irgendwohin, wo die Eingabeaufforderung gerade steht.
                zo = Path(args.ziel).expanduser()
                if zo.is_dir() or not zo.exists():
                    zo.mkdir(parents=True, exist_ok=True)
                    ziel = zo / ziel.name
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_text("\n".join(vorschlaege) + "\n", encoding="utf-8")
            sag("Geschrieben: %s" % ziel.resolve())
    sag("")
    sag("Differenz: unter %.0f dB mono · bis -18 dB schmales Stereo · "
        "bis -5 dB deutliches" % args.mono)
    sag("Stereo · darüber verschiedene Quellen. Sie zählt, nicht die "
        "Ähnlichkeit:")
    sag("0,998 sieht nach identisch aus, sind aber -24 dB Differenz und gut "
        "hörbar.")


def proben_je_kanal(np, roh, kanaele, bits):
    """Rohbytes einer WAV in (Matrix, Anschlagsgrenze).

    Nicht umgerechnet, nur umgedeutet: 32-Bit-Wörter bleiben 32-Bit, die
    Grenze wandert stattdessen. Ein Verschieben um 8 Bit würde bei jedem
    Block eine Kopie von hundert Megabyte kosten.
    """
    if bits == 32:
        a = np.frombuffer(roh, dtype="<i4")
        grenze = 8388607 << 8            # 24 Bit im 32-Bit-Wort
    elif bits == 24:
        b = np.frombuffer(roh, dtype=np.uint8).reshape(-1, 3)
        a = (b[:, 0].astype(np.int32)
             | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int8).astype(np.int32) << 16))
        grenze = 8388607
    elif bits == 16:
        a = np.frombuffer(roh, dtype="<i2")
        grenze = 32767
    else:
        return None, 0
    n = (a.size // kanaele) * kanaele
    return a[:n].reshape(-1, kanaele), grenze


def zaehle_anschlag(np, pfad, folge):
    """Wie oft berührt eine Aufnahme den Vollausschlag?

    Zurück je Kanal: (Proben am Anschlag, Ereignisse ab `folge` Proben,
    längster Lauf, Probe des ersten Treffers). Ein einzelnes Sample am
    Anschlag ist normal und unhörbar — erst mehrere hintereinander sind
    eine abgeschnittene Kuppe.

    Die teure Auswertung Kanal für Kanal läuft nur über die Spalten, die im
    jeweiligen Block überhaupt einen Treffer haben. Bei zweiunddreißig
    Spuren sind das fast immer null.
    """
    info = wav_kopf(pfad)
    ch, rate = info["kanaele"], info["rate"]
    breite = info["align"] // ch
    haeppchen = 32 << 20                      # Bytes je Runde
    stapel = max(1, haeppchen // info["align"]) * info["align"]
    gesamt = np.zeros(ch, dtype=np.int64)
    ereignisse = np.zeros(ch, dtype=np.int64)
    laengster = np.zeros(ch, dtype=np.int64)
    erste = np.full(ch, -1, dtype=np.int64)
    offen = np.zeros(ch, dtype=np.int64)
    ab = 0
    with open(pfad, "rb") as f:
        f.seek(info["offset"])
        rest = info["daten"]
        while rest > 0:
            roh = f.read(min(stapel, rest))
            if not roh:
                break
            rest -= len(roh)
            a, grenze = proben_je_kanal(np, roh, ch, breite * 8)
            if a is None:
                return None, rate, 0
            hier = a.shape[0]
            treffer = (a >= grenze) | (a <= -grenze - 1)
            if treffer.any():
                gesamt += treffer.sum(axis=0)
                spalten = list(np.flatnonzero(treffer.any(axis=0)))
            else:
                spalten = []
            # Läufe, die im Vorblock offen waren und hier nicht weitergehen
            for k in list(np.flatnonzero(offen)):
                if k not in spalten:
                    if offen[k] >= folge:
                        ereignisse[k] += 1
                    laengster[k] = max(laengster[k], offen[k])
                    offen[k] = 0
            for k in spalten:
                t = np.ascontiguousarray(treffer[:, k]).view(np.int8)
                if erste[k] < 0:
                    erste[k] = ab + int(np.argmax(t))
                d = np.diff(t, prepend=np.int8(0), append=np.int8(0))
                an = np.flatnonzero(d == 1)
                aus = np.flatnonzero(d == -1)
                laengen = (aus - an).astype(np.int64)
                if an[0] == 0 and offen[k]:
                    laengen[0] += offen[k]
                offen[k] = 0
                if aus[-1] == hier:           # Lauf reicht über das Blockende
                    offen[k] = laengen[-1]
                    laengen = laengen[:-1]
                if laengen.size:
                    ereignisse[k] += int((laengen >= folge).sum())
                    laengster[k] = max(laengster[k], int(laengen.max()))
            ab += hier
    for k in range(ch):
        if offen[k]:
            if offen[k] >= folge:
                ereignisse[k] += 1
            laengster[k] = max(laengster[k], offen[k])
    return (list(zip(gesamt.tolist(), ereignisse.tolist(),
                     laengster.tolist(), erste.tolist())), rate, ab)


def anschlag_zeilen(dateien, folge=3):
    """Kurzbericht über den Vollausschlag der eben geschriebenen Dateien.

    Zurück kommt eine Liste fertiger Textzeilen — leer, wenn nichts anstößt.
    Ohne numpy gibt es eine einzelne Hinweiszeile statt einer Messung.
    """
    np = _numpy()
    if np is None:
        return ["Anschlag nicht geprüft (numpy fehlt)"]
    zeilen = []
    for pf in dateien:
        try:
            reihen, rate, _n = zaehle_anschlag(np, pf, folge)
        except Exception:
            continue
        if not reihen:
            continue
        name = Path(pf).stem.split("_")[0]
        for k, (summe, ereig, lang, erste_p) in enumerate(reihen, 1):
            if not summe:
                continue
            seite = "" if len(reihen) < 2 else (" L", " R")[k - 1]
            wo = erste_p / float(rate) if erste_p >= 0 else 0.0
            zeilen.append("%-12s %7d Proben · %5d Ereignisse · längster %5d "
                          "· ab %s"
                          % (name + seite, summe, ereig, lang, dauer_text(wo)))
    return zeilen


def cmd_anschlag(args):
    """Zählt, wie oft der Vollausschlag berührt wird — je Datei und Kanal."""
    np = _numpy()
    if np is None:
        abbruch("dieser Befehl braucht numpy:  pip3 install --user numpy")
    dateien = []
    for roh in args.pfade:
        p = Path(roh).expanduser()
        if p.is_dir():
            dateien.extend(sorted(p.rglob("*.wav") if args.rekursiv
                                  else p.glob("*.wav")))
        elif p.exists():
            dateien.append(p)
        else:
            warnung("nicht gefunden: %s" % p)
    if not dateien:
        abbruch("keine WAV-Dateien gefunden")
    sag("%d Datei(en), Anschlag ab %d Proben hintereinander als Clipping "
        "gezählt" % (len(dateien), args.folge))
    sag("")
    sauber_n = 0
    betroffen = 0
    for p in dateien:
        try:
            reihen, rate, n = zaehle_anschlag(np, p, args.folge)
        except Exception as e:
            warnung("%s: %s" % (p.name, e))
            continue
        if reihen is None:
            warnung("%s: Bittiefe wird nicht unterstützt" % p.name)
            continue
        auffaellig = [(k, r) for k, r in enumerate(reihen, 1) if r[0]]
        if not auffaellig:
            sauber_n += 1
            continue
        betroffen += 1
        sag("%s" % p.name)
        sag("   Kanal    Proben   je Million   Ereignisse   längster   "
            "erste Stelle   Urteil")
        for k, (summe, ereig, lang, ersteprobe) in auffaellig:
            wo = ersteprobe / float(rate) if ersteprobe >= 0 else 0.0
            anteil = summe * 1e6 / float(max(1, n))
            urteil = ("Clipping" if ereig else
                      "nur einzelne Proben — unhörbar")
            sag("   %5d %9d %12.1f %12d %10d   %6.2f s       %s"
                % (k, summe, anteil, ereig, lang, wo, urteil))
    sag("")
    sag("%d Datei(en) ohne jeden Anschlag, %d mit." % (sauber_n, betroffen))
    sag("Ein einzelnes Sample am Anschlag ist normal. Erst mehrere "
        "hintereinander sind eine")
    sag("abgeschnittene Kuppe — und erst viele davon hört man als "
        "Verzerrung.")


def cmd_umbenennen(args):
    ordner = Path(args.ordner).expanduser()
    if not ordner.is_dir():
        abbruch("%s ist kein Ordner" % ordner)
    zuordnung = lade_namen(args.namen)
    if not zuordnung:
        abbruch("keine Zuordnungen gefunden (Format:  PULT1-07 = Snare)")
    dateien = sorted(ordner.rglob("*.wav")) if args.rekursiv \
        else sorted(ordner.glob("*.wav"))
    n = 0
    for p in dateien:
        m = NAME_MUSTER.match(p.name)
        lang = m is not None
        if m is None:
            m = NAME_MUSTER_KURZ.match(p.name)
        if not m:
            continue
        sp = m.group("spur").upper()
        # Auch Stereopaare: PULT1-17+18 muss PULT1-17+18 aus der Namensdatei
        # finden, unabhängig von führenden Nullen.
        tp = re.fullmatch(r"([A-Z0-9]+)-0*(\d+)\+0*(\d+)", sp)
        if tp:
            schluessel = "%s-%d+%d" % (tp.group(1), int(tp.group(2)),
                                       int(tp.group(3)))
        else:
            te = re.fullmatch(r"([A-Z0-9]+)-0*(\d+)", sp)
            schluessel = ("%s-%d" % (te.group(1), int(te.group(2)))
                          if te else sp)
        name = zuordnung.get(schluessel)
        if not name:
            continue
        if lang:
            neu = p.with_name("%s_%s_%s" % (m.group("spur"), sauber(name),
                                            m.group("rest")))
        else:
            neu = p.with_name("%s_%s%s" % (m.group("spur"), sauber(name),
                                           m.group("rest")))
        if neu == p:
            continue
        if neu.exists():
            warnung("gibt es schon, übersprungen: %s" % neu.name)
            continue
        p.rename(neu)
        n += 1
    sag("%d Dateien umbenannt." % n)


# ------------------------------------------------------------ Kommandozeile

def main():
    p = argparse.ArgumentParser(
        prog="schnitt.py",
        description="Mehrspuraufnahmen in Stücke und Einzelspuren zerlegen",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Beispiele:

  # 1. Einmal den ganzen Abend vermessen
  ./schnitt.py karte ~/Aufnahmen

  # 2. Nachsehen, was in einem Fenster belegt ist
  ./schnitt.py pegel ~/Aufnahmen --von 20:14 --dauer 6:30

  # 3. Ein Stück schneiden
  ./schnitt.py schnitt ~/Aufnahmen --von 20:14 --dauer 6:30 --name "Band XY"

  # 4. Alle Stücke auf einmal
  ./schnitt.py stuecke ~/Aufnahmen --datei stuecke.txt

  # Spuren von Hand vorgeben statt automatisch
  ./schnitt.py schnitt ~/Aufnahmen --von 20:14 --dauer 6:30 \\
      --spuren "PULT1:1-18; PULT2:1-8"
""")
    p.add_argument("--version", action="version", version="schnitt.py " + VERSION)
    unter = p.add_subparsers(dest="befehl")

    def gemeinsam(sp, mit_fenster=True):
        sp.add_argument("ordner", nargs="+",
                        help="Aufnahmeordner oder deren Elternordner")
        sp.add_argument("--ziel", default="schnitt-ausgabe",
                        help="Ausgabeordner (Vorgabe: schnitt-ausgabe)")
        sp.add_argument("--referenz", default=None,
                        help="Gerät, dessen Zeitachse gilt (Vorgabe: das erste)")
        sp.add_argument("--schwelle", type=float, default=-50.0,
                        help="ab welchem Spitzenpegel eine Spur als belegt gilt "
                             "(dBFS, Vorgabe -50)")
        sp.add_argument("--abstand", type=float, default=20.0,
                        help="Übersprech-Verdacht ab so viel dB unter dem "
                             "lautesten Kanal (Vorgabe 20)")
        sp.add_argument("--block", type=float, default=0.25,
                        help="Zeitraster der Auswertung in Sekunden "
                             "(Vorgabe 0,25). Keine Stichprobe: je Block zählt "
                             "der echte Höchstwert aller Proben. Gröber spart "
                             "nur Platz in karte.json, nicht Rechenzeit")
        sp.add_argument("--frisch", action="store_true",
                        help="karte.json ignorieren und neu messen")
        sp.add_argument("--kein-auto", action="store_true",
                        help="fehlende karte.json und sync.json NICHT von "
                             "selbst anlegen")
        if mit_fenster:
            sp.add_argument("--von", required=True, help="Startzeit HH:MM[:SS]")
            sp.add_argument("--dauer", default=None,
                            help="Dauer: 6:30 = 6 min 30 s · 1:20:00 · 120 = 120 min")
            sp.add_argument("--bis", default=None, help="Endzeit HH:MM[:SS]")

    sp = unter.add_parser("karte", help="den ganzen Abend vermessen")
    gemeinsam(sp, mit_fenster=False)
    sp.add_argument("--raster", type=float, default=60.0,
                    help="Breite einer Spalte im Wärmebild in Sekunden")
    sp.add_argument("--stueck-schwelle", type=float, default=-40.0,
                    help="ab welchem Pegel ein Stück läuft (Vorgabe -40 dBFS)")
    sp.add_argument("--pause", type=float, default=20.0,
                    help="so viele Sekunden Ruhe trennen zwei Stücke")
    sp.add_argument("--mindest", type=float, default=60.0,
                    help="kürzere Abschnitte sind keine Stücke")
    sp.add_argument("--dyn", type=float, default=20.0,
                    help="eine Spur gilt als aktiv, solange sie höchstens so "
                         "viele dB unter ihrer eigenen Spitze liegt")
    sp.add_argument("--anteil", type=float, default=0.15,
                    help="so viele der mitzählenden Spuren müssen gleichzeitig "
                         "aktiv sein, damit ein Stück läuft")
    sp.add_argument("--mindestspuren", type=int, default=2,
                    help="so viele Spuren müssen es mindestens sein — auch "
                         "wenn der Anteil kleiner ausfällt. Für Playback mit "
                         "Gesang reichen zwei")
    sp.add_argument("--immer", type=float, default=0.7,
                    help="Spuren, die über diesen Anteil des Abends aktiv "
                         "sind, zählen bei der Stückerkennung nicht mit")
    sp.add_argument("--vorlauf", type=float, default=5.0)
    sp.add_argument("--nachlauf", type=float, default=5.0)
    sp.set_defaults(fn=cmd_karte)

    sp = unter.add_parser("pegel", help="belegte Spuren in einem Fenster zeigen")
    gemeinsam(sp)
    sp.set_defaults(fn=cmd_pegel)

    def schnitt_optionen(sp):
        sp.add_argument("--format", choices=sorted(AUSGABE_FORMATE),
                        default="s24", help="Ausgabeformat (Vorgabe s24)")
        sp.add_argument("--spuren", default=None,
                        help='statt automatisch, z. B. "PULT1:1-18; PULT2:1-8". '
                             'Ein Plus fasst zwei Spuren zu EINER Stereodatei '
                             'zusammen: "PULT2:31+32"')
        sp.add_argument("--alle", action="store_true",
                        help="Stilleerkennung abschalten; digital stumme "
                             "Spuren und in der Namensdatei abgewählte "
                             "bleiben trotzdem weg")
        sp.add_argument("--stille", type=float, default=-120.0,
                        help="ab so leise gilt eine Spur als digital stumm "
                             "und wird nie ausgegeben (Vorgabe -120 dBFS)")
        sp.add_argument("--ohne-drift", action="store_true",
                        help="keine Taktkorrektur rechnen")
        sp.add_argument("--namen", default=None,
                        help="Textdatei mit Spurnamen:  PULT2-05 = Summe links")
        sp.add_argument("--mit-pegel", dest="mit_pegel",
                        action="store_true",
                        help="Spitzenpegel und Dauer zusätzlich in den "
                             "Dateinamen schreiben (PULT1-07_Snare_-08dB_"
                             "04m12s.wav)")
        sp.add_argument("--kein-anschlag", dest="kein_anschlag",
                        action="store_true",
                        help="nach dem Schnitt nicht nachzählen, wie oft der "
                             "Vollausschlag berührt wird")
        sp.add_argument("--zeige-befehl", dest="zeige_befehl",
                        action="store_true",
                        help="im Trockenlauf den vollständigen ffmpeg-Aufruf "
                             "zeigen statt nur der Dateinamen")
        sp.add_argument("--trocken", action="store_true",
                        help="nur zeigen, was liefe")

    sp = unter.add_parser("schnitt", help="ein Stück schneiden")
    gemeinsam(sp)
    schnitt_optionen(sp)
    sp.add_argument("--name", default=None, help="Name des Stücks")
    sp.set_defaults(fn=cmd_schnitt)

    sp = unter.add_parser("stuecke", help="viele Stücke aus einer Textdatei")
    gemeinsam(sp, mit_fenster=False)
    schnitt_optionen(sp)
    sp.add_argument("--datei", required=True, help="Textdatei mit den Stücken")
    sp.add_argument("--hoechstens", type=float, default=7200.0,
                    help="längste Dauer, die ohne '>' noch als Dauer gilt; "
                         "darüber wird eine zweistellige Angabe mit zwei "
                         "Doppelpunkten als Endzeit gelesen (Vorgabe 2 h)")
    sp.set_defaults(fn=cmd_stuecke)

    sp = unter.add_parser("sync", help="Taktversatz der Geräte messen")
    gemeinsam(sp, mit_fenster=False)
    sp.add_argument("--fenster", type=float, default=10.0,
                    help="Länge der Messfenster in Sekunden (Vorgabe 10). "
                         "Kurz halten: über ein langes Fenster verschmiert der "
                         "Taktversatz die Korrelation")
    sp.add_argument("--punkte", type=int, default=8,
                    help="wie viele Messpunkte über den Abend (Vorgabe 8)")
    sp.add_argument("--arbeitsrate", type=int, default=8000,
                    help="Rate für die Korrelation (Vorgabe 8000 Hz)")
    sp.add_argument("--suchweite", type=float, default=3.0,
                    help="wie weit der Versatz gesucht wird (Sekunden)")
    sp.add_argument("--guete", type=float, default=0.08,
                    help="ab welcher Korrelation eine Messung zählt")
    sp.add_argument("--signal", type=float, default=-40.0,
                    help="ab welchem Pegel eine Stelle als brauchbar gilt")
    sp.add_argument("--rand", type=float, default=30.0,
                    help="Abstand zu Anfang und Ende der Aufnahme")
    sp.add_argument("--paar", default=None,
                    help='Spuren mit demselben Signal vorgeben, '
                         'z. B. "PULT2:5" oder "PULT1:12; PULT2:5"')
    sp.add_argument("--maxppm", type=float, default=60.0,
                    help="größte örtliche Drift, die noch glaubwürdig ist. "
                         "Wird sie überschritten, wird die Kurve verworfen "
                         "und die Ausgleichsgerade benutzt")
    sp.set_defaults(fn=cmd_sync)

    sp = unter.add_parser("kuerzen",
                          help="Dateien außerhalb des Abends aussortieren")
    gemeinsam(sp)
    sp.add_argument("--puffer", type=float, default=300.0,
                    help="so viele Sekunden zusätzlich auf jeder Seite "
                         "behalten (Vorgabe 300 = 5 Minuten)")
    sp.add_argument("--verschieben", action="store_true",
                    help="in den Unterordner _weg verschieben (umkehrbar)")
    sp.add_argument("--loeschen", action="store_true",
                    help="endgültig löschen")
    sp.set_defaults(fn=cmd_kuerzen)

    sp = unter.add_parser("paare",
                          help="messen, welche Spuren dasselbe Signal tragen")
    gemeinsam(sp)
    sp.add_argument("--spuren", default=None,
                    help='auf bestimmte Spuren eingrenzen, z. B. "PULT1:17-19"')
    sp.add_argument("--arbeitsrate", type=int, default=8000)
    sp.add_argument("--aehnlich", type=float, default=0.55,
                    help="ab diesem Wert gilt ein Paar als verwandt")
    sp.add_argument("--hoechstens", type=int, default=32,
                    help="mehr Spuren als diese werden nicht geprüft")
    sp.add_argument("--fenster", type=float, default=60.0,
                    help="Länge eines Messfensters in Sekunden (Vorgabe 60)")
    sp.add_argument("--punkte", type=int, default=5,
                    help="so viele Fenster über die Spanne verteilt "
                         "(Vorgabe 5)")
    sp.add_argument("--leise", type=float, default=-45.0,
                    help="Spuren unter diesem Effektivpegel zählen im "
                         "jeweiligen Fenster nicht mit")
    sp.add_argument("--nachbar", type=float, default=0.25,
                    help="so viel Wellenform-Ähnlichkeit müssen benachbarte "
                         "Spuren mindestens haben, damit ein gleicher "
                         "Pegelverlauf sie zu einem Paar macht")
    sp.add_argument("--huelle", type=float, default=0.85,
                    help="ab dieser Ähnlichkeit der PEGELVERLÄUFE gelten zwei "
                         "benachbarte Spuren als Paar — auch wenn ihre "
                         "Wellenformen kaum korrelieren (Vorgabe 0,85)")
    sp.add_argument("--laufzeit", type=float, default=40.0,
                    help="so viele Millisekunden Laufzeit darf zwischen zwei "
                         "Raummikrofonen liegen (Vorgabe 40 ms, das sind "
                         "etwa 14 m)")
    sp.add_argument("--nachmessen", dest="nachmessen",
                    action="store_true", default=True,
                    help="für Paare ohne Messwert eigene Fenster suchen "
                         "(Vorgabe an)")
    sp.add_argument("--kein-nachmessen", dest="nachmessen",
                    action="store_false",
                    help="keine eigenen Fenster für leise Paare suchen")
    sp.add_argument("--vorschlag", default=None,
                    help="Vorschlag für die Namensdatei in diese Datei "
                         "schreiben, z. B. spuren-namen-vorschlag.txt")
    sp.add_argument("--mono", type=float, default=-50.0,
                    help="ab so leiser Differenz gelten zwei Spuren als "
                         "wirklich identisch (Vorgabe -50 dB)")
    sp.set_defaults(fn=cmd_paare)

    sp = unter.add_parser("summe",
                          help="ist eine Spur die Summe mehrerer anderer?")
    gemeinsam(sp)
    sp.add_argument("--spur", default=None,
                    help='die verdächtige Spur, z. B. "PULT1:5"')
    sp.add_argument("--aus", default=None,
                    help='woraus sie bestehen könnte, z. B. "PULT1:1,2,4,6"')
    sp.add_argument("--arbeitsrate", type=int, default=8000)
    sp.add_argument("--fenster", type=float, default=30.0,
                    help="Länge eines Messfensters in Sekunden (Vorgabe 30)")
    sp.add_argument("--punkte", type=int, default=5,
                    help="so viele Fenster über die Spanne (Vorgabe 5)")
    sp.add_argument("--leise", type=float, default=-45.0,
                    help="Fenster unter diesem Pegel zählen nicht mit")
    sp.add_argument("--laufzeit", type=float, default=40.0,
                    help="Suchbereich für den Versatz in Millisekunden")
    sp.set_defaults(fn=cmd_summe)

    sp = unter.add_parser("anschlag",
                          help="zählen, wie oft der Vollausschlag berührt "
                               "wird (Clipping)")
    sp.add_argument("pfade", nargs="+",
                    help="WAV-Dateien oder Ordner mit welchen")
    sp.add_argument("--rekursiv", action="store_true",
                    help="auch Unterordner durchsuchen")
    sp.add_argument("--folge", type=int, default=3,
                    help="ab so vielen Proben hintereinander am Anschlag "
                         "gilt es als Clipping (Vorgabe 3)")
    sp.set_defaults(fn=cmd_anschlag)

    sp = unter.add_parser("umbenennen", help="Spurnamen nachtragen")
    sp.add_argument("ordner", help="Ordner eines Stücks (oder der Elternordner)")
    sp.add_argument("namen", help="Textdatei:  PULT1-07 = Snare")
    sp.add_argument("--rekursiv", action="store_true",
                    help="auch in allen Unterordnern umbenennen")
    sp.set_defaults(fn=cmd_umbenennen)

    args = p.parse_args()
    if not getattr(args, "fn", None):
        p.print_help()
        return 1
    # Als Allererstes die Fassung nennen. Sonst rätselt man später, warum
    # eine Ausgabe anders aussieht als erwartet.
    sag("schnitt.py %s · %s · %s"
        % (VERSION, args.befehl, os.path.abspath(sys.argv[0])))
    args.fn(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nAbgebrochen.\n")
        sys.exit(130)
