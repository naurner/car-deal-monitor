from dataclasses import dataclass, field


@dataclass
class RawListing:
    source_site: str
    external_id: str
    url: str
    title: str
    price_usd: float | None = None
    year: int | None = None
    brand: str | None = None
    model: str | None = None
    generation_code: str | None = None  # напр. "XV70"; None = не определено
    description: str | None = None
    photos: list[str] = field(default_factory=list)
    country: str | None = None
    extra: dict = field(default_factory=dict)  # пробег, кпп, город и т.п.
