# SPDX-License-Identifier: AGPL-3.0-only
"""Flash stalls fit the overall HTTP deadline without resetting that deadline."""
import asyncio
import unittest
from unittest import mock

from support import web


class TestHttpFlashDelays(unittest.IsolatedAsyncioTestCase):
    async def read_with_delays(self, parts, body=False):
        clock = [0]

        class Reader:
            async def read(self, amount):
                delay, value = parts.pop(0)
                clock[0] += delay
                return value

        async def timed_read(coro, timeout):
            started = clock[0]
            result = await coro
            if clock[0] - started > timeout * 1000:
                raise asyncio.TimeoutError()
            return result

        with mock.patch.object(web.time, "ticks_ms", lambda: clock[0]), \
                mock.patch.object(web.asyncio, "wait_for", timed_read):
            return (await web._read_body(Reader(), b"", 4) if body else
                    await web._read_request(Reader()))

    async def test_header_survives_one_flash_commit_within_five_seconds(self):
        header, body, error = await self.read_with_delays([
            (2500, b"GET /status HTTP/1.1\r\n"), (200, b"Host: device.invalid\r\n\r\n")])
        self.assertIsNone(error)
        self.assertTrue(header.startswith(b"GET /status"))
        self.assertEqual(body, b"")

    async def test_header_survives_flash_delay_inside_service_budget(self):
        header, body, error = await self.read_with_delays([
            (6500, b"GET /status HTTP/1.1\r\nHost: device.invalid\r\n\r\n")])
        self.assertIsNone(error)
        self.assertTrue(header.startswith(b"GET /status"))
        self.assertEqual(body, b"")

    async def test_header_exceeding_service_budget_is_rejected(self):
        _, _, error = await self.read_with_delays([
            (11000, b"GET /status HTTP/1.1\r\nHost: device.invalid\r\n\r\n")])
        self.assertEqual(error, "header_timeout")

    async def test_parser_rejection_logs_reason_and_closes_connection(self):
        class Writer:
            def __init__(self):
                self.data = b""
                self.closed = False
            def write(self, data):
                self.data += data
            async def drain(self):
                pass
            def close(self):
                self.closed = True
            async def wait_closed(self):
                pass

        for reason in ("header_timeout", "header_incomplete", "header_too_large"):
            with self.subTest(reason=reason):
                writer = Writer()
                admitted = web._http_connections
                with mock.patch.object(web, "_read_request", mock.AsyncMock(
                        return_value=(None, None, reason))), \
                        mock.patch.object(web, "log") as logged:
                    await web.handle_http_client(object(), writer)
                logged.assert_called_once_with("WARN", "HTTP",
                                               "Request rejected: %s." % reason)
                expected = b"431" if reason == "header_too_large" else b"400"
                self.assertTrue(writer.data.startswith(b"HTTP/1.0 " + expected))
                self.assertTrue(writer.closed)
                self.assertEqual(web._http_connections, admitted)

    async def test_fragmented_headers_still_share_one_overall_deadline(self):
        _, _, error = await self.read_with_delays([
            (6000, b"GET /status HTTP/1.1\r\n"), (6000, b"Host: device.invalid\r\n\r\n")])
        self.assertEqual(error, "header_timeout")

    async def test_body_survives_flash_but_keeps_overall_deadline(self):
        self.assertEqual(await self.read_with_delays([(2500, b"data")], body=True), b"data")
        self.assertEqual(await self.read_with_delays([(11000, b"data")], body=True), b"")
