# ============================================================
# network组件CMakeLists.txt
# 作用：注册network组件，包含WiFi连接和MQTT客户端
# ============================================================

idf_component_register(
    # SRCS：包含哪些.c源文件
    SRCS
        "wifi_sta.c"
        "mqtt_client.c"
        "tcp_server.c"     # 【Claude修改】TCP服务器

    # INCLUDE_DIRS：头文件搜索路径
    INCLUDE_DIRS "."

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
        lwip          # 【Claude修改】socket API（tcp_server.c 用）
        nvs_flash
)