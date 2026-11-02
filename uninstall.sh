#!/bin/sh
# Désinstalle freeprox. Les baux déjà créés sur la Freebox sont conservés.
# Ajouter --purge pour supprimer aussi /etc/freeprox et /var/lib/freeprox.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "Ce script doit être lancé en root." >&2; exit 1; }

systemctl disable --now freeprox 2>/dev/null || true
rm -f /etc/systemd/system/freeprox.service /usr/local/bin/freeprox
rm -rf /opt/freeprox
systemctl daemon-reload

if [ "${1:-}" = "--purge" ]; then
    rm -rf /etc/freeprox /var/lib/freeprox
fi
echo "freeprox désinstallé."
