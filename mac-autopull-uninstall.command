#!/bin/bash
# Выключает автообновление. Сам бот и уже собранный контейнер не трогает.
set -u
PLIST="$HOME/Library/LaunchAgents/com.cardealtg.autopull.plist"

if [ ! -f "$PLIST" ]; then
  echo "Автообновление не было включено — выключать нечего."
else
  launchctl unload "$PLIST" >/dev/null 2>&1
  rm -f "$PLIST"
  echo "Автообновление выключено."
fi

echo
read -r -p "Нажми Enter, чтобы закрыть..." _
