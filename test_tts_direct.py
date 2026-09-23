"""Ручная проверка живого edge-стрима (НЕ собирается pytest: функция main).

Запуск:  python test_tts_direct.py
Генерирует реальный синтез через edge-tts, декодирует потоково и играет.
"""

from __future__ import annotations

import asyncio
import time

from jarvis.tts import TtsConfig, TextToSpeech


def main() -> None:
    tts = TextToSpeech(TtsConfig.from_env())
    print(f"engine={tts._engine}")

    t0 = time.perf_counter()
    tts.speak("Тест потокового синтеза. Речь начинается до конца генерации.")
    print(f"speak() вернулся за {(time.perf_counter() - t0) * 1000:.1f} мс")
    tts.wait(timeout=60)
    print("--- RU готово ---")

    tts.speak_sync(
        "Streaming synthesis test. Audio starts before the sentence finishes.",
        timeout=60,
    )
    print("--- EN готово ---")
    tts.close()


async def _probe_first_chunk() -> None:
    """Сколько времени до первого mp3-чанка от Azure."""
    from jarvis.tts import edge_mp3_chunks

    t0 = time.perf_counter()
    async for chunk in edge_mp3_chunks("Привет", voice="ru-RU-SvetlanaNeural"):
        print(f"первый чанк: {(time.perf_counter() - t0) * 1000:.0f} мс, {len(chunk)} байт")
        break


if __name__ == "__main__":
    asyncio.run(_probe_first_chunk())
    main()
