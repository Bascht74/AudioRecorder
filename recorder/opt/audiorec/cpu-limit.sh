#!/usr/bin/env bash
#
# cpu-limit.sh — Taktobergrenze des Raspberry Pi setzen, anzeigen, entfernen
#
#   ./cpu-limit.sh                 anzeigen, was gilt (ohne sudo)
#   sudo ./cpu-limit.sh 1500       auf 1500 MHz begrenzen
#   sudo ./cpu-limit.sh aus        Begrenzung entfernen
#
# Wozu: Der Pi 5 laeuft mit 2400 MHz. arecord braucht davon fast nichts —
# die Aufnahme laeuft mit halbem Takt genauso. Weniger Takt heisst weniger
# Verlustleistung und damit niedrigere Temperatur. Das ist der einzige
# wirksame Hebel, wenn kein Luefter moeglich ist.
#
# Vor der Aenderung wird die Datei gesichert. Wirksam wird sie erst nach
# einem Neustart.
#
set -euo pipefail

# Pfad je nach Alter des Systems
CONF=""
for k in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$k" ]] && { CONF="$k"; break; }
done
[[ -n "$CONF" ]] || { echo "config.txt nicht gefunden." >&2; exit 1; }

MIN=600
MAX=2400

zeige() {
    echo "Datei     : $CONF"
    local gesetzt
    gesetzt="$(grep -E '^[[:space:]]*arm_freq[[:space:]]*=' "$CONF" | tail -1 || true)"
    if [[ -n "$gesetzt" ]]; then
        echo "Eingestellt: ${gesetzt//[[:space:]]/}   (wirksam nach Neustart)"
    else
        echo "Eingestellt: nichts — es gilt der Standardtakt"
    fi
    if command -v vcgencmd >/dev/null 2>&1; then
        local hz temp
        hz="$(vcgencmd measure_clock arm 2>/dev/null | cut -d= -f2 || echo 0)"
        temp="$(vcgencmd measure_temp 2>/dev/null || echo '')"
        [[ "$hz" -gt 0 ]] && echo "Gerade    : $((hz / 1000000)) MHz   ${temp#temp=}"
        echo "Gedrosselt: $(vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo '?')   (0x0 = nie)"
    fi
}

case "${1:-}" in
    ""|-h|--help|status)
        zeige
        echo
        echo "Begrenzen:   sudo $0 1500"
        echo "Aufheben :   sudo $0 aus"
        exit 0 ;;
esac

[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo starten:  sudo $0 $*" >&2; exit 1; }

BAK="$CONF.bak-$(date +%Y%m%d-%H%M%S)"
cp -a "$CONF" "$BAK"

# Vor dem Entfernen die Marken zaehlen.
#
# 'sed /anfang/,/ende/d' loescht bis zum Dateiende, wenn die Endmarke
# fehlt — dann waere die halbe config.txt weg und der Pi kaeme nach dem
# Neustart moeglicherweise nicht mehr hoch. Genau dann bricht das Skript
# hier lieber ab und sagt, was zu tun ist.
A=$(grep -c '^# --- audiorec cpu-limit start ---$' "$CONF" || true)
E=$(grep -c '^# --- audiorec cpu-limit ende ---$'  "$CONF" || true)
if [[ "$A" != "$E" ]]; then
    echo "In $CONF stehen $A Anfangs-, aber $E Endmarken." >&2
    echo "Der eigene Block ist beschaedigt. Bitte von Hand aufraeumen:" >&2
    echo "    sudo nano $CONF     # Zeilen '# --- audiorec cpu-limit ...' pruefen" >&2
    echo "Sicherung dieses Aufrufs: $BAK" >&2
    exit 1
fi

# Frueheren eigenen Block herausnehmen. Die config.txt ist in Abschnitte
# geteilt ([all], [pi5], [cm4] …); eine Einstellung gilt nur fuer den
# Abschnitt, in dem sie steht. Deshalb wird der Block immer als Ganzes
# entfernt und mit eigenem [all] neu angehaengt — dann gilt er sicher.
TMP="$(mktemp)"
sed '/^# --- audiorec cpu-limit start ---$/,/^# --- audiorec cpu-limit ende ---$/d' \
    "$CONF" > "$TMP"

# Zweiter Riegel: es duerfen nur die eigenen Blockzeilen verschwunden sein
# (5 Zeilen je Block plus die Leerzeile davor).
VOR=$(wc -l < "$CONF"); NACH=$(wc -l < "$TMP")
if (( VOR - NACH > A * 6 + 2 )); then
    rm -f "$TMP"
    echo "Abbruch: beim Entfernen des eigenen Blocks waeren $((VOR - NACH))" >&2
    echo "Zeilen verschwunden, erwartet waren hoechstens $((A * 6 + 2))." >&2
    echo "$CONF wurde NICHT veraendert. Sicherung: $BAK" >&2
    exit 1
fi

if [[ "$1" == "aus" || "$1" == "off" || "$1" == "0" ]]; then
    install -m 0755 -o root -g root "$TMP" "$CONF"
    rm -f "$TMP"
    echo "Begrenzung entfernt. Sicherung: $BAK"
    echo "Wirksam nach:  sudo reboot"
    exit 0
fi

if ! [[ "$1" =~ ^[0-9]+$ ]] || (( $1 < MIN || $1 > MAX )); then
    rm -f "$TMP"
    echo "Wert muss eine Zahl zwischen $MIN und $MAX sein (MHz). Bekommen: $1" >&2
    exit 1
fi

# Steht ausserhalb unseres Blocks noch ein arm_freq? Dann entscheidet die
# Reihenfolge der Abschnitte, welches gilt — darauf muss hingewiesen werden.
FREMD=0
grep -qE '^[[:space:]]*arm_freq[[:space:]]*=' "$TMP" && FREMD=1

{
    echo ""
    echo "# --- audiorec cpu-limit start ---"
    echo "[all]"
    echo "arm_freq=$1"
    echo "# --- audiorec cpu-limit ende ---"
} >> "$TMP"

install -m 0755 -o root -g root "$TMP" "$CONF"
rm -f "$TMP"

if [[ $FREMD -eq 1 ]]; then
    echo "Hinweis: in der Datei stand schon ein anderes arm_freq —"
    echo "         welches gilt, haengt vom Abschnitt ab. Nachsehen:"
    echo "         grep -n arm_freq $CONF"
fi

echo "Takt auf $1 MHz begrenzt."
echo "Sicherung: $BAK"
echo
zeige
echo
echo "Wirksam nach:  sudo reboot"
echo "Danach pruefen:  $0"
echo
echo "Falls der Pi nach dem Neustart nicht hochkommt: SD-Karte an einen"
echo "anderen Rechner, in config.txt die Zeile 'arm_freq=$1' loeschen."
