"""Flash drive checks: hang protection for device probes and a fake-capacity test.

Hang protection
    Some failing or fake flash drives stop answering: a read can block inside the
    kernel for minutes. A Python thread stuck like that cannot be killed, so device
    probes run in a daemon thread with a timeout (run_with_timeout / GuardedWorker).
    When the timeout passes the caller gets a result marked 'tidak merespons' and the
    device is put in the STUCK registry: no new probe is started on it during a
    cool-down period (and while the old blocked thread is still alive), so stuck
    threads do not pile up. The user can retry after the cool-down.

Capacity test (like H2testw)
    capacity_test() fills free space with files of known content, reads them back and
    compares every 1 MiB block. A fake-capacity drive returns wrong data (often the
    data of an earlier block) beyond its real size.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .logsetup import get_logger

log = get_logger("flashcheck")

NOT_RESPONDING = "tidak merespons"
PROBE_TIMEOUT = 8.0          # seconds for a quick probe (open, boot sector, disk list)
READ_TIMEOUT = 30.0          # seconds for one block read during a long scan
STUCK_COOLDOWN = 120.0       # seconds before a stuck device may be probed again

MIB = 1024 * 1024
GIB = 1024 * MIB
FOLDER_NAME = "capacity_test"
FILE_SUFFIX = ".cap"


# ---------------------------------------------------------------- hang protection

class DeviceNotResponding(RuntimeError):
    """A device operation passed its timeout, or the device is still in its cool-down."""

    def __init__(self, message: str, key: str = ""):
        super().__init__(message)
        self.key = key


def device_key(path: str, whole_drive: bool = False) -> str:
    """Normalise a device or path so that \\\\.\\G:, G:, G:\\ (and with whole_drive any G:\\...) match."""
    p = str(path or "").strip()
    if os.name == "nt" or (len(p) >= 2 and p[1] == ":") or p.startswith("\\\\"):
        up = p.replace("/", "\\").upper()
        for prefix in ("\\\\.\\", "\\\\?\\"):
            if up.startswith(prefix):
                up = up[4:]
                break
        if up.startswith("PHYSICALDRIVE"):
            return up.rstrip("\\")
        if len(up) >= 2 and up[1] == ":" and up[0].isalpha():
            if whole_drive or up[2:] in ("", "\\"):
                return up[:2]
        return os.path.normcase(os.path.abspath(p)) if p else ""
    return p if p.startswith("/dev/") else os.path.abspath(p) if p else ""


class StuckRegistry:
    """Devices whose last operation did not finish in time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def mark(self, key: str, thread: Optional[threading.Thread] = None,
             cooldown: float = STUCK_COOLDOWN, op: str = "") -> None:
        if not key:
            return
        now = time.monotonic()
        with self._lock:
            entry = self._items.setdefault(key, {"since": now, "until": 0.0, "threads": [], "op": op})
            entry["until"] = max(entry["until"], now + cooldown)
            entry["op"] = op or entry["op"]
            if thread is not None:
                entry["threads"].append(thread)
        log.warning("device %s tidak merespons (%s); cool-down %.0f dtk", key, op or "-", cooldown)

    def _entry(self, key: str) -> Optional[dict]:
        entry = self._items.get(key)
        if entry is None:
            return None
        entry["threads"] = [t for t in entry["threads"] if t.is_alive()]
        if time.monotonic() >= entry["until"] and not entry["threads"]:
            del self._items[key]
            return None
        return entry

    def is_stuck(self, key: str) -> bool:
        with self._lock:
            return self._entry(key) is not None

    def remaining(self, key: str) -> float:
        with self._lock:
            entry = self._entry(key)
            return max(0.0, entry["until"] - time.monotonic()) if entry else 0.0

    def message(self, key: str) -> str:
        with self._lock:
            entry = self._entry(key)
            if entry is None:
                return ""
            left = entry["until"] - time.monotonic()
            busy = bool(entry["threads"])
        if left > 0:
            return (f"Perangkat {key} {NOT_RESPONDING}. Tunggu sekitar {int(left) + 1} detik lalu coba lagi, "
                    "atau cabut dan pasang ulang perangkat.")
        if busy:
            return (f"Perangkat {key} {NOT_RESPONDING}: pembacaan sebelumnya masih tertahan di sistem operasi. "
                    "Cabut dan pasang ulang perangkat, lalu coba lagi.")
        return ""

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            out = {}
            for key in list(self._items):
                entry = self._entry(key)
                if entry is not None:
                    out[key] = {"remaining": round(max(0.0, entry["until"] - time.monotonic()), 1),
                                "busy": bool(entry["threads"]), "op": entry["op"]}
            return out

    def clear(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._items.clear()
            else:
                self._items.pop(key, None)


STUCK = StuckRegistry()


@dataclass
class ProbeResult:
    status: str                       # "ok" | "error" | NOT_RESPONDING
    value: Any = None
    error: str = ""
    message: str = ""
    elapsed: float = 0.0
    exception: Optional[BaseException] = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def timed_out(self) -> bool:
        return self.status == NOT_RESPONDING


def run_with_timeout(fn: Callable[..., Any], *args, timeout: float = PROBE_TIMEOUT,
                     key: Optional[str] = None, cooldown: float = STUCK_COOLDOWN,
                     registry: Optional[StuckRegistry] = None, **kwargs) -> ProbeResult:
    """Run fn(*args, **kwargs) in a daemon thread and wait at most `timeout` seconds.

    Never blocks longer than the timeout. On timeout the device `key` is marked stuck;
    while it is stuck, fn is not started at all and a NOT_RESPONDING result comes back
    immediately.
    """
    reg = registry or STUCK
    if key and reg.is_stuck(key):
        return ProbeResult(NOT_RESPONDING, message=reg.message(key))
    box: dict = {}
    done = threading.Event()

    def target() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            box["exc"] = exc
        finally:
            done.set()

    start = time.monotonic()
    thread = threading.Thread(target=target, daemon=True, name=f"probe-{key or getattr(fn, '__name__', 'fn')}")
    thread.start()
    if not done.wait(timeout):
        if key:
            reg.mark(key, thread, cooldown, getattr(fn, "__name__", ""))
        msg = (f"Perangkat {key or ''} {NOT_RESPONDING} setelah {timeout:g} detik. "
               "Coba lagi nanti, atau cabut dan pasang ulang perangkat.").replace("  ", " ")
        return ProbeResult(NOT_RESPONDING, message=msg, elapsed=time.monotonic() - start)
    elapsed = time.monotonic() - start
    if "exc" in box:
        exc = box["exc"]
        return ProbeResult("error", error=str(exc), message=str(exc), elapsed=elapsed, exception=exc)
    return ProbeResult("ok", value=box.get("value"), elapsed=elapsed)


class GuardedWorker:
    """One worker thread that runs device calls one by one, each with a timeout.

    Used for long scans: the open file handle stays in one thread and every read is
    bounded. After the first timeout the worker is broken; later calls fail at once.
    """

    def __init__(self, key: str = "", registry: Optional[StuckRegistry] = None,
                 cooldown: float = STUCK_COOLDOWN):
        self.key = key
        self.registry = registry or STUCK
        self.cooldown = cooldown
        self.broken = False
        self._q: "queue.Queue" = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"guarded-{key}")
        self._thread.start()

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, args, box, ev = item
            try:
                box["value"] = fn(*args)
            except BaseException as exc:  # noqa: BLE001
                box["exc"] = exc
            finally:
                ev.set()

    def call(self, fn: Callable[..., Any], *args, timeout: float = READ_TIMEOUT) -> Any:
        if self.broken:
            raise DeviceNotResponding(self.registry.message(self.key) or f"Perangkat {NOT_RESPONDING}", self.key)
        box: dict = {}
        ev = threading.Event()
        self._q.put((fn, args, box, ev))
        if not ev.wait(timeout):
            self.broken = True
            self._q.put(None)  # let the thread exit once the blocked call returns
            self.registry.mark(self.key, self._thread, self.cooldown, getattr(fn, "__name__", ""))
            raise DeviceNotResponding(
                f"Perangkat {self.key} {NOT_RESPONDING} (lebih dari {timeout:g} detik). "
                "Coba lagi nanti, atau cabut dan pasang ulang perangkat.", self.key)
        if "exc" in box:
            raise box["exc"]
        return box.get("value")

    def close(self) -> None:
        if not self.broken:
            self._q.put(None)

    def after_exit(self, fn: Callable[[], Any]) -> None:
        """Run fn (e.g. closing a handle) in the background once the worker thread has ended."""
        def wait_then_run() -> None:
            self._thread.join()
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=wait_then_run, daemon=True, name=f"cleanup-{self.key}").start()


# ---------------------------------------------------------------- capacity test

class CapacityRefused(ValueError):
    pass


@dataclass
class CapacityResult:
    target_dir: str
    requested_bytes: int
    bytes_written: int = 0
    bytes_verified_ok: int = 0
    mismatched_blocks: int = 0
    unreadable_blocks: int = 0
    first_bad_offset: Optional[int] = None
    write_mb_per_s: float = 0.0
    read_mb_per_s: float = 0.0
    cancelled: bool = False
    disk_full: bool = False
    write_error: str = ""
    cleaned_up: bool = False
    cleanup_error: str = ""
    block_size: int = MIB
    ok: bool = False
    verdict: str = ""


def block_pattern(index: int, size: int = MIB) -> bytes:
    """Deterministic content of block `index` (differs per block, so wrap-around is detected)."""
    return hashlib.shake_128(b"filerecovery-capacity:" + index.to_bytes(8, "little")).digest(size)


def system_drive() -> str:
    return os.environ.get("SystemDrive", "C:").rstrip("\\").upper()


def _is_on_system_drive(path: str) -> bool:
    path = os.path.abspath(path)
    if os.name == "nt":
        return device_key(path, whole_drive=True) == system_drive()
    try:
        return os.stat(path).st_dev == os.stat("/").st_dev
    except OSError:
        return False


def check_capacity_target(target_dir: str) -> str:
    """Validate the folder for a capacity test. Returns the absolute path or raises CapacityRefused."""
    if not target_dir or not str(target_dir).strip():
        raise CapacityRefused("Folder tujuan wajib diisi")
    raw = str(target_dir).strip()
    if raw.startswith(("\\\\.\\", "\\\\?\\")) or raw.startswith("/dev/"):
        raise CapacityRefused("Uji kapasitas butuh folder (contoh G:\\), bukan perangkat mentah")
    path = os.path.abspath(raw)
    if _is_on_system_drive(path):
        raise CapacityRefused(f"{path} ada di drive sistem. Uji kapasitas tidak diizinkan di drive sistem.")
    if not os.path.exists(path):
        raise CapacityRefused(f"Folder tidak ditemukan: {path}")
    if not os.path.isdir(path):
        raise CapacityRefused(f"Bukan folder: {path}")
    return path


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112)


class _BufferedReader:
    def __init__(self, path: str):
        self._f = open(path, "rb", buffering=0)
        if hasattr(os, "posix_fadvise"):
            try:  # drop cached pages so the data really comes from the device
                os.posix_fadvise(self._f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass

    def read(self, n: int) -> bytes:
        return self._f.read(n) or b""

    def seek(self, pos: int) -> None:
        self._f.seek(pos)

    def close(self) -> None:
        self._f.close()


class _UncachedWinReader:
    """Windows reader with FILE_FLAG_NO_BUFFERING, so the verify pass reads the flash chip,
    not the file cache. Reads must be sector-aligned; the block size is a multiple of 4096."""

    def __init__(self, path: str, block_size: int):
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.ReadFile.restype = wintypes.BOOL
        k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
        k32.SetFilePointerEx.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
        k32.VirtualAlloc.restype = ctypes.c_void_p
        k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
        self._k32 = k32
        self._wt = wintypes
        handle = k32.CreateFileW(path, 0x80000000, 0x3, None, 3, 0x20000000 | 0x08000000, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._h = handle
        self._size = block_size
        self._buf = k32.VirtualAlloc(None, block_size, 0x3000, 0x04)
        if not self._buf:
            k32.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())

    def read(self, n: int) -> bytes:
        got = self._wt.DWORD(0)
        if not self._k32.ReadFile(self._h, self._buf, min(n, self._size), ctypes.byref(got), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(self._buf, got.value)

    def seek(self, pos: int) -> None:
        if not self._k32.SetFilePointerEx(self._h, pos, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        self._k32.CloseHandle(self._h)
        self._k32.VirtualFree(self._buf, 0, 0x8000)


def _open_reader(path: str, block_size: int):
    if os.name == "nt" and block_size % 4096 == 0:
        try:
            return _UncachedWinReader(path, block_size)
        except OSError as exc:
            log.debug("uncached read not available for %s: %s", path, exc)
    return _BufferedReader(path)


def _read_block(reader, offset: int, size: int) -> bytes:
    """Read one block. `offset` is the position in the whole test data (tests patch this)."""
    return reader.read(size)


def _write_block(fh, offset: int, data: bytes) -> None:
    """Write one whole block or raise OSError. `offset` is the position in the whole test data."""
    view = memoryview(data)
    while view:
        n = fh.write(view)
        if not n:
            raise OSError(errno.EIO, "penulisan tidak maju")
        view = view[n:]


def _gb(n: int) -> str:
    return f"{n / GIB:.2f}".rstrip("0").rstrip(".") if n < 10 * GIB else f"{n / GIB:.1f}"


def _remove_folder(folder: str) -> str:
    try:
        if os.path.exists(folder):
            shutil.rmtree(folder)
        return ""
    except OSError as exc:
        return str(exc)


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_TARGETS: set[str] = set()   # folders with a capacity test still running in this process


def capacity_test(target_dir: str, total_mb: int,
                  cancel: Optional[threading.Event] = None,
                  progress: Optional[Callable[[str, int, int], None]] = None,
                  file_mb: int = 1024, block_size: int = MIB) -> CapacityResult:
    """Run one capacity test per folder at a time (see _capacity_test).

    A test that was abandoned after a cancel can still be running in its thread. A second
    test on the same folder would delete the first one's files, so it is refused until the
    first one has finished.
    """
    target = check_capacity_target(target_dir)
    with _ACTIVE_LOCK:
        if target in _ACTIVE_TARGETS:
            raise CapacityRefused("Uji kapasitas di drive ini masih berjalan (mungkin tertahan). "
                                  "Tunggu sampai selesai, lalu coba lagi.")
        _ACTIVE_TARGETS.add(target)
    try:
        return _capacity_test(target, total_mb, cancel, progress, file_mb, block_size)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_TARGETS.discard(target)


def _capacity_test(target_dir: str, total_mb: int,
                   cancel: Optional[threading.Event] = None,
                   progress: Optional[Callable[[str, int, int], None]] = None,
                   file_mb: int = 1024, block_size: int = MIB) -> CapacityResult:
    """Fake-capacity test (like H2testw): write known data, read it back, compare every block.

    WARNING: this fills FREE SPACE in target_dir with test data (up to total_mb MB, or all
    free space when total_mb <= 0). Existing files are not touched, but the drive will be
    (almost) full while the test runs, and on a fake-capacity drive writing past the real
    size can corrupt files that are already on it. Copy important data off first.

    Files of `file_mb` MB go into target_dir/capacity_test/, each made of blocks of
    `block_size` bytes filled with block_pattern(index), and each file is fsynced. Writing
    stops at the target size, when the disk is full (ENOSPC) or on cancel. Then all
    blocks are read back and compared. The capacity_test folder is always removed at the
    end, also after cancel or an error.

    progress(phase, done_bytes, total_bytes) is called with phase 'write' or 'verify'.
    Refuses (CapacityRefused) the system drive, a missing folder or a non-folder.
    """
    if block_size < 4096 or block_size % 512:
        raise ValueError("block_size harus kelipatan 512 dan minimal 4096")
    if file_mb < 1:
        raise ValueError("file_mb harus minimal 1")
    target = check_capacity_target(target_dir)
    folder = os.path.join(target, FOLDER_NAME)
    if os.path.exists(folder):
        leftovers = os.listdir(folder) if os.path.isdir(folder) else [FOLDER_NAME]
        if any(not n.endswith(FILE_SUFFIX) for n in leftovers):
            raise CapacityRefused(f"{folder} sudah ada dan berisi file lain. Pindahkan atau hapus dulu.")
        shutil.rmtree(folder)  # leftovers of an earlier, interrupted test

    free = shutil.disk_usage(target).free
    want = total_mb * MIB if total_mb and total_mb > 0 else free
    margin = 4 * MIB  # leave room for file system metadata
    want = min(want, max(0, free - margin))
    want -= want % block_size
    result = CapacityResult(target_dir=target, requested_bytes=want, block_size=block_size)
    blocks_per_file = max(1, (file_mb * MIB) // block_size)
    files: list[tuple[str, int, int]] = []     # (path, first block index, block count)

    def is_cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    os.makedirs(folder)
    try:
        # ---------- write ----------
        t0 = time.monotonic()
        index = 0
        total_blocks = want // block_size
        stop = False
        file_no = 0
        while index < total_blocks and not stop:
            if is_cancelled():
                result.cancelled = True
                break
            file_no += 1
            path = os.path.join(folder, f"{file_no:04d}{FILE_SUFFIX}")
            first = index
            written = 0
            with open(path, "wb", buffering=0) as fh:
                try:
                    while written < blocks_per_file and index < total_blocks:
                        if is_cancelled():
                            result.cancelled = True
                            stop = True
                            break
                        _write_block(fh, index * block_size, block_pattern(index, block_size))
                        index += 1
                        written += 1
                        if progress:
                            progress("write", index * block_size, want)
                    os.fsync(fh.fileno())
                except OSError as exc:
                    stop = True
                    if _is_disk_full(exc):
                        result.disk_full = True
                    else:
                        result.write_error = str(exc)
                    log.info("capacity test: writing stopped at %d bytes: %s", index * block_size, exc)
                    try:  # keep only whole blocks so the verify pass knows what to expect
                        fh.truncate(written * block_size)
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
            if written:
                files.append((path, first, written))
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
        result.bytes_written = index * block_size
        wt = time.monotonic() - t0
        result.write_mb_per_s = round(result.bytes_written / MIB / wt, 2) if wt > 0 else 0.0

        # ---------- verify ----------
        t1 = time.monotonic()
        read_bytes = 0
        if not result.cancelled:
            for path, first, count in files:
                if is_cancelled():
                    result.cancelled = True
                    break
                try:
                    reader = _open_reader(path, block_size)
                except OSError:
                    result.unreadable_blocks += count
                    if result.first_bad_offset is None:
                        result.first_bad_offset = first * block_size
                    continue
                try:
                    need_seek = False
                    for i in range(count):
                        if is_cancelled():
                            result.cancelled = True
                            break
                        idx = first + i
                        offset = idx * block_size
                        try:
                            if need_seek:
                                reader.seek(i * block_size)
                                need_seek = False
                            data = _read_block(reader, offset, block_size)
                        except OSError:
                            data = None
                            need_seek = True
                        if not data:  # read error or nothing returned
                            result.unreadable_blocks += 1
                            bad = True
                        else:
                            bad = data != block_pattern(idx, block_size)
                            if bad:
                                result.mismatched_blocks += 1
                            if len(data) < block_size:
                                need_seek = True
                        if bad:
                            if result.first_bad_offset is None or offset < result.first_bad_offset:
                                result.first_bad_offset = offset
                        else:
                            result.bytes_verified_ok += block_size
                        read_bytes += block_size
                        if progress:
                            progress("verify", read_bytes, result.bytes_written)
                finally:
                    reader.close()
                if result.cancelled:
                    break
        rt = time.monotonic() - t1
        result.read_mb_per_s = round(read_bytes / MIB / rt, 2) if rt > 0 and read_bytes else 0.0
    finally:
        result.cleanup_error = _remove_folder(folder)
        result.cleaned_up = not result.cleanup_error
        result.ok = (not result.cancelled and result.bytes_written > 0
                     and result.mismatched_blocks == 0 and result.unreadable_blocks == 0)
        result.verdict = _verdict(result, folder)
        log.info("capacity test %s: %s", target, result.verdict)
    return result


def _verdict(r: CapacityResult, folder: str) -> str:
    tail = "" if r.cleaned_up else f" Folder uji gagal dihapus ({folder}): {r.cleanup_error}"
    if r.cancelled:
        return (f"Dibatalkan: pengujian dihentikan setelah {_gb(r.bytes_written)} GB ditulis dan "
                f"{_gb(r.bytes_verified_ok)} GB diverifikasi. "
                + ("Folder uji sudah dihapus." if r.cleaned_up else tail.strip()))
    if r.mismatched_blocks or r.unreadable_blocks:
        where = _gb(r.first_bad_offset or 0)
        return (f"Kemungkinan flashdisk palsu: data mulai rusak sekitar {where} GB "
                f"({r.mismatched_blocks} blok berbeda, {r.unreadable_blocks} blok tidak terbaca; "
                f"{_gb(r.bytes_verified_ok)} GB dari {_gb(r.bytes_written)} GB benar).{tail}")
    if r.bytes_written == 0:
        why = f" Kesalahan tulis: {r.write_error}" if r.write_error else ""
        return f"Tidak bisa diuji: tidak ada ruang kosong atau data uji tidak bisa ditulis.{why}{tail}"
    note = ""
    if r.disk_full:
        note = " Ruang kosong habis sebelum target tercapai."
    elif r.write_error:
        note = f" Penulisan berhenti karena kesalahan: {r.write_error}"
    return (f"Kapasitas sesuai: {_gb(r.bytes_written)} GB ditulis dan dibaca ulang tanpa kesalahan."
            f"{note}{tail}")
