# SPDX-License-Identifier: AGPL-3.0-only
"""Board metrics for the status bar. Everything that the device gives about itself. The point is diagnosis in the field: a bad radio connection or a hot chip explain fades that you otherwise only see as "does not work".
"""
import unittest
import asyncio
from unittest import mock

from support import cfg, web


class TestMetrics(unittest.TestCase):
    def test_give_me_a_dict(self):
        self.assertIsInstance(web.board_metrics(), dict)

    def test_contains_the_expected_fields(self):
        m = web.board_metrics()
        for field in ("cpu_hz", "flash_bytes", "fs_total_bytes", "fs_free_bytes",
                     "ram_free_bytes", "ram_alloc_bytes", "mcu_temp_c",
                     "idf_heap_free_bytes", "wifi_rssi", "wifi_channel",
                     "wifi_txpower_dbm", "ap_clients"):
            self.assertIn(field, m, field)

    def test_survives_missing_hardware(self):
        """On other firmware, individual values are missing - this must not cause /status to crash."""
        import sys
        echt = sys.modules.get("esp32")
        sys.modules["esp32"] = None
        try:
            m = web.board_metrics(True)
            self.assertIsNone(m["mcu_temp_c"])
        finally:
            if echt is None:
                sys.modules.pop("esp32", None)
            else:
                sys.modules["esp32"] = echt

    def test_values_are_numbers_or_none(self):
        for key, value in web.board_metrics().items():
            self.assertTrue(value is None or isinstance(value, (int, float)),
                            "%s = %r" % (key, value))

    def test_slow_metrics_are_cached_but_ram_remains_live(self):
        web._BOARD_SLOW_AT = web._BOARD_RADIO_AT = None
        with mock.patch.object(web.os, "statvfs", wraps=web.os.statvfs) as statvfs:
            web.board_metrics()
            web.board_metrics()
            self.assertEqual(statvfs.call_count, 1)


class TestStatusContainsMetrics(unittest.TestCase):
    def test_repeated_status_polls_cache_flash_but_read_current_ram(self):
        web._BOARD_SLOW_AT = web._BOARD_RADIO_AT = None
        replies = []
        with mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web, "_instances", {}), \
                mock.patch.object(web, "_send_json", side_effect=lambda writer, data: replies.append(data)), \
                mock.patch.object(web.os, "statvfs", wraps=web.os.statvfs) as statvfs, \
                mock.patch.object(web.gc, "mem_free", return_value=100000) as ram:
            asyncio.run(web._handle_status_request(None, "/status", "", False, {}))
            ram.return_value = 90000
            asyncio.run(web._handle_status_request(None, "/status", "", False, {}))
        self.assertEqual(statvfs.call_count, 1)
        self.assertEqual([reply["board"]["ram_free_bytes"] for reply in replies],
                         [100000, 90000])

    def test_system_block_is_expanding(self):
        # The status line of the surface reads it from /status.
        with open("web.py") as fh:
            self.assertIn("board_metrics()", fh.read())
