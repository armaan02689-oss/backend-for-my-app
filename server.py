from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
from openai import OpenAI

# Setup
app = FastAPI()
api_router = APIRouter(prefix="/api")

# Groq AI setup
client = OpenAI(
    api_key=os.environ.get("GROQ_API_KEY", ""),
    base_url="https://api.groq.com/openai/v1"
)

UPI_ID = "9319300296@ybl"
PRICE = 89

class AskRequest(BaseModel):
    question: str
    subject: str = "general"

@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.get("/upi/config")
async def upi_config():
    amount_str = f"{PRICE}.00"
    return {
        "upi_id": UPI_ID,
        "name": "Buddy Premium",
        "amount": PRICE,
        "amount_paise": PRICE * 100,
        "upi_link": f"upi://pay?pa={UPI_ID}&pn=Buddy%20Premium&am={amount_str}&cu=INR"
    }

@api_router.post("/upi/claim")
async def upi_claim(data: dict):
    return {"ok": True, "is_premium": True, "status": "pending_verification"}

@api_router.post("/ask")
async def ask_question(req: AskRequest):
    try:
        prompt = f"You are Buddy, a friendly homework helper for kids. Subject: {req.subject}. Question: {req.question}. Give a clear, simple, step-by-step answer. Be encouraging and fun!"
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        return {"answer": response.choices[0].message.content}
    except Exception as e:
        return {"answer": f"Buddy is thinking... try again! Error: {str(e)[:100]}"}

@api_router.get("/premium/status")
async def premium_status():
    return {"is_premium": False, "scans_used": 0, "scans_limit": 5, "price_paise": PRICE * 100}

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
