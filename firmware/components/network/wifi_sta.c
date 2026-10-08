/**
 * wifi_sta.c
 * WiFi Station模式连接管理实现
 *
 * 整体思路：
 *   1. 用esp_netif创建网络接口
 *   2. 用esp_wifi初始化WiFi为Station模式
 *   3. 用esp_event注册事件回调，处理连接/断开/获取IP事件
 *   4. 连接成功后，事件回调里设置一个标志位
 *   5. 主函数阻塞等待这个标志位，实现同步
 *
 * 事件驱动机制：
 *   WiFi连接是异步的。调用esp_wifi_connect()后立即返回，
 *   实际连接过程在后台进行。连接成功/失败通过事件通知。
 *   所以我们需要注册事件回调，在回调里记录连接状态。
 */

#include "wifi_sta.h"
#include "config.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"    // 【Claude修改】SNTP时间同步（TLS证书校验需要准确时间）
#include <stdbool.h>           // 【Claude修改】bool
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include <string.h>

static const char *TAG = "wifi_sta";

// ============================================================
// WiFi配置（临时硬编码，后面改成配网或menuconfig）
// ============================================================
// TODO：等蓝牙配网做好后，这些从NVS读
#define WIFI_SSID      "TP-LINK_7B46"
#define WIFI_PASSWORD  "a1234567"

// ============================================================
// 事件组：用于同步连接状态
// ============================================================
// FreeRTOS事件组是一组二进制标志位。
// 这里用一个位：
//   WIFI_CONNECTED_BIT ：已连接并获取IP（断开时会被清掉）
#define WIFI_CONNECTED_BIT  BIT0
// 【Claude修改】原来还有一个 WIFI_FAIL_BIT（重试5次后放弃）。
// 设备要长期无人值守，路由器半夜重启后必须能自己连回来，所以改成无限重连，
// FAIL 位不再需要，已删除。

static EventGroupHandle_t s_wifi_event_group = NULL;    // WiFi连接事件组句柄
static int s_retry_num = 0;                    // 【Claude修改】只用于日志节流（每5次打一条），不再用于"放弃"

// ============================================================
// WiFi事件处理函数
// ============================================================
// 这个函数由ESP-IDF事件循环调用，不在我们自己的任务上下文。
// 不要在这里做耗时操作，只做状态记录和简单动作。
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data)
{
    // --------------------------------------------------------
    // WiFi事件：STA启动
    // --------------------------------------------------------
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "WiFi启动，开始连接...");
        // STA启动后，发起连接
        esp_wifi_connect();
    }
    // --------------------------------------------------------
    // WiFi事件：STA断开
    // --------------------------------------------------------
    else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        // 【Claude修改】断开的第一件事：清掉"已连接"标志。
        // 不清的话，断线期间 wifi_sta_is_connected() 会一直返回1（说谎）。
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);

        // 【Claude修改】无限重连：设备长期无人值守，路由器重启后必须能自己连回来。
        // s_retry_num 只用于控制日志频率（每5次打印一条），不再用于"放弃"。
        // 顺便打印断开原因码（reason），方便排查：
        //   15=密码错误，201=找不到路由器(信号弱/用了5G频段)，200=beacon超时
        wifi_event_sta_disconnected_t *disconn = (wifi_event_sta_disconnected_t *)event_data;
        esp_wifi_connect();
        s_retry_num++;
        if (s_retry_num % 5 == 1) {
            ESP_LOGW(TAG, "WiFi断开(reason=%d)，自动重连中...（已重试%d次）",
                     disconn->reason, s_retry_num);
        }
    }
    // --------------------------------------------------------
    // IP事件：获取到IP
    // --------------------------------------------------------
    else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "获取IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;  // 重置重试计数

        // 【Claude修改】同步网络时间（第一次拿到IP时启动一次）：
        //   TLS证书校验需要准确的系统时钟——没有时间的话，
        //   证书会被认为"还没生效"，MQTT(TLS)握手会失败。
        static bool sntp_started = false;
        if (!sntp_started) {
            sntp_started = true;
            esp_sntp_config_t sntp_cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("ntp.aliyun.com");
            esp_netif_sntp_init(&sntp_cfg);
            ESP_LOGI(TAG, "SNTP时间同步已启动 (ntp.aliyun.com)");
        }

        // 设置连接成功标志
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

// ============================================================
// 函数：初始化WiFi Station（启动WiFi）
// ============================================================
void wifi_sta_init(void)
{
    // --------------------------------------------------------
    // 第1步：创建事件组
    // --------------------------------------------------------
    s_wifi_event_group = xEventGroupCreate();

    // --------------------------------------------------------
    // 第2步：初始化网络接口
    // --------------------------------------------------------
    // 调用 esp_netif_init() 初始化TCP/IP协议栈
    // 只需调用一次，如果已经在别处调用过，会返回错误，忽略即可
    ESP_ERROR_CHECK(esp_netif_init());

    // 创建默认的事件循环
    // WiFi事件、IP事件都通过这个事件循环分发
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    // 给 ESP32 创建默认的 WiFi Station 网络接口
    // 这个接口用于管理WiFi连接和IP
    esp_netif_create_default_wifi_sta();

    // --------------------------------------------------------
    // 第3步：初始化WiFi驱动：使用默认配置去初始化
    // --------------------------------------------------------
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    // --------------------------------------------------------
    // 第4步：注册事件处理函数
    // --------------------------------------------------------
    // 注册WiFi事件（STA启动、断开等）
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT,                   // 事件类型：WiFi事件
        ESP_EVENT_ANY_ID,             // 所有WiFi事件
        &wifi_event_handler,          // 对应处理函数
        NULL,                         // 要传入的参数
        NULL));                       // 实例句柄

    // 注册IP事件（获取IP）
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT,                     // 事件类型：IP事件
        IP_EVENT_STA_GOT_IP,          // 只关心"获取IP"事件
        &wifi_event_handler,          // 对应处理函数
        NULL,
        NULL));

    // --------------------------------------------------------
    // 第5步：配置WiFi参数
    // --------------------------------------------------------
    wifi_config_t wifi_config = {0};

    // 把SSID和密码复制到配置结构体
    // strncpy限制长度，防止溢出
    strncpy((char *)wifi_config.sta.ssid, WIFI_SSID, sizeof(wifi_config.sta.ssid) - 1);
    strncpy((char *)wifi_config.sta.password, WIFI_PASSWORD, sizeof(wifi_config.sta.password) - 1);

    // 设置认证模式（WPA2-PSK是家庭路由器最常见的）
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    // 如果密码为空，使用开放网络
    if (strlen(WIFI_PASSWORD) == 0) {
        wifi_config.sta.threshold.authmode = WIFI_AUTH_OPEN;
    }

    // --------------------------------------------------------
    // 第6步：应用配置并启动WiFi
    // --------------------------------------------------------
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));                  // 设置为Station模式
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));    // 应用配置
    ESP_ERROR_CHECK(esp_wifi_start());                                  // 启动WiFi驱动

    ESP_LOGI(TAG, "WiFi初始化完成，SSID=%s", WIFI_SSID);
}

// ============================================================
// 函数：连接WiFi路由器（阻塞等待）
// ============================================================
int wifi_sta_connect(void)
{
    if (s_wifi_event_group == NULL) {
        ESP_LOGE(TAG, "请先调用wifi_sta_init()");
        return -1;
    }

    ESP_LOGI(TAG, "等待WiFi连接...");

    // 【Claude修改】无限重连模式下不再有"失败放弃"，所以只等"已连接"这一个位。
    // 参数：
    //   xEventGroupWaitBits(事件组, 等待哪些位, 是否清除位, 是否全部满足, 超时)
    //   pdFALSE：不清除标志位（后面wifi_sta_is_connected要查）
    //   pdTRUE ：只等待这一个位
    //   30000ms：30秒超时（超时不代表放弃，后台还在自动重连）
    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group,
        WIFI_CONNECTED_BIT,
        pdFALSE,                // 不清除位
        pdTRUE,                 // 只等这一个位
        pdMS_TO_TICKS(30000)    // 30秒超时
    );

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "WiFi连接成功");
        return 0;
    } else {
        ESP_LOGE(TAG, "WiFi连接超时（后台会继续自动重连，用 wifi_sta_is_connected() 可查询状态）");
        return -1;
    }
}

// ============================================================
// 查询WiFi是否已连接
// ============================================================
int wifi_sta_is_connected(void)
{
    if (s_wifi_event_group == NULL) {
        return 0;
    }

    EventBits_t bits = xEventGroupGetBits(s_wifi_event_group);
    return (bits & WIFI_CONNECTED_BIT) ? 1 : 0;
}

// ============================================================
// 等待WiFi连接成功
// ============================================================
int wifi_sta_wait_connected(uint32_t timeout_ms)
{
    if (s_wifi_event_group == NULL) {
        return -1;
    }

    TickType_t ticks = (timeout_ms == 0) ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);

    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group,
        WIFI_CONNECTED_BIT,
        pdFALSE,
        pdTRUE,
        ticks);

    return (bits & WIFI_CONNECTED_BIT) ? 0 : -1;
}