#!/bin/sh
# Installe freeprox sur un nœud Proxmox VE. À lancer en root depuis le dépôt cloné.
set -eu

PREFIX=/opt/freeprox
SRC=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit être lancé en root." >&2
    exit 1
fi
if [ ! -d /etc/pve ]; then
    echo "Attention : /etc/pve introuvable, est-ce bien un nœud Proxmox VE ?" >&2
fi
command -v python3 >/dev/null || { echo "python3 est requis." >&2; exit 1; }

echo "-> Copie du code dans $PREFIX"
rm -rf "$PREFIX/freeprox"
mkdir -p "$PREFIX"
cp -r "$SRC/freeprox" "$PREFIX/"
find "$PREFIX" -name __pycache__ -prune -exec rm -rf {} +
install -m 0755 "$SRC/bin/freeprox" /usr/local/bin/freeprox

echo "-> Configuration"
install -d -m 0700 /etc/freeprox
if [ ! -f /etc/freeprox/freeprox.conf ]; then
    install -m 0600 "$SRC/etc/freeprox.conf" /etc/freeprox/freeprox.conf
else
    echo "   /etc/freeprox/freeprox.conf existe déjà, conservé."
fi

echo "-> Service systemd"
install -m 0644 "$SRC/systemd/freeprox.service" /etc/systemd/system/freeprox.service
systemctl daemon-reload

if [ ! -s /etc/freeprox/app_token ]; then
    cat <<'EOF'

Installation terminée. Étapes suivantes :
  1. freeprox register        # puis valider sur l'écran de la Freebox
  2. Dans Freebox OS : Paramètres > Gestion des accès > Applications > Freeprox
     cocher « Modification des réglages de la Freebox »
  3. freeprox check
  4. systemctl enable --now freeprox
EOF
else
    systemctl restart freeprox 2>/dev/null || true
    echo "Mise à jour terminée (service redémarré s'il était actif)."
fi
