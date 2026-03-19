# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import wave


from .detector import Detector, DetectionState
from .audio import AudioRecorder
from .client import ServerNotifier
from utils import rms, get_logger

logger = get_logger(__name__)

TOP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_BASE_URL = "http://localhost:9897"


def get_args():
    parser = argparse.ArgumentParser(
        description="Sherpa-ONNX 关键词检测,使用 PyAudio 从麦克风实时检测关键词",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    # 模型文件参数
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

    # 推理参数
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
        help="推理后端: cpu, cuda, coreml"
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
        help="关键词后跟随的空白帧数（如果关键词之间有重叠token，可设置为较大值如8）"
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

    # 音频参数
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

    return parser.parse_args()


def main():
    args = get_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_devices:
        recorder = AudioRecorder()
        recorder.list_devices()
        return

    # 初始化模型
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
        sys.exit(1)

    detector.create_spotter()
    detector.create_stream()

    # 初始化外围组件
    notifier = ServerNotifier()
    player = AudioPlayer(sample_rate=args.sample_rate)
    notifier.kws_ready()

    # 初始化状态机
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

    # 初始化录音器
    recorder = AudioRecorder(
        sample_rate=args.sample_rate,
        chunk_duration=args.chunk_duration,
        input_device_index=args.input_device_index,
    )

    try:
        # 使用上下文管理器自动启停麦克风
        with recorder:
            logger.info("=" * 60)
            logger.info("关键词检测已启动！请对着麦克风说出关键词...")
            logger.info(f"关键词文件：{args.keywords_file}")
            logger.info("按 Ctrl+C 停止程序")
            logger.info("=" * 60)

            # 初始重置一次流
            detector.reset_stream()

            while True:
                # 修复 Bug: 分别获取用于保存的 bytes 和用于推理的 float32
                audio_bytes, samples_float32 = recorder.read_chunk()

                if state.state == "PASSIVE":
                    state.add_to_pre_roll(audio_bytes)
                    detector.accept_waveform(args.sample_rate, samples_float32)

                    result = detector.detect()
                    if result:
                        count = state.increment_detection_count()
                        timestamp = state.get_timestamp()

                        logger.info("=" * 40)
                        logger.info(
                            f"🎯 检测到关键词！第 {count} 次: {result} ({timestamp})")
                        logger.info("=" * 40)

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

                        # 【状态切换 1】: PASSIVE -> ACTIVE
                        detector.reset_stream()  # 唤醒后立刻清空流特征，为可能的二次唤醒做准备
                        state.start_recording(state.get_pre_roll())

                else:  # ACTIVE (录音状态)
                    state.recorded_frames.append(audio_bytes)
                    state.chunk_count += 1

                    # 支持二次唤醒
                    detector.accept_waveform(args.sample_rate, samples_float32)
                    result = detector.detect()
                    if result:
                        count = state.increment_detection_count()
                        timestamp = state.get_timestamp()
                        logger.info("=" * 40)
                        logger.info(
                            f"🎯 ACTIVE 阶段二次唤醒！第 {count} 次: {result}")
                        logger.info("=" * 40)

                        notifier.interrupt_play()
                        detector.reset_stream()  # 二次唤醒立刻清空流
                        state.stop_recording()  # 停止旧的
                        state.start_recording((state.get_pre_roll())
                        continue  # 跳过本次循环的后续逻辑，直接进入下一次录音周期

                    # ==========================================
                    # VAD 静音检测与自动停止判定
                    # ==========================================

                    # 传入 float32 格式的数据计算当前帧的 RMS 能量
                    cur_rms = rms(samples_float32)
                    state.update_silence(cur_rms < state.silence_rms_threshold)

                    if state.should_stop_recording():
                        # 获取生成的文件名并确保目录存在
                        fname = state.get_recording_filename(TOP_DIR)
                        os.makedirs(os.path.dirname(fname), exist_ok=True)

                        # 使用 bytes 数据保存为 16-bit PCM WAV 文件
                        with wave.open(fname, 'wb') as wav_file:
                            wav_file.setnchannels(1)
                            wav_file.setsampwidth(2) # 2 bytes = 16 bit
                            wav_file.setframerate(args.sample_rate)
                            wav_file.writeframes(b''.join(state.recorded_frames))

                        logger.info(f"💾 录音已保存：{fname}")

                        # 异步上传音频，防止阻塞下一轮唤醒检测
                        threading.Thread(
                            target=notifier.to_upload, args=(fname,), daemon=True
                        ).start()

                        # 【状态切换 2】: ACTIVE -> PASSIVE
                        state.stop_recording()   # 将状态切换回 PASSIVE
                        detector.reset_stream()  # 录音结束后，重置流准备下一轮纯净的唤醒检测
                        logger.info("🔄 录音结束，重置检测流，准备下一轮等待...")

    except KeyboardInterrupt:
        logger.info("\n程序已停止（用户中断）")
    finally:
        # 由于 AudioRecorder 使用了 with 上下文管理器，
        # 退出代码块时会自动调用 __exit__ 并安全关闭麦克风，无需手动 recorder.stop()
        logger.info(f"🏁 运行结束，本次总共检测到 {state.detection_count} 次关键词")


if __name__ == "__main__":
    main()