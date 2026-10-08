/**
 * mux.c
 * CD74HC4067 多路复用器驱动实现
 */

#include "mux.h"           // 自己的头文件
#include "config.h"        // 引脚定义
#include "driver/gpio.h"   // ESP-IDF的GPIO驱动
#include "esp_log.h"       // 日志输出

// 日志标签，输出时显示[mux]
static const char *TAG = "mux";

/**
 * 初始化MUX控制引脚
 *
 * 把GPIO4~7配置为输出模式，用于控制CD74HC4067的通道选择。
 */
void mux_init(void)
{
    // gpio_config_t是一个结构体，用于配置GPIO
    // 这里使用"指定初始化"语法，只设置需要的字段，其他自动为0
    gpio_config_t io_conf = {
        // pin_bit_mask：要配置哪些引脚
        // 用位掩码表示，每一位对应一个GPIO
        // (1ULL << MUX_S0) 表示把第MUX_S0位设为1
        // ULL表示unsigned long long，64位，因为ESP32有最多48个GPIO
        // 竖线 | 是位或，把4个引脚的掩码合并
        .pin_bit_mask = (1ULL << MUX_S0) | (1ULL << MUX_S1) |
                        (1ULL << MUX_S2) | (1ULL << MUX_S3),

        // mode：引脚模式
        // GPIO_MODE_OUTPUT表示输出模式，可以写0或1
        .mode = GPIO_MODE_OUTPUT,

        // pull_up_en：是否启用内部上拉电阻
        // 输出模式下不需要上拉，所以禁用
        .pull_up_en = GPIO_PULLUP_DISABLE,

        // pull_down_en：是否启用内部下拉电阻
        // 输出模式下不需要下拉，所以禁用
        .pull_down_en = GPIO_PULLDOWN_DISABLE,

        // intr_type：中断类型
        // 输出模式下不需要中断，所以禁用
        .intr_type = GPIO_INTR_DISABLE,
    };

    // 调用gpio_config应用配置
    gpio_config(&io_conf);

    ESP_LOGI(TAG, "MUX 初始化完成");
}

/**
 * 选通指定通道
 *
 * @param ch 通道号0~15
 *
 * 原理：
 *   CD74HC4067有4个地址引脚S0~S3。
 *   把ch的二进制4位分别输出到S0~S3。
 *
 * 举例：
 *   ch=0  → 二进制0000 → S3=0,S2=0,S1=0,S0=0 → 选通C0
 *   ch=5  → 二进制0101 → S3=0,S2=1,S1=0,S0=1 → 选通C5
 *   ch=11 → 二进制1011 → S3=1,S2=0,S1=1,S0=1 → 选通C11
 */
void mux_select(int ch)
{
    // (ch >> 0) & 1：取ch的第0位（最低位）
    //   右移0位，然后和1做与运算，得到最低位的值（0或1）
    // gpio_set_level(MUX_S0, x)：把GPIO4设为x
    gpio_set_level(MUX_S0, (ch >> 0) & 1);

    // (ch >> 1) & 1：取ch的第1位
    gpio_set_level(MUX_S1, (ch >> 1) & 1);

    // (ch >> 2) & 1：取ch的第2位
    gpio_set_level(MUX_S2, (ch >> 2) & 1);

    // (ch >> 3) & 1：取ch的第3位
    gpio_set_level(MUX_S3, (ch >> 3) & 1);
}