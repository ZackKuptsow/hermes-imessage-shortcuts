"""The stand-in plugin n8n/check_relay.py aims its copy of the relay at, in place of the real one."""

from __future__ import annotations

import unittest
import urllib.error
import urllib.request

from n8n_check_support import PLUGIN_PORT, check_relay


class StandInTest(unittest.TestCase):
    def setUp(self):
        self.stand_in = check_relay.StandInPlugin()
        self.stand_in.start()
        self.addCleanup(self.stand_in.stop)

    def post(self, body: bytes) -> int:
        request = urllib.request.Request(self.stand_in.url, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_it_answers_202_and_keeps_the_body(self):
        self.assertEqual(self.post(b'{"a": 1}'), check_relay.ACCEPTED_STATUS)
        self.assertEqual(self.stand_in.received, [b'{"a": 1}'])

    def test_it_answers_whatever_it_is_told(self):
        self.stand_in.answer_status = 503
        self.assertEqual(self.post(b"{}"), 503)

    def test_it_refuses_a_body_past_the_plugins_limit_as_the_plugin_does(self):
        self.assertEqual(self.post(b"x" * (check_relay.PLUGIN_BODY_LIMIT_BYTES + 1)), check_relay.MALFORMED_STATUS)

    def test_it_listens_on_the_loopback_address_and_never_the_plugins_port(self):
        self.assertTrue(self.stand_in.url.startswith("http://127.0.0.1:"))
        self.assertEqual(self.stand_in.listening_on, "127.0.0.1")
        self.assertNotIn(PLUGIN_PORT, self.stand_in.url)

    def test_stopped_it_refuses_connections_and_started_it_comes_back_on_the_same_port(self):
        url = self.stand_in.url
        self.stand_in.stop()
        with self.assertRaises(urllib.error.URLError):
            self.post(b"{}")
        self.stand_in.start()
        self.assertEqual(self.stand_in.url, url)
        self.assertEqual(self.post(b"{}"), check_relay.ACCEPTED_STATUS)


if __name__ == "__main__":
    unittest.main()
