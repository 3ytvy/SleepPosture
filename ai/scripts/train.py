#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train.py — SleepPosture 训练流水线

一句话：把 ai/data/ 里的全部真实数据，训练成一个 .keras 模型。

设计目标：数据驱动 + 可增量
    以后新增数据（往 ai/data/ 放新文件夹，配好 body_data.txt）→ 重跑本脚本
    → 自动把"旧的 + 新的"全部数据一起训练 → 得到更新、更强的模型。
    不需要改任何代码。

输入：
    ai/data/<数据集名>/posture_*.csv   每行：timestamp,p0,...,p11,label
    ai/data/<数据集名>/body_data.txt   身高,体重（仅作档案，不参与训练）
输出：
    ai/models/posture_model_<时间>.keras        训练好的模型
    ai/models/posture_model_<时间>_report.txt   训练报告（数据来源/指标/混淆矩阵）

流程（main() 里的七步）：
    1. 扫描并加载全部带标签数据（label=-1 的未标注行丢弃）
    2. 预处理：每帧除以该帧最大值（★必须和固件 ai.cpp 的推理处理一致）
    3. 按类别分层划分训练集/验证集（固定随机种子，结果可复现）
    4. 搭模型：MLP 12→16→16→3
    5. 编译 + 训练（EarlyStopping 自动提前停）
    6. 评估：验证准确率 + 混淆矩阵 + 每类精确率/召回率
    7. 保存 .keras + 报告 txt

用法：
    python ai/scripts/train.py

运行环境：装了 TensorFlow 的 Python 3.10+（本项目用练习项目的 venv）。

本文件结构（按顺序读即可）：
    parse_body_data()  读身高体重档案
    load_csv_file()    读单个 CSV
    load_all()         扫描所有数据集，汇总 X/y
    normalize_rows()   ★归一化（与固件的一致性契约）
    main()             主流程（七步）
"""

import os
# 【必须在 import tensorflow 之前设置】TF_CPP_MIN_LOG_LEVEL 控制 TensorFlow 的 C++ 日志级别：
#   0=全部打印（INFO/WARNING/ERROR），1=隐藏INFO，2=隐藏INFO和WARNING，3=只显示FATAL
#   设成2是为了让终端输出干净，只留下我们自己 print 的训练信息。
# setdefault：如果外部环境已经设过这个变量，就不覆盖。
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import csv                       # 标准库：读写 CSV 文件（逐行解析）
import sys                       # 标准库：sys.exit() 用来在出错时退出程序
from datetime import datetime    # 标准库：取当前时间，用于给模型文件名加时间戳
from pathlib import Path         # 标准库：跨平台的路径操作（比字符串拼接安全）

import numpy as np               # 数值计算库：数组、最大值、随机数、批量运算
import tensorflow as tf          # 深度学习框架：搭模型、训练、评估、保存

# ============================================================
# 配置
# ============================================================
# 项目根目录 = 本文件(ai/scripts/train.py)往上两级：
#   __file__ 是本文件路径 → .resolve() 转成绝对路径 → .parent=scripts/
#   → .parent.parent=ai/ → .parent.parent.parent=项目根目录 SleepPosture/
# 这样无论从哪个目录运行脚本，路径都算得对（不依赖"当前工作目录"）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_ROOT = PROJECT_ROOT / "ai" / "data"      # 数据总目录（每个子文件夹=一个数据集）
MODEL_DIR = PROJECT_ROOT / "ai" / "models"    # 模型输出目录

SEED = 42            # 随机种子：固定住，保证每次的划分/初始化/训练结果可复现
VAL_RATIO = 0.2      # 验证集比例：留20%的数据不参与训练，只用来评估
EPOCHS = 100         # 最大训练轮数（有EarlyStopping，实际会提前停）
BATCH_SIZE = 32      # 每批样本数：每次拿32条算梯度、更新一次权重

# 类别编号 → 中文名（仅用于打印/报告显示）
# ★契约：这里的 0/1/2 必须和固件 config.h 的枚举、采集工具、mqtt/web 的名称表一致
CLASS_NAMES = {0: "仰卧", 1: "左侧卧", 2: "右侧卧"}

# 说明（重要，先写着，以后数据多了要改）：
#   当前每类只有一段录制（一个文件），验证集和训练集来自同一段录制里的相邻帧，
#   帧与帧高度相似 → 验证准确率会偏乐观（相当于"考试题和练习题是同几道题的变体"）。
#   以后每类录了多段之后，可以把划分方式改成"按文件划分"
#   （整段录制备份到验证集），评估更诚实。


# ============================================================
# 读数据：三个函数，从"单个小文件"到"全部数据集"
# ============================================================

def parse_body_data(path: Path):
    """读 body_data.txt（如 '172.5,65'）→ 字典；文件不存在/格式不对就返回 None。

    参数：
        path：body_data.txt 的完整路径
    返回：
        {"height_cm": 172.5, "weight_kg": 65.0} 或 None
    说明：身高体重目前只作为"数据集档案"记录下来（打印/写报告），不参与训练。
    """
    if not path.exists():                 # 文件不存在（不是每个人都会配）→ 直接返回空
        return None
    try:
        # read_text：把整个文件读成一个字符串
        # .strip()：去掉首尾空白（包括换行符）
        # .split(",")：按逗号切成 ["172.5", "65"]
        # 解包给 h, w（如果切出来的不是正好两个元素，会抛 ValueError → 被 except 接住）
        h, w = path.read_text(encoding="utf-8").strip().split(",")
        # float() 把字符串转成浮点数（"172.5" → 172.5）
        return {"height_cm": float(h), "weight_kg": float(w)}
    except (ValueError, OSError):
        # ValueError：内容不是"数字,数字"格式；OSError：文件读不了（权限等）
        # 这些情况都当作"没有档案"，不让它影响训练主流程
        return None


def load_csv_file(path: Path):
    """读一个 posture CSV → (X, y, 未标注丢弃数, 坏行数)

    参数：
        path：posture_*.csv 的完整路径
    返回：
        X：二维列表，每行12个压力值（整流前的原始值）
        y：一维列表，每行对应的标签（0/1/2）
        n_unlabeled：因为 label=-1（未标注）而丢弃的行数
        n_bad：格式坏掉而丢弃的行数（列数不对/不是数字/标签不在0~2）

    CSV 格式（第一行是表头）：
        timestamp,p0,p1,...,p11,label
        └─第0列─┘ └───第1~12列───┘ └第13列┘
    """
    X, y = [], []                         # 准备两个空列表装数据和标签
    n_unlabeled = 0                       # 计数：未标注行
    n_bad = 0                             # 计数：坏行
    # open(...) with：打开文件，块结束后自动关闭（不用手动 close）
    # newline=""：交给 csv 模块处理换行（跨平台正确解析）
    # encoding="utf-8"：文件是 UTF-8 编码
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)            # 创建一个"逐行读、按逗号切分"的读取器
        next(reader, None)                # 读掉第一行表头（next 拿到就扔；None=文件为空时不报错）
        for row in reader:                # 一次循环 = 读一行，row 是字符串列表
            if len(row) != 14:            # 必须是 1个时间戳 + 12个值 + 1个标签 = 14列
                n_bad += 1                # 列数不对 → 坏行，计数后跳过
                continue
            try:
                # 第1~12列（Python切片[1:13]含头不含尾）→ 转成整数列表
                vals = [int(v) for v in row[1:13]]
                # 第13列是标签 → 转成整数
                lab = int(row[13])
            except ValueError:
                # 有任何一列不是数字（比如手改坏了）→ 坏行
                n_bad += 1
                continue
            if lab == -1:                 # -1 = 未标注（采集时没按键/暂停前的数据）
                n_unlabeled += 1          # 计数（用于报告）
                continue                  # 丢弃：不参与训练
            if lab not in CLASS_NAMES:    # 标签必须是 0/1/2 之一
                n_bad += 1
                continue
            X.append(vals)                # 通过所有检查 → 收下这一行
            y.append(lab)
    return X, y, n_unlabeled, n_bad


def load_all():
    """扫描 ai/data/ 下所有数据集，返回 (X, y, 来源清单)

    目录结构约定：
        ai/data/
        ├── zdz/                      ← 一个数据集（一个人 / 一次归档）
        │   ├── body_data.txt
        │   ├── posture_0_....csv
        │   ├── posture_1_....csv
        │   └── ...
        └── （也兼容直接放在 ai/data/ 下的松散 posture_*.csv → 归到"(未分组)"）

    返回的"来源清单"结构：
        [(数据集名, body字典或None, [(文件名, 有效条数, 丢弃未标注数, 坏行数), ...]), ...]
        这个清单只用于打印和写报告（让你知道模型是吃什么数据训出来的）。
    """
    X, y = [], []                         # 汇总所有数据集的数据
    sources = []                          # 来源清单

    # 列出 DATA_ROOT 下的所有子文件夹（sorted 保证每次顺序一致）
    # DATA_ROOT.exists() 防御：目录不存在时 iterdir() 会抛异常
    subjects = [d for d in sorted(DATA_ROOT.iterdir()) if d.is_dir()] if DATA_ROOT.exists() else []

    # 也兼容"直接放在 ai/data/ 下的松散 posture_*.csv"（没有装进子文件夹的情况）
    loose = sorted(DATA_ROOT.glob("posture_*.csv"))

    # 把"子文件夹数据集"和"松散文件（归到根目录这一组）"合成一个待处理列表
    work_list = [(d, d.name) for d in subjects]     # 子文件夹：名字就是文件夹名
    if loose:
        work_list.append((DATA_ROOT, "(未分组)"))    # 松散文件：归到"(未分组)"

    for subject_dir, name in work_list:   # 逐个数据集处理
        body = parse_body_data(subject_dir / "body_data.txt")   # 读档案（可能为None）
        files = sorted(subject_dir.glob("posture_*.csv"))       # 该数据集下所有姿势CSV
        file_rows = []                    # 这个数据集的文件明细（用于报告）
        for f in files:                   # 逐个文件读
            fx, fy, nu, nb = load_csv_file(f)   # 返回 (数据, 标签, 丢弃统计)
            X.extend(fx)                  # extend：把列表里的元素逐个追加（不是追加整个列表）
            y.extend(fy)
            file_rows.append((f.name, len(fx), nu, nb))
        if file_rows:                     # 一个文件都没有的文件夹不登记
            sources.append((name, body, file_rows))

    return X, y, sources


def normalize_rows(X):
    """每帧除以该帧最大值 → 0~1（★必须和固件推理时保持一致）

    参数：
        X：numpy 二维数组，形状 (样本数, 12)
    返回：
        归一化后的二维数组（同形状）

    为什么用 axis=1, keepdims=True？
        axis=1：沿"列方向"求每一行的最大值（即每个样本自己的最大值）
        keepdims=True：结果保持二维，形状变成 (样本数, 1)
        这样 X / row_max 时，NumPy 的"广播"机制会让每一行都除以自己的最大值。
        如果不保维（形状变成 (样本数,)），广播方向会错，结果全错。

    为什么最大值小于1时强制设为1？
        防除零：如果一帧全是0（空床的极端情况），最大值是0，除法会炸。
        把0改成1后，0/1仍然是0，结果安全。
    """
    row_max = X.max(axis=1, keepdims=True)   # 每个样本自己的最大值（形状 (n,1)）
    row_max[row_max < 1] = 1                 # 布尔索引：把所有<1的最大值改成1（防除零）
    return X / row_max                       # 广播除法：每行除以自己那一个最大值


# ============================================================
# 主流程
# ============================================================
def main():
    # 打印分隔线，让终端输出清晰
    print("=" * 62)
    print("SleepPosture 训练流水线")
    print("=" * 62)

    # ---------- 1. 加载 ----------
    # 扫描全部数据集，得到：所有数据 X/y + 来源清单（用于打印和写报告）
    X_list, y_list, sources = load_all()
    if not X_list:                        # 一条数据都没有 → 报错退出（exit code 1）
        print("错误：在 ai/data/ 下没有找到任何训练数据（需要 posture_*.csv）")
        sys.exit(1)

    # 打印"数据清单"：每个数据集、每个人、每个文件、每个文件有多少条有效数据
    print("\n[数据清单]")
    for name, body, file_rows in sources:
        if body:                          # 有 body_data.txt：显示身高体重
            body_str = f"身高 {body['height_cm']}cm  体重 {body['weight_kg']}kg"
        else:
            body_str = "（没有 body_data.txt）"
        print(f"  数据集 {name}:  {body_str}")
        for fname, n, nu, nb in file_rows:
            extra = ""                    # 拼"丢弃信息"（有才显示）
            if nu:
                extra += f"，丢弃未标注 {nu} 条"
            if nb:
                extra += f"，丢弃坏行 {nb} 条"
            print(f"      {fname}:  {n} 条{extra}")

    # 列表 → numpy 数组（后续运算要向量化）
    # float32：TensorFlow 默认/推荐的浮点类型，显存/内存占用是 float64 的一半
    # int32：分类标签用整数（配合 sparse_categorical_crossentropy）
    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int32)

    # 打印总样本数和每个类别的数量（检查是否均衡：三类数量差太多会影响训练）
    print(f"\n[样本统计]  共 {len(X)} 条")
    for c, cname in CLASS_NAMES.items():  # 遍历 {0:仰卧, 1:左侧卧, 2:右侧卧}
        print(f"    {cname}({c}): {int((y == c).sum())} 条")   # (y==c) 是布尔数组，sum() 数 True 的个数

    # ---------- 2. 预处理 ----------
    # ★ 每帧除以该帧的最大值 → 映射到 0~1。
    #    这和固件 ai.cpp 推理时的处理必须完全一致（不然模型在板子上不准）。
    #    这就是"训练/推理一致性契约"的训练侧实现。
    X = normalize_rows(X)

    # ---------- 3. 划分训练/验证（分层，固定种子） ----------
    # rng = 固定种子的随机数生成器：同样的种子 → 同样的随机序列 → 可复现
    rng = np.random.default_rng(SEED)
    idx_train, idx_val = [], []           # 存"下标"（不是数据本身），后面统一切片

    # 分层抽样：每个类别内部单独划分，保证训练/验证集里三类的比例都和总体一致
    for c in CLASS_NAMES:                 # 对每个类别 c（0/1/2）：
        idx = np.where(y == c)[0]         # 取出"所有标签为c的样本"的下标数组
        rng.shuffle(idx)                  # 原地打乱这些下标（随机分）
        n_val = max(1, int(len(idx) * VAL_RATIO))   # 该类验证集条数；至少1条（防止类太小）
        idx_val.extend(idx[:n_val])       # 前 n_val 个 → 验证集
        idx_train.extend(idx[n_val:])     # 剩下的 → 训练集
    rng.shuffle(idx_train)                # 训练集下标再打乱一次（避免训练时按类别扎堆）

    # 用下标一次性把数组切出来（fancy indexing：X[下标数组] 得到对应行）
    X_tr, y_tr = X[idx_train], y[idx_train]
    X_val, y_val = X[idx_val], y[idx_val]
    print(f"\n[划分]  训练集 {len(X_tr)} 条 / 验证集 {len(X_val)} 条"
          f"（验证集比例 {VAL_RATIO:.0%}，种子 {SEED}）")

    # ---------- 4. 搭模型 ----------
    # 固定 TensorFlow 的随机种子（影响参数初始化的随机性 → 结果可复现）
    tf.random.set_seed(SEED)

    # Sequential：按顺序堆叠的模型（一层输出接下一层输入）
    model = tf.keras.Sequential([
        # 第1层：全连接，16个神经元，ReLU激活。
        # input_shape=(12,)：告诉模型输入是12维向量（只写在第1层）
        # 参数量 = 12*16(权重) + 16(偏置) = 208
        tf.keras.layers.Dense(16, activation="relu", input_shape=(12,)),
        # 第2层：全连接16个神经元（输入自动是上一层的16）
        # 参数量 = 16*16 + 16 = 272
        tf.keras.layers.Dense(16, activation="relu"),
        # 第3层（输出层）：3个神经元对应三个类别，Softmax把3个输出变成概率（和为1）
        # 参数量 = 16*3 + 3 = 51
        tf.keras.layers.Dense(3, activation="softmax"),
    ])                                    # 总参数量 208+272+51 = 531
    model.summary()                       # 打印每层形状和参数量（读输出可以核对上面的算式）

    # 编译：指定"怎么学"
    #   optimizer="adam"：自适应学习率的优化器（不用手动调学习率）
    #   loss="sparse_categorical_crossentropy"：分类损失。
    #       "sparse" 表示标签是整数（0/1/2）；如果标签是 one-hot（[1,0,0]）就用 categorical_...
    #   metrics=["accuracy"]：训练时额外打印准确率（不影响训练，只是给人看）
    model.compile(optimizer="adam",
                  loss="sparse_categorical_crossentropy",
                  metrics=["accuracy"])

    # ---------- 5. 训练 ----------
    # EarlyStopping（提前停止）：防止过拟合、也省时间
    #   monitor="val_loss"：盯着验证集损失
    #   patience=20：连续20轮没有改善就停
    #   restore_best_weights=True：停止后恢复到"验证损失最低"的那一轮参数（不是最后一轮）
    early = tf.keras.callbacks.EarlyStopping(
        monitor="val_loss", patience=20, restore_best_weights=True)

    # fit = 训练主循环：按 batch 反复"前向→算损失→反向传播→更新权重"
    model.fit(X_tr, y_tr,
              epochs=EPOCHS,              # 最多把训练集看100遍
              batch_size=BATCH_SIZE,      # 每批32条
              validation_data=(X_val, y_val),  # 每轮结束在验证集上算一次指标
              callbacks=[early],          # 挂上提前停止
              verbose=1)                  # 1=显示进度条

    # ---------- 6. 评估 ----------
    # evaluate：在验证集上算损失和准确率（不训练，只前向计算）
    val_loss, val_acc = model.evaluate(X_val, y_val, verbose=0)
    print(f"\n[评估]  验证集准确率: {val_acc:.4f}   验证损失: {val_loss:.4f}")

    # predict：对验证集每个样本输出3个类别的概率；argmax(axis=1) 取出概率最大的类别下标
    y_pred = np.argmax(model.predict(X_val, verbose=0), axis=1)

    # 手工构建混淆矩阵：cm[真实类别][预测类别] += 1
    # 行=真实，列=预测；对角线上的数字=预测正确的数量
    cm = np.zeros((3, 3), dtype=int)
    for t, p in zip(y_val, y_pred):       # zip：把真实值和预测值配对逐个遍历
        cm[t][p] += 1

    # 打印混淆矩阵（带表头对齐）
    print("\n[混淆矩阵]  行=真实，列=预测")
    print("          " + "".join(f"{CLASS_NAMES[c]:>8}" for c in CLASS_NAMES))
    for c in CLASS_NAMES:
        row = "".join(f"{cm[c][k]:>8}" for k in CLASS_NAMES)
        print(f"  {CLASS_NAMES[c]:>6}  {row}")

    # 计算每类的精确率(precision)/召回率(recall)
    #   tp（真正例）= 对角线：真c且预测c
    #   fp（假正例）= 第c列总和 - tp：别人被错判成c
    #   fn（假负例）= 第c行总和 - tp：c被错判成别人
    #   precision = tp/(tp+fp)：说成c的样本里有多少真的是c（"报得准不准"）
    #   recall    = tp/(tp+fn)：真正的c里有多少被报出来了（"漏没漏"）
    print("\n[每类指标]")
    report_lines = []                     # 存下来，稍后写进报告文件
    for c in CLASS_NAMES:
        tp = cm[c][c]
        fp = cm[:, c].sum() - tp          # 第c列所有行之和 减去 tp
        fn = cm[c, :].sum() - tp          # 第c行所有列之和 减去 tp
        # 防御：分母为0时（该类没有样本/没有预测）直接记0，避免除零
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        line = f"  {CLASS_NAMES[c]}: 精确率 {prec:.3f}  召回率 {rec:.3f}"
        print(line)
        report_lines.append(line)         # 收集给报告用

    # ---------- 7. 保存模型 + 报告 ----------
    # mkdir(parents=True)：父目录不存在也一起创建；exist_ok=True：已存在不报错
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    # 文件名加时间戳（到分钟）：每次训练产出不同文件，不会被覆盖
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    model_path = MODEL_DIR / f"posture_model_{ts}.keras"
    model.save(model_path)                # 保存完整模型（结构+权重+优化器状态，可继续训练）
    print(f"\n[保存]  模型: {model_path}")

    # 报告文件：把这次训练的全部上下文写下来（下次对比、写简历/答辩都能用）
    report_path = MODEL_DIR / f"posture_model_{ts}_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        # 标题行：时间
        f.write(f"SleepPosture 训练报告  {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write("=" * 50 + "\n\n")
        # 数据来源：每个数据集的人（身高体重）+ 文件明细
        f.write("[数据来源]\n")
        for name, body, file_rows in sources:
            body_str = (f"身高 {body['height_cm']}cm 体重 {body['weight_kg']}kg"
                        if body else "无body_data")
            f.write(f"  数据集 {name} ({body_str})\n")
            for fname, n, nu, nb in file_rows:
                f.write(f"    {fname}: {n} 条 (丢弃未标注 {nu} 条, 坏行 {nb} 条)\n")
        # 样本统计
        f.write(f"\n[样本] 共 {len(X)} 条\n")
        for c, cname in CLASS_NAMES.items():
            f.write(f"  {cname}({c}): {int((y == c).sum())} 条\n")
        # 划分信息 + 结果
        f.write(f"\n[划分] 训练 {len(X_tr)} / 验证 {len(X_val)} (种子 {SEED})\n")
        f.write(f"[结果] 验证准确率 {val_acc:.4f}, 验证损失 {val_loss:.4f}\n\n")
        # 混淆矩阵
        f.write("[混淆矩阵] 行=真实 列=预测\n")
        f.write("        " + "".join(f"{CLASS_NAMES[c]:>8}" for c in CLASS_NAMES) + "\n")
        for c in CLASS_NAMES:
            f.write(f"{CLASS_NAMES[c]:>6}  "
                    + "".join(f"{cm[c][k]:>8}" for k in CLASS_NAMES) + "\n")
        # 每类指标
        f.write("\n[每类指标]\n")
        for line in report_lines:
            f.write(line + "\n")
        # 诚实的备注：说明当前评估偏乐观的原因
        f.write("\n[备注] 当前每类只有一段录制，验证集与训练集同源，准确率偏乐观。\n")
    print(f"[保存]  报告: {report_path}")

    # 收尾提示：下一步做什么
    print("\n下一步：")
    print("  - 以后新增数据：往 ai/data/ 放新文件夹（配 body_data.txt），重跑本脚本即可")
    print("  - 部署转换：.keras → 量化 .tflite → C数组（convert.py / to_c_array.py）")


# Python 的入口判断：只有"直接运行本文件"时才执行 main()；
# 被 convert.py 用 import 导入时（为了复用 load_all/normalize_rows）不会自动跑训练。
if __name__ == "__main__":
    main()
