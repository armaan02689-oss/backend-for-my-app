from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import hashlib
import httpx
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

# --- Request models ---
class AskRequest(BaseModel):
    question: str
    subject: str = "general"
    session_id: str = ""

class GenerateUroPayOrder(BaseModel):
    amount_paise: int = 9900
    customer_name: str = "Buddy User"
    customer_email: str = "user@example.com"

class UpdateUroPayOrder(BaseModel):
    order_id: str
    utr: str

class ScanRequest(BaseModel):
    subject: str = "general"
    image_base64: str = ""
    question_text: str = ""

history_store = {}

# ---------- Root ----------
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

# ---------- AI Ask (Gemini) ----------
@api_router.post("/ask")
async def ask_question(req: AskRequest):
    if not gemini_model:
        return {"answer": "AI is not configured properly."}

    prompt = (
        f"You are Buddy, a friendly homework helper for kids. "
        f"Subject: {req.subject}. "
        f"Question: {req.question}. "
        f"IMPORTANT: Keep your answer SHORT and to the point. "
        f"If it's a simple math question like 2+2, just give the answer with one short line. "
        f"For bigger questions, give a brief step-by-step explanation in under 150 words."
    )
    try:
        response = gemini_model.generate_content(prompt)
        answer = response.text
        if req.session_id:
            import uuid, datetime
            item = {
                "id": str(uuid.uuid4()),
                "subject": req.subject,
                "question": req.question,
                "answer": answer,
                "created_at": datetime.datetime.now().isoformat()
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

# ---------- UroPay: Generate QR ----------
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

# ---------- Premium status ----------
@api_router.get("/premium/status")
async def premium_status(session_id: str = ""):
    return {
        "is_premium": False,
        "scans_used": 0,
        "scans_limit": 5,
        "price_paise": 9900
    }

# ---------- Scan ----------
@api_router.post("/scan")
async def scan_homework(req: ScanRequest):
    if not req.image_base64:
        return {"answer": "Please upload a photo."}
    if not gemini_model:
        return {"answer": "AI is not configured."}
    prompt = f"Look at this homework image. Subject: {req.subject}. Solve the problem briefly."
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

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
