# Постинг и поднятия — статус и план калибровки

## Что готово (2026-07-14)
- Playwright + Chromium установлены; persistent-профили браузера в
  data/browser_profiles/<site>/ (mashina, lalafo, unaa).
- Логин: `python tools/login.py <site>` — открывает окно, пользователь
  логинится, сессия сохраняется. Проверка: /login_status в боте.
- Мастер /post: кросс-платформенная анкета (18 полей + фото из Telegram,
  фото сохраняются в data/photos/<user>_<ts>/).
- Таблица posted_ads: car_data JSON + sites JSON (status/external_id/url/
  last_bump/dot_state/error).
- /my_ads — статусы и управление; /adopt — берёт существующие объявления
  из кабинетов под управление (list_my_ads по ссылкам /details/ и /ads/).
- Поднятия по расписанию: раз в сутки, jitter ±2ч (main.scheduled_bumps).
  mashina: клик «Поднять» в кабинете. lalafo: точка в конце описания
  (добавить/убрать, состояние dot_state). Ошибки «оплатите/реклама» →
  уведомление в TG (PAYMENT_MARKERS в posting/manage.py).
- Еженедельный перепост lalafo: posting/post.repost_lalafo — НАПИСАН
  КАРКАС, ВЫКЛЮЧЕН (у пользователя активные объявления; включим и
  оттестируем позже, когда их не будет).

## Откалибровано по живой сессии (2026-07-14, opus)
- Логин работает через СИСТЕМНЫЙ Chrome (channel="chrome"): сборка
  Chromium из Playwright на этой машине не стартует (side-by-side error).
  Профили: mashina, lalafo залогинены и проверены.
- **unaa ПОЛНОСТЬЮ ИСКЛЮЧЁН** (SITES, POST_FORM_URLS, кнопки логина):
  unaa.kg — «сайт в разработке», всё только через моб. приложение.
- URL кабинетов: mashina `https://mashina.kg/profile/my-ads`,
  lalafo `https://lalafo.kg/account` (НЕ /account/ads).
- **Поднятие mashina = API** `POST /api/mbank-proxy/v1/ads/slug/<slug>/up`
  через fetch в браузере (куки сессии). Ответы:
  200/201 — поднято; 400 "up_limit"/"2 раза" — лимит 2/сутки (ок);
  400 "5 минут"/"подождите" — кулдаун (transient, не тревожим);
  оплата/баланс — payment_required → TG. slug = url после /details/.
  Лимиты подтверждены живьём.
- Кулдаун 5 мин между поднятиями → в scheduler разнос asyncio.sleep(330)
  между объявлениями mashina.
- **Поднятие lalafo = точка в описании**: url+"/edit", textarea.first,
  кнопка "Сохранить" (подтверждено live, без сохранения). Реклама
  («Запустить Рекламу») отдельная и необязательная — не мешает.
- Кабинет на 2026-07-14: mashina 4 активных (BMW X6, Lexus LX — авто;
  заднее сидение, колёса — запчасти); lalafo 3 (те же + заднее сидение).

## Что ещё требует калибровки
1. **Автозаполнение форм публикации** (_post_site в posting/post.py —
   сейчас заглушка «черновик сохранён»). Формы — многошаговые SPA с
   каскадными списками; нужно пройти их руками/Playwright-инспектором
   с залогиненной сессией и зафиксировать селекторы.
2. Селекторы кабинетов: кнопка «Поднять» (mashina), форма редактирования
   (lalafo edit URL — проверить точный путь), кабинет unaa.
3. URL'ы кабинетов в sessions.LOGIN_CHECK и manage.list_my_ads —
   проверить по факту.

## Порядок калибровки (следующая сессия)
1. Пользователь запускает `python tools/login.py mashina` (потом lalafo,
   unaa) и логинится.
2. /login_status в боте → все 🟢.
3. /adopt → существующие объявления попадают в posted_ads.
4. Прогнать bump_mashina/bump_lalafo_dot на реальном объявлении, поправить
   селекторы по фактическому DOM (можно смотреть скриншоты Playwright).
5. Прогнать публикацию тестового объявления через /post, дописать
   _post_site по шагам формы каждой площадки.

## Публикация на mashina.kg (2026-10-01, posting/mashina_post.py)

Форма /ads/create открывается только залогиненному (middleware смотрит cookie
`is_auth`), но её JavaScript публичный: страница с `is_auth=true` отдаёт
список чанков, в чанке формы — сборка тела (функция с createAd/presignImages).
- Варианты полей — ПУБЛИЧНЫЕ: `GET /api/mbank-proxy/v1/ads/<attr>/options?category_id=&parent_option_id=`.
  Легковые (cat 1): make 16 → model 17 → year 1 → body_type 18 → generation 13 →
  fuel_type 19 → drive_type 23 → gearbox 21 → modification 14 (каждый —
  parent_option_id предыдущего); steering 22, color 24, condition 20,
  availibility 96, Accounting 82 (учёт: КР, Абхазия, Армения, Казахстан,
  Россия, Беларусь, «Другая страна», «Не стоит на учёте»), is_customs_cleared 5
  (switch), mileage 2 = `{"value": "85000", "suffix": "км"}`, region 51 → city 50.
  Шины 31: tire_type 152, tire_width 138 («225 мм»), tire_height 139, tire_size 140
  («R17»), condition_parts 143. Диски 32: rims_type 151, wheel_pcd 136 («5x114.3»),
  wheel_size 137. «Колёс в сборе» у mashina нет — идут в диски.
- Тело `POST /ads`: `{category_id, image_ids: ["…"], attributes: {"<attr id>":
  "<id варианта>" | текст | объект}, title?, description, price, currency: "USD",
  vin_code?, phones: ["+996…"]}`. Заголовки сайта: `X-Project-Id: 1`, cookies сессии.
- Фото: `POST /ads/images/presign?count=N` → `{images: [{upload_url, image_id}]}`,
  PUT файла на upload_url (без cookies), после создания `POST /ads/images/confirm
  {listing_id}`.
- Телефон — из `GET /profile/me` (`phones`).
- Проверено без входа: тела для Camry 2018 (вся цепочка до «2.5 AT (181 л.с.)»),
  BMW X6, шин, дисков собираются верно. **Сам POST /ads не проверялся** (нужна
  сессия) — первая проверка на Маке; при ошибке бот покажет ответ сайта, тело
  запроса пишется в лог.
