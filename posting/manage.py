"""Управление размещёнными объявлениями: поднятия, детект ошибок оплаты.

ВАЖНО: селекторы кабинетов уточняются по живой сессии (после tools/login.py).
Все действия возвращают ActionResult; ошибки оплаты — отдельным флагом,
бот шлёт их в Telegram.
"""

import copy
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime

from db.models import PostedAd
from db.session import get_session
from posting.sessions import LOGIN_CHECK, SiteBrowser

log = logging.getLogger(__name__)

# фразы площадок, означающие "нужна оплата/реклама"
PAYMENT_MARKERS = [
    "оплатите объявление", "оплатить объявление", "нужно оплатить",
    "платное размещение", "оплатите рекламу", "нужна реклама",
    "продвижение", "активируйте объявление", "лимит бесплатных",
]


@dataclass
class ActionResult:
    ok: bool
    payment_required: bool = False
    transient: bool = False  # временный кулдаун — не тревожить пользователя
    message: str = ""


def _find_payment_marker(text: str) -> str | None:
    low = text.lower()
    for marker in PAYMENT_MARKERS:
        if marker in low:
            return marker
    return None


def _mashina_slug(info: dict) -> str | None:
    """slug для API поднятия: из url /details/<slug> или из external_id."""
    url = info.get("url") or ""
    if "/details/" in url:
        return url.split("/details/", 1)[1].strip("/")
    return None


# прямой вызов API поднятия (надёжнее клика по иконке).
# Ответы: 200/201 — поднято; 400 up_limit — лимит 2/сутки; иначе смотрим текст.
_MASHINA_UP_JS = """async (slug) => {
    try {
        const resp = await fetch(`/api/mbank-proxy/v1/ads/slug/${slug}/up`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'include',
        });
        let body = '';
        try { body = await resp.text(); } catch (e) {}
        return {status: resp.status, body: body.slice(0, 600)};
    } catch (e) {
        return {status: 0, body: String(e)};
    }
}"""


async def bump_mashina(ad: PostedAd) -> ActionResult:
    """Поднятие объявления mashina.kg через API /ads/slug/<slug>/up."""
    info = (ad.sites or {}).get("mashina") or {}
    slug = _mashina_slug(info)
    if not slug:
        return ActionResult(False, message="mashina.kg: не удалось определить slug объявления")
    async with SiteBrowser("mashina") as ctx:
        page = await ctx.new_page()
        await page.goto("https://mashina.kg/profile/my-ads", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        if "Мои объявления" not in (await page.inner_text("body"))[:6000]:
            return ActionResult(False, message="Сессия mashina.kg истекла — войдите заново: 📤 Мои объявления → 🔐 Вход на площадки")
        res = await page.evaluate(_MASHINA_UP_JS, slug)
    status, body = res["status"], res["body"]
    low = body.lower()

    if status in (200, 201):
        return ActionResult(True, message="mashina.kg: объявление поднято")
    if "up_limit" in low or "2 раза" in low:
        # уже поднято максимум раз за сутки — это не проблема, тихо считаем ок
        return ActionResult(True, message="mashina.kg: уже поднято сегодня (лимит 2/сутки)")
    if "5 минут" in low or "подождите" in low or "cooldown" in low:
        # кулдаун между поднятиями — временно, попробуем в следующий раз
        return ActionResult(False, transient=True,
                            message=f"mashina.kg: кулдаун, {_extract_msg(body)}")
    if _find_payment_marker(body) or "оплат" in low or "баланс" in low:
        return ActionResult(False, payment_required=True,
                            message=f"mashina.kg: требуется оплата — {_extract_msg(body)}")
    if status in (401, 403):
        return ActionResult(False, message="mashina.kg: сессия истекла — войдите заново: 📤 Мои объявления → 🔐 Вход на площадки")
    if "not found" in low or status == 404:
        return ActionResult(False, message="mashina.kg: объявление не найдено (снято/неактивно)")
    return ActionResult(False, message=f"mashina.kg: не удалось поднять ({status}): {_extract_msg(body)}")


def _extract_msg(body: str) -> str:
    import json
    try:
        return json.loads(body).get("message", body[:150])
    except Exception:
        return body[:150]


async def bump_lalafo_dot(ad: PostedAd) -> ActionResult:
    """Поднятие lalafo: редактируем описание — добавляем/убираем точку в конце.

    URL редактирования = URL объявления + /edit (проверено по кабинету).
    """
    info = (ad.sites or {}).get("lalafo") or {}
    url = info.get("url")
    if not url:
        return ActionResult(False, message="lalafo: у объявления нет url")
    if not url.startswith("http"):
        url = "https://lalafo.kg" + url
    async with SiteBrowser("lalafo") as ctx:
        page = await ctx.new_page()
        await page.goto(url.rstrip("/") + "/edit", wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)
        body = (await page.inner_text("body"))[:8000]
        # маркер входа — см. posting/sessions.py LOGIN_CHECK (тот же принцип)
        _, marker = LOGIN_CHECK["lalafo"]
        if marker not in body.lower():
            return ActionResult(False, message="Сессия lalafo истекла — войдите заново: 📤 Мои объявления → 🔐 Вход на площадки")
        textarea = page.locator("textarea[name='editable-description__input']")
        if await textarea.count() == 0:
            textarea = page.locator("textarea")
        textarea = textarea.first
        if await textarea.count() == 0:
            marker = _find_payment_marker(body)
            if marker:
                return ActionResult(False, payment_required=True,
                                    message=f"lalafo: «{marker}» — вероятно, требуется оплата/реклама")
            return ActionResult(False, message="lalafo: поле описания не найдено (уточним селектор)")
        current = (await textarea.input_value()).rstrip()
        # решаем по самому тексту, а не по сохранённому состоянию: раньше оно
        # не сохранялось, и точки копились — так заодно убираем лишние
        new_text = current.rstrip(".").rstrip() if current.endswith(".") else current + "."
        # без клика и паузы сайт не успевает подхватить вставленный текст и
        # сохраняет прежний — а мы при этом считали поднятие успешным
        await textarea.click()
        await textarea.fill(new_text)
        await page.wait_for_timeout(800)
        save = page.get_by_role("button", name="Сохранить")
        if await save.count() == 0:
            save = page.get_by_text("Сохранить", exact=False)
        if await save.count() == 0:
            return ActionResult(False, message="lalafo: кнопка «Сохранить» не найдена")
        try:
            async with page.expect_request(
                lambda r: r.method in ("PUT", "POST") and "/posting-ads/" in r.url, timeout=15000
            ) as req_info:
                await save.first.click()
            req = await req_info.value
        except Exception:
            return ActionResult(False, message="lalafo: после «Сохранить» сайт ничего не отправил")
        try:
            sent = (json.loads(req.post_data or "{}").get("description") or "").rstrip()
        except ValueError:
            sent = ""
        if sent != new_text.rstrip():
            return ActionResult(False, message="lalafo: новое описание не ушло на сервер — поднятие не засчитано")
        resp = await req.response()
        if resp is not None and not resp.ok:
            return ActionResult(False, message=f"lalafo: сохранение отклонено (HTTP {resp.status})")
        await page.wait_for_timeout(2000)
        after = (await page.inner_text("body"))[:8000]
        marker = _find_payment_marker(after)
        if marker:
            return ActionResult(False, payment_required=True,
                                message=f"lalafo: после сохранения — «{marker}»")
        return ActionResult(True, message="lalafo: описание обновлено (точка), объявление поднято")


BUMPERS = (("mashina", bump_mashina), ("lalafo", bump_lalafo_dot))


def _update_site(ad_id: int, site: str, **fields) -> None:
    """Меняет поля площадки в posted_ads.sites. Копия ГЛУБОКАЯ: если поправить
    вложенный словарь на месте, SQLAlchemy считает JSON-колонку неизменённой и
    ничего не пишет — так терялись время поднятия и состояние точки lalafo."""
    with get_session() as s:
        row = s.get(PostedAd, ad_id)
        sites = copy.deepcopy(row.sites or {})
        sites.setdefault(site, {}).update(fields)
        row.sites = sites
        s.commit()


def _mark_bumped(ad_id: int, site: str) -> None:
    _update_site(ad_id, site, last_bump=datetime.utcnow().isoformat())


def has_mashina(ad: PostedAd) -> bool:
    return ((ad.sites or {}).get("mashina") or {}).get("status") == "posted"


async def bump_ad(ad: PostedAd) -> list[tuple[str, ActionResult]]:
    """Поднимает объявление на всех площадках, где оно опубликовано."""
    out = []
    for site, action in BUMPERS:
        if ((ad.sites or {}).get(site) or {}).get("status") != "posted":
            continue
        try:
            res = await action(ad)
        except Exception as e:
            log.exception("bump %s ad=%s", site, ad.id)
            res = ActionResult(False, message=f"{site}: ошибка поднятия: {e}")
        if res.ok:
            _mark_bumped(ad.id, site)
        out.append((site, res))
    return out


# JS: для каждой ссылки на объявление вернуть href + чистый заголовок из <h3>
_LIST_ADS_JS = """(sel) => {
    const out = [];
    const seen = new Set();
    document.querySelectorAll(sel).forEach(a => {
        const href = a.getAttribute('href') || '';
        const p = href.replace(/\\/$/, '');
        if (!p || seen.has(p)) return;
        if (p.endsWith('/edit') || p.endsWith('/create') || p.endsWith('/ads')) return;
        seen.add(p);
        // чистый заголовок: <h1-4> внутри ссылки или в карточке-предке
        let title = '';
        const h = a.querySelector('h1,h2,h3,h4');
        if (h) title = h.innerText.trim();
        if (!title) {
            let n = a;
            for (let i = 0; i < 5 && n; i++) {
                n = n.parentElement;
                const hh = n && n.querySelector('h1,h2,h3,h4');
                if (hh) { title = hh.innerText.trim(); break; }
            }
        }
        out.push({href, title: title.slice(0, 80)});
    });
    return out;
}"""


async def list_my_ads(site: str) -> list[dict]:
    """Объявления из кабинета: [{'title','url','external_id','key'}]. key — ключ
    объединения одного авто между площадками (см. _merge_key)."""
    urls = {
        "mashina": "https://mashina.kg/profile/my-ads",
        "lalafo": "https://lalafo.kg/account",
        "unaa": None,  # веб-сайт unaa «в разработке», кабинета нет
    }
    if urls.get(site) is None:
        return []
    selector = "a[href*='/details/']" if site == "mashina" else "a[href*='/ads/']"
    result: list[dict] = []
    async with SiteBrowser(site) as ctx:
        page = await ctx.new_page()
        await page.goto(urls[site], wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(selector, timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        _, marker = LOGIN_CHECK[site]
        body = (await page.inner_text("body"))[:6000].lower()
        if marker not in body:
            raise RuntimeError(
                f"сессия {site} истекла — выполните /login (или python tools/login.py {site})"
            )
        items = await page.evaluate(_LIST_ADS_JS, selector)
    for it in items:
        path = it["href"].rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        title = it["title"]
        if not title:
            title = slug.rsplit("-id-", 1)[0].replace("-", " ")[:80]
        ext = slug.rsplit("-", 1)[-1].replace("id-", "")
        result.append({
            "title": title,
            "url": it["href"],
            "external_id": ext,
            "key": _merge_key(slug),
        })
    return result


# определяем «одно авто» по слагу: берём токены до года(4 цифры)/hex(12+)/id
_HEX_RE = re.compile(r"^[0-9a-f]{12,}$")


def _merge_key(slug: str) -> str:
    parts = []
    for tok in slug.split("-"):
        if tok == "id" or tok.isdigit() and len(tok) == 4 or _HEX_RE.match(tok):
            break
        if tok.isdigit():
            break
        parts.append(tok)
    return "-".join(parts) or slug


def next_bump_delay_hours() -> float:
    """~раз в сутки с небольшим рандомом."""
    return 24 + random.uniform(-2, 2)


def next_bump_delay_hours() -> float:
    """~раз в сутки с небольшим рандомом."""
    return 24 + random.uniform(-2, 2)
