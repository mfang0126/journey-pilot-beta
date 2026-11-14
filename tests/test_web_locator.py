"""Web locators: validation, routing, check dispatch, and the websocket framing."""
import json
import socket
import struct
import threading
import unittest
from unittest import mock

from journey_pilot import adapter, runner, web
from journey_pilot.journey import JourneyError, validate_check, validate_locator, validate_steps


class FakeChrome:
    hits: list = []
    taps: list = []
    text = ""

    def __init__(self, serial=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def find(self, locator):
        return self.hits

    def page_text(self):
        return self.text

    def tap(self, x, y):
        self.taps.append((x, y))


class Validation(unittest.TestCase):
    def test_web_locator_is_valid_alone_only(self):
        self.assertEqual(validate_locator({"css": "#go"}), {"css": "#go"})
        self.assertEqual(validate_locator({"web_text": "Go"}), {"web_text": "Go"})
        with self.assertRaises(JourneyError):
            validate_locator({"css": "#go", "text": "Go"})

    def test_web_locator_is_tap_only_without_fallback(self):
        step = {"step_id": "s", "action": "tap", "locator": {"css": "#go"}}
        validate_steps([step])
        for bad in ({**step, "action": "read_field"},
                    {**step, "fallback": {"alternatives": [{"text": "x"}]}}):
            with self.assertRaises(JourneyError):
                validate_steps([bad])

    def test_web_checks(self):
        validate_check({"element_exists": {"web_text": "Done"}}, step=True)
        validate_check({"text_equals": {"css": "#s", "text": "Ready"}}, step=True)
        with self.assertRaises(JourneyError):
            validate_check({"text_equals": {"css": "#s"}}, step=True)

    def test_tap_xy_is_not_widened(self):
        with self.assertRaises(JourneyError):
            adapter.build_action_script({"type": "tap_xy", "locator": {"css": "#go"}})


class Routing(unittest.TestCase):
    def setUp(self):
        FakeChrome.taps = []
        for target in ("journey_pilot.web.Chrome",):
            patch = mock.patch(target, FakeChrome)
            patch.start()
            self.addCleanup(patch.stop)
        tree = mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": []})
        tree.start()
        self.addCleanup(tree.stop)

    def tap(self, hits, **extra):
        FakeChrome.hits = hits
        return adapter.run_action({"type": "tap", "locator": {"css": "#go"}, "settle_ms": 0,
                                   "poll_ms": 0, **extra}, serial="dev")

    def test_tap_dispatches_one_touch_at_the_element(self):
        out = self.tap([{"text": "Go", "x": 10, "y": 20}])
        self.assertTrue(out["ok"])
        self.assertEqual(FakeChrome.taps, [(10, 20)])
        self.assertEqual(out["value"], "Go")

    def test_missing_and_ambiguous_never_touch(self):
        self.assertEqual(self.tap([])["code"], "locator_missing")
        two = [{"text": "", "x": 1, "y": 1}] * 2
        self.assertEqual(self.tap(two)["code"], "ambiguous_target")
        self.assertEqual(FakeChrome.taps, [])

    def test_web_expect_decides_the_result(self):
        hit = [{"text": "Go", "x": 1, "y": 1}]
        expect = {"element_exists": {"web_text": "Done"}}
        with mock.patch.object(web, "count", return_value=1):
            self.assertTrue(self.tap(hit, expect=expect)["ok"])
        with mock.patch.object(web, "count", return_value=0):
            self.assertEqual(self.tap(hit, expect=expect)["code"], "assertion_failed")

    def test_verification_page_blocks_before_any_touch(self):
        FakeChrome.text = "请完成滑块验证"
        self.addCleanup(setattr, FakeChrome, "text", "")
        self.assertEqual(self.tap([{"text": "Go", "x": 1, "y": 1}])["code"], "platform_blocked")
        self.assertEqual(FakeChrome.taps, [])

    def test_unreadable_page_fails_checks_closed(self):
        with mock.patch.object(web, "count", side_effect=web.WebUnavailable("no visible Chrome tab")):
            for spec in ({"not": {"element_exists": {"css": "#x"}}},
                         {"element_count": {"css": "#x"}, "equals": 0}):
                with self.assertRaises(runner.ProviderUnavailable):
                    runner.check([], spec, "dev")
            out = self.tap([{"text": "Go", "x": 1, "y": 1}],
                           expect={"not": {"element_exists": {"css": "#x"}}})
        self.assertEqual(out["code"], "assertion_failed")

    def test_runner_check_routes_web_specs_with_the_device(self):
        with mock.patch.object(web, "count", return_value=2) as count:
            self.assertTrue(runner.check([], {"element_count": {"css": "li"}, "equals": 2}, "dev"))
            self.assertTrue(runner.check([], {"text_equals": {"css": "#s", "text": "R"}}, "dev"))
        self.assertEqual(count.call_args_list[0].args, ({"css": "li"}, "dev"))


class Framing(unittest.TestCase):
    def test_socket_round_trips_long_json(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        big = json.dumps({"id": 1, "result": "x" * 70000})

        def serve():
            conn, _ = server.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
            head = conn.recv(2)  # masked client frame: header, then 4-byte mask + payload
            n = head[1] & 0x7F
            conn.recv(4 + n)
            payload = big.encode()
            conn.sendall(bytes([0x81, 127]) + struct.pack(">Q", len(payload)) + payload)
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        ws = web._Socket(server.getsockname()[1], "/x", 5)
        ws.send("hi")
        self.assertEqual(json.loads(ws.recv())["result"], "x" * 70000)
        ws.close()
        server.close()


if __name__ == "__main__":
    unittest.main()
