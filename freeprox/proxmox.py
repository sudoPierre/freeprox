"""Lecture des invités (VM QEMU et conteneurs LXC) depuis le système de fichiers /etc/pve."""

import json
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
NET_KEY_RE = re.compile(r"^net(\d+)$")
QEMU_DISK_RE = re.compile(r"^(scsi|virtio|sata|ide)\d+$")
SIZE_RE = re.compile(r"(?:^|,)size=(\d+(?:\.\d+)?)([KMGT]?)", re.I)

CONF_DIRS = {"qemu": "qemu-server", "lxc": "lxc"}


@dataclass
class Nic:
    iface: str            # net0, net1…
    mac: str              # en majuscules
    bridge: Optional[str]
    static_ip: Optional[str] = None  # IP fixée côté invité (LXC ip=… ou cloud-init)


@dataclass
class Guest:
    vmid: int
    type: str             # "qemu" ou "lxc"
    node: str
    config: Dict[str, str]
    mtime: float = 0.0
    nics: List[Nic] = field(default_factory=list)

    @property
    def name(self):
        return self.config.get("name") or self.config.get("hostname") or "%s-%d" % (self.type, self.vmid)

    @property
    def is_template(self):
        return self.config.get("template") == "1"

    @property
    def lock(self):
        return self.config.get("lock")


def parse_config(text):
    """Parse un fichier .conf Proxmox (section courante uniquement, sans les snapshots)."""
    config = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            break  # début des snapshots / pending
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if sep:
            config[key.strip()] = value.strip()
    return config


def parse_props(value):
    """'virtio=AA:..,bridge=vmbr0' -> {'virtio': 'AA:..', 'bridge': 'vmbr0'}"""
    props = {}
    for part in value.split(","):
        key, sep, val = part.partition("=")
        props[key.strip()] = val.strip() if sep else ""
    return props


def _strip_cidr(ip):
    if not ip or ip in ("dhcp", "manual", "auto"):
        return None
    return ip.split("/")[0]


def parse_nics(guest_type, config):
    nics = []
    for key in sorted(config, key=lambda k: (len(k), k)):
        match = NET_KEY_RE.match(key)
        if not match:
            continue
        props = parse_props(config[key])
        mac = props.get("hwaddr") or props.get("macaddr")
        if not mac:
            # QEMU : le MAC est la valeur du modèle (virtio=, e1000=…)
            mac = next((v for k, v in props.items() if k != "bridge" and MAC_RE.match(v)), None)
        if not mac or not MAC_RE.match(mac):
            continue
        if guest_type == "lxc":
            static_ip = _strip_cidr(props.get("ip"))
        else:
            static_ip = _strip_cidr(parse_props(config.get("ipconfig" + match.group(1), "")).get("ip"))
        nics.append(Nic(iface=key, mac=mac.upper(), bridge=props.get("bridge"), static_ip=static_ip))
    return nics


def _size_gib(value):
    match = SIZE_RE.search(value)
    if not match:
        return 0.0
    num, unit = float(match.group(1)), (match.group(2) or "G").upper()
    return num * {"K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}[unit]


def disk_size_gib(guest_type, config):
    total = 0.0
    for key, value in config.items():
        if guest_type == "lxc" and (key == "rootfs" or key.startswith("mp")):
            total += _size_gib(value)
        elif guest_type == "qemu" and QEMU_DISK_RE.match(key) and "media=cdrom" not in value:
            total += _size_gib(value)
    return total


class ProxmoxHost:
    def __init__(self, pve_root="/etc/pve", node=None):
        self.root = pve_root
        self.node = node or self._detect_node()

    def _detect_node(self):
        try:
            # /etc/pve/local -> nodes/<nom-du-nœud>
            return os.path.basename(os.readlink(os.path.join(self.root, "local")))
        except OSError:
            return socket.gethostname().split(".")[0]

    def vmlist(self):
        """Retourne {vmid: {'node':…, 'type':…}} pour tout le cluster."""
        with open(os.path.join(self.root, ".vmlist")) as fh:
            data = json.load(fh)
        return {int(vmid): info for vmid, info in data.get("ids", {}).items()}

    def config_path(self, vmid, guest_type, node):
        return os.path.join(self.root, "nodes", node, CONF_DIRS[guest_type], "%d.conf" % vmid)

    def load_guest(self, vmid, guest_type, node):
        path = self.config_path(vmid, guest_type, node)
        with open(path) as fh:
            config = parse_config(fh.read())
        guest = Guest(vmid=vmid, type=guest_type, node=node, config=config, mtime=os.path.getmtime(path))
        guest.nics = parse_nics(guest_type, config)
        return guest

    def local_guests(self, vmlist=None):
        guests = []
        for vmid, info in sorted((vmlist if vmlist is not None else self.vmlist()).items()):
            if info.get("node") != self.node or info.get("type") not in CONF_DIRS:
                continue
            try:
                guests.append(self.load_guest(vmid, info["type"], info["node"]))
            except OSError:
                continue  # supprimé entre-temps
        return guests
