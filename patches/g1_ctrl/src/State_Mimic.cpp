#include "State_Mimic.h"
#include "FSM/McpCommand.h"
#include "unitree_articulation.h"
#include "isaaclab/envs/mdp/observations/observations.h"
#include "isaaclab/envs/mdp/actions/joint_actions.h"
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/idl/ros2/String_.hpp>

static Eigen::Quaternionf init_quat = Eigen::Quaternionf::Identity();
std::shared_ptr<State_Mimic::MotionLoader_> State_Mimic::motion = nullptr;

// ============ 音乐事件发布（供 music_helper.py 伴随播放/停止 BGM）============
// 所有 Mimic 型状态共用一个发布器；事件格式 "start:<状态名>" / "stop:<状态名>"
static unitree::robot::ChannelPublisher<std_msgs::msg::dds_::String_>& music_event_pub()
{
    static unitree::robot::ChannelPublisher<std_msgs::msg::dds_::String_> pub("rt/g1joystick/event");
    static bool inited = [](){ pub.InitChannel(); return true; }();
    (void)inited;
    return pub;
}

void publish_music_event(const std::string& text)
{
    try {
        std_msgs::msg::dds_::String_ msg;
        msg.data() = text;
        // 实测 BEST_EFFORT 单帧会偶发丢失（5 次 start 丢 2 次、stop 全丢），
        // 连发 3 帧（30ms 间隔）；music_helper 侧对 1s 内同名事件去重。
        for (int i = 0; i < 3; ++i) {
            music_event_pub().Write(msg);
            if (i < 2) usleep(30000);
        }
        spdlog::info("[music] event: {} (x3)", text);
    } catch (const std::exception& e) {
        spdlog::warn("[music] publish failed: {}", e.what());
    }
}

Eigen::Quaternionf torso_quat_w(isaaclab::ManagerBasedRLEnv* env) {
    using G1Type = unitree::BaseArticulation<LowState_t::SharedPtr>;
    G1Type* robot = dynamic_cast<G1Type*>(env->robot.get());

    auto root_quat = env->robot->data.root_quat_w;
    auto & motors = robot->lowstate->msg_.motor_state();

    Eigen::Quaternionf torso_quat = root_quat \
        * Eigen::AngleAxisf(motors[12].q(), Eigen::Vector3f::UnitZ()) \
        * Eigen::AngleAxisf(motors[13].q(), Eigen::Vector3f::UnitX()) \
        * Eigen::AngleAxisf(motors[14].q(), Eigen::Vector3f::UnitY()) \
    ;
    return torso_quat;
};

Eigen::Quaternionf anchor_quat_w(std::shared_ptr<State_Mimic::MotionLoader_> loader)
{
    const auto root_quat = loader->root_quaternion();
    const auto joint_pos = loader->joint_pos();
    Eigen::Quaternionf torso_quat = root_quat \
        * Eigen::AngleAxisf(joint_pos[12], Eigen::Vector3f::UnitZ()) \
        * Eigen::AngleAxisf(joint_pos[13], Eigen::Vector3f::UnitX()) \
        * Eigen::AngleAxisf(joint_pos[14], Eigen::Vector3f::UnitY()) \
    ;
    return torso_quat;
}


namespace isaaclab
{
namespace mdp
{

REGISTER_OBSERVATION(motion_joint_pos)
{
    auto & robot = env->robot;
    auto & loader = State_Mimic::motion;
    auto & ids = robot->data.joint_ids_map;

    auto data_dfs = loader->joint_pos();
    Eigen::VectorXf data_bfs = Eigen::VectorXf::Zero(data_dfs.size());
    for(int i = 0; i < data_dfs.size(); ++i) {
        data_bfs(i) = data_dfs[ids[i]];
    }
    return std::vector<float>(data_bfs.data(), data_bfs.data() + data_bfs.size());
}

REGISTER_OBSERVATION(motion_joint_vel)
{
    auto & robot = env->robot;
    auto & loader = State_Mimic::motion;
    auto & ids = robot->data.joint_ids_map;

    auto data_dfs = loader->joint_vel();
    Eigen::VectorXf data_bfs = Eigen::VectorXf::Zero(data_dfs.size());
    for(int i = 0; i < data_dfs.size(); ++i) {
        data_bfs(i) = data_dfs[ids[i]];
    }
    return std::vector<float>(data_bfs.data(), data_bfs.data() + data_bfs.size());
}

REGISTER_OBSERVATION(motion_command)
{
    auto & robot = env->robot;
    auto & loader = State_Mimic::motion;
    auto & ids = robot->data.joint_ids_map;

    auto pos_dfs = loader->joint_pos();
    Eigen::VectorXf pos_bfs = Eigen::VectorXf::Zero(pos_dfs.size());
    for(int i = 0; i < pos_dfs.size(); ++i) {
        pos_bfs(i) = pos_dfs[ids[i]];
    }
    auto vel_dfs = loader->joint_vel();
    Eigen::VectorXf vel_bfs = Eigen::VectorXf::Zero(vel_dfs.size());
    for(int i = 0; i < vel_dfs.size(); ++i) {
        vel_bfs(i) = vel_dfs[ids[i]];
    }
    std::vector<float> data;
    data.insert(data.end(), pos_bfs.data(), pos_bfs.data() + pos_bfs.size());
    data.insert(data.end(), vel_bfs.data(), vel_bfs.data() + vel_bfs.size());
    return data;
}

REGISTER_OBSERVATION(motion_anchor_ori_b)
{
    // auto & robot = env->robot;
    auto real_quat_w = torso_quat_w(env);
    auto ref_quat_w = anchor_quat_w(State_Mimic::motion);

    real_quat_w.normalize();
    ref_quat_w.normalize();
    init_quat.normalize();

    Eigen::Quaternionf rot_ = (init_quat * ref_quat_w).conjugate() * real_quat_w;
    Eigen::Matrix3f rot = rot_.toRotationMatrix().transpose();

    auto ref_quat = (init_quat * ref_quat_w);

    Eigen::Matrix<float, 6, 1> data;
    data << rot(0, 0), rot(0, 1), rot(1, 0), rot(1, 1), rot(2, 0), rot(2, 1);
    return std::vector<float>(data.data(), data.data() + data.size());
}

}
}


State_Mimic::State_Mimic(int state_mode, std::string state_string)
: FSMState(state_mode, state_string) 
{
    auto cfg = param::config["FSM"][state_string];
    auto policy_dir = param::parser_policy_dir(cfg["policy_dir"].as<std::string>());

    std::filesystem::path motion_file = cfg["motion_file"].as<std::string>();
    std::filesystem::path policy_file = cfg["policy_file"].as<std::string>();
    if(!motion_file.is_absolute()) {
        motion_file = param::proj_dir / motion_file;
    }
    if(!policy_file.is_absolute()) {
        policy_file = param::proj_dir / policy_file;
    }
    
    // Motion
    motion_ = std::make_shared<MotionLoader_>(motion_file.string(), cfg["fps"].as<float>());
    spdlog::info("Loaded motion file '{}' with duration {:.2f}s", motion_file.stem().string(), motion_->duration);
    motion = motion_;
    if(cfg["time_start"]) {
        float time_start = cfg["time_start"].as<float>();
        time_range_[0] = std::clamp(time_start, 0.0f, motion_->duration);
    } else {
        time_range_[0] = 0.0f;
    }
    if(cfg["time_end"]) {
        float time_end = cfg["time_end"].as<float>();
        time_range_[1] = std::clamp(time_end, 0.0f, motion_->duration);
    } 
    else {
        time_range_[1] = motion_->duration;
    }

    env = std::make_unique<isaaclab::ManagerBasedRLEnv>(
        YAML::LoadFile(policy_dir / "params" / "deploy.yaml"),
        std::make_shared<unitree::BaseArticulation<LowState_t::SharedPtr>>(FSMState::lowstate)
    );

    env->alg = std::make_unique<isaaclab::MNNRunner>(policy_file);

    const auto & joy = FSMState::lowstate->joystick;

    // [MCP 2026-11-17] 语音 "stop" 请求：与双击 Start 一样转入 Exit，
    // 完全复用原有退出路径（会触发 policy_thread 收尾与 BGM stop 事件）。
    this->registered_checks.emplace(
        this->registered_checks.begin(),
        std::make_pair(
            []()->bool{
                if (g1mcp::isStopRequested()) {
                    spdlog::warn("[mcp] stop requested, exiting to Exit");
                    return true;
                }
                return false;
            },
            FSMStringMap.right.at("Exit")
        )
    );

    // [Remote] 双击 Start 立即打断，转入 Exit（插在最前，最高优先级）。
    // click_cnt 由 SDK 的 UnitreeJoystick 维护（0.5s 内连按计为 2），
    // 计数跨线程读取不会漏检（on_pressed 单帧边沿在 20Hz FSM 线程下不可靠）。
    this->registered_checks.emplace(
        this->registered_checks.begin(),
        std::make_pair(
            []()->bool{
                if (FSMState::lowstate->joystick.start.click_cnt >= 2) { // double-click Start to interrupt
                    spdlog::warn("[interrupt] double-click Start detected, exiting to Exit");
                    return true;
                }
                return false;
            },
            FSMStringMap.right.at("Exit")
        )
    );
    this->registered_checks.emplace_back(
        std::make_pair(
            [&]()->bool{
                if (((env->episode_length * env->step_dt) > time_range_[1]) || exit_mimic) { // time out or wrong state
                    spdlog::info("[exit] time_end reached or exit_mimic (elapsed {:.2f}s / limit {:.2f}s)",
                                 env->episode_length * env->step_dt, time_range_[1]);
                    return true;
                }
                return false;
            },
            FSMStringMap.right.at("Exit")
        )
    );
    this->registered_checks.emplace_back(
        std::make_pair(
            [&]()->bool{ return isaaclab::mdp::bad_orientation_rel(env.get(), motion->root_quaternion(), 0.8); }, // bad orientation
            FSMStringMap.right.at("Passive")
        )
    );
    spdlog::info("State_Mimic initialized.");
}

void State_Mimic::enter()
{
    lowstate->update();
    if(keyboard) keyboard->update();

    // 音乐事件：动作开始（尽早发布，给喇叭播放链路留出延迟）
    publish_music_event("start:" + getStateString());

    // BGM 提前量：先让音乐响起 music_lead 秒，再开始动作。
    // 仅配置了 FSM.<状态>.music_lead 的策略生效；延迟期间机器人仍由官方控制（安全）。
    {
        auto lead_cfg = param::config["FSM"][getStateString()]["music_lead"];
        if (lead_cfg && lead_cfg.as<float>() > 0.0f) {
            float lead = lead_cfg.as<float>();
            spdlog::info("[music] BGM lead-in {:.1f}s (music starts first)", lead);
            usleep(static_cast<useconds_t>(lead * 1e6));
        }
    }

    client.Init();
    // set gain
    for (int i = 0; i < env->robot->data.joint_stiffness.size(); ++i)
    {
        lowcmd->msg_.motor_cmd()[i].kp() = env->robot->data.joint_stiffness[i];
        lowcmd->msg_.motor_cmd()[i].kd() = env->robot->data.joint_damping[i];
        lowcmd->msg_.motor_cmd()[i].dq() = 0;
        lowcmd->msg_.motor_cmd()[i].tau() = 0;
    }

    motion = motion_; // set for specific motion
    env->reset();
    motion->reset(env->robot->data, time_range_[0]);

    // 清除双击 Start 的历史计数：
    // KeyBase::click_cnt 在双击后一直保持 >=2（直到 0.5s 后的下一次点击才重置），
    // 若不清理，上次打断的残留计数会让下一次进入策略时立即误触发打断。
    FSMState::lowstate->joystick.start.click_cnt = 0;

    // Start policy thread
    policy_thread_running = true;
    exit_mimic = false;
    policy_thread = std::thread([this]{
        using clock = std::chrono::high_resolution_clock;
        const std::chrono::duration<double> desiredDuration(env->step_dt);
        const auto dt = std::chrono::duration_cast<clock::duration>(desiredDuration);

        // Initialize timing
        const auto start = clock::now();
        auto sleepTill = start + dt;

        
        auto ref_yaw = isaaclab::yawQuaternion(motion->root_quaternion()).toRotationMatrix();
        auto robot_yaw = isaaclab::yawQuaternion(torso_quat_w(env.get())).toRotationMatrix();
        init_quat = robot_yaw * ref_yaw.transpose();
        env->reset();

        int current_fsm_id = 0;
        client.GetFsmId(current_fsm_id);
        if(current_fsm_id != 1 )     // only SwitchToUserCtrl in walkrun mode,
        {
            client.SwitchToUserCtrl();
        }
        else{
            policy_thread_running = false;
            exit_mimic = true;
            spdlog::error("Failed to switch to user control mode! Current FSM ID: {}", current_fsm_id);
        }
        bool checked_switch = false;

        while (policy_thread_running)
        {
            lowstate->update();
            if(keyboard) keyboard->update();

            // reset checked_switch every second
            if (checked_switch && env->episode_length % 50 == 0) {
                checked_switch = false;
            }
            env->robot->update();
            motion->update(env->episode_length * env->step_dt + time_range_[0]);
            env->step();

            // // Sleep
            auto now = clock::now();
            if (now > sleepTill) {
                auto over_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - sleepTill).count();
                spdlog::warn("Policy loop timeout: missed deadline by {} ms", over_ms);
            }

            auto action = env->action_manager->latest_action();
            for(int i(0); i < env->robot->data.joint_ids_map.size(); i++) {
                lowcmd->msg_.motor_cmd()[env->robot->data.joint_ids_map[i]].q() = action[i];
            }
            lowcmd->unlockAndPublish();

            std::this_thread::sleep_until(sleepTill);
            sleepTill += dt;
            if (!checked_switch && (env->episode_length * env->step_dt) > 0.1f) { // verify switch after 0.1s
                client.GetFsmId(current_fsm_id);
                if (current_fsm_id != 1000) {
                    exit_mimic = true;
                    spdlog::error("Failed to switch to user control mode! Current FSM ID: {}", current_fsm_id);
                }
                checked_switch = true;
            }
        }
    });
}


void State_Mimic::run()
{
    
}
