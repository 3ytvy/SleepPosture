#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_data.py — SleepPosture 数据采集工具（电脑键盘版：可视化 + 打标签 + 存CSV）

功能：
    1. 连接ESP32的TCP服务器，实时接收12路压力数据
    2. 4×3 热力图显示（颜色 + 数值）
    3. 键盘打标签：0=仰卧 1=左侧卧 2=右侧卧，x=未标注
    4. 支持"定时采集"：勾选后按空格 → 倒计时5秒 → 自动开始 →
       时间到了响铃3秒 → 自动结束 → 弹保存确认窗口
    5. 断线自动重连

【架构】数据层已抽到 collect_core.py，本文件只负责 tkinter 界面与交互。
    另一个入口 web_collect.py（手机网页遥控）使用同一个核心——
    两个入口的采集行为、CSV格式、批次逻辑完全一致。
    ⚠ 两个入口不要同时运行：ESP32 的TCP服务器只服务一个客户端，
      后连上的会把先连上的踢掉。

用法：
    python tools/collect_data.py                  # 用默认IP
    python tools/collect_data.py 192.168.11.110   # 指定IP

按键：
    0 / 1 / 2   切换当前标签（0=仰卧，1=左侧卧，2=右侧卧）
    x           标签置为 -1（未标注）
    空格        开始 / 结束 一次采集（打开后默认待机，不会自动记录；
                结束时弹"确认保存"窗口；倒计时期间按空格=取消）

输出：
    每次确认保存 → 单独一个文件：
        ai/data/posture_<标签>_日期_时间.csv
    格式：timestamp,p0,p1,...,p11,label  （表头全英文，避免Excel乱码）
    （丢弃 → 不写文件；关窗口时若正在采集中 → 自动保存，不弹窗）
"""

import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

try:
    import winsound   # Windows专用：响铃用（系统自带）
except ImportError:
    winsound = None

# 引入共享采集核心（与 web_collect.py 共用同一套逻辑）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_core as core      # noqa: E402

# ============================================================
# 可调参数（数据层参数在 collect_core.py 里）
# ============================================================
MAX_VALUE = 5000              # 颜色标尺：数值到这个值就是最红（只影响颜色显示）
PREPARE_SECONDS = 5           # 定时模式：按空格后等几秒才开始（躺好/摆姿势用的缓冲）
DEFAULT_DURATION_S = 120      # 定时模式：默认采集时长（秒）

# 这些名字从核心模块转出来用，保持本文件里代码可读性
ESP32_IP = core.ESP32_IP
ESP32_PORT = core.ESP32_PORT
RECONNECT_DELAY_S = core.RECONNECT_DELAY_S
DATA_DIR = core.DATA_DIR
POSTURE_NAMES = core.POSTURE_NAMES
GRID_NAMES = core.GRID_NAMES

# ============================================================
# 采集核心实例与线程间通信
# ============================================================
# 全局唯一的采集器：负责连ESP32、收帧、批次缓冲
# emit_events=True：每帧/连接状态变化都会放进 msg_q，供界面轮询
state = core.Collector(emit_events=True)
msg_q = state.msg_q           # 工作线程 → 主线程 的消息队列


def play_ring():
    """3秒铃声（在后台线程响，不卡界面）：三连音循环3遍"""
    if winsound is None:
        return

    def _beep():
        try:
            for _ in range(3):
                winsound.Beep(880, 300)
                winsound.Beep(660, 300)
                winsound.Beep(880, 300)
        except RuntimeError:
            pass   # 有些环境没有蜂鸣器，忽略

    threading.Thread(target=_beep, daemon=True).start()


# ============================================================
# 主界面
# ============================================================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("SleepPosture 数据采集")
        root.configure(bg="#1e1e1e")

        # ---------- 采集状态机（界面的三种状态；批次数据在 core 里） ----------
        #   idle      ：待机（没在采集）
        #   prepare   ：定时模式的倒计时中（还没开始采集）
        #   recording ：采集中（core.begin_burst() 已调用）
        self.phase = "idle"
        self.burst_duration = None      # 本次定时时长（秒）；None=手动模式不限时
        self.countdown_remain = 0       # 倒计时剩余秒数
        self.countdown_timer = None     # 倒计时用的 after 定时器
        self.burst_timer = None         # 定时结束用的 after 定时器

        # ---------- 统计变量 ----------
        self.received = 0                 # 本次会话收到的总帧数（不管存不存）
        self.saved = 0                    # 已确认保存的总条数
        self.label_counts = {0: 0, 1: 0, 2: 0, -1: 0}   # 各标签的已保存条数
        self.stamps = []                  # 最近若干帧的到达时间（算帧率用）

        # ---------- 第一行：连接状态 / IP / 标签显示 ----------
        top = tk.Frame(root, bg="#1e1e1e")
        top.pack(fill="x", padx=10, pady=(10, 4))

        self.status_label = tk.Label(top, text="未连接", bg="#333", fg="white",
                                     font=("Microsoft YaHei", 11), padx=10, pady=4)
        self.status_label.pack(side="left")

        tk.Label(top, text="ESP32 IP:", bg="#1e1e1e", fg="#aaa",
                 font=("Microsoft YaHei", 10)).pack(side="left", padx=(14, 2))
        self.ip_var = tk.StringVar(value=state.ip)
        self.ip_entry = tk.Entry(top, textvariable=self.ip_var, width=14,
                                 font=("Consolas", 11))
        self.ip_entry.pack(side="left")
        self.ip_entry.bind("<Return>", lambda e: self.root.focus_set())

        tk.Button(top, text="重连", command=self.on_reconnect_click).pack(side="left", padx=6)

        # 当前标签（大号显示，颜色区分）
        self.label_display = tk.Label(top, text="当前标签: -1 (未标注)",
                                      bg="#555", fg="white",
                                      font=("Microsoft YaHei", 14, "bold"),
                                      padx=14, pady=4)
        self.label_display.pack(side="right")

        self.rec_display = tk.Label(top, text="⏸ 待机", bg="#444", fg="white",
                                    font=("Microsoft YaHei", 11, "bold"),
                                    padx=10, pady=4)
        self.rec_display.pack(side="right", padx=8)

        # ---------- 第二行：定时采集选项 ----------
        opt = tk.Frame(root, bg="#1e1e1e")
        opt.pack(fill="x", padx=10, pady=2)

        self.timed_var = tk.BooleanVar(value=False)
        tk.Checkbutton(opt, text="定时采集", variable=self.timed_var,
                       command=self._refocus,   # 点完把焦点还给主窗口，空格才能用
                       bg="#1e1e1e", fg="#ccc", selectcolor="#333",
                       activebackground="#1e1e1e", activeforeground="#ccc",
                       font=("Microsoft YaHei", 10)).pack(side="left")

        tk.Label(opt, text="时长(秒):", bg="#1e1e1e", fg="#aaa",
                 font=("Microsoft YaHei", 10)).pack(side="left", padx=(12, 2))
        self.duration_var = tk.StringVar(value=str(DEFAULT_DURATION_S))
        self.duration_entry = tk.Entry(opt, textvariable=self.duration_var, width=6,
                                       font=("Consolas", 11))
        self.duration_entry.pack(side="left")
        # 在输入框里按回车 = 输入完成，焦点还给主窗口（之后空格才有效）
        self.duration_entry.bind("<Return>", lambda e: self.root.focus_set())

        tk.Label(opt, text="（定时模式：空格 → 倒计时5秒 → 自动采集 → 到时响铃并结束）",
                 bg="#1e1e1e", fg="#666", font=("Microsoft YaHei", 9)).pack(side="left", padx=10)

        # ---------- 第三行：4×3 热力图 ----------
        grid = tk.Frame(root, bg="#1e1e1e")
        grid.pack(padx=10, pady=6)

        self.cells = []
        for i in range(12):
            r, c = divmod(i, 3)
            cell = tk.Frame(grid, bg="#101010", bd=2, relief="ridge")
            cell.grid(row=r, column=c, padx=3, pady=3)

            name = tk.Label(cell, text=GRID_NAMES[i], bg="#101010", fg="#aaa",
                            font=("Microsoft YaHei", 9))
            name.pack(fill="x")

            value = tk.Label(cell, text="0", bg="#101010", fg="white",
                             font=("Consolas", 18, "bold"), width=7, height=1)
            value.pack(fill="both", expand=True, padx=6, pady=(0, 4))

            self.cells.append(value)

        # ---------- 第四行：统计 ----------
        stats = tk.Frame(root, bg="#1e1e1e")
        stats.pack(fill="x", padx=10, pady=2)

        self.stats_label = tk.Label(stats, text="已收 0 帧 | 已保存 0 条 | -- 帧/秒",
                                    bg="#1e1e1e", fg="#7fb2e5",
                                    font=("Consolas", 12))
        self.stats_label.pack(side="left")

        self.count_label = tk.Label(stats, text="", bg="#1e1e1e", fg="#9ad",
                                    font=("Microsoft YaHei", 11))
        self.count_label.pack(side="right")

        self.update_count_label()

        # ---------- 第五行：本次采集状态 + 按键提示 ----------
        self.session_label = tk.Label(root, text="待机（空格开始一次采集）",
                                      bg="#1e1e1e", fg="#9ad",
                                      font=("Consolas", 11))
        self.session_label.pack(pady=(2, 0))

        hint = tk.Label(root,
                        text="按键：0=仰卧  1=左侧卧  2=右侧卧  x=未标注  空格=开始/结束"
                             "（每次结束都会弹保存确认窗口）",
                        bg="#1e1e1e", fg="#888", font=("Microsoft YaHei", 10))
        hint.pack(pady=(2, 8))

        # ---------- 按键绑定 / 关窗处理 / 开始轮询 ----------
        root.bind("<Key>", self.on_key)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.update_label_display()
        # 注意：rec_display / session_label 的初始文字在上面创建时已设为"待机"状态

        # 让主窗口拿到键盘焦点（这样一打开就能直接按空格/数字）
        root.focus_set()

        root.after(40, self.poll)

    # ==================== 状态机：开始/结束一次采集 ====================
    def start_burst(self):
        """按空格开始：定时模式 → 先倒计时；手动模式 → 立刻开始"""
        if self.timed_var.get():
            # 定时模式：先检查时长参数
            try:
                secs = int(self.duration_var.get())
                if secs <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("参数错误", "定时时长必须是正整数（秒）")
                return
            self.burst_duration = secs
            self.phase = "prepare"
            self.countdown_remain = PREPARE_SECONDS
            self.update_rec_display()
            self.tick_countdown()
        else:
            self.begin_recording(None)

    def tick_countdown(self):
        """倒计时：每秒一响，到0开始采集（期间按空格可取消）"""
        if self.phase != "prepare":
            return
        if self.countdown_remain <= 0:
            self.begin_recording(self.burst_duration)
            return
        self.session_label.config(
            text=f"准备中…{self.countdown_remain} 秒后开始采集（按空格取消）")
        self.countdown_remain -= 1
        self.countdown_timer = self.root.after(1000, self.tick_countdown)

    def cancel_prepare(self):
        """倒计时期间取消"""
        self._cancel_timer("countdown_timer")
        self.phase = "idle"
        self.burst_duration = None
        self.update_rec_display()
        self.session_label.config(text="已取消（空格重新开始）")

    def begin_recording(self, duration):
        """真正开始采集。duration=秒数(定时模式) 或 None(手动模式)"""
        if not state.begin_burst():
            return
        self.phase = "recording"
        self.burst_duration = duration
        self.update_rec_display()
        self.session_label.config(text="采集中：等待数据…")

        if duration:
            # 定时结束：到点自动调用 timed_finish
            self.burst_timer = self.root.after(duration * 1000, self.timed_finish)

    def timed_finish(self):
        """定时到点（由 after 回调触发）"""
        if self.phase == "recording":
            self.end_burst(auto=True)

    def end_burst(self, auto):
        """结束采集（auto=True表示定时自动结束；False表示手动按空格）"""
        if self.phase != "recording":
            return
        rows = state.end_burst()          # 从核心取出本批次数据
        self.phase = "idle"
        self._cancel_timer("burst_timer")
        self.update_rec_display()

        if auto:
            play_ring()   # 定时到时：响铃3秒（后台线程）

        self.session_label.config(text=f"本次采集 {len(rows)} 行，请确认保存…")
        self.confirm_save(rows)

    def confirm_save(self, rows):
        """弹出保存确认窗口：保存 → 写CSV；丢弃 → 什么都不写"""
        if not rows:
            messagebox.showwarning("没有数据", "本次采集没有收到任何数据，已取消。")
            self.session_label.config(text="本次无数据")
            return

        n = len(rows)
        dur_s = (rows[-1][0] - rows[0][0]) / 1000.0   # 用ESP32时间戳算时长

        # 本次批次里的标签分布（有可能中途改过标签）
        dist = {}
        for r in rows:
            dist[r[2]] = dist.get(r[2], 0) + 1
        dist_str = "  ".join(f"{POSTURE_NAMES.get(k, k)}:{v}"
                             for k, v in sorted(dist.items()))

        # 文件名用"最后一个标签"：0/1/2 是数字，未标注用 x
        lab = rows[-1][2]
        labname = str(lab) if lab >= 0 else "x"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fname = DATA_DIR / f"posture_{labname}_{datetime.now():%Y%m%d_%H%M%S}.csv"

        ok = messagebox.askyesno(
            "确认保存",
            f"本次采集 {n} 行，时长 {dur_s:.0f} 秒\n"
            f"标签分布: {dist_str}\n\n"
            f"保存到:\n{fname.name}\n\n"
            f"【是】保存    【否】丢弃")

        if ok:
            core.write_rows(fname, rows)
            self.saved += n
            for k, v in dist.items():
                self.label_counts[k] += v
            self.update_count_label()
            self.update_stats()
            self.session_label.config(text=f"已保存 {n} 行 → {fname.name}")
        else:
            self.session_label.config(text=f"已丢弃本次 {n} 行")

    def _cancel_timer(self, attr):
        """取消一个 after 定时器（如果存在）"""
        t = getattr(self, attr, None)
        if t is not None:
            try:
                self.root.after_cancel(t)
            except tk.TclError:
                pass
            setattr(self, attr, None)

    # ==================== 按键 ====================
    def on_key(self, event):
        # 焦点在输入框里时，按键是给输入框用的，不抢
        if isinstance(self.root.focus_get(), tk.Entry):
            return

        key = event.keysym
        if key in ("0", "1", "2"):
            state.label = int(key)
            self.update_label_display()
        elif key.lower() == "x":
            state.label = -1
            self.update_label_display()
        elif key == "space":
            if self.phase == "idle":
                self.start_burst()
            elif self.phase == "prepare":
                self.cancel_prepare()
            else:                       # recording
                self.end_burst(auto=False)

    def on_reconnect_click(self):
        new_ip = self.ip_var.get().strip()
        if new_ip:
            state.set_ip(new_ip)          # 换IP并触发重连
        else:
            state.request_reconnect()     # IP没变，仅重连
        self.root.focus_set()   # 点完按钮把焦点还给主窗口（空格要能用）

    def _refocus(self):
        """把键盘焦点还给主窗口（点过复选框/按钮之后用）"""
        self.root.focus_set()

    def on_close(self):
        """关窗口：定时器取消；如果正在采集中，自动保存不弹窗（防丢数据）"""
        self._cancel_timer("countdown_timer")
        self._cancel_timer("burst_timer")

        if self.phase == "recording":
            rows = state.end_burst()
            if rows:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                lab = rows[-1][2]
                labname = str(lab) if lab >= 0 else "x"
                fname = DATA_DIR / f"posture_{labname}_{datetime.now():%Y%m%d_%H%M%S}.csv"
                try:
                    core.write_rows(fname, rows)
                except OSError:
                    pass

        state.shutdown()
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
        except Exception:
            pass
        self.root.after(40, self.poll)

    def on_frame(self, ts, vals):
        """收到一帧：刷新热力图 + 更新采集中的行数显示"""
        self.received += 1

        # 帧率统计：保留最近30帧的到达时间
        now = time.monotonic()
        self.stamps.append(now)
        if len(self.stamps) > 30:
            self.stamps.pop(0)

        # 刷新热力图
        for i, v in enumerate(vals):
            self.cells[i].config(text=str(v), bg=self.value_to_color(v))

        # 采集中 → 更新"行数/秒数"显示（批次缓冲在 core 里，带当前标签）
        if self.phase == "recording":
            self.update_session_label()

        self.update_stats()

    # ==================== 界面刷新 ====================
    def set_status(self, what, info):
        if what == "waiting":
            self.status_label.config(text="等待IP…", bg="#8a6d00")
        elif what == "connecting":
            self.status_label.config(text=f"重连中… ({info})", bg="#8a6d00")
        elif what == "connected":
            self.status_label.config(text=f"已连接 ({info})", bg="#1b7a1b")
        else:
            self.status_label.config(
                text=f"未连接: {info[:40]}（{RECONNECT_DELAY_S:.0f}秒后自动重连）",
                bg="#8b0000")

    def update_label_display(self):
        colors = {0: "#1b7a1b", 1: "#1565c0", 2: "#6a1b9a", -1: "#555"}
        self.label_display.config(
            text=f"当前标签: {state.label} ({POSTURE_NAMES[state.label]})",
            bg=colors[state.label])

    def update_rec_display(self):
        if self.phase == "recording":
            self.rec_display.config(text="● 记录中", bg="#8b0000")
        elif self.phase == "prepare":
            self.rec_display.config(text="◌ 准备中", bg="#8a6d00")
        else:
            self.rec_display.config(text="⏸ 待机", bg="#444")

    def update_session_label(self):
        """采集中：显示 行数 / 已采秒数 / 剩余秒数（定时模式）"""
        st = state.status()
        n = st["burst_rows"]
        if n == 0:
            self.session_label.config(text="采集中：等待数据…")
            return
        dur = st["burst_seconds"]
        text = f"采集中：{n} 行 / {dur:.0f} 秒"
        if self.burst_duration:
            remain = self.burst_duration - dur
            text += f" / 剩余 {max(0.0, remain):.0f} 秒"
        self.session_label.config(text=text)

    def update_stats(self):
        # 帧率 = (最近帧数-1) / 时间跨度
        fps = 0.0
        if len(self.stamps) > 1:
            span = self.stamps[-1] - self.stamps[0]
            if span > 0:
                fps = (len(self.stamps) - 1) / span
        self.stats_label.config(
            text=f"已收 {self.received} 帧 | 已保存 {self.saved} 条 | {fps:.1f} 帧/秒")

    def update_count_label(self):
        c = self.label_counts
        self.count_label.config(
            text=f"已保存: 仰卧 {c[0]} | 左侧卧 {c[1]} | 右侧卧 {c[2]} | 未标注 {c[-1]}")

    def value_to_color(self, v):
        """数值 → 颜色：蓝(小) → 紫 → 红(大)，线性插值"""
        if v < 0:
            v = 0
        t = min(v / MAX_VALUE, 1.0)
        r = int(255 * t)
        b = int(255 * (1 - t))
        return f"#{r:02x}00{b:02x}"


# ============================================================
# 入口
# ============================================================
def main():
    # 命令行给了IP就用命令行的
    if len(sys.argv) > 1:
        state.ip = sys.argv[1]

    root = tk.Tk()
    App(root)

    # 启动采集核心的后台收数线程
    state.start()

    root.mainloop()


if __name__ == "__main__":
    main()
