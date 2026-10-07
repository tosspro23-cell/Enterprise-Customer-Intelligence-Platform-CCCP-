"""Real speech-to-text, validated the only honest way available without a
real phone call: synthesise each scripted utterance to audio with Azure
TTS, then feed that audio into Azure's real-time streaming STT and see
what comes back. This exercises the actual M1-M2 audio path
(docs/architecture.md §9.2) end to end -- the audio is synthetic, but the
synthesis -> network -> recognition round trip is real.

Not a substitute for a real call: there is no telephony integration here,
no live microphone, no Genesys/AudioHook stream. It proves the STT
service call works and measures its real latency; it does not prove
anything about real voice quality, accents, or a live audio pipeline.
"""
from __future__ import annotations

import functools
import os
import threading
import time
from dataclasses import dataclass

import azure.cognitiveservices.speech as speechsdk


# Upper bound on one utterance's recognition; continuous recognition normally
# ends on its own when the pushed stream is closed and drained.
RECOGNITION_TIMEOUT_S = 30.0


@functools.lru_cache(maxsize=None)
def _speech_config(voice: str | None = None, *, for_tts: bool = False) -> speechsdk.SpeechConfig:
    """Built once per (voice, purpose) and reused -- rebuilding it on every
    call added client setup to the measured per-turn latency."""
    cfg = speechsdk.SpeechConfig(subscription=os.environ["AZURE_SPEECH_KEY"],
                                 region=os.environ["AZURE_SPEECH_REGION"])
    if for_tts:
        cfg.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm)
        if voice:
            cfg.speech_synthesis_voice_name = voice
    return cfg


@dataclass(frozen=True)
class SttResult:
    recognized_text: str
    reason: str
    synthesis_s: float
    recognition_s: float


def synthesize(text: str, voice: str | None = None) -> bytes:
    """Text -> WAV bytes (16kHz mono PCM) via Azure neural TTS.

    `voice` picks the neural voice (e.g. "en-US-JennyNeural") -- the Workbench
    uses two different voices for customer vs. agent so a played-back call
    sounds like two people, not one voice reading both sides.
    """
    cfg = _speech_config(voice, for_tts=True)
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
    result = synthesizer.speak_text_async(text).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        raise RuntimeError(f"TTS failed: {result.reason} / {getattr(result, 'cancellation_details', None)}")
    return result.audio_data


def transcribe(wav_bytes: bytes) -> SttResult:
    """WAV bytes -> recognized text via Azure real-time streaming STT.

    Uses PushAudioInputStream so this exercises the same streaming API a
    live call would use (push bytes as they arrive), not the simpler
    file-based recognizer. Continuous recognition, not `recognize_once()`:
    the latter stops at the first pause, silently truncating multi-sentence
    turns. Every recognised segment is joined in order.
    """
    stream = speechsdk.audio.PushAudioInputStream()
    audio_config = speechsdk.audio.AudioConfig(stream=stream)
    recognizer = speechsdk.SpeechRecognizer(speech_config=_speech_config(), audio_config=audio_config)

    segments: list[str] = []
    reasons: list[str] = []
    done = threading.Event()

    def on_recognized(evt) -> None:
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech and evt.result.text:
            segments.append(evt.result.text)

    def on_canceled(evt) -> None:
        details = getattr(evt, "cancellation_details", None) or getattr(evt.result, "cancellation_details", None)
        reason = getattr(details, "reason", None)
        if reason is not None and reason != speechsdk.CancellationReason.EndOfStream:
            reasons.append(f"canceled:{reason}")
        done.set()

    recognizer.recognized.connect(on_recognized)
    recognizer.canceled.connect(on_canceled)
    recognizer.session_stopped.connect(lambda _evt: done.set())

    recognizer.start_continuous_recognition()
    # WAV header is 44 bytes for the format we requested; push the PCM body.
    stream.write(wav_bytes[44:])
    stream.close()
    finished = done.wait(RECOGNITION_TIMEOUT_S)
    recognizer.stop_continuous_recognition()
    if not finished:
        reasons.append(f"timeout:{RECOGNITION_TIMEOUT_S}s")
    if not reasons:
        reasons.append("RecognizedSpeech" if segments else "NoMatch")
    return SttResult(" ".join(segments), ",".join(reasons), 0.0, 0.0)


def roundtrip(text: str) -> SttResult:
    """Full text -> speech -> text validation, with real timings for each leg."""
    t0 = time.perf_counter()
    audio = synthesize(text)
    t1 = time.perf_counter()
    stt = transcribe(audio)
    t2 = time.perf_counter()
    return SttResult(stt.recognized_text, stt.reason, round(t1 - t0, 3), round(t2 - t1, 3))
