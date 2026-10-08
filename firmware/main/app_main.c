/**
 * app_main.c
 * 项目入口
 */

#include "config.h"
#include "mux.h"
#include "ads1115.h"
#include "sensor.h"
#include "ai.h"
#include "mqtt.h"
#include "wifi_sta.h"    // 【Claude修改】WiFi连接（无线数据链路的起点）
#include "tcp_server.h"  // 【Claude修改】TCP服务器（无线数据传输的出口）

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "nvs_flash.h"

static const char *TAG = "app_main";

QueueHandle_t sensor_queue = NULL;
QueueHandle_t result_queue = NULL;

void app_main(void)
{
    // NVS初始化（WiFi驱动需要）
    esp_err_t ret_nvs = nvs_flash_init();
    if (ret_nvs == ESP_ERR_NVS_NO_FREE_PAGES || ret_nvs == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret_nvs = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret_nvs);

    ESP_LOGI(TAG, "SleepPosture 启动");

    // 初始化硬件
    mux_init();
    ESP_LOGI(TAG, "MUX 初始化完成");

    ads1115_init();
    ESP_LOGI(TAG, "ADS1115 初始化完成");

    // --------------------------------------------------------
    // 【Claude修改】连接WiFi（无线数据链路的起点）
    // --------------------------------------------------------
    // 顺序说明：NVS在最开头已经初始化过了（WiFi驱动必须要有NVS）。
    // wifi_sta_init() 只做初始化；wifi_sta_connect() 阻塞等待，最多30秒。
    // 就算超时也没关系：后台会一直自动重连，连上后串口会打印"获取IP: x.x.x.x"。
    wifi_sta_init();
    if (wifi_sta_connect() == 0) {
        ESP_LOGI(TAG, "WiFi 已连接");
    } else {
        ESP_LOGW(TAG, "WiFi 暂时没连上，先继续跑（后台会自动重连）");
    }

    // --------------------------------------------------------
    // 【Claude修改】启动TCP服务器（无线数据传输的出口）
    // --------------------------------------------------------
    // 不用等WiFi连上再启动：bind/listen不依赖IP，电脑随时连进来都能接住；
    // WiFi断线重连也不影响这个socket。
    tcp_server_start(TCP_SERVER_PORT);

    // 创建队列
    sensor_queue = xQueueCreate(SENSOR_QUEUE_DEPTH, sizeof(pressure_frame_t));
    if (sensor_queue == NULL) {
        ESP_LOGE(TAG, "sensor_queue 创建失败");
        return;
    }

    result_queue = xQueueCreate(RESULT_QUEUE_DEPTH, sizeof(posture_result_t));
    if (result_queue == NULL) {
        ESP_LOGE(TAG, "result_queue 创建失败");
        return;
    }

    // 创建任务
    BaseType_t ret;

    // 创建采集任务（优先级最高）
    ret = xTaskCreate(sensor_task, "sensor", STACK_SENSOR,
                      NULL, PRIORITY_SENSOR, NULL);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "sensor_task 创建失败");
        return;
    }

    // 【Claude修改】创建推理任务（TFLite Micro，优先级低于采集）
    ret = xTaskCreate(ai_task, "ai", STACK_AI,
                      NULL, PRIORITY_AI, NULL);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "ai_task 创建失败");
        return;
    }

    // 【Claude修改】创建通信任务（MQTT上传云端，优先级最低）
    // WiFi已经在上面连好了；MQTT客户端在任务里启动（TLS + 自动重连）
    ret = xTaskCreate(mqtt_task, "mqtt", STACK_MQTT,
                      NULL, PRIORITY_MQTT, NULL);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "mqtt_task 创建失败");
        return;
    }

    ESP_LOGI(TAG, "任务创建完成（sensor + ai + mqtt）");
}