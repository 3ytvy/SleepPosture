#pragma once

/**
 * mqtt.h
 * 通信任务
 *
 */

/**
 * 通信任务入口
 *
 * @param pvParam FreeRTOS任务参数（不用，传NULL）
 *
 * 无限循环，不返回。
 */
void mqtt_task(void *pvParam);