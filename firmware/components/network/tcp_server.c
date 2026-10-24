/**
 * tcp_server.c
 * TCP 服务器实现（端口 3333）
 *
 * 作用：
 *   在 ESP32 上开一个 TCP 服务器，电脑端的采集工具（collect_data.py）
 *   作为客户端连上来，sensor_task 每采集一帧就通过这条连接发出去。
 *   服务器在 ESP32、客户端在电脑——这样电脑不用开入站端口，
 *   ESP32 也不用知道电脑的 IP（电脑的 IP 同样是 DHCP 租的，会变）。
 *
 * 两个任务都会碰到"客户端socket"：
 *   - 网络连接 accept 任务：拥有 socket（负责 accept / 监视 / 替换 / close）
 *   - 采集 sensor 任务：只负责调用 tcp_server_send_line() 发送
 * 用一个互斥锁保护 s_client_fd 的读写，保证"取到fd → send完成"期间
 * fd 不会被换掉或关闭；发送失败时只 shutdown 不 close，
 * 让 accept 任务走正常流程关闭（谁拥有、谁关闭，避免 double close）。
 *
 * 状态机（accept 任务的主循环）：
 *   [等连接] --accept()--> [监视客户端] --断开/出错/被新客户端替换--> [清理] --> 回到[等连接]
 *
 * 协议约定：TCP 是字节流、没有"消息边界"，
 *   所以用 '\n' 分帧：每帧发送 "DATA:时间戳,p0,...,p11\n"，
 *   接收端（电脑Python）按行切分。半包/粘包由接收端自行处理。
 */

#include "tcp_server.h"       // 本模块的对外接口声明
#include "config.h"           // PRIORITY_TCP 等配置
#include "esp_log.h"          // ESP_LOGI / ESP_LOGW / ESP_LOGE
#include "freertos/FreeRTOS.h"    // FreeRTOS 基础定义（portMAX_DELAY 等）
#include "freertos/task.h"        // xTaskCreate / vTaskDelay
#include "freertos/semphr.h"      // 互斥锁：xSemaphoreCreateMutex / Take / Give
#include "lwip/sockets.h"     // socket/bind/listen/accept/recv/send/select
#include "lwip/inet.h"        // inet_ntoa_r：把二进制IP转成可读字符串
#include <errno.h>            // errno：socket API 出错时的错误码

static const char *TAG = "tcp_server";

// 监听队列长度：同时只允许1个连接排队。
// backlog 的含义：内核为"已经完成三次握手、但还没被 accept() 取走"的连接
// 最多排队几个。我们只服务一台电脑，1 就够了。
#define TCP_LISTEN_BACKLOG  1
// 发送超时（毫秒）：客户端卡死（不收数据）时 send() 最多阻塞这么久。
// 这是为了"宁可丢帧，不能卡采集"——sensor_task 优先级最高，
// 决不能因为一个坏客户端被无限期阻塞。
#define TCP_SEND_TIMEOUT_MS 200

// 监听 socket（"前台"）：只负责等待新连接，不传输数据。
// -1 表示尚未创建或创建失败。生命周期：tcp_server_start() 创建，永久存在。
static int s_listen_fd = -1;
// 当前客户端 socket（"接通后的电话线"）：所有实际的数据发送都走它。
// -1 表示当前没有客户端。由 accept 任务发布/替换/清除，
// 由 sensor 任务在发送时读取——所以它的读写必须加锁。
static int s_client_fd = -1;
// 保护 s_client_fd 的互斥锁。
// 句柄初值 NULL：创建之前，所有使用它的函数都会先检查（防御"服务器没启动"）。
// 锁对象本体在堆里，这个变量只存它的地址（句柄）。
static SemaphoreHandle_t s_fd_mutex = NULL;

// ============================================================
// 给客户端socket设置选项（每次接入/替换时都要设一遍）
//
// 注意：setsockopt 是针对"单个 socket"设置的，不会从 listen fd 继承。
// 所以 accept() 出来一个新 fd 后，必须重新设置一次。
// ============================================================
static void set_client_opts(int fd)
{
    // TCP_NODELAY：关闭 Nagle 算法，让小包立即发送。
    // Nagle 算法默认会把小数据"攒一攒"再发（提高带宽利用率），
    // 但我们每帧才100字节、且要求实时——攒批毫无意义，只会增加延迟。
    int nodelay = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

    // SO_SNDTIMEO：发送超时200ms。客户端异常卡死、发送缓冲区满时，
    // send() 默认会一直阻塞；设了超时后最多等200ms就返回错误——
    // 保证 sensor_task 不会被拖住（宁可丢帧，不能卡采集）。
    //（注释里写"200ms"，下面拆成秒+微秒两个字段传给内核）
    struct timeval sndto = { .tv_sec = TCP_SEND_TIMEOUT_MS / 1000,
                             .tv_usec = (TCP_SEND_TIMEOUT_MS % 1000) * 1000 };
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &sndto, sizeof(sndto));
}

// ============================================================
// accept任务：管"客户端的一生"（等连接 → 监视 → 清理）
//
// 整个任务是一个三层循环：
//   阶段1：阻塞等第一个客户端
//   阶段2：同时盯"老客户端"和"新客户端"，直到老客户端断开/出错
//   阶段3：摘牌 + 关闭旧 socket，回到阶段1
// ============================================================
static void tcp_accept_task(void *arg)
{
    while (1) {
        // ============ 阶段1：等着，直到有客户端连进来 ============
        // accept() 本身是阻塞调用：没有新连接时，任务会挂起（不占用CPU），
        // 直到有客户端完成三次握手才返回，并带回一个"专门和它通话"的新fd。
        struct sockaddr_in client_addr;         // 用来接收"对方是谁"
        socklen_t addr_len = sizeof(client_addr);
        int fd = accept(s_listen_fd, (struct sockaddr *)&client_addr, &addr_len);
        if (fd < 0) {
            // accept 失败（极少见：资源耗尽等）。歇500ms再试，防止日志刷屏。
            ESP_LOGW(TAG, "accept失败: errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(500));    // 出错了歇一下，防止刷屏
            continue;                          // 跳到开头继续循环
        }

        // accept 返回时，内核已经把对端的地址信息填进 client_addr 了，
        // 下面把它转成可读的字符串打个日志（仅调试用）。

        char ip[16];    
        inet_ntoa_r(client_addr.sin_addr, ip, sizeof(ip));
        // inet_ntoa_r 需要调用者提供缓冲区：意思就是把地址写入ip这个缓冲区（_r = 可重入版本）
        ESP_LOGI(TAG, "客户端已连接: %s", ip);
        set_client_opts(fd);    // 给客户端socket设置选项（NODELAY/发送超时）

        // 登记为"当前客户端"——必须加锁！
        // 因为从这一刻起 sensor_task 就可能来读 s_client_fd 并发数据，
        // 不能让"发布"这个动作被看到一半。
        //（互斥加锁，因为 sensor 任务也在读它：
        //  如果拿不到锁就阻塞在这里等待，等到 sensor 任务 give 出锁）
        xSemaphoreTake(s_fd_mutex, portMAX_DELAY);
        s_client_fd = fd;
        xSemaphoreGive(s_fd_mutex);

        // ============ 阶段2：同时盯着"老客户端"和"新客户端" ============
        // 用 select 同时等两个 socket：
        //   s_listen_fd 有动静 = 有新的电脑工具在连（比如工具重启了）
        //   fd          有动静 = 老客户端发东西 / 断开了
        //
        // 为什么用 select 而不是直接再 accept 或 recv：
        //   一个任务没法同时对两个 socket 做阻塞调用（会卡死在其中一个上）。
        //   select 是"多路复用"：一次等待、监视多个 fd，任意一个就绪就返回，
        //   告诉我们"是谁就绪了"（看 fd_set）。
        while (1) {
            /*
                fd 是让一个线程同时"监视"多个 socket，哪个有动静就处理哪个
                select / fd_set 是 POSIX 提供的 I/O 多路复用（I/O multiplexing）系统调用
                POSIX 是标准，不是库。
                fd_set / FD_SET / select 是 POSIX C 标准定义的、由 C 运行库（glibc/musl）提供定义和实现的接口，
                用 C 语言写，属于 C 的系统编程接口，但不属于 ISO C 标准库。
            */
            fd_set rset;                  // fd 集合（本质是一个位图）
            FD_ZERO(&rset);               // 清空集合
            FD_SET(s_listen_fd, &rset);   // 关注：有没有新连接
            FD_SET(fd, &rset);            // 关注：老客户端有没有动静
            // select 的约定：要传"最大fd + 1"，因为它会扫描 0~maxfd 的所有位。
            int maxfd = (s_listen_fd > fd ? s_listen_fd : fd) + 1;

            // 最多等1秒就返回一次（防止永久阻塞，便于周期性重来）。
            // 注意 select 会修改 rset（只留下就绪的位），
            // 所以每轮循环都必须重新 FD_ZERO / FD_SET。
            struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };   // 每秒醒一次
            int r = select(maxfd, &rset, NULL, NULL, &tv);
            if (r < 0) {
                // 被信号打断（EINTR）属正常，重试即可；其它错误就断开重来。
                if (errno == EINTR) continue;      // 被信号打断，重试即可
                ESP_LOGW(TAG, "select出错: errno=%d", errno);
                break;
            }
            if (r == 0) continue;                  // 只是超时（1秒内两边都没动静），继续盯

            // ---- A. 有新客户端在等 → 新连接上位，旧的踢掉 ----
            // 场景：电脑端工具重启了，旧连接其实已经死了但还没被察觉；
            // 让新连接直接替换，用户不用干等旧连接超时（体验更顺）。
            if (FD_ISSET(s_listen_fd, &rset)) {
                struct sockaddr_in new_addr;
                socklen_t new_len = sizeof(new_addr);
                int newfd = accept(s_listen_fd, (struct sockaddr *)&new_addr, &new_len);
                if (newfd >= 0) {
                    char new_ip[16];
                    inet_ntoa_r(new_addr.sin_addr, new_ip, sizeof(new_ip));
                    ESP_LOGI(TAG, "新客户端接入(%s)，替换旧连接", new_ip);
                    set_client_opts(newfd);

                    // 顺序很重要：先加锁把"当前客户端"换成新的，再关旧的。
                    // 换完之后，sensor 任务发的就是新连接了；
                    // 旧 fd 已不在"当前客户端"位置，没人再能取到它，
                    // 此时关闭是安全的（先取消发布，再销毁）。
                    xSemaphoreTake(s_fd_mutex, portMAX_DELAY);
                    s_client_fd = newfd;
                    xSemaphoreGive(s_fd_mutex);

                    close(fd);      // 关闭旧socket（锁外关闭，安全：已摘牌）
                    fd = newfd;     // 继续盯新客户端
                    continue;
                }
            }

            // ---- B. 老客户端有动静：看看是不是断开了 ----
            // 注意：我们只"下发"数据，正常不需要客户端发东西。
            // 这里 recv 的唯一目的，是检测"连接是否已经断开"：
            //   返回 0  = 对端正常关闭（FIN）→ 断开
            //   返回 <0 = 出错
            //   返回 >0 = 客户端真的发了数据（本项目忽略）
            if (FD_ISSET(fd, &rset)) {
                uint8_t dummy[64];      // 随手接一下，内容不用
                int rr = recv(fd, dummy, sizeof(dummy), 0);
                if (rr == 0) {
                    ESP_LOGI(TAG, "客户端已断开（正常关闭）");
                    break;              // 跳出监视循环 → 走阶段3清理
                }
                if (rr < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
                    // EAGAIN/EWOULDBLOCK = "现在没有数据"，属正常情况，忽略；
                    // 其它错误码才是真的连接异常。
                    ESP_LOGW(TAG, "客户端连接异常: errno=%d", errno);
                    break;
                }
                // rr>0：客户端发来了数据。本项目是"只下发"方向，直接忽略
                // （以后想加"电脑端发命令"的话，在这里处理）
            }
        }

        // ============ 阶段3：清理，回到开头继续等 ============
        // 同样的顺序原则：先摘牌（加锁置-1），让 sensor 任务不会再取到这个fd；
        // 摘牌之后旧 fd 已无人可达，再在锁外 close 就是安全的。
        // 反过来的顺序（先 close 再置-1）会有一个危险窗口：
        //   close 完成后、还没置-1 的瞬间，sensor 任务可能读到旧 fd 并 send，
        //   而此时 fd 号可能已被系统复用来给别的 socket！
        xSemaphoreTake(s_fd_mutex, portMAX_DELAY);
        s_client_fd = -1;               // 先摘牌（sensor从此不会再发到这个socket）
        xSemaphoreGive(s_fd_mutex);
        close(fd);                      // 后关闭
        ESP_LOGI(TAG, "已回到等待连接状态");
    }
}

// ============================================================
// 启动服务器（app_main调用，只调一次）
//
// 标准 TCP 服务器四步：socket() → bind() → listen() → accept()
// 这里完成前三步（端口占住），第四步由 accept 任务负责。
// ============================================================
void tcp_server_start(uint16_t port)
{
    // 1. 创建监听socket
    //    AF_INET = IPv4；SOCK_STREAM = TCP；IPPROTO_TCP = TCP协议。
    //    返回一个"fd"（文件描述符，本质是内核里这个socket对象的编号）。
    s_listen_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s_listen_fd < 0) {
        ESP_LOGE(TAG, "socket创建失败: errno=%d", errno);
        return;
    }

    // SO_REUSEADDR：程序重启后可以立刻重新绑定同一端口。
    // 否则刚关闭的连接会处于 TIME_WAIT 状态，端口被占着一会儿，bind 会失败。
    // 服务器程序的标准配置。
    int opt = 1;
    setsockopt(s_listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    // 2. 绑定端口
    //    sockaddr_in 是 IPv4 地址结构体：地址族 + IP + 端口。
    //    注意多字节字段要转"网络字节序"（大端）：
    //      - htonl：32位（IP地址）
    //      - htons：16位（端口）
    //    不转的话，小端机器上端口号会被对方理解成另一个数。
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);   // 监听本机所有网卡
    addr.sin_port = htons(port);                // 端口要转成网络字节序
    if (bind(s_listen_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        ESP_LOGE(TAG, "bind失败: errno=%d", errno);
        close(s_listen_fd);
        s_listen_fd = -1;       // 置-1，避免留下一个"看起来可用"的失效fd
        return;
    }

    // 3. 开始监听
    //    listen 把 socket 从"普通socket"切换到"监听状态"：
    //    内核开始接受指向这个端口的连接请求（最多 backlog 个排队等 accept）。
    if (listen(s_listen_fd, TCP_LISTEN_BACKLOG) != 0) {
        ESP_LOGE(TAG, "listen失败: errno=%d", errno);
        close(s_listen_fd);
        s_listen_fd = -1;
        return;
    }

    // 4. 创建互斥锁 + accept任务
    //    必须先创建锁、再创建任务——任务一跑起来就可能要加锁，
    //    锁不存在的话 xSemaphoreTake(NULL) 会崩。
    s_fd_mutex = xSemaphoreCreateMutex();
    if (s_fd_mutex == NULL) {
        ESP_LOGE(TAG, "互斥锁创建失败");
        return;
    }
    // accept 任务优先级 PRIORITY_TCP（3，低于采集和推理）；
    // 它平时几乎全在 accept()/select() 里阻塞，不抢CPU。
    xTaskCreate(tcp_accept_task, "tcp_accept", 4096, NULL, PRIORITY_TCP, NULL);

    ESP_LOGI(TAG, "TCP服务器已启动，监听端口 %d", port);
}

// ============================================================
// 发送一行数据（由 sensor_task 调用，每帧一次）
//
// 返回值：>=0 实际发送字节数；-1 没有客户端或发送失败（本帧丢弃）。
//
// 锁的范围覆盖整个 send：这是"保护不变量"的写法——
// 我们要保证的事实是："从取到 fd 到 send 结束，这个 fd 一直有效、
// 且仍然是当前客户端"。所以 send 必须在锁内完成，不能"读一下就放锁"。
// 代价：如果客户端卡死，sensor_task 最多等200ms（SO_SNDTIMEO 兜底）。
// ============================================================
int tcp_server_send_line(const char *buf, int len)
{
    if (s_fd_mutex == NULL) {
        return -1;          // 服务器还没启动（锁还没创建）
    }

    // 拿锁：保证手里这个fd在使用期间不会被accept任务换掉/关掉
    xSemaphoreTake(s_fd_mutex, portMAX_DELAY);
    int fd = s_client_fd;
    int ret = -1;

    if (fd >= 0) {
        // send 可能发不全：发送缓冲区满时只会发出去一部分，
        // 所以用 ret != len 判断"要么全发成功，要么算失败"。
        ret = send(fd, buf, len, 0);
        if (ret != len) {
            // 发送失败/不完整（客户端突然消失、超时……）：
            //   只 shutdown、不 close！
            //   本任务只有发送权，close 是 accept 任务的职责。
            //   shutdown(SHUT_RDWR) 让这条连接立刻"不可用"：
            //   对端收到FIN；本端 accept 任务正在等的 select/recv
            //   会立刻返回（读到0或出错），从而走它自己的清理流程（close）。
            //   这既避免 double close，也避免误关"复用了同一fd号的新连接"。
            ESP_LOGW(TAG, "发送失败(ret=%d, errno=%d)，踢掉客户端", ret, errno);
            shutdown(fd, SHUT_RDWR);
            ret = -1;
        }
    }
    // 没有客户端(fd<0)：直接丢弃，返回-1。
    // 这是设计选择：没有消费者就跳过，不缓存——
    // 训练数据只关心"有人连着的时候"的连续帧。

    xSemaphoreGive(s_fd_mutex);
    return ret;
}

// ============================================================
// 查询是否有客户端连接
//
// 即使只是读一个int，也必须加锁：
// 有写者（accept任务在改）的情况下，读者的同步是必须的。
// 返回 1=有客户端；0=没有（或服务器还没启动）。
// ============================================================
int tcp_server_client_connected(void)
{
    if (s_fd_mutex == NULL) {
        return 0;
    }
    xSemaphoreTake(s_fd_mutex, portMAX_DELAY);
    int connected = (s_client_fd >= 0) ? 1 : 0;
    xSemaphoreGive(s_fd_mutex);
    return connected;
}
