#!/usr/bin/env bash
# freeprox — install / update / uninstall on a Proxmox VE node.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/sudoPierre/freeprox/main/install.sh)"
#
# Non-interactive options: --update, --uninstall [--purge]

set -Eeuo pipefail

REPO="sudoPierre/freeprox"
BRANCH="${FREEPROX_BRANCH:-main}"
APP="freeprox"
PREFIX="/opt/freeprox"
CONF_DIR="/etc/freeprox"
CONF="$CONF_DIR/freeprox.conf"
BACKTITLE="Proxmox VE Helper — freeprox"

# -- output (tteck style) ----------------------------------------------------

YW=$'\033[33m'; BL=$'\033[36m'; RD=$'\033[01;31m'; GN=$'\033[1;92m'; DGN=$'\033[32m'
CL=$'\033[m'; BFR=$'\r\033[K'; HOLD=" "; CM="${GN}✓${CL}"; CROSS="${RD}✗${CL}"
SPINNER_PID=""

header_info() {
  clear
  cat <<"EOF"
    ____
   / __/_______  ___  ____  _________  _  __
  / /_/ ___/ _ \/ _ \/ __ \/ ___/ __ \| |/_/
 / __/ /  /  __/  __/ /_/ / /  / /_/ />  <
/_/ /_/   \___/\___/ .___/_/   \____/_/|_|
                  /_/
EOF
  echo -e "${DGN}  Automatic Freebox IP reservation for Proxmox VMs/LXCs${CL}\n"
}

spinner() {
  local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏') i=0
  while true; do
    printf "\r %s" "${YW}${frames[i++ % ${#frames[@]}]}${CL}"
    sleep 0.1
  done
}

stop_spinner() {
  if [[ -n "$SPINNER_PID" ]]; then
    kill "$SPINNER_PID" 2>/dev/null || true
    wait "$SPINNER_PID" 2>/dev/null || true
    SPINNER_PID=""
  fi
  printf "\r"
}

msg_info() {
  stop_spinner
  echo -ne " ${HOLD} ${YW}$1...${CL}"
  spinner & SPINNER_PID=$!
}
msg_ok()    { stop_spinner; echo -e "${BFR} ${CM} ${GN}$1${CL}"; }
msg_error() { stop_spinner; echo -e "${BFR} ${CROSS} ${RD}$1${CL}"; }
msg_warn()  { stop_spinner; echo -e "${BFR} ${YW}⚠${CL}  ${YW}$1${CL}"; }

on_error() {
  local code=$? line=$1
  stop_spinner
  echo -e "\n${CROSS} ${RD}Error (code ${code}) at line ${line}: ${YW}${BASH_COMMAND}${CL}\n"
  exit "$code"
}
trap 'on_error $LINENO' ERR
trap 'stop_spinner' EXIT

exit_script() { stop_spinner; clear; echo -e "${CROSS} ${RD}Aborted.${CL}\n"; exit 0; }

# -- checks ------------------------------------------------------------------

check_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    msg_error "This script must be run as root (sudo -i)."; exit 1
  fi
}

check_pve() {
  if ! command -v pveversion >/dev/null 2>&1; then
    msg_error "Proxmox VE not detected: this script must be run on a Proxmox node."; exit 1
  fi
  if [[ ! -f /etc/pve/.vmlist ]]; then
    msg_error "/etc/pve is not mounted (pve-cluster service stopped?)."; exit 1
  fi
}

check_deps() {
  local missing=()
  for cmd in python3 whiptail curl tar; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  if (( ${#missing[@]} )); then
    msg_info "Installing dependencies (${missing[*]})"
    apt-get update -qq >/dev/null && apt-get install -y -qq "${missing[@]}" >/dev/null
    msg_ok "Dependencies installed"
  fi
}

is_installed() { [[ -d "$PREFIX/freeprox" ]]; }

# -- fetching the code -------------------------------------------------------

SRC=""
fetch_source() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [[ -n "$here" && -f "$here/freeprox/__init__.py" && -z "${FREEPROX_FORCE_DOWNLOAD:-}" ]]; then
    SRC="$here"
    msg_ok "Using local sources (${SRC})"
    return
  fi
  msg_info "Downloading ${APP} (${REPO}@${BRANCH})"
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" | tar -xz -C "$tmp"
  SRC="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n1)"
  msg_ok "Downloaded ${APP} (${REPO}@${BRANCH})"
}

install_files() {
  msg_info "Installing files"
  rm -rf "$PREFIX/freeprox"
  mkdir -p "$PREFIX"
  cp -r "$SRC/freeprox" "$PREFIX/"
  find "$PREFIX" -name __pycache__ -prune -exec rm -rf {} +
  install -m 0644 "$SRC/etc/freeprox.conf" "$PREFIX/freeprox.conf.example"
  install -m 0755 "$SRC/install.sh" "$PREFIX/install.sh"
  install -m 0755 "$SRC/bin/freeprox" /usr/local/bin/freeprox
  install -m 0644 "$SRC/systemd/freeprox.service" /etc/systemd/system/freeprox.service
  install -d -m 0700 "$CONF_DIR"
  systemctl daemon-reload
  msg_ok "Files installed ($(installed_version))"
}

installed_version() {
  PYTHONPATH="$PREFIX" python3 -c 'import freeprox; print("v" + freeprox.__version__)' 2>/dev/null || echo "?"
}

# -- configuration -----------------------------------------------------------

FBX_URL="http://mafreebox.freebox.fr"
BRIDGES="vmbr0"
TAG="freeprox"
POOL_START=""
POOL_END=""
REMOVE_ON_DESTROY="false"

valid_ip() {
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  local o; for o in "${BASH_REMATCH[@]:1}"; do (( o <= 255 )) || return 1; done
}

list_bridges() {
  local b
  for b in /sys/class/net/vmbr*; do [[ -e "$b" ]] && basename "$b"; done
}

default_settings() {
  echo -e " ${BL}Freebox:${CL}             ${GN}${FBX_URL}${CL}"
  echo -e " ${BL}Watched bridges:${CL}     ${GN}${BRIDGES}${CL}"
  echo -e " ${BL}IP pool:${CL}             ${GN}Freebox DHCP range${CL}"
  echo -e " ${BL}Proxmox tag:${CL}         ${GN}${TAG}${CL}"
  echo -e " ${BL}Release on destroy:${CL}  ${GN}no${CL}\n"
}

advanced_settings() {
  FBX_URL=$(whiptail --backtitle "$BACKTITLE" --title "FREEBOX" --inputbox \
    "Freebox address" 9 60 "$FBX_URL" 3>&1 1>&2 2>&3) || exit_script

  local items=() b state
  while read -r b; do
    [[ -z "$b" ]] && continue
    state=OFF; [[ " ${BRIDGES//,/ } " == *" $b "* ]] && state=ON
    items+=("$b" "" "$state")
  done < <(list_bridges)
  if (( ${#items[@]} )); then
    BRIDGES=$(whiptail --backtitle "$BACKTITLE" --title "NETWORK BRIDGES" --separate-output --checklist \
      "Bridges connected to the Freebox LAN\n(only NICs attached to them get an IP reservation)" \
      16 60 6 "${items[@]}" 3>&1 1>&2 2>&3 | tr '\n' ' ' | xargs) || exit_script
  else
    BRIDGES=$(whiptail --backtitle "$BACKTITLE" --title "NETWORK BRIDGES" --inputbox \
      "Bridges connected to the Freebox LAN (space separated)" 9 60 "$BRIDGES" 3>&1 1>&2 2>&3) || exit_script
  fi

  while true; do
    POOL_START=$(whiptail --backtitle "$BACKTITLE" --title "IP POOL" --inputbox \
      "First IP to assign\n(leave empty = Freebox DHCP range)" 10 60 "$POOL_START" 3>&1 1>&2 2>&3) || exit_script
    [[ -z "$POOL_START" ]] && { POOL_END=""; break; }
    POOL_END=$(whiptail --backtitle "$BACKTITLE" --title "IP POOL" --inputbox \
      "Last IP to assign" 9 60 "$POOL_END" 3>&1 1>&2 2>&3) || exit_script
    valid_ip "$POOL_START" && valid_ip "$POOL_END" && break
    whiptail --backtitle "$BACKTITLE" --msgbox "Invalid IP address, please try again." 8 50
  done

  while true; do
    TAG=$(whiptail --backtitle "$BACKTITLE" --title "PROXMOX TAG" --inputbox \
      "Tag added to each VM/LXC whose IP has been pinned by freeprox\n(leave empty to disable)" 10 60 "$TAG" 3>&1 1>&2 2>&3) || exit_script
    [[ -z "$TAG" || "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_+.-]*$ ]] && break
    whiptail --backtitle "$BACKTITLE" --msgbox "Invalid tag: letters, digits, _ + . - only." 8 50
  done

  if whiptail --backtitle "$BACKTITLE" --title "DESTROY" --defaultno --yesno \
    "Automatically delete the Freebox lease when a VM/LXC is destroyed?" 9 60; then
    REMOVE_ON_DESTROY="true"
  else
    REMOVE_ON_DESTROY="false"
  fi

  echo -e " ${BL}Freebox:${CL}             ${GN}${FBX_URL}${CL}"
  echo -e " ${BL}Watched bridges:${CL}     ${GN}${BRIDGES:-all}${CL}"
  echo -e " ${BL}IP pool:${CL}             ${GN}${POOL_START:-DHCP range}${POOL_END:+ - $POOL_END}${CL}"
  echo -e " ${BL}Proxmox tag:${CL}         ${GN}${TAG:-none}${CL}"
  echo -e " ${BL}Release on destroy:${CL}  ${GN}$([[ $REMOVE_ON_DESTROY == true ]] && echo yes || echo no)${CL}\n"

  whiptail --backtitle "$BACKTITLE" --title "CONFIRMATION" --yesno \
    "Install freeprox with these settings?" 8 50 || exit_script
}

choose_settings() {
  if whiptail --backtitle "$BACKTITLE" --title "SETTINGS" --yes-button "Default" --no-button "Advanced" --yesno \
    "Use default settings?\n\n  Freebox: ${FBX_URL}\n  Bridge: ${BRIDGES}\n  Pool: Freebox DHCP range\n  Proxmox tag: ${TAG}\n  Release on destroy: no" 14 62; then
    default_settings
  else
    header_info
    advanced_settings
  fi
}

write_config() {
  msg_info "Writing configuration"
  [[ -f "$CONF" ]] && cp -p "$CONF" "$CONF.bak"
  (umask 077; cat >"$CONF") <<EOF
# Generated by install.sh on $(date '+%Y-%m-%d %H:%M').
# All options are documented in $PREFIX/freeprox.conf.example

[freebox]
url = ${FBX_URL}

[proxmox]
bridges = ${BRIDGES}
tag = ${TAG}

[dhcp]
pool_start = ${POOL_START}
pool_end = ${POOL_END}

[daemon]
remove_on_destroy = ${REMOVE_ON_DESTROY}
EOF
  msg_ok "Configuration written (${CONF}$([[ -f "$CONF.bak" ]] && echo ", previous one: $CONF.bak"))"
}

# -- Freebox pairing ---------------------------------------------------------

register_freebox() {
  if [[ -s "$CONF_DIR/app_token" ]]; then
    msg_ok "Freebox token already present"
    return
  fi
  whiptail --backtitle "$BACKTITLE" --title "FREEBOX PAIRING" --msgbox \
    "freeprox will now request access to the Freebox API.\n\nAfter pressing OK, go to the Freebox: a request will show up on its LCD display.\nAccept it with the right arrow / OK button (3 minutes max)." 13 66
  echo -e " ${YW}→ Approve the request on the Freebox display...${CL}"
  until freeprox register; do
    whiptail --backtitle "$BACKTITLE" --title "FREEBOX PAIRING" --yes-button "Retry" --no-button "Abort" \
      --yesno "Pairing failed or was not approved in time.\n\nRetry?" 10 60 || exit_script
  done
  msg_ok "Freebox paired"
}

check_permission() {
  local rc
  while true; do
    msg_info "Checking Freebox permissions"
    rc=0; freeprox check >/tmp/freeprox-check.log 2>&1 || rc=$?
    if (( rc == 0 )); then
      msg_ok "Freebox permissions OK"
      sed 's/^/     /' /tmp/freeprox-check.log
      rm -f /tmp/freeprox-check.log
      return
    fi
    if (( rc == 2 )); then
      msg_warn "Missing \"settings modification\" permission"
      whiptail --backtitle "$BACKTITLE" --title "PERMISSION REQUIRED" --yes-button "Done" --no-button "Later" --yesno \
        "In Freebox OS (http://mafreebox.freebox.fr):\n\n  Freebox Settings\n  > Access Management > Applications tab\n  > Freeprox (pencil icon)\n  > tick \"Modification des réglages de la Freebox\"\n\nThen click \"Done\"." 16 64 && continue
      msg_warn "The service cannot create leases until the permission is granted"
      return
    fi
    msg_error "Cannot connect to the Freebox:"
    sed 's/^/     /' /tmp/freeprox-check.log
    rm -f /tmp/freeprox-check.log
    whiptail --backtitle "$BACKTITLE" --title "ERROR" --yes-button "Retry" --no-button "Abort" \
      --yesno "The check failed (see the terminal).\n\nRetry?" 10 60 || exit_script
  done
}

offer_existing() {
  local count
  count=$(PYTHONPATH="$PREFIX" python3 - <<'EOF' 2>/dev/null || echo 0
from freeprox.config import Config
from freeprox.proxmox import ProxmoxHost
cfg = Config()
pve = ProxmoxHost(cfg.get("proxmox", "pve_root"), cfg.get("proxmox", "node") or None)
bridges = set(cfg.getlist("proxmox", "bridges"))
print(sum(1 for g in pve.local_guests() if not g.is_template and any(not bridges or n.bridge in bridges for n in g.nics)))
EOF
)
  (( count > 0 )) || return 0
  if whiptail --backtitle "$BACKTITLE" --title "EXISTING GUESTS" --defaultno --yesno \
    "${count} VM/LXC already exist on this node.\n\nReserve their IPs on the Freebox now as well?\n(otherwise only new guests will be handled)" 12 62; then
    msg_info "Reserving IPs for existing guests"
    freeprox sync --all >/tmp/freeprox-sync.log 2>&1 || true
    msg_ok "Existing guests processed"
    sed 's/^/     /' /tmp/freeprox-sync.log
    rm -f /tmp/freeprox-sync.log
  fi
}

start_service() {
  msg_info "Starting service"
  systemctl enable -q --now freeprox
  sleep 2
  if systemctl is-active -q freeprox; then
    msg_ok "freeprox service started"
  else
    msg_error "The service failed to start: journalctl -u freeprox"
  fi
}

# -- actions -----------------------------------------------------------------

do_install() {
  choose_settings
  fetch_source
  install_files
  write_config
  register_freebox
  check_permission
  offer_existing
  start_service
  echo -e "\n ${GN}freeprox is installed.${CL} Every new VM/LXC will get its IP reserved on the Freebox."
  echo -e " Follow activity:  ${BL}journalctl -u freeprox -f${CL}"
  echo -e " Created leases:   ${BL}freeprox leases --mine${CL}"
  echo -e " Update:           ${BL}bash $PREFIX/install.sh --update${CL}\n"
}

do_update() {
  fetch_source
  install_files
  if systemctl is-enabled -q freeprox 2>/dev/null; then
    msg_info "Restarting service"
    systemctl restart freeprox
    msg_ok "Service restarted"
  fi
  echo -e "\n ${GN}freeprox updated ($(installed_version)).${CL}\n"
}

do_uninstall() {
  local purge="${1:-}"
  msg_info "Stopping service"
  systemctl disable -q --now freeprox 2>/dev/null || true
  msg_ok "Service stopped"
  msg_info "Removing files"
  rm -f /etc/systemd/system/freeprox.service /usr/local/bin/freeprox
  rm -rf "$PREFIX"
  systemctl daemon-reload
  if [[ "$purge" == "purge" ]]; then
    rm -rf "$CONF_DIR" /var/lib/freeprox
    msg_ok "freeprox removed (including configuration and token)"
  else
    msg_ok "freeprox removed (configuration kept in ${CONF_DIR})"
  fi
  echo -e "\n Leases already created stay on the Freebox (Freebox OS > DHCP > Static leases).\n"
}

menu_installed() {
  local choice
  choice=$(whiptail --backtitle "$BACKTITLE" --title "FREEPROX $(installed_version) ALREADY INSTALLED" --menu \
    "What do you want to do?" 15 64 5 \
    "update"      "Update" \
    "reconfigure" "Reconfigure (and re-pair if needed)" \
    "uninstall"   "Uninstall (keep config and token)" \
    "purge"       "Uninstall completely" \
    3>&1 1>&2 2>&3) || exit_script
  case "$choice" in
    update) do_update ;;
    reconfigure) do_install ;;
    uninstall)
      whiptail --backtitle "$BACKTITLE" --defaultno --yesno "Uninstall freeprox?" 8 40 || exit_script
      do_uninstall ;;
    purge)
      whiptail --backtitle "$BACKTITLE" --defaultno --yesno \
        "Remove freeprox, its configuration and its Freebox token?" 8 60 || exit_script
      do_uninstall purge ;;
  esac
}

main() {
  header_info
  check_root
  case "${1:-}" in
    --update) check_pve; check_deps; do_update; return ;;
    --uninstall) do_uninstall "$([[ "${2:-}" == "--purge" ]] && echo purge)"; return ;;
    "") ;;
    *) echo "Usage: $0 [--update | --uninstall [--purge]]"; exit 1 ;;
  esac
  check_pve
  check_deps
  if is_installed; then
    menu_installed
  else
    whiptail --backtitle "$BACKTITLE" --title "FREEPROX" --yesno \
      "Install freeprox on this node ($(hostname))?\n\nWhenever a VM or LXC is created, its IP will be reserved on the Freebox with a descriptive comment." 12 64 || exit_script
    do_install
  fi
}

main "$@"
