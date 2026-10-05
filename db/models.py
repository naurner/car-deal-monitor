from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WatchRequest(Base):
    __tablename__ = "watch_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    source_type: Mapped[str] = mapped_column(String(16))  # manual | from_sale
    brand: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    generation_code: Mapped[str | None] = mapped_column(String(64))
    generation_name: Mapped[str | None] = mapped_column(String(128))
    # мультивыбор поколений: список {code, name, year_from, year_to}.
    # generation_code/name выше остаются для обратной совместимости (первое).
    generations: Mapped[list] = mapped_column(JSON, default=list)
    year_from: Mapped[int | None] = mapped_column(Integer)
    year_to: Mapped[int | None] = mapped_column(Integer)
    price_min: Mapped[float | None] = mapped_column(Float)
    price_max: Mapped[float | None] = mapped_column(Float)
    country: Mapped[str | None] = mapped_column(String(64))
    reg_country: Mapped[str | None] = mapped_column(String(64))  # страна учета, строгий фильтр
    label: Mapped[str | None] = mapped_column(String(128))
    extra_params: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SoldCar(Base):
    __tablename__ = "sold_cars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    brand: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    generation_code: Mapped[str | None] = mapped_column(String(64))
    generation_name: Mapped[str | None] = mapped_column(String(128))
    year: Mapped[int | None] = mapped_column(Integer)
    sale_price: Mapped[float] = mapped_column(Float)
    price_tolerance_pct: Mapped[float] = mapped_column(Float, default=10)
    # маржа-вычет из цены продажи: "1000" ($) или "10%" (процент)
    margin: Mapped[str | None] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text)
    watch_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("watch_requests.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ListingSeen(Base):
    __tablename__ = "listings_seen"
    __table_args__ = (
        UniqueConstraint("watch_request_id", "source_site", "external_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_request_id: Mapped[int] = mapped_column(ForeignKey("watch_requests.id"))
    source_site: Mapped[str] = mapped_column(String(16))  # mashina | lalafo | unaa
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(256))
    price_at_found: Mapped[float | None] = mapped_column(Float)
    last_price_seen: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(Text)
    photos: Mapped[list] = mapped_column(JSON, default=list)
    extracted_features: Mapped[dict] = mapped_column(JSON, default=dict)
    country: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GenerationEntry(Base):
    __tablename__ = "generation_dictionary"
    __table_args__ = (UniqueConstraint("brand", "model", "generation_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    generation_code: Mapped[str] = mapped_column(String(64))
    generation_name: Mapped[str] = mapped_column(String(128))
    year_from: Mapped[int | None] = mapped_column(Integer)
    year_to: Mapped[int | None] = mapped_column(Integer)  # None = по н.в.


class CatalogMake(Base):
    """Точные слаги марок с mashina.kg (регистрозависимые в URL)."""

    __tablename__ = "catalog_makes"

    value: Mapped[str] = mapped_column(String(64), primary_key=True)


class CatalogOption(Base):
    """Справочники значений фильтров mashina.kg (топливо, коробка, кузов...)."""

    __tablename__ = "catalog_options"

    attribute: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(128), primary_key=True)


class ListingNote(Base):
    __tablename__ = "listing_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings_seen.id"))
    note_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ArchivedListing(Base):
    """Глобальный архив всех спаршенных объявлений (включая удалённые)."""

    __tablename__ = "listings_archive"
    __table_args__ = (UniqueConstraint("source_site", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_site: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(256))
    brand: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(64), index=True)
    generation_code: Mapped[str | None] = mapped_column(String(64))
    year: Mapped[int | None] = mapped_column(Integer)
    price_usd: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(Text)
    photos: Mapped[list] = mapped_column(JSON, default=list)
    characteristics: Mapped[dict] = mapped_column(JSON, default=dict)
    country: Mapped[str | None] = mapped_column(String(64))  # страна учёта
    city: Mapped[str | None] = mapped_column(String(64))
    # «В наличии» / «В пути» / «На заказ» (mashina), «Не растаможен» (lalafo)
    availability: Mapped[str | None] = mapped_column(String(32))
    # что ИИ вычитал из описания (matching/ai_reader.py) и хэш текста, по
    # которому это сделано — изменилось описание, перечитываем
    ai_facts: Mapped[dict | None] = mapped_column(JSON)
    ai_hash: Mapped[str | None] = mapped_column(String(16))
    # теги из /pricecheck: {"t:корея": {"v": "yes", "src": "ai", "h": ...},
    # "a:комплектация": {"v": "Limited", "src": "user"}} — ответы ИИ и пользователя
    ai_tags: Mapped[dict | None] = mapped_column(JSON)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)  # дата публикации на площадке
    # история для торга (matching/market.py): первая увиденная цена, подпись
    # описания и самое раннее объявление той же машины, если её перевыставили
    first_price_usd: Mapped[float | None] = mapped_column(Float)
    price_kgs: Mapped[float | None] = mapped_column(Float)  # как на площадке (lalafo)
    # продавец на площадке (matching/sellers.py — чёрный список)
    seller_id: Mapped[str | None] = mapped_column(String(32), index=True)
    seller_name: Mapped[str | None] = mapped_column(String(128))
    desc_sig: Mapped[str | None] = mapped_column(String(16), index=True)
    relist_of: Mapped[int | None] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime)  # когда пропало с площадки


class BannedSeller(Base):
    """Чёрный список продавцов: их объявления не присылаем и не считаем рынком
    (например, перекупщики, выдающие машины в Корее за машины в наличии)."""

    __tablename__ = "banned_sellers"
    __table_args__ = (UniqueConstraint("site", "seller_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site: Mapped[str] = mapped_column(String(16))
    seller_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SellerAsk(Base):
    """Вопрос продавцу по тегам /pricecheck (matching/seller_questions.py).
    Одно объявление — не больше одного вопроса за всё время."""

    __tablename__ = "seller_asks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    archive_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    site: Mapped[str] = mapped_column(String(16))
    ad_url: Mapped[str] = mapped_column(String(512))
    keys: Mapped[list] = mapped_column(JSON, default=list)      # о чём спросили
    wanted: Mapped[dict] = mapped_column(JSON, default=dict)    # что искал пользователь
    label: Mapped[str | None] = mapped_column(String(256))      # для уведомления
    chat_id: Mapped[int] = mapped_column(Integer)               # кому сообщить ответ
    question: Mapped[str] = mapped_column(Text)
    # queued → sent → answered | expired; failed — не удалось отправить
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(String(256))
    answer: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)


class KeywordWatch(Base):
    """Слежение за ключевыми словами в свежих объявлениях."""

    __tablename__ = "keyword_watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    keywords: Mapped[list] = mapped_column(JSON, default=list)  # список = ИЛИ
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class KeywordSeen(Base):
    """Уже отправленные совпадения по ключевым словам (дедуп уведомлений)."""

    __tablename__ = "keyword_seen"
    __table_args__ = (UniqueConstraint("watch_id", "archive_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("keyword_watches.id"))
    archive_id: Mapped[int] = mapped_column(ForeignKey("listings_archive.id"))


class AllowedUser(Base):
    """Пользователи, допущенные к боту (помимо владельца)."""

    __tablename__ = "allowed_users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PostedAd(Base):
    """Наше объявление, размещённое ботом (или взятое под управление)."""

    __tablename__ = "posted_ads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    # кросс-платформенные данные авто
    car_data: Mapped[dict] = mapped_column(JSON, default=dict)
    photos_dir: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    price_usd: Mapped[float | None] = mapped_column(Float)
    # состояние по площадкам: {site: {"status": "posted|error|pending",
    #   "external_id": ..., "url": ..., "error": ..., "last_bump": iso,
    #   "dot_state": bool, "posted_at": iso}}
    sites: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    bump_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    repost_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # lalafo, пока выкл
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ModelTop(Base):
    """Топ-3 самых дешёвых активных объявления по марка+модель+поколение
    (весь рынок, обе площадки) — обновляется после полного сканирования.
    """

    __tablename__ = "model_tops"
    __table_args__ = (UniqueConstraint("brand", "model", "generation_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(64), index=True)
    generation_code: Mapped[str] = mapped_column(String(64))  # "" = без поколения
    generation_name: Mapped[str | None] = mapped_column(String(128))
    # [{"archive_id":, "price":, "photo_path":, "url":, "title":, "source_site":}], до 3, дешёвые впереди
    top_json: Mapped[list] = mapped_column(JSON, default=list)
    avg_price: Mapped[float | None] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    last_anomaly_archive_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))
