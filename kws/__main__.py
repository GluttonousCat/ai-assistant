#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import wave

from .detector import KeywordDetector, DetectionState
from .audio import AudioRecorder
from .server_notify import ServerNotifier, AudioPlayer
from .utils import rms

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

TOP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_BASE_URL = "http://localhost:9897"


def get_args():
    parser = argparse.ArgumentParser(
        description="Sherpa-ONNX 关键词检测 - 使用 PyAudio 从麦克风实时检测关键词",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        "--tokens",
        type=str,
        required=True,
        help="tokens.txt 文件路径"
    )
    parser.add_argument(
        "--encoder",
        type=str,
        required=True,
        help="编码器 ONNX 模型路径"
    )
    parser.add_argument(
        "--decoder",
        type=str,
        required=True,
        help="解码器 ONNX 模型路径"
    )
    parser.add_argument(
        "--joiner",
        type=str,
        required=True,
        help="joiner ONNX 模型路径"
    )
    parser.add_argument(
        "--keywords-file",
        type=str,
        required=True,
        help="关键词文件路径，每行一个关键词（需要先用 text2token 工具处理）"
    )

    parser.add_argument(
        "--num-threads",
        type=int,
        default=2,
        help="神经网络推理使用的线程数"
    )
    parser.add_argument(
        "--provider",
        type=str,
        default="cpu",
        choices=["cpu", "cuda", "coreml"],
        help="推理后端：cpu, cuda, coreml"
    )
    parser.add_argument(
        "--max-active-paths",
        type=int,
        default=4,
        help="解码时保留的最大活跃路径数"
    )
    parser.add_argument(
        "--num-trailing-blanks",
        type=int,
        default=1,
        help="关键词后跟随的空白帧数（如果关键词之间有重叠 token，可设置为较大值如 8）"
    )
    parser.add_argument(
        "--keywords-score",
        type=float,
        default=1.5,
        help="关键词 token 的增强分数，越大越容易被检测到"
    )
    parser.add_argument(
        "--keywords-threshold",
        type=float,
        default=0.07,
        help="关键词触发阈值（概率），越大越难触发"
    )

    parser.add_argument(
        "--sample-rate",
        type=int,
        default=16000,
        help="音频采样率（Hz）"
    )
    parser.add_argument(
        "--chunk-duration",
        type=float,
        default=0.1,
        help="每次读取的音频时长（秒）"
    )
    parser.add_argument(
        "--input-device-index",
        type=int,
        default=None,
        help="PyAudio 输入设备 ID（用于选择 echo-cancel 等虚拟麦克风；不填则使用默认输入设备）"
    )

    parser.add_argument(
        "--pre-roll-seconds",
        type=float,
        default=0.5,
        help="唤醒词前保留的音频时长（秒）"
    )
    parser.add_argument(
        "--silence-rms-threshold",
        type=float,
        default=0.015,
        help="静音 RMS 阈值"
    )
    parser.add_argument(
        "--silence-count-threshold",
        type=int,
        default=15,
        help="静音判定块数阈值"
    )
    parser.add_argument(
        "--min-record-seconds",
        type=float,
        default=1.5,
        help="最小录音时长（秒）"
    )
    parser.add_argument(
        "--max-record-seconds",
        type=float,
        default=15.0,
        help="最大录音时长（秒）"
    )
    parser.add_argument(
        "--post-wake-grace-seconds",
        type=float,
        default=1.2,
        help="唤醒后宽限时长（秒）"
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试日志"
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="列出音频设备并退出"
    )

    return parser.parse_args()


def main():
    """主函数"""
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
        logger.error("请检查模型文件路径是否正确！")
        logger.error("模型下载地址：https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html")
        sys.exit(1)

    recorder = AudioRecorder(
        sample_rate=args.sample_rate,
        chunk_duration=args.chunk_duration,
        input_device_index=args.input_device_index,
    )

    device_info = recorder.list_devices()
    default_device_id = device_info['default_id']

    detector.create_spotter()
    detector.create_stream()

    notifier = ServerNotifier()
    player = AudioPlayer(sample_rate=args.sample_rate)

    notifier.kws_ready()

    input_device_index = args.input_device_index \
        if args.input_device_index is not None else default_device_id
    recorder.start(device_index=input_device_index)

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

    logger.info("=" * 60)
    logger.info("关键词检测已启动！请对着麦克风说出关键词...")
    logger.info(f"关键词文件：{args.keywords_file}")
    logger.info("按 Ctrl+C 停止程序")
    logger.info("=" * 60)

    try:
        while True:
            samples = recorder.read_chunk()
            audio_data = samples.tobytes()

            if state.state == "PASSIVE":
                state.add_to_pre_roll(audio_data)
                detector.accept_waveform(args.sample_rate, samples)

                result = detector.detect()
                if result:
                    count = state.increment_detection_count()
                    timestamp = state.get_timestamp()

                    logger.info("=" * 40)
                    logger.info(f"🎯 检测到关键词！第 {count} 次")
                    logger.info(f"   关键词：{result}")
                    logger.info(f"   时间：{timestamp}")
                    logger.info("=" * 40)

                    logger.info("中断语音队列")
                    notifier.interrupt_play()

                    player.play_tone(
                        freq_hz=880.0, duration_ms=120, volume=0.25)

                    def _play_response():
                        time.sleep(0.15)
                        duration = notifier.play_preset()
                        if duration > 0:
                            time.sleep(min(duration, 5.0))

                    threading.Thread(
                        target=_play_response, daemon=True).start()

                    interrupt_pause_seconds = float(
                        os.environ.get(
                            "INTERRUPT_PAUSE_SECONDS", "0.2"
                        ) or "0.2"
                    )
                    if interrupt_pause_seconds > 0:
                        time.sleep(min(interrupt_pause_seconds, 0.5))

                    detector.reset_stream()

                    state.start_recording(state.get_pre_roll())

            else:
                state.recorded_frames.append(audio_data)
                state.chunk_count += 1

                detector.accept_waveform(args.sample_rate, samples)
                result = detector.detect()
                if result:
                    count = state.increment_detection_count()
                    timestamp = state.get_timestamp()
                    logger.info("=" * 40)
                    logger.info(f"🎯 ACTIVE 阶段检测到关键词！第 {count} 次")
                    logger.info(f"   关键词：{result}")
                    logger.info(f"   时间：{timestamp}")
                    logger.info("=" * 40)
                    logger.info("二次唤醒：中断语音队列并重置录音窗口")
                    notifier.interrupt_play()
                    detector.reset_stream()
                    state.stop_recording()
                    continue

                cur_rms = rms(samples)
                state.update_silence(cur_rms < state.silence_rms_threshold)

                if state.should_stop_recording():
                    fname = state.get_recording_filename(TOP_DIR)

                    with wave.open(fname, 'wb') as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2)
                        wav_file.setframerate(args.sample_rate)
                        wav_file.writeframes(b''.join(state.recorded_frames))

                    player.play_tone(freq_hz=520.0, duration_ms=180, volume=0.3)

                    threading.Thread(
                        target=notifier.to_upload, args=(fname,), daemon=True
                    ).start()

                    state.stop_recording()
                    detector.reset_stream()

    except KeyboardInterrupt:
        logger.info("\n程序已停止（用户中断）")
    finally:
        recorder.stop()
        notifier.close()
        logger.info(f"总共检测到 {state.detection_count} 次关键词")


if __name__ == "__main__":
    main()
