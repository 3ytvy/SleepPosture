# ============================================================
# common组件CMakeLists.txt
# 作用：注册common组件，只包含config.h
# ============================================================

idf_component_register(
    # 注意：这里没有SRCS！
    # 因为common组件只有头文件config.h，没有.c文件需要编译。
    # 头文件是给别人include用的，不需要单独编译。

    # INCLUDE_DIRS：头文件搜索路径
    # "." 表示当前目录
    # 这样其他组件写 #include "config.h" 就能找到
    INCLUDE_DIRS "."

    # REQUIRES：依赖哪些组件
    # config.h里用了GPIO_NUM_4这些宏，来自driver/gpio.h
    # 所以要依赖driver组件，让编译器能找到driver/gpio.h
    REQUIRES
        driver
)