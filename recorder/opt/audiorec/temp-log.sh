#!/usr/bin/env bash
#
# temp-log.sh — Temperatur, Takt und Drosselung mitschreiben
#
#   ./temp-log.sh            alle 10 s, bis Strg-C
#   ./temp-log.sh 30         alle 30 s
#   ./temp-log.sh 10 ~/kuehl-offen.log     zusaetzlich in eine Datei
#
# Gedacht zum Vergleichen: einmal so laufen lassen, wie es jetzt ist,
# dann etwas aendern (Deckel ab, hochkant stellen, Takt begrenzen) und
# noch einmal. Nach zehn Minuten hat sich die Temperatur eingependelt —
# vorher lohnt der Vergleich nicht.
#
set -eu

TAKT="${1:-10}"
DATEI="${2:-}"

command -v vcgencmd >/dev/null 2>&1 || { echo "vcgencmd fehlt." >&2; exit 1; }

kopf() {
    printf 'Zeit      Temp    Takt      CPU   Drosselung\n'
    printf -- '------------------------------------------------------\n'
}

# CPU-Auslastung aus /proc/stat zwischen zwei Messungen
letzte_g=0; letzte_i=0
cpu() {
    local z g i dg di
    z=$(head -1 /proc/stat)
    set -- $z
    shift
    g=0; for w in "$@"; do g=$((g + w)); done
    i=$(( $4 + $5 ))
    dg=$((g - letzte_g)); di=$((i - letzte_i))
    letzte_g=$g; letzte_i=$i
    if [ "$dg" -gt 0 ] && [ "$letzte_g" -ne "$dg" ]; then
        echo $(( (dg - di) * 100 / dg ))
    else
        echo "-"
    fi
}

zeile() {
    local t hz d
    t=$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2 | tr -d "'C")
    hz=$(vcgencmd measure_clock arm 2>/dev/null | cut -d= -f2)
    d=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
    printf '%s  %5s °C  %4s MHz  %3s %%   %s%s\n' \
        "$(date +%H:%M:%S)" "$t" "$((hz / 1000000))" "$(cpu)" "$d" \
        "$([ "$d" = "0x0" ] && echo "" || echo "  <-- !")"
}

cpu >/dev/null            # Bezugspunkt setzen
if [ -n "$DATEI" ]; then
    kopf | tee "$DATEI"
else
    kopf
fi

trap 'echo; echo "Beendet."; exit 0' INT TERM

while true; do
    if [ -n "$DATEI" ]; then
        zeile | tee -a "$DATEI"
    else
        zeile
    fi
    sleep "$TAKT"
done
