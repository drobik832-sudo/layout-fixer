#!/usr/bin/env bash
# Выдать доступ к /dev/uinput, чтобы демон мог инъецировать клавиши.
#
# Запускать от root:  sudo ./scripts/setup-uinput.sh
#
# Почему нужен sudo: /dev/uinput по умолчанию доступен только root'у
# (crw------- root root), а без него инъекция клавиш на Wayland невозможна.
# Демон при этом работает от обычного пользователя.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULE_SRC="$ROOT_DIR/udev/99-layout-fixer-uinput.rules"
RULE_DST="/etc/udev/rules.d/99-layout-fixer-uinput.rules"

if [[ $EUID -ne 0 ]]; then
    echo "Ошибка: скрипт нужно запускать через sudo." >&2
    exit 1
fi

USER_NAME="${SUDO_USER:-}"
if [[ -z "$USER_NAME" ]]; then
    echo "Предупреждение: SUDO_USER не определён, проверка группы будет пропущена." >&2
fi

echo "1/4  установка udev-правила -> $RULE_DST"
install -m 0644 "$RULE_SRC" "$RULE_DST"

echo "2/4  перезагрузка правил udev"
udevadm control --reload-rules
udevadm trigger --subsystem-match=misc

echo "3/4  применение прав к /dev/uinput сейчас (без перезагрузки)"
if [[ ! -e /dev/uinput ]]; then
    modprobe uinput || true
fi
if [[ -e /dev/uinput ]]; then
    chgrp input /dev/uinput
    chmod 0660 /dev/uinput
fi

echo "4/4  проверка"
ls -l /dev/uinput
if [[ -n "$USER_NAME" ]] && ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx input; then
    echo
    echo "ВНИМАНИЕ: пользователь $USER_NAME не входит в группу input."
    echo "Выполните:  sudo usermod -aG input $USER_NAME   и перелогиньтесь"
fi

echo
echo "Готово. Проверка работоспособности (от обычного пользователя):"
echo "  .venv/bin/layout-fixer type 'test' --layout us"
