# ============================================================
# network组件CMakeLists.txt
# 作用：注册network组件，包含WiFi连接和MQTT客户端
# ============================================================

idf_component_register(
    # SRCS：包含哪些.c源文件
    SRCS
        "wifi_sta.c"
        "mqtt_app.c"       # MQTT封装（改名避免和IDF的mqtt_client.h撞名）
        "tcp_server.c"     # TCP服务器
        "web_server.c"     # 网页服务器（手机查看）
        "status_store.c"   # 最新状态快照（ai→web共享）

    # INCLUDE_DIRS：头文件搜索路径
    INCLUDE_DIRS "."

    # 嵌入的网页（编译进固件的Flash；生成 _binary_index_html_start 符号）
    EMBED_FILES "index.html"

    # REQUIRES：依赖哪些组件
    #   freertos  → FreeRTOS（事件循环、任务）
    #   common    → config.h（WiFi SSID/密码可能放这里）
    #   esp_wifi  → ESP-IDF的WiFi驱动
    #   esp_event → 事件循环机制
    #   esp_netif → 网络接口（获取IP）
    #   nvs_flash → 非易失存储（WiFi配置保存）
    REQUIRES
        freertos
        common
        esp_wifi
        esp_event
        esp_netif
        lwip          # socket API（tcp_server.c 用）
        mqtt          # esp-mqtt（mqtt_client.c 用）
        mbedtls       # esp_crt_bundle 内置CA证书仓库
        esp_http_server  # 网页服务器（web_server.c 用）
        nvs_flash
)