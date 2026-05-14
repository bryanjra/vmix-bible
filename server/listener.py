"""In-process audio listener: device → faster-whisper → utterance callback.

Owns one sounddevice InputStream and one WhisperModel. The FastAPI app
flips it on/off via `start(device, channel, model)` and `stop()`.

Both `sounddevice` and `faster_whisper` are imported lazily inside the
methods that need them, so the verse server still boots on a machine
that hasn't installed the audio stack — only the listener is degraded.
"""
from __future__ import annotations

import asyncio
import threading
import time
from queue import Empty, Queue
from typing import Any, Callable, Coroutine, List, Optional


SAMPLE_RATE = 16000
BLOCK_DURATION = 0.5
SILENCE_RMS = 0.012
MIN_UTTERANCE_S = 0.8
MAX_UTTERANCE_S = 15.0
SILENCE_TAIL_S = 0.8


def list_input_devices() -> List[dict]:
    """Return PortAudio's input devices. Empty list if the audio stack is missing."""
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except Exception:
        return []
    out: List[dict] = []
    try:
        for i, d in enumerate(sd.query_devices()):
            ch = int(d.get("max_input_channels", 0))
            if ch <= 0:
                continue
            out.append({
                "index": i,
                "name": d["name"],
                "channels": ch,
                "default_samplerate": float(d.get("default_samplerate", SAMPLE_RATE)),
            })
    except Exception:
        return []
    return out


def default_input_index() -> Optional[int]:
    """The OS default input device index, or None."""
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
        default = sd.default.device
        idx = default[0] if isinstance(default, (list, tuple)) else default
        if idx is None or idx < 0:
            return None
        return int(idx)
    except Exception:
        return None


UtteranceCallback = Callable[[str, float], Coroutine[Any, Any, None]]


class AudioListener:
    """Background audio capture + whisper transcription.

    Thread-safe: `start()` stops any prior session first, so it's also the
    way to reconfigure (different device, channel, or model).
    """

    def __init__(self, on_utterance: UtteranceCallback) -> None:
        self._on_utterance = on_utterance
        self._stream: Any = None
        self._worker: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._audio_q: Queue = Queue()
        self._model: Any = None
        self._model_name: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.status: str = "stopped"
        self.error: Optional[str] = None

    def is_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def start(self, device_index: int, channel: int, model_name: str = "small") -> None:
        """Stop any prior session and start a new one with the given config."""
        self.stop()
        try:
            import sounddevice as sd  # type: ignore[import-not-found]
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as e:
            self.status = "error"
            self.error = f"missing audio deps: {e}. Install requirements.txt."
            return

        self._loop = asyncio.get_running_loop()

        if self._model is None or self._model_name != model_name:
            self.status = f"loading {model_name}…"
            try:
                self._model = WhisperModel(model_name, device="cpu", compute_type="int8")
                self._model_name = model_name
            except Exception as e:
                self.status = "error"
                self.error = f"whisper load failed: {e}"
                return

        self._stop_evt.clear()
        self._audio_q = Queue()
        pick = channel - 1

        def callback(indata, _frames, _time_info, _status):  # type: ignore[no-untyped-def]
            del _frames, _time_info, _status
            if self._stop_evt.is_set():
                return
            self._audio_q.put(indata[:, pick].copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=channel,
                dtype="float32",
                blocksize=int(SAMPLE_RATE * BLOCK_DURATION),
                device=device_index,
                callback=callback,
            )
            self._stream.start()
        except Exception as e:
            self.status = "error"
            self.error = f"audio stream failed: {e}"
            return

        self._worker = threading.Thread(target=self._consume, daemon=True)
        self._worker.start()
        self.status = "listening"
        self.error = None

    def stop(self) -> None:
        self._stop_evt.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        if self.status != "error":
            self.status = "stopped"

    def _consume(self) -> None:
        """Worker thread: VAD-segment + transcribe + dispatch utterance."""
        import numpy as np  # type: ignore[import-not-found]

        buf: List[Any] = []
        silent_for = 0.0
        total_s = 0.0
        in_speech = False

        while not self._stop_evt.is_set():
            try:
                block = self._audio_q.get(timeout=0.5)
            except Empty:
                continue

            block_s = len(block) / SAMPLE_RATE
            rms = float(np.sqrt(np.mean(block * block) + 1e-12))

            if rms > SILENCE_RMS:
                buf.append(block)
                silent_for = 0.0
                total_s += block_s
                in_speech = True
            elif in_speech:
                buf.append(block)
                silent_for += block_s
                total_s += block_s

            finalize = in_speech and (
                silent_for >= SILENCE_TAIL_S or total_s >= MAX_UTTERANCE_S
            )
            if not finalize:
                continue

            if total_s >= MIN_UTTERANCE_S and self._model is not None:
                audio = np.concatenate(buf)
                try:
                    segments, _ = self._model.transcribe(
                        audio,
                        language="es",
                        vad_filter=False,
                        beam_size=1,
                        no_speech_threshold=0.5,
                    )
                    text = " ".join(s.text for s in segments).strip()
                except Exception:
                    text = ""
                if text and self._loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        self._on_utterance(text, time.time()),
                        self._loop,
                    )

            buf = []
            silent_for = 0.0
            total_s = 0.0
            in_speech = False
