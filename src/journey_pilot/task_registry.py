"""Explicit registry of reviewed task modules; never import a user-supplied path."""
from . import read_post, douyin_ai
from .contracts import failure, success

TASKS = {
    "xhs-read-post": {"module": read_post, "required": ["device_id", "title", "author"],
                      "entry": "XHS profile entry or exact own post", "effects": "navigation_only"},
    "douyin-ai": {"module": douyin_ai, "required": ["device_id"],
                   "entry": "Douyin current video comments or AI panel already open",
                   "effects": "navigation_only; opens existing AI summary, sends no chat"},
}


def catalog():
    return success("ok", {"tasks": [{"name": name, **{k: v for k, v in spec.items() if k != "module"}}
                                     for name, spec in TASKS.items()],
                          "qualification": "registered_script_not_verified_journey"})


def execute(store, request):
    spec = TASKS.get(request.get("task")) if isinstance(request.get("task"), str) else None
    if spec is None:
        return failure("invalid_request", "unknown task; inspect task-list")
    return spec["module"].execute(store, request)
