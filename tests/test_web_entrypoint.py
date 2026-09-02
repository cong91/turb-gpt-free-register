import unittest
from unittest import mock

import web


class WebEntrypointTests(unittest.TestCase):
    def test_rejects_a_port_already_bound_by_another_listener(self):
        socket_instance = mock.MagicMock()
        socket_instance.bind.side_effect = OSError(10048, "address in use")

        with mock.patch("web.socket.socket", return_value=socket_instance):  # noqa: SIM117
            with self.assertRaisesRegex(RuntimeError, "5057.*已被占用"):
                web._assert_listen_address_available("127.0.0.1", 5057)

        socket_instance.close.assert_called_once()
