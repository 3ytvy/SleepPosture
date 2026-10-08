/**
 * mqtt.c
 * 通信任务（【Claude修改】本次实现）
 *
 * 职责：
 *   1. 兜底等WiFi就绪（app_main里已经连过）
 *   2. 启动MQTT客户端（TLS，自动重连）
 *   3. 循环从 result_queue 取睡姿结果，按"上传策略"发到云端
 *
 * 上传策略（避免10Hz刷屏云端）：
 *   - 状态变化 → 立刻上传
 *   - 状态没变 → 每30秒发一次"心跳"，让云端知道设备还活着
 */

#include "mqtt.h"
#include "config.h"
#include "wifi_sta.h"
#include "mqtt_app.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include <stdio.h>
#include <stdbool.h>

static const char *TAG = "mqtt_task";

// 全局队列（在app_main.c里定义）
extern QueueHandle_t result_queue;

// 上传策略参数
#define PUBLISH_HEARTBEAT_S  30      // 心跳间隔（秒）

// 状态 → 英文名（JSON里用英文，电脑端/云端处理最省心）
static const char *posture_name(uint8_t p)
{
    switch (p) {
    case POSTURE_SUPINE:     return "supine";      // 仰卧
    case POSTURE_SIDE_LEFT:  return "side_left";   // 左侧卧
    case POSTURE_SIDE_RIGHT: return "side_right";  // 右侧卧
    case POSTURE_OFF_BED:    return "off_bed";     // 离床
    default:                 return "unknown";
    }
}

void mqtt_task(void *pvParam)
{
    ESP_LOGI(TAG, "mqtt_task 启动");

    // 第1步：兜底等WiFi（正常情况下app_main已经连好了）
    if (!wifi_sta_is_connected()) {
        ESP_LOGI(TAG, "等待WiFi连接...");
        if (wifi_sta_wait_connected(30000) != 0) {
            ESP_LOGW(TAG, "WiFi暂时没连上，MQTT先启动（WiFi恢复后会自动接上）");
        }
    }

    // 第2步：启动MQTT客户端（TLS + 自动重连）
    mqtt_client_start();

    // 第3步：循环取结果 → 按策略上传
    posture_result_t result;
    uint8_t last_state = 0xFE;          // 无效初值 → 第一个状态一定会上传
    int64_t last_pub_us = 0;

    while (1) {
        if (xQueueReceive(result_queue, &result, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        // 没连上就先不发（连上后靠"状态变化/心跳"补上）
        if (!mqtt_client_is_connected()) {
            continue;
        }

        int64_t now = esp_timer_get_time();
        bool changed = (result.posture != last_state);
        bool heartbeat = (now - last_pub_us >= (int64_t)PUBLISH_HEARTBEAT_S * 1000000);
        if (!changed && !heartbeat) {
            continue;
        }

        // 组装JSON：posture=数字(0/1/2/255) name=英文名 confidence=置信度 timestamp=开机毫秒
        char json[192];
        snprintf(json, sizeof(json),
                 "{\"posture\":%d,\"name\":\"%s\",\"confidence\":%d,\"timestamp\":%u}",
                 (int)result.posture, posture_name(result.posture),
                 result.confidence, (unsigned)result.timestamp);

        int msg_id = mqtt_client_publish(json);
        if (msg_id >= 0) {
            ESP_LOGI(TAG, "已上传: %s (msg_id=%d)", json, msg_id);
            last_state = result.posture;
            last_pub_us = now;
        } else {
            ESP_LOGW(TAG, "上传失败(msg_id=%d)", msg_id);
        }
    }
}
