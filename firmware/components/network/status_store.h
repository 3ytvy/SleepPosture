#pragma once

#include <stdbool.h>
#include "config.h"     // posture_result_t / pressure_frame_t

/**
 * status_store.h — 最新数据的"共享快照"（新增）
 *
 * 两份快照：
 *   1. 最新识别结果：status_store_set() / status_store_get()
 *        写方：ai.cpp（每帧）   读方：web_server（HTTP请求时）
 *   2. 最新原始压力帧：status_store_set_frame() / status_store_get_frame()
 *        写方：sensor.c（每帧） 读方：web_server（显示12路值）
 *
 * 用临界区保护：写方和读方在不同任务里，读写不能打架。
 * 数据都很小（结果约11字节，一帧约28字节），临界区开销可以忽略。
 *
 * extern "C"：ai.cpp是C++编译的，本模块是C编译的，
 * 不包一层的话C++会把函数名"改名"，链接时找不到（undefined reference）。
 */

#ifdef __cplusplus
extern "C" {
#endif

/** 写入最新识别结果（ai.cpp 每帧调用） */
void status_store_set(const posture_result_t *r);

/**
 * 读取最新识别结果
 * @return true=有数据（out有效）；false=还没收到过任何结果
 */
bool status_store_get(posture_result_t *out);

/** 写入最新原始压力帧（sensor.c 每帧调用） */
void status_store_set_frame(const pressure_frame_t *f);

/**
 * 读取最新原始压力帧
 * @return true=有数据（out有效）；false=还没收到过任何帧
 */
bool status_store_get_frame(pressure_frame_t *out);

#ifdef __cplusplus
}
#endif
