import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import SmsProviderError, connect_database, create_app, hash_verification_code
from werkzeug.security import generate_password_hash


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = os.path.join(self.temp_dir.name, "test.sqlite")
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": database_path,
                "GMAIL_ADDRESS": "resqnet.test@gmail.com",
                "GMAIL_APP_PASSWORD": "test-app-password",
                "SECRET_KEY": "test-session-secret",
                "RESQNET_DEVICE_API_KEY": "test-device-key",
            }
        )
        self.client = self.app.test_client()
        with closing(connect_database(database_path)) as connection:
            connection.execute(
                """
                INSERT INTO admin_account
                    (email, first_name, last_name, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "admin@example.com",
                    "Admin",
                    "User",
                    generate_password_hash("secure-password"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        self.client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "secure-password"},
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dashboard_is_served_by_flask(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Clinic dashboard", response.data)
        self.assertIn(b"Guard mobile number", response.data)
        self.assertNotIn(b"Admin access token", response.data)
        response.close()

    def test_login_authenticates_registered_admin(self):
        client = self.app.test_client()
        response = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "secure-password"},
        )

        self.assertEqual(response.status_code, 200)
        dashboard_response = client.get("/")
        self.assertEqual(dashboard_response.status_code, 200)
        dashboard_response.close()

        client.get("/logout")
        self.assertEqual(client.get("/").status_code, 302)

    def test_invalid_login_does_not_open_dashboard(self):
        client = self.app.test_client()
        response = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "wrong-password"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(client.get("/").status_code, 302)

    def test_legacy_accounts_are_migrated_to_admin_account(self):
        database_path = os.path.join(self.temp_dir.name, "legacy.sqlite")
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                """
                CREATE TABLE accounts (
                    email TEXT PRIMARY KEY COLLATE NOCASE,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO accounts
                    (email, first_name, last_name, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "legacy@example.com",
                    "Legacy",
                    "Admin",
                    generate_password_hash("legacy-password"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()

        migrated_app = create_app(
            {
                "TESTING": True,
                "DATABASE": database_path,
                "SECRET_KEY": "test-session-secret",
            }
        )
        response = migrated_app.test_client().post(
            "/api/auth/login",
            json={"email": "legacy@example.com", "password": "legacy-password"},
        )

        self.assertEqual(response.status_code, 200)
        with closing(connect_database(database_path)) as connection:
            migrated_account = connection.execute(
                "SELECT email FROM admin_account WHERE email = ?",
                ("legacy@example.com",),
            ).fetchone()
        self.assertIsNotNone(migrated_account)

    def test_api_returns_seeded_alerts_and_devices(self):
        alerts_response = self.client.get("/api/alerts")
        devices_response = self.client.get("/api/devices")

        self.assertEqual(alerts_response.status_code, 200)
        self.assertEqual(devices_response.status_code, 200)
        self.assertEqual(len(alerts_response.get_json()), 5)
        self.assertEqual(len(devices_response.get_json()), 6)
        self.assertIn("building", alerts_response.get_json()[0])
        self.assertTrue(all(alert["isSample"] for alert in alerts_response.get_json()))
        example_alert = next(
            alert for alert in alerts_response.get_json()
            if alert["id"] == "ALT-2026-1042"
        )
        self.assertEqual(example_alert["type"], "Rescue needed")
        self.assertEqual(example_alert["building"], "Main Building")
        self.assertEqual(example_alert["floor"], "2nd Floor")
        self.assertIsInstance(devices_response.get_json()[0]["online"], bool)

    def test_status_update_is_saved_and_returned(self):
        response = self.client.patch(
            "/api/alerts/ALT-2026-1042",
            data=json.dumps({"status": "In progress"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "In progress")
        alerts = self.client.get("/api/alerts").get_json()
        saved_alert = next(alert for alert in alerts if alert["id"] == "ALT-2026-1042")
        self.assertEqual(saved_alert["status"], "In progress")

    def test_status_update_rejects_invalid_status(self):
        response = self.client.patch(
            "/api/alerts/ALT-2026-1042",
            json={"status": "Ignored"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_status_update_rejects_non_string_status(self):
        response = self.client.patch(
            "/api/alerts/ALT-2026-1042",
            json={"status": []},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_status_update_rejects_missing_alert(self):
        response = self.client.patch(
            "/api/alerts/ALT-9999",
            json={"status": "Resolved"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Alert not found.")

    def test_guard_contact_accepts_philippine_format_and_normalizes_for_sms(self):
        response = self.client.put(
            "/api/admin/guard-contact",
            json={"phoneNumber": "0917 123 4567"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["phoneNumber"], "+639171234567")
        self.assertEqual(
            self.client.get("/api/admin/guard-contact").get_json()["phoneNumber"],
            "+639171234567",
        )

        invalid_response = self.client.put(
            "/api/admin/guard-contact",
            json={"phoneNumber": "08171234567"},
        )
        self.assertEqual(invalid_response.status_code, 400)

    def test_guard_contact_accepts_plus_63_philippine_format(self):
        response = self.client.put(
            "/api/admin/guard-contact",
            json={"phoneNumber": "+639171234567"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["phoneNumber"], "+639171234567")

    def test_guard_contact_requires_admin_login(self):
        client = self.app.test_client()
        response = client.put(
            "/api/admin/guard-contact",
            json={"phoneNumber": "+639171234567"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            client.get("/api/admin/guard-contact").status_code,
            401,
        )

    def test_device_alert_requires_device_key(self):
        response = self.client.post("/api/devices/RSQ-MB-001/alerts", json={})

        self.assertEqual(response.status_code, 401)

    @patch("app.send_guard_sms", return_value=("SM123", "Queued"))
    def test_device_alert_sends_guard_sms_with_station_location(self, send_sms):
        self.client.put(
            "/api/admin/guard-contact",
            json={"phoneNumber": "+639171234567"},
        )

        response = self.client.post(
            "/api/devices/RSQ-MB-001/alerts",
            json={},
            headers={"X-Device-Key": "test-device-key"},
        )

        self.assertEqual(response.status_code, 201)
        alert = response.get_json()["alert"]
        self.assertEqual(alert["building"], "Main Building")
        self.assertEqual(alert["floor"], "2nd Floor")
        self.assertEqual(alert["smsStatus"], "Queued")
        self.assertFalse(alert["isSample"])
        send_sms.assert_called_once()
        self.assertEqual(send_sms.call_args.args[1], "+639171234567")
        self.assertIn("Main Building, 2nd Floor", send_sms.call_args.args[2])

        alerts = self.client.get("/api/alerts").get_json()
        saved_alert = next(item for item in alerts if item["id"] == alert["id"])
        self.assertEqual(saved_alert["smsStatus"], "Queued")

    @patch("app.send_guard_sms", side_effect=SmsProviderError("provider unavailable"))
    def test_device_alert_failure_is_reported_and_alert_is_kept(self, send_sms):
        self.client.put(
            "/api/admin/guard-contact",
            json={"phoneNumber": "+639171234567"},
        )
        response = self.client.post(
            "/api/devices/RSQ-MB-001/alerts",
            json={},
            headers={"X-Device-Key": "test-device-key"},
        )

        send_sms.assert_called_once()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["alert"]["smsStatus"], "Failed")
        self.assertIn("provider unavailable", response.get_json()["error"])
        self.assertEqual(
            self.client.get("/api/alerts").get_json()[0]["smsStatus"],
            "Failed",
        )

    def test_device_alert_without_guard_number_is_recorded_with_error(self):
        response = self.client.post(
            "/api/devices/RSQ-MB-001/alerts",
            json={},
            headers={"X-Device-Key": "test-device-key"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("no guard phone number", response.get_json()["error"])
        self.assertEqual(response.get_json()["alert"]["smsStatus"], "Not configured")

    def test_unknown_files_are_not_served(self):
        response = self.client.get("/app.py")

        self.assertEqual(response.status_code, 404)

    @patch("app.send_verification_email")
    def test_registration_requires_a_code_sent_to_email(self, send_email):
        response = self.client.post(
            "/api/auth/send-code",
            json={"email": "student@gmail.com"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("expiresInSeconds", response.get_json())
        send_email.assert_called_once()
        self.assertEqual(send_email.call_args.args[1], "student@gmail.com")
        self.assertRegex(send_email.call_args.args[2], r"^\d{6}$")

    def test_verification_code_endpoint_requires_smtp_configuration(self):
        self.app.config["GMAIL_APP_PASSWORD"] = ""

        response = self.client.post(
            "/api/auth/send-code",
            json={"email": "student@gmail.com"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("GMAIL_ADDRESS", response.get_json()["error"])

    @patch("app.send_verification_email")
    def test_verification_code_resend_is_rate_limited(self, send_email):
        self.client.post("/api/auth/send-code", json={"email": "student@gmail.com"})

        response = self.client.post(
            "/api/auth/send-code",
            json={"email": "student@gmail.com"},
        )

        self.assertEqual(response.status_code, 429)
        self.assertGreater(response.get_json()["retryAfter"], 0)
        send_email.assert_called_once()

    @patch("app.send_verification_email")
    def test_registration_creates_account_after_valid_code(self, send_email):
        self.client.post("/api/auth/send-code", json={"email": "student@gmail.com"})
        code = send_email.call_args.args[2]

        response = self.client.post(
            "/api/auth/register",
            json={
                "firstName": "Alysson",
                "lastName": "Matanguihan",
                "email": "student@gmail.com",
                "password": "secure-password-123",
                "code": code,
                "acceptedTerms": True,
            },
        )

        self.assertEqual(response.status_code, 201)
        with closing(connect_database(self.app.config["DATABASE"])) as connection:
            account = connection.execute(
                "SELECT email, password_hash FROM admin_account WHERE email = ?",
                ("student@gmail.com",),
            ).fetchone()
            pending = connection.execute(
                "SELECT 1 FROM registration_codes WHERE email = ?",
                ("student@gmail.com",),
            ).fetchone()
        self.assertIsNotNone(account)
        self.assertNotEqual(account["password_hash"], "secure-password-123")
        self.assertIsNone(pending)

    @patch("app.send_verification_email")
    def test_wrong_verification_code_is_rejected(self, send_email):
        self.client.post("/api/auth/send-code", json={"email": "student@gmail.com"})

        response = self.client.post(
            "/api/auth/register",
            json={
                "firstName": "Test",
                "lastName": "User",
                "email": "student@gmail.com",
                "password": "secure-password-123",
                "code": "000000" if send_email.call_args.args[2] != "000000" else "000001",
                "acceptedTerms": True,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["attemptsRemaining"], 4)

    @patch("app.send_verification_email")
    def test_too_many_wrong_codes_invalidate_the_verification(self, send_email):
        self.client.post("/api/auth/send-code", json={"email": "student@gmail.com"})
        actual_code = send_email.call_args.args[2]
        wrong_code = "000000" if actual_code != "000000" else "000001"
        payload = {
            "firstName": "Test",
            "lastName": "User",
            "email": "student@gmail.com",
            "password": "secure-password-123",
            "code": wrong_code,
            "acceptedTerms": True,
        }

        responses = [
            self.client.post("/api/auth/register", json=payload)
            for _ in range(5)
        ]

        self.assertEqual([response.status_code for response in responses], [400] * 4 + [429])
        self.assertIn("Too many incorrect", responses[-1].get_json()["error"])

    @patch("app.send_verification_email")
    def test_verification_code_expires(self, send_email):
        self.client.post("/api/auth/send-code", json={"email": "student@gmail.com"})
        code = send_email.call_args.args[2]
        with closing(connect_database(self.app.config["DATABASE"])) as connection:
            expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            connection.execute(
                "UPDATE registration_codes SET expires_at = ? WHERE email = ?",
                (expired_at, "student@gmail.com"),
            )
            connection.commit()

        response = self.client.post(
            "/api/auth/register",
            json={
                "firstName": "Test",
                "lastName": "User",
                "email": "student@gmail.com",
                "password": "secure-password-123",
                "code": code,
                "acceptedTerms": True,
            },
        )

        self.assertEqual(response.status_code, 410)
        self.assertIn("expired", response.get_json()["error"].lower())

    def test_registration_code_hash_is_salted(self):
        first_salt = os.urandom(16)
        second_salt = os.urandom(16)

        self.assertNotEqual(
            hash_verification_code("123456", first_salt),
            hash_verification_code("123456", second_salt),
        )


if __name__ == "__main__":
    unittest.main()
