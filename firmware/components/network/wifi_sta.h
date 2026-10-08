#pragma once

#include <stdint.h>              // int16_t、uint32_t、uint8_t
#include "driver/gpio.h"         // GPIO_NUM_4 等

/**
 * wifi_sta.h
 * WiFi Station模式连接管理
 *
 * 功能：
 *   1. 初始化WiFi为Station模式
 *   2. 连接指定路由器
 *   3. 等待获取IP地址
 *   4. 断线自动重连
 *
 * 使用流程：
 *   wifi_sta_init();       // 初始化（只需一次）
 *   wifi_sta_connect();    // 连接路由器（阻塞直到连上或超时）
 *   // 之后WiFi自动重连，不需要再调用
 *
 * 依赖：
 *   - nvs_flash必须提前初始化（WiFi驱动需要NVS存储配置）
 *   - 事件循环必须提前创建（esp_event_loop_create_default）
 */

/**
 * 初始化WiFi Station
 *
 * 做什么：
 *   1. 创建网络接口（esp_netif）
 *   2. 创建默认WiFi Station
 *   3. 注册事件处理函数（处理WiFi连接、断开、获取IP等事件）
 *   4. 配置WiFi为Station模式
 *   5. 启动WiFi
 *
 * 注意：
 *   这个函数只初始化，不连接路由器。
 *   连接路由器要调用 wifi_sta_connect()。
 */
void wifi_sta_init(void);

/**
 * 连接WiFi路由器
 *
 * 做什么：
 *   1. 设置SSID和密码
 *   2. 启动连接
 *   3. 阻塞等待，直到连接成功并获取IP
 *
 * @return
 *   0  ：连接成功
 *   -1 ：连接失败或超时
 *
 * 超时时间：默认30秒
 */
int wifi_sta_connect(void);

/**
 * 查询WiFi是否已连接
 *
 * @return
 *   1 ：已连接且已获取IP
 *   0 ：未连接
 */
int wifi_sta_is_connected(void);

/**
 * 等待WiFi连接成功（阻塞）
 *
 * @param timeout_ms 超时时间（毫秒），0表示无限等待
 * @return
 *   0  ：连接成功
 *   -1 ：超时
 *
 * 使用场景：
 *   mqtt任务启动前，先调用这个函数确保WiFi已连上。
 */
int wifi_sta_wait_connected(uint32_t timeout_ms);