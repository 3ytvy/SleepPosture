#pragma once

// ============================================================
// 模型数据声明（手写文件，不会被 to_c_array.py 覆盖）
// 数组本体在 model_data.c（由 ai/scripts/to_c_array.py 自动生成）
//
// 换模型的流程：
//   训练脚本重跑 → convert.py → to_c_array.py → 重新编译烧录
// ============================================================

// 模型字节数组（int8量化后的 .tflite 内容，const存在Flash里）
extern const unsigned char model_tflite[];

// 数组长度（字节）
extern const unsigned int model_tflite_len;
