/**
 * mqtt_app.c
 * MQTT应用层封装
 *
 * 服务器：EMQX Cloud Serverless（TLS加密，端口8883）
 * 使用的库：ESP-IDF自带的 esp-mqtt（组件名 mqtt，头文件是 mqtt_client.h）
 *
 * 连接安全（两个前提，缺一个都会握手失败）：
 *   1. mqtts:// 走TLS加密
 *   2. TLS证书校验需要"准确的系统时间"——wifi_sta.c里已经开了SNTP时间同步；
 *      证书用ESP-IDF内置的CA仓库（esp_crt_bundle）校验，无需手动管理证书文件
 *   （断线自动重连由 esp-mqtt 内部负责，不用我们自己写重连逻辑）
 */

#include "mqtt_app.h"

#include "mqtt_client.h"        // 这是 ESP-IDF 自带的 esp-mqtt API
#include "esp_event.h"          // ESP_EVENT_ANY_ID
#include "esp_log.h"
#include "esp_crt_bundle.h"     // ESP-IDF内置的CA证书仓库

static const char *TAG = "mqtt";

// ============================================================
// 连接参数（EMQX Cloud Serverless）
// ============================================================
#define MQTT_BROKER_URI   "mqtts://m73f363c.ala.cn-shenzhen.emqxsl.cn:8883"
#define MQTT_USERNAME     "esp32_sleep"
#define MQTT_PASSWORD     "a1234567"
#define MQTT_TOPIC        "sleepposture/status"   // 发布主题（电脑端去订阅这个）
#define MQTT_KEEPALIVE_S  30                      // 心跳间隔（秒）

static esp_mqtt_client_handle_t s_client = NULL;
static volatile bool s_connected = false;  // 连接状态（事件回调里更新）

// ============================================================
// MQTT事件回调（由esp-mqtt内部任务调用）
// ============================================================
static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = event_data;

    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:      // 事件：ESP32 连接上了 MQTT 服务器 Broker
        s_connected = true;        // 更新连接状态（置标志位）
        ESP_LOGI(TAG, "MQTT已连接（TLS）");
        // 上线通知：让电脑端立刻知道设备上线了
        esp_mqtt_client_publish(s_client, MQTT_TOPIC,   // 发布一个订阅说明连接上了
                                "{\"event\":\"online\"}", 0, 1, 0);
        break;

    case MQTT_EVENT_DISCONNECTED:   // 事件：ESP32断开了MQTT连接（可能是网络断了，或者服务器断开了）
        s_connected = false;     // 更新连接状态（置标志位）
        ESP_LOGW(TAG, "MQTT已断开（esp-mqtt会自动重连）");
        break;

    case MQTT_EVENT_ERROR:      // 事件：MQTT连接出错（esp-mqtt内部任务调用）
        s_connected = false;        // 更新连接状态（置标志位）
        if (event->error_handle)    // 根据 “传入参数” 打印出错误日报
        {
            // 连不上时重点看这行：tls错误码能区分"证书问题"还是"网络问题"
            ESP_LOGE(TAG, "MQTT错误: type=%d, tls_err=0x%x, sock_errno=%d",
                     (int)event->error_handle->error_type,
                     (unsigned)event->error_handle->esp_tls_last_esp_err,
                     event->error_handle->esp_transport_sock_errno);
        } else {
            ESP_LOGE(TAG, "MQTT错误");
        }
        break;

    default:
        break;
    }
}

// ============================================================
// 启动客户端（只调用一次）
// ============================================================
void mqtt_client_start(void)
{
    if (s_client != NULL) { // 根据标志位判断是否已经连接了
        return;   // 已经启动过
    }

    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = MQTT_BROKER_URI,  // 服务器 Broker 的地址（mqtts://开头走TLS）
        // 用内置CA仓库校验服务器证书（依赖系统时间正确，见wifi_sta.c的SNTP）
        .broker.verification.crt_bundle_attach = esp_crt_bundle_attach,
        .credentials.username = MQTT_USERNAME,  // 用户名（用于连接 Broker 要的确认）
        .credentials.authentication.password = MQTT_PASSWORD, // 密码（用于连接 Broker 要的确认）
        .session.keepalive = MQTT_KEEPALIVE_S,  // 心跳间隔（秒）：ESP32每隔这么多秒发一次心跳包，服务器没收到就断开连接
    };

    s_client = esp_mqtt_client_init(&cfg); 
    // 初始化客户端（esp-mqtt内部会创建一个任务去处理连接、心跳、重连等），返回客户端句柄
    
    if (s_client == NULL) {
        ESP_LOGE(TAG, "esp_mqtt_client_init 失败");
        return;
    }
    
    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    // 注册事件回调（esp-mqtt内部任务调用，MQTT事件回调上面已写）

    esp_mqtt_client_start(s_client);
    // 启动客户端（esp-mqtt内部任务会去连接服务器，连接成功后会回调 MQTT_EVENT_CONNECTED）

    ESP_LOGI(TAG, "MQTT客户端已启动 → %s", MQTT_BROKER_URI);
    ESP_LOGI(TAG, "发布主题: %s", MQTT_TOPIC);
}



// ============================================================
// 操作函数：发布（QoS1：至少送达一次）
// ============================================================
int mqtt_client_publish(const char *payload)
{
    if (s_client == NULL)   // 根据客户端句柄判断客户端有没启动
    {
        return -1;
    }
    return esp_mqtt_client_publish(s_client, MQTT_TOPIC, payload, 0, 1, 0);
}

// ============================================================
// 操作函数：查询连接状态
// ============================================================
bool mqtt_client_is_connected(void)
{
    return s_connected;
}
