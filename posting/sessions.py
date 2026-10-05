"""Браузерные сессии площадок (Playwright, persistent profiles).

Логин выполняется один раз вручную: `python tools/login.py <site>` открывает
видимое окно, пользователь входит в аккаунт, профиль сохраняется в
data/browser_profiles/<site>/ и дальше используется ботом headless.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright

from config import DATA_DIR, USER_AGENT

log = logging.getLogger(__name__)

# channel="chrome" (системный Chrome) нужен только на этой Windows-машине —
# у неё собственная сборка Chromium из Playwright не запускается (side-by-side
# error). Внутри Docker-контейнера (Linux) ставится обычный Chromium из
# `playwright install`, системного Chrome там нет — channel не передаём.
# Переопределяется переменной окружения PLAYWRIGHT_CHANNEL при необходимости.
_CHANNEL = os.getenv("PLAYWRIGHT_CHANNEL", "chrome" if sys.platform == "win32" else "") or None

PROFILES_DIR = DATA_DIR / "browser_profiles"
PROFILES_DIR.mkdir(exist_ok=True)

# Chrome persistent-профиль нельзя открывать в двух процессах одновременно —
# это роняет сессию (пользователя «разлогинивает»). Один доступ к профилю за раз.
_profile_locks: dict[str, asyncio.Lock] = {}


def _lock_for(site: str) -> asyncio.Lock:
    if site not in _profile_locks:
        _profile_locks[site] = asyncio.Lock()
    return _profile_locks[site]

# unaa.kg исключён: веб-сайт «в разработке», всё только через моб. приложение —
# ни парсинг, ни постинг через браузер невозможны.
SITES = {
    "mashina": "https://mashina.kg",
    "lalafo": "https://lalafo.kg",
}

# проверка залогиненности: url кабинета + текст-маркер, который виден
# ТОЛЬКО авторизованному пользователю. Раньше проверяли "нет слова login
# в финальном URL" — сломалось, когда mashina.kg сменил редирект
# неавторизованных на /auth (без слова login) — false positive "залогинен".
# Content-based проверка надёжнее к таким изменениям сайта.
LOGIN_CHECK = {
    "mashina": ("https://mashina.kg/profile/my-ads", "мои объявления"),
    # "мои объявления"/"выход" у lalafo — не годятся: первое есть в статичном
    # меню и без входа, второго нет в открытом тексте страницы вообще
    # (кнопка выхода спрятана в выпадающем меню). Бот однопользовательский,
    # привязан к аккаунту владельца — имя профиля надёжный маркер входа.
    "lalafo": ("https://lalafo.kg/account", "алексей"),
}


def profile_dir(site: str) -> Path:
    return PROFILES_DIR / site


def has_profile(site: str) -> bool:
    return (profile_dir(site) / "Default").exists() or any(
        profile_dir(site).glob("*")
    ) if profile_dir(site).exists() else False


class SiteBrowser:
    """Контекст-менеджер persistent-браузера площадки."""

    def __init__(self, site: str, headless: bool = True,
                 window: tuple[int, int] | None = None):
        """window — окно на весь виртуальный экран (вход из Telegram): страница
        подстраивается под размер окна, а не под фиксированный viewport."""
        assert site in SITES, f"unknown site {site}"
        self.site = site
        self.headless = headless
        self.window = window
        self._pw = None
        self._lock = _lock_for(site)
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserContext:
        # эксклюзивный доступ к профилю — чтобы не разлогинивало
        await self._lock.acquire()
        try:
            self._pw = await async_playwright().start()
            if self.window:
                w, h = self.window
                screen = {
                    "no_viewport": True,
                    # /dev/shm в Docker всего 64 МБ — видимый Chromium на
                    # тяжёлых страницах иначе падает
                    "args": ["--window-position=0,0", f"--window-size={w},{h}",
                             "--disable-dev-shm-usage"],
                }
            else:
                screen = {"viewport": {"width": 1366, "height": 900}}
            self.context = await self._pw.chromium.launch_persistent_context(
                str(profile_dir(self.site)),
                channel=_CHANNEL,
                headless=self.headless,
                user_agent=USER_AGENT,
                locale="ru-RU",
                **screen,
            )
        except Exception:
            self._lock.release()
            if self._pw:
                await self._pw.stop()
            raise
        return self.context

    async def __aexit__(self, *exc) -> None:
        try:
            if self.context:
                await self.context.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._lock.release()


async def check_logged_in(site: str) -> bool:
    """True, если сохранённая сессия площадки ещё жива (по контенту
    страницы кабинета, не по URL — редиректы неавторизованных меняются)."""
    if not has_profile(site):
        return False
    url, marker = LOGIN_CHECK[site]
    try:
        async with SiteBrowser(site) as ctx:
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            body = (await page.inner_text("body"))[:6000].lower()
            return marker in body
    except Exception:
        log.exception("check_logged_in(%s) failed", site)
        return False
