# SPDX-License-Identifier: AGPL-3.0-only
import gzip
import os
import tempfile
import unittest

from support import web, REPO


class Writer:
    def __init__(self):
        self.data = bytearray()

    def write(self, d):
        self.data += d

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


class TestStaticGzip(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)
        self.addCleanup(self.tmp.cleanup)
        self.plain = b"plain-asset" * 50
        self.packed = gzip.compress(self.plain, mtime=0)
        with open("x.js", "wb") as fh:
            fh.write(self.plain)
        with open("x.js.gz", "wb") as fh:
            fh.write(self.packed)

    async def send(self, accept, method="GET"):
        w = Writer()
        await web._http_send_file(w, "x.js", "application/javascript", method, accept)
        head, body = bytes(w.data).split(b"\r\n\r\n", 1)
        return head.split(b"\r\n"), body

    async def test_gzip_variant_is_served_when_accepted(self):
        lines, body = await self.send(True)
        self.assertIn(b"Content-Encoding: gzip", lines)
        self.assertIn(b"Vary: Accept-Encoding", lines)
        self.assertIn(b"Content-Length: %d" % len(self.packed), lines)
        self.assertEqual(body, self.packed)

    async def test_plain_file_without_accept_encoding(self):
        lines, body = await self.send(False)
        self.assertFalse([l for l in lines if l.startswith(b"Content-Encoding")])
        self.assertIn(b"Content-Length: %d" % len(self.plain), lines)
        self.assertEqual(body, self.plain)

    async def test_plain_file_when_no_gzip_variant_exists(self):
        os.remove("x.js.gz")
        lines, body = await self.send(True)
        self.assertFalse([l for l in lines if l.startswith(b"Content-Encoding")])
        self.assertEqual(body, self.plain)

    async def test_head_reports_the_chosen_length_without_body(self):
        lines, body = await self.send(True, "HEAD")
        self.assertIn(b"Content-Length: %d" % len(self.packed), lines)
        self.assertEqual(body, b"")

    def test_i18n_route_serves_the_precompressed_copy(self):
        os.chdir(REPO)
        from test_access_v11 import http_request
        head, _, body = http_request(
            b"GET /i18n.js?v=15.9.1 HTTP/1.1\r\nHost: 192.168.4.1\r\n"
            b"Accept-Encoding: gzip, deflate, br\r\n\r\n").partition(b"\r\n\r\n")
        with open(os.path.join(REPO, "i18n.js.gz"), "rb") as fh:
            packed = fh.read()
        with open(os.path.join(REPO, "i18n.js"), "rb") as fh:
            plain = fh.read()
        self.assertIn(b"Content-Encoding: gzip", head.split(b"\r\n"))
        self.assertEqual((body, gzip.decompress(body)), (packed, plain))


if __name__ == "__main__":
    unittest.main()
