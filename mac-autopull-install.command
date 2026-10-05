#!/bin/bash
# Включает автообновление: раз в 5 минут Мак сам проверяет GitHub
# и пересобирает контейнер, если код изменился.
set -u
cd "$(dirname "$0")" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Это не git-репозиторий."
  echo "Автообновление работает только на папке, склонированной через git clone —"
  echo "той, что распакована из .zip, оно работать не будет."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "У репозитория не задан адрес origin — автообновлять неоткуда."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

DIR="$(pwd)"

# macOS не пускает фоновые задачи (launchd) в «Документы», «Рабочий стол» и
# «Загрузки» — автообновление падало бы с "Operation not permitted".
case "$(echo "$DIR" | tr '[:upper:]' '[:lower:]')" in
  "$(echo "$HOME" | tr '[:upper:]' '[:lower:]')"/documents*|\
  "$(echo "$HOME" | tr '[:upper:]' '[:lower:]')"/desktop*|\
  "$(echo "$HOME" | tr '[:upper:]' '[:lower:]')"/downloads*)
    echo "Папка бота лежит в Документах / на Рабочем столе / в Загрузках —"
    echo "macOS не даёт фоновому автообновлению туда доступ."
    echo "Перенеси папку, например в домашнюю: mv \"$DIR\" ~/cardealtg"
    read -r -p "Нажми Enter, чтобы закрыть..." _
    exit 1
    ;;
esac

PLIST_NAME="com.cardealtg.autopull.plist"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__DIR__|$DIR|g" "$PLIST_NAME" > "$DEST"

launchctl unload "$DEST" >/dev/null 2>&1
launchctl load "$DEST"

echo "Автообновление включено."
echo "Проверка раз в 5 минут: $(git remote get-url origin)"
echo "Лог: $DIR/autopull.log"
echo
echo "Выключить: mac-autopull-uninstall.command"
echo
read -r -p "Нажми Enter, чтобы закрыть..." _
