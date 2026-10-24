/**
 * web_server.c — 网页服务器
 *
 * 手机看一眼工作流：
 *   手机浏览器打开 http://<ESP32的IP>/
 *     → 网页(内嵌的index.html)每1秒 fetch 一次 /state
 *     → /state 从 status_store 读最新识别结果，返回JSON
 *     → 网页更新大屏/时长统计/事件列表
 *
 * 为什么这么做：
 *   不用装App、不用电脑、不用云——ESP32自己就是服务器，
 *   手机只要和ESP32连同一个WiFi就行。
 */

#include "web_server.h"

#include "status_store.h"
#include "config.h"
#include "esp_log.h"
#include "esp_http_server.h"

#include <stdio.h>

static const char *TAG = "web";

// 嵌进固件的网页文件（由 CMakeLists 的 EMBED_FILES "index.html" 生成）
// ESP-IDF约定：符号名 = _binary_<文件名>_start / _end
extern const uint8_t index_html_start[] asm("_binary_index_html_start");
extern const uint8_t index_html_end[]   asm("_binary_index_html_end");

// ============================================================
// 状态名（和 mqtt_app.c 里的 posture_name 保持一致）
// ============================================================
static const char *state_name(uint8_t p)
{
    switch (p) {
    case POSTURE_SUPINE:     return "supine";      // 仰卧
    case POSTURE_SIDE_LEFT:  return "side_left";   // 左侧卧
    case POSTURE_SIDE_RIGHT: return "side_right";  // 右侧卧
    case POSTURE_OFF_BED:    return "off_bed";     // 离床
    default:                 return "unknown";
    }
}

// ============================================================
// GET /  → 返回网页
// ============================================================
static esp_err_t root_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, (const char *)index_html_start,
                           index_html_end - index_html_start);
}

// ============================================================
// GET /state  → 返回最新状态JSON（手机网页每秒轮询）
// ============================================================
static esp_err_t state_handler(httpd_req_t *req)
{
    posture_result_t r;
    char json[160];

    if (status_store_get(&r)) {
        snprintf(json, sizeof(json),
                 "{\"ready\":true,\"posture\":%d,\"name\":\"%s\","
                 "\"confidence\":%d,\"timestamp\":%u}",
                 (int)r.posture, state_name(r.posture),
                 r.confidence, (unsigned)r.timestamp);
    } else {
        snprintf(json, sizeof(json), "{\"ready\":false}");
    }

    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");   // 别缓存，每次都要新的
    return httpd_resp_sendstr(req, json);
}

// ============================================================
// 启动
// ============================================================
void web_server_start(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();   // 默认端口80
    httpd_handle_t server = NULL;

    if (httpd_start(&server, &cfg) != ESP_OK) {
        ESP_LOGE(TAG, "网页服务器启动失败");
        return;
    }

    httpd_uri_t uri_root = {
        .uri = "/",
        .method = HTTP_GET,
        .handler = root_handler,
    };
    httpd_uri_t uri_state = {
        .uri = "/state",
        .method = HTTP_GET,
        .handler = state_handler,
    };

    // 注册路由：把URL和处理函数关联起来
    httpd_register_uri_handler(server, &uri_root);
    httpd_register_uri_handler(server, &uri_state);

    ESP_LOGI(TAG, "网页服务器已启动 → 手机浏览器打开 http://<本机IP>/");
}
