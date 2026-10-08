/**
 * tcp_server.c
 * TCP服务器实现（【Claude修改】本次新增）
 *
 * 两个任务都会碰到"客户端socket"：
 *   - accept任务：拥有socket（负责accept/监视/close）
 *   - sensor任务：只负责调用 tcp_server_send_line() 发送
 * 用一个互斥锁保护 s_client_fd 的读写；发送失败时只shutdown不close，
 * 让accept任务走正常流程关闭（谁拥有、谁关闭，避免double close）。
 */

#include "tcp_server.h"
#include "config.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "lwip/sockets.h"     // socket/bind/listen/accept/recv/send/select
#include "lwip/inet.h"        // inet_ntoa_r
#include <errno.h>

static const char *TAG = "tcp_server";

// 监听队列长度：同时只允许1个连接排队
#define TCP_LISTEN_BACKLOG  1
// 发送超时（毫秒）：客户端卡死时send最多阻塞这么久，避免拖住sensor任务
#define TCP_SEND_TIMEOUT_MS 200

static int s_listen_fd = -1;                    // 监听socket
static int s_client_fd = -1;                    // 当前客户端socket（-1=没有客户端）
static SemaphoreHandle_t s_fd_mutex = NULL;     // 保护 s_client_fd 的互斥锁

// ============================================================
// 给客户端socket设置选项（每次接入/替换时都要设一遍）
// ============================================================
static void set_client_opts(int fd)
{
    // TCP_NODELAY：小包立即发，不等待攒批（每帧才100字节，攒批毫无意义）
    int nodelay = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

    // SO_SNDTIMEO：发送超时200ms。客户端异常卡死时 send() 不会无限等，
    // 保证 sensor_task 不会被拖住（宁可丢帧，不能卡采集）。
    struct timeval sndto = { .tv_sec = TCP_SEND_TIMEOUT_MS / 1000,
                             .tv_usec = (TCP_SEND_TIMEOUT_MS % 1000) * 1000 };
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &sndto, sizeof(sndto));
}

// ============================================================
// accept任务：管"客户端的一生"（等连接→监视→清理）
// ============================================================
static void tcp_accept_task(void *arg)
{
    while (1) {
        // ============ 阶段1：等着，直到有客户端连进来 ============
        struct sockaddr_in client_addr;
        socklen_t addr_len = sizeof(client_addr);
        int fd = accept(s_listen_fd, (struct sockaddr *)&client_addr, &addr_len);
        if (fd < 0) {
            ESP_LOGW(TAG, "accept失败: errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(500));    // 出错了歇一下，防止刷屏
            continue;
        }

        char ip[16];
        inet_ntoa_r(client_addr.sin_addr, ip, sizeof(ip));
        ESP_LOGI(TAG, "客户端已连接: %s", ip);
        set_client_opts(fd);

        // 登记为"当前客户端"（加锁，因为sensor任务也在读它）
        xSemaphoreTake(s_fd_mutex, portMAX_DELAY);
        s_client_fd = fd;
        xSemaphoreGive(s_fd_mutex);

        // ============ 阶段2：同时盯着"老客户端"和"新客户端" ============
        // 用select同时等两个socket：
        //   s_listen_fd 有动静 = 有新的电脑工具在连（比如工具重启了）
        //   fd          有动静 = 老客户端发东西 / 断开了
        while (1) {
            fd_set rset;
            FD_ZERO(&rset);
            FD_SET(s_listen_fd, &rset);
            FD_SET(fd, &rset);
            int maxfd = (s_listen_fd > fd ? s_listen_fd : fd) + 1;

            struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };   // 每秒醒一次
            int r = select(maxfd, &rset, NULL, NULL, &tv);
            if (r < 0) {
                if (errno == EINTR) continue;      // 被信号打断，重试即可
                ESP_LOGW(TAG, "select出错: errno=%d", errno);
                break;
            }
            if (r == 0) continue;                  // 只是超时，继续盯

            // ---- A. 有新客户端在等 → 新连接上位，旧的踢掉 ----
            // 场景：电脑端工具重启了，旧连接其实已经死了但还没被察觉；
            // 让新连接直接替换，用户不用干等旧连接超时。
            if (FD_ISSET(s_listen_fd, &rset)) {
                struct sockaddr_in new_addr;
                socklen_t new_len = sizeof(new_addr);
                int newfd = accept(s_listen_fd, (struct sockaddr *)&new_addr, &new_len);
                if (newfd >= 0) {
                    char new_ip[16];
                    inet_ntoa_r(new_addr.sin_addr, new_ip, sizeof(new_ip));
                    ESP_LOGI(TAG, "新客户端接入(%s)，替换旧连接", new_ip);
                    set_client_opts(newfd);

                    // 先加锁把"当前客户端"换成新的，再关旧的。
                    // 换完之后，sensor任务发的就是新连接了。
                    xSemaphoreTake(s_fd_mutex, portMAX_DELAY);
                    s_client_fd = newfd;
                    xSemaphoreGive(s_fd_mutex);

                    close(fd);      // 关闭旧socket
                    fd = newfd;     // 继续盯新客户端
                    continue;
                }
            }

            // ---- B. 老客户端有动静：看看是不是断开了 ----
            if (FD_ISSET(fd, &rset)) {
                uint8_t dummy[64];
                int rr = recv(fd, dummy, sizeof(dummy), 0);
                if (rr == 0) {
                    ESP_LOGI(TAG, "客户端已断开（正常关闭）");
                    break;
                }
                if (rr < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
                    ESP_LOGW(TAG, "客户端连接异常: errno=%d", errno);
                    break;
                }
                // rr>0：客户端发来了数据。本项目是"只下发"方向，直接忽略
                // （以后想加"电脑端发命令"的话，在这里处理）
            }
        }

        // ============ 阶段3：清理，回到开头继续等 ============
        xSemaphoreTake(s_fd_mutex, portMAX_DELAY);
        s_client_fd = -1;               // 先摘牌（sensor从此不会再发到这个socket）
        xSemaphoreGive(s_fd_mutex);
        close(fd);                      // 后关闭
        ESP_LOGI(TAG, "已回到等待连接状态");
    }
}

// ============================================================
// 启动服务器（app_main调用，只调一次）
// ============================================================
void tcp_server_start(uint16_t port)
{
    // 1. 创建监听socket
    s_listen_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s_listen_fd < 0) {
        ESP_LOGE(TAG, "socket创建失败: errno=%d", errno);
        return;
    }

    // SO_REUSEADDR：程序重启后可以立刻重新绑定同一端口
    // （否则刚用过的端口会因TIME_WAIT状态被占用一会儿）
    int opt = 1;
    setsockopt(s_listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    // 2. 绑定端口
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);   // 监听本机所有网卡
    addr.sin_port = htons(port);                // 端口要转成网络字节序
    if (bind(s_listen_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        ESP_LOGE(TAG, "bind失败: errno=%d", errno);
        close(s_listen_fd);
        s_listen_fd = -1;
        return;
    }

    // 3. 开始监听
    if (listen(s_listen_fd, TCP_LISTEN_BACKLOG) != 0) {
        ESP_LOGE(TAG, "listen失败: errno=%d", errno);
        close(s_listen_fd);
        s_listen_fd = -1;
        return;
    }

    // 4. 创建互斥锁 + accept任务
    s_fd_mutex = xSemaphoreCreateMutex();
    if (s_fd_mutex == NULL) {
        ESP_LOGE(TAG, "互斥锁创建失败");
        return;
    }
    xTaskCreate(tcp_accept_task, "tcp_accept", 4096, NULL, PRIORITY_TCP, NULL);

    ESP_LOGI(TAG, "TCP服务器已启动，监听端口 %d", port);
}

// ============================================================
// 发送一行数据（由sensor_task调用）
// ============================================================
int tcp_server_send_line(const char *buf, int len)
{
    if (s_fd_mutex == NULL) {
        return -1;          // 服务器还没启动
    }

    // 拿锁：保证手里这个fd在使用期间不会被accept任务换掉/关掉
    xSemaphoreTake(s_fd_mutex, portMAX_DELAY);
    int fd = s_client_fd;
    int ret = -1;

    if (fd >= 0) {
        ret = send(fd, buf, len, 0);
        if (ret != len) {
            // 发送失败/不完整（客户端突然消失、超时……）：
            // 不在这里close（所有权在accept任务），只shutdown，
            // accept任务的select/recv会立刻察觉，然后走正常关闭流程。
            ESP_LOGW(TAG, "发送失败(ret=%d, errno=%d)，踢掉客户端", ret, errno);
            shutdown(fd, SHUT_RDWR);
            ret = -1;
        }
    }
    // 没有客户端(fd<0)：直接丢弃，返回-1

    xSemaphoreGive(s_fd_mutex);
    return ret;
}

// ============================================================
// 查询是否有客户端连接
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
