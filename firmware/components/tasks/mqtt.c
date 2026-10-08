/**
 * mqtt.c
 * 通信任务实现（当前为空壳）
 *
 * TODO：等硬件到货后实现WiFi和MQTT
 */

#include "mqtt.h"
#include "config.h"
#include "wifi_sta.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

static const char *TAG = "mqtt";

extern QueueHandle_t result_queue;

void mqtt_task(void *pvParam)
{
    ESP_LOGI(TAG, "mqtt_task 启动");

    // 第1步：初始化WiFi
    wifi_sta_init();

    // 第2步：连接WiFi（阻塞等待）
    if (wifi_sta_connect() == 0) {
        ESP_LOGI(TAG, "WiFi已连接，IP已获取");
    } else {
        ESP_LOGE(TAG, "WiFi连接失败");
    }

    // 空壳循环
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}