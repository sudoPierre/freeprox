import unittest

from freeprox.allocator import AllocationError, parse_excludes, pick_free_ip
from freeprox.comment import render_comment
from freeprox.config import DEFAULT_TEMPLATE
from freeprox.proxmox import Guest, disk_size_gib, parse_config, parse_nics

QEMU_CONF = """\
#Ma%20VM
boot: order=scsi0;net0
cores: 2
sockets: 2
memory: 4096
meta: creation-qemu=9.0.2,ctime=1730000000
name: web01
net0: virtio=bc:24:11:aa:bb:cc,bridge=vmbr0,firewall=1
net1: e1000=BC:24:11:AA:BB:DD,bridge=vmbr1
ipconfig0: ip=192.168.1.42/24,gw=192.168.1.254
ostype: l26
scsi0: local-lvm:vm-105-disk-0,iothread=1,size=32G
ide2: local:iso/debian.iso,media=cdrom,size=600M
tags: prod;web

[snap1]
name: old-name
net0: virtio=11:11:11:11:11:11,bridge=vmbr0
"""

LXC_CONF = """\
arch: amd64
cores: 1
hostname: dns01
memory: 512
net0: name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:01,ip=dhcp,type=veth
net1: name=eth1,bridge=vmbr0,hwaddr=BC:24:11:00:00:02,ip=192.168.1.30/24,type=veth
ostype: debian
rootfs: local-lvm:vm-200-disk-0,size=8G
mp0: local-lvm:vm-200-disk-1,mp=/data,size=512M
"""

DHCP = {"gateway": "192.168.1.254", "netmask": "255.255.255.0",
        "ip_range_start": "192.168.1.10", "ip_range_end": "192.168.1.15"}


class ProxmoxParsing(unittest.TestCase):
    def test_qemu(self):
        cfg = parse_config(QEMU_CONF)
        self.assertEqual(cfg["name"], "web01")  # la section snapshot est ignorée
        nics = parse_nics("qemu", cfg)
        self.assertEqual([(n.iface, n.mac, n.bridge, n.static_ip) for n in nics], [
            ("net0", "BC:24:11:AA:BB:CC", "vmbr0", "192.168.1.42"),
            ("net1", "BC:24:11:AA:BB:DD", "vmbr1", None),
        ])
        self.assertEqual(disk_size_gib("qemu", cfg), 32)

    def test_lxc(self):
        cfg = parse_config(LXC_CONF)
        nics = parse_nics("lxc", cfg)
        self.assertEqual([(n.mac, n.static_ip) for n in nics],
                         [("BC:24:11:00:00:01", None), ("BC:24:11:00:00:02", "192.168.1.30")])
        self.assertEqual(disk_size_gib("lxc", cfg), 8.5)


class Comment(unittest.TestCase):
    def test_default_template(self):
        cfg = parse_config(QEMU_CONF)
        guest = Guest(105, "qemu", "pve", cfg, nics=parse_nics("qemu", cfg))
        text = render_comment(DEFAULT_TEMPLATE, guest, guest.nics[0], "192.168.1.42")
        self.assertTrue(text.startswith("[PVE] VM 105 web01 | pve/net0 | 4 vCPU, 4 Go, 32 Go | Linux | 2024-10-"))

    def test_unknown_key_and_truncation(self):
        cfg = parse_config(LXC_CONF)
        guest = Guest(200, "lxc", "pve", cfg, nics=parse_nics("lxc", cfg))
        self.assertEqual(render_comment("{name} {nope}", guest, guest.nics[0]), "dns01 ?")
        self.assertEqual(render_comment("x" * 50, guest, guest.nics[0], max_length=10), "xxxxxxx...")


class Allocator(unittest.TestCase):
    def test_skips_used_and_excluded(self):
        ip = pick_free_ip(DHCP, {"192.168.1.10", "192.168.1.11"}, excluded=parse_excludes(["192.168.1.12-192.168.1.13"]))
        self.assertEqual(ip, "192.168.1.14")

    def test_ping_check(self):
        ip = pick_free_ip(DHCP, set(), ping_check=True, pinger=lambda ip: str(ip) == "192.168.1.10")
        self.assertEqual(ip, "192.168.1.11")

    def test_custom_pool_and_exhaustion(self):
        self.assertEqual(pick_free_ip(DHCP, set(), "192.168.1.200", "192.168.1.201"), "192.168.1.200")
        with self.assertRaises(AllocationError):
            pick_free_ip(DHCP, {"192.168.1.200", "192.168.1.201"}, "192.168.1.200", "192.168.1.201")
        with self.assertRaises(AllocationError):
            pick_free_ip(DHCP, set(), "10.0.0.1", "10.0.0.5")


if __name__ == "__main__":
    unittest.main()
