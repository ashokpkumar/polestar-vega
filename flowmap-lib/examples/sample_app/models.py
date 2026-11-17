from typing import Optional

from pydantic import BaseModel, Field


class ItemCreate(BaseModel):
    """Payload a client sends to create an item."""

    name: str = Field(min_length=2, max_length=40)
    quantity: int = Field(default=1, ge=0, le=5)
    price: float = Field(gt=0)
    note: Optional[str] = None


class ItemOut(BaseModel):
    """What the API returns for an item (internal cost is never exposed)."""

    id: int
    name: str
    quantity: int
    price: float
