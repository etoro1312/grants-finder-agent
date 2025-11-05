# app.py
# =========
# Single-file Grants Finder Agent (FastAPI)
# - Free tier: search Grants.gov (keyword/state/eligibility), clean results
# - Pro tier: CSV export + saved searches + alerts-ready hook (stubs)
# - ACP-style checkout endpoints: create/update/complete + in-memory "subscription"
#
# Quickstart:
#   pip install fastapi uvicorn httpx pydantic python-multipart
#   uvicorn app:app --reload
#
# Test searches:
#   curl "http://localhost:8000/api/grants/search?keyword=STEM&limit=5"
# Upgrade (mock checkout):
#   curl -X POST http://localhost:8000/commerce/checkout_sessions -H "Content-Type: application/json" -d '{"items":[{"id":"grants_pro_monthly","quantity":1}],"user_id":"demo-user"}'
#   curl -X POST http://localhost:8000/commerce/checkout_sessions/cs_123/complete -H "Content-Type: application/json" -d '{"user_id":"demo-user"}'
# Then use the agent endpoint (adds CSV in Pro):
#   curl -X POST http://localhost:8000/agent/grants -H "Content-Type: application/json" -d '{"user_id":"demo-user","params":{"keyword":"housing","limit":5}}"
#
# Notes:
# - Grants.gov "search2" is publicly accessible (no key), perfect for MVP.
# - Replace the in-memory store with a database for production.
# - ACP endpoints are simplified to demonstrate the flow end-to-end.

from __future__ import annotations
from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import httpx, csv, io, datetime as dt

app = FastAPI(title="Grants Finder Agent (Single File)", version="0.1.0")

# -----------------------------
# In-memory "DB"
# -----------------------------
USERS: Dict[str, Dict[str, Any]] = {}  # user_id -> {"subscription": "free"|"pro", ...}
SAVED_SEARCHES: Dict[str, List[Dict[str, Any]]] = {}  # user_id -> list of param dicts

# -----------------------------
# Config / Constants
# -----------------------------
GRANTS_SEARCH2 = "https://www.grants.gov/api/common/search2"  # public search endpoint
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
    eligibility: Optional[str] = None   # e.g., "state_governments|nonprofits"
    sort_by: str = "closeDate|asc"      # grants.gov style e.g. "closeDate|asc"
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
    user_id: Optional[str] = None  # who is purchasing (to flip entitlement on complete)

# -----------------------------
# Helpers
# -----------------------------
def normalize_hit(hit: Dict[str, Any]) -> Grant:
    return Grant(
        opportunityNumber = hit.get("opportunityNumber"),
        title = hit.get("title") or hit.get("opportunityTitle") or "(untitled)",
        agency = hit.get("agency") or hit.get("agencyCode"),
        cfdaNumbers = hit.get("cfdaList") or hit.get("assistanceListings"),
        closeDate = hit.get("closeDate"),
        openDate = hit.get("postDate"),
        eligibility = hit.get("eligibilityCategories") or hit.get("applicantTypes"),
        link = hit.get("opportunityLink") or hit.get("opportunityIdLink") or "https://www.grants.gov",
    )

def ensure_user(user_id: str) -> Dict[str, Any]:
    if user_id not in USERS:
        USERS[user_id] = {"subscription": "free", "created_at": dt.datetime.utcnow().isoformat()}
        SAVED_SEARCHES[user_id] = []
    return USERS[user_id]

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

# -----------------------------
# Grants.gov SEARCH
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
    params = {
        "keyword": keyword,
        "startRecordNum": offset,
        "rows": limit,
        "sortBy": sort_by,
    }
    if state: params["state"] = state
    if eligibility: params["eligibility"] = eligibility

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(GRANTS_SEARCH2, params=params)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502, detail=f"Grants.gov error: {e}") from e
        data = r.json()

    raw = data.get("oppHits", []) or []
    results = [normalize_hit(h).model_dump() for h in raw]
    return {
        "source": "grants.gov/search2",
        "total": data.get("totalRecords", len(results)),
        "results": results,
    }

# -----------------------------
# Agent endpoint (Free vs Pro)
# -----------------------------
@app.post("/agent/grants")
async def agent_grants(req: AgentRequest):
    user = ensure_user(req.user_id)
    # call internal search endpoint (so you can swap to caching later)
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get("http://localhost:8000/api/grants/search", params=req.params.model_dump())
        r.raise_for_status()
        payload = r.json()

    results = [Grant(**g) for g in payload.get("results", [])]
    response: Dict[str, Any] = {
        "tier": user["subscription"],
        "summary": f"Found {payload.get('total', len(results))} opportunities; showing {len(results)}.",
        "results": [g.model_dump() for g in results],
        "source": payload.get("source", "grants.gov"),
        "sort_by": req.params.sort_by,
    }

    # Offer CSV + Saved Searches for Pro
    if user["subscription"] == "pro":
        # CSV
        csv_text = csv_from_results(results)
        response["csv_filename"] = f"grants_{dt.date.today()}.csv"
        response["csv_content"] = csv_text  # In production, return as file/URL

        # Save search (optional)
        if req.save_search:
            SAVED_SEARCHES[req.user_id].append(req.params.model_dump())
            response["saved_searches"] = SAVED_SEARCHES[req.user_id]
    else:
        # Upsell hint (for ACP clients to render a "Buy" button)
        response["upsell"] = {
            "message": "Unlock CSV export, saved searches & alerts with Grants Pro.",
            "product_id": "grants_pro_monthly",
            "price_cents": INVENTORY["grants_pro_monthly"],
        }

    return response

# -----------------------------
# Saved-searches (view)
# -----------------------------
@app.get("/user/{user_id}/saved-searches")
def list_saved(user_id: str):
    ensure_user(user_id)
    return {"user_id": user_id, "saved_searches": SAVED_SEARCHES[user_id]}

# -----------------------------
# Simple user view
# -----------------------------
@app.get("/user/{user_id}")
def user_view(user_id: str):
    user = ensure_user(user_id)
    return {"user_id": user_id, **user}

# -----------------------------
# ACP-style checkout (mock)
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
    # store temp session if you want; here we return a static id for demo
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
    # recompute as if cart changed
    return checkout_create(req)

class CompleteReq(BaseModel):
    user_id: Optional[str] = None

@app.post("/commerce/checkout_sessions/{checkout_session_id}/complete")
def checkout_complete(checkout_session_id: str, req: CompleteReq):
    # Here you'd capture on your PSP; if success:
    if req.user_id:
        ensure_user(req.user_id)
        USERS[req.user_id]["subscription"] = "pro"
    return {
        "id": checkout_session_id,
        "status": "completed",
        "links": [{"type": "receipt", "url": f"https://yourco.example/receipts/{checkout_session_id}"}],
    }

# -----------------------------
# Health
# -----------------------------
@app.get("/healthz")
def health():
    return {"ok": True, "ts": dt.datetime.utcnow().isoformat()}
