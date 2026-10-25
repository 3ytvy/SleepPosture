/**
 * sensor.c
 * 采集任务
 *
 * 当前版本：
 *   轮询12路 → 加时间戳 → 串口打印 + TCP发送（10帧/秒）
 *
 *   相比上一版的变化：
 *   1. 帧率 2帧/秒(500ms) → 10帧/秒（用vTaskDelayUntil做周期任务，自动纠偏）
 *   2. 每帧带时间戳（开机以来的毫秒数）
 *   3. 每帧通过TCP发给电脑（没客户端时内部直接丢弃）
 *   4. 串口和TCP用同一种格式：DATA:时间戳,12个值
 *
 * 后续版本：加基线校准、滑动平均
 */

#include <stdio.h>          // snprintf
#include "sensor.h"
#include "config.h"
#include "mux.h"
#include "ads1115.h"
#include "tcp_server.h"     // TCP发送
#include "status_store.h"   // 把"最近一帧"共享给网页(/state 显示12路值)
#include "esp_log.h"
#include "esp_timer.h"      // esp_timer_get_time()：取开机毫秒数
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_rom_sys.h"    // esp_rom_delay_us

static const char *TAG = "sensor";

// 全局队列（在app_main.c里定义）：把压力帧发给推理任务
extern QueueHandle_t sensor_queue;

void sensor_task(void *pvParam)
{
    ESP_LOGI(TAG, "sensor_task 启动");

    int16_t val[SENSOR_COUNT];
    char line[160];                                     // 一帧的打包文本的缓冲
    TickType_t last_wake = xTaskGetTickCount();         // 周期任务的"基准时刻"

    while (1) {
        // ----------------------------------------------------
        // 1. 轮询12路
        // ----------------------------------------------------
        for (int ch = 0; ch < SENSOR_COUNT; ch++) {
            mux_select(ch);     // 选通指定通道
            esp_rom_delay_us(MUX_SETTLE_US);   // 50μs，MUX切换是纳秒级，50μs足够
            val[ch] = ads1115_read();
        }

        // ----------------------------------------------------
        // 2. 打包成一行文本
        // ----------------------------------------------------
        // 时间戳：开机以来的毫秒数（约49天回绕一次）。
        // 注意：这不是"真实时钟"，只是"开机后第几毫秒"
        uint32_t ts = (uint32_t)(esp_timer_get_time() / 1000);

        // 格式：DATA:时间戳,p0,p1,...,p11   （先不带头尾的\n）
        int n = snprintf(line, sizeof(line),
                         "DATA:%u,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
                         (unsigned)ts,
                         val[0], val[1], val[2], val[3], val[4], val[5],
                         val[6], val[7], val[8], val[9], val[10], val[11]);

        // ----------------------------------------------------
        // 3. 发出去（串口一份 + TCP一份）
        // ----------------------------------------------------
        // 串口打印：ESP_LOG自己会补换行，所以打印不带\n的版本
        ESP_LOGI(TAG, "%s", line);

        // TCP发送：补上换行符作为"一帧的结尾标记"（电脑端按行解析）
        line[n++] = '\n';
        tcp_server_send_line(line, n);   // 没客户端时内部直接丢弃，不会阻塞

        // ----------------------------------------------------
        // 4. 打包成结构体，发给推理任务（ai_task）
        // ----------------------------------------------------
        // 用超时0（非阻塞）发送：队列满了就丢弃这一帧——
        // 采集是最高优先级，绝对不能在这里被卡住等队列。
        if (sensor_queue != NULL) {
            pressure_frame_t frame;     // 定义一帧压力数据的数据结构
            for (int i = 0; i < SENSOR_COUNT; i++) {    // 逐个按数据结构写入压力数据
                frame.pressure[i] = val[i];
            }
            frame.timestamp = ts;
            xQueueSend(sensor_queue, &frame, 0);
            status_store_set_frame(&frame);   // 更新"最近一帧"快照（网页 /state 用）
        }

        // ----------------------------------------------------
        // 5. 等下一个周期（10帧/秒 = 每100ms一帧）
        // ----------------------------------------------------
        // vTaskDelayUntil：等到"基准时刻+100ms"再继续，
        // 采集耗时（约20ms）会被自动扣掉 → 帧间隔稳定，不会越跑越慢。
        // （vTaskDelay是"采集完再睡100ms"，周期会变成120ms且不精确）
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(1000 / COLLECT_RATE_HZ));
    }
}
