from fastapi import FastAPI, APIRouter, HTTPException, Request
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import hmac
import hashlib
import logging
import uuid
import base64
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timezone, timedelta

from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
import razorpay


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY', '')
MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-5-20250929"

RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', '')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '')
RAZORPAY_WEBHOOK_SECRET = os.environ.get('RAZORPAY_WEBHOOK_SECRET', '')
PREMIUM_PRICE_PAISE = 9900  # ₹99
FREE_SCAN_LIMIT = 5
MERCHANT_UPI_ID = os.environ.get('MERCHANT_UPI_ID', '')
MERCHANT_UPI_NAME = os.environ.get('MERCHANT_UPI_NAME', 'Buddy Premium')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')

rzp_client = None
if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
    rzp_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

app = FastAPI()
api_router = APIRouter(prefix="/api")


async def get_or_create_plan() -> str:
    """Return Razorpay plan_id for ₹99/month, creating once if needed."""
    cfg = await db.app_config.find_one({"key": "premium_plan"}, {"_id": 0})
    if cfg and cfg.get("plan_id"):
        return cfg["plan_id"]
    if not rzp_client:
        raise HTTPException(status_code=500, detail="Razorpay not configured")
    plan = rzp_client.plan.create({
        "period": "monthly",
        "interval": 1,
        "item": {
            "name": "Buddy Premium",
            "amount": PREMIUM_PRICE_PAISE,
            "currency": "INR",
            "description": "Unlimited scans, Science subject, history export",
        },
    })
    await db.app_config.update_one(
        {"key": "premium_plan"},
        {"$set": {"key": "premium_plan", "plan_id": plan["id"]}},
        upsert=True,
    )
    return plan["id"]


async def is_premium(session_id: str) -> bool:
    sub = await db.subscriptions.find_one(
        {"session_id": session_id, "status": "active"}, {"_id": 0}
    )
    if not sub:
        return False
    expires_at = sub.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                return False
        except Exception:
            pass
    return True


# -------------- Models --------------
class ScanRequest(BaseModel):
    session_id: str
    subject: str  # "math" | "english" | "general"
    image_base64: str  # raw base64 (no data URL prefix)
    question_text: Optional[str] = ""


class AskRequest(BaseModel):
    session_id: str
    subject: str
    question: str


class HistoryItem(BaseModel):
    id: str
    session_id: str
    subject: str
    kind: str  # "scan" | "ask"
    question_text: Optional[str] = ""
    image_base64: Optional[str] = ""
    answer: str
    created_at: str


class HistoryResponse(BaseModel):
    items: List[HistoryItem]


SYSTEM_PROMPTS = {
    "math": (
        "You are Buddy, a super friendly study buddy for kids aged 8-12. "
        "You explain math problems clearly with simple words, lots of encouragement, "
        "and step-by-step solutions. Use numbered steps. Keep sentences short. "
        "Always end with a quick recap of the final answer. Never use scary words. "
        "If the image is unclear, kindly tell the child to try a brighter photo."
    ),
    "english": (
        "You are Buddy, a super friendly study buddy for kids aged 8-12. "
        "You help with English homework — grammar, reading, writing, spelling, vocabulary. "
        "Explain answers in simple, fun language. Use short steps and examples. "
        "Always end with the final answer or correction clearly stated."
    ),
    "general": (
        "You are Buddy, a friendly study buddy for kids aged 8-12. "
        "Help solve homework questions step by step in simple, encouraging words. "
        "If the question is math, show numbered steps. If it's English, give clear, "
        "simple explanations. End with a clear final answer."
    ),
    "science": (
        "You are Buddy, a friendly science teacher for kids aged 8-12. "
        "You explain science topics — physics, biology, chemistry, earth, space — "
        "in fun, simple language with everyday examples and short numbered steps. "
        "Always end with a one-line takeaway. If shown an image, describe what you see "
        "first, then answer."
    ),
}


def build_chat(session_id: str, subject: str) -> LlmChat:
    system_msg = SYSTEM_PROMPTS.get(subject, SYSTEM_PROMPTS["general"])
    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=session_id,
        system_message=system_msg,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)
    return chat


# -------------- Routes --------------
@api_router.get("/")
async def root():
    return {"message": "Homework Helper API running", "model": MODEL_NAME}


@api_router.post("/scan", response_model=HistoryItem)
async def scan_homework(payload: ScanRequest):
    if not EMERGENT_LLM_KEY:
        raise HTTPException(status_code=500, detail="LLM key not configured")
    if not payload.image_base64:
        raise HTTPException(status_code=400, detail="image_base64 is required")

    # Enforce free-tier scan limit (premium = unlimited)
    premium = await is_premium(payload.session_id)
    if not premium:
        used = await db.history.count_documents(
            {"session_id": payload.session_id, "kind": "scan"}
        )
        if used >= FREE_SCAN_LIMIT:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"You've used all {FREE_SCAN_LIMIT} free scans. "
                    "Upgrade to Buddy Premium for unlimited scans + Science!"
                ),
            )

    # Premium-only subject
    if payload.subject == "science" and not premium:
        raise HTTPException(status_code=402, detail="Science is a Premium feature")

    # Strip data URL prefix if present
    img_b64 = payload.image_base64
    if img_b64.startswith("data:"):
        img_b64 = img_b64.split(",", 1)[-1]

    try:
        base64.b64decode(img_b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image")

    user_text = payload.question_text or (
        "Please read this homework question carefully and solve it step by step "
        "in a kid-friendly way."
    )

    try:
        chat = build_chat(payload.session_id, payload.subject)
        image_content = ImageContent(image_base64=img_b64)
        msg = UserMessage(text=user_text, file_contents=[image_content])
        answer = await chat.send_message(msg)
    except Exception as e:
        logger.exception("scan failed")
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

    item = HistoryItem(
        id=str(uuid.uuid4()),
        session_id=payload.session_id,
        subject=payload.subject,
        kind="scan",
        question_text=payload.question_text or "",
        image_base64=img_b64,
        answer=answer if isinstance(answer, str) else str(answer),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    await db.history.insert_one(item.model_dump())
    return item


class CancelRequest(BaseModel):
    session_id: str
    reason: Optional[str] = ""
    refund_upi_id: Optional[str] = ""  # for direct-UPI refund (where to send money back)


@api_router.post("/premium/cancel")
async def cancel_premium(payload: CancelRequest):
    """Cancel premium and issue refund.
    - Razorpay payment → auto-refund via Razorpay API
    - Direct UPI    → queue manual refund (merchant must send ₹99 back)
    """
    subs = await db.subscriptions.find(
        {"session_id": payload.session_id, "status": "active"},
        {"_id": 0},
    ).to_list(length=10)
    if not subs:
        raise HTTPException(status_code=404, detail="No active premium found")

    now = datetime.now(timezone.utc)
    results = []

    for sub in subs:
        refund_info = {"method": None, "status": None, "id": None, "amount_paise": PREMIUM_PRICE_PAISE}

        # Razorpay path
        if sub.get("payment_id") and rzp_client:
            try:
                refund = rzp_client.payment.refund(
                    sub["payment_id"],
                    {"amount": PREMIUM_PRICE_PAISE, "speed": "normal"},
                )
                refund_info.update({
                    "method": "razorpay",
                    "status": "processed",
                    "id": refund.get("id"),
                })
                await db.refund_requests.insert_one({
                    "id": str(uuid.uuid4()),
                    "session_id": payload.session_id,
                    "method": "razorpay",
                    "payment_id": sub["payment_id"],
                    "order_id": sub.get("order_id"),
                    "razorpay_refund_id": refund.get("id"),
                    "amount_paise": PREMIUM_PRICE_PAISE,
                    "status": "processed",
                    "reason": payload.reason,
                    "created_at": now.isoformat(),
                })
            except Exception as e:
                logger.exception("razorpay refund failed")
                refund_info.update({
                    "method": "razorpay",
                    "status": "failed",
                    "error": str(e),
                })

        # Direct UPI path
        elif sub.get("utr"):
            await db.refund_requests.insert_one({
                "id": str(uuid.uuid4()),
                "session_id": payload.session_id,
                "method": "upi_direct",
                "utr": sub.get("utr"),
                "refund_upi_id": (payload.refund_upi_id or "").strip(),
                "amount_paise": PREMIUM_PRICE_PAISE,
                "status": "pending_manual",
                "reason": payload.reason,
                "created_at": now.isoformat(),
            })
            refund_info.update({
                "method": "upi_direct",
                "status": "pending_manual",
            })

        await db.subscriptions.update_one(
            {"session_id": payload.session_id, **(
                {"order_id": sub["order_id"]} if sub.get("order_id") else {"method": "upi_direct"}
            )},
            {"$set": {
                "status": "cancelled",
                "cancelled_at": now.isoformat(),
                "cancel_reason": payload.reason or "",
            }},
        )
        results.append(refund_info)

    return {
        "ok": True,
        "is_premium": False,
        "refunds": results,
        "note": (
            "Razorpay refunds land back to your card/UPI in 3-7 working days. "
            "Direct UPI refunds are queued — the merchant will send ₹99 back to your UPI manually."
        ),
    }


@api_router.get("/premium/refund-requests")
async def list_refund_requests(session_id: str):
    items = await db.refund_requests.find(
        {"session_id": session_id}, {"_id": 0}
    ).sort("created_at", -1).to_list(length=20)
    return {"items": items}


# -------------- Admin (merchant) --------------
from fastapi import Header


def _check_admin(token: Optional[str]):
    if not ADMIN_PASSWORD or token != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")


class AdminLoginRequest(BaseModel):
    password: str


class MarkRefundedRequest(BaseModel):
    refund_id: str
    merchant_utr: Optional[str] = ""
    note: Optional[str] = ""


class ApproveClaimRequest(BaseModel):
    claim_id: str
    note: Optional[str] = ""


class RejectClaimRequest(BaseModel):
    claim_id: str
    reason: Optional[str] = ""


@api_router.post("/admin/login")
async def admin_login(payload: AdminLoginRequest):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin not configured")
    if payload.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong password")
    return {"ok": True, "token": ADMIN_PASSWORD}


@api_router.get("/admin/refunds")
async def admin_list_refunds(
    status: Optional[str] = None,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    _check_admin(x_admin_token)
    q = {}
    if status:
        q["status"] = status
    items = await db.refund_requests.find(q, {"_id": 0}).sort("created_at", -1).to_list(length=200)
    # Attach UPI deeplinks for one-tap refunds (direct-UPI only)
    for it in items:
        if it.get("method") == "upi_direct" and it.get("refund_upi_id"):
            rs = (it.get("refund_upi_id") or "").strip()
            amt = (it.get("amount_paise") or PREMIUM_PRICE_PAISE) / 100
            it["refund_upi_link"] = (
                f"upi://pay?pa={rs}&pn=Buddy%20Refund&am={amt:.2f}&cu=INR&tn=Buddy%20Premium%20Refund"
            )
    return {"items": items}


@api_router.post("/admin/refunds/mark-refunded")
async def admin_mark_refunded(
    payload: MarkRefundedRequest,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    _check_admin(x_admin_token)
    refund = await db.refund_requests.find_one({"id": payload.refund_id}, {"_id": 0})
    if not refund:
        raise HTTPException(status_code=404, detail="Refund not found")
    if refund.get("status") == "refunded":
        return {"ok": True, "already": True}
    await db.refund_requests.update_one(
        {"id": payload.refund_id},
        {"$set": {
            "status": "refunded",
            "merchant_utr": (payload.merchant_utr or "").strip(),
            "merchant_note": (payload.note or "").strip(),
            "refunded_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"ok": True}


@api_router.get("/admin/claims")
async def admin_list_claims(
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    _check_admin(x_admin_token)
    items = await db.upi_claims.find({}, {"_id": 0}).sort("claimed_at", -1).to_list(length=200)
    return {"items": items}


@api_router.get("/admin/stats")
async def admin_stats(
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    _check_admin(x_admin_token)
    total_claims = await db.upi_claims.count_documents({})
    verified_claims = await db.upi_claims.count_documents({"status": "verified"})
    pending_claims = await db.upi_claims.count_documents({"status": "pending_verification"})
    total_active = await db.subscriptions.count_documents({"status": "active"})
    pending_refunds = await db.refund_requests.count_documents({"status": "pending_manual"})
    done_refunds = await db.refund_requests.count_documents({"status": "refunded"})
    razorpay_refunds = await db.refund_requests.count_documents({"method": "razorpay"})
    return {
        "total_upi_claims": total_claims,
        "verified_claims": verified_claims,
        "pending_verification": pending_claims,
        "active_subscriptions": total_active,
        "pending_manual_refunds": pending_refunds,
        "completed_refunds": done_refunds,
        "razorpay_refunds": razorpay_refunds,
        "amount_collected_rupees": verified_claims * (PREMIUM_PRICE_PAISE / 100),
        "amount_to_refund_rupees": pending_refunds * (PREMIUM_PRICE_PAISE / 100),
    }


@api_router.post("/admin/claims/approve")
async def admin_approve_claim(
    payload: ApproveClaimRequest,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    """Merchant verified ₹99 actually arrived in PhonePe — activate Premium."""
    _check_admin(x_admin_token)
    claim = await db.upi_claims.find_one({"id": payload.claim_id}, {"_id": 0})
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    if claim.get("status") == "verified":
        return {"ok": True, "already": True}
    if claim.get("status") == "rejected":
        raise HTTPException(status_code=400, detail="Claim was already rejected")

    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=30)

    await db.upi_claims.update_one(
        {"id": payload.claim_id},
        {"$set": {
            "status": "verified",
            "verified_at": now.isoformat(),
            "merchant_note": (payload.note or "").strip(),
        }},
    )
    await db.subscriptions.update_one(
        {"session_id": claim["session_id"], "method": "upi_direct"},
        {"$set": {
            "session_id": claim["session_id"],
            "method": "upi_direct",
            "status": "active",
            "utr": claim["utr"],
            "activated_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }},
        upsert=True,
    )
    return {"ok": True, "session_id": claim["session_id"], "expires_at": expires.isoformat()}


@api_router.post("/admin/claims/reject")
async def admin_reject_claim(
    payload: RejectClaimRequest,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    """Merchant didn't find ₹99 in PhonePe → mark claim invalid."""
    _check_admin(x_admin_token)
    claim = await db.upi_claims.find_one({"id": payload.claim_id}, {"_id": 0})
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    await db.upi_claims.update_one(
        {"id": payload.claim_id},
        {"$set": {
            "status": "rejected",
            "rejected_at": datetime.now(timezone.utc).isoformat(),
            "reject_reason": (payload.reason or "").strip(),
        }},
    )
    return {"ok": True}


@api_router.post("/ask", response_model=HistoryItem)
async def ask_question(payload: AskRequest):
    if not EMERGENT_LLM_KEY:
        raise HTTPException(status_code=500, detail="LLM key not configured")
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    if payload.subject == "science" and not await is_premium(payload.session_id):
        raise HTTPException(status_code=402, detail="Science is a Premium feature")

    try:
        chat = build_chat(payload.session_id, payload.subject)
        msg = UserMessage(text=payload.question)
        answer = await chat.send_message(msg)
    except Exception as e:
        logger.exception("ask failed")
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

    item = HistoryItem(
        id=str(uuid.uuid4()),
        session_id=payload.session_id,
        subject=payload.subject,
        kind="ask",
        question_text=payload.question,
        image_base64="",
        answer=answer if isinstance(answer, str) else str(answer),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    await db.history.insert_one(item.model_dump())
    return item


@api_router.get("/history", response_model=HistoryResponse)
async def get_history(session_id: str, limit: int = 50):
    cursor = db.history.find(
        {"session_id": session_id}, {"_id": 0}
    ).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    return HistoryResponse(items=items)


@api_router.get("/history/{item_id}", response_model=HistoryItem)
async def get_history_item(item_id: str):
    item = await db.history.find_one({"id": item_id}, {"_id": 0})
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return item


@api_router.delete("/history/{item_id}")
async def delete_history(item_id: str, session_id: str):
    result = await db.history.delete_one({"id": item_id, "session_id": session_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"deleted": True, "id": item_id}


@api_router.delete("/history")
async def clear_history(session_id: str):
    result = await db.history.delete_many({"session_id": session_id})
    return {"deleted": result.deleted_count}


# -------------- Premium / Razorpay --------------
class SubscriptionCreateRequest(BaseModel):
    session_id: str


class VerifyRequest(BaseModel):
    session_id: str
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str


@api_router.get("/premium/status")
async def premium_status(session_id: str):
    premium = await is_premium(session_id)
    used = await db.history.count_documents(
        {"session_id": session_id, "kind": "scan"}
    )
    sub = await db.subscriptions.find_one(
        {"session_id": session_id, "status": "active"}, {"_id": 0}
    )
    return {
        "is_premium": premium,
        "scans_used": used,
        "scans_limit": FREE_SCAN_LIMIT,
        "price_paise": PREMIUM_PRICE_PAISE,
        "expires_at": sub.get("expires_at") if sub else None,
    }


@api_router.post("/subscription/create")
async def create_subscription(payload: SubscriptionCreateRequest):
    """Creates a Razorpay Order for ₹99 (one-time charge → 30 days premium).
    Note: We use Orders API (not Subscriptions) for broader test-account support."""
    if not rzp_client:
        raise HTTPException(status_code=500, detail="Razorpay not configured")
    try:
        receipt = f"buddy_{uuid.uuid4().hex[:16]}"
        order = rzp_client.order.create({
            "amount": PREMIUM_PRICE_PAISE,
            "currency": "INR",
            "receipt": receipt,
            "payment_capture": 1,
            "notes": {"session_id": payload.session_id, "plan": "buddy_premium_monthly"},
        })
    except Exception as e:
        logger.exception("order create failed")
        raise HTTPException(status_code=500, detail=f"Razorpay error: {str(e)}")

    await db.subscriptions.update_one(
        {"order_id": order["id"]},
        {"$set": {
            "order_id": order["id"],
            "session_id": payload.session_id,
            "status": "created",
            "amount_paise": PREMIUM_PRICE_PAISE,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    return {
        "order_id": order["id"],
        "key_id": RAZORPAY_KEY_ID,
        "amount_paise": PREMIUM_PRICE_PAISE,
        "currency": "INR",
    }


@api_router.post("/subscription/verify")
async def verify_subscription(payload: VerifyRequest):
    if not RAZORPAY_KEY_SECRET:
        raise HTTPException(status_code=500, detail="Razorpay not configured")
    # For Orders: HMAC of order_id|payment_id with key_secret
    body = f"{payload.razorpay_order_id}|{payload.razorpay_payment_id}"
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        body.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, payload.razorpay_signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Confirm order exists in our DB for this session (security check)
    existing = await db.subscriptions.find_one(
        {"order_id": payload.razorpay_order_id, "session_id": payload.session_id},
        {"_id": 0},
    )
    if not existing:
        raise HTTPException(status_code=400, detail="Order not found for this session")

    now = datetime.now(timezone.utc)
    expires = now.replace(microsecond=0) + timedelta(days=30)
    await db.subscriptions.update_one(
        {"order_id": payload.razorpay_order_id},
        {"$set": {
            "status": "active",
            "payment_id": payload.razorpay_payment_id,
            "activated_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }},
    )
    return {"ok": True, "is_premium": True, "expires_at": expires.isoformat()}


class UpiClaimRequest(BaseModel):
    session_id: str
    utr: str  # 12-digit UPI Reference Number from the payer's app
    payer_note: Optional[str] = ""


@api_router.get("/upi/config")
async def upi_config():
    """Returns merchant UPI details for QR generation on the frontend."""
    if not MERCHANT_UPI_ID:
        raise HTTPException(status_code=503, detail="UPI not configured")
    upi_link = (
        f"upi://pay?pa={MERCHANT_UPI_ID}"
        f"&pn={MERCHANT_UPI_NAME.replace(' ', '%20')}"
        f"&am={PREMIUM_PRICE_PAISE / 100:.2f}"
        f"&cu=INR&tn=Buddy%20Premium"
    )
    return {
        "upi_id": MERCHANT_UPI_ID,
        "name": MERCHANT_UPI_NAME,
        "amount": PREMIUM_PRICE_PAISE / 100,
        "amount_paise": PREMIUM_PRICE_PAISE,
        "upi_link": upi_link,
    }


@api_router.post("/upi/claim")
async def upi_claim(payload: UpiClaimRequest):
    """User reports they paid via direct UPI.
    Claim is queued as 'pending_verification' — merchant must approve it
    in /admin after checking PhonePe for actual ₹99 incoming.
    Premium is NOT activated automatically."""
    utr = (payload.utr or "").strip()
    if len(utr) < 6 or len(utr) > 30:
        raise HTTPException(status_code=400, detail="UTR looks invalid (6-30 chars)")
    if not utr.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="UTR must be letters/numbers")

    dup = await db.upi_claims.find_one({"utr": utr}, {"_id": 0})
    if dup and dup.get("session_id") != payload.session_id:
        raise HTTPException(status_code=409, detail="This UTR was already used")

    now = datetime.now(timezone.utc)
    claim_id = dup.get("id") if dup else str(uuid.uuid4())

    await db.upi_claims.update_one(
        {"utr": utr},
        {"$set": {
            "id": claim_id,
            "utr": utr,
            "session_id": payload.session_id,
            "amount_paise": PREMIUM_PRICE_PAISE,
            "payer_note": payload.payer_note or "",
            "claimed_at": now.isoformat(),
            "merchant_upi": MERCHANT_UPI_ID,
            "status": "pending_verification",
        }},
        upsert=True,
    )
    return {
        "ok": True,
        "is_premium": False,
        "status": "pending_verification",
        "note": "Thanks! The merchant will verify your payment against their bank and activate Premium within a few hours.",
    }


@api_router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    if RAZORPAY_WEBHOOK_SECRET:
        expected = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")
    try:
        import json
        data = json.loads(body.decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Bad payload")

    event = data.get("event", "")
    payment_entity = (
        (data.get("payload", {}).get("payment", {}) or {}).get("entity", {})
    )
    order_id = payment_entity.get("order_id")
    if order_id and event in ("payment.captured", "order.paid"):
        sub = await db.subscriptions.find_one({"order_id": order_id}, {"_id": 0})
        if sub:
            now = datetime.now(timezone.utc)
            expires = now + timedelta(days=30)
            await db.subscriptions.update_one(
                {"order_id": order_id},
                {"$set": {
                    "status": "active",
                    "last_event": event,
                    "last_event_at": now.isoformat(),
                    "expires_at": expires.isoformat(),
                }},
            )
    return {"ok": True}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
