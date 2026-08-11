#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Misst den Zeitversatz zwischen zwei WAV-Dateien per Kreuzkorrelation.

    python3 versatz.py A.wav B.wav [--ab 30] [--laenge 60]

Positiver Wert: B liegt SPAETER als A.
"""
import os, struct, sys
import numpy as np

VERSION = "1.1"

def kopf(p):
    gr = os.path.getsize(p)
    with open(p, "rb") as f:
        h = f.read(12)
        if h[0:4] not in (b"RIFF", b"RF64") or h[8:12] != b"WAVE":
            raise SystemExit("%s ist keine WAV-Datei" % p)
        rf64 = h[0:4] == b"RF64"; ds64 = None; info = {}
        while True:
            k = f.read(8)
            if len(k) < 8: break
            kid, ln = k[0:4], struct.unpack("<I", k[4:8])[0]
            pos = f.tell()
            if kid == b"ds64":
                b = f.read(min(ln, 28))
                if len(b) >= 16: ds64 = struct.unpack("<Q", b[8:16])[0]
            elif kid == b"fmt ":
                b = f.read(min(ln, 40))
                tag, ch, rate, _br, align, bits = struct.unpack("<HHIIHH", b[0:16])
                if tag == 0xFFFE and len(b) >= 26:
                    g = struct.unpack("<H", b[18:20])[0]
                    if g: bits = g
                info.update(ch=ch, rate=rate, align=align, bits=bits)
            elif kid == b"data":
                d = ds64 if (rf64 and ln == 0xFFFFFFFF and ds64) else ln
                echt = gr - pos
                if d in (0, 0xFFFFFFFF) or d > echt: d = echt
                info.update(off=pos, daten=d); break
            f.seek(pos + ln + (ln & 1))
    info["frames"] = info["daten"] // info["align"]
    return info

def lies(p, ab_s, laenge_s):
    i = kopf(p); bpp = i["align"] // i["ch"]
    ab = min(int(ab_s * i["rate"]), i["frames"])
    n = min(int(laenge_s * i["rate"]), i["frames"] - ab)
    with open(p, "rb") as f:
        f.seek(i["off"] + ab * i["align"]); roh = f.read(n * i["align"])
    if bpp == 4:
        a = np.frombuffer(roh, "<i4").reshape(-1, i["ch"]).astype(np.float64) / 2**31
    elif bpp == 3:
        b = np.frombuffer(roh, np.uint8).reshape(-1, i["ch"], 3)
        a = ((b[:, :, 0].astype(np.int32)) | (b[:, :, 1].astype(np.int32) << 8)
             | (b[:, :, 2].astype(np.int8).astype(np.int32) << 16)).astype(np.float64) / 2**23
    else:
        a = np.frombuffer(roh, "<i2").reshape(-1, i["ch"]).astype(np.float64) / 2**15
    return a.mean(axis=1), i

def messe(a, b, rate, weite_s=3.0, um=None):
    """um: Sekunden, um die herum gesucht wird (sonst voller Bereich).

    Musik ist periodisch: ohne Einschraenkung springt die Korrelation gern
    auf einen Nachbarschlag und liefert einen Wert, der um einen halben Takt
    danebenliegt - bei hoher Guete. Die erste Messung sucht deshalb breit,
    alle weiteren nur noch in der Naehe des ersten Ergebnisses."""
    n = min(len(a), len(b)); a = a[:n] - a[:n].mean(); b = b[:n] - b[:n].mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0: return None
    N = 1 << (2 * n - 1).bit_length()
    r = np.fft.irfft(np.fft.rfft(a, N) * np.conj(np.fft.rfft(b, N)), N)
    w = int(weite_s * rate)
    r = np.concatenate((r[-w:], r[:w + 1]))
    if um is not None:
        mitte = w - int(round(um * rate))
        eng = int(0.030 * rate)
        lo, hi = max(0, mitte - eng), min(len(r), mitte + eng + 1)
        maske = np.zeros(len(r), bool); maske[lo:hi] = True
        r = np.where(maske, r, 0.0)
    i = int(np.argmax(np.abs(r)))
    y0, y1, y2 = abs(r[max(0, i-1)]), abs(r[i]), abs(r[min(len(r)-1, i+1)])
    fein = 0.5 * (y0 - y2) / (y0 - 2*y1 + y2) if (y0 - 2*y1 + y2) != 0 else 0.0
    return (-(i - w + fein) / rate, float(y1 / (na * nb)))

if __name__ == "__main__":
    roh = sys.argv[1:]
    args, opt, i = [], {}, 0
    while i < len(roh):
        if roh[i] in ("--ab", "--laenge"):
            if i + 1 >= len(roh):
                raise SystemExit("%s braucht einen Wert" % roh[i])
            opt[roh[i][2:]] = float(roh[i + 1]); i += 2; continue
        if roh[i].startswith("--"):
            raise SystemExit("unbekannte Option: %s" % roh[i])
        args.append(roh[i]); i += 1
    print("versatz.py %s · %s" % (VERSION, os.path.abspath(sys.argv[0])))
    if len(args) < 2: raise SystemExit(__doc__)
    if len(args) > 2:
        print("ACHTUNG: %d Dateien uebergeben, verglichen werden nur die "
              "ersten beiden." % len(args))
        for x in args: print("   " + x)
        print("   (Ein Suchmuster trifft oft mehr als gedacht - z. B. Logics "
              "umgerechnete\n    Kopien mit '-44k' im Namen.)")
        print()
    ab, laenge = opt.get("ab", 0.0), opt.get("laenge", 60.0)
    a, ia = lies(args[0], ab, laenge); b, ib = lies(args[1], ab, laenge)
    print("A: %-52s %d Hz, %d Bit, %.3f s" % (os.path.basename(args[0]), ia["rate"], ia["bits"], ia["frames"]/ia["rate"]))
    print("B: %-52s %d Hz, %d Bit, %.3f s" % (os.path.basename(args[1]), ib["rate"], ib["bits"], ib["frames"]/ib["rate"]))
    if ia["rate"] != ib["rate"]:
        print("Verschiedene Abtastraten (%d und %d Hz) - B wird zum Messen "
              "umgerechnet." % (ia["rate"], ib["rate"]))
    dauer = min(ia["frames"]/ia["rate"], ib["frames"]/ib["rate"])
    um = None
    for start in (ab, ab + max(0.0, (dauer - laenge - ab) / 2), max(ab, dauer - laenge)):
        x, _ = lies(args[0], start, laenge); y, _ = lies(args[1], start, laenge)
        if ib["rate"] != ia["rate"] and len(y) > 1:
            n = int(round(len(y) * ia["rate"] / float(ib["rate"])))
            y = np.interp(np.arange(n) * (len(y) - 1) / float(max(1, n - 1)),
                          np.arange(len(y)), y)
        e = messe(x, y, ia["rate"], um=um)
        if e is None: continue
        print("  ab %7.1f s:  B liegt %+8.2f ms gegenüber A   (Güte %.3f)%s"
              % (start, e[0]*1000, e[1], "" if um is None else "   [eng gesucht]"))
        if um is None: um = e[0]
