from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import hashlib
import httpx
import imaplib
import email
import re
import random
import time
from datetime import datetime, timezone
from typing import Optional

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

# --- Gmail IMAP config (for payment verification) ---
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
YOUR_UPI_ID = "9319300296@ybl"

# In-memory stores
history_store = {}          # { session_id: [items] }
premium_users = set()       # session_ids with active premium
pending_orders = {}         # { order_id: { amount, created_at, session_id } }
used_utrs = set()           # prevent replay attacks

# ---------- Request models ----------
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
    return {
        "is_premium": session_id in premium_users,
        "scans_used": 0,
        "scans_limit": 5,
        "price_paise": 9900
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

# ============================================================
# PAYMENT / PREMIUM UNLOCK
# ============================================================

def read_gmail_for_payment(expected_amount: float, max_age_minutes: int = 15):
    """
    Connect to Gmail via IMAP, search recent emails for a bank credit
    alert matching the expected amount, and return (utr, amount) if found.
    """
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return None

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        # Search emails from the last hour
        since_date = (datetime.now() - __import__("datetime").timedelta(hours=1)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{since_date}")')
        if status != "OK":
            mail.logout()
            return None

        email_ids = messages[0].split()
        # Check most recent 30 emails
        for eid in reversed(email_ids[-30:]):
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body += part.get_payload(decode=True).decode(errors="ignore")
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")

            body_lower = body.lower()
            # Look for credit/payment keywords
            if not any(k in body_lower for k in ["credited", "received", "payment", "upi"]):
                continue

            # Find amount (e.g., Rs.99.37 or ₹99.37 or INR 99.37)
            amounts = re.findall(r'(?:rs\.?|inr|₹)\s*([\d,]+\.?\d*)', body_lower)
            for amt_str in amounts:
                amt = float(amt_str.replace(",", ""))
                if abs(amt - expected_amount) < 0.01:
                    # Find UTR (12-digit number)
                    utrs = re.findall(r'\b(\d{12})\b', body)
                    if utrs:
                        mail.logout()
                        return {"utr": utrs[0], "amount": amt}

        mail.logout()
    except Exception as e:
        print("Gmail read error:", e)

    return None


@api_router.post("/payment/create-order")
async def create_payment_order(req: CreateOrderRequest):
    """Generate a unique amount QR code for the user to pay."""
    # Add random paise to make each order unique (e.g., 99.01 to 99.99)
    base_amount = 99.00
    random_paise = random.randint(1, 99) / 100
    unique_amount = round(base_amount + random_paise, 2)

    order_id = f"ord_{int(time.time())}_{random.randint(1000, 9999)}"

    pending_orders[order_id] = {
        "amount": unique_amount,
        "created_at": time.time(),
        "session_id": req.session_id
    }

    upi_link = (
        f"upi://pay?pa={YOUR_UPI_ID}"
        f"&pn=AsksBuddy"
        f"&am={unique_amount}"
        f"&cu=INR"
        f"&tn=AsksBuddy Premium {order_id}"
    )

    return {
        "order_id": order_id,
        "amount": unique_amount,
        "upi_id": YOUR_UPI_ID,
        "upi_link": upi_link
    }


@api_router.post("/payment/verify")
async def verify_payment(req: VerifyOrderRequest):
    """
    Check Gmail for a matching payment. If found and UTR is new,
    unlock Premium for the session.
    """
    order = pending_orders.get(req.order_id)
    if not order:
        return {"verified": False, "message": "Order not found or expired."}

    # Expire orders older than 30 minutes
    if time.time() - order["created_at"] > 1800:
        del pending_orders[req.order_id]
        return {"verified": False, "message": "Order expired. Please try again."}

    # Search Gmail for the payment
    result = read_gmail_for_payment(order["amount"])

    if result:
        utr = result["utr"]

        # Prevent replay attacks (same UTR used twice)
        if utr in used_utrs:
            return {"verified": False, "message": "This transaction was already used."}

        # ✅ Payment verified!
        used_utrs.add(utr)
        premium_users.add(req.session_id)
        del pending_orders[req.order_id]

        return {
            "verified": True,
            "message": "Premium unlocked! 🎉",
            "utr": utr,
            "amount": result["amount"]
        }

    return {"verified": False, "message": "Payment not found yet. Wait a few seconds and try again."}


app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
