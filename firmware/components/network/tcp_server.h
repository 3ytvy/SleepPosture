#pragma once

#include <stdint.h>

/**
 * tcp_server.h
 * TCP服务器
 *
 * 做什么：
 *   在指定端口上监听，等待电脑端的Python工具连接。
 *   sensor_task 每采集一帧，就调用 tcp_server_send_line() 把数据发出去。
 *
 * 设计要点（为什么这样设计）：
 *   1. 只服务1个客户端（采集实验够用；电脑端工具重启时会自动"换人"）
 *   2. 没有客户端连接时，发送直接丢弃、不缓存
 *      （训练数据是"人躺着的连续场景"，断线期间的数据没有意义）
 *   3. 客户端socket的所有权归accept任务：连接、替换、关闭都由它负责；
 *      sensor任务只调用send，失败了也不自己close（避免double close）
 */

/**
 * 启动TCP服务器（创建监听socket + accept任务）
 * @param port 监听端口（比如3333）
 *
 * 注意：只调用一次。
 *       bind/listen不依赖WiFi是否已获取IP，所以可以在WiFi
 *       连接结果还没出来时就调用——电脑随时连进来都能被接住。
 */
void tcp_server_start(uint16_t port);

/**
 * 发送一行数据（线程安全，由sensor_task调用）
 * @param buf 数据指针
 * @param len 数据长度（字节）
 * @return 实际发送的字节数；-1表示没客户端或发送失败（数据被丢弃）
 */
int tcp_server_send_line(const char *buf, int len);

/**
 * 查询当前是否有客户端连接
 * @return 1=有客户端；0=没有
 */
int tcp_server_client_connected(void);
