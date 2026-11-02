# freeprox

A service for **Proxmox VE** that, as soon as a **VM** or an **LXC container** is created,
automatically reserves its IP address on the **Freebox** (static DHCP lease through the
Freebox OS API), attaches a descriptive comment to it, and flags the guest with a
`freeprox` **tag** in Proxmox. Lease comments look like this:

```
192.168.1.42   BC:24:11:5E:0A:31   [PVE] VM 105 web01 | pve/net0 | 4 vCPU, 8 GB, 32 GB | Linux | 2026-11-02
192.168.1.43   BC:24:11:9C:44:02   [PVE] LXC 210 dns01 | pve/net0 | 1 vCPU, 512 MB, 8 GB | Debian | 2026-11-02
```

No dependencies: Python 3 standard library only, already available on Proxmox.

## How it works

Proxmox does not expose a "guest created" event (hookscripts only cover starting/stopping
an already configured guest). freeprox therefore runs as a systemd service that polls
`/etc/pve/.vmlist` and the node's guest configuration files (every 10 s by default):

1. **New guest or new NIC** on a watched bridge (`vmbr0` by default):
   - waits for the creation to finish (no `lock:` in the config, config no longer changing);
   - if a static lease already exists for this MAC, it is kept (comment updated);
   - otherwise the IP is chosen in this order:
     1. the static IP declared inside the guest (LXC `ip=…`, cloud-init `ipconfigN`);
     2. the IP the Freebox DHCP server already gave it;
     3. the first free IP of the pool (not reserved, not leased, not excluded, not answering ping);
   - the static lease is created with the comment;
   - the guest gets the `freeprox` tag (via `qm set` / `pct set`, existing tags are kept),
     so the Proxmox UI shows at a glance which guests have a pinned IP.
2. **`freeprox release <vmid>`**: deletes the leases freeprox created and removes the tag.
3. **Guest destroyed**: optionally (`remove_on_destroy = true`), the leases freeprox
   created itself are deleted.
4. **Guest migrated** to another cluster node: the service on that node takes over
   tracking and updates the comment (the lease is tied to the MAC and does not change).

Guests that already exist on first start are skipped (see `freeprox sync --all`).
Templates are skipped by default.

## Installation

In the **Proxmox node shell** (as root), tteck-style:

```sh
bash -c "$(curl -fsSL https://raw.githubusercontent.com/sudoPierre/freeprox/main/install.sh)"
```

The script is fully guided (whiptail dialogs):

1. checks it runs on Proxmox VE and installs missing dependencies;
2. offers **default** or **advanced** settings (Freebox address, network bridges connected
   to the LAN, IP pool, Proxmox tag, lease release on destroy);
3. installs freeprox into `/opt/freeprox` along with the systemd service;
4. **pairs freeprox with the Freebox**: approve the request on the box LCD display;
5. checks the **"Modification des réglages de la Freebox"** (settings modification)
   permission and explains how to enable it if missing (Freebox OS → *Freebox Settings* →
   *Access Management* → *Applications* → *Freeprox*);
6. offers to also reserve IPs for the VMs/LXCs that already exist;
7. starts the service.

Running the same command again on a node where freeprox is installed opens an
**Update / Reconfigure / Uninstall** menu. Without the UI:

```sh
bash /opt/freeprox/install.sh --update
bash /opt/freeprox/install.sh --uninstall [--purge]   # --purge: also remove config and token
```

On a cluster, run the script on every node (each one handles its own guests).

Follow activity: `journalctl -u freeprox -f`

## Configuration

File `/etc/freeprox/freeprox.conf` (see [`etc/freeprox.conf`](etc/freeprox.conf), where
every option is documented). The most useful ones:

| Section | Option | Default | Purpose |
|---|---|---|---|
| `[proxmox]` | `bridges` | `vmbr0` | bridges connected to the Freebox LAN |
| `[proxmox]` | `tag` | `freeprox` | tag added to guests with a pinned IP (empty = off) |
| `[dhcp]` | `pool_start` / `pool_end` | Freebox DHCP range | where static IPs are picked |
| `[dhcp]` | `exclude` | — | IPs/ranges never to assign |
| `[comment]` | `template` | see above | comment format |
| `[daemon]` | `remove_on_destroy` | `false` | release the lease on destroy |
| `[daemon]` | `dry_run` | `false` | log without changing anything |

Comment template variables: `{type}` (`qemu`/`lxc`), `{type_label}` (`VM`/`LXC`),
`{vmid}`, `{name}`, `{node}`, `{iface}`, `{mac}`, `{bridge}`, `{ip}`, `{cores}`, `{memory}`,
`{memory_mib}`, `{disk}`, `{ostype}`, `{os}`, `{tags}` (without the freeprox tag),
`{date}` (creation date).

Tip: give the tag a color in *Datacenter → Options → Tag Style Override*, and filter
on it in the resource tree search.

Tip: keep the static IP pool **outside** the Freebox dynamic DHCP range
(e.g. shrink the DHCP range to `.10-.99` and set `pool_start = 192.168.1.100`,
`pool_end = 192.168.1.199`).

## Commands

```
freeprox register              pair the application with the Freebox
freeprox check                 check connection, permissions and DHCP
freeprox daemon [-n]           service (started by systemd); -n = dry-run
freeprox sync [-n]             run a single pass
freeprox sync --all [-n]       also reserve IPs for existing guests
freeprox sync --vmid 105       (re)process a specific guest
freeprox leases [--mine]       list static leases
freeprox release 105 [...]     delete the leases created for a guest and remove its tag
```

## Notes

- The lease is reserved at creation time: the guest gets the IP on its next DHCP request
  (i.e. on first boot if it uses DHCP).
- For a guest configured with a static IP, the reservation simply ensures the Freebox
  never hands that IP out to another device.
- HTTPS access: `url = https://mafreebox.freebox.fr` with `ca_file` pointing to the
  Freebox root certificates, or `verify_tls = false`.
- Uninstalling leaves existing leases on the Freebox.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

The integration tests use a fake Freebox OS server and a fake `/etc/pve` tree
(`tests/fakes.py`).

## License

MIT
