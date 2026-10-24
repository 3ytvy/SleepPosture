/**
 * ai.cpp
 * 推理任务：TFLite Micro（由 ai.c 改写为 C++ 实现）
 *
 * 为什么是 .cpp：TFLite Micro 是 C++ 库（用类/命名空间），
 * 调用它的文件必须用 C++ 编译。ai.h 里用 extern "C" 包了一层，
 * 所以 app_main.c（C文件）照样能调用 ai_task。
 *
 * 处理流程（★和训练脚本 train.py 必须严格一致，不然板子上不准）：
 *   0.离床闸门：12路总和 < OFF_BED_THRESHOLD(1000) → 直接判"离床"，
 *     不跑模型；否则进模型分姿势。
 *   0.5滑动窗口投票：最近5秒的判定投票、多数胜出才输出——
 *     躺下瞬间的短暂误判不会被传出去（输出慢约2.5秒，但稳得多）
 *   1. 从 sensor_queue 取一帧压力数据（12个值）
 *   2. 归一化：每个值 ÷ 该帧最大值 → 得到的值范围是 0~1
 *   3. 量化：q = round(x / scale) + zero_point → 写进输入张量（int8）
 *   4. interpreter.Invoke() 推理
 *   5. 输出张量（int8）里最大的那个 = 睡姿类别；反量化得到置信度
 *   6. 结果发到 result_queue（以后给mqtt任务用），并打印到串口
 */

#include <cstdint>

#include "ai.h"
#include "config.h"
#include "model_data.h"          // 模型C数组
#include "status_store.h"        // 最新状态快照（给网页服务器用）
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"

static const char *TAG = "ai";

// 全局队列（在app_main.c里定义）
extern QueueHandle_t sensor_queue;
extern QueueHandle_t result_queue;

// ============================================================
// tensor arena：TFLite Micro 推理时的"工作内存"（中间张量全放这里）
// 我们的模型极小，8KB绰绰有余；启动时会打印实际用了多少。
// 以后换更大的模型如果 AllocateTensors 失败，就加大这里。
// ============================================================
constexpr int kTensorArenaSize = 8 * 1024;  // 这是 TFLite Micro 的工作内存大小（字节），8KB绰绰有余
// constexpr 是 C++11 引入的关键字，意思是 “常量表达式”：它告诉编译器，这个变量的值必须在编译期就能算出来，而不是等到运行时才确定 
alignas(16) static uint8_t tensor_arena[kTensorArenaSize];  // 这是 TFLite Micro 的工作内存，必须是 16 字节对齐的
// alignas(16) 是 C++11 的对齐说明符，作用是强制这个变量在内存中的起始地址是 16 的整数倍

// 滑动窗口投票：最近 VOTE_N 帧原始判定的环形缓冲
//   窗口长度 = 秒数 × 帧率（默认 5秒 × 10Hz = 50帧）
//   防误判效果：躺下瞬间1~2秒的短暂误判，在50帧的投票里翻不起浪
#define VOTE_N  (POSTURE_VOTE_WINDOW_S * COLLECT_RATE_HZ)
static uint8_t vote_buf[VOTE_N];    // 环形缓冲（ai_task启动时填满"离床"）
static int vote_idx = 0;            // 环形缓冲写指针
// cnt[4]的下标 → 真实状态值的映射（0/1/2/离床）
static const uint8_t kStateFromIdx[4] = {
    POSTURE_SUPINE, POSTURE_SIDE_LEFT, POSTURE_SIDE_RIGHT, POSTURE_OFF_BED
};

// 类别名字（顺序和训练时的label一一对应：0=仰卧 1=左侧卧 2=右侧卧）
static const char *kPostureNames[POSTURE_CLASSES] = {
    "仰卧", "左侧卧", "右侧卧"
};

void ai_task(void *pvParam)
{
    ESP_LOGI(TAG, "ai_task 启动");

    // --------------------------------------------------------
    // 1. 从C数组解析模型
    // --------------------------------------------------------
    const tflite::Model *model = tflite::GetModel(model_tflite);
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "模型版本不匹配: 模型=%d 运行时=%d",
                 (int)model->version(), (int)TFLITE_SCHEMA_VERSION);
        vTaskDelete(NULL);
    }

    // --------------------------------------------------------
    // 2. 注册算子（只注册模型用到的这三个，省Flash）
    // --------------------------------------------------------
    static tflite::MicroMutableOpResolver<3> resolver;
    resolver.AddFullyConnected();    // 全连接（Dense层）
    resolver.AddRelu();              // ReLU激活
    resolver.AddSoftmax();           // Softmax输出

    // --------------------------------------------------------
    // 3. 创建解释器 + 分配张量
    // --------------------------------------------------------
    // 理解：这是 C++ 和 C语言的一个区别，就是定义对象
    //      interpreter ，也就是创建出来的解释器，是一个对象（static 让它在函数退出后仍然存在（全局））
    //      对象内可以有成员，成员可以是数据也可以是函数，比如 interpreter.input(0) 就是调用了 interpreter 对象的 input 成员函数，返回第0个输入张量的指针
    static tflite::MicroInterpreter interpreter(
    //  模型    算子        工作内存        工作内存大小
        model, resolver, tensor_arena, kTensorArenaSize);

    // AllocateTensors() 会根据模型的输入输出张量的大小，分配工作内存（tensor_arena）中的一部分给输入输出张量和中间张量
    if (interpreter.AllocateTensors() != kTfLiteOk) {
        ESP_LOGE(TAG, "AllocateTensors失败（tensor arena太小？）");
        vTaskDelete(NULL);
    }

    // 获取输入输出张量的指针（方便后面直接操作）
    TfLiteTensor *input = interpreter.input(0);
    TfLiteTensor *output = interpreter.output(0);

    if (input->type != kTfLiteInt8 || output->type != kTfLiteInt8) {
        ESP_LOGE(TAG, "模型不是int8输入输出（转换时没做量化？）");
        vTaskDelete(NULL);
    }

    // 量化参数：从张量里读（不写死 —— 以后换模型也不用改代码）
    const float in_scale = input->params.scale; // 输入张量的量化比例
    const int in_zp = input->params.zero_point; // 输入张量的零点
    const float out_scale = output->params.scale;   // 输出张量的量化比例 
    const int out_zp = output->params.zero_point;   // 输出张量的零点

    ESP_LOGI(TAG, "模型加载完成: arena用 %u/%d 字节, 输入scale=%.8f zp=%d",
             (unsigned)interpreter.arena_used_bytes(), kTensorArenaSize,
             (double)in_scale, in_zp);

    pressure_frame_t frame;
    posture_result_t result;

    // 滑动窗口投票的初始化：
    //   缓冲预先填满"离床"——没人躺时判定本来就是离床，
    //   上电后窗口收敛后就能正确反映真实状态。
    for (int i = 0; i < VOTE_N; i++) { // 初始化环形缓冲全为离床
        vote_buf[i] = POSTURE_OFF_BED;
    }
    uint8_t stable_state = POSTURE_OFF_BED;      // 初始化平滑后的"最终输出状态"

    while (1) {
        // ----------------------------------------------------
        // 4. 等一帧数据（sensor_task每100ms发一帧）
        // ----------------------------------------------------
        if (xQueueReceive(sensor_queue, &frame, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        // ----------------------------------------------------
        // 5.离床闸门：先看12路总和
        //   实测依据：空床总和5~11；有人躺下最低几千、躺好几万。
        //   总和低于阈值 → 直接判"离床"，不跑模型。
        // ----------------------------------------------------
        int32_t total = 0;
        for (int i = 0; i < SENSOR_COUNT; i++) {
            total += frame.pressure[i];
        }

        uint8_t raw_state;
        float prob = 1.0f;      // 离床时置信度按100%处理
        int64_t dt_us = 0;

        if (total < OFF_BED_THRESHOLD) {
            raw_state = POSTURE_OFF_BED;        // 空床：跳过模型
        } else {
            // ------------------------------------------------
            // 6. 归一化：每个值 ÷ 该帧最大值（和训练脚本完全一致）
            // ------------------------------------------------
            int16_t vmax = 0;
            for (int i = 0; i < SENSOR_COUNT; i++) {
                if (frame.pressure[i] > vmax) {
                    vmax = frame.pressure[i];
                }
            }
            if (vmax < 1) {
                vmax = 1;   // 防除零
            }

            // ------------------------------------------------
            // 7. 量化写入输入张量：q = round(x / scale) + zero_point
            // ------------------------------------------------
            for (int i = 0; i < SENSOR_COUNT; i++) {
                float x = (float)frame.pressure[i] / (float)vmax;   // 0~1
                int q = (int)(x / in_scale + 0.5f) + in_zp;         // x>=0，直接+0.5取整
                if (q < -128) q = -128;                             // 夹到int8范围
                if (q > 127) q = 127;
                input->data.int8[i] = (int8_t)q;
            }

            // ------------------------------------------------
            // 8. 推理 + 取结果（argmax + 反量化置信度）
            // ------------------------------------------------
            int64_t t0 = esp_timer_get_time();
            interpreter.Invoke();   // ★ 推理 ★
            dt_us = esp_timer_get_time() - t0;

            int best = 0;
            for (int i = 1; i < POSTURE_CLASSES; i++) {
                if (output->data.int8[i] > output->data.int8[best]) {
                    best = i;
                }
            }
            prob = ((float)output->data.int8[best] - out_zp) * out_scale;
            if (prob < 0.0f) prob = 0.0f;
            if (prob > 1.0f) prob = 1.0f;

            raw_state = (uint8_t)best;
        }

        // ----------------------------------------------------
        // 9.滑动窗口投票 → 输出稳定状态
        //   把这一帧的原始判定放进环形缓冲，然后统计窗口里
        //   各个状态的票数，票多的胜出。
        // ----------------------------------------------------
        vote_buf[vote_idx] = raw_state;
        vote_idx = (vote_idx + 1) % VOTE_N;

        int cnt[4] = {0, 0, 0, 0};   // 下标含义见 kStateFromIdx
        for (int i = 0; i < VOTE_N; i++) {
            switch (vote_buf[i]) {
            case POSTURE_SUPINE:     cnt[0]++; break;
            case POSTURE_SIDE_LEFT:  cnt[1]++; break;
            case POSTURE_SIDE_RIGHT: cnt[2]++; break;
            default:                 cnt[3]++; break;   // 离床
            }
        }
        // 找票数最高的；平票时优先保持当前状态（防来回跳）
        int best_i = (stable_state == POSTURE_OFF_BED) ? 3 : (int)stable_state;
        for (int i = 0; i < 4; i++) {
            if (cnt[i] > cnt[best_i]) {
                best_i = i;
            }
        }
        stable_state = kStateFromIdx[best_i];

        // ----------------------------------------------------
        // 10. 上报：串口日志 + result_queue
        // ----------------------------------------------------
        result.posture = stable_state;
        result.confidence = (uint8_t)(prob * 100.0f + 0.5f);
        result.timestamp = frame.timestamp;

        // 更新共享快照：手机网页(http://ESP32的IP/)从这里读
        status_store_set(&result);

        if (stable_state == POSTURE_OFF_BED) {
            ESP_LOGI(TAG, "识别: 离床  (总和 %d)", (int)total);
        } else {
            ESP_LOGI(TAG, "识别: %s  置信度 %d%%  (推理 %d us)",
                     kPostureNames[stable_state], result.confidence, (int)dt_us);
        }

        // ----------------------------------------------------
        // 9. 结果发给result_queue（以后mqtt任务用；非阻塞，满了就丢）
        // ----------------------------------------------------
        if (result_queue != NULL) {
            xQueueSend(result_queue, &result, 0);
        }
    }
}
