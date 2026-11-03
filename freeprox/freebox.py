"""Minimal Freebox OS API client (authentication + DHCP)."""

import hashlib
import hmac
import json
import logging
import ssl
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# Error codes meaning the session must be renewed.
SESSION_ERRORS = {"auth_required", "invalid_session", "invalid_token"}


class FreeboxError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class FreeboxClient:
    def __init__(self, url, app_id, app_token=None, verify_tls=True, ca_file=None, timeout=10):
        self.url = url.rstrip("/")
        self.app_id = app_id
        self.app_token = app_token
        self.timeout = timeout
        self.permissions = {}
        self._api = None
        self._session = None
        self._ssl = None
        if self.url.startswith("https://"):
            if not verify_tls:
                self._ssl = ssl._create_unverified_context()
            else:
                self._ssl = ssl.create_default_context(cafile=ca_file or None)

    # -- transport -------------------------------------------------------

    def _request(self, method, url, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            # The Freebox returns an explanatory JSON body even on 4xx.
            payload = exc.read()
        except urllib.error.URLError as exc:
            raise FreeboxError("Freebox unreachable (%s): %s" % (self.url, exc.reason))
        try:
            return json.loads(payload.decode() or "{}")
        except ValueError:
            raise FreeboxError("Invalid response from the Freebox on %s %s" % (method, url))

    def discover(self):
        info = self._request("GET", self.url + "/api_version")
        if "api_version" not in info:
            raise FreeboxError("No Freebox found at %s" % self.url)
        major = str(info["api_version"]).split(".")[0]
        self._api = "%s%sv%s/" % (self.url, info.get("api_base_url", "/api/"), major)
        log.debug("Freebox API: %s (%s)", self._api, info.get("device_name"))
        return info

    def call(self, method, path, body=None, auth=True, _retry=True):
        if self._api is None:
            self.discover()
        headers = {}
        if auth:
            if self._session is None:
                self.login()
            headers["X-Fbx-App-Auth"] = self._session
        resp = self._request(method, self._api + path, body, headers)
        if not resp.get("success"):
            code = resp.get("error_code")
            if auth and _retry and code in SESSION_ERRORS:
                self._session = None
                return self.call(method, path, body, auth, _retry=False)
            message = resp.get("msg") or "unknown error"
            raise FreeboxError("%s [%s %s, error_code=%s]" % (message, method, path, code), code)
        return resp.get("result")

    # -- authentication --------------------------------------------------

    def request_authorization(self, app_name, app_version, device_name):
        """Request an app_token; the user must approve it on the Freebox display."""
        res = self.call("POST", "login/authorize/", {
            "app_id": self.app_id,
            "app_name": app_name,
            "app_version": app_version,
            "device_name": device_name,
        }, auth=False)
        return res["app_token"], res["track_id"]

    def authorization_status(self, track_id):
        return self.call("GET", "login/authorize/%s" % track_id, auth=False)["status"]

    def login(self):
        if not self.app_token:
            raise FreeboxError("No app_token: run 'freeprox register' first")
        challenge = self.call("GET", "login/", auth=False)["challenge"]
        password = hmac.new(self.app_token.encode(), challenge.encode(), hashlib.sha1).hexdigest()
        res = self.call("POST", "login/session/", {"app_id": self.app_id, "password": password}, auth=False)
        self._session = res["session_token"]
        self.permissions = res.get("permissions") or {}
        return self.permissions

    def logout(self):
        if self._session:
            try:
                self.call("POST", "login/logout/", _retry=False)
            except FreeboxError:
                pass
            self._session = None

    # -- DHCP ------------------------------------------------------------

    def dhcp_config(self):
        return self.call("GET", "dhcp/config/")

    def static_leases(self):
        return self.call("GET", "dhcp/static_lease/") or []

    def dynamic_leases(self):
        return self.call("GET", "dhcp/dynamic_lease/") or []

    def lan_host(self, mac, interface="pub"):
        """Host seen by the Freebox network browser for this MAC, or None."""
        try:
            return self.call("GET", "lan/browser/%s/ether-%s" % (interface, mac.lower()))
        except FreeboxError as exc:
            if exc.code in ("noent", "nodev"):
                return None
            raise

    def add_static_lease(self, mac, ip, comment=""):
        return self.call("POST", "dhcp/static_lease/", {"mac": mac, "ip": ip, "comment": comment})

    def update_static_lease(self, lease_id, **fields):
        return self.call("PUT", "dhcp/static_lease/%s" % lease_id, fields)

    def delete_static_lease(self, lease_id):
        return self.call("DELETE", "dhcp/static_lease/%s" % lease_id)
