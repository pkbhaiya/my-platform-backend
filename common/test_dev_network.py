"""Development-only LAN access (config/dev_network.py): which origins and hosts are let in."""

import re
from unittest.mock import patch

from django.test import SimpleTestCase

from config.dev_network import DEV_ORIGIN_REGEX, lan_addresses


class DevNetworkTests(SimpleTestCase):
    def test_only_local_and_private_network_origins_match(self):
        allowed = ["http://localhost:5173", "http://127.0.0.1:5173", "http://192.168.0.108:5173", "http://10.0.0.5:4173", "http://172.20.1.2:5173"]
        refused = ["https://192.168.0.108:5173", "http://192.168.0.108", "http://8.8.8.8:5173", "http://172.32.0.1:5173",
                   "http://example.com:5173", "http://192.168.0.108.evil.com:5173"]
        for origin in allowed:
            self.assertRegex(origin, DEV_ORIGIN_REGEX)
        for origin in refused:
            self.assertIsNone(re.match(DEV_ORIGIN_REGEX, origin), origin)

    def test_lan_addresses_are_private_ipv4_only_and_never_raise(self):
        with patch("config.dev_network.socket.socket") as fake_socket, \
                patch("config.dev_network.socket.gethostbyname_ex", return_value=("host", [], ["192.168.0.108", "127.0.0.1", "8.8.8.8", "169.254.1.1"])):
            fake_socket.return_value.__enter__.return_value.getsockname.return_value = ("10.0.0.7", 51000)
            self.assertEqual(lan_addresses(), ["10.0.0.7", "192.168.0.108"])
        with patch("config.dev_network.socket.socket", side_effect=OSError), \
                patch("config.dev_network.socket.gethostbyname_ex", side_effect=OSError):
            self.assertEqual(lan_addresses(), [])
