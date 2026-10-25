#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_core.py — 数据采集共享核心

为什么有这个文件：
    电脑上现在有两个采集入口：
        1. collect_data.py —— tkinter 界面（键盘 0/1/2 + 空格）
        2. web_collect.py  —— 手机网页遥控（浏览器点按钮）
    两者必须用"同一套采集逻辑"（连接、收帧、批次缓冲、CSV格式），
    否则两份实现迟早会不一致。所以把核心抽到这里，两个界面都 import 它。

核心职责（Collector 类）：
    - 连接 ESP32 的 TCP 服务器（3333），断线自动重连
    - 解析 DATA:时间戳,p0..p11 行，更新最新帧/帧率统计
    - 批次采集：begin_burst() 开始缓冲 → 每帧带当前标签 → end_burst() 取出
    - 保存 CSV：save_rows(rows, subject) → ai/data/<subject>/posture_标签_时间.csv

线程模型：
    - 一个后台线程负责收数据（_recv_loop）
    - 界面线程（tkinter/HTTP）通过 status() 读快照、通过 msg_q 收事件
    - 批次缓冲/标签用 threading.Lock 保护（收帧线程写、界面线程读/切换）
"""

import queue
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# ============================================================
# 公共参数
# ============================================================
ESP32_IP = "192.168.11.110"   # 默认IP（以串口打印为准；建议在路由器里做"地址保留"绑定MAC）
ESP32_PORT = 3333             # 与固件 config.h 的 TCP_SERVER_PORT 一致
RECONNECT_DELAY_S = 2.0       # 断线后等几秒再重连

# 项目根目录 = 本文件(tools/)的上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "ai" / "data"

# 标签体系：0=仰卧，1=左侧卧，2=右侧卧，-1=未标注
POSTURE_NAMES = {0: "仰卧", 1: "左侧卧", 2: "右侧卧", -1: "未标注"}

# 4×3 网格名字（与接线表一致：C0=肩左 … C11=臀右）
GRID_NAMES = [
    "肩左 C0", "肩中 C1", "肩右 C2",
    "背左 C3", "背中 C4", "背右 C5",
    "腰左 C6", "腰中 C7", "腰右 C8",
    "臀左 C9", "臀中 C10", "臀右 C11",
]


# ============================================================
# 协议解析与CSV写出（纯函数，方便测试）
# ============================================================
def parse_line(raw: bytes):
    """解析一行：DATA:时间戳,p0..p11 → (时间戳, [12个值])；格式不对返回 None"""
    text = raw.decode("utf-8", errors="ignore").strip()
    if not text.startswith("DATA:"):
        return None
    parts = text[5:].split(",")
    if len(parts) != 13:          # 1个时间戳 + 12个值
        return None
    try:
        ts = int(parts[0])
        vals = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    return ts, vals


def write_rows(path: Path, rows):
    """把 [(ts, [12个值], label), ...] 写成 CSV。

    格式：timestamp,p0,...,p11,label（表头全英文，避免Excel乱码）
    """
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp"] + [f"p{i}" for i in range(12)] + ["label"])
        for ts, vals, lab in rows:
            w.writerow([ts] + list(vals) + [lab])


def safe_subject(subject: str) -> str:
    """把"数据集名"清洗成安全的文件夹名（防止路径穿越/非法字符）"""
    subject = (subject or "").strip()
    subject = subject.replace("\\", "_").replace("/", "_")
    return subject


# ============================================================
# Collector：采集核心
# ============================================================
class Collector:
    """连接ESP32、收帧、批次采集、存CSV。所有对外方法可跨线程调用。"""

    def __init__(self, ip: str = ESP32_IP, port: int = ESP32_PORT,
                 emit_events: bool = True):
        """
        参数：
            ip/port       ESP32 地址（测试时可指向 127.0.0.1）
            emit_events   True：把 status/frame 事件放进 msg_q 给界面轮询；
                          False：不发事件（网页版直接读 status() 快照，省内存）
        """
        self.ip = ip
        self.port = port
        self.emit_events = emit_events

        # 事件队列：工作线程 → 界面线程（只存事件，不存大块数据）
        self.msg_q = queue.Queue()

        # ---- 连接/统计（工作线程写，界面读；单变量读写足够安全）----
        self.stop = False
        self.reconnect_requested = False
        self.connected = False
        self.frames = 0                # 累计收到的帧数（本次会话）
        self.last_vals = None          # 最近一帧的12个值
        self.last_ts = None            # 最近一帧的ESP32时间戳
        self.stamps = deque(maxlen=30) # 最近30帧的到达时间（算帧率）

        # ---- 批次采集（受锁保护）----
        self._label = -1               # 当前标签
        self._recording = False        # 是否在采集批次中
        self._burst = []               # 本批次数据 [(ts, vals, label), ...]
        self._lock = threading.Lock()

        self._worker = None

    # ==================== 生命周期 ====================
    def start(self):
        """启动后台收数线程（只调用一次）"""
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._recv_loop, daemon=True)
        self._worker.start()

    def shutdown(self):
        """请求退出（后台线程会在1秒内结束）"""
        self.stop = True

    # ==================== 连接控制 ====================
    def set_ip(self, ip: str):
        """换目标IP并立即触发重连（空字符串忽略）"""
        ip = (ip or "").strip()
        if not ip:
            return
        self.ip = ip
        self.reconnect_requested = True

    def request_reconnect(self):
        """请求立刻断开重连（IP没变也重连）"""
        self.reconnect_requested = True

    # ==================== 标签 ====================
    @property
    def label(self) -> int:
        with self._lock:
            return self._label

    @label.setter
    def label(self, value: int):
        with self._lock:
            self._label = int(value)

    # ==================== 批次采集 ====================
    def begin_burst(self) -> bool:
        """开始一次采集批次。已在采集中则返回 False。"""
        with self._lock:
            if self._recording:
                return False
            self._recording = True
            self._burst = []
            return True

    def end_burst(self):
        """结束批次，返回 [(ts, vals, label), ...] 并清空缓冲。"""
        with self._lock:
            rows = self._burst
            self._burst = []
            self._recording = False
        return rows

    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    # ==================== 保存 ====================
    def save_rows(self, rows, subject: str = "") -> Path:
        """把一批数据写成 CSV，返回文件路径。

        路径：
            ai/data/<subject>/posture_<标签>_<时间>.csv
            （subject 为空 → 直接放 ai/data/ 下）
        文件名里的标签用"本批最后一条的标签"；-1 记作 x。
        """
        subject = safe_subject(subject)
        out_dir = DATA_DIR / subject if subject else DATA_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        lab = rows[-1][2] if rows else -1
        labname = str(lab) if lab >= 0 else "x"
        fname = out_dir / f"posture_{labname}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        write_rows(fname, rows)
        return fname

    # ==================== 状态快照 ====================
    def fps(self) -> float:
        """最近30帧的帧率"""
        if len(self.stamps) > 1:
            span = self.stamps[-1] - self.stamps[0]
            if span > 0:
                return (len(self.stamps) - 1) / span
        return 0.0

    def status(self) -> dict:
        """给界面/网页用的一站式状态快照（加锁读批次信息）"""
        with self._lock:
            recording = self._recording
            n = len(self._burst)
            first_ts = self._burst[0][0] if n else None
            last_burst_ts = self._burst[-1][0] if n else None
            label = self._label

        burst_seconds = 0.0
        if n and last_burst_ts is not None and first_ts is not None and last_burst_ts >= first_ts:
            burst_seconds = (last_burst_ts - first_ts) / 1000.0

        return {
            "connected": self.connected,
            "ip": self.ip,
            "frames": self.frames,
            "fps": round(self.fps(), 1),
            "recording": recording,
            "label": label,
            "label_name": POSTURE_NAMES.get(label, "?"),
            "burst_rows": n,
            "burst_seconds": round(burst_seconds, 1),
            "last_vals": list(self.last_vals) if self.last_vals else [],
        }

    # ==================== 内部：收数线程 ====================
    def _emit(self, *msg):
        """向界面线程发事件（emit_events=False 时不发）"""
        if self.emit_events:
            self.msg_q.put(msg)

    def _handle_frame(self, ts, vals):
        """处理一帧：更新统计；如果正在采集则追加进批次（带当前标签）"""
        with self._lock:
            self.frames += 1
            self.last_ts = ts
            self.last_vals = vals
            if self._recording:
                self._burst.append((ts, list(vals), self._label))
        self.stamps.append(time.monotonic())
        self._emit("frame", ts, vals)

    def _recv_loop(self):
        """工作线程：连接 → 按行收帧 → 断线自动重连（无限循环）"""
        while not self.stop:
            # 还没有设置IP（网页版等用户填写）：不尝试连接，安静等待
            if not self.ip:
                self.connected = False
                self._emit("status", "waiting", "")
                time.sleep(0.5)
                continue

            ip = self.ip
            self.reconnect_requested = False   # 开始新一轮连接，清掉旧的重连请求
            self.connected = False
            self._emit("status", "connecting", ip)

            sock = None
            try:
                # 连接ESP32（超时5秒）；之后recv用0.5秒超时，便于周期性检查退出/重连标志
                sock = socket.create_connection((ip, self.port), timeout=5)
                sock.settimeout(0.5)
                self.connected = True
                self._emit("status", "connected", ip)

                buf = b""   # TCP是字节流：一行可能分多段到达，也可能一次到达多行
                closed_reason = "连接被关闭"
                while not self.stop:
                    if self.reconnect_requested:
                        self.reconnect_requested = False
                        closed_reason = "手动重连"
                        break
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:            # 空 = 对端关闭
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        parsed = parse_line(line)
                        if parsed:
                            self._handle_frame(parsed[0], parsed[1])

                self._emit("status", "disconnected", closed_reason)
            except Exception as e:
                self._emit("status", "disconnected", f"{e}")
            finally:
                self.connected = False
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if not self.stop:
                time.sleep(RECONNECT_DELAY_S)
