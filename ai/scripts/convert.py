#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert.py — 部署链第1步：.keras → INT8量化 .tflite（【Claude修改】新建）

做什么：
    1. 找最新的 ai/models/posture_model_*.keras（也可以命令行指定）
    2. 读全部真实数据当"代表性数据集"（量化时用来定 scale/zero_point 的范围）
    3. 转成 INT8 量化的 .tflite（体积缩小约4倍，ESP32上跑整数运算）
    4. 验证：用 tflite 模型把所有数据跑一遍，和 keras 模型对比准确率
    5. 保存 ai/models/同名.tflite

用法：
    python ai/scripts/convert.py                          # 自动取最新模型
    python ai/scripts/convert.py ai/models/xxx.keras      # 指定模型

关键点（为什么这么做）：
    - 代表性数据集必须和训练数据同源、同归一化（直接复用 train.py 的加载函数，
      保证两边处理完全一致）
    - 转换完必须自己验证一遍（量化会掉一点精度，掉多少要心里有数）
    - 输入/输出的 scale、zero_point 会打印出来——固件那边运行时也要用它
"""

import os
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import sys
from pathlib import Path

import numpy as np
import tensorflow as tf

# 复用 train.py 的数据加载 + 归一化（保证与训练完全一致）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from train import load_all, normalize_rows, CLASS_NAMES   # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = PROJECT_ROOT / "ai" / "models"

REP_SAMPLES = 300     # 代表性数据集取多少条（够覆盖数据范围就行）


def find_latest_keras():
    """找 ai/models/ 里最新的 .keras"""
    files = sorted(MODEL_DIR.glob("posture_model_*.keras"))
    if not files:
        print(f"错误：{MODEL_DIR} 里没有找到 posture_model_*.keras")
        sys.exit(1)
    return files[-1]      # 文件名带时间戳，排序后最后一个就是最新


def main():
    print("=" * 62)
    print("SleepPosture 部署链 第1步：.keras → INT8 .tflite")
    print("=" * 62)

    # ---------- 1. 找模型 ----------
    if len(sys.argv) > 1:
        keras_path = Path(sys.argv[1])
        if not keras_path.exists():
            print(f"错误：找不到 {keras_path}")
            sys.exit(1)
    else:
        keras_path = find_latest_keras()
    print(f"\n[输入模型] {keras_path}")

    model = tf.keras.models.load_model(keras_path)
    model.summary()

    # ---------- 2. 数据（代表集 + 验证用） ----------
    X_list, y_list, _ = load_all()
    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int32)
    X = normalize_rows(X)              # ★ 和训练完全一样的归一化
    print(f"\n[数据] {len(X)} 条（将全部用于验证；其中 {min(REP_SAMPLES, len(X))} 条用于代表性数据集）")

    def representative_data_gen():
        """量化校准用：喂给转换器一批真实样本，让它知道数值范围"""
        for i in range(min(REP_SAMPLES, len(X))):
            yield [X[i:i+1]]

    # ---------- 3. 配置转换器并转换 ----------
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]          # 开量化优化
    converter.representative_dataset = representative_data_gen    # 代表性数据集
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]  # 只用INT8算子
    converter.inference_input_type = tf.int8                      # 输入输出也是int8
    converter.inference_output_type = tf.int8

    print("\n[转换中] ...")
    tflite_bytes = converter.convert()

    tflite_path = keras_path.with_suffix(".tflite")
    with open(tflite_path, "wb") as f:
        f.write(tflite_bytes)
    print(f"[保存] {tflite_path}")
    print(f"       大小: {len(tflite_bytes)} 字节 ({len(tflite_bytes)/1024:.2f} KB)"
          f"   （keras文件 {keras_path.stat().st_size/1024:.1f} KB）")

    # ---------- 4. 验证 ----------
    interp = tf.lite.Interpreter(model_content=tflite_bytes)
    interp.allocate_tensors()
    inp = interp.get_input_details()[0]
    out = interp.get_output_details()[0]

    in_scale = inp["quantization_parameters"]["scales"][0]
    in_zp = inp["quantization_parameters"]["zero_points"][0]
    out_scale = out["quantization_parameters"]["scales"][0]
    out_zp = out["quantization_parameters"]["zero_points"][0]

    print("\n[量化参数]（固件运行时从张量里读取，这里只是打印出来核对）")
    print(f"  输入: shape={inp['shape']}, dtype={inp['dtype']}, scale={in_scale:.8f}, zero_point={in_zp}")
    print(f"  输出: shape={out['shape']}, dtype={out['dtype']}, scale={out_scale:.8f}, zero_point={out_zp}")

    # 用 tflite 模型把全部数据跑一遍
    print("\n[验证] tflite 模型跑全部数据 ...")
    correct = 0
    for i in range(len(X)):
        # 手工量化：q = round(x/scale) + zero_point，夹到 -128~127
        q = np.clip(np.round(X[i:i+1] / in_scale) + in_zp, -128, 127).astype(np.int8)
        interp.set_tensor(inp["index"], q)
        interp.invoke()
        out_data = interp.get_tensor(out["index"])[0]
        pred = int(np.argmax(out_data))     # int8直接比大小=等价于反量化后比大小
        if pred == y[i]:
            correct += 1
    tflite_acc = correct / len(X)

    # 对比：keras模型在同一批数据上的准确率
    keras_pred = np.argmax(model.predict(X, verbose=0), axis=1)
    keras_acc = float((keras_pred == y).sum()) / len(X)

    print(f"  keras  模型准确率: {keras_acc:.4f}")
    print(f"  tflite 模型准确率: {tflite_acc:.4f}   （量化损失 {keras_acc - tflite_acc:+.4f}）")

    # 随手看一条样本的细节，方便核对
    i = 0
    q = np.clip(np.round(X[i:i+1] / in_scale) + in_zp, -128, 127).astype(np.int8)
    interp.set_tensor(inp["index"], q)
    interp.invoke()
    out_q = interp.get_tensor(out["index"])[0]
    probs = (out_q.astype(np.float32) - out_zp) * out_scale
    print(f"\n[抽样] 第1条(真实标签={CLASS_NAMES[int(y[i])]}):")
    print(f"  int8输出: {out_q}")
    print(f"  反量化概率: {np.round(probs, 4)}")

    print("\n下一步：把 .tflite 转成 C 数组 → python ai/scripts/to_c_array.py")


if __name__ == "__main__":
    main()
