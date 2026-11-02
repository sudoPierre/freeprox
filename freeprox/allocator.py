"""Pick a free IP address on the Freebox LAN."""

import ipaddress
import subprocess


class AllocationError(Exception):
    pass


def lan_network(dhcp_config):
    return ipaddress.IPv4Network("%s/%s" % (dhcp_config["gateway"], dhcp_config["netmask"]), strict=False)


def parse_excludes(items):
    """Accept single IPs or 'a.b.c.d-a.b.c.e' ranges."""
    excluded = set()
    for item in items:
        if "-" in item:
            start, end = (ipaddress.IPv4Address(x.strip()) for x in item.split("-", 1))
            excluded.update(ipaddress.IPv4Address(i) for i in range(int(start), int(end) + 1))
        else:
            excluded.add(ipaddress.IPv4Address(item))
    return excluded


def answers_ping(ip):
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "1", str(ip)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pick_free_ip(dhcp_config, used, pool_start=None, pool_end=None, excluded=(), ping_check=False, pinger=answers_ping):
    """First IP of the pool that is not reserved, leased or excluded (nor reachable if ping_check)."""
    network = lan_network(dhcp_config)
    start = ipaddress.IPv4Address(pool_start or dhcp_config["ip_range_start"])
    end = ipaddress.IPv4Address(pool_end or dhcp_config["ip_range_end"])
    if start not in network or end not in network or start > end:
        raise AllocationError("Invalid IP pool %s-%s for network %s" % (start, end, network))
    taken = {ipaddress.IPv4Address(ip) for ip in used if ip}
    taken.add(ipaddress.IPv4Address(dhcp_config["gateway"]))
    taken.update(excluded)
    taken.update((network.network_address, network.broadcast_address))
    for value in range(int(start), int(end) + 1):
        ip = ipaddress.IPv4Address(value)
        if ip in taken:
            continue
        if ping_check and pinger(ip):
            continue
        return str(ip)
    raise AllocationError("No free IP left in pool %s-%s" % (start, end))
