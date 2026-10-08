idf_component_register(
    SRCS "mux.c" "ads1115.c"
    INCLUDE_DIRS "."
    REQUIRES driver common esp_adc
)