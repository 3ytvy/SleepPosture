#pragma once

/**
 * ai.h
 * 推理任务（TFLite Micro）
 *
 * 职责：
 *   1. 初始化TFLite Micro（加载模型、注册算子、分配tensor arena）
 *   2. 从sensor_queue取压力帧
 *   3. 归一化 + 量化
 *   4. 调用模型推理
 *   5. 结果发到result_queue
 *
 * 【Claude修改】注意：实现文件是 ai.cpp（用C++编译，因为TFLite Micro是C++库）。
 * 这里用 extern "C" 包一层，保证 app_main.c 等 C 文件也能正常调用 ai_task。
 */

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 推理任务入口
 *
 * @param pvParam FreeRTOS任务参数（不用，传NULL）
 *
 * 无限循环，不返回。
 */
void ai_task(void *pvParam);

#ifdef __cplusplus
}
#endif
