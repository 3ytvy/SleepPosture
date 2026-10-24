/**
 * ads1115.c
 * ADS1115 16位ADC驱动实现
 */

#include "ads1115.h"
#include "config.h"
#include "esp_log.h"
#include "driver/i2c_master.h"
#include "esp_rom_sys.h"    // 芯片内部固化了的程序，其中的esp_rom_delay_us()：微秒级延时，轮询等待用
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ads1115";

// ============================================================
// ADS1115寄存器地址
// ============================================================
#define ADS1115_REG_CONVERSION  0x00    // 转换结果寄存器
#define ADS1115_REG_CONFIG      0x01    // 配置寄存器

// ============================================================
// ADS1115的I2C地址
// ============================================================
// ADDR接GND时地址为0x48
#define ADS1115_I2C_ADDR        0x48

// ============================================================
// 配置寄存器值
// ============================================================
// 16位配置字，各位含义：
//   位15: OS=1（启动单次转换）
//   位14-12: MUX=100（AIN0对GND，单端）
//   位11-9: PGA=001（±4.096V）
//   位8: MODE=1（单次转换模式）
//   位7-5: DR=111（860SPS）
//   位4-0: 比较器禁用
//
// 拼起来：1100 0011 1110 0011 = 0xC3E3
#define ADS1115_CONFIG_HIGH     0xC3
#define ADS1115_CONFIG_LOW      0xE3   // 860SPS

// ============================================================
// 静态变量
// ============================================================
static i2c_master_bus_handle_t s_bus_handle = NULL;
static i2c_master_dev_handle_t s_ads_handle = NULL;

// ============================================================
// 初始化
// ============================================================
void ads1115_init(void)
{
    // --------------------------------------------------------
    // 第1步：创建I2C主机总线
    // --------------------------------------------------------
    i2c_master_bus_config_t bus_cfg = {
        .clk_source = I2C_CLK_SRC_DEFAULT,  // I2C 时钟源：使用默认时钟源（通常为 APB 时钟）
        .i2c_port = I2C_NUM_0,      // 使用 I2C 控制器端口 0
        .scl_io_num = I2C_SCL,
        .sda_io_num = I2C_SDA,
        .glitch_ignore_cnt = 7,     // 毛刺过滤阈值：小于 7 个时钟周期的尖峰会被忽略，典型值为 7
        .flags.enable_internal_pullup = true,  // 启用内部上拉
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &s_bus_handle));

    // --------------------------------------------------------
    // 第2步：把ADS1115注册为I2C从机
    // --------------------------------------------------------
    i2c_device_config_t dev_cfg =   {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = ADS1115_I2C_ADDR,
        .scl_speed_hz = 400000,   // 400kHz
    };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_bus_handle, &dev_cfg, &s_ads_handle));

    ESP_LOGI(TAG, "ADS1115 初始化完成");
}   

// ============================================================
// 读一次ADC值
// ============================================================
int16_t ads1115_read(void)
{
    // --------------------------------------------------------
    // 第1步：写配置寄存器，启动单次转换
    // --------------------------------------------------------
    // I2C传输格式：[寄存器地址] [配置高字节] [配置低字节]
    uint8_t cmd[3] = {
        ADS1115_REG_CONFIG,     // 配置寄存器
        ADS1115_CONFIG_HIGH,    // 命令字
        ADS1115_CONFIG_LOW
    };
    ESP_ERROR_CHECK(i2c_master_transmit(s_ads_handle, cmd, 3, -1));

    // --------------------------------------------------------
    // 第2步：等转换完成（轮询OS位）          
    // --------------------------------------------------------
    // 原来这里是 vTaskDelay(pdMS_TO_TICKS(2)); 但它实际等于“不等”：
    //   vTaskDelay 的单位是 tick 不是毫秒。sdkconfig 里 CONFIG_FREERTOS_HZ=100
    //   → 1 tick = 10ms，pdMS_TO_TICKS(2) = (2×100)/1000 = 0
    //   → vTaskDelay(0) 立刻返回，根本没等。
    //   而860SPS下一次转换要约1.17ms，没等够就读 → 读到的永远是上一次转换的结果；
    //   并且这1.17ms里MUX已经扫过约3个通道（一整行）→ 现象就是
    //   “行内三个格子同值 + 整体错位一行（肩→背、背→腰、腰→臀、臀→肩）”。
    //
    // 现改为轮询配置寄存器的OS位（ADS1115标准做法）：
    //   启动转换后，读回OS位：0=还在转换，1=转换完成。
    //   转换一完成立刻退出，不用猜“等多久才够”。
    uint8_t cfg_reg = ADS1115_REG_CONFIG;   // 要轮询的寄存器：配置寄存器（0x01）
    uint8_t cfg[2] = {0};                   // 存放读回的2字节（高字节在前）
    int timeout = 20;                       // 最多查20次，防止硬件异常时死循环

    do {
        esp_rom_delay_us(200);              // 隔200µs查一次（微秒级延时，不能用vTaskDelay）
        ESP_ERROR_CHECK(i2c_master_transmit_receive(s_ads_handle, &cfg_reg, 1, cfg, 2, -1));
        // cfg[0]是配置高字节；0x80 = 1000_0000，按位与取出最高位（OS位）
        // OS=0（还在转换）→ 继续循环；OS=1（转换完成）→ 退出
    } while ((cfg[0] & 0x80) == 0 && --timeout > 0);

    if (timeout == 0) {
        ESP_LOGW(TAG, "OS位轮询超时，本次读数可能偏旧");   // 异常兜底
    }    

    // --------------------------------------------------------
    // 第3步：读转换结果寄存器
    // --------------------------------------------------------
    // I2C传输格式：先写寄存器地址，再读2字节
    uint8_t reg = ADS1115_REG_CONVERSION;
    uint8_t data[2] = {0};
    ESP_ERROR_CHECK(i2c_master_transmit_receive(s_ads_handle, &reg, 1, data, 2, -1));

    // --------------------------------------------------------
    // 第4步：拼接成16位有符号数
    // --------------------------------------------------------
    // data[0]是高字节，data[1]是低字节
    int16_t value = (int16_t)((data[0] << 8) | data[1]);

    return value;
}