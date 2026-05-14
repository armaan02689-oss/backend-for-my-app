from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import google.generativeai as genai

# Setup
app = FastAPI()
api_router = APIRouter(prefix="/api")

# Gemini AI setup
genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
model = genai.GenerativeModel("gemini-2.0-flash")

class AskRequest(BaseModel):
    question: str
    subject: str = "general"

class ScanRequest(BaseModel):
    image: str = ""

@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/ask")
async def ask_question(req: AskRequest):
    try:
        prompt = f"You are Buddy, a friendly homework helper for kids. Subject: {req.subject}. Question: {req.question}. Give a clear, simple, step-by-step answer. Be encouraging!"
        response = model.generate_content(prompt)
        return {"answer": response.text}
    except Exception as e:
        return {"answer": f"Buddy is thinking... try again! Error: {str(e)[:100]}"}

@api_router.post("/scan")
async def scan_homework(req: ScanRequest):
    return {"answer": "Scan feature coming soon! Type your question instead."}

@api_router.get("/history")
async def get_history():
    return []

# Include router and add CORS
app.include_router(api_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
