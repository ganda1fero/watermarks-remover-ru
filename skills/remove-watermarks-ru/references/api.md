# Запросы к сервису (шпаргалка)

Все эндпоинты принимают JSON; файл передаётся в base64. Сначала собери тело запроса в файл, затем
отправляй `curl --data-binary @body.json`: так не мешают ограничения длины командной строки.

## Разбор файла

`python <skill>/scripts/doc_text.py extract|rebuild|check` (подробности: `--help`). Поддерживаются .txt, .md, .docx, .pdf.
Сервис получает только `work/text.txt`; файл целиком в `/clean` отправляется лишь в конце для .docx (слой A по контейнеру).

## Подготовка тела

Windows (PowerShell):

```powershell
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("part1.txt"))
@{ file = $b64; name = "part1.txt"; options = @{ report_edits = $true } } | ConvertTo-Json -Compress |
  Set-Content -Encoding utf8 body.json
```

Linux/macOS/Git Bash:

```bash
printf '{"file":"%s","name":"part1.txt","options":{"report_edits":true}}' "$(base64 -w0 < part1.txt)" > body.json
```

## Вызовы

```bash
WM="${WATERMARKS_SERVICE_URL:-http://127.0.0.1:8765}"
curl -s "$WM/health"
curl -s -X POST "$WM/estimate" -H "Content-Type: application/json" --data-binary @body.json
curl -s -X POST "$WM/clean"    -H "Content-Type: application/json" --data-binary @body.json   # перефразирование + замена слов
curl -s -X POST "$WM/layer-a"  -H "Content-Type: application/json" --data-binary @body.json   # только невидимые символы
curl -s -X POST "$WM/unload"   -H "Content-Type: application/json" -d '{}'                           # освободить память модели
curl -s -X POST "$WM/patch"    -H "Content-Type: application/json" --data-binary @patch.json  # точечная правка слов
```

Если у сервиса задан ключ: добавь `-H "Authorization: Bearer $WATERMARKS_SERVER_API_KEY"` к каждому запросу.
Не используй `-L` (редирект мог бы переслать ключ другому хосту).

## Параметры `options` у `/clean`, `/estimate`, `/layer-a`

| Параметр | Смысл |
|---|---|
| `report_edits` | вернуть список замен (слово, смещение, контекст) в `report.layer_b.steps[].swap.edits` |
| `keep_model` | (необязательно) держать модель 10 минут после запроса; освободить сразу: `POST /unload`. Обычно не нужно: хватает `WATERMARKS_REWRITE_KEEP_ALIVE` |
| `rewrite_model` | модель Ollama на этот запрос вместо `WATERMARKS_REWRITE_MODEL` |
| `meaning_check` | включить (true) или выключить (false) проверку смысла замен на этот запрос |
| `strategy` | стратегия слоя B: `refresh@0.8` (освежение), `swap@0.2`, `paraphrase@0.8` или сочетания, например `refresh@0.8,swap@0.2` (по умолчанию); готовые наборы приходят в `modes` ответа `/estimate` |
| `strip_keep_marks` | только `/clean` для `.docx`: убрать «Не отрывать от следующего», «Не разрывать абзац» и «С новой страницы» (чёрные квадратики на полях в Word); по умолчанию `true`, «С новой страницы» заменяется явным разрывом страницы. В `report.actions` строка `drop paragraph keep marks: …` |

## Ответ `/estimate`

`model` (модель, которую сервис реально вызовет), `model_source` (`env` или `options.rewrite_model`),
`meaning_check`, `words`, `basis` (оценка по реальным прогонам или по типовым значениям), `expected_seconds` и
`expected_range_seconds` для текущей настройки сервиса, `chunk_plan` (`chunk_words`, `chunks`, `model_stays_loaded_between_chunks`),
`modes[]` (каждый режим слоя B: `id`, `title`, `note`, `options`, `expected_seconds`, `relative_time`,
`available`, `is_current`) и `alternatives[]` (другие модели: `model`, `installed`, `expected_seconds`,
`relative_time`, `quality`). Значения `options` из `modes` передаются в `/clean` как есть.

## Ответ `/clean` (текст)

`cleaned` (base64), `report.layer_b.steps[]` (в шаге `swap`: `target`, `accepted`, `edits`, `warnings`,
`offsets_valid`), `report.guards` (потерянные и новые числа, число предложений, доля кириллицы),
`report.model` (`name`, `meaning_check`: что реально использовано), `report.timing` (`layer_b_seconds`, `seconds_per_100_words`), `report.warnings`.

## Тело `/patch`

```json
{"file": "<base64 текста части>", "name": "part1.txt",
 "edits": [{"offset": 1234, "old": "новое слово", "new": "исходное слово"}]}
```

Ответ: `cleaned` (base64), `applied`, `skipped` (с причиной). Заменяются только целые слова ровно по смещению.
Раскодировать ответ: `[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.cleaned))` или `base64 -d`.
