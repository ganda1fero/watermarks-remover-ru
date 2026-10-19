#!/usr/bin/env bash
# Запуск HTTP-сервиса (Linux/macOS/Git Bash) с чтением файла .env.
# Обычный `python service/scripts/server.py` переменные из .env НЕ читает.
#
# Использование:
#   ./scripts/start-server.sh                 # 127.0.0.1:8765
#   ./scripts/start-server.sh 127.0.0.1 9000
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${1:-127.0.0.1}"
PORT="${2:-8765}"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
    echo "Настройки загружены из .env"
else
    echo "Файл .env не найден: сервис запустится без настроек (скопируйте .env.example в .env)." >&2
fi

# На Windows (Git Bash) `python3` часто заглушка Магазина: берём интерпретатор, который реально запускается.
PY=python3
"$PY" -c "" >/dev/null 2>&1 || PY=python
exec "$PY" service/scripts/server.py --host "$HOST" --port "$PORT"
