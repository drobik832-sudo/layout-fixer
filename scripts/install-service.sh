#!/usr/bin/env bash
# Установить layout-fixer как пользовательский сервис systemd.
#
# Запуск:  ./scripts/install-service.sh [observe|fix]
#
# Почему сервис, а не запуск руками: демон должен работать постоянно и
# подниматься сам после перезагрузки. Плюс сервис запускается менеджером
# пользователя с ЧИСТЫМ окружением — это важно, потому что встроенный терминал
# snap-версии VS Code подменяет GTK-переменные и ломает нативные приложения.
#
# Логи:  journalctl --user -u layout-fixer -f

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-observe}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/layout-fixer.service"
BIN="$ROOT_DIR/.venv/bin/layout-fixer"

if [[ "$MODE" != "observe" && "$MODE" != "fix" ]]; then
    echo "Ошибка: режим должен быть observe или fix (получено: $MODE)" >&2
    exit 1
fi

if [[ ! -x "$BIN" ]]; then
    echo "Ошибка: не найден $BIN" >&2
    echo "Создайте окружение:  python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e . --no-build-isolation" >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=layout-fixer — автоисправление раскладки клавиатуры
Documentation=file://$ROOT_DIR/README.md
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
# Режим берётся из ~/.config/layout-fixer/config.toml (ключ mode), а не из юнита:
# один источник истины. Сменить: layout-fixer set-mode observe|fix
ExecStart=$BIN run
WorkingDirectory=$ROOT_DIR
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal
# Пауза/продолжить без остановки сервиса:  systemctl --user kill -s SIGUSR1 layout-fixer

[Install]
WantedBy=graphical-session.target
EOF

echo "1/3  юнит записан: $UNIT"
systemctl --user daemon-reload
echo "2/3  конфигурация systemd перечитана"
"$BIN" set-mode "$MODE" >/dev/null
systemctl --user enable --now layout-fixer.service
echo "3/3  сервис включён и запущен в режиме $MODE"
echo
systemctl --user --no-pager status layout-fixer.service | head -12
echo
echo "Полезные команды:"
echo "  journalctl --user -u layout-fixer -f              # смотреть лог"
echo "  systemctl --user restart layout-fixer             # перезапустить"
echo "  systemctl --user kill -s SIGUSR1 layout-fixer     # пауза/продолжить"
echo "  systemctl --user disable --now layout-fixer       # выключить"
echo "  systemctl --user set-environment ... / drop-in    # сменить режим observe <-> fix"
