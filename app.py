# app.py
# =========
# Grants Finder Agent (FastAPI) — Render/Railway/Fly-ready
# - Fix: removed self-HTTP call to http://localhost:8000 (caused 500 in hosted envs)
# - Uses a shared helper to query Grants.gov "search2" directly
# - Free tier: live search
# - Pro tier: CSV export + saved searches
# - Mock ACP checkout endpoints to flip entitlement (Pro)
#
# Quickstart (local):
#   pip install fastapi uvicorn httpx pydantic python-multipart
#   uvicorn app:app --reload
#
# Deploy (Render/Railway):
#   Build: pip install -r requirements.txt
#   Start: uvicorn app:app --host 0.0.0.0 --port $PORT

from __future__ import annotations
from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import httpx, csv, io, datetime as dt

app = FastAPI(title="Grants Finder Agent", version="0.2.0")

# -----------------------------
# In-memory "DB"
# -----------------------------
USERS: Dict[str, Dict[str, Any]] = {}          # user_id -> {"subscription": "free"|"pro", ...}
SAVED_SEARCHES: Dict[str, List[Dict[str, Any]]] = {}  # user_id -> list of param dicts

# -----------------------------
# Config / Constants
# -----------------------------
GRANTS_SEARCH2 = "https://www.grants.gov/api/common/search2"  # public endpoint (no key)
INVENTORY = {
    "grants_pro_monthly": 1500,   # $15.00
    "grants_team_monthly": 4900,  # $49.00
}

# -----------------------------
# Models
# -----------------------------
class Grant(BaseModel):
    opportunityNumber: Optional[str] = None
    title: str
    agency: Optional[str] = None
    cfdaNumbers: Optional[List[str]] = None
    closeDate: Optional[str] = None
    openDate: Optional[str] = None
    eligibility: Optional[List[str]] = None
    link: str

class SearchParams(BaseModel):
    keyword: str = ""
    state: Optional[str] = None
    eligibility: Optional[str] = None   # pipe-separated per Grants.gov
    sort_by: str = "closeDate|asc"
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)

class AgentRequest(BaseModel):
    user_id: str
    params: SearchParams
    save_search: bool = False

class CheckoutItem(BaseModel):
    id: str
    quantity: int = 1

class CheckoutCreateReq(BaseModel):
    items: List[CheckoutItem]
    user_id: Optional[str] = None

class CompleteReq(BaseModel):
    user_id: Optional[str] = None

# -----------------------------
# Helpers
# -----------------------------
def ensure_user(user_id: str) -> Dict[str, Any]:
    if user_id not in USERS:
        USERS[user_id] = {"subscription": "free", "created_at": dt.datetime.utcnow().isoformat()}
        SAVED_SEARCHES[user_id] = []
    return USERS[user_id]

def normalize_hit(hit: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Grants.gov record to our Grant schema (as dict)."""
    return {
        "opportunityNumber": hit.get("opportunityNumber"),
        "title": hit.get("title") or hit.get("opportunityTitle") or "(untitled)",
        "agency": hit.get("agency") or hit.get("agencyCode"),
        "cfdaNumbers": hit.get("cfdaList") or hit.get("assistanceListings"),
        "closeDate": hit.get("closeDate"),
        "openDate": hit.get("postDate"),
        "eligibility": hit.get("eligibilityCategories") or hit.get("applicantTypes"),
        "link": hit.get("opportunityLink") or hit.get("opportunityIdLink") or "https://www.grants.gov",
    }

def csv_from_results(results: List[Grant]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Title","Agency","CFDA(s)","Close Date","Eligibility","Link"])
    for g in results:
        w.writerow([
            g.title,
            g.agency or "",
            ";".join(g.cfdaNumbers or []),
            g.closeDate or "",
            ";".join(g.eligibility or []),
            g.link
        ])
    return buf.getvalue()

async def fetch_grants_from_grantsdotgov(params: Dict[str, Any]) -> Dict[str, Any]:
    """Shared helper to query Grants.gov directly (no self-HTTP)."""
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(GRANTS_SEARCH2, params=params)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Provide readable upstream error in logs/response
            snippet = e.response.text[:300] if e.response is not None else ""
            raise HTTPException(status_code=502, detail=f"Grants.gov error: {e.response.status_code if e.response else 'n/a'} {snippet}")
        return r.json()

# -----------------------------
# Health
# -----------------------------
@app.get("/healthz")
def health():
    return {"ok": True, "ts": dt.datetime.utcnow().isoformat()}

# -----------------------------
# Public API: /api/grants/search
# -----------------------------
@app.get("/api/grants/search")
async def search_grants(
    keyword: str = Query("", description="e.g., housing OR STEM"),
    state: Optional[str] = None,
    eligibility: Optional[str] = None,  # pipe-separated values
    sort_by: str = "closeDate|asc",
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    params = {"keyword": keyword, "startRecordNum": offset, "rows": limit, "sortBy": sort_by}
    if state: params["state"] = state
    if eligibility: params["eligibility"] = eligibility

    data = await fetch_grants_from_grantsdotgov(params)
    raw = data.get("oppHits", []) or []
    results = [normalize_hit(h) for h in raw]
    return {
        "source": "grants.gov/search2",
        "total": data.get("totalRecords", len(results)),
        "results": results,
    }

# -----------------------------
# Agent endpoint: /agent/grants (Free vs Pro)
# -----------------------------
@app.post("/agent/grants")
async def agent_grants(req: AgentRequest):
    user = ensure_user(req.user_id)

    p = req.params.model_dump()
    fetch_params = {
        "keyword": p["keyword"],
        "startRecordNum": p["offset"],
        "rows": p["limit"],
        "sortBy": p["sort_by"],
    }
    if p.get("state"): fetch_params["state"] = p["state"]
    if p.get("eligibility"): fetch_params["eligibility"] = p["eligibility"]

    data = await fetch_grants_from_grantsdotgov(fetch_params)
    raw = data.get("oppHits", []) or []
    # Validate to Grant then back to dict for clean CSV/export handling
    grants = [Grant(**normalize_hit(h)) for h in raw]

    response: Dict[str, Any] = {
        "tier": user["subscription"],
        "summary": f"Found {data.get('totalRecords', len(grants))} opportunities; showing {len(grants)}.",
        "results": [g.model_dump() for g in grants],
        "source": "grants.gov/search2",
        "sort_by": p["sort_by"],
    }

    if user["subscription"] == "pro":
        csv_text = csv_from_results(grants)
        response["csv_filename"] = f"grants_{dt.date.today()}.csv"
        response["csv_content"] = csv_text  # (In prod: upload & return a signed URL)
        if req.save_search:
            SAVED_SEARCHES[req.user_id].append(req.params.model_dump())
            response["saved_searches"] = SAVED_SEARCHES[req.user_id]
    else:
        response["upsell"] = {
            "message": "Unlock CSV export, saved searches & alerts with Grants Pro.",
            "product_id": "grants_pro_monthly",
            "price_cents": INVENTORY["grants_pro_monthly"],
        }

    return response

# -----------------------------
# Saved searches & user info
# -----------------------------
@app.get("/user/{user_id}")
def user_view(user_id: str):
    user = ensure_user(user_id)
    return {"user_id": user_id, **user}

@app.get("/user/{user_id}/saved-searches")
def list_saved(user_id: str):
    ensure_user(user_id)
    return {"user_id": user_id, "saved_searches": SAVED_SEARCHES[user_id]}

# -----------------------------
# ACP-style checkout (mock) to flip entitlement
# -----------------------------
def _price_lines(items: List[CheckoutItem]):
    line_items = []
    for it in items:
        if it.id not in INVENTORY:
            raise HTTPException(status_code=400, detail=f"Unknown SKU: {it.id}")
        base = INVENTORY[it.id]
        subtotal = base * it.quantity
        tax = 0
        total = subtotal + tax
        line_items.append({
            "id": f"li_{it.id}",
            "item": it.model_dump(),
            "base_amount": base,
            "subtotal": subtotal,
            "tax": tax,
            "total": total
        })
    return line_items

@app.post("/commerce/checkout_sessions")
def checkout_create(req: CheckoutCreateReq):
    line_items = _price_lines(req.items)
    totals = [
        {"type": "subtotal", "amount": sum(li["subtotal"] for li in line_items)},
        {"type": "tax", "amount": sum(li["tax"] for li in line_items)},
        {"type": "total", "amount": sum(li["total"] for li in line_items)},
    ]
    return {
        "id": "cs_123",
        "status": "ready_for_payment",
        "currency": "usd",
        "payment_provider": {"provider": "stripe", "supported_payment_methods": ["card"]},
        "line_items": line_items,
        "totals": totals,
        "links": [{"type": "terms_of_use", "url": "https://yourco.example/terms"}],
        "user_id": req.user_id,
    }

@app.post("/commerce/checkout_sessions/{checkout_session_id}")
def checkout_update(checkout_session_id: str, req: CheckoutCreateReq):
    return checkout_create(req)

@app.post("/commerce/checkout_sessions/{checkout_session_id}/complete")
def checkout_complete(checkout_session_id: str, req: CompleteReq):
    if req.user_id:
        ensure_user(req.user_id)
        USERS[req.user_id]["subscription"] = "pro"
    return {
        "id": checkout_session_id,
        "status": "completed",
        "links": [{"type": "receipt", "url": f"https://yourco.example/receipts/{checkout_session_id}"}],
    }
