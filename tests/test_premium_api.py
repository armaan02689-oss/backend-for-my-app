"""Backend tests for Razorpay Orders-based premium flow + premium gating + scan limit.

Iteration 3: switched from Subscriptions API to Orders API. Signature is now
HMAC(order_id|payment_id, key_secret). Verify endpoint also requires the
order_id to exist in db.subscriptions for the given session_id (security check).
One-time ₹99 charge grants 30 days of premium.
"""
import os
import io
import hmac
import hashlib
import base64
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from PIL import Image, ImageDraw, ImageFont
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://homework-helper-549.preview.emergentagent.com",
).rstrip("/")
API = f"{BASE_URL}/api"

RAZORPAY_KEY_ID = "rzp_test_SoI1SOFUPz11Nv"
RAZORPAY_KEY_SECRET = "1EcFq75vF5u30FgWzQv6Xqwo"

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


# ---------- helpers ----------
def make_math_image_b64():
    img = Image.new("RGB", (400, 200), color=(255, 250, 230))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60
        )
    except Exception:
        font = ImageFont.load_default()
    d.text((40, 60), "7 + 5 = ?", fill=(20, 20, 20), font=font)
    d.rectangle([(10, 10), (390, 190)], outline=(60, 60, 60), width=4)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def new_session(prefix="pytest_v3"):
    return f"{prefix}_{base64.b32encode(os.urandom(5)).decode().lower()}"


def create_order(session_id):
    """Call /subscription/create and return JSON body."""
    r = requests.post(
        f"{API}/subscription/create", json={"session_id": session_id}, timeout=30
    )
    assert r.status_code == 200, r.text
    return r.json()


def sign(order_id, payment_id):
    body = f"{order_id}|{payment_id}".encode()
    return hmac.new(RAZORPAY_KEY_SECRET.encode(), body, hashlib.sha256).hexdigest()


def mark_premium(session_id):
    """Create real order then verify with computed signature to mark session premium."""
    od = create_order(session_id)
    order_id = od["order_id"]
    payment_id = "pay_TEST_" + uuid.uuid4().hex[:10]
    r = requests.post(
        f"{API}/subscription/verify",
        json={
            "session_id": session_id,
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": sign(order_id, payment_id),
        },
    )
    assert r.status_code == 200, r.text
    return order_id, payment_id


@pytest.fixture(scope="module")
def math_img():
    return make_math_image_b64()


# ---------- /api/premium/status (fresh session) ----------
class TestPremiumStatusFresh:
    def test_fresh_session_defaults(self):
        sid = new_session("pytest_v3_fresh")
        r = requests.get(f"{API}/premium/status", params={"session_id": sid})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["is_premium"] is False
        assert data["scans_used"] == 0
        assert data["scans_limit"] == 5
        assert data["price_paise"] == 9900
        assert data.get("expires_at") is None
        assert "_id" not in data


# ---------- /api/subscription/create (real Razorpay Orders test API) ----------
class TestOrderCreate:
    def test_create_returns_order(self):
        sid = new_session("pytest_v3_create")
        data = create_order(sid)
        assert data["order_id"].startswith("order_")
        assert data["key_id"] == RAZORPAY_KEY_ID
        assert data["amount_paise"] == 9900
        assert data["currency"] == "INR"
        assert "subscription_id" not in data  # ensure flipped to orders
        assert "_id" not in data


# ---------- /api/subscription/verify ----------
class TestVerify:
    def test_invalid_signature(self):
        sid = new_session("pytest_v3_bad_sig")
        # Create a real order first
        od = create_order(sid)
        r = requests.post(
            f"{API}/subscription/verify",
            json={
                "session_id": sid,
                "razorpay_order_id": od["order_id"],
                "razorpay_payment_id": "pay_FAKE",
                "razorpay_signature": "deadbeef" * 8,
            },
        )
        assert r.status_code == 400
        assert "signature" in r.text.lower()

    def test_valid_signature_but_unknown_order_rejected(self):
        """Security: valid HMAC for an order not in DB for this session must fail."""
        sid = new_session("pytest_v3_unknown")
        fake_order = "order_NOTREAL" + uuid.uuid4().hex[:10]
        payment_id = "pay_TEST_" + uuid.uuid4().hex[:10]
        r = requests.post(
            f"{API}/subscription/verify",
            json={
                "session_id": sid,
                "razorpay_order_id": fake_order,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": sign(fake_order, payment_id),
            },
        )
        assert r.status_code == 400
        # Should be "Order not found for this session"
        assert "order" in r.text.lower() or "not found" in r.text.lower()

    def test_valid_signature_marks_premium_30_days(self):
        sid = new_session("pytest_v3_ok")
        order_id, _ = mark_premium(sid)

        # premium status now True with ~30-day expiry
        s = requests.get(f"{API}/premium/status", params={"session_id": sid}).json()
        assert s["is_premium"] is True
        assert s.get("expires_at"), s
        exp = datetime.fromisoformat(s["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta = exp - datetime.now(timezone.utc)
        # between 29 and 31 days
        assert timedelta(days=29) < delta < timedelta(days=31), delta


# ---------- expiry honored ----------
class TestExpiryEnforcement:
    def test_expired_premium_reports_not_premium(self):
        sid = new_session("pytest_v3_exp")
        order_id, _ = mark_premium(sid)
        # premium right after verify
        s = requests.get(f"{API}/premium/status", params={"session_id": sid}).json()
        assert s["is_premium"] is True

        # Backdate expires_at directly in db
        async def backdate():
            client = AsyncIOMotorClient(MONGO_URL)
            db = client[DB_NAME]
            past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            await db.subscriptions.update_one(
                {"order_id": order_id}, {"$set": {"expires_at": past}}
            )
            client.close()

        asyncio.get_event_loop().run_until_complete(backdate())
        s2 = requests.get(f"{API}/premium/status", params={"session_id": sid}).json()
        assert s2["is_premium"] is False


# ---------- science gating ----------
class TestScienceGating:
    def test_scan_science_blocked_for_free(self, math_img):
        sid = new_session("pytest_v3_sci_block")
        r = requests.post(
            f"{API}/scan",
            json={
                "session_id": sid,
                "subject": "science",
                "image_base64": math_img,
            },
        )
        assert r.status_code == 402

    def test_ask_science_blocked_for_free(self):
        sid = new_session("pytest_v3_ask_sci_block")
        r = requests.post(
            f"{API}/ask",
            json={
                "session_id": sid,
                "subject": "science",
                "question": "Why is the sky blue?",
            },
        )
        assert r.status_code == 402


class TestPremiumUnlocks:
    def test_premium_can_ask_science(self):
        sid = new_session("pytest_v3_sci_premium")
        mark_premium(sid)
        r = requests.post(
            f"{API}/ask",
            json={
                "session_id": sid,
                "subject": "science",
                "question": "Name one planet.",
            },
            timeout=90,
        )
        assert r.status_code == 200, r.text
        assert "_id" not in r.json()
        requests.delete(f"{API}/history", params={"session_id": sid})


# ---------- webhook ----------
class TestWebhook:
    def test_payment_captured_activates(self):
        sid = new_session("pytest_v3_wh")
        od = create_order(sid)
        order_id = od["order_id"]
        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {"id": "pay_WH_x", "order_id": order_id}
                }
            },
        }
        r = requests.post(f"{API}/webhook/razorpay", json=payload)
        # webhook secret is empty in env → no signature check → 200
        assert r.status_code == 200, r.text

        s = requests.get(f"{API}/premium/status", params={"session_id": sid}).json()
        assert s["is_premium"] is True
        assert s.get("expires_at")


# ---------- existing endpoints still work ----------
class TestRegression:
    def test_root_ok(self):
        r = requests.get(f"{API}/")
        assert r.status_code == 200

    def test_ask_math_still_works(self):
        sid = new_session("pytest_v3_reg")
        r = requests.post(
            f"{API}/ask",
            json={"session_id": sid, "subject": "math", "question": "What is 7*8?"},
            timeout=60,
        )
        assert r.status_code == 200
        assert "56" in r.json()["answer"]
        requests.delete(f"{API}/history", params={"session_id": sid})

    def test_history_no_objectid(self):
        sid = new_session("pytest_v3_reg_hist")
        requests.post(
            f"{API}/ask",
            json={"session_id": sid, "subject": "english", "question": "Define noun"},
            timeout=60,
        )
        items = requests.get(
            f"{API}/history", params={"session_id": sid}
        ).json()["items"]
        for it in items:
            assert "_id" not in it
        requests.delete(f"{API}/history", params={"session_id": sid})
