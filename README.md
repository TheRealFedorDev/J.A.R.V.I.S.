# J.A.R.V.I.S.

Just A Rather Very Intelligent System — локальный голосовой ассистент и панель управления
для Windows. Репозиторий ведёт **Алифанов Фёдор**, 10И, ФТЛ им. Капицы.

---

## Организация проекта (обязательно по ТЗ)

1) Mind Map
   Сервис называется draw.io, доступен по ссылке "https://app.diagrams.net/"
   Вы должны привязать свой гугл аккаунт (разрешите все действия)
   Проект лежит по этой ссылке, зайдите и отметьтесь:
   https://drive.google.com/file/d/1ZnfXRe5Vqi0kwSciTkn5sTqrwKubSVc-/view?usp=sharing

2) В разделе Projects / Проекты напишите любую задачу
   Ознакомьтесь с интерфейсом, поиграйся

MindMap для видения общей структуры проекта,
Project для непосредственных отметок выполнения задач

---

## Что внутри

| Слой | Что делает |
|---|---|
| Микрофон → STT | faster-whisper (`base` на CPU, `large-v3` на CUDA) |
| STT → LLM | Ollama `llama3.2:3b`, при недоступности — жёсткие правила |
| LLM → TTS | edge-tts, русские нейроголоса |
| Панель | FastAPI + WebSocket, минимальный HUD с орбом активности |
| Фоллбэк | Цепочки провайдеров: первый доступный отвечает, остальные страхуют |

---

## Требования

| Компонент | Версия | Обязателен |
|---|---|---|
| Python (основной `.venv`) | 3.11–3.13 | да |
| Python (`.venv-livekit`) | 3.11 ровно | для голоса |
| Ollama | любая свежая | для LLM |
| CUDA 12.x + cuDNN 9 | — | только для Whisper на GPU |
| Node.js | — | не нужен, фронтенд собран и лежит в репозитории |

---

## Установка

```powershell
git clone <url> J.A.R.V.I.S
cd J.A.R.V.I.S

# основное окружение: STT, TTS, веб-панель
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt

# голосовой агент LiveKit (живёт отдельно: собран под Python 3.11)
py -3.11 -m venv .venv-livekit
.venv-livekit\Scripts\python.exe -m pip install -r requirements-livekit.txt

# языковая модель
ollama pull llama3.2:3b

# конфиг
copy .env.example .env
```

Быстрый вариант: `setup.cmd` делает всё то же самое и в конце сам запускает
диагностику.

---

## Запуск

| Что | Команда |
|---|---|
| Веб-панель (по умолчанию) | `.venv\Scripts\python.exe main.py` → <http://localhost:8765> |
| Панель без браузера | `.venv\Scripts\python.exe main.py --no-browser` |
| Классический оверлей PyQt6 | `.venv\Scripts\python.exe main.py --overlay` |
| Голос (STT → LLM → TTS) | `run_voice.cmd` |
| Только диагностика | `.venv\Scripts\python.exe -m jarvis.doctor` |

> Веб-панель и голос конфликтуют за микрофон и порт `8077`. Запускай что-то одно.

---

## Диагностика

Самая полезная команда при переносе на новую машину:

```powershell
.venv\Scripts\python.exe -m jarvis.doctor
```

```
====================================================================
J.A.R.V.I.S. — диагностика бэкендов
====================================================================
Железо: CPU 12 ядер (без CUDA) · 15.4 ГБ RAM · Python 3.13.11
План STT: base / cpu / int8
Цепочка LLM: ollama

[РАСПОЗНАВАНИЕ]
    ok   faster-whisper          12152 мс  base / cpu / int8
[СИНТЕЗ]
    ok   edge                     6489 мс  ru-RU-SvetlanaNeural · 13248 Б
[ЯЗЫКОВАЯ МОДЕЛЬ]
    ok   ollama                    698 мс  llama3.2:3b · Ollama
```

Код возврата `0` — все обязательные проверки прошли. Удобно вешать на запуск
перед демонстрацией.

```powershell
python -m jarvis.doctor --deep          # плюс реальное распознавание фрагмента
python -m jarvis.doctor --json          # машинный вывод
python -m jarvis.doctor --probe llm     # только один раздел
```

То же самое в браузере: <http://localhost:8765/api/backends>.

---

## Перенос на лабораторный ноутбук

Ниже — ровно тот порядок, который проверен на `Ryzen 5 7430U`. На машине с
видеокартой меняется ровно одна строка в `.env`, а остальное подхватится само.

1. Склонировать репозиторий, создать оба окружения, поставить Ollama и
   скачать модель — см. «Установка».
2. `copy .env.example .env`.
3. Запустить `python -m jarvis.doctor` и посмотреть на строку `Железо:`.
4. Если написано `CUDA: нет` — настраивать нечего, система уже выбрала CPU.
5. Если CUDA есть, но Whisper всё равно считает на CPU — читать раздел
   «Железо» ниже.

Что **не** нужно делать: править код. Раньше переезд требовал замены
`WHISPER_DEVICE=cpu` вручную в четырёх местах; теперь достаточно `auto`.

### Железо: что выбирается автоматически

| Класс машины | Модель Whisper | Устройство | compute_type |
|---|---|---|---|
| Без CUDA | `base` | `cpu` | `int8` |
| CUDA, <4 ГБ VRAM | `small` | `cuda` | `float16` |
| CUDA, 4–6 ГБ VRAM | `medium` | `cuda` | `float16` |
| CUDA, ≥7 ГБ VRAM | `large-v3` | `cuda` | `float16` |

Любую ячейку можно зафиксировать руками в `.env` — явное значение всегда
выигрывает у авто. Пустое значение или `auto` означает «выбери сам».

### Если CUDA есть, но не используется

Три причины по убыванию частоты:

1. **Не хватает cuDNN.** ctranslate2 требует CUDA 12.x **и** cuDNN 9.x.
   Проверка: `python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"`
   — должен быть не ноль.
2. **Нет `nvidia-smi` в PATH.** Без него автоопределение не знает VRAM и
   берёт безопасный `small`.
3. **В `.env` стоит явное `cpu`.** Убрать строку или заменить на `auto` —
   иначе авто молча перебивается ручным значением.

### Секреты

`.env` в репозитории отслеживается Git. Если в него попадали ключи, их нужно
ротировать и вынести в локальный `.env`, который не коммитится.

---

## Провайдеры и фоллбэк

Каждая подсистема (STT, TTS, LLM) читает свою цепочку из `.env`. Отвечает
**первый доступный**, остальные страхуют — система деградирует, а не молчит.

```ini
STT_CHAIN=faster-whisper
TTS_CHAIN=edge
LLM_CHAIN=ollama

# облако как запасной вариант:
LLM_CHAIN=ollama,openai:gpt-4o-mini
# edge требует интернет, pyttsx3 работает полностью офлайн:
TTS_CHAIN=edge,pyttsx3
```

Аргумент после двоеточия — имя модели: `openai:gpt-4o-mini`.

Ключи облачных провайдеров читаются из переменных окружения:

| Провайдер | Переменная |
|---|---|
| openai | `OPENAI_API_KEY` |
| openrouter | `OPENROUTER_API_KEY` |
| groq | `GROQ_API_KEY` |
| mistral | `MISTRAL_API_KEY` |
| cerebras | `CEREBRAS_API_KEY` |
| xai | `XAI_API_KEY` |
| github | `GITHUB_TOKEN` |
| hf | `HF_TOKEN` |
| deepgram | `DEEPGRAM_API_KEY` |
| elevenlabs | `ELEVENLABS_API_KEY` |

`python -m jarvis.doctor` покажет, что реально живо с этой машины: `401/403`
означает отвергнутый ключ или заблокированный регион, таймаут — недоступную
сеть.

---

## Ключевые переменные `.env`

| Переменная | Смысл |
|---|---|
| `VOICE_AGENT` | `livekit` \| `pipecat` \| `legacy` |
| `VOICE_ASR` | `faster-whisper` \| `vosk` |
| `WHISPER_MODEL` / `_DEVICE` / `_COMPUTE_TYPE` | `auto` или явное значение |
| `WHISPER_LANGUAGE` | `ru` |
| `VOICE_MIN_SILENCE` | сек тишины до конца фразы (LiveKit) |
| `LLM_MODEL` / `LLM_BASE_URL` | модель и адрес Ollama |
| `OLLAMA_KEEP_ALIVE` | `30m` убирает 12 с ожидания холодной загрузки |
| `LLM_MAX_TOKENS` / `VOICE_LLM_TEMPERATURE` | длина и строгость ответа |
| `GAZE_ENABLED` / `HAND_ENABLED` | камера выключена — быстрый старт |
| `STT_CHAIN` / `TTS_CHAIN` / `LLM_CHAIN` | цепочки провайдеров с фоллбэком |

Полный список с комментариями — в `.env.example`.

---

## Структура репозитория

```
jarvis/
  core/        конфиг, события, шина, service-база
    backends.py    железо, цепочки провайдеров, фоллбэк
  voice/       STT, LLM-агент, локальный voice API
  tts/         синтез, голоса, нормализация
  web/         FastAPI, роутер команд, состояние, статика
  hand/ gaze/ input/  pointer/  подсистемы управления
  doctor.py    диагностика: python -m jarvis.doctor
  launcher.py  точка входа: main.py
models/        обученные модели и offset'ы (не в Git, восстанавливаются)
ui/            исходники фронтенда
libs/          EyeTrax и внешние библиотеки (не в Git)
```

---

## Известные ограничения

* `libs/` не в Git — на новой машине EyeTrax нужно поставить отдельно
  (см. `requirements.txt`), иначе подсистема взгляда не запустится.
  На голос и веб-панель это не влияет.
* `edge-tts` требует интернета. Для полностью офлайн-озвучки ставь
  `TTS_CHAIN=edge,pyttsx3`.
* Web-панель и голос нельзя запускать одновременно: делят микрофон и порт
  `8077`.
* На CPU `small` и `medium` для Whisper медленнее реального времени —
  автоподбор специально ограничивается `base`.