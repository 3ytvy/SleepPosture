#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_collect.py — 手机遥控采集（局域网 HTTP 控制台）

用途（典型场景）：
    人躺在床上，手机连家里WiFi → 浏览器打开本页 → 选姿势标签 →
    点"开始采集" → 躺好采集 → 点"停止并保存"。
    采集逻辑与 collect_data.py 完全共用 collect_core.py。

工作方式：
    ┌────────────┐   HTTP(局域网)   ┌──────────────────────────┐
    │  手机浏览器  │ ───────────────→ │ 电脑：web_collect.py      │
    │  (控制页面)  │ ←─────────────── │  - 内置HTTP服务器(8000)   │
    └────────────┘   网页+状态JSON    │  - 采集核心 Collector      │
                                     └──────────┬───────────────┘
                                                │ TCP(3333)
                                                ▼
                                             ESP32-S3
    网页上的"开始/停止/标签"只是控制信号；压力数据从ESP32直接进电脑，
    保存的CSV落在 ai/data/<数据集>/posture_<标签>_<时间>.csv。

用法：
    python tools/web_collect.py                  # 启动网页服务，ESP32的IP在网页里填
    python tools/web_collect.py 192.168.11.110   # 也可以启动时直接指定ESP32的IP

    ⚠ 不要和 collect_data.py 同时运行：ESP32的TCP服务器只服务一个客户端，
      后连上的会把先连上的踢掉。

Windows提示：
    第一次运行会弹出防火墙询问，请勾选"允许访问"；否则手机打不开网页。
    手机和电脑必须在同一个WiFi/局域网。
"""

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 引入共享采集核心（与 collect_data.py 共用同一套逻辑）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_core as core      # noqa: E402

# 网页服务端口（80 常被占用/需要管理员权限，用 8000；手机访问时要带端口）
WEB_PORT = 8000


# ============================================================
# 网页内容（内嵌在本文件里；页面纯HTML/CSS/JS，无外部依赖）
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SleepPosture 采集遥控</title>
<style>
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; padding: 14px; background: #161616; color: #eee;
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  h1 { font-size: 18px; margin: 0 0 8px; }
  h3 { font-size: 13px; color: #888; margin: 18px 0 8px; font-weight: 500; }
  .conn { font-size: 13px; padding: 8px 10px; border-radius: 8px; background: #333; margin-bottom: 10px; }
  .conn.ok { background: #16401a; color: #8fdf8f; }
  .conn.bad { background: #4a1717; color: #ff9a9a; }

  .card { background: #202020; border: 1px solid #333; border-radius: 14px; padding: 14px; text-align: center; }
  .big { font-size: 44px; font-weight: bold; padding: 10px 0; }
  .rec-on { color: #ff6b6b; font-size: 15px; margin-top: 4px; }
  .rec-off { color: #888; font-size: 15px; margin-top: 4px; }

  .labels { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
  .lab {
    padding: 16px 8px; font-size: 18px; border-radius: 12px; border: 2px solid #444;
    background: #262626; color: #ddd; font-weight: 600;
  }
  .lab.active { border-color: #4da3ff; background: #1b3a5c; color: #fff; }
  .lab.small { font-size: 14px; padding: 12px 8px; color: #999; }

  .actions { display: flex; flex-direction: column; gap: 10px; }
  button { font-family: inherit; cursor: pointer; }
  .actions button { padding: 16px; font-size: 18px; font-weight: bold; border: none; border-radius: 12px; color: #fff; }
  .green { background: #1b7a1b; }
  .red { background: #b02a2a; }
  .gray { background: #444; }
  .actions button:disabled { background: #2a2a2a; color: #666; }

  .meta { font-size: 13px; color: #9ad; margin-top: 10px; font-family: "Courier New", monospace; }
  .result { font-size: 14px; color: #ffd54f; margin-top: 8px; min-height: 18px; }

  /* 12路压力网格：4行×3列，颜色随数值由蓝变红 */
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .cell { background: #262626; border-radius: 10px; padding: 8px 6px; text-align: center; }
  .cell .n { font-size: 11px; color: rgba(255,255,255,0.65); }
  .cell .v { font-size: 20px; font-weight: bold; font-family: "Courier New", monospace; color: #fff; margin-top: 2px; }

  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; font-size: 14px; color: #aaa; }
  .row input { flex: 1; padding: 10px; font-size: 15px; border-radius: 8px; border: 1px solid #444; background: #262626; color: #eee; }
  .row button { padding: 10px 16px; font-size: 14px; border: none; border-radius: 8px; background: #2d7ef7; color: #fff; }
  .hint { font-size: 12px; color: #666; line-height: 1.6; }
</style>
</head>
<body>

<h1>SleepPosture 采集遥控</h1>
<div id="conn" class="conn">等待填写设备IP…</div>

<h3>设备连接</h3>
<div class="row">
  <label>ESP32 IP:</label>
  <input id="ip" placeholder="例如 192.168.11.110">
  <button onclick="reconnect()">连接</button>
</div>

<div class="card">
  <div id="big" class="big">未标注</div>
  <div id="rec" class="rec-off">⏸ 未采集（默认停止）</div>
</div>

<h3>12路压力（最近一帧）</h3>
<div id="grid" class="grid"></div>

<h3>选择当前姿势（躺好后点它）</h3>
<div class="labels">
  <button class="lab" data-label="0" onclick="setLabel(0)">仰卧</button>
  <button class="lab" data-label="1" onclick="setLabel(1)">左侧卧</button>
  <button class="lab" data-label="2" onclick="setLabel(2)">右侧卧</button>
  <button class="lab small" data-label="-1" onclick="setLabel(-1)">未标注</button>
</div>

<h3>采集控制</h3>
<div class="actions">
  <button id="start" class="green" onclick="start()">▶ 开始采集</button>
  <button id="stopSave" class="red" onclick="stop(true)">■ 停止并保存</button>
  <button id="stopDrop" class="gray" onclick="stop(false)">停止并丢弃</button>
</div>
<div class="meta"><span id="frames">已收 0 帧</span></div>
<div id="result" class="result"></div>

<h3>设置</h3>
<div class="row"><label>数据集:</label><input id="subject" value="zdz"><button onclick="applySubject()">应用</button></div>
<p class="hint">
  手机与电脑需连同一WiFi。打不开网页：检查电脑防火墙是否允许 Python 访问网络（端口 8000）。
  采集文件保存到电脑的 ai/data/&lt;数据集&gt;/ 目录。
</p>

<script>
const $ = (id) => document.getElementById(id);
let lastStatus = null;

// ---------- 12路压力网格（4×3，和接线表一致：C0=肩左 … C11=臀右） ----------
const NAMES = ["肩左 C0", "肩中 C1", "肩右 C2",
               "背左 C3", "背中 C4", "背右 C5",
               "腰左 C6", "腰中 C7", "腰右 C8",
               "臀左 C9", "臀中 C10", "臀右 C11"];
const gridCells = [];
(function buildGrid() {
  const g = $("grid");
  for (let i = 0; i < 12; i++) {
    const cell = document.createElement("div");
    cell.className = "cell";
    cell.innerHTML = '<div class="n">' + NAMES[i] + '</div><div class="v">--</div>';
    g.appendChild(cell);
    gridCells.push({ el: cell, v: cell.querySelector(".v") });
  }
})();

// 数值 → 颜色（蓝 小 → 红 大；标尺 5000，与电脑端一致）
function valueColor(v) {
  if (v < 0) v = 0;
  const t = Math.min(v / 5000, 1);
  const hue = 240 * (1 - t);
  const sat = 70 + t * 20;
  const light = 22 + t * 33;
  return "hsl(" + hue + ", " + sat + "%, " + light + "%)";
}

async function api(path, data) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {})
  });
  return await r.json();
}

async function refresh() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    render(await r.json());
  } catch (e) {
    $("conn").textContent = "无法连接电脑（检查同一WiFi / 电脑上程序是否在运行）";
    $("conn").className = "conn bad";
  }
}

let lastIpFromServer = null;
let lastSubjectFromServer = null;

function render(s) {
  lastStatus = s;

  // 连接状态
  const conn = $("conn");
  if (!s.ip) {
    conn.textContent = "请先填写ESP32的IP地址，然后点“连接”";
    conn.className = "conn";
  } else if (s.connected) {
    conn.textContent = "设备已连接 • " + s.ip + " • " + s.fps + " 帧/秒";
    conn.className = "conn ok";
  } else {
    conn.textContent = "设备未连接（自动重连中） • " + s.ip;
    conn.className = "conn bad";
  }

  // 当前标签高亮 + 大字
  document.querySelectorAll(".lab").forEach(b =>
    b.classList.toggle("active", Number(b.dataset.label) === s.label));
  $("big").textContent = s.label_name;

  // 采集状态
  if (s.recording) {
    $("rec").textContent = "● 采集中：" + s.burst_rows + " 行 / " + s.burst_seconds + " 秒";
    $("rec").className = "rec-on";
  } else {
    $("rec").textContent = "⏸ 未采集（默认停止）";
    $("rec").className = "rec-off";
  }

  // 按钮可用性：未采集→只能开始；采集中→只能停止
  $("start").disabled = s.recording || !s.connected;
  $("stopSave").disabled = !s.recording;
  $("stopDrop").disabled = !s.recording;

  $("frames").textContent = "已收 " + s.frames + " 帧";
  $("result").textContent = s.last_result || "";

  // 12路压力网格：更新数值与颜色
  const vals = s.last_vals || [];
  for (let i = 0; i < gridCells.length; i++) {
    if (i < vals.length) {
      gridCells[i].v.textContent = vals[i];
      gridCells[i].el.style.background = valueColor(vals[i]);
    } else {
      gridCells[i].v.textContent = "--";
      gridCells[i].el.style.background = "#262626";
    }
  }

  // 输入框：只在服务器值"发生变化"时同步（避免每800ms覆盖用户正在填的内容）
  if (s.subject !== lastSubjectFromServer) {
    lastSubjectFromServer = s.subject;
    $("subject").value = s.subject;
  }
  if (s.ip !== lastIpFromServer) {
    lastIpFromServer = s.ip;
    if (s.ip) $("ip").value = s.ip;
  }
}

async function setLabel(l) { await api("/api/label", { label: l }); refresh(); }
async function start() { await api("/api/start"); refresh(); }
async function stop(save) {
  const r = await api("/api/stop", { save: save });
  if (r && r.reason === "no data") alert("本次没有收到任何数据，已取消。");
  refresh();
}
async function applySubject() {
  await api("/api/subject", { subject: $("subject").value.trim() });
  refresh();
}
async function reconnect() {
  const ip = $("ip").value.trim();
  if (!ip) { alert("请先填写设备IP（以串口打印为准）"); return; }
  await api("/api/ip", { ip: ip });
  refresh();
}

setInterval(refresh, 800);
refresh();
</script>
</body>
</html>
"""


# ============================================================
# HTTP 处理器
# ============================================================
def make_handler(collector, ui):
    """创建HTTP处理器类（绑定到具体的 collector 和界面状态）。

    ui 是一个可变的字典，用来存网页侧的设置/最近结果：
        {"subject": "zdz", "last_result": "..."}
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "SleepPostureWeb/1.0"

        # 不打印每个请求的日志（手机上每秒轮询一次，会刷屏）
        def log_message(self, fmt, *args):
            pass

        # ---------- 工具方法 ----------
        def _send_bytes(self, body: bytes, content_type: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass   # 手机提前断开，忽略

        def _send_json(self, obj, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", code)

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                return json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, OSError):
                return {}

        # ---------- GET ----------
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send_bytes(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/status":
                s = collector.status()
                s["subject"] = ui["subject"]
                s["last_result"] = ui["last_result"]
                self._send_json(s)
            else:
                self._send_json({"ok": False, "reason": "not found"}, 404)

        # ---------- POST ----------
        def do_POST(self):
            path = self.path.split("?")[0]
            data = self._read_json()

            if path == "/api/label":
                lab = data.get("label")
                if lab not in (0, 1, 2, -1):
                    return self._send_json({"ok": False, "reason": "bad label"}, 400)
                collector.label = int(lab)
                return self._send_json({"ok": True, "label": collector.label})

            if path == "/api/start":
                ok = collector.begin_burst()
                ui["last_result"] = ""
                return self._send_json({"ok": ok, "recording": collector.is_recording()})

            if path == "/api/stop":
                rows = collector.end_burst()          # 取出本批次（可能为空）
                save = bool(data.get("save"))
                if rows and save:
                    fname = collector.save_rows(rows, ui["subject"])
                    ui["last_result"] = f"已保存 {len(rows)} 行 → {fname.name}"
                    return self._send_json({
                        "ok": True, "saved": True, "rows": len(rows),
                        "file": fname.name, "dir": str(fname.parent),
                    })
                if rows:
                    ui["last_result"] = f"已丢弃本次 {len(rows)} 行"
                    return self._send_json({"ok": True, "saved": False, "rows": len(rows)})
                ui["last_result"] = "本次没有数据"
                return self._send_json({"ok": False, "reason": "no data", "rows": 0})

            if path == "/api/ip":
                ip = (data.get("ip") or "").strip()
                if not ip:
                    return self._send_json({"ok": False, "reason": "empty ip"}, 400)
                collector.set_ip(ip)
                return self._send_json({"ok": True, "ip": collector.ip})

            if path == "/api/subject":
                subject = core.safe_subject(data.get("subject") or "")
                ui["subject"] = subject
                return self._send_json({"ok": True, "subject": subject})

            return self._send_json({"ok": False, "reason": "not found"}, 404)

    return Handler


def start_web_server(port, collector, ui):
    """启动HTTP服务器（后台线程），返回server对象（测试里也能直接用）"""
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(collector, ui))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ============================================================
# 工具：获取本机局域网IP（打印给手机用）
# ============================================================
def get_lan_ip():
    """获取本机在局域网里的IP（UDP connect不实际发包，只让系统选路由）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ============================================================
# 入口
# ============================================================
def main():
    # 默认不连接设备：由用户在手机网页上填写ESP32 IP后点"连接"
    # （也可以在命令行直接传：python web_collect.py 192.168.11.110）
    esp32_ip = sys.argv[1] if len(sys.argv) > 1 else ""

    collector = core.Collector(esp32_ip, emit_events=False)
    collector.start()

    ui = {"subject": "zdz", "last_result": ""}   # 默认数据集名，可在网页上改
    start_web_server(WEB_PORT, collector, ui)

    lan_ip = get_lan_ip()
    print("=" * 56)
    print("SleepPosture 手机遥控采集 已启动")
    print("=" * 56)
    print(f"  手机（同一WiFi）打开:  http://{lan_ip}:{WEB_PORT}/")
    print(f"  本机打开:              http://127.0.0.1:{WEB_PORT}/")
    print(f"  连接ESP32:             {esp32_ip or '（请在网页里填写IP后点连接）'}")
    print()
    print("  提示：第一次运行如弹出Windows防火墙提示，请选择“允许访问”，")
    print("        否则手机打不开网页。Ctrl+C 退出。")
    print("=" * 56)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在退出...")
        collector.shutdown()


if __name__ == "__main__":
    main()
