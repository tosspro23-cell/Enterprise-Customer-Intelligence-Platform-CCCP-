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

import os
import time
from dataclasses import dataclass

import azure.cognitiveservices.speech as speechsdk


def _speech_config() -> speechsdk.SpeechConfig:
    return speechsdk.SpeechConfig(subscription=os.environ["AZURE_SPEECH_KEY"],
                                   region=os.environ["AZURE_SPEECH_REGION"])


@dataclass(frozen=True)
class SttResult:
    recognized_text: str
    reason: str
    synthesis_s: float
    recognition_s: float


def synthesize(text: str) -> bytes:
    """Text -> WAV bytes (16kHz mono PCM) via Azure neural TTS."""
    cfg = _speech_config()
    cfg.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm)
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
    result = synthesizer.speak_text_async(text).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        raise RuntimeError(f"TTS failed: {result.reason} / {getattr(result, 'cancellation_details', None)}")
    return result.audio_data


def transcribe(wav_bytes: bytes) -> SttResult:
    """WAV bytes -> recognized text via Azure real-time streaming STT.

    Uses PushAudioInputStream so this exercises the same streaming API a
    live call would use (push bytes as they arrive), not the simpler
    file-based recognizer.
    """
    cfg = _speech_config()
    stream = speechsdk.audio.PushAudioInputStream()
    audio_config = speechsdk.audio.AudioConfig(stream=stream)
    recognizer = speechsdk.SpeechRecognizer(speech_config=cfg, audio_config=audio_config)

    # WAV header is 44 bytes for the format we requested; push the PCM body.
    stream.write(wav_bytes[44:])
    stream.close()

    result = recognizer.recognize_once()
    return SttResult(result.text, str(result.reason), 0.0, 0.0)


def roundtrip(text: str) -> SttResult:
    """Full text -> speech -> text validation, with real timings for each leg."""
    t0 = time.perf_counter()
    audio = synthesize(text)
    t1 = time.perf_counter()
    stt = transcribe(audio)
    t2 = time.perf_counter()
    return SttResult(stt.recognized_text, stt.reason, round(t1 - t0, 3), round(t2 - t1, 3))
