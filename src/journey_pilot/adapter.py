"""Phone adapter: read-only status first, actions later.

Status is the one command that must work even when the phone is locked, so it
never refuses: it reports what it can see and says ``ready: false`` with a
reason. Device enumeration is plain adb; anything that needs the harness goes
through ``phone-harness`` so the same code path records actions later.

The harness's own lock verdict is not trusted (measured 2026-10-20: doctor said
"locked" while the accessibility tree was readable seconds later). Readiness is
decided by an actual read.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import time
from contextvars import ContextVar
from typing import Any

# Per CLI invocation, including readiness and terminal/checkpoint probes.
call_metrics = ContextVar("harness_call_metrics", default=None)

ADB = os.environ.get("ADB_BIN") or shutil.which("adb") or "adb"
HARNESS = os.environ.get("PHONE_HARNESS_BIN") or shutil.which("phone-harness") or ""

_DEVICE_LINE = re.compile(
    r"^(?P<serial>\S+)\s+(?P<state>device|offline|unauthorized)\b(?P<rest>.*)$"
)


def _run(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def list_devices() -> list[dict[str, str]]:
    """Parse ``adb devices -l``. Enumeration only, no control."""
    try:
        out = _run([ADB, "devices", "-l"]).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    devices = []
    for line in out.splitlines():
        match = _DEVICE_LINE.match(line.strip())
        if not match:
            continue
        rest = match.group("rest")
        model = re.search(r"model:(\S+)", rest)
        product = re.search(r"product:(\S+)", rest)
        devices.append({
            "serial": match.group("serial"),
            "state": match.group("state"),
            "model": model.group(1) if model else None,
            "product": product.group(1) if product else None,
        })
    return devices


def _adb(serial: str, *args: str, timeout: float = 15.0) -> str:
    try:
        proc = _run([ADB, "-s", serial, *args], timeout=timeout)
        return proc.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _screen(serial: str) -> dict[str, Any]:
    """Screen size, wakefulness and keyguard flags from read-only dumps."""
    size_raw = _adb(serial, "shell", "wm", "size")
    size = None
    match = re.search(r"(\d+)x(\d+)", size_raw)
    if match:
        size = {"width": int(match.group(1)), "height": int(match.group(2))}

    power = _adb(serial, "shell", "dumpsys", "power")
    wake = None
    match = re.search(r"mWakefulness=(\w+)", power)
    if match:
        wake = match.group(1)

    policy = _adb(serial, "shell", "dumpsys", "window", "policy")
    keyguard = {
        "showing": _flag(policy, "showing"),
        "input_restricted": _flag(policy, "inputRestricted"),
        "secure": _flag(policy, "secure"),
        "bouncer": _flag(policy, "BouncerShowing"),
    }

    return {"size": size, "wakefulness": wake, "keyguard": keyguard,
            "foreground": _focus(serial)}


FOCUS_GREP = "dumpsys window | grep -E 'Display: mDisplayId=|mCurrentFocus='"


def _main_focus(text):
    # Display 0's mCurrentFocus, null included: another display's window
    # (Xiaomi rear screen) never stands in for it. Only output with no
    # display headers falls back to the first non-null focus. Also runs,
    # as source, inside harness scripts (FOCUS_PRELUDE).
    import re
    display, focuses = None, []
    for line in text.splitlines():
        if "mCurrentFocus=" in line:
            focuses.append((display, line.strip()))
            continue
        shown = re.search(r"mDisplayId=(\d+)", line)
        if shown:
            display = int(shown.group(1))
    if any(d is not None for d, _ in focuses):
        return next((f for d, f in focuses if d == 0), "")
    real = [f for _, f in focuses if "mCurrentFocus=null" not in f]
    return (real or [f for _, f in focuses] or [""])[0]


FOCUS_PRELUDE = "\n" + inspect.getsource(_main_focus) + f'''

def _focus_line():
    return _main_focus(shell({FOCUS_GREP!r}))
'''


def _focus(serial: str) -> str | None:
    """Display 0's focused window as ``package/activity``, or None when unreadable."""
    match = re.search(r"mCurrentFocus=Window\{[0-9a-f]+ u0 ([^}]+)\}",
                      _main_focus(_adb(serial, "shell", FOCUS_GREP)))
    return match.group(1).strip() if match else None


def foreground_package(serial: str) -> str | None:
    """Foreground package for per-package pacing; None when unreadable.

    Only a ``package/activity`` focus names a package; window titles such as
    ``PopupWindow:abc`` or ``StatusBar`` are unknown, not a new package.
    """
    match = re.fullmatch(r"([A-Za-z]\w*(?:\.[A-Za-z]\w*)+)/\S+", _focus(serial) or "")
    return match.group(1) if match else None


def _flag(text, name):
    # Only a complete true/false is a value; a missing, empty, truncated or
    # odd one ("unknown", "tru", "name: true") is None, never False. Also
    # runs, as source, inside the wake script.
    import re
    match = re.search(rf"{name}=([^\s,]*)", text)
    return {"true": True, "false": False}.get(match.group(1).lower()) if match else None


_CREDENTIAL_FOCUS = (
    "keyguardbouncer", "confirmdevicecredential", "simunlock", "simpuk",
    "keyguardsimpin",
)


# AOSP keyguard input views; any of them on screen means a credential is asked.
_CREDENTIAL_TREE = ("id/pinentry", "id/passwordentry", "id/lockpatternview")

# The dispatch gate stays wider: any keyguard container (keyguard_pin_view,
# keyguard_status_view, ...) in the tree stops a dispatch, even when the global
# flag says unlocked. Superset of _CREDENTIAL_TREE.
_DISPATCH_TREE = ("pinentry", "passwordentry", "lockpattern", "keyguard")


# Live stop before a raw ``shell`` dispatch: Phone Harness gates ui()/tap on the
# lock, but not ``shell``, and effect sampling only stops on a lock refusal. Reuses the
# shared ``_flag`` (strict true/false) and ``_focus_line`` (display 0) parsers.
DISPATCH_GATE = "\n" + inspect.getsource(_flag) + r'''

def _dispatch_gate(idle_tree_ok=False):
    """None only on explicit evidence the phone is unlocked; never types anything.

    A display-0 credential window wins over a global showing=false, and any
    unknown (flags, display-0 focus, tree) stops the dispatch. ``idle_tree_ok``
    accepts one tree failure, "could not get idle state", on screens that never
    idle (video, compass): launch_activity is how an agent leaves one. The
    flags can describe another display, so display 0 must then show a named
    package/activity; NotificationShade and StatusBar titles have no slash.
    """
    def stop(reason, human=False):
        return {"ok": False, "code": "locked", "reason": "locked: " + reason,
                "requires_human": human}
    focus = _focus_line()
    if any(m in focus.lower() for m in __CREDENTIAL_FOCUS__):
        return stop("credential UI is focused on display 0; a person must unlock", True)
    # A present field is authoritative: an invalid value is unknown, with no
    # fall-through to another source.
    guard = shell("dumpsys window | grep -m1 isKeyguardShowing") or ""
    if "isKeyguardShowing=" in guard:
        showing = _flag(guard, "isKeyguardShowing")
    else:
        policy = shell("dumpsys window policy") or ""
        showing = _flag(policy, "KeyguardShowing" if "KeyguardShowing=" in policy else "showing")
    if showing is True:
        return stop("keyguard is showing")
    if showing is None:
        return stop("keyguard state unknown")
    if not focus or "mCurrentFocus=null" in focus:
        return stop("display 0 focus unknown")
    try:
        nodes = _observe()
    except Exception as exc:
        if (idle_tree_ok and "could not get idle state" in str(exc)
                and "/" in focus and "keyguard" not in focus.lower()):
            return None
        return stop("tree unreadable before dispatch: %s" % str(exc)[:120])
    if not nodes:
        return stop("tree is empty before dispatch")
    ids = " ".join(str(n.get("id") or "") for n in nodes).lower()
    if any(m in ids for m in __DISPATCH_TREE__):
        return stop("lock or credential view is in the tree; a person must unlock", True)
    return None
'''.replace("__CREDENTIAL_FOCUS__", repr(_CREDENTIAL_FOCUS)).replace(
    "__DISPATCH_TREE__", repr(_DISPATCH_TREE))


def lock_assessment(screen: dict[str, Any]) -> dict[str, Any]:
    """Describe a lock without claiming it is open.

    ``secure=false`` never means unlocked: only a later successful tree read
    does. It only says a plain wake and swipe may be attempted. ``requires_human``
    is set only when the device actually reports a bouncer/credential UI, and
    then whatever the global ``showing`` flag says: on a dual-screen phone that
    flag can describe another display, while focus is display 0's.
    """
    keyguard = screen.get("keyguard") or {}
    showing = keyguard.get("showing")
    secure = keyguard.get("secure")
    focus = str(screen.get("foreground") or "").lower()
    credential = (keyguard.get("bouncer") is True
                  or any(marker in focus for marker in _CREDENTIAL_FOCUS))
    if credential:
        kind = "secure"
    elif showing is not True:
        # No display 0 window is unknown, never "none": flags alone do not unlock.
        kind = "none" if showing is False and focus else "unknown"
    elif secure is True:
        kind = "secure"
    elif secure is False:
        kind = "nonsecure"
    else:
        kind = "unknown"
    return {
        "showing": showing,
        "secure": secure,
        "input_restricted": keyguard.get("input_restricted"),
        "bouncer": keyguard.get("bouncer"),
        "credential_visible": credential,
        "requires_human": credential,
        "ordinary_wake_allowed": showing is True and not credential,
        "kind": kind,
    }


def ordinary_wake_allowed(screen: dict[str, Any]) -> bool:
    """True only when status measured a plain keyguard that wake may clear."""
    return (screen.get("lock") or {}).get("ordinary_wake_allowed") is True


def harness_read(script: str, serial: str | None = None, timeout: float = 60.0) -> dict[str, Any]:
    """Measure every harness attempt; scripts and output never enter the meter."""
    started = time.monotonic()
    try:
        return _harness_read(script, serial, timeout)
    finally:
        metrics = call_metrics.get()
        if metrics is not None:
            metrics["calls"] += 1
            metrics["duration_ms"] += (time.monotonic() - started) * 1000


def _harness_read(script: str, serial: str | None = None, timeout: float = 60.0) -> dict[str, Any]:
    """Run one ``phone-harness`` script and return its last JSON line."""
    if not HARNESS:
        return {"error": "phone-harness not found on PATH"}
    env = {
        **os.environ,
        "PHONE_HARNESS_PLATFORM": "android",
        "PHONE_HARNESS_TELEMETRY": "0",
    }
    if serial:
        env["ANDROID_SERIAL"] = serial
    try:
        proc = subprocess.run(
            [HARNESS], input=script, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"error": "phone-harness timed out"}
    except OSError as exc:
        return {"error": f"phone-harness failed to start: {exc}"}
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        return {"error": tail[-1] if tail else f"exit {proc.returncode}"}
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"error": "no JSON in harness output"}


PROBE = '''
# task: readiness probe
# step: read the accessibility tree once; success means the device is usable
import json
try:
    nodes = ui()
    labels = [n.get("text") or n.get("desc") or "" for n in nodes]
    print(json.dumps({"ready": True, "nodes": len(nodes),
                      "sample": [x for x in labels if x][:5]}))
except Exception as exc:
    print(json.dumps({"ready": False, "reason": str(exc)[:160]}))
'''

GEOMETRY = '''
def _geometry():
    import re
    size = shell('wm size')
    rotation = shell('dumpsys input')
    matches = re.findall(r'(\\d+)x(\\d+)', size)
    match = re.search(r'SurfaceOrientation:\\s*(\\d)', rotation) or re.search(r'Viewport INTERNAL:.*?orientation=(\\d)', rotation)
    if not matches or not match:
        return None
    w, h = map(int, matches[-1])
    orientation = int(match.group(1))
    if orientation % 2:
        w, h = h, w
    return {"width": w, "height": h, "orientation": orientation}
'''

OBSERVE_TREE = GEOMETRY + '''
# task: observe the current screen
# step: read the accessibility tree and return it verbatim
import json
try:
    nodes = ui()
    print(json.dumps({"ok": True, "nodes": nodes, "geometry": _geometry()}))
except Exception as exc:
    print(json.dumps({"ok": False, "reason": str(exc)[:200]}))
'''

_SCREENSHOT = '''
# task: observe the current screen
# step: capture one screenshot and return its path and size
import json, os, base64, re, struct, zlib
CROP_STATUS_BAR = __CROP__

def _status_bar_px():
    # The status bar inset is measured, never guessed; no measurement means no image.
    match = re.search(r'type=statusBars frame=\\[0,0\\]\\[\\d+,(\\d+)\\]', shell('dumpsys window'))
    if not match or not int(match.group(1)):
        raise ValueError("status bar height could not be measured")
    return int(match.group(1))

def _crop_top(png, rows):
    # Stdlib PNG crop (8-bit RGB/RGBA, not interlaced). Only rows up to the cut
    # are unfiltered; the first kept row is rewritten raw so the rest stay valid.
    pos, idat, ihdr = 8, b"", None
    while pos < len(png):
        size, kind = struct.unpack(">I4s", png[pos:pos + 8])
        body = png[pos + 8:pos + 8 + size]
        if kind == b"IHDR":
            ihdr = body
        elif kind == b"IDAT":
            idat += body
        pos += 12 + size
    width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", ihdr)
    if depth != 8 or color not in (2, 6) or interlace or not 0 < rows < height:
        raise ValueError("unsupported PNG for status bar crop")
    bpp = 3 if color == 2 else 4
    stride = width * bpp
    raw = zlib.decompress(idat)
    prev = bytearray(stride)
    for y in range(rows + 1):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
    rest = raw[(rows + 1) * (stride + 1):]
    data = zlib.compress(b"\\x00" + bytes(prev) + rest)
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", width, height - rows, depth, color, 0, 0, 0)
    return b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", header) + chunk(b"IDAT", data) + chunk(b"IEND", b"")

path = None
try:
    path = screenshot()
    with open(path, 'rb') as image:
        data = image.read()
    # Some macOS multi-display setups prepend a warning line before the PNG.
    start = data.find(b"\\x89PNG\\r\\n\\x1a\\n")
    if start < 0:
        raise ValueError("screenshot bytes do not contain a PNG signature")
    data = data[start:]
    if CROP_STATUS_BAR:
        data = _crop_top(data, _status_bar_px())
    print(json.dumps({"ok": True, "image_base64": base64.b64encode(data).decode(),
                      "bytes": len(data)}))
except Exception as exc:
    print(json.dumps({"ok": False, "reason": str(exc)[:200]}))
finally:
    if path is not None:
        os.unlink(path)
'''
OBSERVE_SCREENSHOT = _SCREENSHOT.replace("__CROP__", "False")
OBSERVE_SCREENSHOT_NO_STATUS_BAR = _SCREENSHOT.replace("__CROP__", "True")

OBSERVE_PIXELS = GEOMETRY + '''
# task: observe visible controls without an accessibility label
# step: normalize PNG bytes and OCR with explicitly configured languages
import json
from pathlib import Path
from phone_harness import ocr as vision
import Vision
from Foundation import NSURL

def _recognize_zh(path, window):
    # Geometry mirrors phone_harness.ocr.recognize (read-only upstream, no
    # language hook); the request here sets zh-Hans explicitly because the
    # upstream default returned mojibake on Chinese screenshots.
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(path), {})
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(['zh-Hans', 'en-US'])
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f'Vision OCR failed: {err}')
    img_w, img_h = vision.image_size(path)
    sx = window['w'] / img_w
    sy = window['h'] / img_h
    out = []
    for obs in request.results() or []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        bb = obs.boundingBox()
        px = bb.origin.x * img_w
        py_top = (1.0 - bb.origin.y - bb.size.height) * img_h
        pw = bb.size.width * img_w
        ph = bb.size.height * img_h
        out.append({'text': str(cand[0].string()),
                    'confidence': round(float(cand[0].confidence()), 3),
                    'x': round(window['x'] + (px + pw / 2) * sx, 1),
                    'y': round(window['y'] + (py_top + ph / 2) * sy, 1),
                    'w': round(pw * sx, 1), 'h': round(ph * sy, 1)})
    return out

path = None
try:
    path = Path(screenshot())
    data = path.read_bytes()
    path.write_bytes(data[data.index(b"\\x89PNG\\r\\n\\x1a\\n"):])
    w, h = vision.image_size(str(path))
    nodes = _recognize_zh(str(path), {"x": 0, "y": 0, "w": w, "h": h})
    print(json.dumps({"ok": True, "nodes": nodes, "geometry": _geometry()}))
except Exception as exc:
    print(json.dumps({"ok": False, "reason": str(exc)[:200]}))
finally:
    if path is not None:
        path.unlink(missing_ok=True)
'''

# Tree quality gate: decides whether a tree observe also needs pixel OCR.
# Design idea borrowed from Mika's TreeQuality; code and thresholds are ours.
TREE_MIN_USEFUL = 5   # nodes with visible text/desc or clickable needed for "good"
TREE_BANDS = 4        # screen height split into equal horizontal bands
TREE_MIN_BANDS = 3    # bands holding a useful node needed for "good"


def tree_quality(nodes: list, geometry: dict | None) -> str:
    """``good`` skips OCR; ``partial`` (sparse or missing regions) adds OCR;
    ``unusable`` (no tree at all) needs OCR. A failed tree read is unusable too."""
    if not nodes:
        return "unusable"
    useful = [n for n in nodes if isinstance(n, dict)
              and (str(n.get("text") or n.get("desc") or "").strip() or n.get("clickable"))]
    if len(useful) < TREE_MIN_USEFUL:
        return "partial"
    height = (geometry or {}).get("height")
    ys = [n["y"] for n in useful if isinstance(n.get("y"), (int, float)) and height and 0 <= n["y"] < height]
    # ponytail: band coverage is skipped without geometry or node centres; count alone decides.
    if ys and len({int(y * TREE_BANDS / height) for y in ys}) < TREE_MIN_BANDS:
        return "partial"
    return "good"


def ocr_supplement(tree_nodes: list, ocr_nodes: list) -> list:
    """OCR lines the tree does not already carry, each marked ``source: "ocr"``."""
    def norm(value):
        return "".join(str(value or "").split())
    known = [norm(n.get("text")) + "\n" + norm(n.get("desc")) for n in tree_nodes if isinstance(n, dict)]
    return [{**n, "source": "ocr"} for n in ocr_nodes
            if isinstance(n, dict) and norm(n.get("text"))
            and not any(norm(n.get("text")) in k for k in known)]


# Actions that touch the screen; wait and read_field change nothing by design.
EFFECT_ACTIONS = ("tap", "tap_xy", "input_text", "key", "scroll", "launch_app", "launch_activity",
                  "share_text")

BLOCK_SIGNALS = ('操作频繁', '请求过于频繁', '访问频繁', '滑块验证', '安全验证', '人机验证',
                 'too many requests', 'rate limit', 'verify you are human', 'captcha')
# "You hit the limit" notices. Unlike BLOCK_SIGNALS they count only when they
# appear after an action: the same text already on screen before it is a label.
LIMIT_SIGNALS = ('已达上限', '达到上限', '到达上限', '已到上限', '超出上限', '超过上限',
                 '次数已用完', '次数用完', '额度已用完', '已达今日上限',
                 'limit reached', 'reached the limit', 'reached your limit',
                 'daily limit', 'quota exceeded')

LOCATOR_PRELUDE = '''
import json


def _texts(nodes):
    return [str(n.get('text') or n.get('desc') or '') for n in nodes]


def _spans(text, part):
    out, i = [], text.find(part) if part else -1
    while i >= 0:
        out.append((i, i + len(part)))
        i = text.find(part, i + 1)
    return out


def _limit_signal(text, typed):
    # A limit phrase counts unless that very occurrence lies inside an echo of
    # the text this action typed (the edited field, a suggestion row).
    # ponytail: a real notice whose phrase sits only inside the typed string
    # is indistinguishable from an echo and is missed.
    low = text.lower()
    echoes = _spans(low, (typed or "").strip().lower())
    return any(not any(a <= i and j <= b for a, b in echoes)
               for s in __LIMIT__ for i, j in _spans(low, s))


def _blocked(nodes, before=None, typed=None):
    # Verification blocks whenever visible; a limit notice only if it is new
    # since ``before`` (no snapshot, no limit verdict).
    texts = _texts(nodes)
    if any(s in t.lower() for t in texts for s in __SIGNALS__):
        return "verification"
    if before is not None:
        seen = set(_texts(before))
        if any(t not in seen and _limit_signal(t, typed) for t in texts):
            return "limit_notice"
    return None


def _foreground():
    import re
    focus = _focus_line()
    match = re.search(r"mCurrentFocus=Window\\{[0-9a-f]+ u\\d+ ([^/}\\s]+)", focus)
    return match.group(1) if match else None


def _unmet(nodes, exp, gone, app):
    out = []
    if exp and not _check(nodes, exp):
        out.append("expect")
    if gone and _check(nodes, gone):
        out.append("expect_gone")
    if app and _foreground() != app:
        out.append("expect_app")
    return out


def _match(nodes, loc):
    hits = []
    for n in nodes:
        if "resource_id" in loc and n.get("id") != loc["resource_id"]:
            continue
        if "text" in loc and (n.get("text") or "").strip() != loc["text"]:
            continue
        if "text_contains" in loc and loc["text_contains"] not in (n.get("text") or ""):
            continue
        if "description" in loc and (n.get("desc") or "").strip() != loc["description"]:
            continue
        if "class" in loc and n.get("class") != loc["class"]:
            continue
        hits.append(n)
    return hits


def _check(nodes, spec):
    if "element_exists" in spec:
        loc = spec["element_exists"]
        return len(_match(nodes, loc)) > 0
    if "text_equals" in spec:
        want = spec["text_equals"]
        return any((n.get("text") or "").strip() == want for n in nodes)
    return False
'''.replace('__SIGNALS__', repr(BLOCK_SIGNALS)).replace('__LIMIT__', repr(LIMIT_SIGNALS)) + FOCUS_PRELUDE

ACTION_TEMPLATE = '''
# task: {task}
# step: {step}
import json, time
{prelude}
result = {{"ok": False}}
_dispatched = False
metrics = {{"tree_reads": 0, "tree_ms": 0.0, "explicit_wait_ms": 0.0}}
started = time.monotonic()

def _observe():
    begin = time.monotonic()
    metrics["tree_reads"] += 1
    try:
        return ui()
    finally:
        metrics["tree_ms"] += (time.monotonic() - begin) * 1000

def _sleep(seconds):
    begin = time.monotonic()
    time.sleep(seconds)
    metrics["explicit_wait_ms"] += (time.monotonic() - begin) * 1000

def _quiet(read):
    try:
        return read()
    except Exception as exc:
        # Before dispatch a lock refusal is the answer, not a missing sample.
        if not _dispatched and "phone is locked" in str(exc):
            raise
        return None

def _focus():
    return _focus_line()[:200]

try:
    loc = {locator}
    target = None
    _want, _got = {want}, None
    locator_index = None
    _effect = {effect}
    _before = _focus_before = after = None
    if loc:
        nodes = _observe()
        _before = nodes
        locator_index = 0
        hits = _match(nodes, loc)
        if not hits:
            for locator_index, alternative in enumerate({alternatives}, 1):
                hits = _match(nodes, alternative)
                if hits:
                    break
        if _blocked(nodes):
            result = {{"ok": False, "code": "platform_blocked"}}
        elif len(hits) == 0:
            result = {{"ok": False, "code": "locator_missing", "nodes": len(nodes),
                       "labels": [n.get("text") or n.get("desc") for n in nodes
                                  if (n.get("text") or n.get("desc"))][:12]}}
        elif len(hits) > 1:
            result = {{"ok": False, "code": "ambiguous_target", "count": len(hits),
                       "labels": [(h.get("id"), h.get("text")) for h in hits][:6]}}
        else:
            target = hits[0]
    if not result.get("code"):
        {geometry_guard}
    if not result.get("code"):
        exp, gone, app = {expect}, {expect_gone}, {expect_app}
        before = None
        if exp or gone or app:
            # The pre-action screen a later limit notice must be new against.
            before = nodes if loc else _observe()
        if _effect:
            if _before is None:
                _before = before if before is not None else _quiet(_observe)
            _focus_before = _quiet(_focus)
            # The point actually touched; input_text re-reads and may move ``target``.
            _point = [target["x"], target["y"]] if target else {point}
        _dispatched = True
        {body}
        result = {{"ok": True, "action": "{action}", "value": (target.get("text") or target.get("desc")) if target else None}}
        if _want is not None and _got != _want:
            result = {{"ok": False, "code": "assertion_failed", "action": "{action}", "value": _got,
                       "reason": "field text after input did not match the expected text"}}
        if exp or gone or app:
            _sleep({settle_ms} / 1000.0)
            after = _observe()
            _polls = 0
            _deadline = time.monotonic() + {poll_ms} / 1000.0
            blocked = _blocked(after, before, {typed})
            unmet = [] if blocked else _unmet(after, exp, gone, app)
            while not blocked and unmet and time.monotonic() < _deadline:
                _sleep(min(0.3, max(0, _deadline - time.monotonic())))
                after = _observe()
                _polls += 1
                blocked = _blocked(after, before, {typed})
                unmet = [] if blocked else _unmet(after, exp, gone, app)
            result["settle_escalated"] = _polls
            if blocked:
                result["ok"] = False
                result["code"] = "platform_blocked"
                if blocked == "limit_notice":
                    result["limit_notice"] = True
                    result["reason"] = "a limit notice appeared after the action"
            elif unmet:
                result["ok"] = False
                result["code"] = "assertion_failed"
                result["unmet"] = unmet
                result["labels"] = [n.get("text") or n.get("desc") for n in after
                                    if (n.get("text") or n.get("desc"))][:12]
        if _effect:
            # Reads only; a failed read leaves that side unknown, never fails the input.
            if not (exp or gone or app):
                _sleep({settle_ms} / 1000.0)
                after = _quiet(_observe)
            result["_effect_input"] = {{
                "before": _before, "after": after, "focus_before": _focus_before,
                "focus_after": _quiet(_focus),
                "point": _point}}
except Exception as exc:
    # Only a gate refusal before the body proves nothing ran; shell() skips the
    # gate, so a refusal after dispatch stays action_failed (unknown effect).
    _refused = not _dispatched and "phone is locked" in str(exc)
    result = {{"ok": False, "code": "locked" if _refused else "action_failed",
               "reason": str(exc)[:200]}}
result["dispatched"] = _dispatched
result["metrics"] = {{**metrics, "script_ms": (time.monotonic() - started) * 1000}}
result["locator_index"] = locator_index
print(json.dumps(result))
'''


WAIT_KEYS = ("expect", "expect_gone", "expect_app")


def expect_check(step_id: str, action: dict, result: dict) -> dict | None:
    """The ``check`` event for an action's ``expect``; None if it never ran.

    ``expect_gone`` / ``expect_app`` share it: one bounded wait, one check.
    The action script evaluates them only after the input succeeded, so
    ok or ``assertion_failed`` means it ran; any other failure stopped earlier.
    """
    kind = action.get("type") or action.get("action")
    if (kind == "wake"  # WAKE_TEMPLATE never evaluates these waits
            or not any(action.get(k) for k in WAIT_KEYS)
            or not (result.get("ok") or result.get("code") == "assertion_failed")):
        return None
    return {"event": "check", "name": "expect", "step_id": step_id, "ok": bool(result.get("ok"))}


def _locator_literal(locator: dict | None) -> str:
    if not locator:
        return "None"
    from .journey import validate_locator
    return repr(validate_locator(locator))


WAKE_TEMPLATE = GEOMETRY + FOCUS_PRELUDE + "\n" + inspect.getsource(_flag) + r'''
# task: __TASK__
# step: __STEP__
import json, re, time

result = {"ok": False}
metrics = {"tree_reads": 0, "tree_ms": 0.0, "explicit_wait_ms": 0.0,
           "wake_attempts": 0, "swipes": 0}
started = time.monotonic()

def _sleep(seconds):
    begin = time.monotonic()
    time.sleep(seconds)
    metrics["explicit_wait_ms"] += (time.monotonic() - begin) * 1000

def _observe():
    begin = time.monotonic()
    metrics["tree_reads"] += 1
    try:
        return ui()
    finally:
        metrics["tree_ms"] += (time.monotonic() - begin) * 1000

class _Credential(Exception):
    pass

def _state():
    power = shell("dumpsys power | grep -m1 mWakefulness=")
    guard = shell("dumpsys window | grep -m1 isKeyguardShowing")
    policy = shell("dumpsys window policy")
    match = re.search(r"mWakefulness=(\w+)", power)
    showing = (_flag(guard, "isKeyguardShowing")
               if "isKeyguardShowing" in guard else _flag(policy, "KeyguardShowing"))
    # Display 0 only: the rear screen's null focus must not stand in for it.
    focus_text = _focus_line().strip()[:160] or None
    credential_focus = any(marker in (focus_text or "").lower() for marker in (
        "keyguardbouncer", "confirmdevicecredential", "simunlock", "simpuk",
        "keyguardsimpin"))
    return {
        "wakefulness": match.group(1) if match else None,
        "keyguard_showing": showing,
        "secure": _flag(policy, "KeyguardSecure"),
        "input_restricted": _flag(policy, "KeyguardInputRestricted"),
        "bouncer": _flag(policy, "BouncerShowing"),
        "credential_focus": credential_focus,
        "focus": focus_text,
        "focus_known": bool(focus_text) and "mCurrentFocus=null" not in focus_text,
    }

try:
    geometry = _geometry() or {"width": 1080, "height": 2400, "orientation": 0}
    state = _state()
    attempts = 0
    if state.get("wakefulness") != "Awake":
        shell("input keyevent 224")
        _sleep(1.0)
        state = _state()
    if state.get("keyguard_showing") is not False:
        # One swipe can only collapse the shade or hit the wake transition.
        # Three tries are still ordinary wake/swipe; no credential is typed.
        for attempts in range(1, 4):
            metrics["wake_attempts"] = attempts
            shell("input swipe %d %d %d %d 250" % (
                geometry["width"] // 2, int(geometry["height"] * 0.77),
                geometry["width"] // 2, int(geometry["height"] * 0.31)))
            metrics["swipes"] += 1
            _sleep(1.2)
            state = _state()
            if state.get("keyguard_showing") is False:
                break
    credential = (state.get("bouncer") is True
                  or state.get("credential_focus") is True)
    # A secure keyguard that survives an ordinary swipe is the only case that
    # needs a person. secure=false alone is never treated as unlocked.
    requires_human = credential or state.get("secure") is True
    wake = {"attempts": attempts, **state,
            "credential_visible": bool(credential),
            "requires_human": bool(requires_human)}
    if credential:
        # Display 0's bouncer/credential window wins over a global showing=false.
        result = {"ok": False, "code": "locked",
                  "reason": ("a PIN, password or pattern screen is showing; "
                             "a person must enter the PIN or password"),
                  "wake": {**wake, "verified_by": "credential_signal"}}
    elif state.get("keyguard_showing") is True:
        if requires_human:
            reason = ("secure lock screen is showing after ordinary wake and swipe; "
                      "a person must enter the PIN or password")
        else:
            reason = ("ordinary wake and swipe did not clear the non-secure keyguard "
                      "after %d attempt(s); the phone is still locked" % attempts)
        result = {"ok": False, "code": "locked", "reason": reason,
                  "wake": {**wake, "verified_by": "raw_keyguard_state"}}
    elif state.get("keyguard_showing") is not False:
        # Missing or unparsable flags are unknown, never unlocked.
        result = {"ok": False, "code": "locked",
                  "reason": ("keyguard state could not be read after ordinary wake and swipe; "
                             "cannot confirm the phone is unlocked"),
                  "wake": {**wake, "verified_by": "keyguard_state_unknown"}}
    elif not state.get("focus_known"):
        # Cleared flags with no display 0 window are unknown, never unlocked.
        result = {"ok": False, "code": "locked",
                  "reason": ("keyguard flags cleared but display 0 has no focused window; "
                             "cannot confirm the phone is unlocked"),
                  "wake": {**wake, "verified_by": "display0_focus_unknown"}}
    else:
        try:
            nodes = _observe()
            ids = " ".join(str(n.get("id") or "") for n in nodes).lower()
            if not nodes:
                raise RuntimeError("the accessibility tree is empty")
            if any(marker in ids for marker in __CREDENTIAL_TREE__):
                raise _Credential()
            result = {"ok": True, "action": "wake",
                      "value": "awake after %d swipe(s)" % attempts,
                      "wake": {**wake, "credential_visible": False,
                               "requires_human": False,
                               "verified_by": "accessibility_tree",
                               "nodes": len(nodes)}}
        except _Credential:
            result = {"ok": False, "code": "locked",
                      "reason": ("the screen shows a PIN, password or pattern entry; "
                                 "a person must enter the PIN or password"),
                      "wake": {**wake, "credential_visible": True, "requires_human": True,
                               "verified_by": "credential_tree"}}
        except Exception as exc:
            result = {"ok": False, "code": "locked",
                      "reason": ("keyguard flags cleared but the screen is not actually "
                                 "readable: %s" % str(exc)[:160]),
                      "wake": {**wake, "verified_by": "accessibility_tree_failed"}}
except Exception as exc:
    result = {"ok": False, "code": "action_failed", "reason": str(exc)[:200]}
result["metrics"] = {**metrics, "script_ms": (time.monotonic() - started) * 1000}
result["locator_index"] = None
print(json.dumps(result))
'''.replace("__CREDENTIAL_TREE__", repr(_CREDENTIAL_TREE))


def _launch_activity_body(action: dict) -> str:
    import shlex
    component = str(action.get("component") or "")
    data = action.get("data")
    if data:
        target = (f"-n {shlex.quote(component)} -d {shlex.quote(data)}"
                  if component else f"-a android.intent.action.VIEW -d {shlex.quote(data)}")
        # Chrome keeps one tab per application_id and navigates it on later launches.
        reuse = " --es com.android.browser.application_id journey-pilot" if action.get("reuse_tab") else ""
        command = f"am start {target}{reuse}"
    else:
        command = f"am start -n {shlex.quote(component)}"
    return f"shell({command!r})\n        time.sleep(1.5)"


def _share_text_body(action: dict) -> str:
    """ACTION_SEND text/plain to one package; the clipboard is never touched.

    ``adb shell`` hands the string to the device sh, so POSIX quoting keeps any
    text (Chinese, quotes, ``$``, newlines) one literal extra.
    """
    import shlex
    command = ("am start -a android.intent.action.SEND -t text/plain"
               f" --es android.intent.extra.TEXT {shlex.quote(action['text'])}"
               f" -p {shlex.quote(action['package'])}")
    # am prints "Error: ..." and exits 0 when nothing resolves; raise so it is not ok.
    return (f"_am = str(shell({ascii(command)}))\n"
            "        if 'Error' in _am:\n"
            "            raise RuntimeError(_am.strip()[:160])\n"
            "        time.sleep(1.5)")


def build_action_script(action: dict) -> str:
    """Generate the phone-harness script for one whitelisted action."""
    import math
    from .journey import JourneyError, validate_steps
    for key in ("x", "y", "seconds", "settle_ms", "poll_ms"):
        if key in action and (type(action[key]) not in (int, float)
                              or not math.isfinite(action[key]) or action[key] < 0):
            raise JourneyError("invalid_request", f"{key} needs a finite nonnegative number")
    if (action.get("type") or action.get("action")) == "tap_xy" and not {"x", "y"} <= set(action):
        raise JourneyError("invalid_request", "tap_xy requires explicit x and y")
    kind = action.get("type") or action.get("action")
    method = action.get("method", "adb")
    if "method" in action and (kind != "input_text" or method not in ("adb", "paste")):
        raise JourneyError("invalid_request", "method is only adb|paste on input_text")
    if kind == "input_text" and "text" in action and not isinstance(action["text"], str):
        raise JourneyError("invalid_request", "input_text text must be a string after parameter binding")
    if kind == "input_text" and method == "adb":
        text = action.get("text", "")
        if any(ord(char) > 127 for char in text):
            raise JourneyError(
                "unsupported_input",
                "adb input text cannot type non-ASCII; preload the exact text on the device clipboard and use method=paste")
    if kind == "input_text" and method == "paste" and not action.get("text"):
        raise JourneyError("invalid_request", "method=paste needs the expected text")
    if kind == "launch_activity":
        data = action.get("data")
        if data is not None and (not isinstance(data, str)
                                 or not data.startswith(("http://", "https://"))
                                 or len(data) > 2048
                                 or any(ord(char) > 127 or char in "\r\n" for char in data)):
            raise JourneyError("invalid_request",
                               "launch_activity data must be one ASCII http(s) URL")
        if "reuse_tab" in action and (action["reuse_tab"] is not True or not data):
            raise JourneyError("invalid_request", "reuse_tab is true only with launch_activity data")
        if not action.get("component") and not data:
            raise JourneyError("invalid_request",
                               "launch_activity needs component or data")
    spec = dict(action)
    if kind == "tap_xy":
        spec.pop("locator", None)  # provenance only; coordinates do not use a locator
    checked = validate_steps([{**spec, "action": kind,
                               "step_id": action.get("step_id", "action")}])[0]
    locator = "None" if kind == "tap_xy" else _locator_literal(action.get("locator"))
    alternatives = checked.get("fallback", {}).get("alternatives", [])
    task = action.get("task", "journey-pilot action")
    step = action.get("step_id", kind or "action")
    if kind == "wake":
        return (WAKE_TEMPLATE.replace("__TASK__", ascii(str(task)))
                .replace("__STEP__", ascii(str(step))))

    bodies = {
        "tap": "tap(target['x'], target['y'])",
        # Replace semantics: the field must end up exactly equal to ``text``.
        "input_text": (
            "tap(target['x'], target['y'])\n"
            "        wait_stable()\n"
            "        _field, _old = target, len(target.get('text') or '')\n"
            "        if _old:\n"
            "            # The tap leaves the caret anywhere; n DEL + n FORWARD_DEL clear it from any position.\n"
            "            shell('input keyevent ' + ' '.join(['67'] * _old + ['112'] * _old))\n"
            + ("        shell('input keyevent 279')\n"
               if method == "paste" else
               f"        type_text({action['text']!r})\n" if action.get("text") else "")
            + "        _sleep(0.4)\n"
            "        _now = _observe()\n"
            "        _after = _match(_now, loc)\n"
            "        if len(_after) != 1 and _field.get('id'):\n"
            "            _after = [n for n in _now if n.get('id') == _field['id']]\n"
            "        target = _after[0] if len(_after) == 1 else None\n"
            "        _got = (target.get('text') or '') if target else None"),
        "read_field": "pass",
        "tap_xy": f"tap({int(action.get('x', 0))}, {int(action.get('y', 0))})",
        "key": f"press({action.get('key', 'back')!r})",
        "scroll": f"scroll({action.get('direction', 'down')!r})",
        "wait": f"time.sleep({float(action.get('seconds', 1.0))})",
        "launch_app": f"open_app({action.get('app', 'Settings')!r})",
        "launch_activity": _launch_activity_body(action),
        "share_text": _share_text_body(action) if kind == "share_text" else "",
    }
    if kind not in bodies:
        raise ValueError(f"unsupported action type {kind!r}")
    if kind in ("tap", "input_text") and locator == "None":
        raise ValueError(f"{kind} needs a locator")
    return ACTION_TEMPLATE.format(
        task=ascii(str(task)), step=ascii(str(step)), prelude=GEOMETRY + LOCATOR_PRELUDE + DISPATCH_GATE,
        geometry_guard=(("if _geometry() != " + repr(action["geometry"]) + ":\n"
                        "            result = {'ok': False, 'code': 'precondition_mismatch'}")
                       if action.get("geometry") is not None else "pass") + (
            "\n        _targets = _match(_observe(), " + repr(action["fixed_segment"]["target"]) + ")"
            "\n        if len(_targets) != 1 or _targets[0].get('x') != " + repr(action["x"]) +
            " or _targets[0].get('y') != " + repr(action["y"]) + ":"
            "\n            result = {'ok': False, 'code': 'precondition_mismatch'}"
            if action.get("fixed_segment") else "") + (
            "\n        if not result.get('code'):"
            "\n            result = _dispatch_gate(" + repr(kind == "launch_activity") + ") or result"
            if kind in ("share_text", "launch_activity") else ""),
        locator=locator, alternatives=repr(alternatives), body=bodies[kind], action=kind,
        want=repr(action["text"]) if kind == "input_text" else "None",
        effect=action.get("report_effect") is True and kind in EFFECT_ACTIONS,
        point=[int(action["x"]), int(action["y"])] if kind == "tap_xy" else None,
        expect=repr(action.get("expect")) if action.get("expect") else "None",
        expect_gone=repr(action.get("expect_gone")) if action.get("expect_gone") else "None",
        expect_app=repr(action.get("expect_app")) if action.get("expect_app") else "None",
        settle_ms=int(action.get("settle_ms", 800)),
        poll_ms=int(action.get("poll_ms", 2000)),
        typed=ascii(action.get("text")) if kind in ("input_text", "share_text") else "None",
    )


def run_action(action: dict, serial: str | None = None,
               timeout: float = 90.0) -> dict[str, Any]:
    """Execute one action through phone-harness and return its result.

    ``report_effect: true`` on a native input action adds ``effect`` and
    ``tree_blind``; the raw trees behind them never leave this function.
    """
    from . import web
    from .effect import action_effect
    if web.is_web_locator(action.get("locator")) or web.has_web(action.get("expect")):
        # Web paths do not report effect; never let the native script return raw trees.
        return _run_web_action({**action, "report_effect": False}, serial, timeout)
    result = harness_read(build_action_script(action), serial=serial, timeout=timeout)
    raw = result.pop("_effect_input", None)
    if raw is not None:
        result.update(action_effect(**raw))
    return result


def _blocked_text(nodes: list, page: str) -> bool:
    """Same verification / rate-limit signals as native actions, on the tree and the page text."""
    texts = [str(n.get("text") or n.get("desc") or "") for n in nodes] + [page]
    return any(sig in text.lower() for text in texts for sig in BLOCK_SIGNALS)


def _run_web_action(action: dict, serial: str | None, timeout: float) -> dict[str, Any]:
    """Web tap and/or web ``expect``: host side over CDP; native parts stay in the harness."""
    from . import web
    from .runner import ProviderUnavailable, check
    started = time.monotonic()
    metrics = {"tree_reads": 0, "tree_ms": 0.0, "explicit_wait_ms": 0.0}
    locator, expect = action.get("locator"), action.get("expect")
    result: dict[str, Any] = {"ok": True, "action": action.get("type")}
    if web.is_web_locator(locator):
        try:
            with web.Chrome(serial) as chrome:
                hits = chrome.find(locator)
                if _blocked_text(harness_read(OBSERVE_TREE, serial=serial).get("nodes") or [],
                                 chrome.page_text()):
                    result = {"ok": False, "code": "platform_blocked"}
                elif len(hits) != 1:
                    result = {"ok": False, "code": "ambiguous_target" if hits else "locator_missing",
                              "count": len(hits)}
                else:
                    chrome.tap(hits[0]["x"], hits[0]["y"])
                    result["value"] = hits[0]["text"] or None
        except web.WebUnavailable as exc:
            result = {"ok": False, "code": "locator_missing", "reason": str(exc)}
        except Exception as exc:  # after the touch may have landed: outcome unknown
            result = {"ok": False, "code": "action_failed", "reason": str(exc)[:200]}
    else:
        result = harness_read(build_action_script({**action, "expect": None}), serial=serial, timeout=timeout)
        metrics = result.get("metrics") or metrics
    if result.get("ok") and expect:
        time.sleep(int(action.get("settle_ms", 800)) / 1000.0)
        deadline = time.monotonic() + int(action.get("poll_ms", 2000)) / 1000.0
        passed, why = False, None
        while True:
            begin = time.monotonic()
            nodes = harness_read(OBSERVE_TREE, serial=serial).get("nodes") or []
            metrics["tree_reads"] += 1
            metrics["tree_ms"] += (time.monotonic() - begin) * 1000
            if _blocked_text(nodes, web.page_text(serial)):
                result = {**result, "ok": False, "code": "platform_blocked"}
                break
            try:
                passed, why = check(nodes, expect, serial=serial), None
            except ProviderUnavailable as exc:  # unreadable page: the expect did not hold
                passed, why = False, str(exc)
            if passed or time.monotonic() >= deadline:
                break
            time.sleep(0.3)
            metrics["explicit_wait_ms"] += 300
        if result.get("ok") and not passed:
            result = {**result, "ok": False, "code": "assertion_failed", "reason": why,
                      "labels": [n.get("text") or n.get("desc") for n in nodes
                                 if (n.get("text") or n.get("desc"))][:12]}
    result["metrics"] = {**metrics, "script_ms": (time.monotonic() - started) * 1000}
    result["locator_index"] = 0
    return result


def status(device_id: str | None = None) -> dict[str, Any]:
    """Everything an agent needs before deciding what to do."""
    devices = list_devices()
    if device_id:
        devices = [d for d in devices if d["serial"] == device_id]
    if len(devices) > 1 and not device_id:
        return {"devices": devices, "bound": None, "ambiguous": True}

    if not devices:
        return {"devices": [], "bound": None, "ambiguous": False}

    device = devices[0]
    serial = device["serial"]
    result: dict[str, Any] = {
        "devices": devices,
        "bound": serial,
        "ambiguous": False,
        "state": device["state"],
        "model": device["model"],
        **_screen(serial),
        "ready": False,
    }
    result["lock"] = lock_assessment(result)
    if device["state"] != "device":
        result["reason"] = f"adb reports {device['state']}"
        return result

    if result["lock"]["requires_human"]:
        # A PIN pad's tree is readable too; a readable tree is not an unlocked phone.
        result["reason"] = "credential/PIN screen is visible; a person must unlock"
        return result

    probe = harness_read(PROBE, serial=serial)
    if probe.get("ready"):
        result.update(ready=True, nodes=probe.get("nodes"),
                      sample=probe.get("sample"), reason=None)
    else:
        result["reason"] = probe.get("reason") or probe.get("error") or "probe failed"
    return result
