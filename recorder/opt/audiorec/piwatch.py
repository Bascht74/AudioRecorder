#!/usr/bin/env python3
"""Zustand des Pi selbst: Temperatur, Drosselung, Leistungsaufnahme.

Wird von audiorec-status und von der Statusanzeige benutzt. Alle Werte
kommen von vcgencmd; fehlt das Programm oder die Berechtigung, liefern
die Funktionen None statt einer Ausnahme.

  python3 piwatch.py        einmal ausgeben
  python3 piwatch.py -w     fortlaufend

WICHTIG zur Leistung: Der PMIC des Pi 5 misst nur die Zweige, die er
selbst versorgt. Die 5-V-Schiene mit USB-Geraeten, HATs und NVMe haengt
NICHT daran. Was hier steht, ist der Pi ohne Peripherie – Audio-Interface
und SSD sind darin nicht enthalten.
"""

import os
import re
import subprocess
import time
from pathlib import Path

# Zeitpunkt und Ergebnis der letzten Messung, damit haeufige Aufrufe
# nicht dauernd Prozesse starten.
_cache = {"t": 0.0, "wert": None}


def _vcgencmd(*args):
    try:
        r = subprocess.run(["vcgencmd", *args], capture_output=True,
                           text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def temperatur():
    """CPU-Temperatur in °C, oder None."""
    out = _vcgencmd("measure_temp")
    if not out:
        return None
    m = re.search(r"([\d.]+)", out)
    return float(m.group(1)) if m else None


THROTTLE_BITS = [
    (0,  "jetzt", "Unterspannung"),
    (1,  "jetzt", "Takt begrenzt"),
    (2,  "jetzt", "gedrosselt"),
    (3,  "jetzt", "Temperaturgrenze"),
    (16, "seit dem Start", "Unterspannung"),
    (17, "seit dem Start", "Takt begrenzt"),
    (18, "seit dem Start", "gedrosselt"),
    (19, "seit dem Start", "Temperaturgrenze"),
]


def drosselung():
    """(jetzt, seit_start) als Listen von Klartext-Meldungen, oder None.

    Bit 0-3 gelten fuer den Moment, Bit 16-19 merken sich, ob es seit dem
    Einschalten schon einmal vorkam. Gerade der zweite Teil ist bei einem
    langen Abend die eigentliche Auskunft: eine Unterspannung um 20 Uhr
    steht dort auch um Mitternacht noch.
    """
    out = _vcgencmd("get_throttled")
    if not out:
        return None
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    if not m:
        return None
    wert = int(m.group(1), 16)
    jetzt = [t for b, w, t in THROTTLE_BITS if w == "jetzt" and wert & (1 << b)]
    frueher = [t for b, w, t in THROTTLE_BITS
               if w == "seit dem Start" and wert & (1 << b)]
    return jetzt, frueher


def leistung_w():
    """Geschaetzte Leistungsaufnahme des Pi in Watt, oder None.

    Summiert alle Spannungs-/Stromzweige des PMIC. Ohne USB-Peripherie –
    siehe Modulkopf.
    """
    out = _vcgencmd("pmic_read_adc")
    if not out:
        return None
    strom, spannung = {}, {}
    for zeile in out.splitlines():
        m = re.match(r"\s*(\S+?)_([AV])\s+\w+\(\d+\)=([\d.]+)[AV]", zeile.strip())
        if not m:
            continue
        name, art, wert = m.group(1), m.group(2), float(m.group(3))
        (strom if art == "A" else spannung)[name] = wert
    if not strom or not spannung:
        return None
    watt = sum(a * spannung[n] for n, a in strom.items() if n in spannung)
    return round(watt, 2) if watt > 0 else None


# ---------------------------------------------------------------- CPU

_cpu_letzte = {"gesamt": 0, "leerlauf": 0}


def cpu_prozent():
    """Auslastung aller Kerne in Prozent seit dem letzten Aufruf.

    Der erste Aufruf liefert None – es fehlt der Vergleichswert.
    """
    try:
        zeile = Path("/proc/stat").read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    werte = [int(x) for x in zeile.split()[1:]]
    if len(werte) < 5:
        return None
    gesamt = sum(werte)
    leerlauf = werte[3] + werte[4]          # idle + iowait
    dg = gesamt - _cpu_letzte["gesamt"]
    dl = leerlauf - _cpu_letzte["leerlauf"]
    _cpu_letzte["gesamt"], _cpu_letzte["leerlauf"] = gesamt, leerlauf
    if dg <= 0 or _cpu_letzte["gesamt"] == gesamt == dg:
        return None
    return round(max(0.0, min(100.0, (dg - dl) / dg * 100.0)), 1)


def loadavg():
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


# ------------------------------------------------------- Schreibpuffer

def schreibpuffer_mb():
    """(wartend, gerade unterwegs) in MB.

    "Dirty" ist geschriebenes, aber noch nicht auf das Medium gebrachtes
    Material – die eigentliche Warteschlange. Waechst der Wert dauerhaft,
    kommt der Datentraeger nicht mehr hinterher. Der Einbruch der
    Datenrate kommt erst danach.
    """
    dirty = writeback = None
    try:
        for zeile in Path("/proc/meminfo").read_text().splitlines():
            if zeile.startswith("Dirty:"):
                dirty = int(zeile.split()[1]) / 1024.0
            elif zeile.startswith("Writeback:"):
                writeback = int(zeile.split()[1]) / 1024.0
            if dirty is not None and writeback is not None:
                break
    except (OSError, ValueError, IndexError):
        return None, None
    return (round(dirty, 1) if dirty is not None else None,
            round(writeback, 1) if writeback is not None else None)


def _blockgeraet(pfad):
    """Zu einem Pfad das Blockgeraet finden (z. B. sda, mmcblk0)."""
    try:
        st = os.stat(pfad)
    except OSError:
        return None
    maj, min_ = os.major(st.st_dev), os.minor(st.st_dev)
    p = Path(f"/sys/dev/block/{maj}:{min_}")
    if not p.exists():
        return None
    try:
        ziel = p.resolve()
    except OSError:
        return None
    # Partition -> Elterngeraet (sda1 -> sda), sonst das Geraet selbst
    if (ziel / "partition").exists():
        ziel = ziel.parent
    return ziel.name


_io_letzte = {}


def geraet_last(pfad):
    """(Name, Auslastung %, laufende Anfragen) fuer das Medium unter pfad.

    Auslastung kommt aus io_ticks: Millisekunden, in denen das Geraet
    beschaeftigt war. 100 % heisst nicht "zu langsam", aber dauerhaft
    100 % bei gleichzeitig wachsendem Schreibpuffer schon.
    """
    name = _blockgeraet(pfad)
    if not name:
        return None, None, None
    try:
        felder = Path(f"/sys/block/{name}/stat").read_text().split()
        in_flight = int(felder[8])
        io_ticks = int(felder[9])
    except (OSError, ValueError, IndexError):
        return name, None, None
    jetzt = time.time()
    alt = _io_letzte.get(name)
    _io_letzte[name] = (jetzt, io_ticks)
    if not alt:
        return name, None, in_flight
    dt = jetzt - alt[0]
    if dt <= 0:
        return name, None, in_flight
    last = (io_ticks - alt[1]) / (dt * 1000.0) * 100.0
    return name, round(max(0.0, min(100.0, last)), 0), in_flight


def alles(max_alter_s=5.0, ziel=None):
    """Alle Werte auf einmal, mit kurzem Zwischenspeicher."""
    now = time.time()
    if _cache["wert"] is not None and now - _cache["t"] < max_alter_s:
        return _cache["wert"]
    d = drosselung()
    dirty, writeback = schreibpuffer_mb()
    gname, glast, gflight = geraet_last(ziel) if ziel else (None, None, None)
    wert = {
        "temp_c": temperatur(),
        "watt": leistung_w(),
        "throttle_jetzt": d[0] if d else None,
        "throttle_frueher": d[1] if d else None,
        "cpu": cpu_prozent(),
        "load": loadavg(),
        "dirty_mb": dirty,
        "writeback_mb": writeback,
        "disk": gname,
        "disk_last": glast,
        "disk_inflight": gflight,
    }
    _cache["t"], _cache["wert"] = now, wert
    return wert


def zeile(d=None):
    """Einzeilige Zusammenfassung fuer die Anzeige."""
    d = d or alles()
    teile = []
    if d["temp_c"] is not None:
        teile.append(f"{d['temp_c']:.0f} °C")
    if d.get("cpu") is not None:
        teile.append(f"CPU {d['cpu']:.0f} %")
    if d.get("dirty_mb") is not None:
        teile.append(f"Puffer {d['dirty_mb']:.0f} MB")
    if d.get("disk") and d.get("disk_last") is not None:
        teile.append(f"{d['disk']} {d['disk_last']:.0f} %")
    if d["watt"] is not None:
        teile.append(f"{d['watt']:.1f} W (Pi ohne USB)")
    if d["throttle_jetzt"]:
        teile.append("JETZT: " + ", ".join(d["throttle_jetzt"]))
    elif d["throttle_frueher"]:
        teile.append("seit Start aufgetreten: " + ", ".join(d["throttle_frueher"]))
    elif d["throttle_jetzt"] is not None:
        teile.append("keine Drosselung")
    return " · ".join(teile) if teile else "keine Systemwerte (vcgencmd fehlt?)"


# Schwellen, ab denen die Anzeige warnt.
#
# 75 statt 70: der Pi 5 arbeitet bis 80 Grad regulaer, erst dort nimmt er
# den Takt zurueck. Eine Warnung, die den ganzen Abend steht, liest nach
# einer Stunde niemand mehr.
TEMP_WARN = 75.0           # gelb – ab hier wird die Reserve duenn
TEMP_KRITISCH = 80.0       # rot – hier drosselt der Pi 5
DIRTY_WARN_MB = 250.0      # so viel wartet nie, wenn das Medium mitkommt
CPU_WARN = 85.0


def kritisch(d=None):
    """True, wenn etwas Aufmerksamkeit braucht (rot)."""
    d = d or alles()
    if d["throttle_jetzt"] or d["throttle_frueher"]:
        return True
    if d["temp_c"] is not None and d["temp_c"] >= TEMP_KRITISCH:
        return True
    if d.get("dirty_mb") is not None and d["dirty_mb"] >= DIRTY_WARN_MB:
        return True
    return d.get("cpu") is not None and d["cpu"] >= CPU_WARN


def vorwarnung(d=None):
    """True, wenn es noch nicht kritisch ist, aber in die Richtung geht."""
    d = d or alles()
    if kritisch(d):
        return False
    return d["temp_c"] is not None and d["temp_c"] >= TEMP_WARN


if __name__ == "__main__":
    import sys
    if "-w" in sys.argv:
        try:
            while True:
                print("\r\033[K" + zeile(alles(max_alter_s=0)), end="", flush=True)
                time.sleep(1.0)
        except KeyboardInterrupt:
            print()
    else:
        d = alles(max_alter_s=0)
        print(zeile(d))
        if d["watt"] is not None:
            print()
            print("Hinweis: Der PMIC misst nur die Zweige, die er selbst")
            print("versorgt. USB-Geräte, HATs und NVMe hängen an der")
            print("5-V-Schiene und sind NICHT enthalten. Für die")
            print("Gesamtaufnahme braucht es ein USB-C-Messgerät zwischen")
            print("Netzteil und Pi.")
