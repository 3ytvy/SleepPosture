#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
to_c_array.py — 部署链第2步：.tflite → C数组（【Claude修改】新建）

做什么：
    把 .tflite 的每个字节写成一个C数组，生成：
        firmware/components/ai_model/model_data.c    （数组本体，每次自动覆盖）
    （同目录的 model_data.h 是手写的 extern 声明，不会被覆盖）

为什么：
    ESP32上不能"读文件"，模型必须成为固件的一部分，一起编译进Flash。
    TFLite Micro 运行时直接从这块数组里解析模型。

用法：
    python ai/scripts/to_c_array.py                 # 自动取ai/models里最新的.tflite
    python ai/scripts/to_c_array.py xxx.tflite      # 指定文件
"""

import sys
from datetime import datetime
from pathlib import Path

# 项目根目录 = 本文件(ai/scripts/)往上两级
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = PROJECT_ROOT / "ai" / "models"
OUT_DIR = PROJECT_ROOT / "firmware" / "components" / "ai_model"


def find_latest_tflite():
    """找 ai/models/ 里最新的 .tflite"""
    files = sorted(MODEL_DIR.glob("posture_model_*.tflite"))
    if not files:
        print(f"错误：{MODEL_DIR} 里没有找到 posture_model_*.tflite")
        print("请先跑: python ai/scripts/convert.py")
        sys.exit(1)
    return files[-1]      # 文件名带时间戳，排序后最后一个就是最新


def main():
    print("=" * 62)
    print("SleepPosture 部署链 第2步：.tflite → C数组")
    print("=" * 62)

    # ---------- 1. 找文件 ----------
    if len(sys.argv) > 1:
        tflite_path = Path(sys.argv[1])
        if not tflite_path.exists():
            print(f"错误：找不到 {tflite_path}")
            sys.exit(1)
    else:
        tflite_path = find_latest_tflite()
    print(f"\n[输入] {tflite_path}")

    data = tflite_path.read_bytes()
    print(f"       大小: {len(data)} 字节")

    # ---------- 2. 生成C文件 ----------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "model_data.c"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("// ============================================================\n")
        f.write("// 本文件由 ai/scripts/to_c_array.py 自动生成，请勿手动修改！\n")
        f.write(f"// 来源: {tflite_path.name}  ({len(data)} 字节)\n")
        f.write(f"// 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write("// ============================================================\n\n")
        f.write('#include "model_data.h"\n\n')

        # 长度（unsigned int，和 model_data.h 里的声明对应）
        f.write(f"const unsigned int model_tflite_len = {len(data)};\n\n")

        # 数组本体
        # - const   → 放Flash，不占RAM
        # - aligned(8) → TFLite Micro 解析 flatbuffer 要求对齐
        f.write("// const：放Flash省RAM；aligned(8)：TFLite Micro解析模型要求对齐\n")
        f.write("const unsigned char model_tflite[] __attribute__((aligned(8))) = {\n")
        for i in range(0, len(data), 12):
            chunk = data[i:i + 12]
            f.write("    " + " ".join(f"0x{b:02x}," for b in chunk) + "\n")
        f.write("};\n")

    print(f"\n[保存] {out_path}")
    print(f"       共 {out_path.stat().st_size} 字节的C源码")
    print("\n下一步：重新编译固件（模型会一起编进去），烧录后ESP32就能实时推理了")


if __name__ == "__main__":
    main()
