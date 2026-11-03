"""Configuration loading (/etc/freeprox/freeprox.conf)."""

import configparser
import os

DEFAULT_PATH = "/etc/freeprox/freeprox.conf"

# The Freebox rejects static lease comments longer than 63 bytes (UTF-8), see comment.py.
DEFAULT_TEMPLATE = "[PVE] {type_label} {vmid} {name} | {node} | {cores}vCPU {memory} {disk}"

DEFAULTS = {
    "freebox": {
        "url": "http://mafreebox.freebox.fr",
        "token_file": "/etc/freeprox/app_token",
        "verify_tls": "true",
        "ca_file": "",
        "timeout": "10",
    },
    "proxmox": {
        "pve_root": "/etc/pve",
        "node": "",
        "bridges": "vmbr0",
        "tag": "freeprox",
        "skip_templates": "true",
        "process_existing": "false",
    },
    "dhcp": {
        "pool_start": "",
        "pool_end": "",
        "exclude": "",
        "prefer_current_ip": "true",
        "use_guest_static_ip": "true",
        "ping_check": "true",
    },
    "comment": {
        "template": DEFAULT_TEMPLATE,
        "max_length": "63",
        "update_existing": "true",
    },
    "daemon": {
        "interval": "10",
        "settle_seconds": "5",
        "remove_on_destroy": "false",
        "state_file": "/var/lib/freeprox/state.json",
        "dry_run": "false",
        "log_level": "INFO",
    },
}


class Config:
    def __init__(self, path=None):
        self.path = path or os.environ.get("FREEPROX_CONFIG", DEFAULT_PATH)
        # interpolation=None: the comment template contains braces and may contain %.
        self._cp = configparser.ConfigParser(interpolation=None)
        self._cp.read_dict(DEFAULTS)
        self._cp.read(self.path)

    def get(self, section, key):
        return self._cp.get(section, key).strip()

    def getbool(self, section, key):
        return self._cp.getboolean(section, key)

    def getint(self, section, key):
        return self._cp.getint(section, key)

    def getlist(self, section, key):
        raw = self.get(section, key).replace(",", " ")
        return [item for item in raw.split() if item]

    def read_token(self):
        path = self.get("freebox", "token_file")
        try:
            with open(path) as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    def write_token(self, token):
        path = self.get("freebox", "token_file")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
        return path
