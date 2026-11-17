import sqlite3

from fastapi import HTTPException

from .models import ItemCreate

DB_PATH = ":memory:"
MAX_ITEMS_PER_OWNER = 20
TAX_RATE = 0.18

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute(
    "CREATE TABLE IF NOT EXISTS items "
    "(id INTEGER PRIMARY KEY, owner TEXT, name TEXT, quantity INTEGER, price REAL)"
)


def get_db():
    """Hands the shared SQLite connection to whoever needs it."""
    return _conn


def add_tax(price: float) -> float:
    """Adds sales tax to a price and rounds to 2 decimals."""
    return round(price * (1 + TAX_RATE), 2)


class ItemRepository:
    def __init__(self, db):
        self.db = db

    def count_for(self, owner: str) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM items WHERE owner = ?", (owner,)).fetchone()
        return row[0]

    def insert(self, owner: str, item: ItemCreate, price: float) -> int:
        cur = self.db.execute(
            "INSERT INTO items (owner, name, quantity, price) VALUES (?, ?, ?, ?)",
            (owner, item.name, item.quantity, price),
        )
        self.db.commit()
        return cur.lastrowid

    def fetch(self, item_id: int):
        return self.db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def fetch_page(self, owner: str, limit: int, offset: int):
        return self.db.execute(
            "SELECT * FROM items WHERE owner = ? LIMIT ? OFFSET ?", (owner, limit, offset)
        ).fetchall()


def create_item(db, owner: str, item: ItemCreate) -> dict:
    """Checks the owner's quota, applies tax, then stores the item."""
    repo = ItemRepository(db)
    if repo.count_for(owner) >= MAX_ITEMS_PER_OWNER:
        raise HTTPException(status_code=409, detail="Item quota reached")
    price = add_tax(item.price)
    new_id = repo.insert(owner, item, price)
    return {"id": new_id, "name": item.name, "quantity": item.quantity, "price": price}


def get_item(db, item_id: int) -> dict:
    repo = ItemRepository(db)
    row = repo.fetch(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"id": row[0], "name": row[2], "quantity": row[3], "price": row[4]}


def list_items(db, owner: str, limit: int, offset: int) -> list:
    repo = ItemRepository(db)
    rows = repo.fetch_page(owner, limit, offset)
    return [{"id": r[0], "name": r[2], "quantity": r[3], "price": r[4]} for r in rows]
