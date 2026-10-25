#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_viewer.py — 手机实时监视（局域网网页版）

为什么改：原来的 mqtt_monitor.py 是电脑上的 tkinter 窗口；现在改成
"电脑跑一个网页服务器，手机连同一WiFi打开网页就能看"。

页面显示三块内容：
    1. 12路压力实时值（4×3网格，颜色随压力变化）
    2. 最终判断（与上报MQTT的内容一致：稳定状态 + 置信度）
    3. 三个类别各自的概率（仰卧 / 左侧卧 / 右侧卧 的概率条）

数据来源：
    数据全部来自 ESP32 自带的网页服务器：GET http://<ESP32的IP>/state
    （需要固件为"包含 probs 和 vals 字段"的版本：
       probs=三个类别概率，vals=最近一帧12路原始值）
    本工具只做"代理 + 美化显示"：手机 → 电脑网页 → ESP32 /state。

用法：
    python tools/web_viewer.py                  # 启动后手机网页里填ESP32的IP
    python tools/web_viewer.py 192.168.11.110   # 也可以启动时直接指定

    ⚠ 手机与电脑需连同一WiFi；第一次运行请允许Windows防火墙。
"""

import json
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 网页服务端口（采集遥控用的是8000，这里用8001，两个可以同时开）
WEB_PORT = 8001


# ============================================================
# 网页内容（内嵌；纯HTML/CSS/JS，无外部依赖）
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SleepPosture 实时监视</title>
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

  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; font-size: 14px; color: #aaa; }
  .row input { flex: 1; padding: 10px; font-size: 15px; border-radius: 8px; border: 1px solid #444; background: #262626; color: #eee; }
  .row button { padding: 10px 16px; font-size: 14px; border: none; border-radius: 8px; background: #2d7ef7; color: #fff; }

  .card { background: #202020; border: 1px solid #333; border-radius: 14px; padding: 14px; text-align: center; }
  .big { font-size: 46px; font-weight: bold; padding: 6px 0; }
  .sub { font-size: 13px; color: #9ad; font-family: "Courier New", monospace; }

  .bars { background: #202020; border: 1px solid #333; border-radius: 12px; padding: 12px; }
  .bar-row { display: flex; align-items: center; gap: 8px; margin: 8px 0; font-size: 13px; }
  .bar-name { width: 56px; color: #bbb; text-align: right; }
  .bar-track { flex: 1; height: 18px; background: #2a2a2a; border-radius: 9px; overflow: hidden; }
  .bar-fill { height: 100%; width: 0%; border-radius: 9px; transition: width .25s; }
  .bar-val { width: 42px; text-align: right; font-family: "Courier New", monospace; color: #ddd; }
  .bar-fill.c0 { background: #2e9b3f; }
  .bar-fill.c1 { background: #2d7ef7; }
  .bar-fill.c2 { background: #8e44ad; }

  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .cell { background: #262626; border-radius: 10px; padding: 8px 6px; text-align: center; }
  .cell .n { font-size: 11px; color: rgba(255,255,255,0.65); }
  .cell .v { font-size: 20px; font-weight: bold; font-family: "Courier New", monospace; color: #fff; margin-top: 2px; }
  .hint { font-size: 12px; color: #666; line-height: 1.6; }
</style>
</head>
<body>

<h1>SleepPosture 实时监视</h1>
<div id="conn" class="conn">等待填写ESP32的IP…</div>

<h3>设备连接</h3>
<div class="row">
  <label>ESP32 IP:</label>
  <input id="ip" placeholder="例如 192.168.11.110">
  <button onclick="applyIp()">连接</button>
</div>

<div class="card">
  <div id="big" class="big">--</div>
  <div id="sub" class="sub">最终判断（与上报MQTT一致）</div>
</div>

<h3>三个类别概率（本帧模型输出）</h3>
<div class="bars">
  <div class="bar-row"><span class="bar-name">仰卧</span><div class="bar-track"><div id="bar0" class="bar-fill c0"></div></div><span id="pct0" class="bar-val">--</span></div>
  <div class="bar-row"><span class="bar-name">左侧卧</span><div class="bar-track"><div id="bar1" class="bar-fill c1"></div></div><span id="pct1" class="bar-val">--</span></div>
  <div class="bar-row"><span class="bar-name">右侧卧</span><div class="bar-track"><div id="bar2" class="bar-fill c2"></div></div><span id="pct2" class="bar-val">--</span></div>
  <div id="probHint" class="hint" style="display:none;">固件未更新：/state 中没有 probs 字段</div>
</div>

<h3>12路压力（最近一帧）</h3>
<div id="grid" class="grid"></div>

<p class="hint">
  数据来自 ESP32 的 /state（手机→电脑网页→ESP32）。最终判断=设备平滑后的稳定状态，
  与上报MQTT的内容一致；概率是本帧模型输出（过渡瞬间可能与最终状态不同步）。
</p>

<script>
const $ = (id) => document.getElementById(id);
const NAMES = ["肩左 C0", "肩中 C1", "肩右 C2",
               "背左 C3", "背中 C4", "背右 C5",
               "腰左 C6", "腰中 C7", "腰右 C8",
               "臀左 C9", "臀中 C10", "臀右 C11"];
const STATE = {
  0:   ["仰卧", "#1b7a1b"],
  1:   ["左侧卧", "#1565c0"],
  2:   ["右侧卧", "#6a1b9a"],
  255: ["离床", "#555555"]
};

// ---------- 12路网格 ----------
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

function valueColor(v) {
  if (v < 0) v = 0;
  const t = Math.min(v / 5000, 1);
  return "hsl(" + (240 * (1 - t)) + ", " + (70 + t * 20) + "%, " + (22 + t * 33) + "%)";
}

// 输入框只在服务器IP变化时同步（避免覆盖用户输入）
let lastIpFromServer = null;

function renderStatus(res) {
  const conn = $("conn");
  if (!res.ip) {
    conn.textContent = "请先填写ESP32的IP地址，然后点“连接”";
    conn.className = "conn";
    return null;
  }
  if (res.ok) {
    conn.textContent = "已连接 " + res.ip + " • 更新于 " + new Date().toTimeString().slice(0, 8);
    conn.className = "conn ok";
    return res.esp32;
  }
  conn.textContent = "连不上 " + res.ip + "（" + (res.reason || "检查设备是否在线") + "）";
  conn.className = "conn bad";
  return res.last || null;   // 有上一次数据就继续显示（标注离线）
}

function renderData(d) {
  if (!d || !d.ready) {
    $("big").textContent = "--";
    $("sub").textContent = "等待设备数据…";
    setProbs(null);
    setVals([]);
    return;
  }

  const st = STATE[d.posture] || ["未知", "#666"];
  $("big").textContent = st[0];
  $("big").style.color = st[1];
  $("sub").textContent = "最终判断（与上报MQTT一致） • 置信度 " + d.confidence + "%";

  setProbs(Array.isArray(d.probs) ? d.probs : null);
  setVals(Array.isArray(d.vals) ? d.vals : []);
}

function setProbs(probs) {
  const hint = $("probHint");
  if (!probs || probs.length < 3) {
    hint.style.display = "block";
    for (let i = 0; i < 3; i++) { $("bar" + i).style.width = "0%"; $("pct" + i).textContent = "--"; }
    return;
  }
  hint.style.display = "none";
  for (let i = 0; i < 3; i++) {
    const p = Math.max(0, Math.min(100, probs[i]));
    $("bar" + i).style.width = p + "%";
    $("pct" + i).textContent = p + "%";
  }
}

function setVals(vals) {
  for (let i = 0; i < gridCells.length; i++) {
    if (i < vals.length) {
      gridCells[i].v.textContent = vals[i];
      gridCells[i].el.style.background = valueColor(vals[i]);
    } else {
      gridCells[i].v.textContent = "--";
      gridCells[i].el.style.background = "#262626";
    }
  }
}

async function refresh() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    const res = await r.json();
    if (res.ip !== lastIpFromServer) {
      lastIpFromServer = res.ip;
      if (res.ip) $("ip").value = res.ip;
    }
    renderData(renderStatus(res));
  } catch (e) {
    $("conn").textContent = "无法连接电脑（检查同一WiFi / 电脑上程序是否在运行）";
    $("conn").className = "conn bad";
  }
}

async function applyIp() {
  const ip = $("ip").value.trim();
  if (!ip) { alert("请先填写ESP32的IP（以串口打印为准）"); return; }
  await fetch("/api/ip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ip: ip })
  });
  $("big").textContent = "--";
  refresh();
}

setInterval(refresh, 500);
refresh();
</script>
</body>
</html>
"""


# ============================================================
# HTTP 处理器：代理 ESP32 的 /state
# ============================================================
def make_handler(ui):
    """ui = {"ip": "...", "last_good": {...} or None}，可跨线程读写"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "SleepPostureViewer/1.0"

        def log_message(self, fmt, *args):
            pass   # 手机每500ms轮询，不刷屏

        def _send_json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_html(self, html):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send_html(HTML_PAGE)
            if path == "/api/status":
                return self._proxy_status()
            return self._send_json({"ok": False, "reason": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?")[0]
            if path != "/api/ip":
                return self._send_json({"ok": False, "reason": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, OSError):
                data = {}
            ip = (data.get("ip") or "").strip()
            if not ip:
                return self._send_json({"ok": False, "reason": "empty ip"}, 400)
            ui["ip"] = ip
            ui["last_good"] = None          # 换设备后丢弃旧数据
            return self._send_json({"ok": True, "ip": ip})

        def _proxy_status(self):
            """去ESP32拉一次 /state，成功就缓存，失败就把上一次成功的返回给页面"""
            ip = ui.get("ip") or ""
            if not ip:
                return self._send_json({"ok": False, "ip": "", "reason": "no_ip"})

            url = f"http://{ip}/state"
            try:
                with urllib.request.urlopen(url, timeout=1.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                ui["last_good"] = data
                return self._send_json({"ok": True, "ip": ip, "esp32": data})
            except Exception as e:
                return self._send_json({
                    "ok": False, "ip": ip,
                    "reason": str(e)[:60],
                    "last": ui.get("last_good"),
                })

    return Handler


def start_web_server(port, ui):
    """启动HTTP服务器（后台线程），返回server对象（测试里直接用）"""
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(ui))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ============================================================
# 工具：获取本机局域网IP
# ============================================================
def get_lan_ip():
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
    ip = sys.argv[1] if len(sys.argv) > 1 else ""
    ui = {"ip": ip, "last_good": None}

    start_web_server(WEB_PORT, ui)

    lan_ip = get_lan_ip()
    print("=" * 56)
    print("SleepPosture 实时监视（网页版）已启动")
    print("=" * 56)
    print(f"  手机（同一WiFi）打开:  http://{lan_ip}:{WEB_PORT}/")
    print(f"  本机打开:              http://127.0.0.1:{WEB_PORT}/")
    print(f"  ESP32:                 {ip or '（请在网页里填写IP后点连接）'}")
    print()
    print("  提示：需要固件为包含 probs/vals 字段的新版本（重新烧录过一次）。")
    print("        第一次运行请允许Windows防火墙。Ctrl+C 退出。")
    print("=" * 56)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n退出")


if __name__ == "__main__":
    main()
