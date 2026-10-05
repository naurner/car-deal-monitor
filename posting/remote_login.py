"""Вход на площадку прямо из Telegram, когда бот крутится в Docker без экрана.

Поднимаем виртуальный экран (Xvfb), открываем на нём обычный браузер с
профилем площадки и показываем экран через noVNC. Наружу его выводит
временный https-туннель cloudflared — Telegram открывает такую ссылку как
Mini App. VNC-пароль одноразовый, туннель живёт только пока идёт вход.
"""

import asyncio
import logging
import os
import re
import secrets
from dataclasses import dataclass, field

from playwright.async_api import BrowserContext, Page

from posting.sessions import LOGIN_CHECK, SiteBrowser

log = logging.getLogger(__name__)

DISPLAY = ":99"
SCREEN = (1024, 768)  # под экран телефона, noVNC масштабирует
VNC_PORT = 5900
WEB_PORT = 6080
NOVNC_DIR = "/usr/share/novnc"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
TUNNEL_WAIT_SEC = 60
SESSION_TIMEOUT_SEC = 15 * 60


@dataclass
class LoginSession:
    site: str
    url: str
    browser: SiteBrowser
    context: BrowserContext
    procs: list[asyncio.subprocess.Process] = field(default_factory=list)
    timeout_task: asyncio.Task | None = None


_current: LoginSession | None = None
_starting = False  # запуск идёт до минуты — не даём нажать сайт второй раз


def current() -> LoginSession | None:
    return _current


async def _spawn(*args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )


async def _drain(stream: asyncio.StreamReader) -> None:
    # иначе переполненный pipe заблокирует cloudflared
    while await stream.readline():
        pass


async def _tunnel_url(proc: asyncio.subprocess.Process) -> str:
    while True:
        line = await proc.stderr.readline()
        if not line:
            raise RuntimeError("cloudflared завершился, не выдав адрес туннеля")
        m = TUNNEL_RE.search(line.decode(errors="replace"))
        if m:
            asyncio.create_task(_drain(proc.stderr))
            return m.group(0)


async def _kill(procs: list[asyncio.subprocess.Process]) -> None:
    for p in reversed(procs):
        if p.returncode is None:
            p.terminate()
    for p in procs:
        try:
            await asyncio.wait_for(p.wait(), 5)
        except asyncio.TimeoutError:
            p.kill()


async def start(site: str) -> LoginSession:
    global _starting
    if _current:
        raise RuntimeError(f"уже идёт вход на {_current.site} — завершите его")
    if _starting:
        raise RuntimeError("браузер уже запускается — подождите")
    _starting = True
    try:
        return await _start(site)
    finally:
        _starting = False


async def _start(site: str) -> LoginSession:
    global _current
    password = secrets.token_hex(4)  # VNC-пароль не длиннее 8 символов
    w, h = SCREEN
    procs: list[asyncio.subprocess.Process] = []
    try:
        procs.append(await _spawn("Xvfb", DISPLAY, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"))
        await asyncio.sleep(1)
        procs.append(await _spawn(
            "x11vnc", "-display", DISPLAY, "-rfbport", str(VNC_PORT), "-localhost",
            "-forever", "-shared", "-passwd", password, "-quiet",
        ))
        procs.append(await _spawn(
            "websockify", "--web", NOVNC_DIR, f"127.0.0.1:{WEB_PORT}", f"127.0.0.1:{VNC_PORT}",
        ))
        tunnel = await asyncio.create_subprocess_exec(
            "cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{WEB_PORT}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        procs.append(tunnel)
        base = await asyncio.wait_for(_tunnel_url(tunnel), TUNNEL_WAIT_SEC)
    except BaseException:
        await _kill(procs)
        raise

    os.environ["DISPLAY"] = DISPLAY
    browser = SiteBrowser(site, headless=False, window=SCREEN)
    try:
        context = await browser.__aenter__()
    except BaseException:
        await _kill(procs)
        raise
    session = LoginSession(
        site=site,
        url=f"{base}/vnc.html?autoconnect=1&resize=scale&reconnect=1&password={password}",
        browser=browser, context=context, procs=procs,
    )
    _current = session
    try:
        page = await active_page()
        await page.goto(LOGIN_CHECK[site][0], wait_until="domcontentloaded")
    except BaseException:
        await finish()
        raise
    return session


async def active_page() -> Page:
    """Последняя открытая вкладка; если пользователь закрыл все — открываем заново."""
    session = _current
    if session.context.pages:
        return session.context.pages[-1]
    page = await session.context.new_page()
    await page.goto(LOGIN_CHECK[session.site][0], wait_until="domcontentloaded")
    return page


async def type_text(text: str) -> None:
    page = await active_page()
    await page.keyboard.type(text, delay=40)


async def press_enter() -> None:
    page = await active_page()
    await page.keyboard.press("Enter")


async def screenshot() -> bytes:
    page = await active_page()
    return await page.screenshot()


async def finish() -> None:
    """Закрывает браузер (профиль с сессией сохраняется) и гасит экран/туннель."""
    global _current
    session = _current
    if session is None:
        return
    _current = None
    task = asyncio.current_task()
    if session.timeout_task and session.timeout_task is not task:
        session.timeout_task.cancel()
    try:
        await session.browser.__aexit__(None, None, None)
    except Exception:
        log.exception("remote login: закрытие браузера")
    await _kill(session.procs)
