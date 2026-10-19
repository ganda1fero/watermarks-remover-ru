# Запуск сервиса на Windows одной командой: проверяет и докачивает всё нужное, затем запускает сервис.
# Обычно запускается двойным щелчком по start.bat в корне репозитория.
#
# Что делает (каждый раз, быстро, если всё уже на месте):
#   1. Python 3.10+ (если нет: предложит установить через winget)
#   2. Виртуальное окружение .venv и пакеты из requirements.txt
#   3. Внешние программы для файлов (exiftool, qpdf, ffmpeg): предупредит и предложит поставить через winget
#   4. Файл .env (создаст из .env.example)
#   5. Ollama и модель из .env: только сообщит, чего не хватает; модель сам НЕ скачивает
#   6. Запуск сервиса
param(
    [string]$ListenHost = "127.0.0.1",
    [int]$Port = 8765,
    [switch]$NoChecks,
    [switch]$InstallTools   # снова предложить программы, от которых раньше отказались (ответ n)
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ask3($question) {
    # y = да; Enter = пропустить сейчас (спросим в следующий раз); n = пропустить и больше не спрашивать
    $answer = Read-Host "$question [y = установить, Enter = пропустить, n = больше не спрашивать]"
    if ($null -eq $answer) { return "skip" }
    $a = $answer.Trim().ToLower()
    if ($a -in @("y", "yes", "д", "да")) { return "yes" }
    if ($a -in @("n", "no", "н", "нет")) { return "never" }
    return "skip"
}
function Ask($question) {
    # простой вопрос: y = да, всё остальное (в том числе Enter) = нет, ничего не запоминается
    $answer = Read-Host "$question [y = да, Enter = нет]"
    if ($null -eq $answer) { return $false }
    return ($answer.Trim().ToLower() -in @("y", "yes", "д", "да"))
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Has-Command($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Winget-Install($id, $title) {
    if (-not (Has-Command "winget")) {
        Say "winget не найден: установите $title вручную." Yellow
        return $false
    }
    Say "Устанавливаю $title через winget (ниже виден ход загрузки)..." White
    & winget install --id $id --exact --accept-package-agreements --accept-source-agreements
    Refresh-Path
    return ($LASTEXITCODE -eq 0)
}
function Read-EnvFile($path) {
    $result = @{}
    if (Test-Path $path) {
        Get-Content $path -Encoding UTF8 | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $name, $value = $line -split "=", 2
                $value = ($value -replace "\s+#.*$", "").Trim().Trim('"').Trim("'")
                if ($value -ne "") { $result[$name.Trim()] = $value }
            }
        }
    }
    return $result
}

Write-Host ""
Say "================================================================" Green
Say "  Сервис очистки текста от следов ИИ (watermarks-remover-ru)" Green
Say "================================================================" Green
Say "Это окно показывает только журнал работы сервиса."
Say "Чтобы остановить сервис, нажмите Ctrl+C или закройте окно."
Say "Адрес сервиса: http://${ListenHost}:${Port}  (проверка: /health)"

# ---------------------------------------------------------------- 1. Python
function Find-Python {
    $candidates = @(@("py", "-3"), @("python"), @("python3"))
    foreach ($c in $candidates) {
        if (-not (Has-Command $c[0])) { continue }
        try {
            $args2 = @()
            if ($c.Count -gt 1) { $args2 += $c[1..($c.Count - 1)] }
            $args2 += @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
            $ver = & $c[0] @args2 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -match "^(\d+)\.(\d+)$" -and ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10))) {
                return ,$c
            }
        } catch {}
    }
    return $null
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Step "1/6 Python"
    $py = Find-Python
    if (-not $py) {
        Say "Python 3.10 или новее не найден." Yellow
        if (Ask "Установить Python 3.12 через winget?") {
            if (Winget-Install "Python.Python.3.12" "Python 3.12") { $py = Find-Python }
        }
        if (-not $py) {
            Say "Без Python сервис не запустить. Установите его с https://www.python.org/downloads/ (отметьте 'Add python.exe to PATH') и запустите start.bat снова." Red
            exit 1
        }
    }
    Say "Python найден: $($py -join ' ')" Green
    Step "2/6 Виртуальное окружение"
    $createArgs = @()
    if ($py.Count -gt 1) { $createArgs += $py[1..($py.Count - 1)] }
    $createArgs += @("-m", "venv", ".venv")
    & $py[0] @createArgs
    if ($LASTEXITCODE -ne 0) { Say "Не удалось создать .venv" Red; exit 1 }
} else {
    Step "1/6 Python"
    Say "Окружение .venv уже есть." Green
    Step "2/6 Виртуальное окружение"
}

# Пакеты ставятся заново, только если requirements.txt изменился.
$reqFile = Join-Path $root "requirements.txt"
$reqStamp = Join-Path $root ".venv\.requirements.sha256"
$reqHash = (Get-FileHash $reqFile -Algorithm SHA256).Hash
if ((Test-Path $reqStamp) -and ((Get-Content $reqStamp -Raw).Trim() -eq $reqHash)) {
    Say "Пакеты Python уже установлены." Green
} else {
    Say "Устанавливаю пакеты Python из requirements.txt (ниже виден ход загрузки)..." White
    & $venvPython -m pip install --disable-pip-version-check -r $reqFile
    if ($LASTEXITCODE -ne 0) { Say "Не удалось установить пакеты." Red; exit 1 }
    Set-Content -Path $reqStamp -Value $reqHash
    Say "Пакеты установлены." Green
}

# ---------------------------------------------------------------- 4. .env
Step "3/6 Файл настроек .env"
$envPath = Join-Path $root ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $root ".env.example") $envPath
    Say "Создан .env из .env.example. Откройте его, если хотите сменить модель или адрес Ollama." Yellow
} else {
    Say ".env найден." Green
}
$cfg = Read-EnvFile $envPath

if (-not $NoChecks) {
    # ------------------------------------------------------------ 3. внешние программы
    Step "4/6 Внешние программы для файлов"
    Say "Нужны только для картинок, PDF и видео; для текста и .docx не требуются." DarkGray
    $stateFile = Join-Path $root ".venv\.tools-declined"
    $declined = if (Test-Path $stateFile) { @(Get-Content $stateFile) } else { @() }
    # что это и когда нужно: для каждой программы коротко, чтобы человек сам решил, ставить ли её
    $tools = @(
        @{ cmd = "exiftool"; id = "OliverBetz.ExifTool"; title = "ExifTool";
           what = "читает и стирает скрытые данные в файлах (автор, программа, геометки, следы ИИ)";
           need = "если очищаете картинки, PDF или документы Office как файлы" },
        @{ cmd = "qpdf"; id = "QPDF.QPDF"; title = "QPDF";
           what = "перепаковывает PDF-файлы и убирает из них служебные данные";
           need = "если очищаете PDF как файл" },
        @{ cmd = "ffmpeg"; id = "Gyan.FFmpeg"; title = "FFmpeg";
           what = "работает с видео и аудио, здесь стирает их метаданные";
           need = "если очищаете видео или аудио" },
        @{ cmd = "gswin64c"; alt = @("gswin32c", "gs"); id = $null; title = "Ghostscript";
           what = "обрабатывает PDF (пересборка страниц, удаление скрытого содержимого)";
           need = "если очищаете PDF как файл, дополнительно к QPDF";
           url = "https://ghostscript.com/releases/gsdnld.html" },
        @{ cmd = "c2patool"; id = $null; title = "c2patool";
           what = "проверяет и убирает подписи происхождения C2PA (Content Credentials) в картинках и видео";
           need = "если очищаете картинки или видео, сделанные ИИ-сервисами";
           url = "https://github.com/contentauth/c2pa-rs/releases (положите c2patool.exe в папку из PATH)" }
    )
    function Tool-Present($t) {
        if (Has-Command $t.cmd) { return $true }
        foreach ($a in @($t.alt)) { if ($a -and (Has-Command $a)) { return $true } }
        return $false
    }
    $missing = @($tools | Where-Object { -not (Tool-Present $_) })
    if ($missing.Count -eq 0) {
        Say "Все внешние программы найдены." Green
    } else {
        Say "Не найдены (для текста и .docx они не нужны, ничего ставить не обязательно):" Yellow
        foreach ($t in $missing) {
            Say "  * $($t.title): $($t.what)." White
            Say "      Пригодится, $($t.need)." DarkGray
        }
    }
    $auto = @($missing | Where-Object { $_.id -and ($InstallTools -or ($declined -notcontains $_.cmd)) })
    if ($auto.Count -gt 0) {
        $choice = Ask3 "Установить через winget: $(($auto | ForEach-Object { $_.title }) -join ', ')?"
        if ($choice -eq "yes") {
            foreach ($t in $auto) {
                if (-not (Winget-Install $t.id $t.title)) { Say "Не удалось установить $($t.title), поставьте вручную." Yellow }
            }
            if (Test-Path $stateFile) { Remove-Item $stateFile -ErrorAction SilentlyContinue }
        } elseif ($choice -eq "never") {
            Set-Content -Path $stateFile -Value ($auto | ForEach-Object { $_.cmd })
            Say "Хорошо, больше не спрашиваю. Передумаете: start.bat -InstallTools" DarkGray
        } else {
            Say "Пропущено, при следующем запуске спрошу снова. Установить сейчас: start.bat -InstallTools" DarkGray
        }
    } elseif ($missing | Where-Object { $_.id }) {
        Say "Раньше вы отказались от установки. Если передумали, запустите: start.bat -InstallTools" DarkGray
    }
    foreach ($t in @($missing | Where-Object { -not $_.id })) {
        Say "  $($t.title) в winget нет, при необходимости скачайте вручную: $($t.url)" Yellow
    }

    # ------------------------------------------------------------ 5. Ollama
    Step "5/6 Ollama и модель"
    if ($cfg["WATERMARKS_REWRITE_BACKEND"] -ne "ollama") {
        Say "В .env не выбран бэкенд ollama: проверка пропущена." DarkGray
    } else {
        $base = if ($cfg["WATERMARKS_REWRITE_BASE_URL"]) { $cfg["WATERMARKS_REWRITE_BASE_URL"].TrimEnd("/") } else { "http://127.0.0.1:11434" }
        $model = $cfg["WATERMARKS_REWRITE_MODEL"]
        $tags = $null
        try { $tags = Invoke-RestMethod -Uri "$base/api/tags" -TimeoutSec 4 } catch {}
        if ($null -eq $tags) {
            if (-not (Has-Command "ollama")) {
                Say "Ollama не установлена (она нужна для перефразирования и замены слов)." Yellow
                if (Ask "Установить Ollama через winget?") {
                    if (Winget-Install "Ollama.Ollama" "Ollama") { Say "Запустите приложение Ollama и снова start.bat." Yellow }
                } else {
                    Say "Установить можно позже с https://ollama.com/download" DarkGray
                }
            } else {
                Say "Ollama установлена, но не отвечает по адресу $base. Запустите приложение Ollama (или в командной строке: ollama serve)." Yellow
            }
            Say "Без Ollama работает только слой A (невидимые символы)." DarkGray
        } else {
            Say "Ollama отвечает: $base" Green
            $names = @($tags.models | ForEach-Object { $_.name })
            if ($model -and ($names -notcontains $model) -and ($names -notcontains "${model}:latest")) {
                Say "Модель '$model' не найдена в Ollama." Yellow
                Say "Скачайте её сами, в командной строке введите:" White
                Say "    ollama pull $model" Cyan
                $profilesPath = Join-Path $root "config\model_profiles.json"
                if (Test-Path $profilesPath) {
                    try {
                        $profiles = (Get-Content $profilesPath -Raw -Encoding UTF8 | ConvertFrom-Json).profiles
                        Say "Проверенные модели (подробности в README):" DarkGray
                        foreach ($p in $profiles) { Say "    $($p.model)   $($p.title)" DarkGray }
                    } catch {}
                }
            } elseif ($model) {
                Say "Модель '$model' скачана." Green
            }
        }
    }
}

# ---------------------------------------------------------------- 6. запуск
Step "6/6 Запуск сервиса"
foreach ($k in $cfg.Keys) { Set-Item -Path "Env:$k" -Value $cfg[$k] }
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

try {
    $h = Invoke-RestMethod -Uri "http://${ListenHost}:${Port}/health" -TimeoutSec 2
    if ($h.ok) { Say "Сервис уже запущен на порту $Port. Закройте то окно или выберите другой порт: start.bat -Port 9000" Yellow; exit 0 }
} catch {}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Say "Порт $Port занят другой программой. Запустите с другим портом: start.bat -Port 9000" Red
    exit 1
}

Say "Сервис запускается. Журнал работы ниже." Green
Say "Теперь можно просить Claude очистить текст (скилл remove-watermarks-ru)." Green
Write-Host ""
& $venvPython service/scripts/server.py --host $ListenHost --port $Port
