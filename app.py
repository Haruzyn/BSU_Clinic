from __future__ import annotations

import base64
import json
import hmac
import os
import hashlib
import re
import secrets
import sqlite3
import smtplib
import ssl
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import logging
import math
from cryptography.fernet import Fernet

from flask import Flask, jsonify, redirect, request, send_from_directory, session
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json, base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
    PublicKeyCredentialDescriptor,
)


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
ALLOWED_STATUSES = {"New", "In progress", "Resolved"}
VERIFICATION_CODE_LIFETIME = timedelta(minutes=10)
VERIFICATION_RESEND_INTERVAL = timedelta(seconds=60)
MAX_VERIFICATION_ATTEMPTS = 5
WEB_FILES = {
    "index.html",
    "login.html",
    "register.html",
    "style.css",
    "dashboard.css",
    "dashboard.js",
    "login.js",
}


def connect_database(database_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def seed_sample_data(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0:
        connection.executemany(
            """
            INSERT INTO devices (id, name, building, floor, online)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("RSQ-MB-001", "Main Entrance Station", "Main Building", "2nd Floor", 1),
                ("RSQ-SC-003", "Science Hall Station", "Science Building", "1st Floor", 1),
                ("RSQ-GY-002", "Gymnasium Station", "Gymnasium", "Ground Floor", 1),
                ("RSQ-AD-001", "Admin Office Station", "Admin Building", "Ground Floor", 1),
                ("RSQ-LB-004", "Library Station", "Library", "3rd Floor", 0),
                ("RSQ-CA-002", "Cafeteria Station", "Cafeteria", "Ground Floor", 1),
            ],
        )

    if connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0:
        now = datetime.now(timezone.utc)
        sample_alerts = [
            ("ALT-2026-1042", "Rescue needed", "RSQ-MB-001", 18, "New"),
            ("ALT-2026-1041", "First aid request", "RSQ-SC-003", 102, "In progress"),
            ("ALT-2026-1040", "Medical assistance", "RSQ-GY-002", 250, "Resolved"),
            ("ALT-2026-1039", "Emergency alert", "RSQ-AD-001", 1235, "Resolved"),
            ("ALT-2026-1038", "First aid request", "RSQ-LB-004", 1625, "In progress"),
        ]
        connection.executemany(
            """
            INSERT INTO alerts (id, type, device_id, received_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    alert_id,
                    alert_type,
                    device_id,
                    (now - timedelta(minutes=minutes)).isoformat(),
                    status,
                )
                for alert_id, alert_type, device_id, minutes, status in sample_alerts
            ],
        )

    alert_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(alerts)")
    }
    if "is_sample" not in alert_columns:
        connection.execute(
            """
            ALTER TABLE alerts
            ADD COLUMN is_sample INTEGER NOT NULL DEFAULT 0
                CHECK (is_sample IN (0, 1))
            """
        )
    connection.execute(
        """
        UPDATE alerts SET is_sample = 1
        WHERE id IN (
            'ALT-2026-1042', 'ALT-2026-1041', 'ALT-2026-1040',
            'ALT-2026-1039', 'ALT-2026-1038'
        )
        """
    )
    connection.execute(
        """
        UPDATE alerts SET type = 'Rescue needed'
        WHERE id = 'ALT-2026-1042' AND is_sample = 1
        """
    )

    connection.commit()


def alert_as_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "deviceId": row["device_id"],
        "building": row["building"],
        "floor": row["floor"],
        "receivedAt": row["received_at"],
        "status": row["status"],
        "smsStatus": row["sms_status"] or "Not sent",
        "isSample": bool(row["is_sample"]),
    }


def normalize_philippine_mobile_number(phone_number: str) -> str | None:
    compact_number = re.sub(r"[\s()-]", "", phone_number)
    if re.fullmatch(r"09[0-9]{9}", compact_number):
        return f"+63{compact_number[1:]}"
    if re.fullmatch(r"\+639[0-9]{9}", compact_number):
        return compact_number
    return None


class SmsProviderError(Exception):
    pass


def send_guard_sms(app: Flask, recipient: str, message: str) -> tuple[str, str]:
    account_sid = app.config["TWILIO_ACCOUNT_SID"]
    auth_token = app.config["TWILIO_AUTH_TOKEN"]
    sender_number = app.config["TWILIO_FROM_NUMBER"]
    if not account_sid or not auth_token or not sender_number:
        raise SmsProviderError(
            "Twilio is not configured. Set TWILIO_ACCOUNT_SID, "
            "TWILIO_AUTH_TOKEN, and TWILIO_FROM_NUMBER."
        )

    credentials = base64.b64encode(
        f"{account_sid}:{auth_token}".encode("utf-8")
    ).decode("ascii")
    request_body = urlencode(
        {"To": recipient, "From": sender_number, "Body": message}
    ).encode("ascii")
    sms_request = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=request_body,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urlopen(sms_request, timeout=20) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        try:
            provider_result = json.loads(error.read())
        except (UnicodeDecodeError, json.JSONDecodeError):
            provider_result = {}
        provider_message = (
            provider_result.get("message")
            if isinstance(provider_result, dict)
            else None
        )
        detail = provider_message if isinstance(provider_message, str) else str(error)
        raise SmsProviderError(f"Twilio rejected the SMS: {detail}") from error
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SmsProviderError(f"Could not reach Twilio: {error}") from error

    if not isinstance(result, dict):
        raise SmsProviderError("Twilio returned an invalid SMS response.")
    message_sid = result.get("sid")
    delivery_status = result.get("status")
    if not isinstance(message_sid, str) or not isinstance(delivery_status, str):
        raise SmsProviderError("Twilio returned an invalid SMS response.")
    return message_sid, delivery_status.title()


def hash_verification_code(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        code.encode("ascii"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
    )


def face_template_cipher(app: Flask) -> Fernet:
    key = hashlib.sha256(app.config["SECRET_KEY"].encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def send_verification_email(
    app: Flask,
    recipient: str,
    code: str,
) -> None:
    app.logger.info("Sending verification email.")
    sender = app.config["GMAIL_ADDRESS"]
    app_password = app.config["GMAIL_APP_PASSWORD"].replace(" ", "")
    message = EmailMessage()
    message["Subject"] = "Your RESQNET registration verification code"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        f"Your RESQNET verification code is {code}.\n\n"
        "It expires in 10 minutes. If you did not request this code, "
        "you can ignore this email."
    )

    context = ssl.create_default_context()
    app.logger.info(f"Connecting to Gmail SMTP server. In {app.config['SMTP_HOST']}:{app.config['SMTP_PORT']}")
    with smtplib.SMTP(app.config["SMTP_HOST"], app.config["SMTP_PORT"], timeout=20) as smtp:
        app.logger.info("Starting TLS for Gmail SMTP.")
        smtp.ehlo()
        smtp.starttls(context=context)
        app.logger.info("Connected to Gmail SMTP server and started TLS. Logging in.")
        smtp.ehlo()
        app.logger.info(f"Logging in to Gmail SMTP as {sender}.")
        smtp.login(sender, app_password)
        app.logger.info("Logged in to Gmail SMTP server. Sending email.")
        smtp.send_message(message)
        app.logger.info("Verification email sent successfully.")


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.path.join(app.instance_path, "resqnet.sqlite"),
        JSON_SORT_KEYS=False,
        GMAIL_ADDRESS=os.environ.get("GMAIL_ADDRESS", ""),
        GMAIL_APP_PASSWORD=os.environ.get("GMAIL_APP_PASSWORD", ""),
        TWILIO_ACCOUNT_SID=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        TWILIO_AUTH_TOKEN=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        TWILIO_FROM_NUMBER=os.environ.get("TWILIO_FROM_NUMBER", ""),
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        RESQNET_DEVICE_API_KEY=os.environ.get("RESQNET_DEVICE_API_KEY", ""),
        WEBAUTHN_RP_ID=os.environ.get("WEBAUTHN_RP_ID", "localhost"),
        WEBAUTHN_ORIGIN=os.environ.get("WEBAUTHN_ORIGIN", "http://localhost:5000"),
        SESSION_COOKIE_SECURE=os.environ.get(
            "SESSION_COOKIE_SECURE", "false"
        ).lower() == "true",
        SMTP_HOST="smtp.gmail.com",
        SMTP_PORT=587,
    )
    if test_config:
        app.config.update(test_config)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    os.makedirs(app.instance_path, exist_ok=True)
    with closing(connect_database(app.config["DATABASE"])) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                building TEXT NOT NULL,
                floor TEXT NOT NULL,
                online INTEGER NOT NULL CHECK (online IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                device_id TEXT NOT NULL REFERENCES devices(id),
                received_at TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('New', 'In progress', 'Resolved'))
            );
            CREATE TABLE IF NOT EXISTS guard_contacts (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                phone_number TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sms_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT NOT NULL REFERENCES alerts(id),
                recipient_number TEXT,
                message_content TEXT NOT NULL,
                delivery_status TEXT NOT NULL,
                sent_at TEXT,
                provider_response TEXT
            );
            CREATE TABLE IF NOT EXISTS accounts (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_account (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registration_codes (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                code_hash BLOB NOT NULL,
                salt BLOB NOT NULL,
                expires_at TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS email_send_limits (
                ip_address TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passkey_credentials (
                credential_id TEXT PRIMARY KEY,
                email TEXT NOT NULL COLLATE NOCASE REFERENCES admin_account(email) ON DELETE CASCADE,
                public_key BLOB NOT NULL,
                sign_count INTEGER NOT NULL DEFAULT 0,
                user_handle BLOB NOT NULL,
                transports TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS face_templates (
                email TEXT PRIMARY KEY COLLATE NOCASE REFERENCES admin_account(email) ON DELETE CASCADE,
                descriptor TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO admin_account
                (email, first_name, last_name, password_hash, created_at)
            SELECT email, first_name, last_name, password_hash, created_at
            FROM accounts
            """
        )
        connection.commit()
        seed_sample_data(connection)

    @app.get("/")
    def dashboard() -> Any:
        if "admin_email" not in session:
            return redirect("/login.html")
        return send_from_directory(BASE_DIR, "index.html")

    @app.get("/logout")
    def logout() -> Any:
        session.clear()
        return redirect("/login.html")

    @app.get("/<path:filename>")
    def web_file(filename: str) -> Any:
        if filename == "index.html" and "admin_email" not in session:
            return redirect("/login.html")
        safe_image = filename.startswith("images/") and Path(filename).suffix.lower() in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".avif",
        }
        if filename not in WEB_FILES and not safe_image:
            return jsonify(error="File not found."), 404
        return send_from_directory(BASE_DIR, filename)

    @app.post("/api/auth/login")
    def login_account() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Request body must be a JSON object."), 400

        email = payload.get("email")
        password = payload.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            return jsonify(error="Enter your email and password."), 400

        with closing(connect_database(app.config["DATABASE"])) as connection:
            account = connection.execute(
                "SELECT email, password_hash FROM admin_account WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        if not account or not check_password_hash(account["password_hash"], password):
            return jsonify(error="Email or password is incorrect."), 401

        session.clear()
        session["admin_email"] = account["email"]
        return jsonify(message="Signed in.")

    @app.post("/api/auth/passkey/register/options")
    def passkey_registration_options() -> Any:
        email = session.get("admin_email")
        payload = request.get_json(silent=True)
        password = payload.get("password") if isinstance(payload, dict) else None
        if not email or not isinstance(password, str):
            return jsonify(error="Sign in and confirm your password to add a passkey."), 401
        with closing(connect_database(app.config["DATABASE"])) as connection:
            account = connection.execute(
                "SELECT first_name, last_name, password_hash FROM admin_account WHERE email = ?",
                (email,),
            ).fetchone()
            credentials = connection.execute(
                "SELECT credential_id FROM passkey_credentials WHERE email = ?", (email,)
            ).fetchall()
        if not account or not check_password_hash(account["password_hash"], password):
            return jsonify(error="Password is incorrect."), 401
        options = generate_registration_options(
            rp_id=app.config["WEBAUTHN_RP_ID"],
            rp_name="RESQNET",
            user_id=secrets.token_bytes(32),
            user_name=email,
            user_display_name=f"{account['first_name']} {account['last_name']}",
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row["credential_id"]))
                for row in credentials
            ],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        session["passkey_challenge"] = options.challenge.hex()
        session["passkey_email"] = email
        session["passkey_user_handle"] = options.user.id.hex()
        return app.response_class(options_to_json(options), mimetype="application/json")

    @app.get("/api/auth/passkey/status")
    def passkey_status() -> Any:
        email = session.get("admin_email")
        if not email:
            return jsonify(error="Sign in first."), 401
        with closing(connect_database(app.config["DATABASE"])) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM passkey_credentials WHERE email = ?", (email,)
            ).fetchone()[0]
        return jsonify(registered=bool(count))

    @app.post("/api/auth/passkey/register/verify")
    def verify_passkey_registration() -> Any:
        email = session.get("admin_email")
        challenge = session.pop("passkey_challenge", None)
        expected_email = session.pop("passkey_email", None)
        user_handle = session.pop("passkey_user_handle", None)
        payload = request.get_json(silent=True)
        if not email or expected_email != email or not challenge or not user_handle or not isinstance(payload, dict):
            return jsonify(error="Passkey setup expired. Please try again."), 400
        try:
            verification = verify_registration_response(
                credential=payload,
                expected_challenge=bytes.fromhex(challenge),
                expected_rp_id=app.config["WEBAUTHN_RP_ID"],
                expected_origin=app.config["WEBAUTHN_ORIGIN"],
                require_user_verification=True,
            )
        except Exception:
            app.logger.info("Passkey registration verification failed.")
            return jsonify(error="Could not verify this device. Please try again."), 400
        credential_id = bytes_to_base64url(verification.credential_id)
        transports = payload.get("response", {}).get("transports", [])
        if not isinstance(transports, list):
            transports = []
        with closing(connect_database(app.config["DATABASE"])) as connection:
            connection.execute(
                "INSERT INTO passkey_credentials (credential_id,email,public_key,sign_count,user_handle,transports,created_at) VALUES (?,?,?,?,?,?,?)",
                (credential_id, email, verification.credential_public_key, verification.sign_count,
                bytes.fromhex(user_handle), json.dumps(transports), datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        return jsonify(message="Face ID / Windows Hello passkey added.")

    @app.post("/api/auth/passkey/login/options")
    def passkey_login_options() -> Any:
        payload = request.get_json(silent=True)
        email = payload.get("email", "").strip().lower() if isinstance(payload, dict) and isinstance(payload.get("email"), str) else ""
        if not email:
            return jsonify(error="Enter your email first."), 400
        with closing(connect_database(app.config["DATABASE"])) as connection:
            rows = connection.execute(
                "SELECT credential_id, transports FROM passkey_credentials WHERE email = ?", (email,)
            ).fetchall()
        if not rows:
            return jsonify(error="No passkey is registered for this account."), 404
        options = generate_authentication_options(
            rp_id=app.config["WEBAUTHN_RP_ID"],
            allow_credentials=[
                PublicKeyCredentialDescriptor(
                    id=base64url_to_bytes(row["credential_id"]),
                    transports=json.loads(row["transports"]),
                ) for row in rows
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        session["passkey_challenge"] = options.challenge.hex()
        session["passkey_email"] = email
        return app.response_class(options_to_json(options), mimetype="application/json")

    @app.post("/api/auth/passkey/login/verify")
    def verify_passkey_login() -> Any:
        challenge = session.pop("passkey_challenge", None)
        email = session.pop("passkey_email", None)
        payload = request.get_json(silent=True)
        if not challenge or not email or not isinstance(payload, dict):
            return jsonify(error="Passkey sign-in expired. Please try again."), 400
        credential_id = payload.get("id")
        with closing(connect_database(app.config["DATABASE"])) as connection:
            credential = connection.execute(
                "SELECT * FROM passkey_credentials WHERE credential_id = ? AND email = ?",
                (credential_id, email),
            ).fetchone()
        if not credential:
            return jsonify(error="This passkey is not registered to the account."), 401
        try:
            verification = verify_authentication_response(
                credential=payload,
                expected_challenge=bytes.fromhex(challenge),
                expected_rp_id=app.config["WEBAUTHN_RP_ID"],
                expected_origin=app.config["WEBAUTHN_ORIGIN"],
                credential_public_key=credential["public_key"],
                credential_current_sign_count=credential["sign_count"],
                credential_id=base64url_to_bytes(credential["credential_id"]),
                require_user_verification=True,
            )
        except Exception:
            app.logger.info("Passkey authentication verification failed.")
            return jsonify(error="Passkey verification failed."), 401
        with closing(connect_database(app.config["DATABASE"])) as connection:
            connection.execute(
                "UPDATE passkey_credentials SET sign_count = ? WHERE credential_id = ?",
                (verification.new_sign_count, credential["credential_id"]),
            )
            connection.commit()
        session.clear()
        session["admin_email"] = email
        return jsonify(message="Signed in with passkey.")

    @app.get("/api/auth/face/status")
    def face_status() -> Any:
        email = session.get("admin_email")
        if not email:
            return jsonify(error="Sign in first."), 401
        with closing(connect_database(app.config["DATABASE"])) as connection:
            registered = connection.execute(
                "SELECT 1 FROM face_templates WHERE email = ?", (email,)
            ).fetchone()
        return jsonify(registered=bool(registered))

    @app.post("/api/auth/face/enroll")
    def enroll_face() -> Any:
        email = session.get("admin_email")
        payload = request.get_json(silent=True)
        if not email or not isinstance(payload, dict):
            return jsonify(error="Sign in first to enroll your face."), 401
        password = payload.get("password")
        descriptor = payload.get("descriptor")
        if not isinstance(password, str) or not isinstance(descriptor, list):
            return jsonify(error="Confirm your password and capture your face."), 400
        if len(descriptor) != 128 or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or abs(value) > 10
            for value in descriptor
        ):
            return jsonify(error="Invalid face data. Please capture again."), 400
        with closing(connect_database(app.config["DATABASE"])) as connection:
            account = connection.execute(
                "SELECT password_hash FROM admin_account WHERE email = ?", (email,)
            ).fetchone()
            if not account or not check_password_hash(account["password_hash"], password):
                return jsonify(error="Password is incorrect."), 401
            connection.execute(
                "INSERT INTO face_templates (email, descriptor, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET descriptor = excluded.descriptor, created_at = excluded.created_at",
                (
                    email,
                    face_template_cipher(app).encrypt(json.dumps(descriptor).encode("utf-8")).decode("ascii"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        return jsonify(message="Face login set up successfully.")

    @app.post("/api/auth/face/login")
    def face_login() -> Any:
        payload = request.get_json(silent=True)
        email = payload.get("email", "").strip().lower() if isinstance(payload, dict) and isinstance(payload.get("email"), str) else ""
        descriptor = payload.get("descriptor") if isinstance(payload, dict) else None
        if not email or not isinstance(descriptor, list) or len(descriptor) != 128 or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or abs(value) > 10
            for value in descriptor
        ):
            return jsonify(error="Could not recognize the face. Try again or use your password."), 401
        with closing(connect_database(app.config["DATABASE"])) as connection:
            row = connection.execute(
                "SELECT descriptor FROM face_templates WHERE email = ?", (email,)
            ).fetchone()
        if not row:
            return jsonify(error="No face is enrolled for this account."), 401
        try:
            reference = json.loads(
                face_template_cipher(app).decrypt(row["descriptor"].encode("ascii"))
            )
        except Exception:
            app.logger.exception("Could not decrypt a stored face template.")
            return jsonify(error="Face login is unavailable. Sign in with your password."), 503
        distance = math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(reference, descriptor)))
        if distance > 0.48:
            return jsonify(error="Face did not match. Try again or use your password."), 401
        session.clear()
        session["admin_email"] = email
        return jsonify(message="Signed in with face recognition.")

    @app.post("/api/auth/send-code")
    def send_registration_code() -> Any:
        app.logger.info("Sending registration verification code.")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Request body must be a JSON object."), 400

        email = payload.get("email")
        if not isinstance(email, str):
            return jsonify(error="Enter a valid email address."), 400
        email = email.strip().lower()
        if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return jsonify(error="Enter a valid email address."), 400

        if not app.config["GMAIL_ADDRESS"] or not app.config["GMAIL_APP_PASSWORD"]:
            return jsonify(
                error=(
                    "Email verification is not set up yet. In the project's .env "
                    "file, set GMAIL_ADDRESS to the sender Gmail account and "
                    "GMAIL_APP_PASSWORD to that account's Google App Password "
                    "(not its normal Gmail password), then restart Flask."
                )
            ), 503

        now = datetime.now(timezone.utc)
        with closing(connect_database(app.config["DATABASE"])) as connection:
            account = connection.execute(
                "SELECT 1 FROM admin_account WHERE email = ?",
                (email,),
            ).fetchone()
            if account:
                return jsonify(error="An account with this email already exists."), 409

            ip_address = request.remote_addr or "unknown"
            ip_limit = connection.execute(
                "SELECT sent_at FROM email_send_limits WHERE ip_address = ?",
                (ip_address,),
            ).fetchone()
            if ip_limit:
                retry_after = int(
                    (
                        VERIFICATION_RESEND_INTERVAL
                        - (now - datetime.fromisoformat(ip_limit["sent_at"]))
                    ).total_seconds()
                )
                if retry_after > 0:
                    return jsonify(
                        error="Please wait before requesting another code.",
                        retryAfter=retry_after,
                    ), 429

            previous = connection.execute(
                "SELECT sent_at FROM registration_codes WHERE email = ?",
                (email,),
            ).fetchone()
            if previous:
                sent_at = datetime.fromisoformat(previous["sent_at"])
                retry_after = int(
                    (VERIFICATION_RESEND_INTERVAL - (now - sent_at)).total_seconds()
                )
                if retry_after > 0:
                    return jsonify(
                        error="Please wait before requesting another code.",
                        retryAfter=retry_after,
                    ), 429

            code = f"{secrets.randbelow(1_000_000):06d}"
            salt = secrets.token_bytes(16)
            connection.execute(
                """
                INSERT INTO registration_codes
                    (email, code_hash, salt, expires_at, sent_at, attempts)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(email) DO UPDATE SET
                    code_hash = excluded.code_hash,
                    salt = excluded.salt,
                    expires_at = excluded.expires_at,
                    sent_at = excluded.sent_at,
                    attempts = 0
                """,
                (
                    email,
                    hash_verification_code(code, salt),
                    salt,
                    (now + VERIFICATION_CODE_LIFETIME).isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO email_send_limits (ip_address, sent_at)
                VALUES (?, ?)
                ON CONFLICT(ip_address) DO UPDATE SET sent_at = excluded.sent_at
                """,
                (ip_address, now.isoformat()),
            )
            connection.commit()
            app.logger.info("Verification code generated and stored in the database.")
        try:
            send_verification_email(app, email, code)
        except Exception as error:
            app.logger.exception("Failed to send registration verification email.")
            with closing(connect_database(app.config["DATABASE"])) as connection:
                connection.execute(
                    "DELETE FROM registration_codes WHERE email = ?",
                    (email,),
                )
                connection.execute(
                    "DELETE FROM email_send_limits WHERE ip_address = ?",
                    (ip_address,),
                )
                connection.commit()
            return jsonify(error="Could not send the verification email. Check the Gmail SMTP settings and try again."), 502

        return jsonify(
            message="Verification code sent. Check your email inbox and spam folder.",
            expiresInSeconds=int(VERIFICATION_CODE_LIFETIME.total_seconds()),
            resendAfterSeconds=int(VERIFICATION_RESEND_INTERVAL.total_seconds()),
        ), 200

    @app.post("/api/auth/register")
    def register_account() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Request body must be a JSON object."), 400

        email = payload.get("email")
        code = payload.get("code")
        first_name = payload.get("firstName")
        last_name = payload.get("lastName")
        password = payload.get("password")
        accepted_terms = payload.get("acceptedTerms")
        if not all(isinstance(value, str) for value in (
            email, code, first_name, last_name, password
        )):
            return jsonify(error="Complete all registration fields."), 400
        if accepted_terms is not True:
            return jsonify(error="You must agree to the terms and conditions."), 400

        email = email.strip().lower()
        first_name = first_name.strip()
        last_name = last_name.strip()
        if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return jsonify(error="Enter a valid email address."), 400
        if not first_name or len(first_name) > 80:
            return jsonify(error="Enter a valid first name."), 400
        if not last_name or len(last_name) > 80:
            return jsonify(error="Enter a valid last name."), 400
        if len(password) < 8 or len(password) > 1024:
            return jsonify(error="Password must be between 8 and 1024 characters."), 400
        if not re.fullmatch(r"\d{6}", code):
            return jsonify(error="Enter the 6-digit verification code."), 400

        now = datetime.now(timezone.utc)
        with closing(connect_database(app.config["DATABASE"])) as connection:
            pending = connection.execute(
                "SELECT * FROM registration_codes WHERE email = ?",
                (email,),
            ).fetchone()
            if not pending:
                return jsonify(error="Request a verification code before registering."), 400

            expires_at = datetime.fromisoformat(pending["expires_at"])
            if now >= expires_at:
                connection.execute(
                    "DELETE FROM registration_codes WHERE email = ?",
                    (email,),
                )
                connection.commit()
                return jsonify(error="The verification code expired. Request a new one."), 410

            submitted_hash = hash_verification_code(code, pending["salt"])
            if not secrets.compare_digest(submitted_hash, pending["code_hash"]):
                attempts = pending["attempts"] + 1
                if attempts >= MAX_VERIFICATION_ATTEMPTS:
                    connection.execute(
                        "DELETE FROM registration_codes WHERE email = ?",
                        (email,),
                    )
                    connection.commit()
                    return jsonify(error="Too many incorrect codes. Request a new code."), 429
                connection.execute(
                    "UPDATE registration_codes SET attempts = ? WHERE email = ?",
                    (attempts, email),
                )
                connection.commit()
                return jsonify(
                    error="The verification code is incorrect.",
                    attemptsRemaining=MAX_VERIFICATION_ATTEMPTS - attempts,
                ), 400

            try:
                connection.execute(
                    """
                    INSERT INTO admin_account
                        (email, first_name, last_name, password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        email,
                        first_name,
                        last_name,
                        generate_password_hash(password),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return jsonify(error="An account with this email already exists."), 409

            connection.execute(
                "DELETE FROM registration_codes WHERE email = ?",
                (email,),
            )
            connection.commit()

        return jsonify(message="Email verified and account created."), 201

    @app.get("/api/alerts")
    def get_alerts() -> Any:
        if "admin_email" not in session:
            return jsonify(error="Sign in to access the clinic dashboard."), 401
        with closing(connect_database(app.config["DATABASE"])) as connection:
            rows = connection.execute(
                """
                SELECT alerts.id, alerts.type, alerts.device_id, alerts.received_at,
                       alerts.status, alerts.is_sample, devices.building, devices.floor,
                       (SELECT sms_logs.delivery_status FROM sms_logs
                        WHERE sms_logs.alert_id = alerts.id
                        ORDER BY sms_logs.id DESC LIMIT 1) AS sms_status
                FROM alerts
                JOIN devices ON devices.id = alerts.device_id
                ORDER BY alerts.received_at DESC
                """
            ).fetchall()
        return jsonify([alert_as_dict(row) for row in rows])

    @app.patch("/api/alerts/<alert_id>")
    def update_alert(alert_id: str) -> Any:
        if "admin_email" not in session:
            return jsonify(error="Sign in to access the clinic dashboard."), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Request body must be a JSON object."), 400

        status = payload.get("status")
        if not isinstance(status, str) or status not in ALLOWED_STATUSES:
            return jsonify(error="Status must be New, In progress, or Resolved."), 400

        with closing(connect_database(app.config["DATABASE"])) as connection:
            connection.execute(
                "UPDATE alerts SET status = ? WHERE id = ?",
                (status, alert_id),
            )
            row = connection.execute(
                """
                SELECT alerts.id, alerts.type, alerts.device_id, alerts.received_at,
                       alerts.status, alerts.is_sample, devices.building, devices.floor,
                       (SELECT sms_logs.delivery_status FROM sms_logs
                        WHERE sms_logs.alert_id = alerts.id
                        ORDER BY sms_logs.id DESC LIMIT 1) AS sms_status
                FROM alerts
                JOIN devices ON devices.id = alerts.device_id
                WHERE alerts.id = ?
                """,
                (alert_id,),
            ).fetchone()
            connection.commit()

        if row is None:
            return jsonify(error="Alert not found."), 404
        return jsonify(alert_as_dict(row))

    @app.get("/api/admin/guard-contact")
    def get_guard_contact() -> Any:
        if "admin_email" not in session:
            return jsonify(error="Sign in to manage the guard contact."), 401
        with closing(connect_database(app.config["DATABASE"])) as connection:
            row = connection.execute(
                "SELECT phone_number FROM guard_contacts WHERE id = 1"
            ).fetchone()
        return jsonify(phoneNumber=row["phone_number"] if row else "")

    @app.put("/api/admin/guard-contact")
    def update_guard_contact() -> Any:
        if "admin_email" not in session:
            return jsonify(error="Sign in to manage the guard contact."), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Request body must be a JSON object."), 400

        phone_number = payload.get("phoneNumber")
        if not isinstance(phone_number, str):
            return jsonify(error="Enter a valid Philippine mobile number."), 400
        phone_number = phone_number.strip()
        normalized_number = (
            normalize_philippine_mobile_number(phone_number) if phone_number else ""
        )
        if phone_number and normalized_number is None:
            return jsonify(
                error=(
                    "Enter a Philippine mobile number as 09XXXXXXXXX or "
                    "+639XXXXXXXXX."
                )
            ), 400

        with closing(connect_database(app.config["DATABASE"])) as connection:
            if normalized_number:
                connection.execute(
                    """
                    INSERT INTO guard_contacts (id, phone_number, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        phone_number = excluded.phone_number,
                        updated_at = excluded.updated_at
                    """,
                    (normalized_number, datetime.now(timezone.utc).isoformat()),
                )
            else:
                connection.execute("DELETE FROM guard_contacts WHERE id = 1")
            connection.commit()
        return jsonify(phoneNumber=normalized_number)

    @app.post("/api/devices/<device_id>/alerts")
    def create_device_alert(device_id: str) -> Any:
        device_api_key = app.config["RESQNET_DEVICE_API_KEY"]
        if not device_api_key:
            return jsonify(error="Device alert access is not configured on the server."), 503
        submitted_key = request.headers.get("X-Device-Key", "")
        if not hmac.compare_digest(submitted_key, device_api_key):
            return jsonify(error="Device API key is invalid."), 401

        payload = request.get_json(silent=True)
        if request.data and not isinstance(payload, dict):
            return jsonify(error="Request body must be a JSON object."), 400
        alert_type = payload.get("type", "Assistance") if payload else "Assistance"
        if (
            not isinstance(alert_type, str)
            or not alert_type.strip()
            or len(alert_type) > 50
        ):
            return jsonify(error="Alert type must be between 1 and 50 characters."), 400

        now = datetime.now(timezone.utc)
        alert_id = f"ALT-{now.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3).upper()}"
        with closing(connect_database(app.config["DATABASE"])) as connection:
            device = connection.execute(
                "SELECT name, building, floor FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if device is None:
                return jsonify(error="Device not found."), 404

            contact = connection.execute(
                "SELECT phone_number FROM guard_contacts WHERE id = 1"
            ).fetchone()
            recipient = contact["phone_number"] if contact else None
            message = (
                f"RESQNET: Rescue needed at {device['building']}, {device['floor']}. "
                f"Station: {device['name']} ({device_id}). Alert: {alert_id}."
            )
            connection.execute(
                """
                INSERT INTO alerts (id, type, device_id, received_at, status)
                VALUES (?, ?, ?, ?, 'New')
                """,
                (alert_id, alert_type.strip(), device_id, now.isoformat()),
            )
            cursor = connection.execute(
                """
                INSERT INTO sms_logs
                    (alert_id, recipient_number, message_content, delivery_status)
                VALUES (?, ?, ?, ?)
                """,
                (
                    alert_id,
                    recipient,
                    message,
                    "Pending" if recipient else "Not configured",
                ),
            )
            sms_log_id = cursor.lastrowid
            connection.commit()

        notification_error = None
        sms_status = "Not configured"
        if recipient:
            try:
                provider_sid, sms_status = send_guard_sms(app, recipient, message)
                provider_response = f"{provider_sid} ({sms_status})"
                sent_at = datetime.now(timezone.utc).isoformat()
            except SmsProviderError as error:
                app.logger.error("Guard SMS failed for alert %s: %s", alert_id, error)
                sms_status = "Failed"
                provider_response = str(error)
                sent_at = None
                notification_error = str(error)

            with closing(connect_database(app.config["DATABASE"])) as connection:
                connection.execute(
                    """
                    UPDATE sms_logs
                    SET delivery_status = ?, sent_at = ?, provider_response = ?
                    WHERE id = ?
                    """,
                    (sms_status, sent_at, provider_response, sms_log_id),
                )
                connection.commit()

        with closing(connect_database(app.config["DATABASE"])) as connection:
            alert = connection.execute(
                """
                SELECT alerts.id, alerts.type, alerts.device_id, alerts.received_at,
                       alerts.status, alerts.is_sample, devices.building, devices.floor,
                       (SELECT sms_logs.delivery_status FROM sms_logs
                        WHERE sms_logs.alert_id = alerts.id
                        ORDER BY sms_logs.id DESC LIMIT 1) AS sms_status
                FROM alerts
                JOIN devices ON devices.id = alerts.device_id
                WHERE alerts.id = ?
                """,
                (alert_id,),
            ).fetchone()

        response = {"alert": alert_as_dict(alert)}
        if notification_error:
            response["error"] = (
                "The alert was recorded, but the guard SMS could not be sent: "
                f"{notification_error}"
            )
            return jsonify(response), 502
        if not recipient:
            response["error"] = (
                "The alert was recorded, but no guard phone number is configured."
            )
            return jsonify(response), 503
        return jsonify(response), 201

    @app.get("/api/devices")
    def get_devices() -> Any:
        if "admin_email" not in session:
            return jsonify(error="Sign in to access the clinic dashboard."), 401
        with closing(connect_database(app.config["DATABASE"])) as connection:
            rows = connection.execute(
                """
                SELECT id, name, building, floor, online
                FROM devices
                ORDER BY name
                """
            ).fetchall()
        return jsonify(
            [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "building": row["building"],
                    "floor": row["floor"],
                    "online": bool(row["online"]),
                }
                for row in rows
            ]
        )

    return app


app = create_app()
app.logger.setLevel(logging.INFO)


if __name__ == "__main__":
    app.run()
