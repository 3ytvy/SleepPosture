# ============================================================
# ai_model 组件：存放TFLite模型的C数组
# model_data.c 由 ai/scripts/to_c_array.py 自动生成（换模型重跑生成脚本即可）
# ============================================================
idf_component_register(
    SRCS "model_data.c"
    INCLUDE_DIRS "."
)
