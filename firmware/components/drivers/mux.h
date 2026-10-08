#pragma once

/**
 * mux.h
 * CD74HC4067 16通道多路复用器驱动
 *
 * 功能：
 *   通过GPIO控制S0~S3四个引脚，选择16个通道中的一个。
 *   选中的通道会把它的模拟电压送到SIG引脚。
 *
 * 引脚连接（见config.h）：
 *   S0 → GPIO4
 *   S1 → GPIO5
 *   S2 → GPIO6
 *   S3 → GPIO7
 *   SIG → ADS1115的A0
 */

/**
 * 初始化MUX控制引脚
 *
 * 把GPIO4~7配置为输出模式。
 * 必须在调用mux_select之前调用这个函数。
 */
void mux_init(void);

/**
 * 选通指定通道
 *
 * @param ch 通道号，范围0~15
 *           0~11对应12路FSR
 *           12~15未使用
 *
 * 原理：
 *   把ch的二进制4位分别写到S0~S3。
 *   比如ch=5（二进制0101）：
 *     S0=1, S1=0, S2=1, S3=0
 */
void mux_select(int ch);