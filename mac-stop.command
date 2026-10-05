#!/bin/bash
# Останавливает бота. База и настройки сохраняются.
set -u
cd "$(dirname "$0")" || exit 1

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не установлен — останавливать нечего."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 0
fi

echo "Останавливаю бота..."
docker compose down --remove-orphans

echo
echo "Остановлен. База и настройки в data/ на месте — запустить снова"
echo "можно файлом mac-start.command"
echo
read -r -p "Нажми Enter, чтобы закрыть..." _
