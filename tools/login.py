"""Одноразовый вход в аккаунт площадки.

Запуск:  .venv\\Scripts\\python tools\\login.py mashina|lalafo|unaa

Откроется окно браузера — войдите в аккаунт как обычно (телефон, SMS и т.д.).
После входа закройте окно. Сессия сохранится и бот будет использовать её.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from posting.sessions import SITES, SiteBrowser  # noqa: E402


async def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in SITES:
        print(f"Использование: python tools/login.py [{'|'.join(SITES)}]")
        sys.exit(1)
    site = sys.argv[1]
    print(f"Открываю {SITES[site]} — войдите в аккаунт и закройте окно браузера.")
    async with SiteBrowser(site, headless=False) as ctx:
        page = await ctx.new_page()
        await page.goto(SITES[site])
        try:
            while ctx.pages:  # ждём, пока пользователь закроет окно
                await asyncio.sleep(1)
        except Exception:
            pass
    print("Сессия сохранена.")


if __name__ == "__main__":
    asyncio.run(main())
