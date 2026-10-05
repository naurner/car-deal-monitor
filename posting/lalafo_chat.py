"""Переписка с продавцами lalafo от имени владельца (его сессия браузера).

Отправка — через интерфейс объявления: «Написать» → поле «Спросить продавца»
→ «Отправить» (сам сайт шлёт сообщения через сокет, отдельного HTTP-метода
нет). Чтение — тем же HTTP API, которым пользуется сайт; токен (Bearer)
берём из запросов открытой страницы чатов. Разведка API — 2026-09.
"""

import asyncio
import base64
import json
import logging
import re
import uuid

from posting.sessions import SiteBrowser

log = logging.getLogger(__name__)

API = "https://lalafo.kg/api/chat/v4"
CHATS_PAGE = "https://lalafo.kg/account/chats"
BASE_HEADERS = {"device": "pc", "country-id": "12", "language": "ru_RU",
                "content-type": "application/json"}
MSG_TEXT = 1  # type=21 — автоприветствие бизнес-аккаунта, это не ответ продавца
THREAD_RE = re.compile(r"\{ad_chat,\s*(\d+),\s*(\d+),\s*(\d+)\}")


class SessionExpired(RuntimeError):
    pass


def ad_id_from_url(url: str) -> int | None:
    m = re.search(r"-id-(\d+)", url)
    return int(m.group(1)) if m else None


class LalafoChat:
    """async with LalafoChat() as chat: await chat.send(...); await chat.replies(...)"""

    def __init__(self) -> None:
        self._browser = SiteBrowser("lalafo")
        self.token: str | None = None
        self.me: int | None = None

    async def __aenter__(self) -> "LalafoChat":
        self.ctx = await self._browser.__aenter__()
        try:
            self.page = await self.ctx.new_page()

            def grab(request) -> None:
                auth = request.headers.get("authorization", "")
                if not self.token and "/api/" in request.url and auth.startswith("Bearer "):
                    self.token = auth

            self.page.on("request", grab)
            await self.page.goto(CHATS_PAGE, wait_until="domcontentloaded")
            for _ in range(30):
                if self.token:
                    break
                await asyncio.sleep(0.5)
            if not self.token:
                raise SessionExpired("lalafo: сессия истекла — войдите заново: /login")
            payload = self.token.split()[1].split(".")[1]
            payload += "=" * (-len(payload) % 4)
            self.me = int(json.loads(base64.urlsafe_b64decode(payload))["id"])
        except BaseException:
            await self._browser.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self._browser.__aexit__(*exc)

    async def _post(self, path: str, body: dict) -> dict:
        body = {**body, "ack": str(uuid.uuid4())}
        resp = await self.ctx.request.post(
            API + path, data=json.dumps(body),
            headers={**BASE_HEADERS, "authorization": self.token},
        )
        if resp.status in (401, 403):
            raise SessionExpired("lalafo: сессия истекла — войдите заново: /login")
        if not resp.ok:
            raise RuntimeError(f"lalafo chat API {path}: HTTP {resp.status}")
        return await resp.json()

    async def threads(self, pages: int = 3, limit: int = 50) -> list[dict]:
        out: list[dict] = []
        for page in range(pages):
            data = await self._post("/chat-update/get-paginated?sort=byNewest", {
                "syncTime": 0, "feedType": [1, 3], "ref": "ChatUpdatesPaginated",
                "type": [1, 2], "status": [1], "skip": page * limit, "limit": limit,
            })
            batch = data.get("chatUpdates") or []
            out += batch
            if len(batch) < limit:
                break
        return out

    @staticmethod
    def thread_ad_id(thread: dict) -> int | None:
        m = THREAD_RE.search(thread.get("threadId") or "")
        return int(m.group(1)) if m else (thread.get("ad") or {}).get("id")

    async def messages(self, thread: dict, count: int = 50) -> list[dict]:
        m = THREAD_RE.search(thread.get("threadId") or "")
        if not m:
            return []
        ad_id, u1, u2 = (int(x) for x in m.groups())
        data = await self._post("/message/retrieve", {
            "feedType": 1, "feedId": {"adId": ad_id, "userId1": u1, "userId2": u2},
            "count": count, "direction": "up", "ref": "Retrieve",
        })
        return data.get("messages") or []

    async def send(self, ad_url: str, text: str) -> bool:
        """Пишет продавцу. True — сообщение нашлось в переписке после отправки."""
        page = self.page
        await page.goto(ad_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        write = page.get_by_role("button", name="Написать")
        if await write.count() == 0:
            log.warning("lalafo: нет кнопки «Написать» (чат скрыт продавцом?) %s", ad_url)
            return False
        await write.first.click()
        await page.wait_for_timeout(2000)
        boxes = page.locator("textarea[placeholder='Спросить продавца']")
        box = None
        for i in range(await boxes.count()):
            if await boxes.nth(i).is_visible():
                box = boxes.nth(i)
                break
        if box is None:
            log.warning("lalafo: не нашёл поле сообщения %s", ad_url)
            return False
        await box.fill(text)
        send = page.get_by_role("button", name="Отправить")
        clicked = False
        for i in range(await send.count()):
            if await send.nth(i).is_visible():
                await send.nth(i).click()
                clicked = True
                break
        if not clicked:
            await box.press("Enter")
        return await self._confirm_sent(ad_id_from_url(ad_url), text)

    async def _confirm_sent(self, ad_id: int | None, text: str) -> bool:
        if ad_id is None:
            return False
        for _ in range(6):
            await asyncio.sleep(2)
            for t in await self.threads(pages=1, limit=20):
                if self.thread_ad_id(t) != ad_id:
                    continue
                for msg in await self.messages(t, count=10):
                    if msg.get("origin") == self.me and (msg.get("payload") or "").strip() == text.strip():
                        return True
        return False

    async def seller_replies(self, ad_id: int, after_ts: float,
                             threads: list[dict] | None = None) -> list[str]:
        """Тексты продавца по объявлению после нашего сообщения (без автоприветствий)."""
        for t in threads if threads is not None else await self.threads():
            if self.thread_ad_id(t) != ad_id:
                continue
            return [
                (m.get("payload") or "").strip()
                for m in sorted(await self.messages(t), key=lambda m: m.get("created", 0))
                if m.get("origin") != self.me and m.get("type") == MSG_TEXT
                and m.get("created", 0) > after_ts and (m.get("payload") or "").strip()
            ]
        return []
