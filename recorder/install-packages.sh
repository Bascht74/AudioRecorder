#!/usr/bin/env bash
#
# audiorec – Paketinstallation für Raspberry Pi OS (Bookworm oder neuer)
#
#   sudo ./install-packages.sh           installieren
#        ./install-packages.sh --check   nur prüfen, ob alles da ist (ohne sudo)
#
set -euo pipefail

PACKAGES=(
    alsa-utils      # arecord – die Aufnahme-Engine
    python3         # Laufzeit für Dienst und Anzeige
    python3-tk      # Tkinter – ohne das keine Statusanzeige
    ffmpeg          # Nachbearbeitung: Zerlegen in Einzelspuren
    tmux            # manuelle Aufnahme als Rückfallebene
    usbutils        # lsusb
    rsync           # Material wegkopieren
    exfatprogs      # exFAT-Sticks formatieren und pruefen
)

MODE="install"
case "${1:-}" in
    --check) MODE="check" ;;
    "")      MODE="install" ;;
    *) echo "Unbekannte Option: $1"; sed -n '3,7p' "$0"; exit 1 ;;
esac

is_installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "ok installed"
}

# ---------------------------------------------------------------- prüfen
echo "==> Pakete"
missing=()
for p in "${PACKAGES[@]}"; do
    if is_installed "$p"; then
        printf '  \033[32m✓\033[0m %s\n' "$p"
    else
        printf '  \033[33m·\033[0m %s  (fehlt)\n' "$p"
        missing+=("$p")
    fi
done
echo

# ------------------------------------------------------------ installieren
if [[ "$MODE" == "install" ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "Bitte mit sudo starten:  sudo $0" >&2
        exit 1
    fi
    if [[ ${#missing[@]} -eq 0 ]]; then
        echo "Nichts zu tun – alle Pakete sind bereits installiert."
    else
        echo "==> Paketlisten aktualisieren"
        apt-get update
        echo "==> Installiere: ${missing[*]}"
        apt-get install -y --no-install-recommends "${missing[@]}"
        echo
    fi
elif [[ ${#missing[@]} -gt 0 ]]; then
    echo "Es fehlen ${#missing[@]}: ${missing[*]}"
    echo "Installation mit:  sudo $0"
    echo
fi

# ------------------------------------------------------------- Funktionstest
echo "==> Funktionsprüfung"

fail=0
check_bin() {
    if command -v "$1" >/dev/null 2>&1; then
        printf '  \033[32m✓\033[0m %-10s %s\n' "$1" "$("$1" --version 2>&1 | head -1 | cut -c1-52)"
    else
        printf '  \033[31m✗\033[0m %-10s nicht gefunden\n' "$1"
        fail=1
    fi
}

check_bin arecord
check_bin ffmpeg
check_bin tmux

if python3 -c "import tkinter" 2>/dev/null; then
    printf '  \033[32m✓\033[0m %-10s Tkinter importierbar\n' "python3"
else
    printf '  \033[31m✗\033[0m %-10s Tkinter fehlt – Statusanzeige startet nicht\n' "python3"
    fail=1
fi

echo
if [[ $fail -eq 0 ]]; then
    [[ "$MODE" == "check" ]] && echo "Alles bereit." \
                             || echo "Alles bereit. Weiter mit:  sudo ./install.sh"
else
    echo "Mindestens ein Werkzeug fehlt – siehe oben." >&2
    exit 1
fi
