"""Backend tests for the new Direct UPI flow (iteration 4).

Endpoints under test:
- GET  /api/upi/config
- POST /api/upi/claim  (UTR validation, duplicate rejection, idempotency)
- Premium activation via UPI claim, science gating respects upi_direct sub
- Regression: existing /api/scan, /api/ask, /api/premium/status, no _id leaks
"""
import os
import io
import base64
import uuid
import random
import string

import pytest
import requests
from PIL import Image, ImageDraw, ImageFont
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio

def _read_env(path, key):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


BASE_URL = (
    os.environ.get("REACT_APP_BACKEND_URL")
    or _read_env("/app/frontend/.env", "REACT_APP_BACKEND_URL")
).rstrip("/")
API = f"{BASE_URL}/api"

MONGO_URL = os.environ.get("MONGO_URL") or _read_env(
    "/app/backend/.env", "MONGO_URL"
)
DB_NAME = os.environ.get("DB_NAME") or _read_env("/app/backend/.env", "DB_NAME")


def new_session(prefix="pytest_upi"):
    return f"{prefix}_{base64.b32encode(os.urandom(5)).decode().lower()}"


def random_utr(length=12):
    return "TEST" + "".join(
        random.choices(string.ascii_uppercase + string.digits, k=length - 4)
    )


def make_math_image_b64():
    img = Image.new("RGB", (300, 150), color=(255, 250, 230))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 50
        )
    except Exception:
        font = ImageFont.load_default()
    d.text((30, 40), "2 + 2 = ?", fill=(20, 20, 20), font=font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# --------- /api/upi/config ---------
class TestUpiConfig:
    def test_config_returns_merchant_details(self):
        r = requests.get(f"{API}/upi/config", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["upi_id"] == "9319300296@ybl"
        assert data["name"] == "Buddy Premium"
        assert data["amount"] == 99.0
        assert data["amount_paise"] == 9900
        assert data["upi_link"].startswith("upi://pay?pa=9319300296@ybl")
        assert "am=99.00" in data["upi_link"]
        assert "cu=INR" in data["upi_link"]
        assert "_id" not in data


# --------- /api/upi/claim validation ---------
class TestUpiClaimValidation:
    def test_short_utr_rejected(self):
        sid = new_session()
        r = requests.post(
            f"{API}/upi/claim", json={"session_id": sid, "utr": "abc"}, timeout=15
        )
        assert r.status_code == 400
        assert "utr" in r.text.lower()

    def test_invalid_chars_rejected(self):
        sid = new_session()
        r = requests.post(
            f"{API}/upi/claim",
            json={"session_id": sid, "utr": "TEST@$%1234"},
            timeout=15,
        )
        assert r.status_code == 400

    def test_too_long_utr_rejected(self):
        sid = new_session()
        r = requests.post(
            f"{API}/upi/claim",
            json={"session_id": sid, "utr": "A" * 35},
            timeout=15,
        )
        assert r.status_code == 400

    def test_hyphen_allowed(self):
        sid = new_session()
        utr = "TEST-" + uuid.uuid4().hex[:8].upper()
        r = requests.post(
            f"{API}/upi/claim", json={"session_id": sid, "utr": utr}, timeout=15
        )
        assert r.status_code == 200, r.text


# --------- /api/upi/claim happy path + duplicate + idempotent ---------
class TestUpiClaimFlow:
    def test_valid_claim_activates_premium(self):
        sid = new_session("pytest_upi_ok")
        utr = random_utr()
        r = requests.post(
            f"{API}/upi/claim",
            json={"session_id": sid, "utr": utr, "payer_note": "test"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["is_premium"] is True
        assert data.get("expires_at")
        assert "_id" not in data

        # premium status reflects this
        s = requests.get(
            f"{API}/premium/status", params={"session_id": sid}
        ).json()
        assert s["is_premium"] is True
        assert s.get("expires_at")
        assert "_id" not in s

        # ~30 days expiry
        from datetime import datetime, timezone, timedelta
        exp = datetime.fromisoformat(s["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta = exp - datetime.now(timezone.utc)
        assert timedelta(days=29) < delta < timedelta(days=31), delta

    def test_duplicate_utr_different_session_rejected_409(self):
        sid1 = new_session("pytest_upi_a")
        sid2 = new_session("pytest_upi_b")
        utr = random_utr()
        r1 = requests.post(
            f"{API}/upi/claim", json={"session_id": sid1, "utr": utr}
        )
        assert r1.status_code == 200, r1.text
        r2 = requests.post(
            f"{API}/upi/claim", json={"session_id": sid2, "utr": utr}
        )
        assert r2.status_code == 409, r2.text
        assert "already" in r2.text.lower()

    def test_same_utr_same_session_is_idempotent(self):
        sid = new_session("pytest_upi_idem")
        utr = random_utr()
        r1 = requests.post(
            f"{API}/upi/claim", json={"session_id": sid, "utr": utr}
        )
        assert r1.status_code == 200
        r2 = requests.post(
            f"{API}/upi/claim", json={"session_id": sid, "utr": utr}
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["is_premium"] is True


# --------- DB persistence ---------
class TestUpiPersistence:
    def test_upi_claim_doc_in_db(self):
        sid = new_session("pytest_upi_db")
        utr = random_utr()
        r = requests.post(
            f"{API}/upi/claim", json={"session_id": sid, "utr": utr}
        )
        assert r.status_code == 200

        async def fetch():
            c = AsyncIOMotorClient(MONGO_URL)
            doc = await c[DB_NAME].upi_claims.find_one({"utr": utr}, {"_id": 0})
            c.close()
            return doc

        doc = asyncio.get_event_loop().run_until_complete(fetch())
        assert doc is not None
        assert doc["session_id"] == sid
        assert doc["merchant_upi"] == "9319300296@ybl"
        assert doc["status"] == "claimed"
        assert doc["amount_paise"] == 9900


# --------- premium unlocks via upi_direct ---------
class TestUpiPremiumGating:
    def test_science_scan_works_after_upi_claim(self):
        sid = new_session("pytest_upi_sci")
        utr = random_utr()
        r = requests.post(
            f"{API}/upi/claim", json={"session_id": sid, "utr": utr}
        )
        assert r.status_code == 200

        img = make_math_image_b64()
        rs = requests.post(
            f"{API}/scan",
            json={
                "session_id": sid,
                "subject": "science",
                "image_base64": img,
                "question_text": "What is this?",
            },
            timeout=90,
        )
        assert rs.status_code == 200, rs.text
        body = rs.json()
        assert "_id" not in body
        assert body["subject"] == "science"
        requests.delete(f"{API}/history", params={"session_id": sid})


# --------- regression ---------
class TestRegression:
    def test_root(self):
        r = requests.get(f"{API}/")
        assert r.status_code == 200

    def test_history_no_objectid_after_upi(self):
        sid = new_session("pytest_upi_reg")
        requests.post(
            f"{API}/upi/claim",
            json={"session_id": sid, "utr": random_utr()},
        )
        r = requests.get(
            f"{API}/history", params={"session_id": sid}
        )
        assert r.status_code == 200
        for it in r.json()["items"]:
            assert "_id" not in it

    def test_subscription_create_still_works(self):
        sid = new_session("pytest_upi_rzp")
        r = requests.post(
            f"{API}/subscription/create", json={"session_id": sid}, timeout=30
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["order_id"].startswith("order_")
        assert d["amount_paise"] == 9900
        assert "_id" not in d
