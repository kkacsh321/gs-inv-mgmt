from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from hashlib import sha1

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base, Product
from app.repository import InventoryRepository


def deterministic_suffix(seed: str, length: int = 8) -> str:
    """Return a stable lowercase suffix suitable for test ids/SKUs."""
    return sha1(seed.encode("utf-8")).hexdigest()[:length]


@contextmanager
def in_memory_repo() -> tuple[Session, InventoryRepository]:
    """Yield a fresh in-memory DB session and repository for integration tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db = factory()
    try:
        yield db, InventoryRepository(db)
    finally:
        db.close()
        engine.dispose()


def create_test_product(
    repo: InventoryRepository,
    *,
    sku_seed: str,
    qty: int = 10,
    category: str = "bullion",
    metal_type: str = "silver",
    weight_oz: Decimal = Decimal("1.0000"),
    acquisition_cost: Decimal = Decimal("25.00"),
    acquired_at: datetime = datetime(2026, 3, 1, 12, 0, 0),
) -> Product:
    suffix = deterministic_suffix(sku_seed)
    return repo.create_product(
        sku=f"GS-TEST-{suffix.upper()}",
        title=f"Test Product {suffix}",
        category=category,
        description="",
        metal_type=metal_type,
        weight_oz=weight_oz,
        acquisition_cost=acquisition_cost,
        current_quantity=qty,
        acquired_at=acquired_at,
    )
