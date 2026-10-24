#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
idle_sum.py — 空载压力总和"漂移观察"工具

用途：
    床垫上什么都不放，观察"12路压力总和"随时间是否漂移（10分钟起步）。
    如果总和稳定 → 不需要做"基线校准"，可以直接采训练数据；
    如果缓慢上升/下降 → 需要做基线校准，我们再在固件里加。

界面：
    1. 顶部：连接状态 + ESP32 IP（可重连）
    2. 中间：当前总和（大字）+ 当前12路原始值 + 实时曲线（自动缩放大小的）
    3. 底部：[开始采集空载压力总和] → 采集期间随便放多久 → [结束并保存CSV]

输出：
    ai/data/idle_sum_日期_时间.csv
    格式：index,timestamp,p0,p1,...,p11,sum  （表头全英文，避免Excel乱码）
        index       从1开始
        timestamp   开机毫秒
        p0~p11      12路原始值（C0=肩左 … C11=臀右）
        sum         12路之和
    拉线：X轴用index或timestamp，Y轴用sum

用法：
    python idle_sum.py                  # 用默认IP
    python idle_sum.py 192.168.11.100   # 指定IP
"""

import csv
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

# ============================================================
# 可调参数
# ============================================================
ESP32_IP = "192.168.11.100"   # 默认IP（看串口"获取IP"那行）
ESP32_PORT = 3333             # 和固件 config.h 的 TCP_SERVER_PORT 一致
RECONNECT_DELAY_S = 2.0       # 断线后等几秒再重连
LIVE_POINTS = 600             # 不采集时，曲线显示最近多少帧（10Hz下=60秒）

# 项目根目录 = 本文件(tools/)的上一级；数据存到 ai/data/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "ai" / "data"

# 4×3 网格名字（和接线表一致：C0=肩左 … C11=臀右）
GRID_NAMES = [
    "肩左 C0", "肩中 C1", "肩右 C2",
    "背左 C3", "背中 C4", "背右 C5",
    "腰左 C6", "腰中 C7", "腰右 C8",
    "臀左 C9", "臀中 C10", "臀右 C11",
]

# ============================================================
# 线程间通信（和 collect_data.py 相同的结构）
# ============================================================
msg_q = queue.Queue()


class State:
    def __init__(self):
        self.stop = False                 # 请求退出
        self.reconnect_requested = False  # 请求立刻重连
        self.ip = ESP32_IP                # 目标IP


state = State()


# ============================================================
# 工作线程：连接 → 收数据 → 断线自动重连
# ============================================================
def recv_worker():
    while not state.stop:
        ip = state.ip
        msg_q.put(("status", "connecting", ip))

        sock = None
        try:
            sock = socket.create_connection((ip, ESP32_PORT), timeout=5)
            sock.settimeout(0.5)
            msg_q.put(("status", "connected", ip))

            buf = b""
            closed_reason = "连接被关闭"
            while not state.stop:
                if state.reconnect_requested:
                    state.reconnect_requested = False
                    closed_reason = "手动重连"
                    break
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    parsed = parse_line(line)
                    if parsed:
                        msg_q.put(("frame", parsed[0], parsed[1]))

            msg_q.put(("status", "disconnected", closed_reason))
        except Exception as e:
            msg_q.put(("status", "disconnected", f"{e}"))
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

        if not state.stop:
            time.sleep(RECONNECT_DELAY_S)


def parse_line(raw: bytes):
    """解析一行：DATA:时间戳,p0..p11 → (时间戳, [12个值])；格式不对返回None"""
    text = raw.decode("utf-8", errors="ignore").strip()
    if not text.startswith("DATA:"):
        return None
    parts = text[5:].split(",")
    if len(parts) != 13:
        return None
    try:
        ts = int(parts[0])
        vals = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    return ts, vals


def write_csv(path: Path, rows):
    """把 [(ts, [12个值], sum), ...] 写成CSV（独立函数，方便测试）

    格式：index,timestamp,p0,...,p11,sum（index从1开始）
    表头全英文：中文表头在Excel里会乱码（Excel按GBK读UTF-8的CSV）
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["index", "timestamp"] + [f"p{i}" for i in range(12)] + ["sum"])
        for idx, (ts, vals, s) in enumerate(rows, start=1):
            w.writerow([idx, ts] + list(vals) + [s])


# ============================================================
# 主界面
# ============================================================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("空载压力总和 · 漂移观察")
        root.configure(bg="#1e1e1e")

        # ---------- 采样状态 ----------
        self.recording = False            # 是否正在"采集空载总和"
        self.session = []                 # 本次采集的 [(ts, sum), ...]
        self.session_min = None           # 本次采集的 sum 最小/最大（看漂移幅度）
        self.session_max = None
        self.live_sums = []               # 不采集时的实时滚动窗口（画图用）

        # ---------- 第一行：状态 + IP ----------
        top = tk.Frame(root, bg="#1e1e1e")
        top.pack(fill="x", padx=10, pady=(10, 4))

        self.status_label = tk.Label(top, text="未连接", bg="#333", fg="white",
                                     font=("Microsoft YaHei", 11), padx=10, pady=4)
        self.status_label.pack(side="left")

        tk.Label(top, text="ESP32 IP:", bg="#1e1e1e", fg="#aaa",
                 font=("Microsoft YaHei", 10)).pack(side="left", padx=(14, 2))
        self.ip_var = tk.StringVar(value=state.ip)
        tk.Entry(top, textvariable=self.ip_var, width=14,
                 font=("Consolas", 11)).pack(side="left")
        tk.Button(top, text="重连", command=self.on_reconnect_click).pack(side="left", padx=6)

        # ---------- 第二行：当前总和（大字） ----------
        mid = tk.Frame(root, bg="#1e1e1e")
        mid.pack(fill="x", padx=10)

        self.sum_label = tk.Label(mid, text="总和: --", bg="#1e1e1e", fg="#ffd54f",
                                  font=("Consolas", 28, "bold"))
        self.sum_label.pack(side="left")

        # 12路小网格：每个点位显示"名字 + 实时数值"，
        # 能直接看出是哪一路在漂
        grid_frame = tk.Frame(mid, bg="#1e1e1e")
        grid_frame.pack(side="left", padx=20)
        self.cell_values = []   # 12个数值Label，收到帧就更新
        for i in range(12):
            r, c = divmod(i, 3)
            cell = tk.Frame(grid_frame, bg="#101010", bd=1, relief="ridge")
            cell.grid(row=r, column=c, padx=2, pady=2)
            tk.Label(cell, text=GRID_NAMES[i], bg="#101010", fg="#777",
                     font=("Microsoft YaHei", 8)).pack(fill="x")
            vlab = tk.Label(cell, text="0", bg="#101010", fg="#ddd",
                            font=("Consolas", 12, "bold"), width=5)
            vlab.pack()
            self.cell_values.append(vlab)

        # ---------- 第三行：曲线 ----------
        self.canvas = tk.Canvas(root, width=820, height=220,
                                bg="#101010", highlightthickness=1,
                                highlightbackground="#333")
        self.canvas.pack(padx=10, pady=6)

        self.range_label = tk.Label(root, text="（曲线Y轴自动缩放；数值看上面的“总和”）",
                                    bg="#1e1e1e", fg="#666", font=("Microsoft YaHei", 9))
        self.range_label.pack()

        # ---------- 第四行：采集按钮 + 本次统计 ----------
        ctl = tk.Frame(root, bg="#1e1e1e")
        ctl.pack(fill="x", padx=10, pady=6)

        self.start_btn = tk.Button(ctl, text="开始采集空载压力总和",
                                   command=self.start_recording,
                                   bg="#2d7ef7", fg="white",
                                   font=("Microsoft YaHei", 12, "bold"), padx=16, pady=6)
        self.start_btn.pack(side="left")

        self.stop_btn = tk.Button(ctl, text="结束并保存CSV",
                                  command=self.stop_recording,
                                  bg="#d32f2f", fg="white",
                                  font=("Microsoft YaHei", 12, "bold"), padx=16, pady=6,
                                  state="disabled")
        self.stop_btn.pack(side="left", padx=10)

        self.session_label = tk.Label(ctl, text="未开始", bg="#1e1e1e", fg="#9ad",
                                      font=("Consolas", 11))
        self.session_label.pack(side="left", padx=12)

        tk.Label(root, text="用法：什么都不放 → 点开始 → 放10分钟 → 点结束。CSV存到 ai/data/",
                 bg="#1e1e1e", fg="#888", font=("Microsoft YaHei", 10)).pack(pady=(0, 10))

        # ---------- 收尾 ----------
        root.bind("<Key>", self.on_key)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(40, self.poll)

    # ==================== 按钮/按键 ====================
    def on_key(self, event):
        # 输入框里打字时不抢键
        if isinstance(self.root.focus_get(), tk.Entry):
            return
        if event.keysym == "space":
            if self.recording:
                self.stop_recording()
            else:
                self.start_recording()

    def on_reconnect_click(self):
        new_ip = self.ip_var.get().strip()
        if new_ip:
            state.ip = new_ip
        state.reconnect_requested = True

    def start_recording(self):
        self.session = []
        self.session_min = None
        self.session_max = None
        self.recording = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.session_label.config(text="采集中：0 行 / 0 秒")

    def stop_recording(self):
        """结束采集：写CSV + 弹提示"""
        self.finish_recording(show_msg=True)

    def finish_recording(self, show_msg: bool):
        if not self.recording:
            return
        self.recording = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

        rows = self.session
        self.session_label.config(text=f"已结束（{len(rows)} 行）")

        if not rows:
            return

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fname = DATA_DIR / f"idle_sum_{datetime.now():%Y%m%d_%H%M%S}.csv"
        write_csv(fname, rows)

        if show_msg:
            from tkinter import messagebox
            msg = (f"已保存 {len(rows)} 行\n\n"
                   f"总和范围: {int(self.session_min)} ~ {int(self.session_max)}\n"
                   f"（漂移幅度 = {int(self.session_max - self.session_min)}）\n\n"
                   f"文件:\n{fname}")
            messagebox.showinfo("采集完成", msg)

    def on_close(self):
        # 采集中直接关窗口：先把数据存下来，不丢
        if self.recording:
            self.finish_recording(show_msg=False)
        state.stop = True
        self.root.destroy()

    # ==================== 定时轮询消息队列 ====================
    def poll(self):
        try:
            while True:
                msg = msg_q.get_nowait()
                if msg[0] == "status":
                    _, what, info = msg
                    self.set_status(what, info)
                elif msg[0] == "frame":
                    _, ts, vals = msg
                    self.on_frame(ts, vals)
        except queue.Empty:
            pass
        self.root.after(40, self.poll)

    def on_frame(self, ts, vals):
        s = sum(vals)   # ★ 核心：12个值加在一起

        # 顶部大字 + 12路小网格
        self.sum_label.config(text=f"总和: {s}")
        for i, v in enumerate(vals):
            self.cell_values[i].config(text=str(v))

        # 滚动窗口（不采集时曲线显示这个）
        self.live_sums.append(s)
        if len(self.live_sums) > LIVE_POINTS:
            self.live_sums.pop(0)

        # 采集状态：存数据 + 统计
        if self.recording:
            self.session.append((ts, vals, s))   # (时间戳, 12路原始值, 总和)
            if self.session_min is None or s < self.session_min:
                self.session_min = s
            if self.session_max is None or s > self.session_max:
                self.session_max = s

            n = len(self.session)
            secs = (ts - self.session[0][0]) / 1000.0   # 用ESP32时间戳算时长
            self.session_label.config(
                text=f"采集中：{n} 行 / {secs:.0f} 秒 / 范围 {int(self.session_min)}~{int(self.session_max)}")

        # 画曲线（注意：session的行是 (时间戳, 12路值, 总和)，总和在下标2）
        sums = [r[2] for r in self.session] if self.recording else self.live_sums
        self.draw_chart(sums)

    # ==================== 界面刷新 ====================
    def set_status(self, what, info):
        if what == "connecting":
            self.status_label.config(text=f"重连中… ({info})", bg="#8a6d00")
        elif what == "connected":
            self.status_label.config(text=f"已连接 ({info})", bg="#1b7a1b")
        else:
            self.status_label.config(
                text=f"未连接: {info[:40]}（{RECONNECT_DELAY_S:.0f}秒后自动重连）",
                bg="#8b0000")

    def draw_chart(self, sums):
        """画一条折线：X=时间，Y=总和（自动缩放）"""
        c = self.canvas
        c.delete("all")
        if len(sums) < 2:
            return

        # 抽稀：点太多时每隔几个取一个（画1000个点就够看了）
        step = max(1, len(sums) // 1000)
        pts = sums[::step]

        lo = min(pts)
        hi = max(pts)
        if hi - lo < 1:          # 完全平的线：撑开一点，否则除法没法算
            lo -= 1
            hi += 1
        pad = (hi - lo) * 0.1
        lo -= pad
        hi += pad

        w, h = 820, 220
        n = len(pts)
        coords = []
        for i, v in enumerate(pts):
            x = i * w / (n - 1)
            y = h - (v - lo) / (hi - lo) * h
            coords.extend([x, y])
        c.create_line(*coords, fill="#4da3ff", width=2)


# ============================================================
# 入口
# ============================================================
def main():
    if len(sys.argv) > 1:
        state.ip = sys.argv[1]

    root = tk.Tk()
    App(root)

    worker = threading.Thread(target=recv_worker, daemon=True)
    worker.start()

    root.mainloop()


if __name__ == "__main__":
    main()
