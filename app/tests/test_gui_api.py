from __future__ import annotations

import csv
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from flightmill.api.app import create_app
from flightmill.acquisition.serial_transport import SerialTransport


def test_application_starts_with_disconnected_usb_source(tmp_path):
    # In-process HTTP only. Starting the application must not open a USB port.
    with patch.object(SerialTransport, "open") as open_port:
        with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
            client.get("/api/session")
            state = client.get("/api/state").json()
            assert state["source"] == "serial"
            assert state["state"] == "DISCONNECTED"
            assert not state["connected"] and not state["ready"]
            assert client.get("/health").json()["mode"] == "serial"
        open_port.assert_not_called()


class GuiApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.app = create_app(self.directory)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.token = self.client.get("/api/session").json()["token"]
        self.assertEqual(self.action("select_source", source="simulation").status_code, 200)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def action(self, name, **payload):
        return self.client.post("/api/action", json={"action": name, **payload},
                                headers={"X-Flightmill-Token": self.token})

    def arm(self):
        self.assertEqual(self.action("connect").status_code, 200)
        result = self.action("arm", species_code="SIM", trial_type="api", trial_number=1,
                             well_id="A1", attempt=1, arm_radius_cm=10)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_local_authorization_and_origin(self):
        self.assertEqual(self.client.post("/api/action", json={"action": "connect"}).status_code,
                         403)
        hostile = {"Origin": "https://example.invalid", "X-Flightmill-Token": self.token}
        self.assertEqual(self.client.post("/api/action", json={"action": "connect"},
                                          headers=hostile).status_code, 403)
        self.assertEqual(self.client.get("/api/session", headers={"Host": "evil.invalid"})
                         .status_code, 403)
        self.assertEqual(self.client.get("/api/session", headers={"Sec-Fetch-Site": "cross-site"})
                         .status_code, 403)
        self.assertEqual(self.action("connect").status_code, 200)

    def test_websocket_snapshot_and_client_disconnect_do_not_stop_trial(self):
        self.arm()
        self.assertEqual(self.action("start").status_code, 200)
        with self.client.websocket_connect(f"ws://127.0.0.1/ws?token={self.token}") as socket:
            value = socket.receive_json()
            self.assertEqual(value["state"], "RECORDING")
        self.assertEqual(self.client.get("/api/state").json()["state"], "RECORDING")
        self.assertEqual(self.action("stop").status_code, 200)

    def test_second_local_instance_does_not_invalidate_first_session(self):
        with TestClient(create_app(self.directory / "second"),
                        base_url="http://127.0.0.1:8766") as second:
            second.cookies.update(self.client.cookies)
            second.get("/api/session")
            self.client.cookies.update(second.cookies)
            self.assertEqual(self.client.get("/api/state").status_code, 200)
            self.assertEqual(second.get("/api/state").status_code, 200)

    def test_zero_event_trial_exports_contract_and_provenance(self):
        self.action("connect")
        self.action("configure_simulation", profile="zero", seed=42, rate_hz=1)
        armed = self.arm()
        self.assertEqual(self.action("start").status_code, 200)
        trial_id = armed["trial"]["id"]
        self.assertEqual(self.client.get(f"/api/trials/{trial_id}/bundle").status_code, 409)
        result = self.action("stop")
        self.assertEqual(result.status_code, 200, result.text)
        trial = result.json()["trial"]
        self.assertFalse(trial["incomplete"])
        raw = self.client.get(f"/api/trials/{trial_id}/files/raw")
        self.assertEqual(raw.status_code, 200, raw.text)
        rows = list(csv.reader(io.StringIO(raw.text)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 9)
        bundle = self.client.get(f"/api/trials/{trial_id}/bundle")
        self.assertEqual(bundle.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            self.assertIn("SIMULATED_DATA.txt", archive.namelist())
            self.assertTrue(any(name.endswith("_simulation.json") for name in archive.namelist()))
        self.assertEqual(self.client.get(f"/api/trials/{trial_id}/files/unknown").status_code, 404)
        self.assertEqual(self.client.get("/api/trials/unknown/bundle").status_code, 404)

    def test_invalid_transitions_and_bad_body_surface_errors(self):
        self.assertEqual(self.action("start").status_code, 409)
        bad = self.client.post("/api/action", content="not json",
                               headers={"X-Flightmill-Token": self.token})
        self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
