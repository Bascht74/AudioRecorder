#!/usr/bin/env bash
#
# check-recording.sh — Aufnahmeordner prüfen: Dateien gültig, Reihenfolge
# lückenlos, Kanalzahl und Format überall gleich.
#
#   ./check-recording.sh <Ordner>          einen Ordner prüfen
#   ./check-recording.sh <Elternordner>    alle gig_* darin prüfen
#   ./check-recording.sh <Elternordner> -a  auch die Reste ausführlich
#
# Läuft auf dem Pi und auf dem Mac (dort: brew install ffmpeg).
#
set -eu

# Zahlen mit Punkt als Dezimaltrennzeichen — ffprobe liefert "600.000000",
# und printf/awk wuerden das in einer deutschen Umgebung (LC_NUMERIC=de_DE)
# nicht mehr einlesen: die Zahl bricht am Punkt ab, printf meldet einen
# Fehler, und mit "set -e" endet das Skript mittendrin.
# LC_ALL muss weg, sonst uebersteuert es LC_NUMERIC. LC_CTYPE bleibt, damit
# Umlaute in der Ausgabe stimmen.
unset LC_ALL
export LC_NUMERIC=C

command -v ffprobe >/dev/null 2>&1 || { echo "ffprobe fehlt." >&2; exit 1; }

# Ordner unter dieser Laenge gelten als Rest (Dienstneustart, Wackler)
REST_S=60

# Bytes je Abtastwert aus dem Codecnamen
breite_von() {
    case "$1" in
        pcm_s16le) echo 2 ;;
        pcm_s24le) echo 3 ;;
        pcm_s32le|pcm_f32le) echo 4 ;;
        *) echo 0 ;;
    esac
}

G_ORDNER=0; G_REST=0; G_BYTES=0; G_DATEIEN=0; G_FEHLER=0; G_LISTE=""
# Je Ordner eine Zeile "Name ppm Genauigkeit" fuer den Vergleich am Ende
G_TAKT=""

# Ab dieser Aufnahmedauer ist die Taktmessung aussagekraeftig. Die
# Dateinamen haben Sekundenaufloesung; auf 30 min sind das +-560 ppm, auf
# 7 h noch +-40 ppm.
TAKT_MIN_S=1800

pruefe_ordner() {
    ORDNER="$1"
    G_ORDNER=$((G_ORDNER + 1))

    # Erst still durchrechnen, um Reste einzeilig abhandeln zu koennen
    LAENGE_ROH=0
    for f in "$ORDNER"/*.wav; do
        [ -f "$f" ] || continue
        d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null || echo 0)
        LAENGE_ROH=$(awk -v a="$LAENGE_ROH" -v b="${d:-0}" 'BEGIN{printf "%.0f", a+b}')
    done
    if [ "$LAENGE_ROH" -lt "$REST_S" ] && [ "${AUSFUEHRLICH:-0}" -eq 0 ]; then
        gr=$(du -sh "$ORDNER" 2>/dev/null | cut -f1)
        printf '  \033[90m%-46s %4s s  %6s  Rest\033[0m\n' \
            "$(basename "$ORDNER")" "$LAENGE_ROH" "$gr"
        G_REST=$((G_REST + 1))
        return
    fi

    echo "=============================================================="
    echo "$ORDNER"

    ANZ=0; SUMME=0; FEHLER=0; KAN=""; FMT=""; RATE=""; SUMME_S=0
    VOR_ENDE=""      # errechnetes Ende der vorigen Datei, als Sekunde des Tages
    ERSTE_START=""
    LETZTER_START="" # Anfang der LETZTEN Datei – Basis der Taktmessung
    LETZTE_DAUER=0
    DAUERN=""        # "Name Dauer" je Zeile, fuer die Kurzdatei-Pruefung

    for f in "$ORDNER"/*.wav; do
        [ -f "$f" ] || continue
        ANZ=$((ANZ + 1))
        name="$(basename "$f")"

        # Mit Schluesselnamen abfragen: ffprobe gibt die Felder in seiner
        # eigenen Reihenfolge aus, nicht in der angefragten.
        daten=$(ffprobe -v error -select_streams a:0 \
                -show_entries stream=channels,sample_rate,codec_name \
                -show_entries format=duration \
                -of default=noprint_wrappers=1 "$f" 2>/dev/null || true)
        hole() { echo "$daten" | sed -nE "s/^$1=(.*)$/\\1/p" | head -1; }
        ch=$(hole channels)
        rate=$(hole sample_rate)
        fmt=$(hole codec_name)
        dauer=$(hole duration)

        if [ -z "${dauer:-}" ] || [ "$dauer" = "N/A" ]; then
            printf '  %-26s \033[31mUNLESBAR\033[0m\n' "$name"
            FEHLER=$((FEHLER + 1))
            continue
        fi

        [ -z "$KAN" ]  && KAN="$ch"
        [ -z "$FMT" ]  && FMT="$fmt"
        [ -z "$RATE" ] && RATE="$rate"

        hinweis=""
        [ "$ch"   != "$KAN" ]  && { hinweis="$hinweis  \033[31mKANALZAHL $ch statt $KAN\033[0m"; FEHLER=$((FEHLER+1)); }
        [ "$fmt"  != "$FMT" ]  && { hinweis="$hinweis  \033[31mFORMAT $fmt statt $FMT\033[0m";     FEHLER=$((FEHLER+1)); }
        [ "$rate" != "$RATE" ] && { hinweis="$hinweis  \033[31mRATE $rate statt $RATE\033[0m";     FEHLER=$((FEHLER+1)); }

        # Startzeit aus dem Dateinamen: r_JJMMTT_HHMMSS.wav
        zeit=$(echo "$name" | sed -nE 's/^r_[0-9]{6}_([0-9]{2})([0-9]{2})([0-9]{2})\.wav$/\1 \2 \3/p')
        if [ -n "$zeit" ]; then
            set -- $zeit
            start=$(( 10#$1 * 3600 + 10#$2 * 60 + 10#$3 ))
            if [ -n "$VOR_ENDE" ]; then
                luecke=$(awk -v a="$start" -v b="$VOR_ENDE" 'BEGIN{d=a-b; if(d<-43200) d+=86400; printf "%.0f", d}')
                # 0 bis 2 Sekunden sind normal (Datei schliessen und neu oeffnen)
                if [ "$luecke" -gt 2 ] || [ "$luecke" -lt -2 ]; then
                    hinweis="$hinweis  \033[33mLUECKE ${luecke}s zur vorigen Datei\033[0m"
                fi
            fi
            [ -z "$ERSTE_START" ] && ERSTE_START="$start"
            LETZTER_START="$start"
            LETZTE_DAUER="$dauer"
            VOR_ENDE=$(awk -v s="$start" -v d="$dauer" 'BEGIN{printf "%.0f", s+d}')
        fi

        groesse=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f")

        # Kopf gegen Dateigroesse gegenrechnen.
        #
        # arecord traegt beim Oeffnen die ERWARTETE Laenge in den WAV-Kopf
        # ein und berichtigt sie beim Schliessen. Wird der Prozess hart
        # abgeschossen (SIGKILL, Stromausfall), bleibt die falsche Angabe
        # stehen: ffprobe meldet dann eine Dauer, die es gar nicht gibt,
        # und beim Zusammenbinden entstehen Luecken oder Rauschen.
        # Das faellt sonst erst am Mac auf – hier faellt es sofort auf.
        breite=$(breite_von "$fmt")
        if [ "$breite" -gt 0 ] && [ -n "${ch:-}" ] && [ -n "${rate:-}" ]; then
            abw=$(awk -v g="$groesse" -v c="$ch" -v r="$rate" -v b="$breite" \
                      -v d="$dauer" 'BEGIN{
                        bps = c*r*b; if (bps<=0) { print "x"; exit }
                        echt = (g-44)/bps;
                        diff = d - echt; if (diff<0) diff = -diff;
                        grenze = echt*0.02; if (grenze<1.0) grenze = 1.0;
                        if (diff > grenze) printf "%.1f", echt; else print ""
                      }')
            if [ -n "$abw" ] && [ "$abw" != "x" ]; then
                hinweis="$hinweis  \033[31mKOPF FALSCH: enthält ${abw}s\033[0m"
                FEHLER=$((FEHLER + 1))
            fi
        fi

        SUMME=$(awk -v a="$SUMME" -v b="$groesse" 'BEGIN{printf "%.0f", a+b}')
        DAUERN="$DAUERN
$name $dauer"
        SUMME_S=$(awk -v a="$SUMME_S" -v b="$dauer" 'BEGIN{printf "%.0f", a+b}')
        printf '  %-26s %5.1f s  %2s ch  %-10s %6.2f GB%b\n' \
            "$name" "$dauer" "$ch" "$fmt" \
            "$(awk -v b="$groesse" 'BEGIN{printf "%.2f", b/1073741824}')" "$hinweis"
    done

    if [ "$ANZ" -eq 0 ]; then
        echo "  keine WAV-Dateien"
        return
    fi

    # Kurze Stuecke aufspueren.
    #
    # arecord schneidet nach max_file_time_s eine neue Datei an — alle
    # Stuecke einer Sitzung sind deshalb gleich lang, nur das letzte ist
    # kuerzer. Weicht ein Stueck MITTENDRIN ab, hat der Prozess dort
    # ausgesetzt: entweder ist Material verloren, oder die Datei ist
    # unvollstaendig geschlossen. Bei 80 Zeilen faellt das von Hand
    # niemandem auf.
    KURZ=$(printf '%s' "$DAUERN" | awk 'NF==2 {n++; na[n]=$1; d[n]=$2;
             if ($2>max) max=$2}
        END{ if (n<3 || max<=0) exit
             for (i=1; i<n; i++)
                 if (max - d[i] > 2.0)
                     printf "%s (%.1f statt %.1f s)\n", na[i], d[i], max }')
    if [ -n "$KURZ" ]; then
        anz=$(printf '%s\n' "$KURZ" | wc -l | tr -d ' ')
        printf '  \033[31m%s Stück(e) kürzer als die übrigen – dort hat die Aufnahme ausgesetzt:\033[0m\n' "$anz"
        printf '%s\n' "$KURZ" | head -5 | sed 's/^/      /'
        [ "$anz" -gt 5 ] && echo "      … und $((anz - 5)) weitere"
        FEHLER=$((FEHLER + anz))
    fi

    GESAMT=$(awk -v b="$SUMME" 'BEGIN{printf "%.2f", b/1073741824}')
    echo "  --------------------------------------------------------"
    printf '  %d Dateien · %s GB · %s Kanäle · %s · %s Hz\n' \
        "$ANZ" "$GESAMT" "$KAN" "$FMT" "$RATE"
    # Zwei Rechenwege: aus den Zeitstempeln der Dateinamen (dann sind
    # Luecken mit drin) oder als Summe der Stuecke. Der erste geht nur bei
    # Dateien im Muster r_JJMMTT_HHMMSS.wav – bei von Hand benannten
    # Dateien stuende sonst "0:00:00" da, was wie ein Fehler aussieht.
    if [ -n "$ERSTE_START" ]; then
        LAENGE=$(awk -v s="$VOR_ENDE" -v a="$ERSTE_START" 'BEGIN{d=s-a; if(d<0) d+=86400;
                 printf "%d:%02d:%02d", d/3600, (d%3600)/60, d%60}')
        echo "  Gesamtlänge inklusive Lücken: $LAENGE"
    else
        LAENGE=$(awk -v d="$SUMME_S" 'BEGIN{printf "%d:%02d:%02d", d/3600, (d%3600)/60, d%60}')
        echo "  Gesamtlänge (Summe der Stücke): $LAENGE"
    fi
    # ----------------------------------------------------------------
    # Abtasttakt gegen die Systemuhr
    #
    # Jedes Interface hat einen eigenen Quarz. Zwei Pulte, die nicht
    # gemeinsam getaktet sind, laufen deshalb ueber einen langen Abend
    # auseinander – bei 250 ppm sind das 6 s auf 7 Stunden. Wer beide
    # Aufnahmen auf EINE Zeitachse legt, muss das wissen.
    #
    # Gemessen wird so: zwischen dem Anfang der ersten und dem Anfang der
    # letzten Datei liegen (ANZ-1) Stuecke. Deren Gesamtdauer ist die
    # Zeit, die das GERAET gezaehlt hat; die Differenz der Dateinamen ist
    # die Zeit, die die SYSTEMUHR gezaehlt hat.
    # ----------------------------------------------------------------
    if [ -n "$ERSTE_START" ] && [ -n "$LETZTER_START" ] && [ "$ANZ" -ge 3 ]; then
        WALL=$(awk -v a="$ERSTE_START" -v b="$LETZTER_START" \
               'BEGIN{d=b-a; if(d<0) d+=86400; printf "%.0f", d}')
        AUDIO=$(awk -v s="$SUMME_S" -v l="$LETZTE_DAUER" 'BEGIN{printf "%.1f", s-l}')
        if [ "$WALL" -ge "$TAKT_MIN_S" ]; then
            set -- $(awk -v w="$WALL" -v a="$AUDIO" -v r="${RATE:-48000}" \
                     'BEGIN{ printf "%.0f %.1f %.0f", (a/w-1)*1e6, r*a/w, 1e6/w }')
            PPM="$1"; IST="$2"; GENAU="$3"
            printf '  Abtasttakt: %s Hz gemessen · %+d ppm zur Systemuhr (±%d ppm)\n' \
                "$IST" "$PPM" "$GENAU"
            G_TAKT="$G_TAKT
$(basename "$ORDNER") $PPM $GENAU"
        else
            printf '  Abtasttakt: nicht gemessen – dafür müsste die Aufnahme über %d min laufen\n' \
                "$((TAKT_MIN_S / 60))"
        fi
    fi

    for extra in aufnahme.txt spuren.txt; do
        [ -f "$ORDNER/$extra" ] && echo "  $extra vorhanden"
    done
    G_DATEIEN=$((G_DATEIEN + ANZ))
    G_BYTES=$(awk -v a="$G_BYTES" -v b="$SUMME" 'BEGIN{printf "%.0f", a+b}')
    G_FEHLER=$((G_FEHLER + FEHLER))
    if [ "$FEHLER" -eq 0 ]; then
        printf '  \033[32mIn Ordnung.\033[0m\n'
    else
        printf '  \033[31m%d Auffälligkeiten – siehe oben.\033[0m\n' "$FEHLER"
    fi
}

bilanz() {
    echo
    echo "=============================================================="
    printf 'Gesamt: %d Ordner mit Material, %d Reste unter %d s\n' \
        "$((G_ORDNER - G_REST))" "$G_REST" "$REST_S"
    printf '        %d Dateien · %s GB\n' "$G_DATEIEN" \
        "$(awk -v b="$G_BYTES" 'BEGIN{printf "%.2f", b/1073741824}')"
    if [ "$G_FEHLER" -eq 0 ]; then
        printf '        \033[32mkeine Auffälligkeiten\033[0m\n'
    else
        printf '        \033[31m%d Auffälligkeiten\033[0m\n' "$G_FEHLER"
    fi
    # Auseinanderlaufen zweier Geraete – die Zahl fuer die Nachbearbeitung
    ANZ_TAKT=$(printf '%s' "$G_TAKT" | awk 'NF==3' | wc -l | tr -d ' ')
    if [ "${ANZ_TAKT:-0}" -ge 2 ]; then
        echo
        echo "Abtasttakt der Geräte im Vergleich"
        printf '%s\n' "$G_TAKT" | awk 'NF==3 {printf "  %-42s %+5d ppm  (±%d)\n", $1, $2, $3}'
        printf '%s\n' "$G_TAKT" | awk 'NF==3 {n++; na[n]=$1; p[n]=$2; g[n]=$3}
            END{ if (n<2) exit
                 print ""
                 for (i=1; i<n; i++) for (j=i+1; j<=n; j++) {
                     d = p[i]-p[j]; if (d<0) d=-d
                     u = g[i]+g[j]
                     printf "  %s  gegen  %s\n", na[i], na[j]
                     printf "    %d ppm auseinander (±%d)\n", d, u
                     printf "    Versatz nach 1 h: %.0f ms · nach 7 h: %.1f s\n",
                            d*3600/1000, d*25200/1e6
                 }
                 print ""
                 print "  Das sind die Quarze der Interfaces, keine Lücken in der Aufnahme."
                 print "  Wer beide Aufnahmen auf EINE Zeitachse legt, muss eine davon"
                 print "  entsprechend dehnen. Wer Stück für Stück arbeitet und jedes"
                 print "  Stück einzeln ausrichtet, kann die Zahl ignorieren."
               }'
    fi

    if [ "$G_REST" -gt 0 ]; then
        echo
        echo "Die Reste stammen von Dienstneustarts oder kurzen Abrissen."
        echo "Wegräumen (erst ansehen, dann löschen):"
        echo "  find <Ordner> -maxdepth 1 -name 'gig_*' -exec sh -c \\"
        echo "    'test \$(du -s \"\$1\" | cut -f1) -lt 40000 && echo \"\$1\"' _ {} \\;"
    fi
}

ZIEL="${1:-}"
[ -d "$ZIEL" ] || { sed -n '3,10p' "$0"; exit 1; }

case "${2:-}" in -a|--alle) AUSFUEHRLICH=1 ;; *) AUSFUEHRLICH=0 ;; esac

if ls "$ZIEL"/*.wav >/dev/null 2>&1; then
    AUSFUEHRLICH=1
    pruefe_ordner "$ZIEL"
    exit 0
else
    gefunden=0
    for d in "$ZIEL"/gig_*; do
        [ -d "$d" ] || continue
        gefunden=1
        pruefe_ordner "$d"
    done
    if [ "$gefunden" -eq 0 ]; then
        echo "Keine Aufnahmeordner in $ZIEL gefunden."
    else
        bilanz
    fi
fi
