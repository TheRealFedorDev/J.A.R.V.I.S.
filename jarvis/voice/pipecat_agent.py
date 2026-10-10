"""Голосовой ассистент на pipecat: STT → поиск/LLM (Ollama) → TTS, бардж-ин.

Конвейер:
    LocalAudioTransport.input → InterimSTT (stream partials) → WhisperSTT
        → EventBusBridge → SearchRouter → LLMUserAggregator → OLLamaLLM
        → ReplyTap → EdgeTTSService → LocalAudioTransport.output
        → LLMAssistantAggregator

* Silero VAD + turn strategies — конец речи / barge-in.
* InterimSTT — энергетический VAD + rolling partial (beam=1) → VoicePartialEvent
  во время речи (WhisperSTT сегментный и не даёт interim-кадров).
* EventBusBridge: final → VoicePartialEvent; команды → VoiceCommandEvent
  (и не уходят в LLM); разговорные фразы → LLM → женский edge-tts.
* SearchRouter: знаниевые запросы (погода/факты/…) → быстрый веб-поиск
  (<~1 с) → TTSSpeakFrame мимо LLM; иначе → LLM как раньше.
* ReplyTap: текст ответа LLM → BotReplyEvent (чат web-панели).
* Запускается в отдельном потоке с собственным asyncio-циклом (BaseService).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

from jarvis.core.backends import resolve_stt
from jarvis.core.config import VoiceConfig
from jarvis.core.events import (
    BotReplyEvent,
    EventBus,
    VoiceCommandEvent,
    VoicePartialEvent,
)
from jarvis.core.service import BaseService
from jarvis.voice.commands import parse_command
from jarvis.voice.tasks_rule import parse_task_command, run, HttpTaskAPI
from jarvis.voice.web_search import maybe_web_search, needs_web_search

log = logging.getLogger("jarvis.voice.pipecat")

DEFAULT_SYSTEM_PROMPT = (
    "Ты Berta — локальный голосовой ассистент на компьютере пользователя. "
    "Отвечай кратко, по-русски (или на языке речи собеседника), дружелюбно. "
    "Не выдумывай, что уже сделал действия; для кликов/скролла есть отдельные голосовые команды."
)


# ---------------------------------------------------------------------------
# Мост кадров pipecat → EventBus
# ---------------------------------------------------------------------------
def dispatch_transcription(
    bus: EventBus, text: str, *, final: bool
) -> bool:
    """Опубликовать события; True — фраза уходит в LLM (не команда)."""
    text = (text or "").strip()
    if not text:
        return False
    bus.publish(VoicePartialEvent(text=text, final=final))
    if not final:
        return True
    cmd = parse_command(text)
    if cmd is not None:
        bus.publish(
            VoiceCommandEvent(
                action=cmd.action, args=cmd.args, text=text
            )
        )
        log.info("голос(команда): %s | %s", text, cmd.action)
        return False
    log.debug("голос(речь→LLM): %s", text)
    return True


def _make_bridge_class():
    """Создать FrameProcessor-мост (ленивый импорт pipecat)."""
    from pipecat.frames.frames import (
        Frame,
        InterimTranscriptionFrame,
        TranscriptionFrame,
    )
    from pipecat.processors.frame_processor import (
        FrameDirection,
        FrameProcessor,
    )

    class EventBusBridge(FrameProcessor):
        def __init__(self, bus: EventBus) -> None:
            super().__init__(name="jarvis-eventbus-bridge")
            self._bus = bus

        async def process_frame(
            self, frame: Frame, direction: FrameDirection
        ) -> None:
            if isinstance(frame, InterimTranscriptionFrame):
                dispatch_transcription(
                    self._bus, frame.text, final=False
                )
                await self.push_frame(frame, direction)
            elif isinstance(frame, TranscriptionFrame):
                if dispatch_transcription(self._bus, frame.text, final=True):
                    await self.push_frame(frame, direction)
                # команда — не пробрасываем к LLM
            else:
                await self.push_frame(frame, direction)

    return EventBusBridge


def _make_interim_class():
    """Создать InterimSTT: rolling partial во время речи (ленивый импорт).

    Энергетический VAD (как legacy WhisperStreamDecoder) + beam=1 по общему
    faster-whisper модели WhisperSTT. Публикует VoicePartialEvent(final=False);
    финалы идут отдельно через сегментный WhisperSTT → EventBusBridge.
    """
    import numpy as np

    from jarvis.voice.stt import is_voiced, join_segments, to_float32
    from pipecat.frames.frames import (
        Frame,
        InputAudioRawFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import (
        FrameDirection,
        FrameProcessor,
    )

    class InterimSTT(FrameProcessor):
        def __init__(
            self,
            bus: EventBus,
            *,
            whisper_model: Any,
            language: str | None,
            sample_rate: int,
            partial_seconds: float,
            vad_threshold: float,
            silence_end: float,
            partial_max_seconds: float = 12.0,
            partial_work_budget: float = 24.0,
        ) -> None:
            super().__init__(name="jarvis-interim-stt")
            self._bus = bus
            self._model = whisper_model
            self._language = language
            self._sr = int(sample_rate)
            self._partial_every = float(partial_seconds)
            self._vad = float(vad_threshold)
            self._silence_end = float(silence_end)
            self._max_window = float(partial_max_seconds)
            self._work_budget = float(partial_work_budget)
            self._work = 0.0
            self._buf: list[np.ndarray] = []
            self._speech = False
            self._silence = 0.0
            self._speech_len = 0.0
            self._next_partial = 0.0
            self._last_partial = ""
            self._partial_task: asyncio.Task | None = None

        async def process_frame(
            self, frame: Frame, direction: FrameDirection
        ) -> None:
            if isinstance(frame, InputAudioRawFrame):
                self._feed(bytes(frame.audio), frame.sample_rate or self._sr)
            elif isinstance(
                frame,
                (
                    UserStartedSpeakingFrame,
                    VADUserStartedSpeakingFrame,
                ),
            ):
                if not self._speech:
                    self._start_utterance()
            elif isinstance(
                frame,
                (
                    UserStoppedSpeakingFrame,
                    VADUserStoppedSpeakingFrame,
                ),
            ):
                self._reset_utterance()
            await self.push_frame(frame, direction)

        # ------------------------------------------------------------------
        def _start_utterance(self) -> None:
            self._speech = True
            self._buf = []
            self._speech_len = 0.0
            self._silence = 0.0
            self._work = 0.0
            self._next_partial = self._partial_every
            self._last_partial = ""
            # плашка «СЛУШАЮ» сразу при начале речи
            try:
                self._bus.publish(
                    VoicePartialEvent(text="…", final=False)
                )
            except Exception:
                log.exception("interim: не удалось показать «слушаю»")

        def _reset_utterance(self) -> None:
            self._speech = False
            self._buf = []
            self._silence = 0.0
            self._speech_len = 0.0
            self._next_partial = 0.0
            self._last_partial = ""
            # не даём позднему partial перезаписать финальную плашку
            if self._partial_task is not None and not self._partial_task.done():
                self._partial_task.cancel()

        def _feed(self, data: bytes, sample_rate: int) -> None:
            if not data:
                return
            pcm = to_float32(data)
            if sample_rate and sample_rate != self._sr:
                # кратный декрим/интерп не делаем — кадры local transport 16 кГц
                pass
            voiced = is_voiced(pcm, self._vad)
            block = len(pcm) / float(self._sr or 16000)

            if voiced:
                if not self._speech:
                    self._start_utterance()
                self._buf.append(pcm)
                self._speech_len += block
                self._silence = 0.0
                if self._speech_len >= self._next_partial:
                    self._next_partial += self._partial_every
                    self._schedule_partial()
                return

            if not self._speech:
                return
            self._buf.append(pcm)
            self._silence += block
            if self._silence >= self._silence_end:
                self._reset_utterance()

        def _schedule_partial(self) -> None:
            if self._model is None or not self._buf:
                return
            if self._partial_task is not None and not self._partial_task.done():
                return
            # бюджет проверяем ДО склейки: concat растущего буфера сам по себе жжёт CPU
            sr = self._sr or 16000
            window = min(self._speech_len, self._max_window)
            if self._work + window > self._work_budget:
                return
            audio = np.concatenate(self._buf)
            # скользящее окно: не распознаём фразу целиком заново каждые N секунд
            max_samples = int(self._max_window * sr)
            if audio.size > max_samples:
                audio = audio[-max_samples:]
            if audio.size < int(sr * 0.3):
                return
            self._work += audio.size / float(sr)
            try:
                self._partial_task = self.create_task(
                    self._run_partial(audio.copy())
                )
            except Exception:
                log.exception("interim: не удалось запустить partial")

        async def _run_partial(self, audio: np.ndarray) -> None:
            try:
                text = await asyncio.to_thread(self._transcribe_fast, audio)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("interim: partial-распознавание упало")
                return
            # ход уже закончен — не перетирать финальную фразу
            if not self._speech:
                return
            if not text or text == self._last_partial:
                return
            self._last_partial = text
            try:
                self._bus.publish(
                    VoicePartialEvent(text=text, final=False)
                )
            except Exception:
                log.exception("interim: publish partial упал")

        def _transcribe_fast(self, audio: np.ndarray) -> str:
            if self._model is None:
                return ""
            segments, _info = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=1,
                best_of=1,
                condition_on_previous_text=False,
                vad_filter=False,
                without_timestamps=True,
                temperature=0.0,
            )
            return join_segments(segments)

    return InterimSTT


# ---------------------------------------------------------------------------
# Ход: финальная фраза → локальный LLM (стрим) по умолчанию, веб — исключение
# ---------------------------------------------------------------------------
def _make_search_router_class():
    """FrameProcessor: финальная фраза → ответ (LLM streaming / веб) → TTS.

    Позиция в конвейере: после EventBusBridge, до LLMUserAggregator.

    Политика хода (по требованию): обычный разговор идёт в локальный Ollama
    со стримингом; веб-поиск срабатывает только на явный запрос («найди»,
    «погугли», «в интернете») или на темы, где модель физически не знает
    ответа (погода, курсы) — иначе 3B-модель просто выдумывает числа.

    LLM-ход обслуживается здесь, а не LLMUserAggregator: агрегатор pipecat
    не отдавал ход (User stopped speaking -> strategy: None) и запрос к
    Ollama не уходил вовсе.

    Пока ответ готовится, кадры UserStoppedSpeaking/… НЕ уходят в
    LLMUserAggregator — иначе ход закрывается с пустой агрегацией.
    Barge-in (начало новой речи) отменяет незавершённый ответ.
    """
    from pipecat.frames.frames import (
        Frame,
        ProposedUserStoppedSpeakingFrame,
        TranscriptionFrame,
        TTSSpeakFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import (
        FrameDirection,
        FrameProcessor,
    )

    _STOP_FRAMES = (
        UserStoppedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
        ProposedUserStoppedSpeakingFrame,
    )

    class SearchRouter(FrameProcessor):
        def __init__(
            self,
            bus: EventBus,
            turn: dict[str, str],
            *,
            llm_model: str = "llama3.2:3b",
            system_prompt: str = "",
            max_tokens: int = 220,
            web_search_enabled: bool = True,
        ) -> None:
            super().__init__(name="jarvis-search-router")
            self._bus = bus
            self._turn = turn  # {"query": …} — общий с ReplyTap
            self._llm_model = llm_model
            self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
            self._max_tokens = int(max_tokens)
            self._web_search_enabled = bool(web_search_enabled)
            self._llm_lock = threading.Lock()
            self._search_task: asyncio.Task | None = None
            self._searching = False
            self._pending_stop: Frame | None = None

        # ------------------------------------------------------------------
        def _stream_llm_sync(self, text: str, on_chunk) -> str:
            """Синхронный стрим Ollama (вызывается в отдельном потоке)."""
            import ollama

            parts: list[str] = []
            with self._llm_lock:
                stream = ollama.chat(
                    model=self._llm_model,
                    messages=[
                        {"role": "system", "content": self._system_prompt},
                        {"role": "user", "content": text},
                    ],
                    stream=True,
                    options={
                        "num_predict": self._max_tokens,
                        "temperature": 0.4,
                    },
                )
                for part in stream:
                    chunk = (part.get("message") or {}).get("content") or ""
                    if chunk:
                        parts.append(chunk)
                        on_chunk(chunk)
            return "".join(parts).strip()

        async def process_frame(
            self, frame: Frame, direction: FrameDirection
        ) -> None:
            if isinstance(
                frame,
                (
                    UserStartedSpeakingFrame,
                    VADUserStartedSpeakingFrame,
                ),
            ):
                # бarge-in: сбросить поиск и буфер stop-кадра
                if self._search_task is not None and not self._search_task.done():
                    self._search_task.cancel()
                self._searching = False
                self._pending_stop = None
                await self.push_frame(frame, direction)
                return

            # пока идёт поиск — держим stop-кадры (иначе LLM-ход закроется рано)
            if self._searching and isinstance(frame, _STOP_FRAMES):
                self._pending_stop = frame
                return

            if isinstance(frame, TranscriptionFrame):
                text = (frame.text or "").strip()
                if text:
                    # команда задач — детерминированный ответ мимо LLM
                    cmd = parse_task_command(text)
                    if cmd is not None:
                        self._turn["query"] = text
                        self._start_task(self._task_and_speak(text))
                        return  # ответ задач сам закроет ход
                if text and self._web_search_enabled and needs_web_search(text):
                    self._turn["query"] = text
                    self._start_task(self._search_and_speak(text))
                    return  # веб-ответ сам закроет ход
                if text:
                    # обычный разговор → локальный LLM (стрим), не в агрегатор
                    self._turn["query"] = text
                    self._start_task(self._llm_and_speak(text))
                    return
                await self.push_frame(frame, direction)
                return

            await self.push_frame(frame, direction)

        def _start_task(self, coro) -> None:
            if self._search_task is not None and not self._search_task.done():
                self._search_task.cancel()
            self._searching = True
            self._pending_stop = None
            self._search_task = self.create_task(coro)

        async def _finish_turn(self) -> None:
            """Закрыть пользовательский ход после ответа (пустая агрегация → LLM не стартует)."""
            pending_stop = self._pending_stop
            self._pending_stop = None
            if pending_stop is not None:
                await self.push_frame(pending_stop, FrameDirection.DOWNSTREAM)
            else:
                await self.push_frame(
                    UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM
                )

        async def _llm_and_speak(self, text: str) -> None:
            """Основной путь: локальный Ollama со стримингом → TTS."""
            from time import perf_counter

            t0 = perf_counter()
            buf: list[str] = []
            last = [0.0]

            def on_chunk(chunk: str) -> None:
                buf.append(chunk)
                # прогрессивный вывод в терминал, но не чаще раза в 0.7 с
                now = perf_counter()
                if now - last[0] >= 0.7:
                    last[0] = now
                    log.info("Берта (llm): %s…", "".join(buf)[-90:])

            answer = ""
            try:
                answer = await asyncio.to_thread(self._stream_llm_sync, text, on_chunk)
            except asyncio.CancelledError:
                self._searching = False
                self._pending_stop = None
                raise
            except Exception:
                log.exception("Локальный LLM упал: %s", text)

            dt_ms = int((perf_counter() - t0) * 1000)
            self._searching = False

            if not answer:
                log.info("Локальный LLM не ответил за %d мс | %s", dt_ms, text)
                await self._finish_turn()
                return

            log.info("Локальный LLM: ответ за %d мс | %s", dt_ms, answer[:120])
            try:
                self._bus.publish(
                    BotReplyEvent(text=answer, query=text, source="llm")
                )
            except Exception:
                log.exception("BotReplyEvent (llm) publish упал")
            await self.push_frame(
                TTSSpeakFrame(answer, append_to_context=False),
                FrameDirection.DOWNSTREAM,
            )
            await self._finish_turn()

        async def _search_and_speak(self, text: str) -> None:
            from time import perf_counter

            t0 = perf_counter()
            answer = ""
            try:
                answer = await maybe_web_search(text)
            except asyncio.CancelledError:
                self._searching = False
                self._pending_stop = None
                raise
            except Exception:
                log.exception("веб-поиск упал: %s", text)

            dt_ms = int((perf_counter() - t0) * 1000)
            self._searching = False

            if not answer:
                log.info("веб-поиск: нет ответа за %d мс → LLM | %s", dt_ms, text)
                # фоллбэк: обычный путь — локальный LLM
                await self._llm_and_speak(text)
                return

            log.info(
                "веб-поиск: ответ за %d мс → TTS | %s",
                dt_ms, answer[:120],
            )
            try:
                self._bus.publish(
                    BotReplyEvent(text=answer, query=text, source="search")
                )
            except Exception:
                log.exception("BotReplyEvent publish упал")
            await self.push_frame(
                TTSSpeakFrame(answer, append_to_context=False),
                FrameDirection.DOWNSTREAM,
            )
            await self._finish_turn()

        async def _task_and_speak(self, text: str) -> None:
            from time import perf_counter

            t0 = perf_counter()
            try:
                answer = await asyncio.to_thread(
                    run, parse_task_command(text), HttpTaskAPI()
                )
            except asyncio.CancelledError:
                self._searching = False
                self._pending_stop = None
                raise
            except Exception:
                log.exception("голос(задача): упало: %s", text)
                answer = "Панель задач недоступна."

            dt_ms = int((perf_counter() - t0) * 1000)
            self._searching = False

            log.info(
                "голос(задача): ответ за %d мс → TTS | %s",
                dt_ms, (answer or "")[:120],
            )
            if not answer:
                await self._finish_turn()
                return
            try:
                self._bus.publish(
                    BotReplyEvent(text=answer, query=text, source="task")
                )
            except Exception:
                log.exception("BotReplyEvent (task) publish упал")
            await self.push_frame(
                TTSSpeakFrame(answer, append_to_context=False),
                FrameDirection.DOWNSTREAM,
            )
            await self._finish_turn()

    return SearchRouter


def _make_reply_tap_class():
    """FrameProcessor: агрегированный ответ LLM → BotReplyEvent (чат).

    Позиция: между OLLamaLLM и EdgeTTS. Копит TextFrame’ы текущего
    ответа и на LLMFullResponseEndFrame публикует итог.
    """
    from pipecat.frames.frames import (
        Frame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        TextFrame,
        TranscriptionFrame,
        InterimTranscriptionFrame,
        EagerTranscriptionFrame,
    )
    from pipecat.processors.frame_processor import (
        FrameDirection,
        FrameProcessor,
    )

    class ReplyTap(FrameProcessor):
        def __init__(self, bus: EventBus, turn: dict[str, str]) -> None:
            super().__init__(name="jarvis-reply-tap")
            self._bus = bus
            self._turn = turn
            self._buf: list[str] = []
            self._active = False

        async def process_frame(
            self, frame: Frame, direction: FrameDirection
        ) -> None:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._buf = []
                self._active = True
            elif isinstance(frame, TextFrame) and self._active:
                if not isinstance(
                    frame,
                    (TranscriptionFrame, InterimTranscriptionFrame, EagerTranscriptionFrame),
                ):
                    self._buf.append(frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame):
                text = "".join(self._buf).strip()
                self._buf = []
                self._active = False
                if text:
                    try:
                        self._bus.publish(
                            BotReplyEvent(
                                text=text,
                                query=self._turn.get("query", ""),
                                source="llm",
                            )
                        )
                    except Exception:
                        log.exception("BotReplyEvent (llm) publish упал")
            await self.push_frame(frame, direction)

    return ReplyTap


# ---------------------------------------------------------------------------
# Сервис
# ---------------------------------------------------------------------------
class PipecatVoiceService(BaseService):
    """Разговорный голосовой контур (pipecat) в потоке BaseService."""

    def __init__(self, config: VoiceConfig, bus: EventBus) -> None:
        super().__init__(name="voice-pipecat")
        self._cfg = config
        self._bus = bus
        self._runner: Any = None
        self._run_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        asyncio.run(self._amain())

    async def _amain(self) -> None:
        try:
            await self._run_pipeline()
        except Exception:
            log.exception("pipecat-конвейер упал")
            self._stop_event.set()

    async def _run_pipeline(self) -> None:
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMAssistantAggregator,
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.services.ollama.llm import (
            OLLamaLLMService,
            OllamaLLMSettings,
        )
        from pipecat.services.whisper.stt import WhisperSTTService
        from pipecat.transcriptions.language import Language
        from pipecat.transports.local.audio import (
            LocalAudioTransport,
            LocalAudioTransportParams,
        )

        from jarvis.tts.edge_service import create_edge_tts_service

        cfg = self._cfg
        sr_in = int(cfg.samplerate)
        sr_out = 24000

        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=sr_in,
                audio_out_sample_rate=sr_out,
                audio_in_channels=1,
                audio_out_channels=1,
                input_device_index=cfg.device,
            )
        )
        vad = SileroVADAnalyzer(
            sample_rate=sr_in,
            params=VADParams(
                confidence=0.7,
                start_secs=0.2,
                stop_secs=float(cfg.silence_end),
            ),
        )
        lang: Language | None = None
        if cfg.whisper_language:
            try:
                lang = Language(cfg.whisper_language)
            except ValueError:
                lang = Language.RU

        stt_plan = resolve_stt()
        stt = WhisperSTTService(
            device=stt_plan.device,
            compute_type=stt_plan.compute_type,
            sample_rate=sr_in,
            settings=WhisperSTTService.Settings(
                model=stt_plan.model,
                language=lang or Language.RU,
                no_speech_prob=0.4,
                hotwords=None,
                initial_prompt=None,
            ),
        )

        # Stream partials: делим загруженную faster-whisper модель с WhisperSTT
        InterimSTT = _make_interim_class()
        interim = InterimSTT(
            self._bus,
            whisper_model=getattr(stt, "_model", None),
            language=cfg.whisper_language or "ru",
            sample_rate=sr_in,
            partial_seconds=float(cfg.partial_seconds),
            vad_threshold=float(cfg.vad_threshold),
            silence_end=float(cfg.silence_end),
            partial_max_seconds=float(cfg.partial_max_seconds),
            partial_work_budget=float(cfg.partial_work_budget),
        )

        llm_model = os.environ.get("LLM_MODEL", "llama3.2:3b")
        llm_base = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
        system_prompt = os.environ.get(
            "BERTA_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT
        )
        # короткие ответы → быстрее первый токен и старт TTS
        llm_max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "220"))
        llm = OLLamaLLMService(
            base_url=llm_base,
            settings=OllamaLLMSettings(
                model=llm_model,
                system_instruction=system_prompt,
                max_tokens=llm_max_tokens,
                temperature=0.4,
            ),
        )

        tts = create_edge_tts_service(sample_rate=sr_out)

        context = LLMContext()
        Bridge = _make_bridge_class()
        bridge = Bridge(self._bus)
        turn: dict[str, str] = {"query": ""}
        SearchRouter = _make_search_router_class()
        search_router = SearchRouter(
            self._bus,
            turn,
            llm_model=llm_model,
            system_prompt=system_prompt,
            max_tokens=llm_max_tokens,
            web_search_enabled=os.environ.get(
                "VOICE_WEB_SEARCH", "1"
            ).lower() not in ("0", "false", "no"),
        )
        ReplyTap = _make_reply_tap_class()
        reply_tap = ReplyTap(self._bus, turn)
        user_agg, assistant_agg = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(vad_analyzer=vad),
        )

        pipeline = Pipeline(
            [
                transport.input(),
                interim,
                stt,
                bridge,
                search_router,
                user_agg,
                llm,
                reply_tap,
                tts,
                transport.output(),
                assistant_agg,
            ]
        )
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=sr_in,
                audio_out_sample_rate=sr_out,
                enable_metrics=False,
                enable_usage_metrics=False,
            ),
            name="berta-voice",
            cancel_on_idle_timeout=False,
            idle_timeout_secs=None,
            enable_rtvi=False,
        )
        runner = PipelineRunner(
            handle_sigint=False, handle_sigterm=False, name="berta-runner"
        )
        await runner.add_workers(worker)
        self._runner = runner
        self._run_task = asyncio.create_task(runner.run())
        log.info(
            "pipecat: whisper=%s (interim+final) llm=%s → %s "
            "(web-search on, max_tokens=%d)",
            cfg.whisper_model, llm_model, llm_base, llm_max_tokens,
        )

        while not self._stop_event.is_set() and not self._run_task.done():
            await asyncio.sleep(0.2)

        if not self._run_task.done():
            log.info("pipecat: остановка…")
            runner.cancel(reason="jarvis-stop")
            try:
                await asyncio.wait_for(self._run_task, timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._run_task.cancel()
                try:
                    await self._run_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


__all__ = [
    "PipecatVoiceService",
    "dispatch_transcription",
    "needs_web_search",
]
