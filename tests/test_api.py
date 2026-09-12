import http.client
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "simple_api_3cx.py"
spec = importlib.util.spec_from_file_location("simple_api_3cx", MODULE_PATH)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = api.CONFIG_PATH
        api.CONFIG_PATH = Path(self.temp.name) / "config.json"
        self.token = "test-secret"
        api.save_config({
            "sip_domain": "example.test", "sip_target": "127.0.0.1",
            "listen_host": "127.0.0.1", "listen_port": 0,
            "public_url": "https://example.test",
            "allowed_ips": ["127.0.0.1/32"],
            "keys": {"reader": {"hash": api.digest(self.token), "scopes": ["read"]}},
            "tickets": {},
        })
        self.server = api.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        api.CONFIG_PATH = self.old_path
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = json.loads(response.read())
        status = response.status
        conn.close()
        return status, payload

    def test_read_key_cannot_change_status(self):
        status, payload = self.request(
            "POST", "/simple-api-3cx/v1/users/100/status",
            json.dumps({"status": "available"}),
            {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertIn("Droit", payload["error"])

    def test_ip_filter_rejects_before_key(self):
        status, payload = self.request(
            "GET", "/simple-api-3cx/v1/users",
            headers={"Authorization": "Bearer " + self.token, "X-Real-IP": "203.0.113.4"},
        )
        self.assertEqual(status, 403)
        self.assertIn("IP", payload["error"])

    def test_browser_ticket_is_single_use(self):
        config = api.load_config()
        config["tickets"]["one"] = {
            "hash": api.digest("secret"),
            "route": "/browser/status/100/available",
            "expires": int(api.time.time()) + 60,
        }
        api.save_config(config)
        path = "/simple-api-3cx/v1/browser/status/100/available?ticket=one:secret"
        with mock.patch.object(api.Handler, "change_status", lambda self, *args: self.respond(200, {"ok": True})):
            first, _ = self.request("GET", path, headers={"X-Real-IP": "203.0.113.4"})
            second, _ = self.request("GET", path)
        self.assertEqual(first, 200)
        self.assertEqual(second, 401)

    def test_persistent_link_requires_allowed_ip_and_can_be_revoked(self):
        config = api.load_config()
        config["links"] = {"away100": {
            "hash": api.digest("secret"),
            "route": "/browser/status/100/away",
            "created": int(api.time.time()),
        }}
        api.save_config(config)
        path = "/simple-api-3cx/v1/browser/status/100/away?link=away100:secret"
        with mock.patch.object(api.Handler, "change_status", lambda self, *args: self.respond(200, {"ok": True})):
            first, _ = self.request("GET", path)
            second, _ = self.request("GET", path)
            blocked, _ = self.request("GET", path, headers={"X-Real-IP": "203.0.113.4"})
            config = api.load_config()
            del config["links"]["away100"]
            api.save_config(config)
            revoked, _ = self.request("GET", path)
        self.assertEqual((first, second, blocked, revoked), (200, 200, 403, 401))

    def test_control_key_can_change_one_queue_for_multiqueue_agent(self):
        config = api.load_config()
        config["keys"]["controller"] = {"hash": api.digest("controller-secret"), "scopes": ["control"]}
        api.save_config(config)
        selected = {"queue_logged_in": False, "global_logged_in": True,
                    "effective_logged_in": False}
        with mock.patch.object(api, "change_queue_individual", return_value=selected) as change:
            status, payload = self.request(
                "POST", "/simple-api-3cx/v1/queues/800/agents/100/login",
                json.dumps({"logged_in": False}),
                {"Authorization": "Bearer controller-secret", "Content-Type": "application/json"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["queue_logged_in"], False)
        change.assert_called_once_with("100", "800", False)

    def test_queue_browser_link_is_limited_to_its_action(self):
        config = api.load_config()
        config["links"] = {"off800": {"hash": api.digest("secret"),
            "route": "/browser/queues/800/agents/100/logout", "created": 1}}
        api.save_config(config)
        base = "/simple-api-3cx/v1/browser/queues/800/agents/100/"
        with mock.patch.object(api.Handler, "change_queue_individual",
                               lambda self, *args: self.respond(200, {"ok": True})):
            allowed, _ = self.request("GET", base + "logout?link=off800:secret")
            other_action, _ = self.request("GET", base + "login?link=off800:secret")
        self.assertEqual((allowed, other_action), (200, 403))

    def test_automation_url_changes_status_with_variable_poste_and_action(self):
        config = api.load_config()
        config["automation_keys"] = {"client": {"hash": api.digest("secret")}}
        api.save_config(config)
        with mock.patch.object(api.Handler, "change_status",
                               lambda self, poste, action, config: self.respond(200, {"poste": poste, "action": action})):
            first, first_data = self.request("GET", "/simple-api-3cx/v1/automation?poste=10&action=away&auth=client:secret")
            second, second_data = self.request("GET", "/simple-api-3cx/v1/automation?poste=14&action=available&auth=client:secret")
        self.assertEqual((first, first_data, second, second_data),
                         (200, {"poste": "10", "action": "away"}, 200, {"poste": "14", "action": "available"}))

    def test_automation_url_changes_only_selected_queue(self):
        config = api.load_config()
        config["automation_keys"] = {"client": {"hash": api.digest("secret")}}
        api.save_config(config)
        with mock.patch.object(api.Handler, "change_queue_individual",
                               lambda self, poste, file_number, logged_in: self.respond(200, {"poste": poste, "file": file_number, "logged_in": logged_in})):
            on, on_data = self.request("GET", "/simple-api-3cx/v1/automation?poste=14&file=81&action=login&auth=client:secret")
            off, off_data = self.request("GET", "/simple-api-3cx/v1/automation?poste=10&file=82&action=logout&auth=client:secret")
        self.assertEqual((on, on_data, off, off_data),
                         (200, {"poste": "14", "file": "81", "logged_in": True},
                          200, {"poste": "10", "file": "82", "logged_in": False}))

    def test_automation_rejects_unauthorized_and_malformed_requests(self):
        config = api.load_config()
        config["automation_keys"] = {"client": {"hash": api.digest("secret")}}
        api.save_config(config)
        base = "/simple-api-3cx/v1/automation?poste=10&action=away&auth=client:secret"
        blocked, _ = self.request("GET", base, headers={"X-Real-IP": "203.0.113.4"})
        invalid_key, _ = self.request("GET", base.replace("secret", "wrong"))
        wrong_action, _ = self.request("GET", base.replace("action=away", "action=login"))
        duplicate, _ = self.request("GET", base + "&poste=14")
        self.assertEqual((blocked, invalid_key, wrong_action, duplicate), (403, 401, 400, 400))


class PeriodTests(unittest.TestCase):
    def test_week_starts_monday_in_paris(self):
        start, end = api.parse_period({"period": ["week"]})
        self.assertEqual(start.weekday(), 0)
        self.assertEqual((start.hour, start.minute), (0, 0))
        self.assertEqual(start.tzinfo, api.PARIS)
        self.assertGreater(end, start)

    def test_requires_offset_for_explicit_dates(self):
        with self.assertRaises(api.ApiError):
            api.parse_period({"from": ["2026-09-07T00:00:00"], "to": ["2026-09-08T00:00:00"]})


if __name__ == "__main__":
    unittest.main()
