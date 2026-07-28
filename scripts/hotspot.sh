#!/usr/bin/env bash
# Jetson tutor hotspot — toggle between access point and station.
#
#   scripts/hotspot.sh up [ssid] [password]   serve students (NO internet)
#   scripts/hotspot.sh down                   rejoin the last WiFi network
#   scripts/hotspot.sh status                 what mode are we in, and the URL
#
# WHY A TOGGLE AND NOT A PERMANENT CHANGE
# The Jetson's Realtek RTL8822CE runs NVIDIA's out-of-tree rtl8822ce driver,
# which reports `interface combinations are not supported` (verified 2026-07-27).
# AP mode and station mode each work; both at once do not. So serving students
# and having internet are mutually exclusive, and moving between them should be
# one command rather than a reconfiguration.
#
# WHAT YOU LOSE WHILE THE AP IS UP
#   - internet: no git push, no apt, no ollama pull
#   - any cloud LLM call. The tutor itself is unaffected (local Ollama), and
#     offline tutoring was verified end-to-end on 2026-07-27: MCQ grading,
#     free-text grading and step advancement all work with WiFi off.
set -euo pipefail

AP_CON="tutor-hotspot"
IFACE="${HOTSPOT_IFACE:-wlP1p1s0}"
DEFAULT_SSID="AITutor"
PORT="${TUTOR_PORT:-8000}"
# NetworkManager's shared mode always puts the AP on 10.42.0.1 and runs DHCP +
# DNS for clients. Fixed, so the student-facing URL never changes.
AP_IP="10.42.0.1"

die() { echo "error: $*" >&2; exit 1; }

# The station connection to come back to. Recorded when going up so `down`
# restores what you were actually on, not a guess.
STATE_FILE="${XDG_RUNTIME_DIR:-/tmp}/tutor-hotspot-prev"

cmd_up() {
    local ssid="${1:-$DEFAULT_SSID}" pass="${2:-${HOTSPOT_PASSWORD:-}}"
    [ -n "$pass" ] || die "a WiFi password is required (8+ chars).
  usage: $0 up '$ssid' '<password>'   or set HOTSPOT_PASSWORD"
    [ "${#pass}" -ge 8 ] || die "WPA2 requires at least 8 characters (got ${#pass})."

    local prev
    prev="$(nmcli -t -f NAME,DEVICE con show --active | awk -F: -v i="$IFACE" '$2==i{print $1; exit}')" || true
    [ -n "${prev:-}" ] && [ "$prev" != "$AP_CON" ] && printf '%s\n' "$prev" > "$STATE_FILE"

    echo "==> bringing up AP '$ssid' on $IFACE (this drops internet)"
    nmcli con delete "$AP_CON" >/dev/null 2>&1 || true
    # band bg = 2.4 GHz. Deliberate: the regulatory domain is unset (country 00),
    # which permits 2.4 GHz channels 1-11 but blocks most of 5 GHz, and 2.4 GHz
    # has better range and universal phone support. For 5 GHz, set a country
    # first (`iw reg set SC`) and change band to 'a'.
    nmcli con add type wifi ifname "$IFACE" con-name "$AP_CON" autoconnect no \
        ssid "$ssid" >/dev/null
    nmcli con modify "$AP_CON" \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        802-11-wireless.powersave 2 \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.proto rsn \
        wifi-sec.pairwise ccmp \
        wifi-sec.group ccmp \
        wifi-sec.psk "$pass" \
        ipv4.method shared \
        ipv6.method ignore >/dev/null
    nmcli con up "$AP_CON" >/dev/null
    sleep 3
    cmd_status
}

cmd_down() {
    echo "==> tearing down the AP"
    nmcli con down "$AP_CON" >/dev/null 2>&1 || true
    local prev=""
    [ -f "$STATE_FILE" ] && prev="$(cat "$STATE_FILE")"
    if [ -n "$prev" ]; then
        echo "==> rejoining '$prev'"
        nmcli con up "$prev" >/dev/null 2>&1 || nmcli device connect "$IFACE" >/dev/null 2>&1 || true
    else
        nmcli device connect "$IFACE" >/dev/null 2>&1 || true
    fi
    sleep 5
    cmd_status
}

# The port students actually use. The kiosk service (infra/systemd/ai-tutor.service)
# serves on 80 so there is no port number to type; a hand-started dev server uses
# 8000. Printing the wrong one sends people to a dead URL on a board that is
# working fine — which is exactly what this script used to do, because it
# predates the kiosk and assumed 8000 unconditionally.
serving_port() {
    if systemctl is-active --quiet ai-tutor.service 2>/dev/null; then
        echo 80
    else
        echo "$PORT"
    fi
}

cmd_status() {
    local active port
    port="$(serving_port)"
    active="$(nmcli -t -f NAME,DEVICE con show --active | awk -F: -v i="$IFACE" '$2==i{print $1; exit}')" || true
    if [ "${active:-}" = "$AP_CON" ]; then
        local ssid
        ssid="$(nmcli -t -f 802-11-wireless.ssid con show "$AP_CON" | cut -d: -f2)"
        echo "mode:     ACCESS POINT (no internet)"
        echo "ssid:     $ssid"
        echo "students: http://$AP_IP$( [ "$port" = 80 ] || printf ':%s' "$port" )/student/login/"
        if systemctl is-active --quiet ai-tutor.service 2>/dev/null; then
            echo "server:   ai-tutor.service is running on port 80 — nothing to start"
        else
            echo
            echo "Serve the app so clients can reach it (0.0.0.0, not localhost):"
            echo "  ./serve.py                      # or, for the kiosk:"
            echo "  sudo scripts/tutor_kiosk.sh enable"
        fi
    elif [ -n "${active:-}" ]; then
        local ip
        ip="$(ip -4 -br addr show "$IFACE" | awk '{print $3}' | cut -d/ -f1)"
        echo "mode:     station — joined '$active'"
        echo "address:  ${ip:-none}"
        echo "students: http://${ip%%/*}$( [ "$port" = 80 ] || printf ':%s' "$port" )/student/login/  (same network only)"
    else
        echo "mode:     $IFACE is not connected"
    fi
}

case "${1:-status}" in
    up)     shift; cmd_up "$@" ;;
    down)   cmd_down ;;
    status) cmd_status ;;
    *)      die "usage: $0 {up [ssid] [password]|down|status}" ;;
esac
