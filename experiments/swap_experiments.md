# Эксперименты: пословный swap (окно ±n слов)

Вход — исходный текст (без перефразирования), цель — 20% слов-кандидатов, температура 0,3, num_ctx 4096.
Тексты: space (космос, 1958 токенов), web (веб-разработка).

**Версии кода:** v1 — без правил для дат/чисел и без словаря; v2 — слова рядом с числами, месяцы и «год/лет»
не заменяются, замена проверяется по словарю pymorphy3 (слова из самого текста допускаются) и по
грамматической форме. Кандидатов в v2 меньше, поэтому «цель» (20% от них) не сопоставима с v1 напрямую.

Колонки: **принято/цель** — выполнение квоты; **отказ** — модель ответила NULL; **негодн.** — ответ отбракован
по формату (латиница, не одно слово); **слов.** / **форма** — отбраковано словарём / проверкой формы (только v2);
**вызовов** — число запросов; **~ток.** — макс. оценка размера промпта; **суфф.*** — доля замен с тем же
окончанием (последние 2 буквы), грубый признак формы;
**оценка вручную** на 15 случайных заменах: ✓ годная замена / Ф неверная форма / Н несуществующее слово / С потеря смысла.

| Вер. | Модель | Текст | Конфиг | принято/цель | отказ | негодн. | слов. | форма | вызовов | сек | ~ток. | суфф.* | ✓ | Ф | Н | С |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ab_hl | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU+HL, n=10 | 73/73 | 6 | 3 | 3 | 6 | 91 | 34.9 | 294 | 0.51 |  |  |  |  |
| ab_hl | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU+HL, n=10 | 64/64 | 4 | 3 | 1 | 4 | 76 | 31.2 | 287 | 0.38 |  |  |  |  |
| ab_json | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU-JSON, n=10 | 73/73 | 0 | 4 | 5 | 5 | 87 | 57.7 | 255 | 0.45 |  |  |  |  |
| ab_json | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU-JSON, n=10 | 64/64 | 30 | 1 | 3 | 2 | 100 | 57.4 | 253 | 0.48 |  |  |  |  |
| ab_plain2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 | 73/73 | 2 | 6 | 2 | 6 | 89 | 38.8 | 249 | 0.41 |  |  |  |  |
| ab_plain2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 | 64/64 | 2 | 9 | 2 | 6 | 83 | 35.9 | 247 | 0.45 |  |  |  |  |
| ab_plain3 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 | 73/73 | 3 | 7 | 4 | 7 | 94 | 36.4 | 250 | 0.45 |  |  |  |  |
| ab_plain3 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 | 64/64 | 1 | 6 | 3 | 5 | 79 | 31.2 | 247 | 0.44 |  |  |  |  |
| g_final | gemma3:4b | space | RU, n=10 | 67/73 | 0 | 25 | 9 | 16 | 183 | 83.4 | 249 | 0.49 | 20 | 0 | 0 | 10 |
| g_final | gemma3:4b | web | RU, n=10 | 57/64 | 0 | 33 | 7 | 24 | 157 | 64.0 | 248 | 0.54 | 17 | 0 | 0 | 13 |
| g_plain | gemma3:4b | space | RU, n=10 | 73/73 | 0 | 14 | 5 | 14 | 106 | 30.6 | 249 | 0.41 | 18 | 0 | 0 | 12 |
| g_plain | gemma3:4b | web | RU, n=10 | 62/64 | 0 | 25 | 6 | 18 | 111 | 32.8 | 247 | 0.5 | 14 | 0 | 0 | 16 |
| sy_final | OxW/Saiga_YandexGPT_8B:q4_K_M | space | RU, n=10 | 0/73 | 4 | 190 | 7 | 24 | 364 | 321.5 | 251 | 0.0 |  |  |  |  |
| sy_final | OxW/Saiga_YandexGPT_8B:q4_K_M | space | RU, n=10 | 0/73 | 7 | 193 | 3 | 18 | 364 | 317.5 | 251 | 0.0 |  |  |  |  |
| sy_final | OxW/Saiga_YandexGPT_8B:q4_K_M | web | RU, n=10 | 0/64 | 25 | 157 | 0 | 14 | 320 | 259.4 | 248 | 0.0 |  |  |  |  |
| sy_final | OxW/Saiga_YandexGPT_8B:q4_K_M | web | RU, n=10 | 0/64 | 24 | 152 | 3 | 19 | 320 | 257.5 | 248 | 0.0 |  |  |  |  |
| sy_plain | OxW/Saiga_YandexGPT_8B:q4_K_M | space | RU, n=10 | 67/73 | 5 | 112 | 2 | 16 | 202 | 178.7 | 250 | 0.51 | 16 | 0 | 0 | 14 |
| sy_plain | OxW/Saiga_YandexGPT_8B:q4_K_M | space | RU, n=10 | 70/73 | 1 | 87 | 2 | 8 | 168 | 179.7 | 252 | 0.36 | 16 | 0 | 0 | 14 |
| sy_plain | OxW/Saiga_YandexGPT_8B:q4_K_M | web | RU, n=10 | 60/64 | 12 | 81 | 1 | 11 | 165 | 141.6 | 247 | 0.53 | 17 | 0 | 0 | 13 |
| sy_plain | OxW/Saiga_YandexGPT_8B:q4_K_M | web | RU, n=10 | 60/64 | 13 | 63 | 2 | 10 | 148 | 117.1 | 248 | 0.43 | 17 | 0 | 0 | 13 |
| v1 | qwen2.5:14b | space | RU, n=5 | 46/83 | 294 | 16 | – | – | 356 | 504.1 | 218 | 0.5 | 8 | 2 | 1 | 4 |
| v1 | qwen2.5:14b | space | RU, n=10 | 61/83 | 215 | 16 | – | – | 292 | 417.7 | 253 | 0.49 | 7 | 4 | 2 | 2 |
| v1 | qwen2.5:14b | space | RU-short, n=10 | 79/83 | 97 | 16 | – | – | 192 | 482.8 | 128 | 0.43 | 5 | 2 | 0 | 8 |
| v1 | qwen2.5:14b | space | EN, n=10 | 56/83 | 242 | 26 | – | – | 324 | 581.5 | 252 | 0.57 | 8 | 0 | 1 | 6 |
| v1 | qwen2.5:14b | web | RU, n=5 | 40/73 | 279 | 14 | – | – | 333 | 445.8 | 220 | 0.47 | 7 | 3 | 0 | 5 |
| v1 | qwen2.5:14b | web | RU, n=10 | 48/73 | 259 | 12 | – | – | 319 | 432.4 | 248 | 0.65 | 6 | 2 | 0 | 7 |
| v1 | qwen2.5:14b | web | RU-short, n=10 | 69/73 | 115 | 14 | – | – | 198 | 392.1 | 124 | 0.67 | 8 | 2 | 1 | 4 |
| v1 | qwen2.5:14b | web | EN, n=10 | 35/73 | 296 | 15 | – | – | 346 | 415.8 | 247 | 0.6 | 8 | 2 | 1 | 4 |
| v1 | qwen2.5:7b | space | RU, n=5 | 83/83 | 3 | 22 | – | – | 108 | 43.4 | 219 | 0.3 | 6 | 1 | 1 | 7 |
| v1 | qwen2.5:7b | space | RU, n=10 | 83/83 | 5 | 20 | – | – | 108 | 47.4 | 254 | 0.3 | 5 | 4 | 2 | 4 |
| v1 | qwen2.5:7b | space | RU, n=15 | 83/83 | 9 | 17 | – | – | 109 | 51.6 | 288 | 0.39 | 6 | 0 | 1 | 8 |
| v1 | qwen2.5:7b | space | RU-short, n=10 | 83/83 | 9 | 54 | – | – | 146 | 77.0 | 129 | 0.28 | 7 | 3 | 2 | 3 |
| v1 | qwen2.5:7b | space | EN, n=10 | 81/83 | 19 | 45 | – | – | 145 | 62.1 | 250 | 0.37 | 4 | 5 | 1 | 5 |
| v1 | qwen2.5:7b | web | RU, n=5 | 73/73 | 3 | 14 | – | – | 90 | 42.0 | 219 | 0.36 | 8 | 2 | 0 | 5 |
| v1 | qwen2.5:7b | web | RU, n=10 | 73/73 | 2 | 11 | – | – | 86 | 43.6 | 250 | 0.27 | 8 | 4 | 0 | 3 |
| v1 | qwen2.5:7b | web | RU, n=15 | 73/73 | 3 | 12 | – | – | 88 | 41.8 | 275 | 0.27 | 5 | 4 | 0 | 6 |
| v1 | qwen2.5:7b | web | RU-short, n=10 | 72/73 | 16 | 45 | – | – | 133 | 65.9 | 124 | 0.32 | 9 | 3 | 0 | 3 |
| v1 | qwen2.5:7b | web | EN, n=10 | 73/73 | 9 | 27 | – | – | 109 | 55.8 | 247 | 0.3 | 4 | 5 | 0 | 6 |
| v2 | gemma3:4b | space | RU, n=5 | 73/73 | 0 | 22 | 9 | 12 | 116 | 28.2 | 216 | 0.41 | 10 | 2 | 0 | 18 |
| v2 | gemma3:4b | space | RU, n=10 | 73/73 | 0 | 14 | 7 | 14 | 108 | 29.1 | 249 | 0.36 | 16 | 1 | 0 | 13 |
| v2 | gemma3:4b | web | RU, n=5 | 64/64 | 0 | 22 | 6 | 9 | 101 | 26.1 | 216 | 0.52 | 11 | 1 | 1 | 17 |
| v2 | gemma3:4b | web | RU, n=10 | 64/64 | 0 | 19 | 3 | 13 | 99 | 25.7 | 247 | 0.44 | 14 | 1 | 0 | 15 |
| v2 | qwen2.5:7b | space | RU, n=3 | 72/73 | 2 | 24 | 5 | 22 | 125 | 48.8 | 202 | 0.51 | 4 | 1 | 0 | 10 |
| v2 | qwen2.5:7b | space | RU, n=10 | 73/73 | 3 | 20 | 11 | 11 | 118 | 49.7 | 249 | 0.44 | 7 | 0 | 0 | 8 |
| v2 | qwen2.5:7b | space | RU, n=20 | 72/73 | 4 | 20 | 7 | 22 | 125 | 58.3 | 316 | 0.46 | 7 | 1 | 0 | 7 |
| v2 | qwen2.5:7b | space | RU, n=30 | 73/73 | 6 | 17 | 8 | 22 | 126 | 57.8 | 387 | 0.48 | 6 | 1 | 0 | 8 |
| v2 | qwen2.5:7b | web | RU, n=3 | 63/64 | 1 | 16 | 10 | 11 | 101 | 37.9 | 201 | 0.46 | 6 | 1 | 0 | 8 |
| v2 | qwen2.5:7b | web | RU, n=10 | 63/64 | 5 | 11 | 7 | 13 | 99 | 41.0 | 246 | 0.33 | 5 | 2 | 0 | 8 |
| v2 | qwen2.5:7b | web | RU, n=20 | 63/64 | 6 | 15 | 6 | 17 | 107 | 45.3 | 309 | 0.37 | 3 | 2 | 0 | 10 |
| v2 | qwen2.5:7b | web | RU, n=30 | 62/64 | 5 | 24 | 11 | 26 | 128 | 68.0 | 365 | 0.45 | 7 | 0 | 0 | 8 |
| v2 | saiga-mistral-fixed | space | RU, n=5 | 49/73 | 0 | 67 | 120 | 33 | 269 | 128.8 | 219 | 0.59 | 8 | 2 | 0 | 20 |
| v2 | saiga-mistral-fixed | space | RU, n=10 | 49/73 | 0 | 81 | 136 | 30 | 296 | 158.0 | 251 | 0.47 | 5 | 2 | 0 | 23 |
| v2 | saiga-mistral-fixed | web | RU, n=5 | 46/64 | 0 | 74 | 89 | 35 | 245 | 1023.1 | 219 | 0.52 |  |  |  |  |
| v2 | saiga-mistral-fixed | web | RU, n=10 | 39/64 | 0 | 81 | 115 | 39 | 274 | 146.8 | 249 | 0.49 |  |  |  |  |
| v2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=5 | 73/73 | 1 | 8 | 1 | 7 | 90 | 33.4 | 216 | 0.44 | 18 | 1 | 0 | 11 |
| v2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 | 73/73 | 2 | 7 | 2 | 7 | 91 | 36.3 | 250 | 0.44 | 19 | 1 | 0 | 10 |
| v2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=5 | 64/64 | 0 | 6 | 1 | 5 | 76 | 28.7 | 215 | 0.39 | 16 | 1 | 0 | 13 |
| v2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 | 64/64 | 2 | 6 | 4 | 4 | 80 | 31.5 | 247 | 0.44 | 16 | 1 | 0 | 13 |
| v3a | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=5 | 71/73 | 2 | 9 | 2 | 12 | 136 | 85.6 | 216 | 0.42 | 22 | 0 | 0 | 8 |
| v3a | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 | 72/73 | 2 | 10 | 4 | 10 | 136 | 95.7 | 251 | 0.5 | 24 | 0 | 0 | 6 |
| v3a | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=5 | 64/64 | 0 | 8 | 1 | 4 | 106 | 66.1 | 214 | 0.45 | 20 | 0 | 0 | 10 |
| v3a | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 | 64/64 | 0 | 9 | 2 | 8 | 115 | 74.3 | 248 | 0.52 | 22 | 0 | 0 | 8 |
| v3a_hi | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 | 59/73 | 4 | 18 | 8 | 15 | 232 | 142.8 | 251 | 0.53 | 23 | 0 | 0 | 7 |
| v3a_hi | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 | 56/64 | 1 | 17 | 7 | 10 | 198 | 128.9 | 246 | 0.57 | 21 | 0 | 0 | 9 |
| v3b2_agree_k1 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 +k=1 agree (ц/л/п/об 73/0/0/0, сл.2, сос.4) | 73/73 | 0 | 5 | 11 | 9 | 104 | 147.5 | 364 | 0.45 | 22 | 1 | 0 | 7 |
| v3b2_agree_k1 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 +k=1 agree (ц/л/п/об 64/0/0/0, сл.2, сос.3) | 64/64 | 0 | 4 | 7 | 5 | 85 | 107.2 | 356 | 0.44 |  |  |  |  |
| v3b2_agree_k2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 +k=2 agree (ц/л/п/об 71/1/0/0, сл.3, сос.21) | 73/73 | 0 | 4 | 17 | 4 | 121 | 243.8 | 379 | 0.5 |  |  |  |  |
| v3b2_agree_k2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 +k=2 agree (ц/л/п/об 64/0/0/0, сл.4, сос.40) | 64/64 | 0 | 8 | 8 | 5 | 129 | 234.3 | 376 | 0.42 |  |  |  |  |
| v3b2_free_k1 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 +k=1 free (ц/л/п/об 71/0/1/0, сл.1, сос.0) | 73/73 | 0 | 5 | 14 | 7 | 99 | 146.5 | 347 | 0.38 |  |  |  |  |
| v3b2_free_k1 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 +k=1 free (ц/л/п/об 64/0/0/0, сл.3, сос.2) | 64/64 | 0 | 11 | 8 | 5 | 93 | 122.2 | 350 | 0.44 |  |  |  |  |
| v3b2_free_k2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 +k=2 free (ц/л/п/об 61/6/0/0, сл.0, сос.10) | 73/73 | 0 | 4 | 11 | 8 | 100 | 206.9 | 363 | 0.5 |  |  |  |  |
| v3b2_free_k2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 +k=2 free (ц/л/п/об 54/3/1/0, сл.1, сос.23) | 63/64 | 0 | 7 | 14 | 3 | 106 | 191.8 | 360 | 0.37 |  |  |  |  |
| v3b_agree_k1 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 +k=1 agree (ц/л/п/об 29/3/1/0, сл.287, сос.4) | 37/73 | 0 | 2 | 0 | 23 | 349 | 271.3 | 374 | 0.39 |  |  |  |  |
| v3b_agree_k1 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 +k=1 agree (ц/л/п/об 22/0/0/0, сл.267, сос.4) | 22/64 | 0 | 1 | 0 | 14 | 308 | 242.2 | 372 | 0.41 |  |  |  |  |
| v3b_agree_k2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 +k=2 agree (ц/л/п/об 11/1/0/0, сл.334, сос.3) | 13/73 | 0 | 0 | 0 | 7 | 356 | 277.1 | 374 | 0.42 |  |  |  |  |
| v3b_agree_k2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 +k=2 agree (ц/л/п/об 6/0/0/0, сл.304, сос.1) | 6/64 | 0 | 0 | 1 | 1 | 313 | 231.2 | 372 | 0.33 |  |  |  |  |
| v3b_free_k1 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 +k=1 free (ц/л/п/об 24/5/2/1, сл.265, сос.0) | 41/73 | 0 | 2 | 0 | 24 | 323 | 243.3 | 368 | 0.39 |  |  |  |  |
| v3b_free_k1 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 +k=1 free (ц/л/п/об 19/4/1/1, сл.264, сос.0) | 32/64 | 0 | 1 | 0 | 19 | 309 | 229.3 | 366 | 0.4 |  |  |  |  |
| v3b_free_k2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 +k=2 free (ц/л/п/об 15/3/0/1, сл.321, сос.0) | 26/73 | 0 | 0 | 1 | 5 | 346 | 276.4 | 368 | 0.39 |  |  |  |  |
| v3b_free_k2 | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 +k=2 free (ц/л/п/об 16/3/1/0, сл.280, сос.1) | 24/64 | 0 | 0 | 1 | 1 | 303 | 210.1 | 368 | 0.29 |  |  |  |  |
| v3final | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | space | RU, n=10 | 73/73 | 3 | 14 | 4 | 13 | 158 | 91.8 | 254 | 0.53 | 22 | 0 | 0 | 8 |
| v3final | yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest | web | RU, n=10 | 63/64 | 1 | 13 | 3 | 7 | 119 | 73.9 | 247 | 0.41 | 21 | 0 | 0 | 9 |
