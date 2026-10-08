/**
 * ai.cpp
 * 推理任务：TFLite Micro（【Claude修改】由 ai.c 改写为 C++ 实现）
 *
 * 为什么是 .cpp：TFLite Micro 是 C++ 库（用类/命名空间），
 * 调用它的文件必须用 C++ 编译。ai.h 里用 extern "C" 包了一层，
 * 所以 app_main.c（C文件）照样能调用 ai_task。
 *
 * 处理流程（★和训练脚本 train.py 必须严格一致，不然板子上不准）：
 *   0.【Claude修改】离床闸门：12路总和 < OFF_BED_THRESHOLD(1000) → 直接判"离床"，
 *     不跑模型；否则进模型分姿势。状态切换带3帧去抖（迟滞）。
 *   1. 从 sensor_queue 取一帧压力数据（12个值）
 *   2. 归一化：每个值 ÷ 该帧最大值 → 0~1
 *   3. 量化：q = round(x / scale) + zero_point → 写进输入张量（int8）
 *   4. interpreter.Invoke() 推理
 *   5. 输出张量（int8）里最大的那个 = 睡姿类别；反量化得到置信度
 *   6. 结果发到 result_queue（以后给mqtt任务用），并打印到串口
 */

#include <cstdint>

#include "ai.h"
#include "config.h"
#include "model_data.h"          // 模型C数组（to_c_array.py 生成）
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
constexpr int kTensorArenaSize = 8 * 1024;
alignas(16) static uint8_t tensor_arena[kTensorArenaSize];

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
    resolver.AddFullyConnected();
    resolver.AddRelu();
    resolver.AddSoftmax();

    // --------------------------------------------------------
    // 3. 创建解释器 + 分配张量
    // --------------------------------------------------------
    static tflite::MicroInterpreter interpreter(
        model, resolver, tensor_arena, kTensorArenaSize);

    if (interpreter.AllocateTensors() != kTfLiteOk) {
        ESP_LOGE(TAG, "AllocateTensors失败（tensor arena太小？）");
        vTaskDelete(NULL);
    }

    TfLiteTensor *input = interpreter.input(0);
    TfLiteTensor *output = interpreter.output(0);

    if (input->type != kTfLiteInt8 || output->type != kTfLiteInt8) {
        ESP_LOGE(TAG, "模型不是int8输入输出（转换时没做量化？）");
        vTaskDelete(NULL);
    }

    // 量化参数：从张量里读（不写死——以后换模型也不用改代码）
    const float in_scale = input->params.scale;
    const int in_zp = input->params.zero_point;
    const float out_scale = output->params.scale;
    const int out_zp = output->params.zero_point;

    ESP_LOGI(TAG, "模型加载完成: arena用 %u/%d 字节, 输入scale=%.8f zp=%d",
             (unsigned)interpreter.arena_used_bytes(), kTensorArenaSize,
             (double)in_scale, in_zp);

    pressure_frame_t frame;
    posture_result_t result;

    // 【Claude修改】"离床闸门"的稳定状态 + 去抖计数：
    //   规则判定只有"离床/在床"两级，但临界点可能有抖动，
    //   所以连续 OFF_BED_STABLE_FRAMES 帧判定一致，才真正切换状态。
    uint8_t stable_state = POSTURE_OFF_BED;      // 上电先按"离床"对待（更安全）
    uint8_t cand_state = POSTURE_OFF_BED;        // 正在观察的候选状态
    int cand_count = 0;                          // 候选状态连续出现次数

    while (1) {
        // ----------------------------------------------------
        // 4. 等一帧（sensor_task每100ms发一帧）
        // ----------------------------------------------------
        if (xQueueReceive(sensor_queue, &frame, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        // ----------------------------------------------------
        // 5.【Claude修改】离床闸门：先看12路总和
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
            // 6. 归一化：每个值 ÷ 该帧最大值（★和训练脚本完全一致）
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
            interpreter.Invoke();
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
        // 9.【Claude修改】去抖：连续N帧判定一致才真正切换状态
        // ----------------------------------------------------
        if (raw_state == stable_state) {
            cand_count = 0;                       // 和稳定状态一致，候选清零
        } else if (raw_state == cand_state) {
            cand_count++;
            if (cand_count >= OFF_BED_STABLE_FRAMES) {
                stable_state = raw_state;         // 切换生效
                cand_count = 0;
            }
        } else {
            cand_state = raw_state;               // 出现新的候选状态，重新计数
            cand_count = 1;
        }

        // ----------------------------------------------------
        // 10. 上报：串口日志 + result_queue
        // ----------------------------------------------------
        result.posture = stable_state;
        result.confidence = (uint8_t)(prob * 100.0f + 0.5f);
        result.timestamp = frame.timestamp;

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
