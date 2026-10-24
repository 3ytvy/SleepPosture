/**
 * status_store.c — 最新识别结果共享快照（新增）
 *
 * 一个极小的"单变量存储"：ai.cpp写、web_server读。
 * 用端口临界区（portENTER_CRITICAL）保证读写原子性——
 * 比互斥锁轻，适合这种"只拷贝8字节"的场景。
 */

#include "status_store.h"

#include "freertos/FreeRTOS.h"

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static posture_result_t s_latest;   // 最新一次结果
static bool s_has = false;          // 有没有写过（没写过时网页显示"等待数据"）

// 函数：更新共享快照，写入最新结果
void status_store_set(const posture_result_t *r)
{
    portENTER_CRITICAL(&s_mux);
    s_latest = *r;
    s_has = true;
    portEXIT_CRITICAL(&s_mux);
}

// 函数：读取共享快照，获取最新结果
bool status_store_get(posture_result_t *out)
{
    bool has;
    portENTER_CRITICAL(&s_mux);
    *out = s_latest;
    has = s_has;
    portEXIT_CRITICAL(&s_mux);
    return has;
}
