from sqlalchemy import text

from app.db.session import engine


def init_db() -> None:
    # Lightweight startup check to fail fast if DB is unavailable.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
