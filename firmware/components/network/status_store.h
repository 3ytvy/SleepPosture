#pragma once

#include <stdbool.h>
#include "config.h"     // posture_result_t

/**
 * status_store.h — 最新识别结果的"共享快照"（新增）
 *
 * 用途：
 *   ai.cpp 每帧把最新结果写进来（status_store_set）；
 *   网页服务器(web_server)在HTTP请求里读走给手机显示（status_store_get）。
 *
 * 用临界区保护：ai任务和HTTP任务是不同线程，读写不能打架。
 * 结构体很小（8字节），临界区开销可以忽略。
 *
 * extern "C"：ai.cpp是C++编译的，本模块是C编译的，
 * 不包一层的话C++会把函数名"改名"，链接时找不到（undefined reference）。
 */

#ifdef __cplusplus
extern "C" {
#endif

/** 写入最新状态（ai.cpp 每帧调用） */
void status_store_set(const posture_result_t *r);

/**
 * 读取最新状态
 * @return true=有数据（out有效）；false=还没收到过任何结果
 */
bool status_store_get(posture_result_t *out);

#ifdef __cplusplus
}
#endif
