#pragma once

#include <stdbool.h>

/**
 * mqtt_app.h
 * MQTT应用层封装（【Claude修改】本次实现）
 *
 * 服务器：EMQX Cloud Serverless（TLS加密，端口8883）
 *
 * 注意命名：本模块原来叫 mqtt_client，但ESP-IDF 5.4里 esp-mqtt 组件的
 * 头文件就叫 mqtt_client.h——重名会让 #include 找到我们自己而不是IDF的，
 * 所以改名 mqtt_app 区分。
 *
 * 用法：
 *   mqtt_client_start();            // 启动（断线自动重连由esp-mqtt内部负责）
 *   mqtt_client_publish(json);      // 发布到配置的主题
 *   mqtt_client_is_connected();     // 查询连接状态
 */

/** 启动MQTT客户端（TLS连接EMQX服务器；只调用一次） */
void mqtt_client_start(void);

/**
 * 发布一条消息到配置的主题（QoS1）
 * @param payload 字符串（以\0结尾）
 * @return 消息id（>=0）；-1 失败（未启动/未连接）
 */
int mqtt_client_publish(const char *payload);

/** 是否已连接 @return true=已连接 */
bool mqtt_client_is_connected(void);
