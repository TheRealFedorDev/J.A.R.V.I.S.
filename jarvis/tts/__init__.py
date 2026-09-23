"""TTS (Text-to-Speech) подсистема J.A.R.V.I.С — полностью потоковая.

Синтез идёт стримом: edge-tts отдаёт mp3-чанки по мере генерации,
декодер превращает их в PCM в отдельном потоке, а плеер начинает
звучать до конца синтеза. `speak()` не блокирует вызывающий поток.

Голоса (женские, проверены в edge-tts 7.x):
  * EN — en-GB-SoniaNeural, RU — ru-RU-SvetlanaNeural (автовыбор по языку).

Использование:
    from jarvis.tts import TtsConfig, TextToSpeech
    tts = TextToSpeech(TtsConfig.from_env())
    tts.speak("Привет, я J.A.R.V.I.С.")   # не блокирует
    tts.wait()                            # дождаться окончания
    tts.stop()                            # прервать (barge-in)
"""

from jarvis.tts.normalize import is_russian, normalize_text
from jarvis.tts.player import StreamPlayer
from jarvis.tts.service import TtsConfig, TextToSpeech
from jarvis.tts.stream import (
    IncrementalMp3Decoder,
    decode_to_sink,
    edge_mp3_chunks,
)
from jarvis.tts.voices import DEFAULT_EN_VOICE, DEFAULT_RU_VOICE, select_voice

__all__ = [
    "TtsConfig",
    "TextToSpeech",
    "StreamPlayer",
    "IncrementalMp3Decoder",
    "edge_mp3_chunks",
    "decode_to_sink",
    "normalize_text",
    "is_russian",
    "select_voice",
    "DEFAULT_EN_VOICE",
    "DEFAULT_RU_VOICE",
]
