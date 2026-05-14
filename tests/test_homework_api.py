"""Backend API tests for Homework Helper."""
import os
import io
import base64
import pytest
import requests
from PIL import Image, ImageDraw, ImageFont

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://homework-helper-549.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


def make_math_image_b64():
    """Create a real JPEG image with visible math text."""
    img = Image.new("RGB", (400, 200), color=(255, 250, 230))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60)
    except Exception:
        font = ImageFont.load_default()
    d.text((40, 60), "7 + 5 = ?", fill=(20, 20, 20), font=font)
    # Add some texture/edges
    d.rectangle([(10, 10), (390, 190)], outline=(60, 60, 60), width=4)
    d.line([(30, 150), (370, 150)], fill=(100, 100, 100), width=2)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture(scope="module")
def session_id():
    return "TEST_sess_" + base64.b32encode(os.urandom(5)).decode().lower()


@pytest.fixture(scope="module")
def math_img():
    return make_math_image_b64()


# ---------- Root ----------
def test_root():
    r = requests.get(f"{API}/")
    assert r.status_code == 200
    data = r.json()
    assert "message" in data
    assert data.get("model") == "claude-sonnet-4-5-20250929"


# ---------- /scan ----------
class TestScan:
    def test_scan_math_valid(self, session_id, math_img):
        r = requests.post(f"{API}/scan", json={
            "session_id": session_id,
            "subject": "math",
            "image_base64": math_img,
            "question_text": "Solve this please",
        }, timeout=90)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["kind"] == "scan"
        assert data["subject"] == "math"
        assert data["session_id"] == session_id
        assert isinstance(data["answer"], str) and len(data["answer"]) > 10
        assert "_id" not in data
        assert "id" in data

    def test_scan_english_valid(self, session_id, math_img):
        r = requests.post(f"{API}/scan", json={
            "session_id": session_id,
            "subject": "english",
            "image_base64": math_img,
        }, timeout=90)
        assert r.status_code == 200, r.text
        assert len(r.json()["answer"]) > 0

    def test_scan_invalid_base64(self, session_id):
        r = requests.post(f"{API}/scan", json={
            "session_id": session_id,
            "subject": "math",
            "image_base64": "!!!not_base64!!!",
        })
        assert r.status_code == 400


# ---------- /ask ----------
class TestAsk:
    def test_ask_math(self, session_id):
        r = requests.post(f"{API}/ask", json={
            "session_id": session_id,
            "subject": "math",
            "question": "What is 7 times 8?",
        }, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["kind"] == "ask"
        assert data["subject"] == "math"
        assert "_id" not in data
        assert "56" in data["answer"]  # 7*8=56

    def test_ask_english(self, session_id):
        r = requests.post(f"{API}/ask", json={
            "session_id": session_id,
            "subject": "english",
            "question": "What is a noun? Give one example.",
        }, timeout=60)
        assert r.status_code == 200
        assert len(r.json()["answer"]) > 0

    def test_ask_empty_question(self, session_id):
        r = requests.post(f"{API}/ask", json={
            "session_id": session_id,
            "subject": "math",
            "question": "   ",
        })
        assert r.status_code == 400


# ---------- /history ----------
class TestHistory:
    def test_history_list_sorted(self, session_id):
        r = requests.get(f"{API}/history", params={"session_id": session_id})
        assert r.status_code == 200
        items = r.json()["items"]
        assert isinstance(items, list)
        assert len(items) >= 1
        # Check no _id
        for it in items:
            assert "_id" not in it
            assert "id" in it
        # sorted desc
        times = [it["created_at"] for it in items]
        assert times == sorted(times, reverse=True)

    def test_history_get_item(self, session_id):
        items = requests.get(f"{API}/history", params={"session_id": session_id}).json()["items"]
        first_id = items[0]["id"]
        r = requests.get(f"{API}/history/{first_id}")
        assert r.status_code == 200
        assert r.json()["id"] == first_id
        assert "_id" not in r.json()

    def test_history_get_404(self):
        r = requests.get(f"{API}/history/nonexistent_id_xyz")
        assert r.status_code == 404

    def test_history_delete_item(self, session_id):
        items = requests.get(f"{API}/history", params={"session_id": session_id}).json()["items"]
        target = items[0]["id"]
        r = requests.delete(f"{API}/history/{target}", params={"session_id": session_id})
        assert r.status_code == 200
        assert r.json().get("deleted") is True
        # Verify gone
        g = requests.get(f"{API}/history/{target}")
        assert g.status_code == 404

    def test_history_clear_all(self, session_id):
        r = requests.delete(f"{API}/history", params={"session_id": session_id})
        assert r.status_code == 200
        assert "deleted" in r.json()
        # Verify empty
        items = requests.get(f"{API}/history", params={"session_id": session_id}).json()["items"]
        assert items == []
