#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Erzeugt eine nachgebaute Aufnahme, wie sie der Pi-Recorder ablegt.

Zwei Geraete mit unterschiedlichem Quarz, Dateiwechsel wie bei arecord,
44-Byte-Kopf wie arecord, belegte und stille Spuren, ein bewusst leises
Uebersprechen und ein gemeinsames Signal auf beiden Geraeten.
"""
import json
import os
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ZIEL = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/claude/schnitt/testmaterial")
RATE = 8000
KANAELE = 32
DATEI_S = 120.0                 # arecord --max-file-time
GESAMT_S = 26 * 60.0            # Wanduhr
PPM_B = 400.0                   # Pult B laeuft schneller als Pult A
VERZUG_S = 0.0015               # Laufzeit Summe Pult A -> Eingang Pult B

A_START = datetime(2026, 8, 8, 18, 52, 0)
B_START = datetime(2026, 8, 8, 18, 51, 45)

# Stuecke in Wanduhrzeit (Start, Dauer)
STUECKE = [(datetime(2026, 8, 8, 19, 0, 0), 300.0),
           (datetime(2026, 8, 8, 19, 7, 0), 240.0),
           (datetime(2026, 8, 8, 19, 14, 0), 120.0)]

rng = np.random.default_rng(20260808)

# Bandbegrenztes Rauschen als Funktion der Wanduhrzeit: Summe vieler Sinus.
N_TON = 48
F_TON = rng.uniform(60.0, 3000.0, N_TON)[:, None]
PH_TON = rng.uniform(0.0, 2 * np.pi, N_TON)[:, None]
A_TON = 1.0 / (1.0 + F_TON / 400.0)
A_TON = A_TON / np.sqrt((A_TON ** 2).sum() / 2.0)


def rauschartig(t):
    """Deterministisch, bandbegrenzt, damit Abtastung mit anderem Takt passt."""
    return (A_TON * np.sin(2 * np.pi * F_TON * t[None, :] + PH_TON)).sum(0)


def huelle(t, ab, dauer, rand=1.0):
    """Weiche Ein-/Ausblendung ueber ein Stueck."""
    x = np.zeros_like(t)
    innen = (t >= ab) & (t <= ab + dauer)
    x[innen] = 1.0
    auf = (t >= ab) & (t < ab + rand)
    x[auf] = (t[auf] - ab) / rand
    zu = (t > ab + dauer - rand) & (t <= ab + dauer)
    x[zu] = (ab + dauer - t[zu]) / rand
    return x


def sekunden(dt, bezug):
    return (dt - bezug).total_seconds()


TAG0 = datetime(2026, 8, 8, 0, 0, 0)
ST = [(sekunden(a, TAG0), d) for a, d in STUECKE]


def laeuft(t):
    x = np.zeros_like(t)
    for ab, d in ST:
        x = np.maximum(x, huelle(t, ab, d))
    return x


def kick(t):
    """2 Schlaege je Sekunde, kurz."""
    ph = (t * 2.0) % 1.0
    return np.exp(-ph * 45.0) * np.sin(2 * np.pi * 62.0 * t) * laeuft(t)


def snare_einmal(t):
    """Genau ein Schlag je Stueck, 30 s nach dem Beginn."""
    y = np.zeros_like(t)
    for ab, d in ST:
        if d < 35:
            continue
        dt = t - (ab + 30.0)
        treffer = (dt >= 0) & (dt < 0.25)
        y[treffer] += np.exp(-dt[treffer] * 20.0) * rauschartig(t[treffer] * 3.0)
    return y


def bass(t):
    return rauschartig(t * 0.31) * laeuft(t)


def gesang(t):
    return rauschartig(t * 0.77 + 11.0) * laeuft(t)


def nur_stueck2(t):
    ab, d = ST[1]
    return rauschartig(t * 1.3 + 5.0) * huelle(t, ab, d)


def db(x):
    return 10.0 ** (x / 20.0)


def tom(t):
    """Der harte Fall: EIN Wirbel je Stueck, 0,8 s lang, sonst nichts."""
    y = np.zeros_like(t)
    for ab, d in ST:
        if d < 50:
            continue
        dt = t - (ab + 45.0)
        treffer = (dt >= 0) & (dt < 0.8)
        y[treffer] += (np.exp(-(dt[treffer] % 0.2) * 22.0)
                       * np.sin(2 * np.pi * 105.0 * t[treffer]))
    return y


def kanaele_a(t):
    """Liste (kanal_1basiert, signal)"""
    mischung = 0.5 * kick(t) + 0.4 * bass(t) + 0.4 * gesang(t)
    return [
        (1, db(-6) * kick(t)),
        (2, db(-8) * snare_einmal(t)),
        (3, db(-18) * bass(t)),
        (5, db(-36) * kick(t)),          # Uebersprechen, 30 dB unter Kanal 1
        (8, db(-10) * nur_stueck2(t)),
        # Tom: 0,8 s laut je Stueck, dazwischen nur leises Uebersprechen.
        (10, db(-5) * tom(t) + db(-52) * mischung),
        # Reines Uebersprechen: kein eigenes Signal, nur die Mischung leise.
        (12, db(-26) * mischung),
        (18, db(-24) * gesang(t)),       # leise, aber echt
    ]


def summe_a(t):
    """Was Pult A als Stereo-Summe herausgibt (Mischung aus 1 und 3)."""
    return 0.6 * db(-6) * kick(t) + 0.6 * db(-12) * bass(t)


def stimme(t, versatz, skala):
    """Wie gesang(), aber mit eigener Klangfarbe — Huellkurve bleibt in der
    Wanduhrzeit, sonst laege das Stueck woanders."""
    return rauschartig(t * skala + versatz) * laeuft(t)


RAUM_S = 0.015                  # Laufzeit zwischen den beiden Raummikros


def zuspieler(t, versatz):
    """Laeuft NUR waehrend Stueck 2 — sonst ist die Spur digital still.

    Der harte Fall fuer die Fenstersuche: ein gleichmaessiges Raster ueber
    den Abend trifft diese Spur mit hoher Wahrscheinlichkeit nie.
    """
    ab, d = ST[1]
    return rauschartig(t * 1.7 + versatz) * huelle(t, ab, d)


def kanaele_b(t):
    tv = t - VERZUG_S
    summe = summe_a(tv)
    v1 = db(-15) * stimme(t, 3.0, 0.61)
    v2 = db(-17) * stimme(t, 7.0, 0.93)
    # Zwei Raummikros, weit auseinander: dieselbe Quelle, aber mit Laufzeit
    # dazwischen, und jedes hat seinen eigenen Nahbereich. Bei Versatz null
    # korrelieren sie kaum — mit Laufzeitausgleich deutlich.
    raum_l = rauschartig(t * 2.7 + 21.0)
    raum_r = rauschartig((t - RAUM_S) * 2.7 + 21.0)
    eigen_l = rauschartig(t * 3.9 + 41.0)
    eigen_r = rauschartig(t * 4.3 + 61.0)
    pegel = 0.3 + 0.7 * laeuft(t)
    pub_l = db(-33) * (raum_l + 0.7 * eigen_l) * pegel
    pub_r = db(-33) * (raum_r + 0.7 * eigen_r) * pegel
    main = 0.4 * (v1 + v2 + summe)
    return [(1, v1), (2, v2),
            (5, db(-10) * summe), (6, db(-10) * summe * 0.98),
            (7, db(-12) * zuspieler(t, 2.0)),
            (8, db(-12) * zuspieler(t, 2.0) * 0.9
             + db(-24) * zuspieler(t, 9.0)),
            (12, pub_l), (13, pub_r),
            (31, main), (32, main * 0.99)]


def schreibe_kopf(f, kanaele, rate, bits, daten_bytes):
    """44-Byte-Kopf, genau wie arecord ihn schreibt (Format-Tag 1)."""
    align = kanaele * bits // 8
    f.write(b"RIFF")
    f.write(struct.pack("<I", 36 + daten_bytes))
    f.write(b"WAVEfmt ")
    f.write(struct.pack("<IHHIIHH", 16, 1, kanaele, rate,
                        rate * align, align, bits))
    f.write(b"data")
    f.write(struct.pack("<I", daten_bytes))


def bau(ordner, start_wanduhr, ppm, bauer, name):
    ordner.mkdir(parents=True, exist_ok=True)
    echte_rate = RATE * (1.0 + ppm / 1e6)
    frames_je_datei = int(round(DATEI_S * RATE))
    frames_gesamt = int(GESAMT_S * echte_rate)
    t0 = sekunden(start_wanduhr, TAG0)
    geschrieben = 0
    dateien = 0
    while geschrieben < frames_gesamt:
        n = min(frames_je_datei, frames_gesamt - geschrieben)
        # Wanduhr beim Oeffnen der Datei
        wand = TAG0 + timedelta(seconds=t0 + geschrieben / echte_rate)
        pfad = ordner / wand.strftime("r_%y%m%d_%H%M%S.wav")
        with open(pfad, "wb") as f:
            schreibe_kopf(f, KANAELE, RATE, 32, n * KANAELE * 4)
            blk = 120000
            ab = 0
            while ab < n:
                m = min(blk, n - ab)
                idx = np.arange(geschrieben + ab, geschrieben + ab + m)
                t = t0 + idx / echte_rate          # Wanduhrzeit dieser Proben
                puffer = np.zeros((m, KANAELE), dtype=np.float64)
                puffer += rng.normal(0.0, db(-96), (m, KANAELE))
                for k, sig in bauer(t):
                    puffer[:, k - 1] += sig
                np.clip(puffer, -0.999, 0.999, out=puffer)
                # 24 Bit im 32-Bit-Wort, wie es Pulte ueber USB liefern
                ganz = np.round(puffer * (2.0 ** 23 - 1)).astype(np.int32) * 256
                f.write(ganz.astype("<i4").tobytes())
                ab += m
        geschrieben += n
        dateien += 1
    (ordner / "aufnahme.txt").write_text(
        "Gerät       : %s\nALSA-Device : hw:1,0\nStart       : %s\n"
        "Format      : S32_LE\nKanäle      : %d\nAbtastrate  : %d Hz\n"
        "Dateilänge  : %d s\n"
        % (name, start_wanduhr.strftime("%Y-%m-%d %H:%M:%S"), KANAELE, RATE,
           int(DATEI_S)), encoding="utf-8")
    print("%-6s %2d Dateien, %d Frames, %.1f MB, echter Takt %+0.0f ppm"
          % (name, dateien, geschrieben,
             sum(os.path.getsize(p) for p in ordner.glob("*.wav")) / 1e6, ppm))


def main():
    if ZIEL.exists():
        import shutil
        shutil.rmtree(ZIEL)
    bau(ZIEL / "gig_PULT1_2026-08-08_185200", A_START, 0.0, kanaele_a, "PULT1")
    bau(ZIEL / "gig_PULT2_2026-08-08_185145", B_START, PPM_B, kanaele_b, "PULT2")
    soll = {
        "pult1_belegt": [1, 2, 3, 5, 8, 10, 12, 18],
        "pult1_uebersprechen": [5, 12],
        "pult1_tom_nur_kurz": [10],
        "pult2_belegt": [1, 2, 5, 6, 7, 8, 12, 13, 31, 32],
        "pult2_zuspieler_nur_stueck2": [7, 8],
        "pult2_raumpaar_laufzeit_s": RAUM_S,
        "ppm_pult2_gegen_pult1": PPM_B,
        "verzug_a_zu_b_s": VERZUG_S,
        "stuecke": [[a.strftime("%H:%M:%S"), d] for a, d in STUECKE],
    }
    (ZIEL / "soll.json").write_text(json.dumps(soll, indent=2), encoding="utf-8")
    print("\nSoll-Werte in", ZIEL / "soll.json")


if __name__ == "__main__":
    main()
