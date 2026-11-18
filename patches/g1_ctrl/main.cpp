#include "FSM/CtrlFSM.h"
#include "FSM/State_Passive.h"
#include "FSM/State_FixStand.h"
#include "FSM/State_RLBase.h"
#include "FSM/State_Exit.h"
#include "FSM/McpCommand.h"
#include "State_Mimic.h"
#include "State_Replay.h"
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/idl/ros2/String_.hpp>

std::unique_ptr<LowCmd_t> FSMState::lowcmd = nullptr;
std::shared_ptr<LowState_t> FSMState::lowstate = nullptr;
std::shared_ptr<Keyboard> FSMState::keyboard = std::make_shared<Keyboard>();

// [MCP 2026-11-17] 语音指令订阅：rt/g1mcp/cmd (std_msgs/String_)
// 只做新增：不改变任何原有遥控器按键路径。
static std::shared_ptr<unitree::robot::ChannelSubscriber<std_msgs::msg::dds_::String_>>
    g_mcp_cmd_sub;

static void init_mcp_command_channel()
{
    g_mcp_cmd_sub = std::make_shared<
        unitree::robot::ChannelSubscriber<std_msgs::msg::dds_::String_>>(
            "rt/g1mcp/cmd",
            [](const void *msg) {
                if (!msg) return;
                const auto *str = static_cast<const std_msgs::msg::dds_::String_ *>(msg);
                g1mcp::setCommand(str->data());
                spdlog::info("[mcp] command received: {}", str->data());
            },
            10);
    g_mcp_cmd_sub->InitChannel();
    spdlog::info("[mcp] listening on rt/g1mcp/cmd");
}

void init_fsm_state()
{
    auto lowcmd_sub = std::make_shared<unitree::robot::g1::subscription::LowCmd>();
    usleep(0.2 * 1e6);
    // Do not shutdown internal control
    // if(!lowcmd_sub->isTimeout())
    // {
    //     spdlog::critical("The other process is using the lowcmd channel, please close it first.");
    //     unitree::robot::go2::shutdown();
    //     // exit(0);
    // }
    FSMState::lowcmd = std::make_unique<LowCmd_t>();
    FSMState::lowstate = std::make_shared<LowState_t>();
    spdlog::info("Waiting for connection to robot...");
    FSMState::lowstate->wait_for_connection();
    spdlog::info("Connected to robot.");
}

int main(int argc, char** argv)
{
    // Load parameters
    auto vm = param::helper(argc, argv);

    std::cout << " --- Unitree Robotics --- \n";
    std::cout << "     G1-29dof UniStore Controller \n";

    // Unitree DDS Config
    unitree::robot::ChannelFactory::Instance()->Init(0, vm["network"].as<std::string>());

    init_fsm_state();

    init_mcp_command_channel();

    FSMState::lowcmd->msg_.mode_machine() = 5; // 29dof
    if(!FSMState::lowcmd->check_mode_machine(FSMState::lowstate)) {
        spdlog::critical("Unmatched robot type.");
        exit(-1);
    }
    
    // Initialize FSM
    auto fsm = std::make_unique<CtrlFSM>(param::config["FSM"]);
    fsm->start();

    std::cout << "Remote control bindings (official wireless remote):\n"
              << "  F1 + Y    : Box      (box, 11s)\n"
              << "  F1 + A    : Box1     (box1, 11s)\n"
              << "  F1 + X    : LeftPunch   (LeftPunch, 5.4s)\n"
              << "  F1 + B    : RightPunch  (RightPunch_fix, 5.9s)\n"
              << "  Up + Y    : Video22  (video22_fix kungfu, BGM 4s lead)\n"
              << "  Up + X    : Kungfu3  (Kungfu3_fix, 92.8s, BGM: 男儿当自强 12.5s intro lead)\n"
              << "  Up + B    : Video25  (video25, BGM: kungfu 4s lead)\n"
              << "  Left + Y  : Bow      (bow, 6.8s)\n"
              << "  Left + A  : Zhanmusi (zhanmusi_fix, 9.8s)\n"
              << "  Left + X  : Su       (su, 5.1s, BGM: SIUUU, sync start)\n"
              << "  Left + B  : Video28  (video28, 20.1s)\n"
              << "  F3 + Up   : Gangnam  (gangnam_style, BGM auto, 4s lead)\n"
              << "  F3 + Left : Jixiewu1 (jixiewu1_fix, 20.6s, BGM: HOZIN 4s lead)\n"
              << "  F3 + Right: Jixiewu2 (jixiewu2_fix, 12.4s, BGM: 霓虹迪斯科 4s lead)\n"
              << "  F3 + Down : Video27  (video27_fix, BGM auto, 4s lead)\n"
              << "  Double-click Start : interrupt running policy -> Exit\n"
              << "  (F3 on web UI == F2 in SDK, same physical key, bit7)\n";

    while (true)
    {
        usleep(1000000);
    }
    
    return 0;
}

