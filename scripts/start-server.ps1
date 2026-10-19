# Запуск HTTP-сервиса на Windows с чтением файла .env.
# Обычный `python service/scripts/server.py` переменные из .env НЕ читает,
# поэтому настройки (модель, адрес Ollama, параметры swap) надо передать через окружение.
#
# Использование:
#   .\scripts\start-server.ps1                 # 127.0.0.1:8765, настройки из .env
#   .\scripts\start-server.ps1 -Port 9000
param(
    [string]$ListenHost = "127.0.0.1",
    [int]$Port = 8765,
    [string]$EnvFile = ""
)

$root = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $root ".env" }

if (Test-Path $EnvFile) {
    Get-Content $EnvFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $name, $value = $line -split '=', 2
            # отрезаем хвостовой комментарий " # ..." и кавычки
            $value = ($value -replace '\s+#.*$', '').Trim().Trim('"').Trim("'")
            if ($value -ne '') { Set-Item -Path "Env:$($name.Trim())" -Value $value }
        }
    }
    Write-Host "Настройки загружены из $EnvFile"
} else {
    Write-Warning "Файл $EnvFile не найден: сервис запустится без настроек (скопируйте .env.example в .env)."
}

Set-Location $root
python service/scripts/server.py --host $ListenHost --port $Port
