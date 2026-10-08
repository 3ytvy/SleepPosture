idf_component_register(
    SRCS "app_main.c"
    INCLUDE_DIRS "."
    REQUIRES freertos common drivers network tasks nvs_flash
)