#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_tcp.py — SleepPosture TCP链路测试（最小验证脚本）

作用：
    只做一件事——连接ESP32的TCP服务器，把收到的数据打印出来。
    不做可视化、不保存文件。用来确认"链路通了、数据格式对"。

用法：
    python test_tcp.py
    或指定IP：  python test_tcp.py 192.168.11.100
    （ESP32的IP看串口启动时打印的"获取IP: x.x.x.x"）

依赖：无（只用Python标准库 socket）

数据格式（ESP32每帧发一行）：
    DATA:时间戳,p0,p1,p2,...,p11\\n
    时间戳 = 开机以来的毫秒数
"""

import socket
import sys
import time

ESP32_IP = "192.168.11.100"   # ★ 默认IP，串口里变了就改这里（或命令行传入）
ESP32_PORT = 3333             # 和固件 config.h 里的 TCP_SERVER_PORT 一致


def main():
    # 命令行传了IP就用命令行的
    ip = sys.argv[1] if len(sys.argv) > 1 else ESP32_IP

    # 连接ESP32（TCP客户端主动连服务器）
    # create_connection：自动完成 socket创建 + connect
    print(f"正在连接 {ip}:{ESP32_PORT} ...")
    sock = socket.create_connection((ip, ESP32_PORT), timeout=5)
    print("已连接！按 Ctrl+C 退出\n")

    count = 0          # 收到的帧数
    t0 = time.time()   # 开始时间（算平均帧率用）

    # makefile("r") 把socket包成"按行读"的文件对象：
    # ESP32每帧发一行（以\n结尾），我们一次读一行，最省事
    with sock.makefile("r", encoding="utf-8", newline="\n") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("DATA:"):
                continue

            count += 1

            # 每10帧打印一次，防止刷屏
            if count % 10 == 1:
                nums = line[5:].split(",")      # 去掉"DATA:"前缀，按逗号切
                ts = nums[0]                    # 第1个数：时间戳
                vals = nums[1:]                 # 后12个数：压力值
                print(f"[第{count}帧] ts={ts}ms  12个值={vals}")

            # 每100帧汇报一次平均帧率
            if count % 100 == 0:
                dt = time.time() - t0
                print(f"---- 已收 {count} 帧，平均 {count/dt:.1f} 帧/秒 ----")

    # 走到这里说明ESP32那边断开了（读到文件结尾）
    print("\n连接已断开（ESP32关闭了连接）")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出（Ctrl+C）")
    except Exception as e:
        print(f"出错: {e}")
