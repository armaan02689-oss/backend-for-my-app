from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import hashlib
import httpx
from openai import OpenAI

app = FastAPI()
api_router = APIRouter(prefix="/api")

# --- UroPay Configuration ---
UROPAY_API_KEY = os.environ.get("UROPAY_API_KEY", "TEST_C1WGEUM2TMYLZDZY")
UROPAY_SECRET = os.environ.get("UROPAY_SECRET", "TEST_HSIXY9M4S32S51D2W8185P5WT8636NBUMT23I9R12L643BVM3W")
UROPAY_BASE_URL = "https://api.uropay.me"

def get_uropay_headers():
    hashed_secret = hashlib.sha512(UROPAY_SECRET.encode("utf-8")).hexdigest()
    return {
        "X-API-KEY": UROPAY_API_KEY,
        "Authorization": f"Bearer {hashed_secret}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

# --- Groq AI ---
groq_client = OpenAI(
    api_key=os.environ.get("GROQ_API_KEY", ""),
    base_url="https://api.groq.com/openai/v1"
)

class AskRequest(BaseModel):
    question: str
    subject: str = "general"

# --- Root health check ---
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

# --- AI endpoint ---
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

# --- UroPay: Generate QR ---
class GenerateUroPayOrder(BaseModel):
    amount_paise: int = 9900
    customer_name: str = "Buddy User"
    customer_email: str = "user@example.com"

@api_router.post("/uropay/generate-qr")
async def generate_uropay_qr(req: GenerateUroPayOrder):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{UROPAY_BASE_URL}/order/generate",
            headers=get_uropay_headers(),
            json={
                "vpa": "9319300296@ybl",
                "vpaName": "Buddy Premium",
                "amount": req.amount_paise,
                "merchantOrderId": f"buddy_{os.urandom(4).hex()}",
                "customerName": req.customer_name,
                "customerEmail": req.customer_email,
                "transactionNote": "Buddy Premium Upgrade"
            }
        )
        data = response.json()
        if "data" not in data:
            return {"error": "Failed to generate QR", "details": data}
        return {
            "qr_code": data["data"]["qrCode"],
            "upi_link": data["data"]["upiString"],
            "order_id": data["data"]["uroPayOrderId"]
        }

# --- UroPay: Update order with UTR ---
class UpdateUroPayOrder(BaseModel):
    order_id: str
    utr: str

@api_router.post("/uropay/update-order")
async def update_uropay_order(req: UpdateUroPayOrder):
    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"{UROPAY_BASE_URL}/order/update",
            headers=get_uropay_headers(),
            json={
                "uroPayOrderId": req.order_id,
                "referenceNumber": req.utr
            }
        )
        return response.json()

# --- UroPay: Check order status ---
@api_router.get("/uropay/status/{order_id}")
async def check_uropay_status(order_id: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{UROPAY_BASE_URL}/order/status/{order_id}",
            headers={"X-API-KEY": UROPAY_API_KEY, "Accept": "application/json"}
        )
        data = response.json()
        status = data.get("data", {}).get("orderStatus", "PENDING")
        return {"status": status, "is_completed": status == "COMPLETED"}

# --- IMPORTANT: Include the router ---
app.include_router(api_router)

# --- CORS (allow all origins) ---
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
