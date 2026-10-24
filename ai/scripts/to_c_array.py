#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
to_c_array.py — 部署链第2步：.tflite → C数组

一句话：把 .tflite 文件的每个字节，写成一个 C 语言数组，让固件能把它编译进去。

输入：
    ai/models/ 里最新的 posture_model_*.tflite（也可以命令行指定）
输出：
    firmware/components/ai_model/model_data.c
        - 数组本体（const、8字节对齐）
        - 数组长度 model_tflite_len

为什么需要这一步（和 EMBED_FILES 的关系）：
    ESP32 上没有"文件系统读取"这一层（我们也没挂文件系统），
    固件里不能 fopen("model.tflite")。
    所以把模型的字节直接写进 C 源码，跟固件一起编译、烧进 Flash。
    这是"资源进固件"的两种常见方式之一：
      - 本脚本：把字节转成 C 数组（手动、可控）
      - CMake EMBED_FILES：构建时把文件变成符号（网页用的是这种）

生成的 C 文件长这样（示意）：
    #include "model_data.h"
    const unsigned int model_tflite_len = 3928;
    const unsigned char model_tflite[] __attribute__((aligned(8))) = {
        0x1c, 0x00, 0x00, 0x00, ...
    };

配套文件（手写，不归本脚本管）：
    firmware/components/ai_model/model_data.h   只放 extern 声明
        extern const unsigned char model_tflite[];
        extern const unsigned int model_tflite_len;
    为什么不把数组直接放 .h？→ 数组只能"定义"一次；.h 被多个 .c 包含会重复定义。
    所以：.c 放定义（只编一次），.h 放声明（谁都能 include）。

用法：
    python ai/scripts/to_c_array.py                 # 自动取ai/models里最新的.tflite
    python ai/scripts/to_c_array.py xxx.tflite      # 指定文件

运行环境：普通 Python 3 即可（不需要 TensorFlow）。
"""

import sys                        # sys.argv（命令行参数）/ sys.exit
from datetime import datetime     # 生成时间写进文件头注释（方便追溯）
from pathlib import Path          # 跨平台路径操作

# 项目根目录 = 本文件(ai/scripts/)往上两级
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = PROJECT_ROOT / "ai" / "models"                        # .tflite 所在目录
OUT_DIR = PROJECT_ROOT / "firmware" / "components" / "ai_model"   # 生成文件的去向


def find_latest_tflite():
    """找 ai/models/ 里最新的 .tflite（按文件名排序取最后一个）。

    返回：
        最新的 .tflite 的 Path
    失败：
        提示先跑 convert.py，然后退出
    """
    files = sorted(MODEL_DIR.glob("posture_model_*.tflite"))
    if not files:
        print(f"错误：{MODEL_DIR} 里没有找到 posture_model_*.tflite")
        print("请先跑: python ai/scripts/convert.py")
        sys.exit(1)
    return files[-1]                  # 文件名带时间戳，排序后最后一个就是最新


def main():
    print("=" * 62)
    print("SleepPosture 部署链 第2步：.tflite → C数组")
    print("=" * 62)

    # ---------- 1. 找文件 ----------
    if len(sys.argv) > 1:
        tflite_path = Path(sys.argv[1])       # 命令行指定
        if not tflite_path.exists():
            print(f"错误：找不到 {tflite_path}")
            sys.exit(1)
    else:
        tflite_path = find_latest_tflite()    # 自动取最新
    print(f"\n[输入] {tflite_path}")

    # 一次读出全部字节（模型只有几KB，放内存毫无压力）
    data = tflite_path.read_bytes()
    print(f"       大小: {len(data)} 字节")

    # ---------- 2. 生成C文件 ----------
    # 输出目录不存在就创建（parents=True 连父目录一起建；exist_ok=True 已存在不报错）
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "model_data.c"

    # 以文本方式写 C 源码
    with open(out_path, "w", encoding="utf-8") as f:
        # ---- 文件头注释：来源、大小、生成时间（工程留痕，别人/未来的你知道文件怎么来的）
        f.write("// ============================================================\n")
        f.write("// 本文件由 ai/scripts/to_c_array.py 自动生成，请勿手动修改！\n")
        f.write(f"// 来源: {tflite_path.name}  ({len(data)} 字节)\n")
        f.write(f"// 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write("// ============================================================\n\n")

        # ---- 包含头文件：拿到 extern 声明（让编译器检查类型一致）
        f.write('#include "model_data.h"\n\n')

        # ---- 数组长度：在 .h 里也是 extern 声明，两边类型必须一致（unsigned int）
        f.write(f"const unsigned int model_tflite_len = {len(data)};\n\n")

        # ---- 数组本体 ----
        # const：数据放进 Flash 的 .rodata 段（只读），不占 RAM。
        #        如果去掉 const，链接器可能放进 .data（可写），启动时复制到 RAM，白占几KB。
        # __attribute__((aligned(8)))：要求数组起始地址按 8 字节对齐——
        #        TFLite Micro 解析 flatbuffer 时对地址对齐有要求，不对齐可能崩溃或变慢。
        f.write("// const：放Flash省RAM；aligned(8)：TFLite Micro解析模型要求对齐\n")
        f.write("const unsigned char model_tflite[] __attribute__((aligned(8))) = {\n")

        # 逐字节输出十六进制，每行 12 个，方便阅读/diff
        # range(0, len, 12)：每 12 个字节取一段；data[i:i+12] 是这一段的字节
        for i in range(0, len(data), 12):
            chunk = data[i:i + 12]
            # f"0x{b:02x}"：把字节 b 格式化成两位小写十六进制（如 0x1c）
            # " ".join(...)：用空格连接；末尾补逗号；行首缩进4空格
            f.write("    " + " ".join(f"0x{b:02x}," for b in chunk) + "\n")
        f.write("};\n")

    # ---- 打印结果 ----
    print(f"\n[保存] {out_path}")
    print(f"       共 {out_path.stat().st_size} 字节的C源码")
    print("\n下一步：重新编译固件（模型会一起编进去），烧录后ESP32就能实时推理了")


# 直接运行本脚本时才执行 main
if __name__ == "__main__":
    main()
