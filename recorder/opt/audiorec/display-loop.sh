#!/usr/bin/env bash
#
# Startet die Statusanzeige und holt sie zurueck, falls sie stirbt.
# Wird ueber den Autostart des Desktops aufgerufen.
#
# Beenden von aussen:   pkill -f display-loop.sh ; pkill -f status_display.py
#
export DISPLAY="${DISPLAY:-:0}"

while true; do
    python3 /opt/audiorec/status_display.py
    code=$?
    # 0 = absichtlich beendet (Strg-Alt-Q an der Werkbank) -> nicht neu starten
    [ "$code" -eq 0 ] && break
    logger -t audiorec-display "Statusanzeige beendet (Code $code) – Neustart in 3 s"
    sleep 3
done
