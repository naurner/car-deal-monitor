# Собирается нативно и под arm64 (Apple Silicon), и под amd64 (VPS).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONIOENCODING=utf-8 \
    RUNNING_IN_DOCKER=1

# Браузеры ставятся при сборке от root, а бот работает от cardeal — без
# общего пути Playwright ищет их в /home/cardeal/.cache и не находит.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Зависимости отдельным слоем — пока requirements.txt не менялся, пересборка
# после правки кода не тянет их заново.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Playwright ставит свой Chromium + системные библиотеки для него одной
# командой. Отдельный системный Chrome (как на Windows-машине разработки,
# см. posting/sessions.py) тут не нужен — сборка Playwright запускается
# в Linux-контейнере нормально.
RUN playwright install --with-deps chromium

# Вход на площадки из Telegram (posting/remote_login.py): виртуальный экран,
# VNC-сервер, веб-клиент noVNC и туннель cloudflared наружу.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH
ADD https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${TARGETARCH} /usr/local/bin/cloudflared
RUN chmod 755 /usr/local/bin/cloudflared

COPY . .

RUN useradd --create-home --uid 1000 cardeal \
    && mkdir -p /app/data \
    && chown -R cardeal:cardeal /app

# коммит, из которого собран образ — бот показывает его в меню
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
USER cardeal

# База, фото топ-3 и профили браузера (сессии площадок) живут в томе и
# переживают пересборку образа.
VOLUME ["/app/data"]

ENTRYPOINT ["python", "main.py"]
