"""End-to-end scenarios against a fake Freebox server and a fake /etc/pve."""

import os
import shutil
import tempfile
import unittest

from freeprox import APP_ID
from freeprox.config import Config
from freeprox.freebox import FreeboxClient
from freeprox.proxmox import ProxmoxHost
from freeprox.service import MISSING_PASSES_BEFORE_DESTROY, Freeprox, State

from .fakes import APP_TOKEN, FakeFreebox, FakePve

VM = "name: web01\ncores: 2\nmemory: 2048\nostype: l26\nscsi0: local-lvm:vm-{id}-disk-0,size=20G\nnet0: virtio={mac},bridge=vmbr0\n"
CT = "hostname: ct{id}\ncores: 1\nmemory: 512\nostype: debian\nrootfs: local-lvm:vm-{id}-disk-0,size=8G\nnet0: name=eth0,bridge=vmbr0,hwaddr={mac},ip={ip},type=veth\n"


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fbx = FakeFreebox()
        self.pve = FakePve(os.path.join(self.tmp, "pve"))
        self.conf = os.path.join(self.tmp, "freeprox.conf")
        with open(os.path.join(self.tmp, "token"), "w") as fh:
            fh.write(APP_TOKEN)
        self.write_conf()

    def tearDown(self):
        self.fbx.close()
        shutil.rmtree(self.tmp)

    def write_conf(self, **extra):
        sections = {
            "freebox": {"url": self.fbx.url, "token_file": os.path.join(self.tmp, "token")},
            "proxmox": {"pve_root": self.pve.root},
            "dhcp": {"ping_check": "false"},
            "daemon": {"state_file": os.path.join(self.tmp, "state.json"), "settle_seconds": "0"},
        }
        for key, value in extra.items():
            section, option = key.split("__")
            sections.setdefault(section, {})[option] = value
        lines = []
        for section, options in sections.items():
            lines.append("[%s]" % section)
            lines += ["%s = %s" % item for item in options.items()]
        with open(self.conf, "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def service(self):
        cfg = Config(self.conf)
        client = FreeboxClient(cfg.get("freebox", "url"), APP_ID, cfg.read_token())
        host = ProxmoxHost(cfg.get("proxmox", "pve_root"))
        return Freeprox(cfg, client, host, State(cfg.get("daemon", "state_file")), pinger=lambda ip: False)

    def test_existing_guests_ignored_new_ones_reserved(self):
        self.pve.add(100, "qemu", VM.format(id=100, mac="BC:24:11:00:01:00"))
        self.service().run_once()
        self.assertEqual(self.fbx.static, {})

        self.pve.add(101, "qemu", VM.format(id=101, mac="bc:24:11:00:01:01"))
        self.pve.add(200, "lxc", CT.format(id=200, mac="BC:24:11:00:02:00", ip="192.168.1.77/24"))
        self.service().run_once()
        self.assertEqual(sorted(self.fbx.static), ["BC:24:11:00:01:01", "BC:24:11:00:02:00"])
        vm = self.fbx.static["BC:24:11:00:01:01"]
        self.assertEqual(vm["ip"], "192.168.1.10")
        self.assertTrue(vm["comment"].startswith("[PVE] VM 101 web01 | pve/net0 | 2 vCPU, 2 GB, 20 GB | Linux |"))
        self.assertEqual(self.fbx.static["BC:24:11:00:02:00"]["ip"], "192.168.1.77")  # CT static IP reused

        # A second pass creates nothing new.
        self.service().run_once()
        self.assertEqual(len(self.fbx.static), 2)

    def test_prefers_current_dynamic_ip_and_avoids_used(self):
        self.service().run_once()  # initialization
        self.fbx.dynamic = [{"mac": "BC:24:11:00:01:02", "ip": "192.168.1.33"},
                            {"mac": "AA:AA:AA:AA:AA:AA", "ip": "192.168.1.10"}]
        self.pve.add(102, "qemu", VM.format(id=102, mac="BC:24:11:00:01:02"))
        self.pve.add(103, "qemu", VM.format(id=103, mac="BC:24:11:00:01:03"))
        self.service().run_once()
        self.assertEqual(self.fbx.static["BC:24:11:00:01:02"]["ip"], "192.168.1.33")
        self.assertEqual(self.fbx.static["BC:24:11:00:01:03"]["ip"], "192.168.1.11")

    def test_other_bridges_templates_and_locks_skipped(self):
        self.service().run_once()
        self.pve.add(104, "qemu", VM.format(id=104, mac="BC:24:11:00:01:04").replace("vmbr0", "vmbr9"))
        self.pve.add(105, "qemu", VM.format(id=105, mac="BC:24:11:00:01:05") + "template: 1\n")
        self.pve.add(106, "lxc", CT.format(id=106, mac="BC:24:11:00:01:06", ip="dhcp") + "lock: create\n")
        self.service().run_once()
        self.assertEqual(self.fbx.static, {})

        # Lock released: the container is processed on the next pass.
        self.pve.add(106, "lxc", CT.format(id=106, mac="BC:24:11:00:01:06", ip="dhcp"))
        self.service().run_once()
        self.assertIn("BC:24:11:00:01:06", self.fbx.static)

    def test_remove_on_destroy(self):
        self.write_conf(daemon__remove_on_destroy="true")
        self.service().run_once()
        self.fbx.static["CC:CC:CC:CC:CC:CC"] = {"id": "CC:CC:CC:CC:CC:CC", "mac": "CC:CC:CC:CC:CC:CC", "ip": "192.168.1.5"}
        self.pve.add(107, "qemu", VM.format(id=107, mac="BC:24:11:00:01:07"))
        self.service().run_once()
        self.assertIn("BC:24:11:00:01:07", self.fbx.static)

        self.pve.remove(107)
        for _ in range(MISSING_PASSES_BEFORE_DESTROY):
            self.service().run_once()
        self.assertEqual(list(self.fbx.static), ["CC:CC:CC:CC:CC:CC"])

    def test_existing_lease_comment_updated_not_duplicated(self):
        self.service().run_once()
        self.fbx.static["BC:24:11:00:01:08"] = {"id": "BC:24:11:00:01:08", "mac": "BC:24:11:00:01:08",
                                                "ip": "192.168.1.20", "comment": "old"}
        self.pve.add(108, "qemu", VM.format(id=108, mac="BC:24:11:00:01:08"))
        self.service().run_once()
        self.assertEqual(self.fbx.static["BC:24:11:00:01:08"]["ip"], "192.168.1.20")
        self.assertTrue(self.fbx.static["BC:24:11:00:01:08"]["comment"].startswith("[PVE] VM 108"))

    def test_failure_is_retried(self):
        self.service().run_once()
        self.fbx.settings_permission = False
        self.pve.add(109, "qemu", VM.format(id=109, mac="BC:24:11:00:01:09"))
        with self.assertLogs("freeprox.service", "ERROR"):
            self.service().run_once()
        self.assertEqual(self.fbx.static, {})
        self.fbx.settings_permission = True
        self.service().run_once()
        self.assertIn("BC:24:11:00:01:09", self.fbx.static)


if __name__ == "__main__":
    unittest.main()
