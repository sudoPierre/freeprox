"""Interface en ligne de commande : freeprox <commande>."""

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


# -- commandes ---------------------------------------------------------------

def cmd_register(cfg, args):
    fbx, pve = build(cfg, with_token=False)
    info = fbx.discover()
    print("Freebox détectée : %s (API %s)" % (info.get("device_name", "?"), info.get("api_version")))
    token, track_id = fbx.request_authorization(APP_NAME, __version__, "Proxmox %s" % pve.node)
    print(">>> Validez la demande sur l'écran LCD de la Freebox (flèche droite / bouton OK)…")
    status = "pending"
    deadline = time.time() + 180
    while status == "pending" and time.time() < deadline:
        time.sleep(2)
        status = fbx.authorization_status(track_id)
    if status != "granted":
        print("Autorisation non accordée (statut : %s)." % status, file=sys.stderr)
        return 1
    path = cfg.write_token(token)
    print("Autorisation accordée, jeton enregistré dans %s" % path)
    print()
    print("IMPORTANT : dans Freebox OS > Paramètres de la Freebox > Gestion des accès > Applications,")
    print("éditez « %s » et cochez « Modification des réglages de la Freebox »." % APP_NAME)
    print("Vérifiez ensuite avec : freeprox check")
    return 0


def cmd_check(cfg, args):
    fbx, pve = build(cfg)
    info = fbx.discover()
    print("Freebox       : %s (API %s)" % (info.get("device_name", "?"), info.get("api_version")))
    perms = fbx.login()
    ok = bool(perms.get("settings"))
    print("Session       : OK")
    print("Permission    : réglages = %s" % ("oui" if ok else "NON (à activer dans Freebox OS)"))
    dhcp = fbx.dhcp_config()
    print("DHCP          : %s, passerelle %s/%s, plage %s - %s" % (
        "actif" if dhcp.get("enabled") else "INACTIF", dhcp.get("gateway"), dhcp.get("netmask"),
        dhcp.get("ip_range_start"), dhcp.get("ip_range_end")))
    print("Baux statiques: %d" % len(fbx.static_leases()))
    print("Nœud Proxmox  : %s (ponts surveillés : %s)" % (pve.node, cfg.get("proxmox", "bridges") or "tous"))
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
        print("Invité %d : %d bail(s) supprimé(s)" % (vmid, count))
        if not svc.dry_run:
            svc.state.guests.pop(str(vmid), None)
    if not svc.dry_run:
        svc.state.save()
    svc.fbx.logout()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="freeprox", description="Réserve une IP Freebox pour chaque VM/LXC Proxmox.")
    parser.add_argument("-c", "--config", help="fichier de configuration (défaut : /etc/freeprox/freeprox.conf)")
    parser.add_argument("-v", "--verbose", action="store_true", help="logs détaillés")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("register", help="associer freeprox à la Freebox (validation sur l'écran de la box)")
    sub.add_parser("check", help="vérifier la connexion et les permissions")
    p = sub.add_parser("daemon", help="surveiller en continu les créations de VM/LXC")
    p.add_argument("-n", "--dry-run", action="store_true", help="n'effectue aucune modification")
    p = sub.add_parser("sync", help="effectuer une seule passe")
    p.add_argument("-a", "--all", action="store_true", help="traiter aussi les invités existants")
    p.add_argument("--vmid", type=int, action="append", help="ne traiter que cet invité (répétable)")
    p.add_argument("-n", "--dry-run", action="store_true", help="n'effectue aucune modification")
    p = sub.add_parser("leases", help="lister les baux statiques de la Freebox")
    p.add_argument("-m", "--mine", action="store_true", help="seulement ceux créés par freeprox")
    p = sub.add_parser("release", help="supprimer les baux créés pour un invité")
    p.add_argument("vmid", type=int, nargs="+")
    p.add_argument("-n", "--dry-run", action="store_true", help="n'effectue aucune modification")

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
