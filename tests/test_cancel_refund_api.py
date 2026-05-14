"""Iteration 5: Cancel Premium + Refund endpoints.

Covers /api/premium/cancel and /api/premium/refund-requests for both
- direct UPI path (manual refund queued)
- Razorpay path (auto refund attempted; with fake test payment_id Razorpay errors
  but the endpoint must still return 200 with status='failed' and cancel the sub).
Plus regression on prior endpoints.
"""
import os
import hmac
import hashlib
import base64
import uuid

import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://homework-helper-549.preview.emergentagent.com",
).rstrip("/")
API = f"{BASE_URL}/api"

RAZORPAY_KEY_SECRET = "1EcFq75vF5u30FgWzQv6Xqwo"


def new_session(prefix="pytest_cancel"):
    return f"{prefix}_{base64.b32encode(os.urandom(5)).decode().lower()}"


def new_utr():
    return "PYT" + base64.b32encode(os.urandom(6)).decode().rstrip("=")[:10]


def sign(order_id, payment_id):
    body = f"{order_id}|{payment_id}".encode()
    return hmac.new(RAZORPAY_KEY_SECRET.encode(), body, hashlib.sha256).hexdigest()


def activate_upi(sid, utr=None):
    utr = utr or new_utr()
    r = requests.post(f"{API}/upi/claim", json={"session_id": sid, "utr": utr})
    assert r.status_code == 200, r.text
    return utr


def activate_razorpay(sid):
    r = requests.post(f"{API}/subscription/create", json={"session_id": sid})
    assert r.status_code == 200, r.text
    order_id = r.json()["order_id"]
    payment_id = "pay_TEST_" + uuid.uuid4().hex[:10]
    v = requests.post(
        f"{API}/subscription/verify",
        json={
            "session_id": sid,
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": sign(order_id, payment_id),
        },
    )
    assert v.status_code == 200, v.text
    return order_id, payment_id


# ---------- cancel: no active premium ----------
class TestCancelNoSub:
    def test_404_when_nothing_active(self):
        sid = new_session("no_sub")
        r = requests.post(
            f"{API}/premium/cancel",
            json={"session_id": sid, "reason": "test", "refund_upi_id": "x@upi"},
        )
        assert r.status_code == 404, r.text
        assert "no active" in r.json()["detail"].lower()


# ---------- cancel: UPI direct path (manual refund queued) ----------
class TestCancelUpiDirect:
    def test_upi_cancel_full_flow(self):
        sid = new_session("upi")
        utr = activate_upi(sid)

        # premium active
        s = requests.get(f"{API}/premium/status", params={"session_id": sid}).json()
        assert s["is_premium"] is True

        r = requests.post(
            f"{API}/premium/cancel",
            json={"session_id": sid, "reason": "paid by mistake", "refund_upi_id": "kid@paytm"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "_id" not in body
        assert body["is_premium"] is False
        assert len(body["refunds"]) >= 1
        ref = body["refunds"][0]
        assert ref["method"] == "upi_direct"
        assert ref["status"] == "pending_manual"
        assert ref["amount_paise"] == 9900

        # status now false
        s2 = requests.get(f"{API}/premium/status", params={"session_id": sid}).json()
        assert s2["is_premium"] is False

        # refund-requests endpoint
        rr = requests.get(f"{API}/premium/refund-requests", params={"session_id": sid})
        assert rr.status_code == 200
        items = rr.json()["items"]
        assert any(
            i.get("method") == "upi_direct"
            and i.get("utr") == utr
            and i.get("refund_upi_id") == "kid@paytm"
            and i.get("status") == "pending_manual"
            and i.get("reason") == "paid by mistake"
            for i in items
        )
        for it in items:
            assert "_id" not in it

    def test_second_cancel_returns_404(self):
        sid = new_session("upi_twice")
        activate_upi(sid)
        r1 = requests.post(
            f"{API}/premium/cancel",
            json={"session_id": sid, "reason": "", "refund_upi_id": "a@upi"},
        )
        assert r1.status_code == 200
        r2 = requests.post(
            f"{API}/premium/cancel",
            json={"session_id": sid, "reason": "", "refund_upi_id": "a@upi"},
        )
        assert r2.status_code == 404

    def test_multiple_cycles_create_multiple_refund_requests(self):
        sid = new_session("upi_multi")
        # cycle 1
        activate_upi(sid)
        requests.post(
            f"{API}/premium/cancel",
            json={"session_id": sid, "refund_upi_id": "a@upi"},
        )
        # cycle 2 with fresh UTR
        activate_upi(sid)
        requests.post(
            f"{API}/premium/cancel",
            json={"session_id": sid, "refund_upi_id": "a@upi"},
        )

        rr = requests.get(
            f"{API}/premium/refund-requests", params={"session_id": sid}
        ).json()["items"]
        assert len(rr) >= 2


# ---------- cancel: Razorpay path (refund attempts, fake id fails gracefully) ----------
class TestCancelRazorpay:
    def test_razorpay_cancel_graceful_failure(self):
        sid = new_session("rzp")
        _, payment_id = activate_razorpay(sid)

        r = requests.post(
            f"{API}/premium/cancel",
            json={"session_id": sid, "reason": "wrong tap"},
        )
        # Endpoint must return 200 even though refund API rejects fake payment id
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_premium"] is False
        assert len(body["refunds"]) >= 1
        ref = body["refunds"][0]
        assert ref["method"] == "razorpay"
        # Expected: 'failed' because pay_TEST_xxx isn't a real captured payment
        assert ref["status"] in ("failed", "processed"), ref
        if ref["status"] == "failed":
            assert "error" in ref

        # Subscription must still be cancelled
        s = requests.get(
            f"{API}/premium/status", params={"session_id": sid}
        ).json()
        assert s["is_premium"] is False


# ---------- regression on other endpoints ----------
class TestRegression:
    def test_root(self):
        assert requests.get(f"{API}/").status_code == 200

    def test_upi_config(self):
        r = requests.get(f"{API}/upi/config")
        assert r.status_code == 200
        assert "upi_link" in r.json()

    def test_history_no_objectid(self):
        sid = new_session("reg_hist")
        # write an item via ask
        r = requests.post(
            f"{API}/ask",
            json={"session_id": sid, "subject": "math", "question": "2+2?"},
            timeout=60,
        )
        assert r.status_code == 200
        items = requests.get(
            f"{API}/history", params={"session_id": sid}
        ).json()["items"]
        assert items and all("_id" not in i for i in items)
        requests.delete(f"{API}/history", params={"session_id": sid})

    def test_refund_requests_for_unknown_session_empty(self):
        sid = new_session("reg_empty")
        r = requests.get(f"{API}/premium/refund-requests", params={"session_id": sid})
        assert r.status_code == 200
        assert r.json()["items"] == []
