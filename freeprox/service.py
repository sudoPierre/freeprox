"""Core logic: detect new guests and reserve an IP for them on the Freebox."""

import ipaddress
import json
import logging
import os
import tempfile
import time

from .allocator import lan_network, parse_excludes, pick_free_ip, answers_ping
from .comment import render_comment
from .proxmox import TAG_RE

log = logging.getLogger(__name__)

# A guest must be missing from .vmlist for this many passes before it is considered
# destroyed (guards against a transient incomplete read of pmxcfs).
MISSING_PASSES_BEFORE_DESTROY = 3


class State:
    """Remembers already processed NICs: {vmid: {type, node, nics: {mac: {...}}}}."""

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
        self.tag = config.get("proxmox", "tag")
        if self.tag and not TAG_RE.match(self.tag):
            log.error("Invalid Proxmox tag %r (allowed: letters, digits, _ + . -): tagging disabled", self.tag)
            self.tag = ""

    # -- selection -------------------------------------------------------

    def eligible_nics(self, guest):
        return [nic for nic in guest.nics if not self.bridges or nic.bridge in self.bridges]

    def _ready(self, guest):
        if guest.lock:
            log.debug("%s %d locked (%s), waiting", guest.type, guest.vmid, guest.lock)
            return False
        if time.time() - guest.mtime < self.cfg.getint("daemon", "settle_seconds"):
            return False
        return True

    # -- DHCP lease -----------------------------------------------------

    def ensure_lease(self, guest, nic):
        """Create (or update) the NIC's static lease. Returns (ip, created_by_us)."""
        statics = self.fbx.static_leases()
        existing = next((l for l in statics if l.get("mac", "").upper() == nic.mac), None)

        if existing:
            ip = existing["ip"]
            comment = self._comment(guest, nic, ip)
            if self.cfg.getbool("comment", "update_existing") and existing.get("comment") != comment:
                log.info("%s %d/%s: existing lease %s -> %s, updating comment",
                         guest.type, guest.vmid, nic.iface, nic.mac, ip)
                if not self.dry_run:
                    self.fbx.update_static_lease(existing["id"], comment=comment)
            else:
                log.info("%s %d/%s: existing lease %s -> %s", guest.type, guest.vmid, nic.iface, nic.mac, ip)
            return ip, False

        ip = self._choose_ip(nic, statics)
        comment = self._comment(guest, nic, ip)
        log.info("%s %d/%s: reserving %s -> %s \"%s\"%s", guest.type, guest.vmid, nic.iface,
                 nic.mac, ip, comment, " (dry-run)" if self.dry_run else "")
        if not self.dry_run:
            self.fbx.add_static_lease(nic.mac, ip, comment)
        return ip, True

    def _comment(self, guest, nic, ip):
        return render_comment(self.cfg.get("comment", "template"), guest, nic, ip,
                              self.cfg.getint("comment", "max_length"), hidden_tags=[self.tag] if self.tag else ())

    def _current_ips(self, nic, dynamic):
        """Dynamic lease first, then the Freebox network browser (still knows the guest's IP
        when the dynamic lease list was emptied, e.g. after a static lease was deleted)."""
        for lease in dynamic:
            if lease.get("mac", "").upper() == nic.mac:
                yield lease["ip"]
        try:
            host = self.fbx.lan_host(nic.mac)
        except Exception as exc:
            log.debug("LAN browser lookup failed for %s: %s", nic.mac, exc)
            return
        for conn in (host or {}).get("l3connectivities", []):
            if conn.get("af") == "ipv4" and conn.get("active") and conn.get("reachable"):
                yield conn["addr"]

    def _choose_ip(self, nic, statics):
        dhcp = self.fbx.dhcp_config()
        network = lan_network(dhcp)
        reserved = {l["ip"] for l in statics}
        dynamic = self.fbx.dynamic_leases()

        # 1. Static IP already configured inside the guest (LXC ip=… / cloud-init).
        if self.cfg.getbool("dhcp", "use_guest_static_ip") and nic.static_ip:
            if _in_network(nic.static_ip, network) and nic.static_ip not in reserved:
                return nic.static_ip
            log.warning("Static IP %s of %s unusable (outside LAN %s or already reserved)", nic.static_ip, nic.mac, network)

        # 2. IP currently used by this MAC: pin it.
        if self.cfg.getbool("dhcp", "prefer_current_ip"):
            for current in self._current_ips(nic, dynamic):
                if current not in reserved and _in_network(current, network):
                    return current

        # 3. First free IP in the pool.
        used = reserved | {l["ip"] for l in dynamic if l.get("mac", "").upper() != nic.mac}
        return pick_free_ip(
            dhcp, used,
            pool_start=self.cfg.get("dhcp", "pool_start") or None,
            pool_end=self.cfg.get("dhcp", "pool_end") or None,
            excluded=parse_excludes(self.cfg.getlist("dhcp", "exclude")),
            ping_check=self.cfg.getbool("dhcp", "ping_check"),
            pinger=self.pinger,
        )

    # -- guest processing -----------------------------------------------

    def process_guest(self, guest, force=False):
        if guest.is_template and self.cfg.getbool("proxmox", "skip_templates"):
            return
        entry = self.state.guest(guest.vmid)
        entry.update(type=guest.type, node=guest.node, missing=0)
        pinned = False
        for nic in self.eligible_nics(guest):
            if nic.mac in entry["nics"] and not force:
                continue
            try:
                ip, created = self.ensure_lease(guest, nic)
            except Exception as exc:  # retried on the next pass
                log.error("%s %d/%s: reservation failed: %s", guest.type, guest.vmid, nic.iface, exc)
                continue
            pinned = True
            if self.dry_run:
                continue
            previous = entry["nics"].get(nic.mac, {})
            entry["nics"][nic.mac] = {
                "iface": nic.iface,
                "ip": ip,
                "created": created or previous.get("created", False),
            }
        if pinned:
            self.tag_guest(guest)

    # -- guest tag -------------------------------------------------------

    def tag_guest(self, guest):
        """Flag the guest with the freeprox tag so the UI shows its IP is pinned."""
        if not self.tag or guest.has_tag(self.tag):
            return
        log.info("%s %d: adding tag '%s'%s", guest.type, guest.vmid, self.tag, " (dry-run)" if self.dry_run else "")
        if self.dry_run:
            return
        try:
            self.pve.set_tags(guest, guest.tags + [self.tag])
        except Exception as exc:
            log.error("%s %d: failed to add tag '%s': %s", guest.type, guest.vmid, self.tag, exc)

    def untag_guest(self, guest):
        if not self.tag or not guest.has_tag(self.tag):
            return
        log.info("%s %d: removing tag '%s'%s", guest.type, guest.vmid, self.tag, " (dry-run)" if self.dry_run else "")
        if self.dry_run:
            return
        try:
            self.pve.set_tags(guest, [t for t in guest.tags if t.lower() != self.tag.lower()])
        except Exception as exc:
            log.error("%s %d: failed to remove tag '%s': %s", guest.type, guest.vmid, self.tag, exc)

    def release_guest(self, vmid):
        """Delete the leases freeprox created for this guest."""
        entry = self.state.guests.get(str(vmid))
        if not entry:
            return 0
        removed = 0
        for mac, info in entry.get("nics", {}).items():
            if not info.get("created"):
                continue
            log.info("Guest %s: releasing lease %s (%s)%s", vmid, mac, info.get("ip"),
                     " (dry-run)" if self.dry_run else "")
            if self.dry_run:
                continue
            try:
                self.fbx.delete_static_lease(mac)
                removed += 1
            except Exception as exc:
                log.error("Failed to delete lease %s: %s", mac, exc)
        return removed

    # -- passes ----------------------------------------------------------

    def initialize(self, guests):
        """First start: guests that already exist are marked as known without being processed."""
        if self.state.initialized:
            return
        if not self.cfg.getbool("proxmox", "process_existing"):
            for guest in guests:
                entry = self.state.guest(guest.vmid)
                entry.update(type=guest.type, node=guest.node, missing=0)
                for nic in self.eligible_nics(guest):
                    entry["nics"].setdefault(nic.mac, {"iface": nic.iface, "ip": None, "created": False})
            log.info("Initialization: %d existing guest(s) skipped (see 'freeprox sync --all')", len(guests))
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
                log.info("Guest %s migrated to %s: tracking handed over", vmid, info.get("node"))
                del self.state.guests[vmid]
                continue
            entry = self.state.guests[vmid]
            entry["missing"] = entry.get("missing", 0) + 1
            if entry["missing"] < MISSING_PASSES_BEFORE_DESTROY:
                continue
            if self.cfg.getbool("daemon", "remove_on_destroy"):
                self.release_guest(vmid)
            else:
                log.info("Guest %s destroyed (lease kept, remove_on_destroy=false)", vmid)
            del self.state.guests[vmid]

        if not self.dry_run:
            self.state.save()

    def run_forever(self):
        interval = self.cfg.getint("daemon", "interval")
        log.info("freeprox started on node %s (bridges: %s, interval %ds)",
                 self.pve.node, ", ".join(sorted(self.bridges)) or "all", interval)
        while True:
            try:
                self.run_once()
            except Exception as exc:
                log.error("Error during pass: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            time.sleep(interval)


def _in_network(ip, network):
    try:
        return ipaddress.IPv4Address(ip) in network
    except ValueError:
        return False
