#!/bin/bash
# Проверяет GitHub на обновления и пересобирает контейнер, если код изменился.
# Запускается сам через launchd — см. mac-autopull-install.command:
# раз в 5 минут и сразу, как бот положит data/update_request.json
# (кнопка «Обновить с GitHub», см. bot/host_update.py).
set -u
cd "$(dirname "$0")" || exit 1

# launchd запускает с урезанным PATH (/usr/bin:/bin:...) — без этого docker
# (ставится в /usr/local/bin) не находится.
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"

LOG="autopull.log"
# Лог не должен расти вечно — держим последние ~2000 строк.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
  tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# Запрос из Telegram: забираем его (переименованием — бот поймёт, что Мак
# взялся за работу) и по итогу пишем data/update_result.txt.
FROM_BOT=0
if [ -f data/update_request.json ]; then
  mv data/update_request.json data/update_inflight.json && FROM_BOT=1
fi

result() {  # status from to detail
  [ "$FROM_BOT" = 1 ] || return 0
  printf 'status=%s\nfrom=%s\nto=%s\ndetail=%s\n' "$1" "$2" "$3" "$4" > data/update_result.tmp \
    && mv data/update_result.tmp data/update_result.txt
}

echo "$(date '+%Y-%m-%d %H:%M:%S') проверяю обновления...$([ "$FROM_BOT" = 1 ] && echo ' (запрос из Telegram)')" >> "$LOG"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "  это не git-репозиторий — автообновление недоступно" >> "$LOG"
  result error "" "" "папка бота на Маке не git-клон"
  exit 1
fi

if ! git fetch origin main --quiet 2>>"$LOG"; then
  echo "  fetch не удался (нет сети или доступа к репозиторию)" >> "$LOG"
  result error "" "" "Мак не достучался до GitHub (сеть или SSH-ключ)"
  exit 0
fi

LOCAL=$(git rev-parse --short HEAD)
REMOTE=$(git rev-parse --short origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  echo "  без изменений ($LOCAL)" >> "$LOG"
  result nochange "$LOCAL" "$LOCAL" ""
  exit 0
fi

echo "  обновление $LOCAL -> $REMOTE" >> "$LOG"
# reset, а не pull: на Маке после chmod +x git считает скрипты изменёнными,
# и pull отказывается их обновлять. Правок кода здесь не бывает, а .env и
# data/ не отслеживаются git — reset их не трогает.
if ! git reset --hard origin/main --quiet >> "$LOG" 2>&1; then
  echo "  обновить код не удалось, оставляю как было" >> "$LOG"
  result error "$LOCAL" "$REMOTE" "git reset не удался — см. autopull.log"
  exit 1
fi

echo "  пересобираю контейнер..." >> "$LOG"
export GIT_COMMIT="$REMOTE"
if docker compose up -d --build >> "$LOG" 2>&1; then
  echo "  готово" >> "$LOG"
  result updated "$LOCAL" "$REMOTE" ""
else
  echo "  сборка не удалась — работает прежний контейнер" >> "$LOG"
  result error "$LOCAL" "$REMOTE" "сборка контейнера не удалась (работает прежняя версия) — см. autopull.log"
fi
