# -*- coding: utf-8 -*-
"""
@date: 2026/03/23
@author: GluttonousCat
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import wave

import numpy as np

from .detector import KeywordDetector
from .audio import AudioRecorder
from .server_notify import ServerNotifier
from .player import AudioPlayer
from .state import DetectionState
from .utils import rms, get_logger, AudioMonitor

logger = get_logger(__name__)

TOP_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_BASE_URL = "http://localhost:9897"


def get_args():
    parser = argparse.ArgumentParser(
        description="Sherpa-ONNX Keyword Spotting: Real-time keyword detection via PyAudio",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--tokens", type=str, required=True,
                        help="Path to tokens.txt")
    parser.add_argument("--encoder", type=str, required=True,
                        help="Path to encoder model")
    parser.add_argument("--decoder", type=str, required=True,
                        help="Path to decoder model")
    parser.add_argument("--joiner", type=str, required=True,
                        help="Path to joiner model")
    parser.add_argument("--keywords-file", type=str, required=True,
                        help="Path to keywords.txt")
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--provider", type=str, default="cpu",
                        choices=["cpu", "cuda", "coreml"])
    parser.add_argument("--max-active-paths", type=int, default=4)
    parser.add_argument("--num-trailing-blanks", type=int, default=1)
    parser.add_argument("--keywords-score", type=float, default=1.5)
    parser.add_argument("--keywords-threshold", type=float, default=0.07)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--chunk-duration", type=float, default=0.1)
    parser.add_argument("--input-device-index", type=int, default=None)
    parser.add_argument("--pre-roll-seconds", type=float, default=1.0)
    parser.add_argument("--silence-rms-threshold", type=float, default=0.01)
    parser.add_argument("--silence-count-threshold", type=int, default=10)
    parser.add_argument("--min-record-seconds", type=float, default=1.0)
    parser.add_argument("--max-record-seconds", type=float, default=10.0)
    parser.add_argument("--post-wake-grace-seconds", type=float, default=0.5)
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--list-devices", action="store_true",
                        help="List audio input devices")
    return parser.parse_args()


def main():
    args = get_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_devices:
        recorder = AudioRecorder()
        recorder.list_devices()
        return

    detector = KeywordDetector(
        tokens=args.tokens,
        encoder=args.encoder,
        decoder=args.decoder,
        joiner=args.joiner,
        keywords_file=args.keywords_file,
        num_threads=args.num_threads,
        provider=args.provider,
        max_active_paths=args.max_active_paths,
        num_trailing_blanks=args.num_trailing_blanks,
        keywords_score=args.keywords_score,
        keywords_threshold=args.keywords_threshold,
    )

    if not detector.validate_files():
        logger.error("Model files validation failed! Please check file paths.")
        sys.exit(1)

    detector.create_spotter()
    detector.create_stream()

    notifier = ServerNotifier()
    player = AudioPlayer(sample_rate=args.sample_rate)
    notifier.kws_ready()

    state = DetectionState(
        sample_rate=args.sample_rate,
        chunk_duration=args.chunk_duration,
        pre_roll_seconds=args.pre_roll_seconds,
        silence_rms_threshold=args.silence_rms_threshold,
        silence_count_threshold=args.silence_count_threshold,
        min_record_seconds=args.min_record_seconds,
        max_record_seconds=args.max_record_seconds,
        post_wake_grace_seconds=args.post_wake_grace_seconds,
    )

    recorder = AudioRecorder(
        sample_rate=args.sample_rate,
        chunk_duration=args.chunk_duration,
        input_device_index=args.input_device_index,
    )

    # Audio Monitor (5s summary)
    audio_monitor = AudioMonitor(interval_seconds=5)

    try:
        with recorder:
            logger.info("=" * 60)
            logger.info("KWS Service Started! Listening for keywords...")
            logger.info(f"Keywords File: {args.keywords_file}")
            logger.info(f"Threshold:     {args.keywords_threshold}")
            logger.info(f"Score Bonus:   {args.keywords_score}")
            logger.info("=" * 60)

            detector.reset_stream()

            while True:
                audio_bytes, samples_float32 = recorder.read_chunk()
                cur_rms = rms(samples_float32)

                audio_monitor.add_sample(cur_rms, state.state)

                if state.state == "PASSIVE":
                    state.add_to_pre_roll(audio_bytes)
                    detector.accept_waveform(args.sample_rate, samples_float32)

                    result = detector.detect()
                    if result:
                        count = state.increment_detection_count()
                        timestamp = state.get_timestamp()

                        kw_str = getattr(result, 'keyword', str(result))
                        score_val = getattr(result, 'score', 'N/A')
                        wake_frames = state.get_pre_roll()

                        if wake_frames:
                            wake_bytes = b''.join(wake_frames)
                            wake_samples = np.frombuffer(wake_bytes,
                                                         dtype=np.int16).astype(
                                np.float32) / 32768.0
                            real_avg_rms = float(
                                np.sqrt(np.mean(np.square(wake_samples))))
                            chunk_rmss = [
                                rms(np.frombuffer(c, dtype=np.int16).astype(
                                    np.float32) / 32768.0) for c in wake_frames]
                            real_max_rms = max(
                                chunk_rmss) if chunk_rmss else cur_rms
                        else:
                            real_avg_rms = cur_rms
                            real_max_rms = cur_rms

                        # Detection Report
                        logger.info("\n" + "=" * 50)
                        logger.info(f"🎯 WAKE WORD DETECTED! (Count: {count})")
                        logger.info(f"   Keyword: {kw_str}")
                        logger.info(f"   ------------------------")
                        logger.info(f"   🔊 [Volume Analysis]")
                        logger.info(
                            f"      Sentence Avg RMS: {real_avg_rms:.6f}")
                        logger.info(
                            f"      Peak Instant RMS: {real_max_rms:.6f}")
                        logger.info(f"      Trigger Chunk RMS: {cur_rms:.6f} ")
                        logger.info(f"   ------------------------")
                        logger.info(
                            f"   Config Threshold: {args.keywords_threshold}")
                        logger.info(
                            f"   Config Score:     {args.keywords_score}")
                        logger.info(f"   Model Final Score: {score_val}")
                        logger.info(f"   ------------------------")
                        logger.info(f"   Timestamp: {timestamp}")
                        logger.info("=" * 50 + "\n")

                        notifier.interrupt_play()
                        player.play_tone(freq_hz=880.0, duration_ms=120,
                                         volume=0.25)

                        def _play_response():
                            time.sleep(0.15)
                            duration = notifier.play_preset()
                            if duration > 0:
                                time.sleep(min(duration, 5.0))

                        threading.Thread(target=_play_response,
                                         daemon=True).start()

                        pause_sec = float(
                            os.environ.get("INTERRUPT_PAUSE_SECONDS",
                                           "0.2") or "0.2")
                        if pause_sec > 0:
                            time.sleep(min(pause_sec, 0.5))

                        detector.reset_stream()
                        state.start_recording(state.get_pre_roll())

                else:
                    state.recorded_frames.append(audio_bytes)
                    state.chunk_count += 1

                    detector.accept_waveform(args.sample_rate, samples_float32)
                    result = detector.detect()
                    if result:
                        count = state.increment_detection_count()
                        score_val = getattr(result, 'score', 'N/A')

                        logger.info("\n" + "=" * 50)
                        logger.info(
                            f"🎯 Interruption Detected in ACTIVE Phase! (Count: {count})")
                        logger.info(f"   Keyword: {result}")
                        logger.info(
                            f"   Current RMS: {cur_rms:.6f} | Score: {score_val}")
                        logger.info("=" * 50 + "\n")

                        notifier.interrupt_play()
                        detector.reset_stream()
                        state.stop_recording()
                        state.start_recording(state.get_pre_roll())
                        continue

                    state.update_silence(cur_rms < state.silence_rms_threshold)

                    if state.should_stop_recording():
                        fname = state.get_recording_filename(TOP_DIR)
                        os.makedirs(os.path.dirname(fname), exist_ok=True)

                        with wave.open(fname, 'wb') as wav_file:
                            wav_file.setnchannels(1)
                            wav_file.setsampwidth(2)
                            wav_file.setframerate(args.sample_rate)
                            wav_file.writeframes(
                                b''.join(state.recorded_frames))

                        logger.info(f"💾 Recording finished and saved: {fname}")

                        threading.Thread(
                            target=notifier.to_upload, args=(fname,),
                            daemon=True
                        ).start()

                        state.stop_recording()
                        detector.reset_stream()
                        logger.info(
                            "🔄 Audio uploaded. Detection stream reset. Ready for next trigger...")

    except KeyboardInterrupt:
        logger.info("\nProgram stopped by user (KeyboardInterrupt)")
    finally:
        logger.info(
            f"🏁 Service terminated. Total keywords detected: {state.detection_count}")


if __name__ == "__main__":
    main()