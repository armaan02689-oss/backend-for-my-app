from fastapi import FastAPI, APIRouter, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import hashlib
import httpx
import time
from datetime import datetime, timezone

# --- Google Gemini ---
try:
    import google.generativeai as genai
    genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
    gemini_model = genai.GenerativeModel("gemini-2.0-flash-lite")
except Exception as e:
    print("Gemini init error:", e)
    gemini_model = None

app = FastAPI()
api_router = APIRouter(prefix="/api")

# --- UroPay Configuration ---
UROPAY_API_KEY = os.environ.get("UROPAY_API_KEY", "")
UROPAY_SECRET = os.environ.get("UROPAY_SECRET", "")
UROPAY_BASE_URL = "https://api.uropay.me"

def get_uropay_headers():
    hashed_secret = hashlib.sha512(UROPAY_SECRET.encode("utf-8")).hexdigest()
    return {
        "X-API-KEY": UROPAY_API_KEY,
        "Authorization": f"Bearer {hashed_secret}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

# --- Premium config ---
PREMIUM_PRICE_RUPEES = 199
PREMIUM_DAYS = 365

# In-memory stores
history_store = {}
premium_users = {}
pending_orders = {}

# ---------- Models ----------
class AskRequest(BaseModel):
    question: str
    subject: str = "general"
    session_id: str = ""

class ScanRequest(BaseModel):
    subject: str = "general"
    image_base64: str = ""
    question_text: str = ""
    session_id: str = ""

class CreateOrderRequest(BaseModel):
    session_id: str

class VerifyOrderRequest(BaseModel):
    order_id: str
    session_id: str

# ---------- Root ----------
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

# ---------- AI Ask (Gemini) ----------
@api_router.post("/ask")
async def ask_question(req: AskRequest):
    if not gemini_model:
        return {"answer": "AI is not configured."}
    prompt = (
        f"You are Buddy, a friendly homework helper for kids. "
        f"Subject: {req.subject}. Question: {req.question}. "
        f"Keep answers SHORT. For simple math, just give the number. "
        f"For bigger questions, give a brief step-by-step in under 150 words."
    )
    try:
        response = gemini_model.generate_content(prompt)
        answer = response.text
        if req.session_id:
            import uuid
            item = {
                "id": str(uuid.uuid4()),
                "subject": req.subject,
                "question": req.question,
                "answer": answer,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            history_store.setdefault(req.session_id, []).append(item)
        return {"answer": answer}
    except Exception as e:
        return {"answer": f"Error: {str(e)[:150]}"}

# ---------- History ----------
@api_router.get("/history")
async def get_history(session_id: str = ""):
    return {"items": history_store.get(session_id, [])}

@api_router.delete("/history")
async def clear_history(session_id: str = ""):
    if session_id in history_store:
        del history_store[session_id]
    return {"deleted": True}

@api_router.delete("/history/{item_id}")
async def delete_history_item(item_id: str, session_id: str = ""):
    if session_id in history_store:
        history_store[session_id] = [i for i in history_store[session_id] if i["id"] != item_id]
    return {"deleted": True}

# ---------- Premium status ----------
@api_router.get("/premium/status")
async def premium_status(session_id: str = ""):
    expiry = premium_users.get(session_id, 0)
    now = time.time()
    is_premium = expiry > now
    days_left = max(0, int((expiry - now) / 86400)) if is_premium else 0
    return {
        "is_premium": is_premium,
        "days_left": days_left,
        "expires_at": expiry if is_premium else None,
        "scans_used": 0,
        "scans_limit": 5,
        "price_paise": PREMIUM_PRICE_RUPEES * 100,
        "duration_days": PREMIUM_DAYS,
    }

# ---------- Scan (Gemini Vision) ----------
@api_router.post("/scan")
async def scan_homework(req: ScanRequest):
    if not req.image_base64:
        return {"answer": "Please upload a photo."}
    if not gemini_model:
        return {"answer": "AI not configured."}
    prompt = f"Look at this homework image. Subject: {req.subject}. Solve briefly."
    try:
        import base64
        image_data = base64.b64decode(req.image_base64)
        response = gemini_model.generate_content([
            prompt,
            {"mime_type": "image/jpeg", "data": image_data}
        ])
        return {"answer": response.text}
    except Exception as e:
        return {"answer": f"Error: {str(e)[:150]}"}

# ---------- UroPay: Create Payment Order (DEBUG VERSION) ----------
@api_router.post("/payment/create-order")
async def create_payment_order(req: CreateOrderRequest):
    order_id = f"buddy_{int(time.time())}_{os.urandom(2).hex()}"
    amount_paise = PREMIUM_PRICE_RUPEES * 100

    request_body = {
        "vpa": "9319300296@ybl",
        "vpaName": "AsksBuddy",
        "amount": amount_paise,
        "merchantOrderId": order_id,
        "customerName": "Buddy User",
        "customerEmail": "user@example.com",
        "transactionNote": "AsksBuddy Premium"
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{UROPAY_BASE_URL}/order/generate",
                headers=get_uropay_headers(),
                json=request_body
            )
            print("UroPay status:", response.status_code)
            print("UroPay response:", response.text)

            if response.status_code != 200:
                return {
                    "error": "UroPay rejected the request",
                    "status_code": response.status_code,
                    "uropay_response": response.text[:500],
                    "sent_body": request_body,
                    "has_api_key": bool(UROPAY_API_KEY),
                    "has_secret": bool(UROPAY_SECRET)
                }

            data = response.json()
            if "data" not in data:
                return {"error": "No data in response", "details": data}

            pending_orders[order_id] = {
                "uropay_order_id": data["data"].get("uroPayOrderId"),
                "amount": PREMIUM_PRICE_RUPEES,
                "created_at": time.time(),
                "session_id": req.session_id
            }

            return {
                "order_id": order_id,
                "amount": PREMIUM_PRICE_RUPEES,
                "qr_code": data["data"]["qrCode"],
                "upi_link": data["data"]["upiString"],
                "uropay_order_id": data["data"].get("uroPayOrderId")
            }
    except Exception as e:
        return {
            "error": "Exception during request",
            "error_message": str(e)[:300],
            "has_api_key": bool(UROPAY_API_KEY),
            "has_secret": bool(UROPAY_SECRET)
        }

# ---------- UroPay: Verify Payment ----------
@api_router.post("/payment/verify")
async def verify_payment(req: VerifyOrderRequest):
    order = pending_orders.get(req.order_id)
    if not order:
        return {"verified": False, "message": "Order not found or expired."}

    if time.time() - order["created_at"] > 1800:
        del pending_orders[req.order_id]
        return {"verified": False, "message": "Order expired. Please try again."}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{UROPAY_BASE_URL}/order/status/{order['uropay_order_id']}",
                headers={"X-API-KEY": UROPAY_API_KEY, "Accept": "application/json"}
            )
            data = response.json()
            status = data.get("data", {}).get("orderStatus", "PENDING")

            if status == "COMPLETED":
                expiry = time.time() + (PREMIUM_DAYS * 24 * 60 * 60)
                premium_users[req.session_id] = expiry
                del pending_orders[req.order_id]
                return {
                    "verified": True,
                    "message": "Premium unlocked for 1 year! 🎉",
                    "expires_at": expiry,
                    "days": PREMIUM_DAYS
                }

            return {
                "verified": False,
                "message": "Payment not confirmed yet. Try again in a moment.",
                "current_status": status
            }
    except Exception as e:
        return {"verified": False, "message": f"Error: {str(e)[:150]}"}

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
