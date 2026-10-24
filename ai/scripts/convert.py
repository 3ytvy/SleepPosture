#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert.py — 部署链第1步：.keras → INT8量化 .tflite

一句话：把训练好的模型"瘦身"成能跑在单片机上的部署格式，并验证瘦身没把精度弄坏。

输入：
    ai/models/ 里最新的 posture_model_*.keras（也可以命令行指定）
    全部真实数据（用于量化校准 + 转换后验证）
输出：
    与输入同名的 .tflite 文件（INT8量化）

在流水线中的位置：
    train.py → [convert.py] → to_c_array.py → 固件

流程：
    1. 找模型（默认取最新）
    2. 读全部真实数据（复用 train.py 的加载函数，保证与训练同源、同归一化）
    3. 定义代表性数据集生成器（量化校准用）
    4. 配置 TFLite 转换器（INT8）
    5. 转换并保存 .tflite
    6. 验证：用 tflite 模型跑全部数据，和 keras 模型对比准确率
    7. 打印输入/输出的量化参数（scale/zero_point）

关键点（为什么这么做）：
    - 代表性数据集必须和训练数据同源、同归一化（直接复用 train.py 的函数，
      保证两边处理完全一致）
    - 转换完必须自己验证一遍（量化会掉一点精度，掉多少要心里有数）
    - 输入/输出的 scale、zero_point 会打印出来——固件那边运行时也要用它

用法：
    python ai/scripts/convert.py                          # 自动取最新模型
    python ai/scripts/convert.py ai/models/xxx.keras      # 指定模型

运行环境：装了 TensorFlow 的 Python 3.10+。
"""

import os
# 在 import tensorflow 之前设置日志级别（2=只显示 ERROR 及以上），让输出干净
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import sys                        # sys.argv（命令行参数）/ sys.path / sys.exit
from pathlib import Path          # 跨平台路径操作

import numpy as np                # 数值计算
import tensorflow as tf           # TensorFlow：加载 Keras 模型 + TFLite 转换器 + 解释器

# ============================================================
# 复用 train.py 的数据加载和归一化函数
# ============================================================
# 为什么要把脚本所在目录插进 sys.path？
#   直接运行 python convert.py 时 Python 会自动把脚本目录加入搜索路径，
#   但用某些方式（如 -m、被其他脚本导入）运行时不一定——显式加一次最保险。
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ★ 关键设计：不重新写一套数据加载/归一化，而是 import train.py 里的实现。
#   这样"训练用的预处理"和"量化校准/验证用的预处理"在物理上就是同一份代码，
#   杜绝"两边写得不一样"的可能——这就是一致性契约的工程保证。
from train import load_all, normalize_rows, CLASS_NAMES   # noqa: E402

# 项目根目录 = 本文件(ai/scripts/)往上两级
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = PROJECT_ROOT / "ai" / "models"

# 代表性数据集取多少条？
#   量化需要知道"真实输入的数值范围"才能算准 scale/zero_point。
#   300 条足够覆盖分布范围；每条样本很小，这一步耗时可忽略。
REP_SAMPLES = 300


def find_latest_keras():
    """找 ai/models/ 里最新的 .keras 模型（按文件名排序，时间戳命名保证有序）。

    返回：
        最新的 .keras 的 Path
    失败：
        打印错误并退出（exit code 1）
    """
    # glob：按模式匹配文件；sorted：按文件名排序（posture_model_20261023_1030 这种命名天然按时间递增）
    files = sorted(MODEL_DIR.glob("posture_model_*.keras"))
    if not files:
        print(f"错误：{MODEL_DIR} 里没有找到 posture_model_*.keras")
        sys.exit(1)
    return files[-1]      # 取列表最后一个 = 时间戳最新的那个


def main():
    print("=" * 62)
    print("SleepPosture 部署链 第1步：.keras → INT8 .tflite")
    print("=" * 62)

    # ---------- 1. 找模型 ----------
    if len(sys.argv) > 1:
        # 命令行传了模型路径就用它（方便对历史模型做转换/对比）
        keras_path = Path(sys.argv[1])
        if not keras_path.exists():
            print(f"错误：找不到 {keras_path}")
            sys.exit(1)
    else:
        # 没传参 → 自动挑最新的
        keras_path = find_latest_keras()
    print(f"\n[输入模型] {keras_path}")

    # 加载 .keras（完整模型：结构+权重；训练相关信息这里用不到也没关系）
    model = tf.keras.models.load_model(keras_path)
    model.summary()                       # 打印结构（核对输入12、输出3）

    # ---------- 2. 数据（代表集 + 验证用） ----------
    # 用 train.py 的 load_all()：扫描 ai/data/ 所有数据集，丢弃未标注/坏行
    # 第三个返回值是"来源清单"，这里用不上，用 _ 丢弃
    X_list, y_list, _ = load_all()
    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int32)
    # ★ 用 train.py 的 normalize_rows()：和训练时逐字相同的归一化
    X = normalize_rows(X)
    print(f"\n[数据] {len(X)} 条（将全部用于验证；其中 {min(REP_SAMPLES, len(X))} 条用于代表性数据集）")

    def representative_data_gen():
        """代表性数据集生成器——量化校准用。

        转换器会反复调用这个函数，每次拿到一个样本，用来观察输入的真实数值分布，
        从而为每个张量计算合适的 scale 和 zero_point。

        ★ 必须是生成器（yield），不能一次 return 一个列表：
          转换器是"边要边用"，不需要把300条一次性装进内存。
        ★ 返回形状必须是 (1, 12)：第一维是 batch（固定为1），第二维是特征数12。
          形状不对转换器会报错。
        """
        for i in range(min(REP_SAMPLES, len(X))):   # 最多300条（数据不够就全给）
            yield [X[i:i+1]]                        # 用列表包一层：表示"一个输入张量"

    # ---------- 3. 配置转换器并转换 ----------
    # 从 Keras 模型创建转换器（它记录模型结构，后面按配置导出）
    converter = tf.lite.TFLiteConverter.from_keras_model(model)

    # 开启默认优化（其中包含量化）——这是"允许压缩"的开关
    converter.optimizations = [tf.lite.Optimize.DEFAULT]

    # 指定代表性数据集：给转换器一批真实样本，用于计算量化参数
    converter.representative_dataset = representative_data_gen

    # 限定只使用"内置 INT8 算子"——不允许回退到浮点实现。
    # 这样能保证转换出来的模型在纯整数路径上运行（ESP32 上没有 FPU 加速，浮点慢）
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]

    # 指定模型的"对外接口"也是 int8：
    #   - inference_input_type=int8：固件只需要把 int8 数据塞进去，不需要浮点接口
    #   - inference_output_type=int8：输出也是 int8，固件自己做反量化
    converter.inference_input_type = tf.int8
    converter.inference_output_type = tf.int8

    print("\n[转换中] ...")
    # 执行转换：返回 .tflite 文件的全部字节（bytes 对象）
    tflite_bytes = converter.convert()

    # 输出文件与输入模型同名，只把扩展名换成 .tflite：
    #   with_suffix(".tflite")：posture_model_xxx.keras → posture_model_xxx.tflite
    tflite_path = keras_path.with_suffix(".tflite")
    with open(tflite_path, "wb") as f:    # "wb"：二进制写
        f.write(tflite_bytes)
    print(f"[保存] {tflite_path}")
    # 对比文件大小：直观感受量化带来的体积变化（keras 含优化器状态/训练配置，所以大很多）
    print(f"       大小: {len(tflite_bytes)} 字节 ({len(tflite_bytes)/1024:.2f} KB)"
          f"   （keras文件 {keras_path.stat().st_size/1024:.1f} KB）")

    # ---------- 4. 验证 ----------
    # 用 TFLite 解释器加载刚生成的模型（这是电脑端的推理引擎，和固件的 TFLM 是"同门兄弟"）
    interp = tf.lite.Interpreter(model_content=tflite_bytes)
    interp.allocate_tensors()             # 分配张量（电脑端不需要手动管 arena，框架内部处理）

    # 取输入/输出张量的详细信息（字典形式）
    inp = interp.get_input_details()[0]   # 第0个输入
    out = interp.get_output_details()[0]  # 第0个输出

    # 从张量描述里取量化参数
    # quantization_parameters["scales"] 是个列表（每个量化维度一个 scale，我们这模型是单值）
    in_scale = inp["quantization_parameters"]["scales"][0]
    in_zp = inp["quantization_parameters"]["zero_points"][0]
    out_scale = out["quantization_parameters"]["scales"][0]
    out_zp = out["quantization_parameters"]["zero_points"][0]

    print("\n[量化参数]（固件运行时从张量里读取，这里只是打印出来核对）")
    print(f"  输入: shape={inp['shape']}, dtype={inp['dtype']}, scale={in_scale:.8f}, zero_point={in_zp}")
    print(f"  输出: shape={out['shape']}, dtype={out['dtype']}, scale={out_scale:.8f}, zero_point={out_zp}")

    # 用 tflite 模型把全部数据跑一遍，统计准确率
    print("\n[验证] tflite 模型跑全部数据 ...")
    correct = 0
    for i in range(len(X)):
        # 手工量化：q = round(x / scale) + zero_point，然后夹到 int8 的 -128~127。
        # np.round + astype 的顺序：先按公式算，再四舍五入，最后转 int8。
        # np.clip 防止极端值越界（防御性写法）。
        q = np.clip(np.round(X[i:i+1] / in_scale) + in_zp, -128, 127).astype(np.int8)
        interp.set_tensor(inp["index"], q)    # 把量化后的输入写进输入张量
        interp.invoke()                        # 推理一次
        out_data = interp.get_tensor(out["index"])[0]   # 取输出（3个int8）
        # argmax 直接比 int8：反量化是单调线性变换，整数谁大 → 实数谁大
        pred = int(np.argmax(out_data))
        if pred == y[i]:
            correct += 1
    tflite_acc = correct / len(X)

    # 对比：Keras 模型在同一批数据上的准确率（作为"量化前的基准"）
    keras_pred = np.argmax(model.predict(X, verbose=0), axis=1)   # 输出3个概率 → argmax
    keras_acc = float((keras_pred == y).sum()) / len(X)

    print(f"  keras  模型准确率: {keras_acc:.4f}")
    print(f"  tflite 模型准确率: {tflite_acc:.4f}   （量化损失 {keras_acc - tflite_acc:+.4f}）")

    # 随手看一条样本的细节，方便人工核对量化/反量化是否算对
    i = 0                                                     # 取第0条
    q = np.clip(np.round(X[i:i+1] / in_scale) + in_zp, -128, 127).astype(np.int8)
    interp.set_tensor(inp["index"], q)
    interp.invoke()
    out_q = interp.get_tensor(out["index"])[0]                # int8 原始输出
    probs = (out_q.astype(np.float32) - out_zp) * out_scale   # 反量化成概率
    print(f"\n[抽样] 第1条(真实标签={CLASS_NAMES[int(y[i])]}):")
    print(f"  int8输出: {out_q}")
    print(f"  反量化概率: {np.round(probs, 4)}")

    print("\n下一步：把 .tflite 转成 C 数组 → python ai/scripts/to_c_array.py")


# 直接运行本脚本时才执行 main（被 import 时不会自动跑）
if __name__ == "__main__":
    main()
