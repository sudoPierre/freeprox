"""Fake Freebox OS server and fake /etc/pve tree for tests."""

import hashlib
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_TOKEN = "dGVzdC10b2tlbg=="
CHALLENGE = "challenge-123"
SESSION = "session-abc"


class FakeFreebox:
    def __init__(self, settings_permission=True):
        self.static = {}
        self.dynamic = []
        self.settings_permission = settings_permission
        self.dhcp = {"enabled": True, "gateway": "192.168.1.254", "netmask": "255.255.255.0",
                     "ip_range_start": "192.168.1.10", "ip_range_end": "192.168.1.50"}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, payload, code=200):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def _route(self, method):
                path = self.path
                if path == "/api_version":
                    return self._send({"api_version": "10.2", "api_base_url": "/api/", "device_name": "Fake Freebox"})
                if not path.startswith("/api/v10/"):
                    return self._send({"success": False, "error_code": "invalid_request"}, 404)
                path = path[len("/api/v10/"):]
                if path == "login/" and method == "GET":
                    return self._send({"success": True, "result": {"logged_in": False, "challenge": CHALLENGE}})
                if path == "login/session/" and method == "POST":
                    expected = hmac.new(APP_TOKEN.encode(), CHALLENGE.encode(), hashlib.sha1).hexdigest()
                    if self._body().get("password") != expected:
                        return self._send({"success": False, "error_code": "invalid_token"}, 403)
                    return self._send({"success": True, "result": {
                        "session_token": SESSION, "permissions": {"settings": fake.settings_permission}}})
                if path == "login/logout/":
                    return self._send({"success": True})
                if self.headers.get("X-Fbx-App-Auth") != SESSION:
                    return self._send({"success": False, "error_code": "auth_required"}, 403)
                if path == "dhcp/config/":
                    return self._send({"success": True, "result": fake.dhcp})
                if path == "dhcp/dynamic_lease/":
                    return self._send({"success": True, "result": fake.dynamic})
                if path == "dhcp/static_lease/" and method == "GET":
                    return self._send({"success": True, "result": list(fake.static.values())})
                if path == "dhcp/static_lease/" and method == "POST":
                    if not fake.settings_permission:
                        return self._send({"success": False, "error_code": "insufficient_rights"}, 403)
                    body = self._body()
                    if any(l["ip"] == body["ip"] for l in fake.static.values()):
                        return self._send({"success": False, "error_code": "alloc_failed"})
                    lease = {"id": body["mac"], "mac": body["mac"], "ip": body["ip"], "comment": body.get("comment", "")}
                    fake.static[body["mac"]] = lease
                    return self._send({"success": True, "result": lease})
                if path.startswith("dhcp/static_lease/"):
                    lease_id = path[len("dhcp/static_lease/"):]
                    if lease_id not in fake.static:
                        return self._send({"success": False, "error_code": "noent"}, 404)
                    if method == "PUT":
                        fake.static[lease_id].update(self._body())
                        return self._send({"success": True, "result": fake.static[lease_id]})
                    if method == "DELETE":
                        del fake.static[lease_id]
                        return self._send({"success": True})
                return self._send({"success": False, "error_code": "invalid_request"}, 404)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

            def do_DELETE(self):
                self._route("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakePve:
    def __init__(self, root, node="pve"):
        self.root = root
        self.node = node
        self.ids = {}
        os.makedirs(os.path.join(root, "nodes", node, "qemu-server"), exist_ok=True)
        os.makedirs(os.path.join(root, "nodes", node, "lxc"), exist_ok=True)
        os.symlink(os.path.join("nodes", node), os.path.join(root, "local"))
        self._write_vmlist()

    def _write_vmlist(self):
        with open(os.path.join(self.root, ".vmlist"), "w") as fh:
            json.dump({"version": 1, "ids": self.ids}, fh)

    def add(self, vmid, guest_type, text, node=None, age=60):
        node = node or self.node
        folder = "qemu-server" if guest_type == "qemu" else "lxc"
        os.makedirs(os.path.join(self.root, "nodes", node, folder), exist_ok=True)
        path = os.path.join(self.root, "nodes", node, folder, "%d.conf" % vmid)
        with open(path, "w") as fh:
            fh.write(text)
        mtime = os.path.getmtime(path) - age
        os.utime(path, (mtime, mtime))
        self.ids[str(vmid)] = {"node": node, "type": guest_type, "version": 1}
        self._write_vmlist()

    def remove(self, vmid):
        info = self.ids.pop(str(vmid))
        folder = "qemu-server" if info["type"] == "qemu" else "lxc"
        os.remove(os.path.join(self.root, "nodes", info["node"], folder, "%d.conf" % vmid))
        self._write_vmlist()
