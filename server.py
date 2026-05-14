from fastapi import FastAPI, APIRouter, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import razorpay
import hmac
import hashlib
import json

app = FastAPI()
api_router = APIRouter(prefix="/api")

# Razorpay test keys
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "rzp_test_Sp7hZ2Ji3dXA8k")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "JmrynNMIIvAGbuZlDasvdbF6")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# In-memory premium storage (replace with DB in production)
premium_users = set()  # store session_ids with active premium

class AskRequest(BaseModel):
    question: str
    subject: str = "general"

# ---------- AI endpoint (unchanged) ----------
from openai import OpenAI
groq_client = OpenAI(
    api_key=os.environ.get("GROQ_API_KEY", ""),
    base_url="https://api.groq.com/openai/v1"
)

@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/ask")
async def ask_question(req: AskRequest):
    try:
        prompt = f"You are Buddy, a friendly homework helper for kids. Subject: {req.subject}. Question: {req.question}. Give a clear, simple, step-by-step answer. Be encouraging and fun!"
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        return {"answer": response.choices[0].message.content}
    except Exception as e:
        return {"answer": f"Buddy is thinking... try again! Error: {str(e)[:100]}"}

# ---------- Premium status ----------
@api_router.get("/premium/status")
async def premium_status(session_id: str):
    return {
        "is_premium": session_id in premium_users,
        "scans_used": 0,
        "scans_limit": 5,
        "price_paise": 9900
    }

# ---------- Razorpay: create order ----------
class CreateOrderRequest(BaseModel):
    session_id: str

@api_router.post("/subscription/create")
async def create_order(req: CreateOrderRequest):
    amount = 9900  # ₹99 in paise
    order = razorpay_client.order.create({
        "amount": amount,
        "currency": "INR",
        "payment_capture": 1  # auto capture
    })
    return {
        "order_id": order["id"],
        "key_id": RAZORPAY_KEY_ID,
        "amount_paise": amount,
        "currency": "INR"
    }

# ---------- Razorpay: verify payment & activate premium ----------
class VerifyPaymentRequest(BaseModel):
    session_id: str
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@api_router.post("/subscription/verify")
async def verify_payment(req: VerifyPaymentRequest):
    # Verify signature
    body = f"{req.razorpay_order_id}|{req.razorpay_payment_id}".encode()
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(req.razorpay_signature, expected_signature):
        return {"error": "Invalid signature"}, 400
    
    # Activate premium for this session
    premium_users.add(req.session_id)
    return {"is_premium": True, "message": "Premium activated!"}

# ---------- UPI config (for QR code) ----------
@api_router.get("/upi/config")
async def upi_config():
    return {
        "upi_id": "9319300296@ybl",
        "name": "Buddy Premium",
        "amount": 99,
        "amount_paise": 9900,
        "upi_link": "upi://pay?pa=9319300296@ybl&pn=Buddy%20Premium&am=99.00&cu=INR"
    }

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
