#pragma once

#include <stdint.h>              // int16_t、uint32_t、uint8_t
#include "driver/gpio.h"         // GPIO_NUM_4 等

// ============================================================
// 引脚定义
// ============================================================
// CD74HC4067 多路复用器控制引脚
#define MUX_S0          GPIO_NUM_4
#define MUX_S1          GPIO_NUM_5
#define MUX_S2          GPIO_NUM_6
#define MUX_S3          GPIO_NUM_7

// ADS1115 I2C 引脚
#define I2C_SDA         GPIO_NUM_8
#define I2C_SCL         GPIO_NUM_9

// 注意：GPIO35/36/37 被 Octal PSRAM 占用，禁止使用

// ============================================================
// 系统参数定义
// ============================================================
#define SENSOR_COUNT        12      // 12路 - FSR传感器输入
#define SAMPLE_RATE_HZ      100     // 采样率100Hz
#define SAMPLE_PERIOD_MS    (1000 / SAMPLE_RATE_HZ)  // 10ms

#define FILTER_WINDOW_SIZE  8       // 滑动平均窗口大小
#define BASELINE_SAMPLES    50      // 基线校准采样次数
#define MUX_SETTLE_US       50      // MUX切换后等待微秒数

// ============================================================
// 网络与上传参数（本次新增）
// ============================================================
#define COLLECT_RATE_HZ     10      // 采集帧率（帧/秒）：采集→串口/TCP的节奏
#define TCP_SERVER_PORT     3333    // TCP服务器端口（电脑端Python连这个端口）
#define PRIORITY_TCP        3       // TCP服务器(accept)任务优先级（低于sensor/ai）

// ============================================================
// 离床判断参数（新增）
// ============================================================
// 12路压力总和低于此值 → 判"离床"（空床），不跑模型。
// 依据（zdz实测@这台床垫）：空床总和 5~11；在床最低几千、躺好几万。
// 阈值1000 离两边都有巨大余量。
#define OFF_BED_THRESHOLD      1000
// 状态稳定窗口（秒）：滑动窗口投票——最近这么多秒的原始判定里，
// 出现次数最多的状态才作为输出；平票保持当前状态。
// 作用：躺下瞬间1~2秒的误判不会被传出去；切换延迟≈窗口一半（5秒→约2.5秒）。
#define POSTURE_VOTE_WINDOW_S  5

// ============================================================
// 队列深度定义
// ============================================================
#define SENSOR_QUEUE_DEPTH  5       // 压力帧队列深度
#define RESULT_QUEUE_DEPTH  5       // 睡姿结果队列深度

// ============================================================
// 任务优先级（数字越大优先级越高）定义
// ============================================================
#define PRIORITY_SENSOR     5       // 采集任务：最高
#define PRIORITY_AI         4       // 推理任务：中
#define PRIORITY_MQTT       3       // 通信任务：最低

// ============================================================
// 任务栈大小（字节）定义
// ============================================================
#define STACK_SENSOR        6144    // 采集任务栈大小（4096→6144：sensor现在会调用TCP发送，lwIP调用要额外栈）
#define STACK_AI            8192    // AI任务栈大小：AI任务需要更大栈（TFLite Micro）
#define STACK_MQTT          6144    // MQTT任务栈大小（4096→6144：esp-mqtt/TLS初始化吃栈）

// ============================================================
// 睡姿类别定义
// ============================================================
// 标签体系已改为三类：0=仰卧、1=左侧卧、2=右侧卧（不再区分俯卧）
// 顺序必须和训练数据的label、采集工具、ai.cpp 一一对应！
typedef enum{
    POSTURE_SUPINE = 0,          // 仰卧
    POSTURE_SIDE_LEFT,           // 左侧卧（原宏名POSTURE_SIDE）
    POSTURE_SIDE_RIGHT,          // 右侧卧（原宏名POSTURE_PRONE）
    POSTURE_CLASSES,             // 类别总数
}posture_type;

// 系统状态：离床（注意：它不是模型的输出类别！模型仍然只有3类）
// 由 ai.cpp 的"离床闸门"产生：12路总和 < OFF_BED_THRESHOLD 时输出这个值
#define POSTURE_OFF_BED     0xFF


// ============================================================
// 数据结构定义
// ============================================================

// 一帧压力数据的数据结构
typedef struct {
    int16_t pressure[SENSOR_COUNT];   // 12路压力值
    uint32_t timestamp;                // 时间戳（ms）
} pressure_frame_t;

// 睡姿识别结果的数据结构
typedef struct {
    uint8_t posture;                   // 睡姿类别
    uint8_t confidence;                // 置信度（0~100）
    uint8_t probs[POSTURE_CLASSES];    // 三个类别各自的概率（0~100；离床帧为0）
    uint32_t timestamp;                // 时间戳（ms）
} posture_result_t;