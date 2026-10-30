# SPDX-License-Identifier: AGPL-3.0-only
"""Run tracking.py and pointstore.py on a LittleFS image (littlefs-python).

The device uses LittleFS v2 through MicroPython's VFS: a write goes straight
into the LittleFS file cache, flush() is lfs_file_sync and only sync or close
commits file data; rename and remove are atomic metadata commits. Volume
mounts the same C library on a RAM image with the device's geometry and maps
the few os and open() calls the tracker uses onto it. A power cut before
block write N (program or erase) is a copy of the image taken just before
that write: exactly the flash content at the instant of the cut. The run goes
on undisturbed (LittleFS asserts if it is fed errors and used further), so one
run yields the images for every requested cut; only those copies are booted.
"""
import errno
import os as _real_os
from unittest import mock

import pointstore
import support
import tracking

from littlefs import FileHandle, LittleFS, UserContext, lfs
from littlefs.errors import LittleFSError

_FLAG = lfs.LFSFileFlag
# MicroPython's VFS flags; littlefs-python's own "a" mode does not create.
_FLAGS = {"w": _FLAG.wronly | _FLAG.creat | _FLAG.trunc,
          "a": _FLAG.wronly | _FLAG.creat | _FLAG.append,
          "r+": _FLAG.rdwr}

# MicroPython's VfsLfs2 defaults on the ESP32-S3: 4 KiB blocks, 32-byte read
# and program units, 32-byte lookahead, cache 4 * max(read, prog).
GEOMETRY = {"block_size": 4096, "read_size": 32, "prog_size": 32, "cache_size": 128,
            "lookahead_size": 32, "block_cycles": 100}
BLOCKS = 96


class _Recorder(UserContext):
    def __init__(self, buffer):
        super().__init__(buffer=buffer)
        self.writes, self.cuts, self.images = 0, (), {}

    def _spend(self):
        if self.writes in self.cuts:
            self.images[self.writes] = bytes(self.buffer)
        self.writes += 1

    def prog(self, cfg, block, off, data):
        self._spend(); return super().prog(cfg, block, off, data)

    def erase(self, cfg, block):
        self._spend(); return super().erase(cfg, block)


def _os_error(error, path):
    # LittleFS error codes are negative errno values (LFS_ERR_NOENT = -2).
    code = -getattr(error, "code", -errno.EIO)
    return OSError(code if code > 0 else errno.EIO, str(error), path)


class _Reader:
    """Read mode: the file content as of open, with byte offsets like the device."""

    def __init__(self, data, binary):
        self.data, self.binary, self.position, self.closed = data, binary, 0, False

    def _out(self, raw): return raw if self.binary else raw.decode("utf-8")

    def read(self, size=-1):
        end = len(self.data) if size is None or size < 0 else self.position + size
        raw = self.data[self.position:end]; self.position += len(raw)
        return self._out(raw)

    def readline(self):
        end = self.data.find(b"\n", self.position)
        end = len(self.data) if end < 0 else end + 1
        raw = self.data[self.position:end]; self.position = end
        return self._out(raw)

    def __iter__(self):
        while True:
            line = self.readline()
            if not line: return
            yield line

    def seek(self, offset, whence=0):
        self.position = offset if whence == 0 else (
            self.position + offset if whence == 1 else len(self.data) + offset)
        return self.position

    def tell(self): return self.position

    def close(self): self.closed = True

    def __enter__(self): return self

    def __exit__(self, *args): self.close()


class _Writer:
    def __init__(self, raw, binary, path):
        self.raw, self.binary, self.path, self.closed = raw, binary, path, False

    def _call(self, function, *args):
        try: return function(*args)
        except LittleFSError as error: raise _os_error(error, self.path)

    def write(self, data):
        return self._call(self.raw.write, data if self.binary else data.encode("utf-8"))

    def flush(self): return self._call(self.raw.flush)

    def seek(self, offset, whence=0): return self._call(self.raw.seek, offset, whence)

    def tell(self): return self._call(self.raw.tell)

    def truncate(self, size=None):
        if size is not None: self.seek(size)
        return self._call(self.raw.truncate)

    def close(self):
        if not self.closed:
            self.closed = True
            self._call(self.raw.close)

    def __enter__(self): return self

    def __exit__(self, *args): self.close()


class Volume:
    """A mounted LittleFS image plus the os/open shims for the flash code."""

    def __init__(self, image=None):
        fresh = image is None
        self.context = _Recorder(bytearray(b"\xff" * (GEOMETRY["block_size"] * BLOCKS))
                               if fresh else bytearray(image))
        self.fs = LittleFS(self.context, mount=False, block_count=BLOCKS, **GEOMETRY)
        if fresh: self.fs.format()
        self.fs.mount()

    def cut_before(self, *writes):
        """Record the image before each of these block writes (0-based)."""
        self.context.cuts = set(writes)

    def images(self):
        """{write: image} for the recorded cuts; a cut past the end is the final image."""
        images = dict(self.context.images)
        for write in self.context.cuts:
            images.setdefault(write, self.image())
        return images

    def image(self): return bytes(self.context.buffer)

    @staticmethod
    def _path(path): return "/" if path in ("", ".") else path

    def open(self, path, mode="r", *args, **kwargs):
        binary = "b" in mode
        try:
            if mode.replace("b", "") == "r":
                with self.fs.open(path, "rb", buffering=0) as source:
                    return _Reader(source.readall(), binary)
            handle = lfs.file_open(self.fs.fs, path, _FLAGS[mode.replace("b", "").replace("t", "")])
            return _Writer(FileHandle(self.fs.fs, handle), binary, path)
        except LittleFSError as error:
            raise _os_error(error, path)

    # The os functions tracking.py and pointstore.py call.
    def listdir(self, path="."):
        try: return self.fs.listdir(self._path(path))
        except LittleFSError as error: raise _os_error(error, path)

    def stat(self, path):
        try: value = self.fs.stat(self._path(path))
        except LittleFSError as error: raise _os_error(error, path)
        return (0x4000 if value.type == 2 else 0x8000, 0, 0, 0, 0, 0, value.size, 0, 0, 0)

    def remove(self, path):
        try: self.fs.remove(path)
        except LittleFSError as error: raise _os_error(error, path)

    def rename(self, source, target):
        try: self.fs.rename(source, target)
        except LittleFSError as error: raise _os_error(error, source)

    def mkdir(self, path):
        try: self.fs.mkdir(path)
        except LittleFSError as error: raise _os_error(error, path)

    def statvfs(self, path):
        used = self.fs.used_block_count
        free = BLOCKS - used
        return (GEOMETRY["block_size"], GEOMETRY["block_size"], BLOCKS, free, free,
                0, 0, 0, 0, 255)

    # The inventory epoch of an undo candidate (#21c); not a flash access.
    def urandom(self, size): return _real_os.urandom(size)

    def patches(self):
        return [support.patch_module_global("tracking", "os", self),
                mock.patch.object(pointstore, "os", self),
                support.patch_module_global("tracking", "open", self.open, create=True),
                mock.patch.object(pointstore, "open", self.open, create=True)]

    def __enter__(self):
        self._active = self.patches()
        for patch in self._active: patch.start()
        return self

    def __exit__(self, *args):
        for patch in reversed(self._active): patch.stop()
