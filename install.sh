#!/usr/bin/env bash
# freeprox — installation / mise à jour / désinstallation sur un nœud Proxmox VE.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/sudoPierre/freeprox/main/install.sh)"
#
# Options non interactives : --update, --uninstall [--purge]

set -Eeuo pipefail

REPO="sudoPierre/freeprox"
BRANCH="${FREEPROX_BRANCH:-main}"
APP="freeprox"
PREFIX="/opt/freeprox"
CONF_DIR="/etc/freeprox"
CONF="$CONF_DIR/freeprox.conf"
BACKTITLE="Proxmox VE Helper — freeprox"

# -- affichage (style tteck) -------------------------------------------------

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
  echo -e "${DGN}  Réservation automatique des IP Freebox pour les VM/LXC Proxmox${CL}\n"
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
  echo -e "\n${CROSS} ${RD}Erreur (code ${code}) à la ligne ${line} : ${YW}${BASH_COMMAND}${CL}\n"
  exit "$code"
}
trap 'on_error $LINENO' ERR
trap 'stop_spinner' EXIT

exit_script() { stop_spinner; clear; echo -e "${CROSS} ${RD}Abandon.${CL}\n"; exit 0; }

# -- vérifications -----------------------------------------------------------

check_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    msg_error "Ce script doit être lancé en root (sudo -i)."; exit 1
  fi
}

check_pve() {
  if ! command -v pveversion >/dev/null 2>&1; then
    msg_error "Proxmox VE non détecté : ce script doit être lancé sur un nœud Proxmox."; exit 1
  fi
  if [[ ! -f /etc/pve/.vmlist ]]; then
    msg_error "/etc/pve n'est pas monté (service pve-cluster arrêté ?)."; exit 1
  fi
}

check_deps() {
  local missing=()
  for cmd in python3 whiptail curl tar; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  if (( ${#missing[@]} )); then
    msg_info "Installation des dépendances (${missing[*]})"
    apt-get update -qq >/dev/null && apt-get install -y -qq "${missing[@]}" >/dev/null
    msg_ok "Dépendances installées"
  fi
}

is_installed() { [[ -d "$PREFIX/freeprox" ]]; }

# -- récupération du code ----------------------------------------------------

SRC=""
fetch_source() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [[ -n "$here" && -f "$here/freeprox/__init__.py" && -z "${FREEPROX_FORCE_DOWNLOAD:-}" ]]; then
    SRC="$here"
    msg_ok "Sources locales utilisées (${SRC})"
    return
  fi
  msg_info "Téléchargement de ${APP} (${REPO}@${BRANCH})"
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" | tar -xz -C "$tmp"
  SRC="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n1)"
  msg_ok "Téléchargé ${APP} (${REPO}@${BRANCH})"
}

install_files() {
  msg_info "Installation des fichiers"
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
  msg_ok "Fichiers installés ($(installed_version))"
}

installed_version() {
  PYTHONPATH="$PREFIX" python3 -c 'import freeprox; print("v" + freeprox.__version__)' 2>/dev/null || echo "?"
}

# -- configuration -----------------------------------------------------------

FBX_URL="http://mafreebox.freebox.fr"
BRIDGES="vmbr0"
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
  echo -e " ${BL}Freebox :${CL}            ${GN}${FBX_URL}${CL}"
  echo -e " ${BL}Ponts surveillés :${CL}   ${GN}${BRIDGES}${CL}"
  echo -e " ${BL}Pool d'IP :${CL}          ${GN}plage DHCP de la Freebox${CL}"
  echo -e " ${BL}Libérer à la suppr. :${CL} ${GN}non${CL}\n"
}

advanced_settings() {
  FBX_URL=$(whiptail --backtitle "$BACKTITLE" --title "FREEBOX" --inputbox \
    "Adresse de la Freebox" 9 60 "$FBX_URL" 3>&1 1>&2 2>&3) || exit_script

  local items=() b state
  while read -r b; do
    [[ -z "$b" ]] && continue
    state=OFF; [[ " ${BRIDGES//,/ } " == *" $b "* ]] && state=ON
    items+=("$b" "" "$state")
  done < <(list_bridges)
  if (( ${#items[@]} )); then
    BRIDGES=$(whiptail --backtitle "$BACKTITLE" --title "PONTS RÉSEAU" --separate-output --checklist \
      "Ponts reliés au LAN de la Freebox\n(seules les cartes réseau branchées dessus auront une IP réservée)" \
      16 60 6 "${items[@]}" 3>&1 1>&2 2>&3 | tr '\n' ' ' | xargs) || exit_script
  else
    BRIDGES=$(whiptail --backtitle "$BACKTITLE" --title "PONTS RÉSEAU" --inputbox \
      "Ponts reliés au LAN de la Freebox (séparés par des espaces)" 9 60 "$BRIDGES" 3>&1 1>&2 2>&3) || exit_script
  fi

  while true; do
    POOL_START=$(whiptail --backtitle "$BACKTITLE" --title "POOL D'IP" --inputbox \
      "Première IP à attribuer\n(laisser vide = plage DHCP de la Freebox)" 10 60 "$POOL_START" 3>&1 1>&2 2>&3) || exit_script
    [[ -z "$POOL_START" ]] && { POOL_END=""; break; }
    POOL_END=$(whiptail --backtitle "$BACKTITLE" --title "POOL D'IP" --inputbox \
      "Dernière IP à attribuer" 9 60 "$POOL_END" 3>&1 1>&2 2>&3) || exit_script
    valid_ip "$POOL_START" && valid_ip "$POOL_END" && break
    whiptail --backtitle "$BACKTITLE" --msgbox "Adresse IP invalide, recommencez." 8 50
  done

  if whiptail --backtitle "$BACKTITLE" --title "SUPPRESSION" --defaultno --yesno \
    "Supprimer automatiquement le bail Freebox quand une VM/un LXC est détruit ?" 9 60; then
    REMOVE_ON_DESTROY="true"
  else
    REMOVE_ON_DESTROY="false"
  fi

  echo -e " ${BL}Freebox :${CL}            ${GN}${FBX_URL}${CL}"
  echo -e " ${BL}Ponts surveillés :${CL}   ${GN}${BRIDGES:-tous}${CL}"
  echo -e " ${BL}Pool d'IP :${CL}          ${GN}${POOL_START:-plage DHCP}${POOL_END:+ - $POOL_END}${CL}"
  echo -e " ${BL}Libérer à la suppr. :${CL} ${GN}$([[ $REMOVE_ON_DESTROY == true ]] && echo oui || echo non)${CL}\n"

  whiptail --backtitle "$BACKTITLE" --title "CONFIRMATION" --yesno \
    "Installer freeprox avec ces réglages ?" 8 50 || exit_script
}

choose_settings() {
  if whiptail --backtitle "$BACKTITLE" --title "RÉGLAGES" --yes-button "Par défaut" --no-button "Avancés" --yesno \
    "Utiliser les réglages par défaut ?\n\n  Freebox : ${FBX_URL}\n  Pont : ${BRIDGES}\n  Pool : plage DHCP de la Freebox\n  Libération à la suppression : non" 13 62; then
    default_settings
  else
    header_info
    advanced_settings
  fi
}

write_config() {
  msg_info "Écriture de la configuration"
  [[ -f "$CONF" ]] && cp -p "$CONF" "$CONF.bak"
  (umask 077; cat >"$CONF") <<EOF
# Généré par install.sh le $(date '+%Y-%m-%d %H:%M').
# Toutes les options sont documentées dans $PREFIX/freeprox.conf.example

[freebox]
url = ${FBX_URL}

[proxmox]
bridges = ${BRIDGES}

[dhcp]
pool_start = ${POOL_START}
pool_end = ${POOL_END}

[daemon]
remove_on_destroy = ${REMOVE_ON_DESTROY}
EOF
  msg_ok "Configuration écrite (${CONF}$([[ -f "$CONF.bak" ]] && echo ", ancienne : $CONF.bak"))"
}

# -- association Freebox -----------------------------------------------------

register_freebox() {
  if [[ -s "$CONF_DIR/app_token" ]]; then
    msg_ok "Jeton Freebox déjà présent"
    return
  fi
  whiptail --backtitle "$BACKTITLE" --title "ASSOCIATION FREEBOX" --msgbox \
    "freeprox va demander l'accès à l'API de la Freebox.\n\nAprès avoir validé, rendez-vous devant la Freebox : une demande va s'afficher sur son écran LCD.\nAcceptez-la avec la flèche droite / le bouton OK (3 minutes maximum)." 13 66
  echo -e " ${YW}→ Validez la demande sur l'écran de la Freebox...${CL}"
  until freeprox register; do
    whiptail --backtitle "$BACKTITLE" --title "ASSOCIATION FREEBOX" --yes-button "Réessayer" --no-button "Abandonner" \
      --yesno "L'association a échoué ou n'a pas été validée à temps.\n\nRéessayer ?" 10 60 || exit_script
  done
  msg_ok "Freebox associée"
}

check_permission() {
  local rc
  while true; do
    msg_info "Vérification des droits sur la Freebox"
    rc=0; freeprox check >/tmp/freeprox-check.log 2>&1 || rc=$?
    if (( rc == 0 )); then
      msg_ok "Droits Freebox OK"
      sed 's/^/     /' /tmp/freeprox-check.log
      rm -f /tmp/freeprox-check.log
      return
    fi
    if (( rc == 2 )); then
      msg_warn "Droit « Modification des réglages » manquant"
      whiptail --backtitle "$BACKTITLE" --title "PERMISSION REQUISE" --yes-button "C'est fait" --no-button "Plus tard" --yesno \
        "Dans Freebox OS (http://mafreebox.freebox.fr) :\n\n  Paramètres de la Freebox\n  > Gestion des accès > onglet Applications\n  > Freeprox (crayon)\n  > cocher « Modification des réglages de la Freebox »\n\nPuis cliquez sur « C'est fait »." 16 64 && continue
      msg_warn "Le service ne pourra pas créer de bail tant que le droit n'est pas accordé"
      return
    fi
    msg_error "Connexion à la Freebox impossible :"
    sed 's/^/     /' /tmp/freeprox-check.log
    rm -f /tmp/freeprox-check.log
    whiptail --backtitle "$BACKTITLE" --title "ERREUR" --yes-button "Réessayer" --no-button "Abandonner" \
      --yesno "La vérification a échoué (voir le terminal).\n\nRéessayer ?" 10 60 || exit_script
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
  if whiptail --backtitle "$BACKTITLE" --title "INVITÉS EXISTANTS" --defaultno --yesno \
    "${count} VM/LXC existent déjà sur ce nœud.\n\nRéserver aussi leur IP sur la Freebox maintenant ?\n(sinon seuls les nouveaux invités seront traités)" 12 62; then
    msg_info "Réservation des IP des invités existants"
    freeprox sync --all >/tmp/freeprox-sync.log 2>&1 || true
    msg_ok "Invités existants traités"
    sed 's/^/     /' /tmp/freeprox-sync.log
    rm -f /tmp/freeprox-sync.log
  fi
}

start_service() {
  msg_info "Démarrage du service"
  systemctl enable -q --now freeprox
  sleep 2
  if systemctl is-active -q freeprox; then
    msg_ok "Service freeprox démarré"
  else
    msg_error "Le service n'a pas démarré : journalctl -u freeprox"
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
  echo -e "\n ${GN}freeprox est installé.${CL} Chaque nouvelle VM/LXC aura son IP réservée sur la Freebox."
  echo -e " Suivre l'activité :  ${BL}journalctl -u freeprox -f${CL}"
  echo -e " Baux créés :         ${BL}freeprox leases --mine${CL}"
  echo -e " Mettre à jour :      ${BL}bash $PREFIX/install.sh --update${CL}\n"
}

do_update() {
  fetch_source
  install_files
  if systemctl is-enabled -q freeprox 2>/dev/null; then
    msg_info "Redémarrage du service"
    systemctl restart freeprox
    msg_ok "Service redémarré"
  fi
  echo -e "\n ${GN}freeprox mis à jour ($(installed_version)).${CL}\n"
}

do_uninstall() {
  local purge="${1:-}"
  msg_info "Arrêt du service"
  systemctl disable -q --now freeprox 2>/dev/null || true
  msg_ok "Service arrêté"
  msg_info "Suppression des fichiers"
  rm -f /etc/systemd/system/freeprox.service /usr/local/bin/freeprox
  rm -rf "$PREFIX"
  systemctl daemon-reload
  if [[ "$purge" == "purge" ]]; then
    rm -rf "$CONF_DIR" /var/lib/freeprox
    msg_ok "freeprox supprimé (configuration et jeton compris)"
  else
    msg_ok "freeprox supprimé (configuration conservée dans ${CONF_DIR})"
  fi
  echo -e "\n Les baux déjà créés restent sur la Freebox (Freebox OS > DHCP > Baux statiques).\n"
}

menu_installed() {
  local choice
  choice=$(whiptail --backtitle "$BACKTITLE" --title "FREEPROX $(installed_version) DÉJÀ INSTALLÉ" --menu \
    "Que voulez-vous faire ?" 15 64 5 \
    "update"      "Mettre à jour" \
    "reconfigure" "Reconfigurer (et réassocier si besoin)" \
    "uninstall"   "Désinstaller (garder config et jeton)" \
    "purge"       "Désinstaller complètement" \
    3>&1 1>&2 2>&3) || exit_script
  case "$choice" in
    update) do_update ;;
    reconfigure) do_install ;;
    uninstall)
      whiptail --backtitle "$BACKTITLE" --defaultno --yesno "Désinstaller freeprox ?" 8 40 || exit_script
      do_uninstall ;;
    purge)
      whiptail --backtitle "$BACKTITLE" --defaultno --yesno \
        "Supprimer freeprox, sa configuration et son jeton Freebox ?" 8 60 || exit_script
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
    *) echo "Usage : $0 [--update | --uninstall [--purge]]"; exit 1 ;;
  esac
  check_pve
  check_deps
  if is_installed; then
    menu_installed
  else
    whiptail --backtitle "$BACKTITLE" --title "FREEPROX" --yesno \
      "Installer freeprox sur ce nœud ($(hostname)) ?\n\nÀ chaque création de VM ou de LXC, son IP sera réservée sur la Freebox avec un commentaire descriptif." 12 64 || exit_script
    do_install
  fi
}

main "$@"
