<img width="1235" height="2747" alt="Pasted image 20261025173231" src="https://github.com/user-attachments/assets/0b96b6c1-9e79-4034-a7aa-a2eb48fe6500" /># SleepPosture

**智能床垫睡姿监测系统** —— ESP32-S3 + 12 路 FSR 压力阵列 + 端侧 TinyML，实时识别「仰卧 / 左侧卧 / 右侧卧 / 离床」，经 MQTT(TLS) 上云，并自带局域网网页控制台。

![ESP-IDF](https://img.shields.io/badge/ESP--IDF-v5.4-blue)
![ESP32-S3](https://img.shields.io/badge/MCU-ESP32--S3-red)
![TFLite Micro](https://img.shields.io/badge/Inference-TFLite%20Micro-orange)
![C/C++](https://img.shields.io/badge/Language-C%2FC%2B%2B-lightgrey)

---
## 界面：

手机端无需安装 App——设备与工具均自带局域网网页，浏览器直接访问：

|              实时监视 · 仰卧               |                实时监视 · 左侧卧                 |              实时监视 · 右侧卧              |              实时监视 · 离床               |
| :----------------------------------: | :---------------------------------------: | :----------------------------------: | :----------------------------------: |
| <img width="961" height="2171" alt="Pasted image 20261025173210" src="https://github.com/user-attachments/assets/d5618d3c-4581-4337-8d41-b844b389c200" /> |  <img width="1602" height="3565" alt="Pasted image 20261025173313" src="https://github.com/user-attachments/assets/5f6952b2-e649-4b5d-87bc-c8423af1870f" /> | <img width="1289" height="2880" alt="Pasted image 20261025173249" src="https://github.com/user-attachments/assets/6cdcafac-3617-434a-b851-85781b80c696" /> | <img width="1263" height="2853" alt="Pasted image 20261025173327" src="https://github.com/user-attachments/assets/298e9d24-ed9e-459e-af69-37382afa9197" /> |
| <img width="1235" height="2747" alt="Pasted image 20261025173231" src="https://github.com/user-attachments/assets/a3ac6539-3247-4038-8993-5d8c2a33a403" /> |<img width="1289" height="2890" alt="Pasted image 20261025173300" src="https://github.com/user-attachments/assets/c78786c8-b4dd-414b-910c-edbe5950dec2" /> | <img width="1418" height="2703" alt="Pasted image 20261025173253" src="https://github.com/user-attachments/assets/c14eec53-10d1-4054-9e9b-9012b1430f30" />
 |  |


|            手机遥控采集（选标签 / 开始 / 停止保存）             |
| :--------------------------------------------: |
| <img width="1080" height="2388" alt="0b0207c2109eb4d5704b9d1621772fa6" src="https://github.com/user-attachments/assets/0dc09282-86e0-4355-85cd-1cb4460cdff6" /> |


---

## 项目简介

本项目把一块 4×3 的 FSR 压力传感阵列做成"智能床垫"，在 **ESP32-S3 上完成从采集、端侧推理到多端输出的完整闭环**：

- **边缘推理**：INT8 量化模型仅 **3.8 KB**，TFLite Micro 在 MCU 内完成推理，不依赖云端算力；
- **状态可信**：用"压力总和闸门"处理离床、用"滑动窗口投票"平滑瞬时误判，输出的是稳定状态而非单帧噪声；
- **全链路可复现**：数据采集 → 训练 → 量化 → C 数组 → 固件内嵌，脚本化、可增量重训；
- **多端一致**：串口 / TCP / MQTT / HTTP 四路输出共用同一个设备端稳定状态，天然一致。

| 关键指标  | 数值                                     |
| ----- | -------------------------------------- |
| 传感阵列  | 12 路 FSR（4×3：肩/背/腰/臀 × 左/中/右）          |
| 采集帧率  | 10 Hz（12 路全阵列扫描，单帧约 19 ms）             |
| 模型    | MLP 12→16→16→3，531 参数                  |
| 量化模型  | INT8，**3928 字节（3.84 KB）**              |
| 训练数据  | 3518 条真实样本（3 类）                        |
| 验证准确率 | **99.86%**                             |
| 通信    | MQTT over TLS(8883)、TCP(3333)、HTTP(80) |
|       |                                        |

---

## 系统架构

### 硬件链路

```
FSR×12 ──分压(10kΩ)──▶ CD74HC4067(16选1) ──SIG──▶ ADS1115(16bit, I2C)
                                                        │ SDA/SCL
                                                        ▼
                                                    ESP32-S3
```

### 软件总体架构

固件采用 **"分层组件 + 数据流驱动"** 的结构：配置层与驱动层提供无状态的基础能力，任务层构成一条三级处理流水线，网络层负责连接生命周期与数据出口，`main` 只做初始化编排。任务之间不直接调用，所有业务数据单向流动，仅通过**队列**与**共享快照**交互。

| 层 | 组件 / 文件 | 职责 | 对外接口 |
|---|---|---|---|
| 编排层 | `main/app_main.c` | 启动顺序、队列与任务创建 | — |
| 配置层 | `components/common/config.h` | 引脚、运行参数、优先级、数据结构，全项目唯一配置源 | 宏 / 结构体 / 枚举 |
| 驱动层 | `components/drivers/`（`mux.c`、`ads1115.c`） | 多路复用选通、ADC 寄存器级读写；不感知任务与业务 | `mux_init/select`、`ads1115_init/read` |
| 任务层 | `components/tasks/`（`sensor.c`、`ai.cpp`、`mqtt.c`） | 采集调度、推理与决策、上报策略 | `sensor_task`、`ai_task`、`mqtt_task` |
| 网络层 | `components/network/`（WiFi、TCP、MQTT、HTTP、快照） | 连接建立/重连、协议收发、最新状态共享 | `wifi_sta_*`、`tcp_server_*`、`mqtt_client_*`、`web_server_start`、`status_store_*` |
| 模型数据 | `components/ai_model/` | 只读的模型字节数组 | `model_tflite[]`、`model_tflite_len` |

**依赖方向单向、无环**：

```
main ──▶ tasks ──▶ drivers ──▶ common
           │            ▲
           ▼            │
        network ────────┘
```

这样改驱动不会触碰业务逻辑，换模型不需要改任务代码，新增网络出口也不影响采集与推理。

### 启动与初始化

`app_main` 不做业务，只按依赖顺序完成六件事：

| 顺序  | 动作                         | 为什么必须在此处                          |
| --- | -------------------------- | --------------------------------- |
| 1   | NVS 初始化                    | WiFi 驱动依赖 NVS 存储校准数据；失败时自动擦除重试    |
| 2   | 驱动初始化（MUX / I2C / ADS1115） | 任务一旦创建即可能访问硬件，硬件必须先就绪             |
| 3   | WiFi 连接（最长阻塞 30 s，失败继续）    | TCP / MQTT 依赖网络栈；失败不阻塞启动，后台持续重连   |
| 4   | TCP 服务器、HTTP 服务器           | `bind/listen` 不依赖是否拿到 IP，可提前就绪    |
| 5   | 创建两个队列                     | 任务启动后第一件事就是阻塞在队列上，队列句柄必须先存在       |
| 6   | 创建三个任务                     | 所有依赖（硬件 / 网络 / 服务器 / 队列）已备齐，流水线启动 |

`app_main` 返回后由 FreeRTOS 调度器接管，三个任务按优先级运行。

### 任务与队列模型

| 任务 | 优先级 | 栈（字节） | 触发方式 | 阻塞对象 | 关键策略 |
|---|---|---|---|---|---|
| `sensor_task` | 5（最高） | 6144 | 10 Hz 周期 | 延时链表（`vTaskDelayUntil`） | 周期基准自动纠偏，扫描耗时被扣除 |
| `ai_task` | 4 | 8192 | 每帧 | `sensor_queue`（`portMAX_DELAY`） | 单次推理毫秒级，处理完立即回到等待 |
| `mqtt_task` | 3（最低） | 6144 | 每帧取结果 | `result_queue`（`portMAX_DELAY`） | 变化即发 + 30 s 心跳，QoS1 |

| 队列 | 深度 | 元素 | 生产者 | 消费者 | 满时行为 |
|---|---|---|---|---|---|
| `sensor_queue` | 5 | `pressure_frame_t`（12 路值 + 时间戳） | `sensor_task` | `ai_task` | 生产侧超时 0，**丢帧不阻塞采集** |
| `result_queue` | 5 | `posture_result_t`（状态 + 置信度 + 三类概率 + 时间戳） | `ai_task` | `mqtt_task` | 非阻塞发送，满时丢弃旧帧 |

设计要点：

- **优先级按"时间敏感度"划分**：采集是硬周期任务，错过一个周期就丢数据；推理是软实时；网络受制于远端，天然最慢。
- **采集侧绝不等待下游**：队列发送超时为 0。生产 10 Hz、消费小于 2 ms，正常不会满；深度 5 只是抖动缓冲，同时也是天然限流。
- **周期任务用 `vTaskDelayUntil`**：以"基准 + 周期"计算唤醒时刻，扫描耗时不累积到下一帧，帧间隔稳定在 100 ms。

### 数据流与出口

系统的数据流是单向的，分四级：

1. **采集**：`sensor_task` 依次选通 12 路，读 ADC、打时间戳、组装一帧；同一帧同时送往串口、TCP 和 `sensor_queue`。
2. **推理**：`ai_task` 从 `sensor_queue` 取帧，经"离床闸门 → 归一化 → 量化 → TFLM 推理 → argmax / 反量化 → 滑动窗口投票"，得到稳定状态。
3. **分发**：稳定状态写入两处——`result_queue`（事件流，供 MQTT 使用）和 `status_store`（最新值快照，供 HTTP 使用）。
4. **出口**：
   - 串口与 TCP（3333）：原始帧 `DATA:<ts>,p0..p11`，用于电脑端采集与调试；
   - MQTT（TLS，8883）：状态事件与心跳，供云端与远程客户端；
   - HTTP（80）：`GET /state` 返回状态、三类概率与最近一帧原始值，供手机网页轮询。

四种出口的数据都源自设备端的同一个 `stable_state`，不存在"某一路显示不同结果"的情况。

### 关键机制

**端侧推理（TFLite Micro）**

- 模型以 flatbuffer 形式存放在 Flash（`const` 数组），`GetModel` 零拷贝解析；
- 只注册实际用到的 3 个算子（全连接 / ReLU / Softmax），TFLM 按需裁剪固件体积；
- 推理在预分配的 8 KB tensor arena 内完成，`AllocateTensors` 一次性规划所有中间张量，**稳态运行无动态分配**；
- 量化参数（scale / zero_point）在运行时从输入输出张量读取，替换模型不需要修改固件代码；
- 每帧推理前后可打印耗时（`esp_timer`），便于验证实时性。

**训练 / 部署一致性**

训练侧与推理侧对同一帧做完全相同的预处理（每路除以本帧最大值）。为保证这一点，`convert.py` 直接复用 `train.py` 的归一化函数——**同一份代码**，而不是两份"看起来一样"的实现。量化转换完成后，`convert.py` 会用全部数据复核 keras 与 tflite 的准确率，量化损失可量化、可追溯。

**状态可信性：规则闸门 + 滑动窗口投票**

- 模型是闭集分类器，空床输入也会被强制归入某一类。因此推理前先判断 12 路总和：低于阈值直接输出"离床"，不进入模型。空床实测总和 5~11，在床最低数千，可分性以数量级计。
- 单帧判断存在抖动（躺下的过渡动作可能持续 1~2 秒）。实现 50 帧（5 秒）滑动窗口投票：窗口内多数状态胜出，平票保持当前状态（迟滞）。新状态需要赢过窗口一半，切换延迟约 2.5 秒，窗口长度可配置。
- 平滑在设备端完成，因此串口、TCP、MQTT、网页读到的都是同一个已经稳定的状态。

**网络层设计**

- **WiFi**：事件回调只做置位与日志，任务侧通过事件组同步"已连接"状态；断线后无限重连（床边设备需要自愈能力）；断开时清除连接标志，避免状态查询返回过期结果；启动 SNTP 对时——TLS 证书校验依赖准确的系统时间。
- **TCP 采集服务器（3333）**：使用 `select()` 同时监视监听 socket 与当前连接；新客户端接入时自动替换旧连接（电脑端工具重启无需等待超时）；互斥锁保护 socket 句柄，遵循"谁拥有谁关闭"：发送失败只 `shutdown`，由 accept 任务统一 `close`，避免 double close 与 fd 复用误伤；发送侧设置 200 ms 超时，防止坏客户端拖住采集任务。
- **MQTT（TLS）**：`mqtts://` 连接 + 内置 CA 证书仓库校验；上报策略为"状态变化立即发 + 30 秒心跳"，QoS1；心跳同时承担设备存活指示；消息携带三个类别概率。
- **HTTP（80）**：`index.html` 通过 `EMBED_FILES` 在编译期嵌入固件，由 `esp_http_server` 直接发送；`/state` 走"拉模式"，页面每秒轮询，不引入 WebSocket 等额外协议。

**并发与共享数据**

| 机制 | 使用位置 | 选择的理由 |
|---|---|---|
| 队列（阻塞式） | 任务间传递数据帧 / 结果 | 事件流模型，天然解耦与限流 |
| 互斥锁 | TCP 服务器中的 socket 句柄 | 保护"取 fd → send 完成"整个事务不被换连接打断 |
| 端口临界区（`portMUX`） | `status_store` 的结果与最新帧快照 | 只拷贝几十字节，微秒级，比互斥锁更轻 |
| `volatile` 标志 | MQTT 连接状态 | 单向查询，不需要等待与唤醒 |

**C / C++ 混编**

TFLite Micro 是 C++ 库（类、命名空间、模板），因此推理任务为 `ai.cpp`；其余驱动、任务、网络代码保持 C。所有供 C++ 调用的 C 模块（如 `status_store`）以及供 C 调用的 C++ 任务入口（`ai_task`），均在共享头文件中使用 `extern "C"` 包裹，避免 C++ name mangling 造成链接失败。

### 内存与资源

- 模型数组 `const` 存放于 Flash（.rodata），不占 RAM；
- tensor arena 为 8 KB 静态数组，启动日志打印实际用量；
- 任务栈按调用深度分配：传感器 6144、推理 8192、通信 6144 字节；
- `sdkconfig` 配置为 16 MB Flash + 1.5 MB app 分区（默认 1 MB 放不下 WiFi + TLS + TFLM 组合）；
- 稳态运行不使用动态内存，行为与耗时具备确定性。

### 可靠性设计

- WiFi 无限重连，断开即重试，不因路由器短时重启而永久离线；
- TCP 发送设置超时，坏连接只影响一帧，不影响采集周期；
- MQTT 由 esp-mqtt 内部自动重连，心跳间隔与上报策略解耦；
- 采集工具断线自动重连，服务端新连接自动替换旧连接；
- 监视网页在设备不可达时回退显示最后一次有效数据；
- 时间戳采用开机毫秒（约 49 天回绕），满足训练数据对齐需求。

---

## 功能特性

- 12 路压力 10 Hz 实时采集（16 位 ADC）
- 端侧 TFLite Micro 推理：仰卧 / 左侧卧 / 右侧卧 / 离床
- 离床规则闸门 + 50 帧滑动窗口投票
- WiFi 断线无限重连、SNTP 时间同步
- MQTT over TLS 上云（状态变化 + 心跳，含三类概率）
- 设备自带 HTTP 网页（手机查看状态 / 12 路值 / 概率）
- 局域网网页遥控采集（选标签 / 开始 / 停止并保存）
- 本地实时监视网页（12 路网格 + 最终判断 + 概率条）
- 数据驱动的训练流水线（新增数据重跑即可，不改代码）

---

## 硬件

| 器件 | 数量 | 说明 |
|---|---|---|
| ESP32-S3 N16R8 | 1 | 16 MB Flash + 8 MB PSRAM |
| FSR402 薄膜压力传感器 | 12 | 4×3 阵列 |
| CD74HC4067 | 1 | 16 选 1 多路复用，使用 C0~C11 |
| ADS1115 | 1 | 16 位 ADC，I2C 地址 0x48（ADDR 接 GND） |
| 10 kΩ 电阻 | 12 | FSR 分压 |
| 面包板 / 杜邦线 | - | 原型搭建 |

**引脚分配**

| 功能 | GPIO |
|---|---|
| MUX S0 / S1 / S2 / S3 | GPIO4 / 5 / 6 / 7 |
| I2C SDA / SCL | GPIO8 / 9 |
| ADS1115 ADDR / MUX EN | GND |
| 禁用 | GPIO35/36/37（PSRAM 占用） |

分压电路与量程：

```
3.3V ── FSR ──┬── 10kΩ ── GND
              └── Cx（接 MUX 通道）
Vout = 3.3V × 10kΩ / (R_FSR + 10kΩ)
```

ADS1115 采用 ±4.096 V 量程（1 LSB = 125 µV）、860 SPS 单次转换，转换完成通过轮询 OS 状态位确认。

---

## 软件结构

```
SleepPosture/
├── ai/
│   ├── data/                 采集数据（ai/data/<数据集>/*.csv）
│   ├── models/               训练产物（.keras / .tflite / 训练报告）
│   └── scripts/
│       ├── train.py          训练 + 评估 + 报告
│       ├── convert.py        INT8 量化 + 转换后精度验证
│       └── to_c_array.py     .tflite → model_data.c
├── firmware/                 ESP-IDF 工程（见上文组件结构）
├── tools/
│   ├── collect_core.py       采集核心（TCP 连接 / 收帧 / 批次 / 存 CSV）
│   ├── collect_data.py       电脑键盘采集（0/1/2 打标、定时采集、确认保存）
│   ├── web_collect.py        手机遥控采集（局域网网页，端口 8000）
│   ├── web_viewer.py         手机实时监视（12 路值 / 判断 / 概率，端口 8001）
│   ├── idle_sum.py           空载压力总和漂移观察
│   ├── test_tcp.py           TCP 链路最小验证
│   └── pressure_monitor.html 串口版热力图（调试用）
└── docs/images/              README 图片
```

---

## 快速开始

### 1. 编译与烧录

ESP-IDF v5.4：

```bash
cd firmware
idf.py build
idf.py flash monitor
```

- 首次编译会由组件管理器自动下载 `espressif/esp-tflite-micro`（见 `components/tasks/idf_component.yml`）；
- `sdkconfig` 已配置为 **16 MB Flash + 1.5 MB app 分区**（默认 1 MB 分区放不下 WiFi + TLS + TFLM）；
- 串口应输出 WiFi/IP、`模型加载完成: arena 用 N/8192 字节` 与每帧识别日志。

### 2. 配置

- WiFi：`firmware/components/network/wifi_sta.c` 中的 `WIFI_SSID` / `WIFI_PASSWORD`；
- MQTT：`firmware/components/network/mqtt_app.c` 中的 Broker 地址、用户名、密码与主题。

> 公开仓库前请替换为自己的配置，不要把真实凭据提交到 GitHub。

### 3. 使用工具

```bash
# 电脑键盘采集（0/1/2 打标，空格开始/结束，结束时确认保存）
python tools/collect_data.py

# 手机遥控采集：电脑运行后，手机浏览器打开 http://<电脑IP>:8000/
python tools/web_collect.py

# 手机实时监视：电脑运行后，手机浏览器打开 http://<电脑IP>:8001/
python tools/web_viewer.py
```

- `web_collect.py` / `collect_data.py` **不要同时运行**：设备 TCP 采集服务器同一时间只服务一个客户端；
- `web_viewer.py` 走设备 HTTP `/state`，**可与采集工具同时运行**；
- 采集文件保存为 `ai/data/<数据集>/posture_<标签>_<时间>.csv`；
- 设备 IP 由 DHCP 分配，可能变化——建议在路由器做"地址保留"绑定 MAC。

---

## 数据采集与模型训练

### 数据

- 受试者 `zdz`（172.5 cm / 65 kg），单床原型；
- 3 类姿势共 **3518 条**样本（仰卧 1192 / 左侧卧 1133 / 右侧卧 1193），10 Hz 采集；
- 采集工具支持键盘打标、定时采集（5 秒准备倒计时）与批次保存确认。

### 训练

`train.py` 自动扫描 `ai/data/*/` 下全部数据集，丢弃未标注与坏行，按类别分层划分（seed=42），训练 MLP 12→16→16→3（Adam + EarlyStopping），输出 `.keras` 与训练报告。

```bash
python ai/scripts/train.py          # 训练
python ai/scripts/convert.py        # INT8 量化 + 全量数据复核精度
python ai/scripts/to_c_array.py     # 生成 model_data.c
```

**最终一次训练结果**（`ai/models/posture_model_20261025_1616_report.txt`）：

| 项目 | 数值 |
|---|---|
| 样本 | 3518 条（训练 2816 / 验证 702） |
| 验证准确率 | **99.86%** |
| 验证损失 | 0.0040 |
| 量化模型 | 3928 字节（3.84 KB） |

混淆矩阵（行=真实，列=预测）：

| | 仰卧 | 左侧卧 | 右侧卧 |
|---|---|---|---|
| **仰卧** | 237 | 0 | 1 |
| **左侧卧** | 0 | 226 | 0 |
| **右侧卧** | 0 | 0 | 238 |

每类指标：

| 类别 | 精确率 | 召回率 |
|---|---|---|
| 仰卧 | 1.000 | 0.996 |
| 左侧卧 | 1.000 | 1.000 |
| 右侧卧 | 0.996 | 1.000 |

> **说明**：当前验证集与训练集来自同一受试者的同期录制（不同时间段），指标偏乐观；跨人 / 跨床的泛化能力尚未验证，是下一步工作。

---

## 通信协议

| 通道 | 格式 |
|---|---|
| 串口 / TCP `:3333` | `DATA:<开机毫秒>,p0,p1,...,p11\n` |
| MQTT `sleepposture/status` | `{"posture":1,"name":"side_left","confidence":98,"timestamp":123456,"probs":[1,98,1]}` |
| HTTP `/state` | `{"ready":true,"posture":1,"name":"side_left","confidence":98,"timestamp":123456,"probs":[1,98,1],"vals":[p0..p11]}` |

`posture`：0=仰卧，1=左侧卧，2=右侧卧，255=离床；`probs` 为三类概率（0~100，离床帧为 0）。

---

## 引脚连接

|          系统实物          |                        |
| :--------------------: | ---------------------- |
| <img width="4320" height="3240" alt="DSCN4949" src="https://github.com/user-attachments/assets/756d78b9-6db8-4001-a39d-6955073bfc93" /> | <img width="4320" height="3240" alt="DSCN4950" src="https://github.com/user-attachments/assets/68a0f8e0-badc-46b1-8935-599cbdb0618a" /> |


引脚连接：
ESP32-S3 → CD74HC4067

| ESP32-S3 | CD74HC4067 |
| -------- | ---------- |
| GPIO4    | S0         |
| GPIO5    | S1         |
| GPIO6    | S2         |
| GPIO7    | S3         |
| 3.3V     | VCC        |
| GND      | GND        |
| GND      | EN         |

## ESP32-S3 → ADS1115

| ESP32-S3 | ADS1115 |
|----------|---------|
| 3.3V | VCC |
| GND | GND |
| GPIO8 | SDA |
| GPIO9 | SCL |
| GND | ADDR |

## CD74HC4067 → ADS1115

| CD74HC4067 | ADS1115 |
|------------|---------|
| SIG | A0 |

## FSR分压电路 → CD74HC4067

| FSR | 分压中间节点接 |
|-----|--------------|
| FSR0 | C0 |
| FSR1 | C1 |
| FSR2 | C2 |
| FSR3 | C3 |
| FSR4 | C4 |
| FSR5 | C5 |
| FSR6 | C6 |
| FSR7 | C7 |
| FSR8 | C8 |
| FSR9 | C9 |
| FSR10 | C10 |
| FSR11 | C11 |

分压电路：
```
3.3V ── FSR ──┬── 10kΩ ── GND
              └── Cx
```

---

## END

个人学习与求职作品，代码仅供学习交流参考。
