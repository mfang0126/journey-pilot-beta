# Example task script; platform interfaces can change and require independent verification.
"""XHS business route; execution, evidence and timing live in task_runtime."""
from .contracts import failure
from .runner import _jev_choice
from .task_runtime import execute as run_task, RouteBlocked, select

ROUTE = {"id": "xhs-own-post-read-v1", "app": "com.xingin.xhs",
         "max_profile_scrolls": 3, "max_comment_scrolls": 2,
         "budget_seconds": 90, "freshness_seconds": 8}


def own_card(node, title, author, nodes):
    nav_top = min((n['y']-n.get('h', 0)/2 for n in nodes
                   if n.get('desc') in ('首页', '市集', '我') and n.get('y')), default=float('inf'))
    return bool(node.get('clickable') and node.get('y', 0) < nav_top and
                f'笔记,{title},来自{author},' in (node.get('desc') or ''))


def _read(ctx):
    import re
    title, author = ctx.request["title"], ctx.request["author"]
    def verified(nodes):
        texts = {n.get("text") for n in nodes}
        return {title, author, "公开可见"}.issubset(texts)
    def card(n):
        return own_card(n, title, author, ctx.nodes)

    if ctx.foreground.split("/", 1)[0] != ROUTE["app"]:
        ctx.act("launch_app", app=ROUTE["app"])

    nodes = ctx.observe()
    if not verified(nodes):
        profile_pred = lambda n: n.get("clickable") and n.get("desc") == "我"
        profile_fallback = lambda n: n.get("clickable") and (n.get("desc") or n.get("text")) in ("我", "个人主页")

        ctx.tap(profile_pred, "personal profile", fallback=profile_fallback)
        nodes = ctx.observe()

        for attempt in range(ROUTE["max_profile_scrolls"]+1):
            if any(card(n) for n in nodes):
                break
            ctx.require(attempt < ROUTE["max_profile_scrolls"], "exact own post not found")
            ctx.require(any(n.get("desc") == "已选定笔记" for n in nodes), "profile landmark missing")
            ctx.act("scroll", direction="down")
            nodes = ctx.observe()

        ctx.tap(card, "exact own post")
        nodes = ctx.observe()

    ctx.require(verified(nodes), "title/author/public-visibility postcondition failed")
    post_text = [n["text"] for n in nodes if n.get("text")]

    for attempt in range(ROUTE["max_comment_scrolls"]+1):
        if any(re.search(r"共\d+条评论", n.get("desc") or n.get("text") or "") for n in nodes):
            break
        ctx.require(attempt < ROUTE["max_comment_scrolls"], "comment area not observed")
        ctx.act("scroll", direction="down")
        nodes = ctx.observe()

    return {"title": title, "author": author, "post_text": post_text,
            "comment_view_text": [n.get("text") or n.get("desc") for n in nodes
                                  if n.get("text") or n.get("desc")],
            "coverage": "visible_only"}


def execute(store, request):
    if not all(isinstance(request.get(k), str) and request[k].strip() for k in ("title", "author")):
        return failure("invalid_request", "read-post needs exact title and author")
    return run_task(store, request, ROUTE, _read, chooser=_jev_choice)
