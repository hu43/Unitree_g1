// Copyright (c) 2025, Unitree Robotics Co., Ltd.
// All rights reserved.

#pragma once

#include "FSMState.h"
#include "FSM/McpCommand.h"
#include <vector>
#include "unitree/robot/g1/loco/g1_loco_api.hpp"
#include "unitree/robot/g1/loco/g1_loco_client.hpp"

class State_Passive : public FSMState
{
public:
    State_Passive(int state, std::string state_string = "Passive") 
    : FSMState(state, state_string) 
    {
        auto motor_mode = param::config["FSM"]["Passive"]["mode"];
        if(motor_mode.IsDefined())
        {
            auto values = motor_mode.as<std::vector<int>>();
            for(int i(0); i<values.size(); ++i)
            {
                lowcmd->msg_.motor_cmd()[i].mode() = values[i];
            }
        }

        loco_client_.Init();

        // [MCP 2026-11-17] 语音指令通道（纯追加，不改原遥控逻辑）：
        // 基类已按 config.yaml 的 transitions 注册遥控器检查，优先级更高；
        // 这里只追加"按状态名进入"的检查，且只在本状态（Passive）生效。
        static const std::vector<std::string> kMcpStates = {
            "Gangnam", "Jixiewu1", "Jixiewu2", "Video27",
            "Video22", "Video25", "Kungfu3", "Su"
        };
        for (const auto &name : kMcpStates) {
            if (!FSMStringMap.right.count(name)) {
                spdlog::warn("[mcp] state '{}' not in FSMStringMap, skipped", name);
                continue;
            }
            const int fsm_id = FSMStringMap.right.at(name);
            registered_checks.emplace_back(
                std::make_pair(
                    [name]()->bool {
                        return g1mcp::peekCommand() == name;
                    },
                    fsm_id
                )
            );
        }
    } 

    void enter()
    {
        static bool first_enter = true;
        // set gain
        static auto kd = param::config["FSM"]["Passive"]["kd"].as<std::vector<float>>();
        for(int i(0); i < kd.size(); ++i)
        {
            auto & motor = lowcmd->msg_.motor_cmd()[i];
            motor.kp() = 0;
            motor.kd() = kd[i];
            motor.dq() = 0;
            motor.tau() = 0;
        }

        // 状态转移改为遥控器 DSL 配置驱动（config.yaml: FSM.Passive.transitions）：
        //   F1+Y -> Box, F1+B -> Box1, F1+A -> Bow（在 FSMState 构造函数中注册）
        if(first_enter)
        {
            first_enter = false;
        }
        else
        {
            loco_client_.SwitchToInternalCtrl(unitree::robot::g1::InternalFsmMode::PASSIVE);
        }
        lowcmd->unlockAndPublish();
    }

    void exit()
    {
        // 离开 Passive（遥控或 MCP 触发）后清掉未消费的指令，避免残留误触发
        g1mcp::clearCommand();
    }

    void run()
    {
        lowstate->update();
        if(keyboard) keyboard->update();
        for(int i(0); i < lowcmd->msg_.motor_cmd().size(); ++i)
        {
            lowcmd->msg_.motor_cmd()[i].q() = lowstate->msg_.motor_state()[i].q();
        }
    }

private:
    unitree::robot::g1::LocoClient loco_client_;
};

REGISTER_FSM(State_Passive)
