#!/usr/bin/env bash
#
# split-tracks.sh — Aufnahme zusammenbinden und in benannte Einzelspuren zerlegen
#
# Gedacht für den Mac. Nimmt einen Aufnahmeordner (die r_*.wav-Stücke einer
# Sitzung), hängt sie in der richtigen Reihenfolge aneinander und schreibt
# je Kanal eine durchgehende Mono-Datei mit sprechendem Namen.
#
#   ./split-tracks.sh <Aufnahmeordner> [Zielordner]
#       zerlegen (nutzt spuren.txt, falls vorhanden)
#
#   ./split-tracks.sh --namen <Aufnahmeordner>
#       nur eine Vorlage spuren.txt anlegen, nichts zerlegen
#
#   ./split-tracks.sh --umbenennen <Spurenordner> <spuren.txt>
#       fertige Spuren nachträglich umbenennen (dauert Sekunden)
#
#   ./split-tracks.sh --s24 <Aufnahmeordner> [Zielordner]
#       32-Bit-Material als 24 Bit ablegen. Die Pulte wandeln mit 24 Bit;
#       die vierte Byte-Ebene ist Füllung. Spart ein Viertel Platz und
#       hält lange Spuren unter der 4-GiB-Grenze von WAV.
#
# Voraussetzung:  brew install ffmpeg
#
# Gelesen wird nur einmal: ffmpeg hängt die Stücke im selben Durchlauf
# zusammen, in dem es die Kanäle trennt. Kein Zwischenspeicher nötig.
#
# Bewusst ohne bash-4-Eigenheiten geschrieben — macOS liefert bash 3.2 aus.
#
set -eu

# ffprobe liefert Zahlen mit Punkt ("600.000000"). In einer deutschen
# Umgebung liest awk/printf sie sonst nur bis zum Punkt – die Laengen- und
# Groessenangaben waeren falsch. LC_ALL muss weg, sonst uebersteuert es
# LC_NUMERIC; LC_CTYPE bleibt, damit Umlaute in Spurnamen stimmen.
unset LC_ALL
export LC_NUMERIC=C

# --------------------------------------------------------------- Hilfsmittel

fehler() { echo "$*" >&2; exit 1; }
hilfe()  { sed -n '3,22p' "$0"; exit "${1:-1}"; }

pruefe_werkzeuge() {
    command -v ffmpeg  >/dev/null 2>&1 || fehler "ffmpeg fehlt.  Installieren:  brew install ffmpeg"
    command -v ffprobe >/dev/null 2>&1 || fehler "ffprobe fehlt.  Installieren:  brew install ffmpeg"
}

# Gerätename aus dem Ordnernamen: gig_PULT1_2026-08-07_180012 -> PULT1
geraet_aus_ordner() {
    basename "$1" | sed -E 's/^[^_]+_//; s/_[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]+$//' \
                  | sed -E 's/[^A-Za-z0-9-]+/_/g'
}

# Namen aus spuren.txt lesen -> globale Variable NAMEN (eine Zeile je Kanal)
NAMEN=""
lies_namen() {
    NAMEN=""
    [ -f "$1" ] || return 0
    while IFS= read -r zeile || [ -n "$zeile" ]; do
        case "$zeile" in \#*) continue ;; esac
        sauber=$(printf '%s' "$zeile" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//; s#[/:]#-#g')
        NAMEN="$NAMEN
$sauber"
    done < "$1"
    return 0
}

# n-ten Namen holen (1-basiert), leer wenn keiner da
name_nr() {
    printf '%s' "$NAMEN" | sed -n "$(($1 + 1))p"
}

# Dateiname für Kanal n:  PULT1_07_Gitarre.wav  bzw.  PULT1_07.wav
spurname() {
    _nr=$(printf '%02d' "$1")
    _txt=$(name_nr "$1")
    if [ -n "$_txt" ]; then
        printf '%s_%s_%s.wav' "$PRAEFIX" "$_nr" "$_txt"
    else
        printf '%s_%s.wav' "$PRAEFIX" "$_nr"
    fi
}

vorlage_schreiben() {
    _ziel="$1"; _kanaele="$2"
    if [ -f "$_ziel" ]; then echo "Es gibt schon: $_ziel"; return 0; fi
    {
        echo "# Spurnamen für $PRAEFIX — eine Zeile je Kanal, in der Reihenfolge"
        echo "# der Aufnahme. Leere Zeile = Kanal behält nur seine Nummer."
        echo "# Zeilen mit # werden übersprungen."
        echo "#"
        echo "# Aus Kanal 7 mit dem Eintrag 'Gitarre li' wird:"
        echo "#     ${PRAEFIX}_07_Gitarre li.wav"
        echo "#"
        _i=1
        while [ "$_i" -le "$_kanaele" ]; do
            printf '\n'
            _i=$((_i + 1))
        done
    } > "$_ziel"
    echo "Vorlage angelegt: $_ziel   ($_kanaele Kanäle)"
    echo "Ausfüllen, dann das Skript ohne --namen erneut starten."
}

# ------------------------------------------------------------ Betriebsarten

# --s24 ist ein Schalter, keine Betriebsart: er wird vorweg abgeraeumt,
# danach geht es normal weiter.
NACH_S24=0
if [ "${1:-}" = "--s24" ]; then
    NACH_S24=1
    shift
fi

case "${1:-}" in
    ""|-h|--help) hilfe 0 ;;

    --namen)
        QUELLE="${2:-}"
        [ -d "$QUELLE" ] || fehler "Ordner nicht gefunden: ${QUELLE:-<fehlt>}"
        pruefe_werkzeuge
        ERSTE=""
        for f in "$QUELLE"/*.wav; do
            if [ -f "$f" ]; then ERSTE="$f"; break; fi
        done
        [ -n "$ERSTE" ] || fehler "Keine WAV-Dateien in $QUELLE"
        KANAELE=$(ffprobe -v error -select_streams a:0 \
                          -show_entries stream=channels -of csv=p=0 "$ERSTE")
        PRAEFIX=$(geraet_aus_ordner "$QUELLE")
        vorlage_schreiben "$QUELLE/spuren.txt" "$KANAELE"
        exit 0 ;;

    --umbenennen)
        ORDNER="${2:-}"; DATEI="${3:-}"
        [ -d "$ORDNER" ] || fehler "Ordner nicht gefunden: ${ORDNER:-<fehlt>}"
        [ -f "$DATEI" ]  || fehler "Namensdatei nicht gefunden: ${DATEI:-<fehlt>}"
        lies_namen "$DATEI"
        n=0
        for f in "$ORDNER"/*.wav; do
            [ -f "$f" ] || continue
            n=$((n + 1))
            PRAEFIX=$(basename "$f" | sed -E 's/_[0-9]{2}(_.*)?\.wav$//')
            neu="$ORDNER/$(spurname "$n")"
            [ "$f" = "$neu" ] && continue
            mv -n "$f" "$neu" && echo "  $(basename "$f")  ->  $(basename "$neu")"
        done
        echo "$n Dateien geprüft."
        exit 0 ;;

    -*) fehler "Unbekannte Option: $1   (--help für die Hilfe)" ;;
esac

# --------------------------------------------------------------- Zerlegen

QUELLE="$1"
[ -d "$QUELLE" ] || fehler "Ordner nicht gefunden: $QUELLE"
ZIEL="${2:-$QUELLE/spuren}"
pruefe_werkzeuge

# Stücke einsammeln. Der Glob sortiert alphabetisch, und weil im Namen
# JJMMTT_HHMMSS steht, ist das zugleich chronologisch — auch über
# Mitternacht hinweg.
LISTE=$(mktemp /tmp/audiorec-liste.XXXXXX)
trap 'rm -f "$LISTE"' EXIT
ANZAHL=0; ERSTE=""; LETZTE=""
for f in "$QUELLE"/*.wav; do
    [ -f "$f" ] || continue
    voll=$(cd "$(dirname "$f")" && pwd)/$(basename "$f")
    printf "file '%s'\n" "$voll" >> "$LISTE"
    ANZAHL=$((ANZAHL + 1))
    [ -z "$ERSTE" ] && ERSTE="$f"
    LETZTE="$f"
done
[ "$ANZAHL" -gt 0 ] || fehler "Keine WAV-Dateien in $QUELLE"

lies() { ffprobe -v error -select_streams a:0 -show_entries "stream=$1" -of csv=p=0 "$2"; }
KANAELE=$(lies channels    "$ERSTE")
RATE=$(lies    sample_rate "$ERSTE")
# Nicht sample_fmt auswerten! ffprobe meldet fuer 24-Bit-Material
# sample_fmt=s32 (ffmpeg rechnet intern in 32 Bit). Wer danach geht,
# blaest die Spuren von 3 auf 4 Byte auf – ein Viertel mehr Daten ohne
# einen Deut mehr Klang. Der Codecname sagt die Wahrheit.
CODEC=$(lies codec_name "$ERSTE")
case "$CODEC" in
    pcm_s16le) BREITE=2 ;;
    pcm_s24le) BREITE=3 ;;
    pcm_s32le|pcm_f32le) BREITE=4 ;;
    *) echo "Unbekannter Codec: $CODEC – nehme pcm_s24le" >&2
       CODEC=pcm_s24le; BREITE=3 ;;
esac
QUELLCODEC="$CODEC"

# --s24: 32-Bit-Ganzzahlmaterial als 24 Bit ablegen. Die Pulte wandeln mit
# 24 Bit, ALSA legt sie in 4 Byte ab und fuellt das unterste Byte mit
# Nullen — das faellt hier wieder weg. Bei Gleitkomma (f32le) wird nicht
# angefasst, da waere es ein echter Eingriff.
if [ "$NACH_S24" -eq 1 ]; then
    if [ "$CODEC" = "pcm_s32le" ]; then
        CODEC=pcm_s24le; BREITE=3
    else
        echo "--s24 wird übergangen: Quelle ist $CODEC, nicht pcm_s32le" >&2
    fi
fi
FMT="$CODEC"

SEK=0
for f in "$QUELLE"/*.wav; do
    [ -f "$f" ] || continue
    d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null || echo 0)
    SEK=$(awk -v a="$SEK" -v b="${d:-0}" 'BEGIN{printf "%.0f", a+b}')
done
PRO_SPUR=$(awk -v s="$SEK" -v r="$RATE" -v b="$BREITE" 'BEGIN{printf "%.2f", s*r*b/1073741824}')
GESAMT=$(awk -v g="$PRO_SPUR" -v c="$KANAELE" 'BEGIN{printf "%.1f", g*c}')

PRAEFIX=$(geraet_aus_ordner "$QUELLE")
lies_namen "$QUELLE/spuren.txt"

echo "Quelle    : $QUELLE"
echo "Stücke    : $ANZAHL   ($(basename "$ERSTE") … $(basename "$LETZTE"))"
if [ "$CODEC" = "$QUELLCODEC" ]; then
    echo "Format    : $KANAELE Kanäle · $RATE Hz · $CODEC"
else
    echo "Format    : $KANAELE Kanäle · $RATE Hz · $QUELLCODEC → $CODEC (--s24)"
fi
printf 'Länge     : %d:%02d:%02d\n' $((SEK/3600)) $(((SEK%3600)/60)) $((SEK%60))
echo "Je Spur   : ca. ${PRO_SPUR} GiB   ·   zusammen ca. ${GESAMT} GiB"
echo "Präfix    : $PRAEFIX"
if [ -n "$NAMEN" ]; then
    echo "Namen     : aus $QUELLE/spuren.txt"
else
    echo "Namen     : keine spuren.txt — Spuren heißen ${PRAEFIX}_01.wav …"
fi
echo "Ziel      : $ZIEL"

# WAV kann höchstens 4 GiB. Darüber schreibt ffmpeg RF64 — Logic liest das,
# aber es ist besser, vorher davon zu wissen.
if awk -v g="$PRO_SPUR" 'BEGIN{exit !(g>3.9)}'; then
    echo
    echo "!! Jede Spur wird ${PRO_SPUR} GiB groß und damit als RF64 geschrieben,"
    echo "!! nicht als klassische WAV. Logic liest das; ältere Programme nicht"
    echo "!! unbedingt."
    if [ "$CODEC" = "pcm_s32le" ]; then
        S24=$(awk -v g="$PRO_SPUR" 'BEGIN{printf "%.2f", g*3/4}')
        echo "!!"
        echo "!! Das Material ist 32-Bit-verpacktes 24-Bit-Audio (die Pulte"
        echo "!! wandeln mit 24 Bit). Als pcm_s24le bleibt jede Spur bei"
        echo "!! ${S24} GiB und damit unter der 4-GiB-Grenze — gewöhnliche WAV,"
        echo "!! ohne hörbaren Unterschied:"
        echo "!!     $0 --s24 \"$QUELLE\""
    fi
fi
echo

mkdir -p "$ZIEL"
FILTER=""
c=0
set --
while [ "$c" -lt "$KANAELE" ]; do
    FILTER="${FILTER}[0:a]pan=mono|c0=c${c}[o${c}];"
    # -rf64 gehört VOR JEDE Ausgabedatei.
    #
    # Es ist eine Einstellung des WAV-Schreibers, keine allgemeine Option:
    # einmal vor dem ersten Dateinamen genannt, gilt es nur für die erste
    # Spur. Spur 2 bis n würden über 4 GiB stillschweigend als gewöhnliche
    # WAV geschrieben und wären ab dort unbrauchbar — bei 7 Stunden in
    # S32_LE ist eine Monospur 4,5 GiB gross, das trifft also jede.
    set -- "$@" -map "[o${c}]" -c:a "$CODEC" -rf64 auto \
                "$ZIEL/$(spurname $((c + 1)))"
    c=$((c + 1))
done
FILTER=$(printf '%s' "$FILTER" | sed 's/;$//')

echo "Zerlege … ein Durchlauf über ${GESAMT} GiB, das dauert."
ffmpeg -hide_banner -loglevel warning -stats \
       -f concat -safe 0 -i "$LISTE" \
       -filter_complex "$FILTER" \
       -y "$@"

echo
echo "Fertig — $KANAELE Dateien in $ZIEL"
ls "$ZIEL" | head -4
[ "$KANAELE" -gt 4 ] && echo "  …"
echo
echo "In Logic: alle Dateien zusammen markieren und ins Arrangement ziehen."
echo "Logic legt je Datei eine Spur an und übernimmt die Dateinamen."
if [ -z "$NAMEN" ]; then
    echo
    echo "Namen nachtragen geht in Sekunden — neu zerlegen ist nicht nötig:"
    echo "  $0 --namen \"$QUELLE\"        # Vorlage anlegen, ausfüllen"
    echo "  $0 --umbenennen \"$ZIEL\" \"$QUELLE/spuren.txt\""
fi
