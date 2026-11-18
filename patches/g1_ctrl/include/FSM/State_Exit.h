// Copyright (c) 2025, Unitree Robotics Co., Ltd.
// All rights reserved.

#pragma once

#include "FSMState.h"
#include "FSM/McpCommand.h"
#include <vector>
#include "unitree/robot/g1/loco/g1_loco_api.hpp"
#include "unitree/robot/g1/loco/g1_loco_client.hpp"

class State_Exit : public FSMState
{
public:
    State_Exit(int state, std::string state_string = "Exit") 
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

        // [MCP 2026-11-17] 舞蹈正常结束后 CtrlFSM 会停在 Exit（原设计靠遥控键再从
        // 这里触发下一支）。这里追加与 Passive 相同的语音指令检查，纯追加；
        // 原有遥控 transitions 在基类中已先注册，优先级更高。
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

        // 状态转移改为遥控器 DSL 配置驱动（config.yaml: FSM.Exit.transitions）：
        //   F1+Y -> Box, F1+B -> Box1, F1+A -> Bow（在 FSMState 构造函数中注册）

        auto rpy = lowstate->msg_.imu_state().rpy();

        if(std::abs(rpy[0])>1.0f || std::abs(rpy[1])>0.8f)  // if the robot is in a bad state, switch to passive mode to avoid damage
        {
            loco_client_.SwitchToInternalCtrl(unitree::robot::g1::InternalFsmMode::PASSIVE);
        }
        else{
            loco_client_.SwitchToInternalCtrl(unitree::robot::g1::InternalFsmMode::WALKRUN);
        }
        lowcmd->unlockAndPublish();
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

    void exit()
    {
        // 从 Exit 进入下一支策略后清掉指令，避免残留
        g1mcp::clearCommand();
    }

private:
    unitree::robot::g1::LocoClient loco_client_;

};

REGISTER_FSM(State_Exit)
