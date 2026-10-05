#!/bin/bash
# Запуск бота на Mac через Docker. Двойной клик по файлу.
set -u
cd "$(dirname "$0")" || exit 1

echo "CarDealTG"
echo

if ! command -v docker >/dev/null 2>&1; then
  cat <<'EOF'
  Docker не установлен.

  1. Скачай Docker Desktop: https://www.docker.com/products/docker-desktop/
     Выбери версию для Apple Silicon (Mac с чипом M1/M2/M3).
  2. Установи и запусти его — в верхней строке появится значок кита.
  3. Запусти этот файл ещё раз.

EOF
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "  Docker установлен, но не запущен."
  echo "  Открой Docker Desktop, дождись значка кита в верхней строке,"
  echo "  затем запусти этот файл снова."
  echo
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

mkdir -p data

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
  fi
  echo "Первый запуск: заполни .env (BOT_TOKEN и OWNER_TELEGRAM_ID) и запусти файл снова."
  echo "Открываю .env для правки..."
  open -e .env 2>/dev/null || true
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

# Останавливаем прошлый запуск. Без этого второй экземпляр конфликтует
# с первым: Telegram отдаёт сообщения только одному боту с этим токеном
# (плюс у самого бота есть свой lock-файл — см. data/bot.lock).
docker compose down --remove-orphans >/dev/null 2>&1

echo "Собираю контейнер (при первом запуске — пара минут, тянет Chromium)..."
echo

export GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if ! docker compose up -d --build; then
  echo
  echo "  Запустить не удалось. Покажи вывод выше — по нему видно причину."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

echo
echo "Работает. Показываю логи — закрыть окно можно в любой момент,"
echo "бот от этого не остановится. Выйти из логов: Ctrl+C"
echo
echo "Чтобы войти в аккаунт площадки (mashina.kg / lalafo.kg) — команда"
echo "/login в самом боте объяснит, что делать (вход выполняется локально,"
echo "не в контейнере, т.к. внутри него негде показать окно браузера)."
echo
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && git remote get-url origin >/dev/null 2>&1; then
  echo "Автообновление с GitHub раз в 5 минут: mac-autopull-install.command"
  echo
fi
docker compose logs -f
