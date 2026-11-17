"""Small FastAPI app used to demonstrate flowmap.

Run the diagram with:   flowmap serve examples.sample_app.main:app
"""
import time
from collections import defaultdict

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import services
from .models import ItemCreate, ItemOut

app = FastAPI(title="Sample shop API")

API_KEYS = {"secret-key-1": "ashok", "secret-key-2": "guest"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Blocks a client that sends too many requests inside the time window."""

    def __init__(self, app, max_requests: int = 5, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.hits = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        client = request.client.host if request.client else "unknown"
        now = time.time()
        recent = [t for t in self.hits[client] if now - t < self.window_seconds]
        if len(recent) >= self.max_requests:
            return JSONResponse({"detail": "Too many requests"}, status_code=429)
        recent.append(now)
        self.hits[client] = recent
        return await call_next(request)


app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])
app.add_middleware(RateLimitMiddleware, max_requests=5, window_seconds=60)


@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    """Measures how long the request took and adds it as a response header."""
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time"] = str(round(time.perf_counter() - start, 4))
    return response


def current_user(x_api_key: str = Header(...)) -> str:
    """Turns the X-API-Key header into a user name, or rejects the call."""
    user = API_KEYS.get(x_api_key)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


@app.post("/items", response_model=ItemOut, status_code=201, tags=["items"])
def create_item(item: ItemCreate, user: str = Depends(current_user), db=Depends(services.get_db)):
    """Create an item for the calling user."""
    return services.create_item(db, user, item)


@app.get("/items/{item_id}", response_model=ItemOut, tags=["items"])
def read_item(item_id: int, user: str = Depends(current_user), db=Depends(services.get_db)):
    return services.get_item(db, item_id)


@app.get("/items", response_model=list[ItemOut], tags=["items"])
def list_items(
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    user: str = Depends(current_user),
    db=Depends(services.get_db),
):
    """List the caller's items, one page at a time."""
    return services.list_items(db, user, limit, offset)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}
