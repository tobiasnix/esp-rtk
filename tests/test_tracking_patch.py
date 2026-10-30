# SPDX-License-Identifier: AGPL-3.0-only
"""Fan-out patches of tracking module globals and the guard of the tracking facade (#26c).

After tracking.py becomes the package tracking/, a module global such as `log`, `os` or
`MAX_FEATURES` is bound in several package modules. support.patch_module_global replaces it
in every module that binds it; a plain patch of the facade would reach none of them, so
the facade refuses it.
"""
import sys
import unittest
from unittest import mock

import support
import tracking


def tracking_modules():
    return [m for k, m in sorted(sys.modules.items())
            if m is not None and (k == "tracking" or k.startswith("tracking."))]


def bindings(name):
    return {m.__name__: vars(m)[name] for m in tracking_modules() if name in vars(m)}


class TrackingPatchTests(unittest.TestCase):
    def tearDown(self):
        mock.patch.stopall()

    def test_patch_module_global_shares_one_mock_with_options(self):
        before = bindings("_now_seconds")
        self.assertTrue(before)
        with support.patch_module_global("tracking", "_now_seconds", return_value=7) as fake:
            self.assertEqual(tracking._now_seconds(), 7)
            for module_name in before:
                with self.subTest(module=module_name):
                    self.assertIs(vars(sys.modules[module_name])["_now_seconds"], fake)
        self.assertEqual(bindings("_now_seconds"), before)
        with support.patch_module_global("tracking", "_now_seconds", side_effect=OSError) as fake:
            with self.assertRaises(OSError):
                tracking._now_seconds()
            self.assertEqual(bindings("_now_seconds"), dict.fromkeys(before, fake))
        self.assertEqual(bindings("_now_seconds"), before)
        original = tracking._now_seconds
        with support.patch_module_global("tracking", "_now_seconds", wraps=lambda: 11) as fake:
            self.assertEqual(tracking._now_seconds(), 11)
            fake.assert_called_once_with()
        self.assertIs(tracking._now_seconds, original)

    def test_patch_module_global_creates_a_missing_name_everywhere_and_removes_it(self):
        sentinel = object()
        self.assertEqual(bindings("open"), {})
        p = support.patch_module_global("tracking", "open", sentinel, create=True)
        self.assertIs(p.start(), sentinel)
        for module in tracking_modules():
            with self.subTest(module=module.__name__):
                self.assertIs(vars(module).get("open"), sentinel)
        p.stop()
        self.assertEqual(bindings("open"), {})

    def test_patch_module_global_refuses_a_missing_name_without_create(self):
        with self.assertRaises(AttributeError):
            support.patch_module_global("tracking", "no_such_name").start()
        self.assertEqual(bindings("no_such_name"), {})

    def test_tracking_facade_refuses_a_plain_patch(self):
        plain = (
            ("patch.object", lambda: mock.patch.object(tracking, "log").start()),
            ("patch", lambda: mock.patch("tracking.quality_check").start()),
            ("patch.multiple", lambda: mock.patch.multiple(tracking, MAX_PROJECTS=1).start()),
            ("setattr", lambda: setattr(tracking, "MAX_FEATURES", 1)),
            ("create", lambda: mock.patch.object(tracking, "open", 1, create=True).start()),
        )
        try:
            for label, attempt in plain:
                with self.subTest(form=label):
                    with self.assertRaises(AttributeError) as caught:
                        attempt()
                    self.assertIn("patch_module_global", str(caught.exception))
        finally:
            mock.patch.stopall()
        self.assertEqual(tracking.MAX_FEATURES, 5000)
        self.assertNotEqual(tracking.MAX_PROJECTS, 1)
        self.assertNotIsInstance(tracking.log, mock.MagicMock)
        self.assertNotIsInstance(tracking.quality_check, mock.MagicMock)
        self.assertNotIn("open", vars(tracking))

    def test_tracking_facade_allows_its_own_names(self):
        original = tracking.tracker
        with mock.patch("tracking.tracker") as fake:
            self.assertIs(tracking.tracker, fake)
        self.assertIs(tracking.tracker, original)
        self.assertEqual(type(tracking).owned, frozenset({"tracker"}))

    def test_fan_out_patches_survive_stopall(self):
        log = tracking.log
        support.patch_module_global("tracking", "log").start()
        opener = support.patch_module_global("tracking", "open", object(), create=True)
        opener.start()
        self.assertIsInstance(tracking.log, mock.MagicMock)
        mock.patch.stopall()
        self.assertIs(tracking.log, log)
        self.assertEqual(bindings("open"), {})
        opener.stop()                   # a second stop after stopall() is a no-op
        self.assertEqual(bindings("open"), {})


if __name__ == "__main__":
    unittest.main()
