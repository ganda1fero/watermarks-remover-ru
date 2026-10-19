# Эксперименты с заменой слов (swap)

Здесь лежат скрипты и данные, на которых выбраны настройки тактики `swap`. Папка нужна для
воспроизводимости; для обычной работы сервиса она не требуется.

## Файлы

| Файл | Что это |
|---|---|
| `test_text.txt`, `test_text_web.txt` | два тестовых текста (космос и веб-разработка, по ~2000 токенов) |
| `bench_swap.py` | прогон одной модели на тестовых текстах с заданным вариантом настроек |
| `swap_runs.jsonl` | журнал всех прогонов (статистика: сколько принято, отказов, время) |
| `swap_pairs/` | все выполненные замены каждого прогона (слово → слово с контекстом) |
| `swap_samples/` | случайные выборки по 30 замен, которые оценивались вручную |
| `swap_labels.json` | ручная разметка каждой замены (G годная, F неверная форма, N несуществующее слово, S потеря смысла) |
| `swap_grades.json` | те же оценки в виде счётчиков по прогонам |
| `swap_experiments.md` | полная таблица всех прогонов (генерируется `make_table.py`) |
| `swap_best_variants.md` | сводка лучших вариантов (генерируется `best_table.py`) |
| `validator_eval2.py` | проверка, насколько хорошо запросы «то же значение?» отделяют годные замены от плохих |
| `ab_score.py` | оценка всех замен прогонов проверяющим (для сравнения вариантов без ручной разметки) |
| `reply_len.py` | замер длины ответов моделей (чтобы выбрать лимит токенов) |
| `embed_eval.py` | качество проверки смысла (AUC): эмбеддинги или запрос к модели; пишет `verify_scores_*.json` |
| `pick_thresholds.py` | подбор порога проверки смысла для каждой модели по этим оценкам (`verify_thresholds.json`) |
| `Modelfile.saiga-fixed` | как исправить модель Saiga, у которой в Ollama сломан шаблон чата |

## Как запускать

Нужны запущенный Ollama и скачанная модель (`ollama pull yandex/YandexGPT-5-Lite-8B-instruct-GGUF`).
Из корня репозитория:

```bash
# один вариант на обоих текстах: имя модели и список конфигураций (ru_n5, ru_n10, ...)
python experiments/bench_swap.py "yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest" ru_n10

# то же с проверкой смысла (так работает рабочий режим)
WATERMARKS_SWAP_VERIFY=1 BENCH_VER=my_run python experiments/bench_swap.py "yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest" ru_n10

# пересобрать таблицы
python experiments/make_table.py
python experiments/best_table.py
```

Результаты и образцы пишутся в эту же папку (`swap_runs.jsonl`, `swap_samples/`, `swap_pairs/`,
`swap_out/`). Метки годности ставятся вручную в `swap_grades.json` / `swap_labels.json`.

## Главные выводы

* Лучшая модель для русского из проверенных: YandexGPT-5-Lite 8B (qwen2.5, gemma3:4b, Saiga-Mistral и Saiga-YandexGPT хуже).
* Словарь и проверка формы (`pymorphy3`) убирают выдуманные слова и ошибки согласования.
* Проверка смысла вторым запросом поднимает долю годных замен примерно с 58% до 74%.
* Размер окна 5 и 10 слов дают одинаковое качество; JSON-ответ, нумерация слов и правка соседей
  не помогли.

## Подобрать порог для новой модели

```bash
python experiments/embed_eval.py paraphrase-multilingual "<модель>"
python experiments/pick_thresholds.py
```

Затем впишите `verify_min` в `config/model_profiles.json`.
