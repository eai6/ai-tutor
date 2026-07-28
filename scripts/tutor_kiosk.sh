#!/usr/bin/env bash
# Kiosk mode: boot the Jetson headless, serve the tutor over its own WiFi.
#
#   sudo scripts/tutor_kiosk.sh install    copy units, pin the hotspot IP
#   sudo scripts/tutor_kiosk.sh enable     start now AND on every boot
#   sudo scripts/tutor_kiosk.sh disable    stop now and on boot; rejoin WiFi
#   sudo scripts/tutor_kiosk.sh model <spec>  swap the tutor model (testing)
#        scripts/tutor_kiosk.sh status     what is running, and the URL
#
# Enabled, powering on the board is enough: Ollama starts, the tutor starts on
# port 80, and the AP comes up on a fixed address. No monitor, no login.
#
# Disabled, nothing starts and the WiFi rejoins your normal network, so the box
# goes back to being a development machine with internet.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNITS=(ollama.service ai-tutor.service)
AP_CON="tutor-hotspot"
AP_IP="10.42.0.1"
IFACE="${HOTSPOT_IFACE:-wlP1p1s0}"

need_root() { [ "$(id -u)" -eq 0 ] || { echo "error: '$1' needs sudo" >&2; exit 1; }; }

cmd_install() {
    need_root install
    for u in "${UNITS[@]}"; do
        install -m 0644 "$ROOT/infra/systemd/$u" "/etc/systemd/system/$u"
        echo "  installed /etc/systemd/system/$u"
    done
    systemctl daemon-reload

    # Pin the AP address. NetworkManager's shared mode already defaults to
    # 10.42.0.1, but leaving it implicit means the student-facing URL depends on
    # a default that could change. Pinned, it can go on a poster.
    if nmcli -t -f NAME con show | grep -qx "$AP_CON"; then
        nmcli con modify "$AP_CON" ipv4.method shared ipv4.addresses "$AP_IP/24"
        echo "  pinned $AP_CON to $AP_IP/24"
    else
        echo "  WARNING: connection '$AP_CON' does not exist yet." >&2
        echo "           Create it first:  scripts/hotspot.sh up AITutor '<password>'" >&2
    fi
    echo "install done — 'sudo $0 enable' to start it and make it boot-persistent"
}

cmd_enable() {
    need_root enable
    for u in "${UNITS[@]}"; do
        [ -f "/etc/systemd/system/$u" ] || { echo "error: run '$0 install' first" >&2; exit 1; }
    done
    # autoconnect: this is what makes the AP come up at boot with no login.
    nmcli con modify "$AP_CON" connection.autoconnect yes connection.autoconnect-priority 100
    nmcli con up "$AP_CON" >/dev/null 2>&1 || echo "  (AP will come up at boot)"
    systemctl enable --now "${UNITS[@]}"
    sleep 3
    cmd_status
}

cmd_disable() {
    need_root disable
    systemctl disable --now "${UNITS[@]}" 2>/dev/null || true
    nmcli con modify "$AP_CON" connection.autoconnect no 2>/dev/null || true
    nmcli con down "$AP_CON" >/dev/null 2>&1 || true
    # Rejoin a normal network so the box is usable for development again.
    nmcli device connect "$IFACE" >/dev/null 2>&1 || true
    sleep 4
    echo "kiosk disabled — services stopped, AP off, WiFi rejoining"
    cmd_status
}

cmd_model() {
    need_root model
    local spec="${1:-}"
    [ -n "$spec" ] || { echo "usage: $0 model <provider/tag>   ('' or 'default' to reset)" >&2; exit 1; }
    if [ "$spec" = "default" ]; then
        rm -f /etc/default/ai-tutor
        echo "  reset to the unit default (qwen3-4b-jetson)"
    else
        # The tag must have an EXACT entry in apps/llm/model_profiles.py. Without
        # one it falls through to a cloud family profile sized at num_ctx=24192,
        # which does not fit this box.
        printf 'TUTOR_MODEL_OVERRIDE=%s\n' "$spec" > /etc/default/ai-tutor
        echo "  set $spec"
    fi
    systemctl restart ai-tutor.service 2>/dev/null || true
    sleep 8
    cmd_status
}

cmd_status() {
    printf '%-14s %s\n' "ollama:" "$(systemctl is-active ollama.service 2>/dev/null || echo unknown)/$(systemctl is-enabled ollama.service 2>/dev/null || echo -)"
    printf '%-14s %s\n' "ai-tutor:" "$(systemctl is-active ai-tutor.service 2>/dev/null || echo unknown)/$(systemctl is-enabled ai-tutor.service 2>/dev/null || echo -)"
    local auto
    auto="$(nmcli -t -f connection.autoconnect con show "$AP_CON" 2>/dev/null | cut -d: -f2 || echo '-')"
    local m; m="$(grep -hoE 'TUTOR_MODEL_OVERRIDE=.*' /etc/default/ai-tutor 2>/dev/null | cut -d= -f2)"
    printf '%-14s %s\n' "model:" "${m:-local_ollama/qwen3-4b-jetson (unit default)}"
    printf '%-14s %s\n' "hotspot boot:" "${auto:--}"
    local active
    active="$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | awk -F: -v i="$IFACE" '$2==i{print $1; exit}')" || true
    printf '%-14s %s\n' "wifi now:" "${active:-not connected}"
    if [ "${active:-}" = "$AP_CON" ]; then
        printf '%-14s %s\n' "students:" "http://$AP_IP/student/login/"
    else
        local ip; ip="$(ip -4 -br addr show "$IFACE" 2>/dev/null | awk '{print $3}' | cut -d/ -f1)"
        [ -n "${ip:-}" ] && printf '%-14s %s\n' "students:" "http://$ip/student/login/  (LAN)"
    fi
}

case "${1:-status}" in
    install) cmd_install ;;
    enable)  cmd_enable ;;
    disable) cmd_disable ;;
    model)   shift; cmd_model "${1:-}" ;;
    status)  cmd_status ;;
    *) echo "usage: $0 {install|enable|disable|model <spec>|status}" >&2; exit 1 ;;
esac
