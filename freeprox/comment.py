"""Build the comment attached to the static lease."""

import re
import time

from .proxmox import disk_size_gib, parse_props

# Longest comment the Freebox accepts, in UTF-8 bytes (measured on Freebox OS:
# 64 bytes are refused with error_code=internal).
FREEBOX_COMMENT_MAX_BYTES = 63

OS_LABELS = {
    "l26": "Linux", "l24": "Linux 2.4", "other": "Other", "solaris": "Solaris",
    "wxp": "Windows XP", "w2k": "Windows 2000", "w2k3": "Windows 2003", "w2k8": "Windows 2008",
    "wvista": "Windows Vista", "win7": "Windows 7", "win8": "Windows 8/2012",
    "win10": "Windows 10/2016/2019", "win11": "Windows 11/2022/2025",
}


class _SafeDict(dict):
    def __missing__(self, key):
        return "?"


def _human_mib(mib):
    if mib >= 1024 and mib % 1024 == 0:
        return "%dGB" % (mib // 1024)
    if mib >= 1024:
        return "%.1fGB" % (mib / 1024)
    return "%dMB" % mib


def _human_gib(gib):
    if gib <= 0:
        return "?"
    if gib >= 1024:
        return "%.1fTB" % (gib / 1024)
    return ("%dGB" % gib) if gib == int(gib) else ("%.1fGB" % gib)


def _creation_time(guest):
    # Recent QEMU VMs store their creation date in "meta: …,ctime=<epoch>".
    ctime = parse_props(guest.config.get("meta", "")).get("ctime")
    try:
        return float(ctime)
    except (TypeError, ValueError):
        return time.time()


def guest_context(guest, nic, ip="", hidden_tags=()):
    cfg = guest.config
    cores = int(cfg.get("cores", "1") or 1)
    if guest.type == "qemu":
        cores *= int(cfg.get("sockets", "1") or 1)
    memory = int(cfg.get("memory", "2048" if guest.type == "qemu" else "512") or 0)
    ostype = cfg.get("ostype", "")
    hidden = {t.lower() for t in hidden_tags}
    return _SafeDict(
        type=guest.type,
        type_label="VM" if guest.type == "qemu" else "LXC",
        vmid=guest.vmid,
        name=guest.name,
        node=guest.node,
        iface=nic.iface,
        mac=nic.mac,
        bridge=nic.bridge or "",
        ip=ip,
        cores=cores,
        memory=_human_mib(memory),
        memory_mib=memory,
        disk=_human_gib(disk_size_gib(guest.type, cfg)),
        ostype=ostype or "?",
        os=OS_LABELS.get(ostype, ostype.capitalize() or "?"),
        tags=",".join(t for t in guest.tags if t.lower() not in hidden) or "-",
        date=time.strftime("%Y-%m-%d", time.localtime(_creation_time(guest))),
    )


def render_comment(template, guest, nic, ip="", max_length=FREEBOX_COMMENT_MAX_BYTES, hidden_tags=()):
    """Render the template, truncated to max_length UTF-8 bytes (never above the Freebox limit)."""
    text = template.format_map(guest_context(guest, nic, ip, hidden_tags))
    text = re.sub(r"\s+", " ", text).strip()
    limit = min(max_length or FREEBOX_COMMENT_MAX_BYTES, FREEBOX_COMMENT_MAX_BYTES)
    if len(text.encode("utf-8")) > limit:
        cut = text.encode("utf-8")[: limit - 3].decode("utf-8", "ignore")
        text = cut.rstrip() + "..."
    return text
