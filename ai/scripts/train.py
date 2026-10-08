#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train.py — SleepPosture 训练流水线（【Claude修改】新建）

设计目标：数据驱动 + 可增量。
    以后新增数据（往 ai/data/ 里放新文件夹，配好 body_data.txt）→ 重跑本脚本
    → 自动把"旧的+新的"全部数据一起训练 → 得到更新、更强的模型。
    不需要改任何代码。

流程：
    1. 扫描 ai/data/ 下每个"数据集"文件夹（含 body_data.txt + posture_*.csv）
    2. 读取全部带标签数据（label=-1 的未标注行自动丢弃）
    3. 预处理：每帧除以该帧最大值（★必须和以后ESP32推理时的处理保持一致）
    4. 按类别分层划分 训练集/验证集（固定随机种子，结果可复现）
    5. 训练 MLP 12→16→16→3
    6. 评估：验证准确率 + 混淆矩阵 + 每类精确率/召回率
    7. 保存 ai/models/posture_model_<时间>.keras + 一份报告txt

用法：
    python ai/scripts/train.py

运行环境：用练习项目那个装了TensorFlow的venv（或任何装好TF的Python 3.10+）。
"""

import os
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")   # 少打点TensorFlow的噪音日志

import csv
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import tensorflow as tf

# ============================================================
# 配置
# ============================================================
# 项目根目录 = 本文件(ai/scripts/)往上两级
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_ROOT = PROJECT_ROOT / "ai" / "data"      # 数据总目录（里面每个子文件夹=一个数据集）
MODEL_DIR = PROJECT_ROOT / "ai" / "models"    # 模型输出目录

SEED = 42            # 随机种子：固定住，保证每次划分/训练结果可复现
VAL_RATIO = 0.2      # 验证集比例
EPOCHS = 100         # 最大训练轮数（有EarlyStopping，实际会提前停）
BATCH_SIZE = 32

CLASS_NAMES = {0: "仰卧", 1: "左侧卧", 2: "右侧卧"}

# 说明（重要，先写着，以后数据多了要改）：
#   当前每类只有一段录制（一个文件），验证集和训练集来自同一段录制里的相邻帧，
#   帧与帧高度相似 → 验证准确率会偏乐观。以后每类录了多段之后，
#   可以把划分方式改成"按文件划分"（整段录制备份到验证集），评估更诚实。


# ============================================================
# 读取数据
# ============================================================
def parse_body_data(path: Path):
    """读 body_data.txt（如 '172.5,65'）→ dict；没有/格式不对就返回None。
    身高体重目前只作为数据集档案记录，不参与训练。"""
    if not path.exists():
        return None
    try:
        h, w = path.read_text(encoding="utf-8").strip().split(",")
        return {"height_cm": float(h), "weight_kg": float(w)}
    except (ValueError, OSError):
        return None


def load_csv_file(path: Path):
    """读一个 posture CSV → (X, y, 未标注丢弃数, 坏行数)

    CSV格式：timestamp,p0,...,p11,label   （表头全英文）
    - label = -1（未标注）→ 不参与训练，丢弃并计数
    - label 不在 0/1/2 里 → 当坏行丢弃
    """
    X, y = [], []
    n_unlabeled = 0
    n_bad = 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)              # 跳过表头
        for row in reader:
            if len(row) != 14:          # timestamp + 12个值 + label
                n_bad += 1
                continue
            try:
                vals = [int(v) for v in row[1:13]]
                lab = int(row[13])
            except ValueError:
                n_bad += 1
                continue
            if lab == -1:
                n_unlabeled += 1
                continue
            if lab not in CLASS_NAMES:
                n_bad += 1
                continue
            X.append(vals)
            y.append(lab)
    return X, y, n_unlabeled, n_bad


def load_all():
    """扫描 ai/data/ 下所有数据集，返回 (X, y, 来源清单)

    来源清单用于打印：[(数据集名, body信息, [(文件名, 条数, 丢弃未标注数), ...]), ...]
    """
    X, y = [], []
    sources = []

    # 每个子文件夹 = 一个数据集（一个人 / 一次归档）
    subjects = [d for d in sorted(DATA_ROOT.iterdir()) if d.is_dir()] if DATA_ROOT.exists() else []

    # 也兼容"直接放在 ai/data/ 下的松散 posture_*.csv"（归到"(未分组)"）
    loose = sorted(DATA_ROOT.glob("posture_*.csv"))

    work_list = [(d, d.name) for d in subjects]
    if loose:
        work_list.append((DATA_ROOT, "(未分组)"))

    for subject_dir, name in work_list:
        body = parse_body_data(subject_dir / "body_data.txt")
        files = sorted(subject_dir.glob("posture_*.csv"))
        file_rows = []
        for f in files:
            fx, fy, nu, nb = load_csv_file(f)
            X.extend(fx)
            y.extend(fy)
            file_rows.append((f.name, len(fx), nu, nb))
        if file_rows:
            sources.append((name, body, file_rows))

    return X, y, sources


def normalize_rows(X):
    """每帧除以该帧最大值 → 0~1（★必须和固件推理时保持一致）"""
    row_max = X.max(axis=1, keepdims=True)
    row_max[row_max < 1] = 1            # 全0的空帧：防除零
    return X / row_max


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 62)
    print("SleepPosture 训练流水线")
    print("=" * 62)

    # ---------- 1. 加载 ----------
    X_list, y_list, sources = load_all()
    if not X_list:
        print("错误：在 ai/data/ 下没有找到任何训练数据（需要 posture_*.csv）")
        sys.exit(1)

    print("\n[数据清单]")
    for name, body, file_rows in sources:
        if body:
            body_str = f"身高 {body['height_cm']}cm  体重 {body['weight_kg']}kg"
        else:
            body_str = "（没有 body_data.txt）"
        print(f"  数据集 {name}:  {body_str}")
        for fname, n, nu, nb in file_rows:
            extra = ""
            if nu:
                extra += f"，丢弃未标注 {nu} 条"
            if nb:
                extra += f"，丢弃坏行 {nb} 条"
            print(f"      {fname}:  {n} 条{extra}")

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int32)

    print(f"\n[样本统计]  共 {len(X)} 条")
    for c, cname in CLASS_NAMES.items():
        print(f"    {cname}({c}): {int((y == c).sum())} 条")

    # ---------- 2. 预处理 ----------
    # ★ 每帧除以该帧的最大值 → 映射到 0~1。
    #    这和以后ESP32推理时的处理必须完全一致（不然模型在板子上不准）
    X = normalize_rows(X)

    # ---------- 3. 划分训练/验证（分层，固定种子） ----------
    rng = np.random.default_rng(SEED)
    idx_train, idx_val = [], []
    for c in CLASS_NAMES:
        idx = np.where(y == c)[0]
        rng.shuffle(idx)
        n_val = max(1, int(len(idx) * VAL_RATIO))
        idx_val.extend(idx[:n_val])
        idx_train.extend(idx[n_val:])
    rng.shuffle(idx_train)

    X_tr, y_tr = X[idx_train], y[idx_train]
    X_val, y_val = X[idx_val], y[idx_val]
    print(f"\n[划分]  训练集 {len(X_tr)} 条 / 验证集 {len(X_val)} 条"
          f"（验证集比例 {VAL_RATIO:.0%}，种子 {SEED}）")

    # ---------- 4. 搭模型 ----------
    tf.random.set_seed(SEED)
    model = tf.keras.Sequential([
        tf.keras.layers.Dense(16, activation="relu", input_shape=(12,)),
        tf.keras.layers.Dense(16, activation="relu"),
        tf.keras.layers.Dense(3, activation="softmax"),
    ])
    model.summary()

    model.compile(optimizer="adam",
                  loss="sparse_categorical_crossentropy",
                  metrics=["accuracy"])

    # ---------- 5. 训练 ----------
    # EarlyStopping：验证损失连续20轮不下降就提前停，并恢复到最好的那一轮
    early = tf.keras.callbacks.EarlyStopping(
        monitor="val_loss", patience=20, restore_best_weights=True)

    model.fit(X_tr, y_tr,
              epochs=EPOCHS,
              batch_size=BATCH_SIZE,
              validation_data=(X_val, y_val),
              callbacks=[early],
              verbose=1)

    # ---------- 6. 评估 ----------
    val_loss, val_acc = model.evaluate(X_val, y_val, verbose=0)
    print(f"\n[评估]  验证集准确率: {val_acc:.4f}   验证损失: {val_loss:.4f}")

    y_pred = np.argmax(model.predict(X_val, verbose=0), axis=1)
    cm = np.zeros((3, 3), dtype=int)
    for t, p in zip(y_val, y_pred):
        cm[t][p] += 1

    print("\n[混淆矩阵]  行=真实，列=预测")
    print("          " + "".join(f"{CLASS_NAMES[c]:>8}" for c in CLASS_NAMES))
    for c in CLASS_NAMES:
        row = "".join(f"{cm[c][k]:>8}" for k in CLASS_NAMES)
        print(f"  {CLASS_NAMES[c]:>6}  {row}")

    print("\n[每类指标]")
    report_lines = []
    for c in CLASS_NAMES:
        tp = cm[c][c]
        fp = cm[:, c].sum() - tp
        fn = cm[c, :].sum() - tp
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        line = f"  {CLASS_NAMES[c]}: 精确率 {prec:.3f}  召回率 {rec:.3f}"
        print(line)
        report_lines.append(line)

    # ---------- 7. 保存模型 + 报告 ----------
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    model_path = MODEL_DIR / f"posture_model_{ts}.keras"
    model.save(model_path)
    print(f"\n[保存]  模型: {model_path}")

    report_path = MODEL_DIR / f"posture_model_{ts}_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"SleepPosture 训练报告  {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write("=" * 50 + "\n\n")
        f.write("[数据来源]\n")
        for name, body, file_rows in sources:
            body_str = (f"身高 {body['height_cm']}cm 体重 {body['weight_kg']}kg"
                        if body else "无body_data")
            f.write(f"  数据集 {name} ({body_str})\n")
            for fname, n, nu, nb in file_rows:
                f.write(f"    {fname}: {n} 条 (丢弃未标注 {nu} 条, 坏行 {nb} 条)\n")
        f.write(f"\n[样本] 共 {len(X)} 条\n")
        for c, cname in CLASS_NAMES.items():
            f.write(f"  {cname}({c}): {int((y == c).sum())} 条\n")
        f.write(f"\n[划分] 训练 {len(X_tr)} / 验证 {len(X_val)} (种子 {SEED})\n")
        f.write(f"[结果] 验证准确率 {val_acc:.4f}, 验证损失 {val_loss:.4f}\n\n")
        f.write("[混淆矩阵] 行=真实 列=预测\n")
        f.write("        " + "".join(f"{CLASS_NAMES[c]:>8}" for c in CLASS_NAMES) + "\n")
        for c in CLASS_NAMES:
            f.write(f"{CLASS_NAMES[c]:>6}  "
                    + "".join(f"{cm[c][k]:>8}" for k in CLASS_NAMES) + "\n")
        f.write("\n[每类指标]\n")
        for line in report_lines:
            f.write(line + "\n")
        f.write("\n[备注] 当前每类只有一段录制，验证集与训练集同源，准确率偏乐观。\n")
    print(f"[保存]  报告: {report_path}")

    print("\n下一步：")
    print("  - 以后新增数据：往 ai/data/ 放新文件夹（配 body_data.txt），重跑本脚本即可")
    print("  - 部署转换：.keras → 量化 .tflite → C数组（convert.py / to_c_array.py，待写）")


if __name__ == "__main__":
    main()
