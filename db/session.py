import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from config import DB_PATH
from db.models import Base

log = logging.getLogger(__name__)

BUSY_TIMEOUT_SEC = 60

# Задачи бота пишут в базу одновременно (полный скан, автопроверка, ИИ,
# поднятия). SQLite разрешает одного писателя: без ожидания вторая задача
# сразу падала с «database is locked» — из-за этого переставали приходить
# уведомления по поискам. timeout = ждать освобождения, а не падать.
engine = create_engine(
    f"sqlite:///{DB_PATH}", connect_args={"timeout": BUSY_TIMEOUT_SEC}
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    # WAL: чтение не блокирует запись и наоборот (на сетевых ФС может быть
    # недоступен — тогда SQLite просто останется в прежнем режиме)
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_SEC * 1000}")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        log.info("SQLite: journal_mode=%s, ожидание блокировки %d с",
                 conn.execute(text("PRAGMA journal_mode")).scalar_one(), BUSY_TIMEOUT_SEC)
    _migrate()


def _migrate() -> None:
    """Мини-миграции: create_all не добавляет колонки в существующие таблицы."""
    import os

    from sqlalchemy import text

    owner = os.getenv("OWNER_TELEGRAM_ID", "0")
    additions = {
        "watch_requests": [
            ("reg_country", "VARCHAR(64)", None),
            ("user_id", "INTEGER", owner),
            ("generations", "JSON", "'[]'"),
        ],
        "sold_cars": [
            ("user_id", "INTEGER", owner),
            ("margin", "VARCHAR(16)", None),
        ],
        "listings_archive": [
            ("availability", "VARCHAR(32)", None),
            ("ai_facts", "JSON", None),
            ("ai_hash", "VARCHAR(16)", None),
            ("ai_tags", "JSON", None),
            ("first_price_usd", "FLOAT", "price_usd"),
            ("desc_sig", "VARCHAR(16)", None),
            ("relist_of", "INTEGER", None),
            ("price_kgs", "FLOAT", None),
            ("seller_id", "VARCHAR(32)", None),
            ("seller_name", "VARCHAR(128)", None),
        ],
    }
    with engine.connect() as conn:
        for table, cols_to_add in additions.items():
            cols = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            for name, type_, default in cols_to_add:
                if name not in cols:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {type_}")
                    )
                    if default is not None:
                        conn.execute(
                            text(f"UPDATE {table} SET {name} = {default}")
                        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_listings_archive_desc_sig "
                          "ON listings_archive (desc_sig)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_listings_archive_seller_id "
                          "ON listings_archive (seller_id)"))
        conn.commit()


def get_session() -> Session:
    return SessionLocal()
