#pragma once

#include <stdint.h>

/**
 * ads1115.h
 * ADS1115 16位ADC驱动
 *
 * 连接：
 *   SDA → GPIO8
 *   SCL → GPIO9
 *   ADDR → GND（I2C地址0x48）
 *   A0 → CD74HC4067的SIG
 *
 * 配置：
 *   单次转换模式
 *   通道：A0对GND（单端）
 *   增益：±4.096V（1x）
 *   数据率：860SPS   ←【Claude修改】原写128SPS，与实际配置（0xC3E3）不一致
 */

/**
 * 初始化I2C总线和ADS1115
 */
void ads1115_init(void);

/**
 * 读一次ADC值
 * @return 16位有符号数（量化后的数字值）
 */
int16_t ads1115_read(void);