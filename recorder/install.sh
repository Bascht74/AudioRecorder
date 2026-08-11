#!/usr/bin/env bash
#
# audiorec – Installation auf Raspberry Pi OS (Bookworm oder neuer, 64-bit)
#
#   sudo ./install.sh                  installieren oder aktualisieren
#   sudo ./install.sh --keep-config    dabei die eigene Konfiguration behalten
#
# Beim Aktualisieren wird die bestehende Konfiguration gesichert und durch
# die neue ersetzt – sonst fehlen neue Einstellungen und alte Vorgabewerte
# bleiben stehen. Mit --keep-config bleibt die eigene erhalten, die neue
# landet als .neu daneben.
#
# Nach der Installation ist keine Bedienung mehr noetig: Interface
# anstecken, die Aufnahme startet von allein mit allen Kanaelen,
# die das Geraet anbietet.
#
set -euo pipefail

CONF=/etc/audiorec/audiorec.conf

if [[ $EUID -ne 0 ]]; then
    echo "Bitte mit sudo starten:  sudo $0" >&2
    exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------- installieren
TARGET_USER="${SUDO_USER:-pi}"
USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

echo "==> Benutzer: $TARGET_USER  ($USER_HOME)"

echo "==> Pakete prüfen und installieren"
if [[ -x "$SRC/install-packages.sh" ]]; then
    "$SRC/install-packages.sh"
else
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        alsa-utils python3 python3-tk ffmpeg tmux usbutils rsync exfatprogs
fi

echo "==> Programme nach /opt/audiorec"
install -d -m 0755 /opt/audiorec
install -m 0755 "$SRC/opt/audiorec/audiorec.py"       /opt/audiorec/
install -m 0755 "$SRC/opt/audiorec/status_display.py" /opt/audiorec/
install -m 0755 "$SRC/opt/audiorec/display-loop.sh"   /opt/audiorec/
install -m 0644 "$SRC/opt/audiorec/piwatch.py"        /opt/audiorec/
install -m 0755 "$SRC/opt/audiorec/cpu-limit.sh"      /opt/audiorec/
ln -sf /opt/audiorec/cpu-limit.sh /usr/local/bin/audiorec-cpu-limit
install -m 0755 "$SRC/opt/audiorec/temp-log.sh"       /opt/audiorec/
ln -sf /opt/audiorec/temp-log.sh  /usr/local/bin/audiorec-temp-log
install -m 0755 "$SRC/opt/audiorec/check-recording.sh" /opt/audiorec/
ln -sf /opt/audiorec/check-recording.sh /usr/local/bin/audiorec-check
install -m 0755 "$SRC/opt/audiorec/audiorec-status"   /opt/audiorec/
ln -sf /opt/audiorec/audiorec-status /usr/local/bin/audiorec-status
echo "    Statusabfrage:  audiorec-status   (fortlaufend: audiorec-status -w)"

echo "==> Programm zum Zerlegen (laeuft auf dem Mac, liegt hier zum Abholen)"
[[ -f "$SRC/split-tracks.sh" ]] && install -m 0755 "$SRC/split-tracks.sh" /opt/audiorec/

echo "==> Konfiguration nach /etc/audiorec"
install -d -m 0755 /etc/audiorec
if [[ -f "$CONF" ]] && [[ "${1:-}" == "--keep-config" ]]; then
    echo "    vorhandene Konfiguration bleibt erhalten (--keep-config)"
    install -m 0644 "$SRC/etc/audiorec/audiorec.conf" "$CONF.neu"
    echo "    neue Vorlage liegt daneben: $CONF.neu"
    echo "    Unterschiede:  diff $CONF $CONF.neu"
else
    if [[ -f "$CONF" ]]; then
        # Bei einem Update wuerde eine alte Konfiguration neue Schluessel
        # nicht kennen und alte Vorgabewerte weiterschleppen. Deshalb wird
        # sie ersetzt – und vorher gesichert, damit nichts verloren geht.
        BAK="$CONF.bak-$(date +%Y%m%d-%H%M%S)"
        cp -a "$CONF" "$BAK"
        echo "    bisherige Konfiguration gesichert: $BAK"
        echo "    (eigene Aenderungen zurueckholen:  diff $BAK $CONF)"
    fi
    install -m 0644 "$SRC/etc/audiorec/audiorec.conf" "$CONF"
    sed -i "s/^owner = pi$/owner = $TARGET_USER/" "$CONF"
    sed -i "s|^target_dir = /home/pi/rec$|target_dir = /home/$TARGET_USER/rec|" "$CONF"
    echo "    neue Konfiguration eingespielt"

    # Eigene Einstellungen, die dabei verlorengegangen sind, benennen.
    # Sonst faellt erst am Abend auf, dass min_free_gb wieder auf dem
    # Vorgabewert steht.
    if [[ -n "${BAK:-}" ]]; then
        werte() { sed -nE 's/^[[:space:]]*([a-z_]+)[[:space:]]*=[[:space:]]*(.*)$/\1=\2/p' "$1" | sort; }
        UNTERSCHIED="$(comm -23 <(werte "$BAK") <(werte "$CONF") || true)"
        if [[ -n "$UNTERSCHIED" ]]; then
            echo
            echo "    ACHTUNG – diese Werte standen vorher anders und sind jetzt"
            echo "    auf dem Vorgabewert:"
            while IFS='=' read -r k v; do
                [[ -z "$k" ]] && continue
                neu="$(sed -nE "s/^[[:space:]]*$k[[:space:]]*=[[:space:]]*(.*)$/\1/p" "$CONF" | head -1)"
                printf '      %-22s vorher: %-24s jetzt: %s\n' "$k" "$v" "${neu:-<nicht mehr vorhanden>}"
            done <<< "$UNTERSCHIED"
            echo "    Zuruecksetzen von Hand:  sudo nano $CONF"
            echo "    Vollstaendiger Vergleich: diff $BAK $CONF"
            echo
        fi
    fi
fi

echo "==> Aufnahmeordner anlegen"
# Zielordner aus der Konfiguration lesen, damit SD-Karten- und
# SSD-Variante beide funktionieren
REC_DIR="$(sed -n 's/^target_dir *= *//p' "$CONF" | head -1)"
REC_DIR="${REC_DIR:-/home/$TARGET_USER/rec}"
install -d -m 0755 "$REC_DIR"
chown "$TARGET_USER":"$TARGET_USER" "$REC_DIR"
echo "    $REC_DIR"

echo "==> systemd-Dienst anlegen und aktivieren"
install -m 0644 "$SRC/etc/systemd/system/audiorec.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable audiorec.service
echo "    audiorec.service angelegt, startet künftig automatisch beim Booten"

echo "==> Statusanzeige beim Desktop-Start"
install -d -m 0755 "$USER_HOME/.config/autostart"
cat > "$USER_HOME/.config/autostart/audiorec-display.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=audiorec Status
Comment=Statusanzeige des Mehrspur-Recorders
Exec=/opt/audiorec/display-loop.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
chown -R "$TARGET_USER":"$TARGET_USER" "$USER_HOME/.config/autostart"

echo "==> Touch-Bedienung erlauben (Herunterfahren, Dienst starten/stoppen)"
cat > /etc/sudoers.d/audiorec <<EOF
$TARGET_USER ALL=(root) NOPASSWD: /sbin/shutdown, /usr/sbin/shutdown, \\
    /usr/bin/systemctl start audiorec, /usr/bin/systemctl stop audiorec, \\
    /bin/systemctl start audiorec, /bin/systemctl stop audiorec
EOF
chmod 0440 /etc/sudoers.d/audiorec
visudo -cf /etc/sudoers.d/audiorec >/dev/null && echo "    ok"

echo "==> Schreibpuffer verkleinern (weniger Verlust bei Stromausfall)"
if [[ -f "$SRC/etc/sysctl.d/99-audiorec.conf" ]]; then
    install -m 0644 "$SRC/etc/sysctl.d/99-audiorec.conf" /etc/sysctl.d/
    sysctl --quiet -p /etc/sysctl.d/99-audiorec.conf 2>/dev/null || true
    echo "    dirty_expire_centisecs = $(cat /proc/sys/vm/dirty_expire_centisecs)"
    echo "    (rueckgaengig: sudo rm /etc/sysctl.d/99-audiorec.conf && sudo reboot)"
fi

echo "==> Bildschirmschoner und Standby abschalten"
install -d -m 0755 /etc/X11/xorg.conf.d
cat > /etc/X11/xorg.conf.d/10-blanking.conf <<'EOF'
Section "ServerFlags"
    Option "BlankTime"   "0"
    Option "StandbyTime" "0"
    Option "SuspendTime" "0"
    Option "OffTime"     "0"
EndSection
EOF

echo "==> Statusanzeige neu starten"
# Ohne das laeuft nach einem Update die alte Fassung im Speicher weiter –
# neue Anzeigefunktionen fehlen dann scheinbar grundlos.
LOOP_LAEUFT=0
pgrep -f "display-loop.sh" >/dev/null 2>&1 && LOOP_LAEUFT=1

if pgrep -f "status_display.py" >/dev/null 2>&1; then
    pkill -f "status_display.py" || true
    sleep 2
fi

if [[ $LOOP_LAEUFT -eq 1 ]]; then
    echo "    beendet – die Neustartschleife holt sie in wenigen Sekunden zurueck"
elif pgrep -u "$TARGET_USER" -x "Xorg|labwc|wayfire|lxsession" >/dev/null 2>&1 \
     || [[ -n "$(ls /tmp/.X11-unix/ 2>/dev/null)" ]]; then
    # Kein Loop vorhanden (alte Installation): selbst starten.
    echo "    starte die Anzeige neu"
    su - "$TARGET_USER" -c \
       "DISPLAY=:0 XAUTHORITY=$USER_HOME/.Xauthority \
        setsid nohup /opt/audiorec/display-loop.sh >/dev/null 2>&1 &" || true
    sleep 2
    if pgrep -f "status_display.py" >/dev/null 2>&1; then
        echo "    laeuft"
    else
        echo "    nicht gestartet – manuell:  sudo reboot"
    fi
else
    echo "    kein Desktop gefunden – die Anzeige startet beim naechsten Hochfahren"
fi

echo
echo "==> Dienst starten"
systemctl restart audiorec.service
sleep 2
systemctl is-active --quiet audiorec.service \
    && echo "    läuft" \
    || echo "    FEHLER – prüfen mit: journalctl -u audiorec -n 30"

echo
echo "  Aktuelle Konfiguration:"
grep -E "^(channels|rate|format_preference|max_file_time_s|target_dir|usb_target|min_free_gb|startup_grace_s|wait_for_clock) *=" \
     "$CONF" | sed 's/^/    /'

cat <<EOF

================================================================
 Installation fertig.

 Aufnahmeziel: Externe SSD oder Stick werden automatisch genommen,
 ohne Rueckfrage und unabhaengig vom Namen. Steckt keiner, geht die
 Aufnahme nach $REC_DIR.

 Vor der ersten Aufnahme wartet der Dienst bis zu 60 Sekunden auf
 den Datentraeger und auf den Zeitabgleich – dann startet er in
 jedem Fall.

 Naechste Schritte:

 1) Status im Terminal ansehen:
      audiorec-status         einmal
      audiorec-status -w      fortlaufend, Ende mit Strg-C

    Die Anzeige auf dem Display startet mit dem Desktop von selbst,
    laeuft im Vollbild und laesst sich per Touch nicht schliessen.
    Von aussen beenden:  pkill -f display-loop.sh

 2) Interface einstecken und zusehen:
      journalctl -u audiorec -f

 3) Neustart, damit alles automatisch hochkommt:
      sudo reboot

 Nach dem Abend: split-tracks.sh auf den Mac holen und die Aufnahme
 in benannte Einzelspuren zerlegen –
      scp $TARGET_USER@\$(hostname):/opt/audiorec/split-tracks.sh ~/
================================================================
EOF
