#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mqtt_monitor.py — SleepPosture 云端监控界面（新建）

数据流：
    ESP32 --MQTT(TLS)--> EMQX Cloud --(订阅)--> 本工具显示

功能：
    1. 当前状态大屏：仰卧 / 左侧卧 / 右侧卧 / 离床（大字号+颜色区分）
    2. 设备在线指示：心跳30秒一条，超过90秒没消息会提示"疑似离线"
    3. 本次会话统计：各状态累计时长 + 消息条数
    4. 状态变化事件列表（时间线）+ 原始消息日志

依赖：paho-mqtt（已装到练习项目venv和系统Python里）
用法：
    python tools/mqtt_monitor.py
    （如果系统Python报错找不到paho，用：
     D:\\my_project\\practice_tensorflow\\venv\\Scripts\\python.exe tools\\mqtt_monitor.py）
"""

import json
import queue
import time
import tkinter as tk
from datetime import datetime, timedelta

import paho.mqtt.client as mqtt

# ============================================================
# 连接参数（和固件 components/network/mqtt_app.c 保持一致）
# ============================================================
BROKER = "m73f363c.ala.cn-shenzhen.emqxsl.cn"
PORT = 8883
USERNAME = "esp32_sleep"
PASSWORD = "a1234567"
TOPIC = "sleepposture/status"

# 状态定义：posture值 → (名字, 颜色)
STATES = {
    0:   ("仰卧", "#1b7a1b"),
    1:   ("左侧卧", "#1565c0"),
    2:   ("右侧卧", "#6a1b9a"),
    255: ("离床", "#555555"),
}

AUTO_CONNECT = True      # 打开工具就自动连接（自动化测试时可以关掉）
OFFLINE_AFTER_S = 90     # 超过这么多秒没收到消息 → 提示"疑似离线"

msg_q = queue.Queue()    # paho后台线程 → 主线程 的消息队列


def fmt_duration(sec):
    """秒 → 中文时长文字"""
    sec = int(sec)
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分{sec % 60:02d}秒"
    return f"{sec // 3600}小时{(sec % 3600) // 60:02d}分"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("SleepPosture 云端监控")
        root.configure(bg="#1e1e1e")
        root.minsize(760, 620)

        self.client = None
        self.connected = False

        # ---------- 会话统计 ----------
        self.current_state = None        # 当前状态（posture值）
        self.last_change = None          # 状态最近一次变化的时间（墙钟）
        self.last_msg_time = None        # 最近一条消息的到达时间
        self.msg_count = 0               # 收到消息总数
        self.durations = {0: 0.0, 1: 0.0, 2: 0.0, 255: 0.0}   # 各状态累计秒数

        # ---------- 第1行：连接状态 + 按钮 ----------
        top = tk.Frame(root, bg="#1e1e1e")
        top.pack(fill="x", padx=12, pady=(12, 4))

        self.status_label = tk.Label(top, text="未连接", bg="#333", fg="white",
                                     font=("Microsoft YaHei", 11), padx=10, pady=4)
        self.status_label.pack(side="left")

        tk.Button(top, text="连接", command=self.connect).pack(side="right", padx=4)
        tk.Button(top, text="断开", command=self.disconnect).pack(side="right", padx=4)

        # ---------- 第2行：当前状态大屏 ----------
        mid = tk.Frame(root, bg="#1e1e1e")
        mid.pack(fill="x", padx=12, pady=6)

        self.state_label = tk.Label(mid, text="等待数据", bg="#333", fg="white",
                                    font=("Microsoft YaHei", 40, "bold"),
                                    padx=30, pady=16)
        self.state_label.pack(side="left")

        right = tk.Frame(mid, bg="#1e1e1e")
        right.pack(side="left", padx=16)
        self.online_label = tk.Label(right, text="等待设备数据…", bg="#1e1e1e",
                                     fg="#888", font=("Microsoft YaHei", 13))
        self.online_label.pack(anchor="w")
        self.detail_label = tk.Label(right, text="", bg="#1e1e1e", fg="#aaa",
                                     font=("Consolas", 11))
        self.detail_label.pack(anchor="w", pady=(6, 0))

        # ---------- 第3行：会话时长统计 ----------
        self.stats_label = tk.Label(root, text="会话时长统计: --",
                                    bg="#1e1e1e", fg="#7fb2e5",
                                    font=("Consolas", 12))
        self.stats_label.pack(anchor="w", padx=12, pady=(2, 6))

        # ---------- 第4行：事件列表 + 原始日志 ----------
        lists = tk.Frame(root, bg="#1e1e1e")
        lists.pack(fill="both", expand=True, padx=12)

        left_box = tk.Frame(lists, bg="#1e1e1e")
        left_box.pack(side="left", fill="both", expand=True)
        tk.Label(left_box, text="状态变化事件", bg="#1e1e1e", fg="#aaa",
                 font=("Microsoft YaHei", 10)).pack(anchor="w")
        self.event_list = tk.Listbox(left_box, bg="#111", fg="#ddd",
                                     font=("Consolas", 10), height=12,
                                     highlightthickness=0)
        self.event_list.pack(fill="both", expand=True)

        right_box = tk.Frame(lists, bg="#1e1e1e")
        right_box.pack(side="left", fill="both", expand=True, padx=(10, 0))
        tk.Label(right_box, text="原始消息日志", bg="#1e1e1e", fg="#aaa",
                 font=("Microsoft YaHei", 10)).pack(anchor="w")
        self.raw_text = tk.Text(right_box, bg="#101010", fg="#bbb",
                                font=("Consolas", 9), height=12,
                                state="disabled", highlightthickness=0)
        self.raw_text.pack(fill="both", expand=True)

        # ---------- 第5行：清空 ----------
        bottom = tk.Frame(root, bg="#1e1e1e")
        bottom.pack(fill="x", padx=12, pady=(6, 12))
        tk.Button(bottom, text="清空统计与日志", command=self.clear_all).pack(side="left")
        tk.Label(bottom, text=f"主题: {TOPIC}   心跳策略: 变化即发 + 30秒心跳",
                 bg="#1e1e1e", fg="#666", font=("Microsoft YaHei", 9)).pack(side="left", padx=12)

        # ---------- 关窗处理 / 轮询 ----------
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        if AUTO_CONNECT:
            self.connect()

        root.after(300, self.poll)

    # ==================== MQTT 连接 ====================
    def connect(self):
        if self.client is not None:
            return
        # client_id要唯一（同一broker上不同客户端不能重名）
        cid = f"sleepposture-pc-{int(time.time()) % 10000}"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        self.client.username_pw_set(USERNAME, PASSWORD)
        self.client.tls_set()                      # 默认用系统CA证书校验服务器（TLS）
        self.client.reconnect_delay_set(min_delay=2, max_delay=30)  # 断线自动重连
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self.set_status("连接中…", "#8a6d00")
        self.client.connect_async(BROKER, PORT, keepalive=60)
        self.client.loop_start()                   # paho自己在后台线程收消息

    def disconnect(self):
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.connected = False
        self.set_status("未连接", "#8b0000")

    # 下面三个回调运行在paho的后台线程里：
    # 只往queue里塞消息（不能碰界面——tkinter只能在主线程操作）
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = (getattr(reason_code, "value", reason_code) == 0)
        msg_q.put(("conn", ok, str(reason_code)))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        msg_q.put(("conn", False, str(reason_code)))

    def _on_message(self, client, userdata, msg):
        msg_q.put(("msg", msg.topic, msg.payload.decode("utf-8", errors="ignore")))

    # ==================== 定时轮询（主线程） ====================
    def poll(self):
        try:
            while True:
                item = msg_q.get_nowait()
                if item[0] == "conn":
                    _, ok, info = item
                    self.connected = ok
                    if ok:
                        self.set_status(f"已连接 ({BROKER})", "#1b7a1b")
                        self.add_raw("--- 已连接，订阅中 ---")
                        if self.client:
                            self.client.subscribe(TOPIC)   # 断线重连后也会走到这里重新订阅
                            self.add_raw(f"--- 已订阅 {TOPIC} ---")
                    else:
                        self.set_status(f"断开/重连中 ({info})", "#8b0000")
                        self.add_raw(f"--- 连接断开: {info} ---")
                elif item[0] == "msg":
                    self.on_message(item[2])
        except queue.Empty:
            pass

        self.update_online_label()
        self.update_stats_label()
        self.root.after(300, self.poll)

    # ==================== 处理一条消息 ====================
    def on_message(self, payload: str):
        self.msg_count += 1
        self.last_msg_time = datetime.now()
        self.add_raw(f"[{self.last_msg_time:%H:%M:%S}] {payload}")

        try:
            data = json.loads(payload)
        except ValueError:
            return   # 不是JSON，忽略

        # 设备上线通知
        if "event" in data:
            self.add_event(f"[{self.last_msg_time:%H:%M:%S}] 设备上线/重连")
            return

        state = int(data.get("posture", -1))
        conf = data.get("confidence", "?")
        if state not in STATES:
            return

        now = datetime.now()

        # 状态变化：结算上一个状态的时长 + 记事件
        if state != self.current_state:
            if self.current_state is not None and self.last_change is not None:
                self.durations[self.current_state] += (now - self.last_change).total_seconds()
            prev_name = STATES[self.current_state][0] if self.current_state is not None else "（无）"
            name = STATES[state][0]
            self.add_event(f"[{now:%H:%M:%S}] {prev_name} → {name}  (置信度{conf}%)")
            self.current_state = state
            self.last_change = now

        # 更新大屏
        name, color = STATES[state]
        self.state_label.config(text=name, bg=color)
        self.detail_label.config(text=f"置信度 {conf}%    更新于 {now:%H:%M:%S}")

    # ==================== 界面刷新 ====================
    def set_status(self, text, color):
        self.status_label.config(text=text, bg=color)

    def update_online_label(self):
        if self.last_msg_time is None:
            self.online_label.config(text="等待设备数据…", fg="#888")
            return
        age = (datetime.now() - self.last_msg_time).total_seconds()
        if age < OFFLINE_AFTER_S:
            self.online_label.config(text=f"设备在线（{age:.0f}秒前有数据）", fg="#4caf50")
        else:
            self.online_label.config(text=f"疑似离线（{fmt_duration(age)}没数据）", fg="#d32f2f")

    def update_stats_label(self):
        parts = []
        for s in (0, 1, 2, 255):
            dur = self.durations[s]
            # 当前状态还在累计中：算到"现在"
            if s == self.current_state and self.last_change is not None:
                dur += (datetime.now() - self.last_change).total_seconds()
            parts.append(f"{STATES[s][0]} {fmt_duration(dur)}")
        self.stats_label.config(
            text="会话时长统计: " + " | ".join(parts) + f"    （消息 {self.msg_count} 条）")

    def add_event(self, text):
        self.event_list.insert(0, text)          # 新的在最上面
        if self.event_list.size() > 200:
            self.event_list.delete(200, "end")

    def add_raw(self, text):
        self.raw_text.config(state="normal")
        self.raw_text.insert("end", text + "\n")
        # 控制总行数，防止越积越多
        total = int(self.raw_text.index("end-1c").split(".")[0])
        if total > 120:
            self.raw_text.delete("1.0", f"{total - 100}.0")
        self.raw_text.see("end")
        self.raw_text.config(state="disabled")

    # ==================== 其他 ====================
    def clear_all(self):
        """清空日志和统计（不影响MQTT连接）"""
        self.event_list.delete(0, "end")
        self.raw_text.config(state="normal")
        self.raw_text.delete("1.0", "end")
        self.raw_text.config(state="disabled")
        self.msg_count = 0
        self.durations = {0: 0.0, 1: 0.0, 2: 0.0, 255: 0.0}
        self.last_change = datetime.now()        # 从"现在"开始重新计时当前状态

    def on_close(self):
        self.disconnect()
        self.root.destroy()


# ============================================================
# 入口
# ============================================================
def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
