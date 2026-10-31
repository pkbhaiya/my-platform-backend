"""Development only: let a phone on the same Wi-Fi reach the dev servers.

Used by config/settings.py while DEBUG is on. Run the API with
``manage.py runserver 0.0.0.0:8000`` and the frontend with ``npm run dev:lan``,
then open http://<this computer's LAN address>:5173 on the phone.
"""

import ipaddress
import socket

# http://localhost, 127.0.0.1 or a private (RFC 1918) address, any port.
DEV_ORIGIN_REGEX = (
    r"^http://(localhost|127\.0\.0\.1|10(\.\d{1,3}){3}|192\.168(\.\d{1,3}){2}"
    r"|172\.(1[6-9]|2\d|3[01])(\.\d{1,3}){2}):\d{1,5}$"
)


def lan_addresses():
    """This computer's private IPv4 addresses (e.g. 192.168.0.108); never raises."""
    found = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # picks the outgoing interface; sends nothing
            found.add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    addresses = []
    for address in found:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local:
            addresses.append(address)
    return sorted(addresses)
