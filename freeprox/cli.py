"""Command line interface: freeprox <command>."""

import argparse
import logging
import sys
import time

from . import APP_ID, APP_NAME, __version__
from .config import Config
from .freebox import FreeboxClient, FreeboxError
from .proxmox import ProxmoxHost
from .service import Freeprox, State

log = logging.getLogger("freeprox")


def build(cfg, with_token=True):
    fbx = FreeboxClient(
        cfg.get("freebox", "url"), APP_ID,
        app_token=cfg.read_token() if with_token else None,
        verify_tls=cfg.getbool("freebox", "verify_tls"),
        ca_file=cfg.get("freebox", "ca_file") or None,
        timeout=cfg.getint("freebox", "timeout"),
    )
    pve = ProxmoxHost(cfg.get("proxmox", "pve_root"), cfg.get("proxmox", "node") or None)
    return fbx, pve


def make_service(cfg, args):
    fbx, pve = build(cfg)
    state = State(cfg.get("daemon", "state_file"))
    return Freeprox(cfg, fbx, pve, state, dry_run=getattr(args, "dry_run", False))


# -- commands ----------------------------------------------------------------

def cmd_register(cfg, args):
    fbx, pve = build(cfg, with_token=False)
    info = fbx.discover()
    print("Freebox found: %s (API %s)" % (info.get("device_name", "?"), info.get("api_version")))
    token, track_id = fbx.request_authorization(APP_NAME, __version__, "Proxmox %s" % pve.node)
    print(">>> Approve the request on the Freebox LCD display (right arrow / OK button)...")
    status = "pending"
    deadline = time.time() + 180
    while status == "pending" and time.time() < deadline:
        time.sleep(2)
        status = fbx.authorization_status(track_id)
    if status != "granted":
        print("Authorization not granted (status: %s)." % status, file=sys.stderr)
        return 1
    path = cfg.write_token(token)
    print("Authorization granted, token saved to %s" % path)
    print()
    print("IMPORTANT: in Freebox OS > Freebox Settings > Access Management > Applications,")
    print("edit \"%s\" and tick \"Modification des réglages de la Freebox\" (settings)." % APP_NAME)
    print("Then check with: freeprox check")
    return 0


def cmd_check(cfg, args):
    fbx, pve = build(cfg)
    info = fbx.discover()
    print("Freebox       : %s (API %s)" % (info.get("device_name", "?"), info.get("api_version")))
    perms = fbx.login()
    ok = bool(perms.get("settings"))
    print("Session       : OK")
    print("Permission    : settings = %s" % ("yes" if ok else "NO (enable it in Freebox OS)"))
    dhcp = fbx.dhcp_config()
    print("DHCP          : %s, gateway %s/%s, range %s - %s" % (
        "enabled" if dhcp.get("enabled") else "DISABLED", dhcp.get("gateway"), dhcp.get("netmask"),
        dhcp.get("ip_range_start"), dhcp.get("ip_range_end")))
    print("Static leases : %d" % len(fbx.static_leases()))
    print("Proxmox node  : %s (watched bridges: %s)" % (pve.node, cfg.get("proxmox", "bridges") or "all"))
    fbx.logout()
    return 0 if ok else 2


def cmd_daemon(cfg, args):
    make_service(cfg, args).run_forever()
    return 0


def cmd_sync(cfg, args):
    svc = make_service(cfg, args)
    if args.all or args.vmid:
        guests = svc.pve.local_guests()
        svc.state.initialized = True
        for guest in guests:
            if args.vmid and guest.vmid not in args.vmid:
                continue
            svc.process_guest(guest, force=True)
        if not svc.dry_run:
            svc.state.save()
    else:
        svc.run_once()
    svc.fbx.logout()
    return 0


def cmd_leases(cfg, args):
    fbx, _ = build(cfg)
    leases = sorted(fbx.static_leases(), key=lambda l: tuple(int(x) for x in l["ip"].split(".")))
    for lease in leases:
        if args.mine and not lease.get("comment", "").startswith("[PVE]"):
            continue
        print("%-15s  %s  %s" % (lease["ip"], lease["mac"], lease.get("comment", "")))
    fbx.logout()
    return 0


def cmd_release(cfg, args):
    svc = make_service(cfg, args)
    for vmid in args.vmid:
        count = svc.release_guest(vmid)
        print("Guest %d: %d lease(s) deleted" % (vmid, count))
        if not svc.dry_run:
            svc.state.guests.pop(str(vmid), None)
    if not svc.dry_run:
        svc.state.save()
    svc.fbx.logout()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="freeprox", description="Reserve a Freebox IP for every Proxmox VM/LXC.")
    parser.add_argument("-c", "--config", help="configuration file (default: /etc/freeprox/freeprox.conf)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("register", help="pair freeprox with the Freebox (approve on the box display)")
    sub.add_parser("check", help="check connection and permissions")
    p = sub.add_parser("daemon", help="continuously watch for VM/LXC creation")
    p.add_argument("-n", "--dry-run", action="store_true", help="do not change anything")
    p = sub.add_parser("sync", help="run a single pass")
    p.add_argument("-a", "--all", action="store_true", help="also process existing guests")
    p.add_argument("--vmid", type=int, action="append", help="only process this guest (repeatable)")
    p.add_argument("-n", "--dry-run", action="store_true", help="do not change anything")
    p = sub.add_parser("leases", help="list the Freebox static leases")
    p.add_argument("-m", "--mine", action="store_true", help="only those created by freeprox")
    p = sub.add_parser("release", help="delete the leases created for a guest")
    p.add_argument("vmid", type=int, nargs="+")
    p.add_argument("-n", "--dry-run", action="store_true", help="do not change anything")

    args = parser.parse_args(argv)
    cfg = Config(args.config)
    level = "DEBUG" if args.verbose else cfg.get("daemon", "log_level").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(levelname)s %(message)s")

    handlers = {
        "register": cmd_register, "check": cmd_check, "daemon": cmd_daemon,
        "sync": cmd_sync, "leases": cmd_leases, "release": cmd_release,
    }
    try:
        return handlers[args.command](cfg, args)
    except FreeboxError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
