"""Logique principale : détecter les nouveaux invités et leur réserver une IP sur la Freebox."""

import ipaddress
import json
import logging
import os
import tempfile
import time

from .allocator import lan_network, parse_excludes, pick_free_ip, answers_ping
from .comment import render_comment

log = logging.getLogger(__name__)

# Un invité doit avoir disparu de .vmlist pendant ce nombre de passes avant d'être
# considéré comme supprimé (protège contre une lecture transitoire incomplète de pmxcfs).
MISSING_PASSES_BEFORE_DESTROY = 3


class State:
    """Mémorise les cartes réseau déjà traitées : {vmid: {type, node, nics: {mac: {...}}}}."""

    def __init__(self, path):
        self.path = path
        self.initialized = False
        self.guests = {}
        try:
            with open(path) as fh:
                data = json.load(fh)
            self.initialized = data.get("initialized", False)
            self.guests = data.get("guests", {})
        except (OSError, ValueError):
            pass

    def guest(self, vmid):
        return self.guests.setdefault(str(vmid), {"nics": {}})

    def save(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-")
        with os.fdopen(fd, "w") as fh:
            json.dump({"initialized": self.initialized, "guests": self.guests}, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)


class Freeprox:
    def __init__(self, config, freebox, proxmox, state, dry_run=False, pinger=answers_ping):
        self.cfg = config
        self.fbx = freebox
        self.pve = proxmox
        self.state = state
        self.dry_run = dry_run or config.getbool("daemon", "dry_run")
        self.pinger = pinger
        self.bridges = set(config.getlist("proxmox", "bridges"))

    # -- sélection -------------------------------------------------------

    def eligible_nics(self, guest):
        return [nic for nic in guest.nics if not self.bridges or nic.bridge in self.bridges]

    def _ready(self, guest):
        if guest.lock:
            log.debug("%s %d verrouillé (%s), on attend", guest.type, guest.vmid, guest.lock)
            return False
        if time.time() - guest.mtime < self.cfg.getint("daemon", "settle_seconds"):
            return False
        return True

    # -- bail DHCP -------------------------------------------------------

    def ensure_lease(self, guest, nic):
        """Crée (ou met à jour) le bail statique de la carte. Retourne (ip, créé_par_nous)."""
        statics = self.fbx.static_leases()
        existing = next((l for l in statics if l.get("mac", "").upper() == nic.mac), None)
        template = self.cfg.get("comment", "template")
        max_len = self.cfg.getint("comment", "max_length")

        if existing:
            ip = existing["ip"]
            comment = render_comment(template, guest, nic, ip, max_len)
            if self.cfg.getbool("comment", "update_existing") and existing.get("comment") != comment:
                log.info("%s %d/%s : bail existant %s -> %s, mise à jour du commentaire",
                         guest.type, guest.vmid, nic.iface, nic.mac, ip)
                if not self.dry_run:
                    self.fbx.update_static_lease(existing["id"], comment=comment)
            else:
                log.info("%s %d/%s : bail existant %s -> %s", guest.type, guest.vmid, nic.iface, nic.mac, ip)
            return ip, False

        ip = self._choose_ip(nic, statics)
        comment = render_comment(template, guest, nic, ip, max_len)
        log.info("%s %d/%s : réservation %s -> %s « %s »%s", guest.type, guest.vmid, nic.iface,
                 nic.mac, ip, comment, " (dry-run)" if self.dry_run else "")
        if not self.dry_run:
            self.fbx.add_static_lease(nic.mac, ip, comment)
        return ip, True

    def _choose_ip(self, nic, statics):
        dhcp = self.fbx.dhcp_config()
        network = lan_network(dhcp)
        reserved = {l["ip"] for l in statics}
        dynamic = self.fbx.dynamic_leases()

        # 1. IP statique déjà configurée dans l'invité (LXC ip=… / cloud-init).
        if self.cfg.getbool("dhcp", "use_guest_static_ip") and nic.static_ip:
            if _in_network(nic.static_ip, network) and nic.static_ip not in reserved:
                return nic.static_ip
            log.warning("IP statique %s de %s inutilisable (hors LAN %s ou déjà réservée)", nic.static_ip, nic.mac, network)

        # 2. IP actuellement attribuée par le DHCP à ce MAC : on la fige.
        if self.cfg.getbool("dhcp", "prefer_current_ip"):
            current = next((l["ip"] for l in dynamic if l.get("mac", "").upper() == nic.mac), None)
            if current and current not in reserved and _in_network(current, network):
                return current

        # 3. Première IP libre du pool.
        used = reserved | {l["ip"] for l in dynamic if l.get("mac", "").upper() != nic.mac}
        return pick_free_ip(
            dhcp, used,
            pool_start=self.cfg.get("dhcp", "pool_start") or None,
            pool_end=self.cfg.get("dhcp", "pool_end") or None,
            excluded=parse_excludes(self.cfg.getlist("dhcp", "exclude")),
            ping_check=self.cfg.getbool("dhcp", "ping_check"),
            pinger=self.pinger,
        )

    # -- traitement d'un invité -----------------------------------------

    def process_guest(self, guest, force=False):
        if guest.is_template and self.cfg.getbool("proxmox", "skip_templates"):
            return
        entry = self.state.guest(guest.vmid)
        entry.update(type=guest.type, node=guest.node, missing=0)
        for nic in self.eligible_nics(guest):
            if nic.mac in entry["nics"] and not force:
                continue
            try:
                ip, created = self.ensure_lease(guest, nic)
            except Exception as exc:  # on réessaiera à la prochaine passe
                log.error("%s %d/%s : échec de la réservation : %s", guest.type, guest.vmid, nic.iface, exc)
                continue
            if self.dry_run:
                continue
            previous = entry["nics"].get(nic.mac, {})
            entry["nics"][nic.mac] = {
                "iface": nic.iface,
                "ip": ip,
                "created": created or previous.get("created", False),
            }

    def release_guest(self, vmid):
        """Supprime les baux créés par freeprox pour cet invité."""
        entry = self.state.guests.get(str(vmid))
        if not entry:
            return 0
        removed = 0
        for mac, info in entry.get("nics", {}).items():
            if not info.get("created"):
                continue
            log.info("Invité %s supprimé : libération du bail %s (%s)%s", vmid, mac, info.get("ip"),
                     " (dry-run)" if self.dry_run else "")
            if self.dry_run:
                continue
            try:
                self.fbx.delete_static_lease(mac)
                removed += 1
            except Exception as exc:
                log.error("Échec de la suppression du bail %s : %s", mac, exc)
        return removed

    # -- passes ----------------------------------------------------------

    def initialize(self, guests):
        """Premier démarrage : les invités déjà présents sont marqués comme connus, sans être traités."""
        if self.state.initialized:
            return
        if not self.cfg.getbool("proxmox", "process_existing"):
            for guest in guests:
                entry = self.state.guest(guest.vmid)
                entry.update(type=guest.type, node=guest.node, missing=0)
                for nic in self.eligible_nics(guest):
                    entry["nics"].setdefault(nic.mac, {"iface": nic.iface, "ip": None, "created": False})
            log.info("Initialisation : %d invité(s) existant(s) ignoré(s) (voir 'freeprox sync --all')", len(guests))
        self.state.initialized = True

    def run_once(self):
        vmlist = self.pve.vmlist()
        guests = self.pve.local_guests(vmlist)
        self.initialize(guests)

        for guest in guests:
            known = self.state.guests.get(str(guest.vmid), {}).get("nics", {})
            if all(nic.mac in known for nic in self.eligible_nics(guest)):
                self.state.guest(guest.vmid)["missing"] = 0
                continue
            if self._ready(guest):
                self.process_guest(guest)

        local_ids = {g.vmid for g in guests}
        for vmid in list(self.state.guests):
            if int(vmid) in local_ids:
                continue
            info = vmlist.get(int(vmid))
            if info is not None and info.get("node") != self.pve.node:
                log.info("Invité %s migré vers %s : suivi transféré", vmid, info.get("node"))
                del self.state.guests[vmid]
                continue
            entry = self.state.guests[vmid]
            entry["missing"] = entry.get("missing", 0) + 1
            if entry["missing"] < MISSING_PASSES_BEFORE_DESTROY:
                continue
            if self.cfg.getbool("daemon", "remove_on_destroy"):
                self.release_guest(vmid)
            else:
                log.info("Invité %s supprimé (bail conservé, remove_on_destroy=false)", vmid)
            del self.state.guests[vmid]

        if not self.dry_run:
            self.state.save()

    def run_forever(self):
        interval = self.cfg.getint("daemon", "interval")
        log.info("freeprox démarré sur le nœud %s (ponts : %s, intervalle %ds)",
                 self.pve.node, ", ".join(sorted(self.bridges)) or "tous", interval)
        while True:
            try:
                self.run_once()
            except Exception as exc:
                log.error("Erreur pendant la passe : %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            time.sleep(interval)


def _in_network(ip, network):
    try:
        return ipaddress.IPv4Address(ip) in network
    except ValueError:
        return False
