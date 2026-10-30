# SPDX-License-Identifier: AGPL-3.0-only
"""Power-cut injection for the tracker and point-store flash code on the host.

Every mutating file-system step that tracking.py and pointstore.py perform
(open for writing, write, flush, truncate, close of a written file, rename,
remove, mkdir) is a numbered crash point. At crash point N the directory is
copied as it is on disk at that instant, and N and every later step raise
PowerCut, a BaseException, so no ``except Exception`` cleanup can still change
the frozen image. Data that Python still buffers is not in the copy, like an
unsynced LittleFS file after a reset. test_restore_power_cut.py also runs the
same scenario on a real LittleFS image when littlefs-python is installed.
"""
import os
import shutil
from unittest import mock

import pointstore
import support
import tracking


class PowerCut(BaseException):
    """The supply failed; nothing after this point reaches flash."""


class _Os:
    def __init__(self, injector, real):
        self._injector, self._real = injector, real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def rename(self, source, target):
        self._injector.point(); return self._real.rename(source, target)

    def remove(self, path):
        self._injector.point(); return self._real.remove(path)

    def mkdir(self, path):
        self._injector.point(); return self._real.mkdir(path)


class _Writer:
    def __init__(self, injector, handle):
        self._injector, self._handle = injector, handle

    def write(self, data):
        if self._injector.fired: return None
        self._injector.point(); return self._handle.write(data)

    def flush(self):
        # A finalizer (__del__) may still flush after the cut; nothing lands.
        if self._injector.fired: return None
        self._injector.point(); return self._handle.flush()

    def truncate(self, *args):
        self._injector.point(); return self._handle.truncate(*args)

    def close(self):
        if self._handle.closed or self._injector.fired: return None
        # The image is frozen before this raises, so whatever the abandoned
        # handle flushes later cannot reach it.
        self._injector.point()
        return self._handle.close()

    def __enter__(self): return self

    def __exit__(self, *args): self.close()

    def __iter__(self): return iter(self._handle)

    def __getattr__(self, name): return getattr(self._handle, name)


class Injector:
    """Count crash points; freeze ``folder`` into ``image`` at ``crash_at``."""

    def __init__(self, folder, image=None, crash_at=None):
        self.folder, self.image, self.crash_at = folder, image, crash_at
        self.count, self.fired = 0, False

    def point(self):
        if self.fired: raise PowerCut()
        self.count += 1
        if self.crash_at is not None and self.count >= self.crash_at:
            shutil.copytree(self.folder, self.image)
            self.fired = True
            raise PowerCut()

    def open(self, path, mode="r", *args, **kwargs):
        writing = any(flag in mode for flag in "wa+")
        if writing: self.point()
        handle = open(path, mode, *args, **kwargs)
        return _Writer(self, handle) if writing else handle

    def patches(self):
        fake = _Os(self, os)
        return [support.patch_module_global("tracking", "os", fake),
                mock.patch.object(pointstore, "os", fake),
                support.patch_module_global("tracking", "open", self.open, create=True),
                mock.patch.object(pointstore, "open", self.open, create=True)]

    def __enter__(self):
        self._active = self.patches()
        for patch in self._active: patch.start()
        return self

    def __exit__(self, *args):
        for patch in reversed(self._active): patch.stop()
