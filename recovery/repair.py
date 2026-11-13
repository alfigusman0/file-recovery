"""Automatic repair for damaged files.

Each repairer reads a damaged file and writes a repaired copy. Originals are
never modified. Repairs are best-effort: they recover the valid part of the
file and report what was done. Formats not listed here are not repaired.

Supported: PNG, JPEG, PDF, ZIP (DOCX/XLSX/PPTX/APK), SQLite.
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass, field
from typing import Callable, Optional

from .formats import jpeg_scan_start


@dataclass
class RepairResult:
    source: str
    output: Optional[str]
    ok: bool
    actions: list[str] = field(default_factory=list)


# ---------- PNG ----------

PNG_SIG = b"\x89PNG\r\n\x1a\n"
IEND = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)


def repair_png(data: bytes) -> tuple[Optional[bytes], list[str]]:
    actions: list[str] = []
    if not data.startswith(PNG_SIG):
        if PNG_SIG in data[:4096]:
            data = data[data.index(PNG_SIG):]
            actions.append("removed garbage before PNG signature")
        else:
            return None, ["PNG signature not found"]
    out = bytearray(PNG_SIG)
    pos = len(PNG_SIG)
    seen_iend = False
    chunks = 0
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + length  # chunk = length(4) + type(4) + data + crc(4)
        if end > len(data) or not ctype.isalpha():
            actions.append(f"stopped at truncated/invalid chunk at byte {pos}")
            break
        body = data[pos + 4:pos + 8 + length]
        crc = struct.unpack(">I", data[end - 4:end])[0]
        if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
            actions.append(f"dropped chunk {ctype!r} at byte {pos} (bad CRC)")
            break
        out += data[pos:end]
        chunks += 1
        pos = end
        if ctype == b"IEND":
            seen_iend = True
            break
    if chunks == 0:
        return None, ["no valid PNG chunks"]
    if not seen_iend:
        out += IEND
        actions.append("appended missing IEND chunk")
    return bytes(out), actions


# ---------- JPEG ----------

def repair_jpeg(data: bytes) -> tuple[Optional[bytes], list[str]]:
    if not data.startswith(b"\xff\xd8"):
        return None, ["JPEG SOI marker not found"]
    actions: list[str] = []
    scan = jpeg_scan_start(data)
    if scan is None:
        # Header is damaged: fall back to the last EOI in the file.
        eoi = data.rfind(b"\xff\xd9")
        if eoi != -1 and eoi == len(data) - 2:
            return data, ["file already ends with EOI marker"]
        actions.append("could not parse JPEG headers; appended EOI marker")
        return data + b"\xff\xd9", actions
    # Inside entropy-coded data, 0xFF is always followed by 0x00 or RSTn, so the first
    # FFD9 after SOS is the real end of the image. EXIF thumbnails appear before SOS.
    eoi = data.find(b"\xff\xd9", scan)
    if eoi != -1:
        if eoi + 2 < len(data):
            actions.append("truncated trailing data after EOI marker")
        return data[:eoi + 2], actions
    actions.append("appended missing EOI marker (lower part of image may be grey)")
    return data + b"\xff\xd9", actions


# ---------- PDF ----------

def repair_pdf(data: bytes) -> tuple[Optional[bytes], list[str]]:
    if b"%PDF-" not in data[:1024]:
        return None, ["PDF header not found"]
    start = data.index(b"%PDF-")
    data = data[start:]
    actions: list[str] = []
    if start:
        actions.append("removed garbage before %PDF header")
    eof = data.rfind(b"%%EOF")
    if eof != -1:
        cut = eof + len(b"%%EOF")
        if cut < len(data):
            actions.append("truncated data after last %%EOF")
        return data[:cut] + b"\n", actions
    actions.append("appended missing %%EOF (xref may still need a PDF tool)")
    return data + b"\n%%EOF\n", actions


# ---------- ZIP / Office / APK ----------

LFH = b"PK\x03\x04"
CDH = b"PK\x01\x02"
EOCD_SIG = b"PK\x05\x06"
DESC_SIG = b"PK\x07\x08"


def _inflate_exact(buf: bytes) -> tuple[Optional[bytes], int]:
    """Decompress raw deflate from buf. Returns (content, bytes_consumed) or (None, 0) if incomplete."""
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(buf)
    except zlib.error:
        return None, 0
    if not d.eof:
        return None, 0
    return out, len(buf) - len(d.unused_data)


def _unix_time(dos_time: int, dos_date: int) -> tuple[int, int]:
    return dos_time, dos_date


def _read_entry(data: bytes, dstart: int, method: int, flags: int, crc_hdr: int,
                csize_hdr: int) -> tuple[Optional[bytes], Optional[bytes], int, int, str]:
    """Locate and validate one entry's data.

    Returns (raw_compressed, content, crc, end_offset, note). content is None when
    the entry cannot be verified; note explains why.
    """
    if method not in (0, 8):
        return None, None, 0, dstart, f"unsupported compression method {method}"

    if not flags & 0x08:
        # Sizes known from the local header: validate against the CRC stored there.
        if dstart + csize_hdr > len(data):
            return None, None, 0, dstart, "data truncated (csize beyond end of file)"
        raw = data[dstart:dstart + csize_hdr]
        if method == 0:
            content = raw
            consumed = len(raw)
        else:
            content, consumed = _inflate_exact(raw)
            if content is None:
                return None, None, 0, dstart, "deflate stream incomplete or corrupt"
        crc = zlib.crc32(content) & 0xFFFFFFFF
        if crc != crc_hdr:
            return None, None, 0, dstart + consumed, "CRC mismatch"
        return raw[:consumed], content, crc, dstart + consumed, ""

    # Bit 3 set: sizes and CRC follow the data in a data descriptor.
    if method == 8:
        content, consumed = _inflate_exact(data[dstart:])
        if content is None:
            return None, None, 0, dstart, "deflate stream incomplete or corrupt"
        raw = data[dstart:dstart + consumed]
        desc_at = dstart + consumed
        crc = zlib.crc32(content) & 0xFFFFFFFF
        end = _skip_descriptor(data, desc_at, crc, len(raw), len(content))
        return raw, content, crc, end, ""

    # Stored + descriptor: search for a descriptor whose csize matches and whose CRC is right.
    search = dstart
    while True:
        p = data.find(DESC_SIG, search)
        if p == -1 or p > len(data) - 16:
            break
        raw = data[dstart:p]
        content = raw
        crc = zlib.crc32(content) & 0xFFFFFFFF
        desc_crc, desc_csize = struct.unpack("<II", data[p + 4:p + 12])
        if desc_crc == crc and desc_csize == len(raw):
            return raw, content, crc, p + 16, ""
        search = p + 1
    return None, None, 0, dstart, "stored entry with data descriptor not verifiable"


def _skip_descriptor(data: bytes, at: int, crc: int, csize: int, usize: int) -> int:
    """Return the offset after the data descriptor at `at` (with or without signature)."""
    if data[at:at + 4] == DESC_SIG and at + 16 <= len(data):
        return at + 16
    if at + 12 <= len(data):
        return at + 12
    return len(data)


def repair_zip(data: bytes) -> tuple[Optional[bytes], list[str]]:
    """Rebuild the central directory from local file headers.

    Works when the end-of-central-directory record is missing or corrupt,
    which is typical for partially deleted or truncated archives. Only entries
    whose content verifies (CRC and size) are written to the output.
    """
    actions: list[str] = []
    entries: list[dict] = []
    pos = data.find(LFH)
    if pos == -1:
        return None, ["no ZIP local file headers found"]
    if pos:
        actions.append("removed garbage before first local header")

    while pos != -1 and pos + 30 <= len(data):
        if data[pos:pos + 4] != LFH:
            pos = data.find(LFH, pos + 1)
            continue
        flags, method, dos_time, dos_date, crc_hdr, csize_hdr, usize_hdr, nlen, xlen = struct.unpack(
            "<HHHHIIIHH", data[pos + 6:pos + 30])
        name = data[pos + 30:pos + 30 + nlen]
        dstart = pos + 30 + nlen + xlen
        raw, content, crc, end, note = _read_entry(data, dstart, method, flags, crc_hdr, csize_hdr)
        label = name.decode("utf-8", "replace")
        if content is None:
            actions.append(f"dropped entry {label!r}: {note}")
        else:
            entries.append({
                "name": name,
                "method": method,
                "flags": flags & ~0x08,
                "crc": crc,
                "csize": len(raw),
                "usize": len(content),
                "time": dos_time,
                "date": dos_date,
                "offset": None,
                "raw": raw,
                "local": data[pos:dstart],
            })
        nxt = data.find(LFH, max(end, pos + 4))
        pos = nxt

    if not entries:
        return None, actions + ["no verifiable ZIP entries"]

    out = bytearray()
    cd = bytearray()
    for e in entries:
        e["offset"] = len(out)
        lh = bytearray(e["local"])
        # Rewrite local header: no data descriptor, correct sizes and CRC.
        struct.pack_into("<H", lh, 6, e["flags"])
        struct.pack_into("<I", lh, 14, e["crc"])
        struct.pack_into("<I", lh, 18, e["csize"])
        struct.pack_into("<I", lh, 22, e["usize"])
        out += lh + e["raw"]
        cd += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, e["flags"], e["method"],
                          e["time"], e["date"], e["crc"], e["csize"], e["usize"],
                          len(e["name"]), 0, 0, 0, 0, 0, e["offset"])
        cd += e["name"]
    cd_offset = len(out)
    out += cd
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(cd), cd_offset, 0)
    actions.append(f"rebuilt central directory with {len(entries)} verified entr{'y' if len(entries) == 1 else 'ies'}")
    return bytes(out), actions


# ---------- SQLite ----------

def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_python_copy(path: str, out_path: str, actions: list[str]) -> bool:
    """Copy readable tables with the Python driver, keeping schema and whatever rows are readable."""
    src_uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    src = sqlite3.connect(src_uri, uri=True)
    dst = sqlite3.connect(out_path)
    copied = 0
    try:
        tables = src.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for name, sql in tables:
            try:
                if sql:
                    dst.execute(sql)
                else:
                    cols = [r[1] for r in src.execute(f"PRAGMA table_info({_quote_ident(name)})")]
                    dst.execute(f"CREATE TABLE {_quote_ident(name)} ({', '.join(_quote_ident(c) for c in cols)})")
            except sqlite3.Error as exc:
                actions.append(f"table {name} schema unreadable: {exc}")
                continue
            cols = [r[1] for r in dst.execute(f"PRAGMA table_info({_quote_ident(name)})")]
            placeholders = ",".join("?" * len(cols))
            inserted = 0
            partial = False
            try:
                cursor = src.execute(f"SELECT * FROM {_quote_ident(name)}")
                batch = []
                for row in cursor:
                    batch.append(row)
                    if len(batch) >= 500:
                        dst.executemany(f"INSERT INTO {_quote_ident(name)} VALUES ({placeholders})", batch)
                        inserted += len(batch)
                        batch = []
                if batch:
                    dst.executemany(f"INSERT INTO {_quote_ident(name)} VALUES ({placeholders})", batch)
                    inserted += len(batch)
            except sqlite3.Error as exc:
                partial = True
                actions.append(f"table {name}: stopped after {inserted} row(s): {exc}")
            if inserted or not partial:
                copied += 1
            if partial:
                dst.commit()
        dst.commit()
    finally:
        src.close()
        dst.close()
    actions.append(f"copied {copied} table(s) with Python fallback")
    return copied > 0


def repair_sqlite(path: str, out_path: str) -> tuple[bool, list[str]]:
    """Recover readable tables from a corrupt SQLite database.

    Writes to a temporary file first, so a failed attempt never leaves a broken output.
    """
    actions: list[str] = []
    out_dir = os.path.dirname(os.path.abspath(out_path))
    fd, tmp_out = tempfile.mkstemp(suffix=".sqlite", dir=out_dir)
    os.close(fd)
    os.remove(tmp_out)  # sqlite3 creates the file itself
    try:
        sqlite_cli = shutil.which("sqlite3")
        if sqlite_cli:
            with tempfile.TemporaryDirectory() as tmp:
                dump = os.path.join(tmp, "dump.sql")
                with open(dump, "wb") as fh:
                    rc = subprocess.run([sqlite_cli, path, ".recover"], stdout=fh,
                                        stderr=subprocess.DEVNULL, check=False).returncode
                if rc == 0 and os.path.getsize(dump) > 0:
                    with open(dump, "rb") as fh:
                        rc2 = subprocess.run([sqlite_cli, tmp_out], stdin=fh, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, check=False).returncode
                    if rc2 == 0 and os.path.exists(tmp_out):
                        os.replace(tmp_out, out_path)
                        actions.append("recovered with sqlite3 .recover")
                        return True, actions
                actions.append("sqlite3 .recover failed; trying Python fallback")
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)

        ok = _sqlite_python_copy(path, tmp_out, actions)
        if ok:
            os.replace(tmp_out, out_path)
            return True, actions
        actions.append("no readable tables")
        return False, actions
    except sqlite3.Error as exc:
        return False, actions + [f"sqlite error: {exc}"]
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass


# ---------- dispatch ----------

_REPAIRERS: dict[str, Callable[[bytes], tuple[Optional[bytes], list[str]]]] = {
    ".png": repair_png,
    ".jpg": repair_jpeg,
    ".jpeg": repair_jpeg,
    ".pdf": repair_pdf,
    ".zip": repair_zip,
    ".docx": repair_zip,
    ".xlsx": repair_zip,
    ".pptx": repair_zip,
    ".apk": repair_zip,
}


def _unique_out(out_dir: str, base: str) -> str:
    candidate = os.path.join(out_dir, "repaired_" + base)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(base)
    n = 1
    while True:
        candidate = os.path.join(out_dir, f"repaired_{stem}_{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def repair_file(path: str, out_dir: str) -> RepairResult:
    """Repair one damaged file and write the result into out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    base = os.path.basename(path)
    out_path = _unique_out(out_dir, base)

    if ext in (".sqlite", ".db", ".sqlite3"):
        ok, actions = repair_sqlite(path, out_path)
        return RepairResult(path, out_path if ok else None, ok, actions)

    repairer = _REPAIRERS.get(ext)
    if repairer is None:
        return RepairResult(path, None, False, [f"no repairer for extension {ext!r}"])

    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return RepairResult(path, None, False, [f"cannot read file: {exc}"])
    fixed, actions = repairer(data)
    if fixed is None:
        return RepairResult(path, None, False, actions)
    with open(out_path, "wb") as fh:
        fh.write(fixed)
    return RepairResult(path, out_path, True, actions)
