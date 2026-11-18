// 2026-11-17 新增：小野AI/MCP 语音指令通道（纯加法，不改变原遥控逻辑）
//
//   DDS 主题: rt/g1mcp/cmd   (std_msgs::msg::dds_::String_)
//   - "<状态名>" 或 "start:<状态名>"：仅在 Passive 状态触发对应 Mimic 策略
//   - "stop"：策略执行中请求退出，复用 State_Mimic 原有的 Exit 路径
//
// 说明：
//   - start 指令 3 秒未被消费自动过期，避免残留误触发。
//   - stop 指令用独立的一次性标志，不设超时：带 music_lead 的策略在 enter()
//     里会先等 4~12.5 秒才切用户控制，stop 必须能跨越这段时间。
//   - Passive::exit() 会同时清掉两者。
#pragma once

#include <algorithm>
#include <chrono>
#include <cctype>
#include <mutex>
#include <string>

namespace g1mcp {

inline std::mutex &mtx()
{
    static std::mutex m;
    return m;
}

inline std::string &slot()
{
    static std::string s;
    return s;
}

inline bool &stopFlag()
{
    static bool b = false;
    return b;
}

inline std::chrono::steady_clock::time_point &stamp()
{
    static std::chrono::steady_clock::time_point t{};
    return t;
}

inline std::string trim(const std::string &in)
{
    std::size_t b = 0;
    std::size_t e = in.size();
    while (b < e && std::isspace(static_cast<unsigned char>(in[b]))) ++b;
    while (e > b && std::isspace(static_cast<unsigned char>(in[e - 1]))) --e;
    return in.substr(b, e - b);
}

inline void setCommand(const std::string &raw)
{
    std::string text = trim(raw);
    if (text.rfind("start:", 0) == 0) {
        text = trim(text.substr(6));
    }

    std::lock_guard<std::mutex> lk(mtx());
    if (text == "stop") {
        stopFlag() = true;
        slot().clear();
    } else {
        stopFlag() = false;
        slot() = text;
        stamp() = std::chrono::steady_clock::now();
    }
}

inline std::string peekCommand()
{
    std::lock_guard<std::mutex> lk(mtx());
    if (stopFlag() || slot().empty()) {
        return "";
    }
    const auto age = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - stamp()).count();
    if (age > 3000) {
        slot().clear();
        return "";
    }
    return slot();
}

inline void clearCommand()
{
    std::lock_guard<std::mutex> lk(mtx());
    slot().clear();
    stopFlag() = false;
}

// 一次性消费 stop 请求
inline bool isStopRequested()
{
    std::lock_guard<std::mutex> lk(mtx());
    if (stopFlag()) {
        stopFlag() = false;
        return true;
    }
    return false;
}

} // namespace g1mcp
