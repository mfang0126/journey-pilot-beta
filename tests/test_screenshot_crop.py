"""observe screenshot can drop the measured status bar, without extra dependencies."""
import base64
import io
import json
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path

from journey_pilot import adapter

W, H, BPP = 7, 12, 4


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def make_png():
    """Pixels p(x, y, c); every row uses a different PNG filter (0..4, cycling)."""
    pix = [[[(x * 31 + y * 17 + c * 5) % 256 for c in range(BPP)] for x in range(W)] for y in range(H)]
    out, prev = b"", bytes(W * BPP)
    for y, row in enumerate(pix):
        cur, kind, line = bytes(v for px in row for v in px), y % 5, bytearray()
        for i, v in enumerate(cur):
            a = cur[i - BPP] if i >= BPP else 0
            b, c = prev[i], (prev[i - BPP] if i >= BPP else 0)
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            guess = (0, a, b, (a + b) // 2, a if pa <= pb and pa <= pc else b if pb <= pc else c)[kind]
            line.append((v - guess) & 255)
        out += bytes([kind]) + bytes(line)
        prev = cur
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(out)) + chunk(b"IEND", b""))
    return png, pix


def decode(png):
    pos, idat = 8, b""
    while pos < len(png):
        size, kind = struct.unpack(">I4s", png[pos:pos + 8])
        if kind == b"IHDR":
            w, h = struct.unpack(">II", png[pos + 8:pos + 16])
        if kind == b"IDAT":
            idat += png[pos + 8:pos + 8 + size]
        pos += 12 + size
    raw, rows, prev = zlib.decompress(idat), [], bytes(w * BPP)
    for y in range(h):
        s = y * (w * BPP + 1)
        kind, line = raw[s], bytearray(raw[s + 1:s + 1 + w * BPP])
        for i in range(len(line)):
            a = line[i - BPP] if i >= BPP else 0
            b, c = prev[i], (prev[i - BPP] if i >= BPP else 0)
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            line[i] = (line[i] + (0, a, b, (a + b) // 2, a if pa <= pb and pa <= pc else b if pb <= pc else c)[kind]) & 255
        rows.append(bytes(line))
        prev = bytes(line)
    return w, h, rows


class Crop(unittest.TestCase):
    def run_script(self, script, dumpsys):
        png, pix = make_png()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.png"
            path.write_bytes(b"warning\n" + png)
            out = io.StringIO()
            with redirect_stdout(out):
                exec(script, {"screenshot": lambda: str(path), "shell": lambda cmd: dumpsys})
        return json.loads(out.getvalue()), pix

    def test_crop_keeps_every_pixel_below_the_measured_bar(self):
        bar = "InsetsSource type=statusBars frame=[0,0][1200,5] visible=true"
        result, pix = self.run_script(adapter.OBSERVE_SCREENSHOT_NO_STATUS_BAR, bar)
        w, h, rows = decode(base64.b64decode(result["image_base64"]))
        self.assertEqual((w, h), (W, H - 5))
        self.assertEqual(rows, [bytes(v for px in row for v in px) for row in pix[5:]])

    def test_unmeasurable_bar_returns_no_image(self):
        result, _ = self.run_script(adapter.OBSERVE_SCREENSHOT_NO_STATUS_BAR, "nothing here")
        self.assertFalse(result["ok"])
        self.assertNotIn("image_base64", result)

    def test_default_screenshot_is_unchanged(self):
        result, _ = self.run_script(adapter.OBSERVE_SCREENSHOT, "")
        self.assertEqual(decode(base64.b64decode(result["image_base64"]))[1], H)


if __name__ == "__main__":
    unittest.main()
