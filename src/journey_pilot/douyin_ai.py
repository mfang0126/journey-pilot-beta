# Example task script; platform interfaces can change and require independent verification.
"""Douyin-specific knowledge only; starts at an already open comment/AI panel."""
import re
import time

from .contracts import failure
from .task_runtime import execute as run_task, select, RouteBlocked

ROUTE = {"id": "douyin-comment-ai-v1", "app": "com.ss.android.ugc.aweme",
         "budget_seconds": 90, "freshness_seconds": 8}
INPUT = "输入问题或按住说话"


def _header(nodes):
    return select(nodes, lambda n: bool(re.fullmatch(r"评论\s*\d+", n.get("text") or "")))


def _summary(nodes, header_y):
    # The rendered AI document exposes off-screen rows at (0,0) too.
    return [n["text"].strip() for n in nodes if n.get("text", "").strip() and not n.get("id")
            and (n.get("y", 0) == 0 or n.get("y", 0) > header_y)
            and n["text"] != INPUT]


def _read(ctx):
    ctx.require(ctx.foreground.split("/", 1)[0] == ROUTE["app"], "open Douyin comments first")
    nodes = ctx.observe()
    header = _header(nodes)
    if not any(n.get("text") == INPUT for n in nodes):
        # The AI icon has an empty accessibility label on the observed version.
        # Pixel OCR may merge the decorative icon into BAI; row geometry is required.
        ctx.observe("pixels")
        def pixel_tab(n):
            return bool(re.fullmatch(r"[A-Za-z]*AI", (n.get("text") or "").replace(" ", ""))) and \
                abs(n.get("y", 0)-header["y"]) < 45 and n.get("x", 0) > header["x"]
        if any(pixel_tab(n) for n in ctx.nodes):
            ctx.tap(pixel_tab, "AI tab", source="pixels")
        else:
            # Screenshot-verified layout binding: this version exposes the AI
            # tab as a two-space TextView beside the comments heading. Never
            # reuse saved x/y. Missing/duplicate structural anchors must stop.
            nodes = ctx.observe()
            header = _header(nodes)
            ctx.tap(lambda n: n.get("text") == "  " and not n.get("id") and
                    n.get("class") == "android.widget.TextView" and
                    abs(n.get("y", 0)-header["y"]) < 45 and
                    header["x"] < n.get("x", 0) < header["x"]+3*header.get("w", 0),
                    "screenshot-verified AI structural anchor")
        nodes = ctx.observe()
    def body(screen):
        # Tree rows carry whole paragraphs (>=50); OCR returns visual lines
        # (~20 chars each), so the threshold is per channel. Short video
        # captions observed on this page stay below 20.
        threshold = 20 if ctx.source == "pixels" else 50
        return any(len(t) >= threshold and n.get("confidence", 1) >= 0.5
                   for n, t in ((n, n.get("text", "").strip()) for n in screen)
                   if not n.get("id") and (n.get("y", 0) == 0 or n.get("y", 0) > header["y"])
                   and t and t != INPUT)

    def ready(screen):
        return any(n.get("text") == INPUT for n in screen) and body(screen)

    wait = ctx.request.get("wait_seconds", 20)
    deadline = time.monotonic() + wait
    missing_reads = 0
    body_nodes = None
    while not ready(ctx.nodes):
        ctx.budget()
        ctx.require(time.monotonic() < deadline,
                    "result did not become ready within wait budget")
        missing_reads = missing_reads + 1 if any(n.get("text") == INPUT for n in ctx.nodes) else 0
        if missing_reads >= 2:
            # The panel is open but the tree keeps lacking the body. Jev decides
            # the next safe step; restarting the whole task is not an option.
            action = ctx.decide("summary_text_missing", {
                "pixel_read": "read the summary from the screenshot with local OCR instead",
                "reobserve": "read the accessibility tree once more",
                "handoff": "stop and hand the live screen to the calling agent",
            }, "The AI panel is open but the accessibility tree exposes no summary text. "
               "The goal is to READ the summary, not restart the task. Safest next step?")
            missing_reads = 0
            if action == "pixel_read":
                ctx.observe("pixels")
                if body(ctx.nodes):
                    pix_nodes = ctx.nodes
                    # OCR text is only attributable to this video's AI panel
                    # if the panel is still live when the text was captured.
                    tree_now = ctx.observe()
                    if any(n.get("text") == INPUT for n in tree_now):
                        body_nodes = pix_nodes
                        break
                    raise RouteBlocked("screen changed during pixel read; OCR text is not attributable",
                                       handoff=ctx.handoff("page identity lost during pixel read",
                                        "re-verify which screen is live before reading any text"))
                continue
            if action == "handoff":
                raise RouteBlocked("summary text unavailable in the tree; handed back",
                                   handoff=ctx.handoff("summary text missing from tree",
                                    "read the summary via another channel or stop"))
        ctx.timed("wait", lambda: time.sleep(min(0.4, max(0, deadline-time.monotonic()))))
        ctx.observe()
    nodes = body_nodes if body_nodes is not None else ctx.nodes
    if body_nodes is None:
        # identity re-check on tree reads; a pixel read only follows a
        # tree-verified panel that is still live at capture time
        _header(nodes)
    texts = _summary(nodes, header["y"])
    expected = ctx.request.get("expected_summary")
    ctx.require(not expected or any(expected in t for t in texts), "summary identity does not match requested topic")
    return {"summary_title": texts[0], "summary_text": texts,
            "coverage": "pixel_ocr_lines" if body_nodes is not None else "accessibility_exposed",
            "source": "douyin_generated_ai_response",
            "comment_analysis_verified": False, "chat_message_sent": False,
            "entry": "current_video_comments_or_ai_panel"}


def execute(store, request):
    timeout = request.get("wait_seconds", 20)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        return failure("invalid_request", "wait_seconds must be within 0..30")
    if request.get("expected_summary") is not None and not isinstance(request["expected_summary"], str):
        return failure("invalid_request", "expected_summary must be a string")
    return run_task(store, request, ROUTE, _read)
