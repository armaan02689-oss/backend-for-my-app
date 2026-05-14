from fastapi import FastAPI, APIRouter, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import hashlib
import httpx
from openai import OpenAI

app = FastAPI()
api_router = APIRouter(prefix="/api")

# --- UroPay Configuration ---
UROPAY_API_KEY = os.environ.get("UROPAY_API_KEY", "YOUR_API_KEY_HERE")
UROPAY_SECRET = os.environ.get("UROPAY_SECRET", "YOUR_SECRET_HERE")
UROPAY_BASE_URL = "https://api.uropay.me"

def get_uropay_headers():
    """Create the headers required for UroPay API requests."""
    hashed_secret = hashlib.sha512(UROPAY_SECRET.encode("utf-8")).hexdigest()
    return {
        "X-API-KEY": UROPAY_KEY,
        "Authorization": f"Bearer {hashed_secret}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

# --- Groq AI (unchanged) ---
groq_client = OpenAI(
    api_key=os.environ.get("GROQ_API_KEY", ""),
    base_url="https://api.groq.com/openai/v1"
)

class AskRequest(BaseModel):
    question: str
    subject: str = "general"

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

# --- UroPay: Generate QR Code ---
class GenerateOrderRequest(BaseModel):
    amount_paise: int = 9900
    customer_name: str = "Buddy User"
    customer_email: str = "user@example.com"

@api_router.post("/upi/generate-qr")
async def generate_upi_qr(req: GenerateOrderRequest):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{UROPAY_BASE_URL}/order/generate",
            headers=get_uropay_headers(),
            json={
                "amount": req.amount_paise,
                "merchantOrderId": f"buddy_{os.urandom(4).hex()}",
                "customerName": req.customer_name,
                "customerEmail": req.customer_email,
                "transactionNote": "Buddy Premium"
            }
        )
        data = response.json()
        return {
            "qr_code": data["data"]["qrCode"],
            "upi_link": data["data"]["upiString"],
            "order_id": data["data"]["uroPayOrderId"]
        }

# --- UroPay: Verify Payment & Activate Premium ---
class VerifyUpiRequest(BaseModel):
    order_id: str
    utr: str
    session_id: str

@api_router.post("/upi/verify")
async def verify_upi_payment(req: VerifyUpiRequest):
    async with httpx.AsyncClient() as client:
        # Submit the UTR to UroPay
        await client.patch(
            f"{UROPAY_BASE_URL}/order/update",
            headers=get_uropay_headers(),
            json={
                "uroPayOrderId": req.order_id,
                "referenceNumber": req.utr
            }
        )
        # Check the order status
        status_response = await client.get(
            f"{UROPAY_BASE_URL}/order/status/{req.order_id}",
            headers={"X-API-KEY": UROPAY_API_KEY, "Accept": "application/json"}
        )
        status_data = status_response.json()
        is_completed = status_data.get("data", {}).get("orderStatus") == "COMPLETED"
        
        return {
            "is_premium": is_completed,
            "message": "Premium activated!" if is_completed else "Payment verification in progress..."
        }

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
