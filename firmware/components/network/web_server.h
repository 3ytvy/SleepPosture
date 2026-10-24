#pragma once

/**
 * web_server.h — 网页服务器（新增）
 *
 * 做什么：
 *   在ESP32上跑一个HTTP服务器，手机上连同一个WiFi后，
 *   用浏览器打开 http://<ESP32的IP>/ 就能看实时状态。
 *
 * 提供两个地址：
 *   GET /       → 手机显示的网页（index.html，编译时嵌进固件）
 *   GET /state  → 最新状态的JSON（网页每秒轮询一次）
 *
 * 依赖：ESP-IDF 的 esp_http_server 组件
 */

/** 启动网页服务器（端口80，只调用一次） */
void web_server_start(void);
