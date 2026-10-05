"""Резервная копия базы для отправки в Telegram (команда /backup).

Снимок — через backup API SQLite: согласованная копия, даже пока бот пишет в
базу. Сжимаем в zip; не влезает в PART_SIZE — режем на куски .001, .002…,
которые склеиваются обратно обычным cat / copy /b.
"""

import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from config import DB_PATH

PART_SIZE = 25 * 1024 * 1024  # Telegram у ботов принимает файлы до 50 МБ


def fmt_size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} МБ"


def make_parts(workdir: Path, part_size: int | None = None) -> tuple[list[Path], int]:
    """(файлы для отправки, размер несжатой базы)."""
    part_size = part_size or PART_SIZE
    snapshot = workdir / "cardeal.db"
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(snapshot)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    archive = workdir / f"cardeal_{datetime.now():%Y-%m-%d_%H-%M}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(snapshot, "cardeal.db")
    raw_size = snapshot.stat().st_size
    snapshot.unlink()
    if archive.stat().st_size <= part_size:
        return [archive], raw_size
    parts = []
    with archive.open("rb") as f:
        i = 1
        while chunk := f.read(part_size):
            part = workdir / f"{archive.name}.{i:03d}"
            part.write_bytes(chunk)
            parts.append(part)
            i += 1
    archive.unlink()
    return parts, raw_size


def instructions(parts: list[Path]) -> str:
    if len(parts) == 1:
        return f"✅ База отправлена: {parts[0].name}\nВнутри — cardeal.db."
    zip_name = parts[0].name.rsplit(".", 1)[0]
    joined = "+".join(p.name for p in parts)
    return (
        f"✅ База отправлена частями ({len(parts)}). Сохраните все части в одну папку и склейте:\n\n"
        f"Mac / Linux:\ncat {zip_name}.* > {zip_name}\n\n"
        f"Windows:\ncopy /b {joined} {zip_name}\n\n"
        f"Получится {zip_name}, внутри — cardeal.db."
    )
