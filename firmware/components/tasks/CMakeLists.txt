idf_component_register(
    SRCS "sensor.c" "ai.cpp" "mqtt.c"
    INCLUDE_DIRS "."
    # ai.c→ai.cpp（TFLite Micro是C++库）；
    # ai_model=模型C数组组件；esp-tflite-micro由 idf_component.yml 自动下载
    REQUIRES freertos common drivers network esp_timer ai_model esp-tflite-micro
)