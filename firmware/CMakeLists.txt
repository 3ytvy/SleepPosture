# ============================================================
# 项目根CMakeLists.txt
# 作用：定义整个ESP-IDF项目
# ============================================================

# 要求CMake最低版本3.16
cmake_minimum_required(VERSION 3.16)

# 包含ESP-IDF的构建系统
# $ENV{IDF_PATH} 是环境变量，指向ESP-IDF安装目录
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# 定义项目名
# 这个名字决定编译输出的文件名：
#   build/sleep_posture_firmware.bin
#   build/sleep_posture_firmware.elf
project(sleep_posture_firmware)