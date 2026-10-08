#pragma once

/**
 * sensor.h
 * 采集任务
 */

/**
 * 采集任务入口
 * 先做最简版：轮询12路，串口打印
 */
void sensor_task(void *pvParam);