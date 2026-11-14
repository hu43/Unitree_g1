"""Closed-loop control logic test with a simulated odometry source.

Runs without the Unitree SDK/robot. Run with:
    python test_control_sim.py
"""
import math
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import g1_card_server as srv


class FakeLoco:
    def __init__(self):
        self.vx = 0.0
        self.vy = 0.0
        self.omega = 0.0
        self.stop_count = 0

    def SetVelocity(self, vx, vy, omega, duration=1.0):
        self.vx, self.vy, self.omega = vx, vy, omega
        return 0

    def StopMove(self):
        self.vx = self.vy = self.omega = 0.0
        self.stop_count += 1


fake = FakeLoco()
srv.loco = fake
srv.DRY_RUN = False  # exercise the real SetVelocity/StopMove path via FakeLoco
srv.cancel_evt.clear()
srv.state.update(running=True, started_at=time.time(), last_poll=time.time())

sim = {"yaw": 0.0, "x": 0.0, "y": 0.0, "rev": 1.0}
stop = threading.Event()


def integrator():
    last = time.time()
    while not stop.is_set():
        now = time.time()
        dt = min(0.05, now - last)
        last = now
        sim["yaw"] += fake.omega * dt * sim["rev"]
        c, s = math.cos(sim["yaw"]), math.sin(sim["yaw"])
        wx = fake.vx * c - fake.vy * s
        wy = fake.vx * s + fake.vy * c
        sim["x"] += wx * dt
        sim["y"] += wy * dt
        with srv.odom_lock:
            srv.odom.update(x=sim["x"], y=sim["y"], yaw=sim["yaw"], t=now, ok=True)
        time.sleep(0.01)


def reset():
    sim.update(x=0.0, y=0.0, yaw=0.0)
    fake.StopMove()
    with srv.odom_lock:
        srv.odom.update(x=0.0, y=0.0, yaw=0.0, t=time.time(), ok=True)


def deg(x):
    return math.degrees(x)


t = threading.Thread(target=integrator, daemon=True)
t.start()

failures = []


def check(name, cond, detail):
    print(("PASS  " if cond else "FAIL  ") + name + "  " + detail)
    if not cond:
        failures.append(name)


# 1. left 90 deg
reset()
pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
ok = srv.turn_block("turn_left", 90, 0, 1, pose)
time.sleep(0.05)
check("left90", ok and abs(deg(pose["heading"]) + 90) < 3 and abs(deg(sim["yaw"]) - 90) < 3,
      "heading=%.1f yaw=%.1f" % (deg(pose["heading"]), deg(sim["yaw"])))

# 2. right 90 deg
reset()
pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
ok = srv.turn_block("turn_right", 90, 0, 1, pose)
time.sleep(0.05)
check("right90", ok and abs(deg(pose["heading"]) - 90) < 3 and abs(deg(sim["yaw"]) + 90) < 3,
      "heading=%.1f yaw=%.1f" % (deg(pose["heading"]), deg(sim["yaw"])))

# 3. reversed yaw sign -> auto flip
reset()
sim["rev"] = -1.0
pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
ok = srv.turn_block("turn_left", 45, 0, 1, pose)
time.sleep(0.05)
check("autoflip", ok and abs(deg(pose["heading"]) + 45) < 4 and abs(deg(sim["yaw"]) - 45) < 4,
      "heading=%.1f yaw=%.1f (expected yaw +45 after flip)" % (deg(pose["heading"]), deg(sim["yaw"])))
sim["rev"] = 1.0

# 4. forward 0.5 m (odom world x is forward at yaw 0)
reset()
pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
ok = srv.move_block("move_forward", 0.5, 0, 1, 0.5, pose)
time.sleep(0.05)
check("forward0.5", ok and abs(pose["y"] - 0.5) < 0.08 and abs(sim["x"] - 0.5) < 0.08,
      "pose.y=%.3f odom.x=%.3f" % (pose["y"], sim["x"]))

# 5. left strafe 0.4 m (odom world y is left at yaw 0)
reset()
pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
ok = srv.move_block("move_left", 0.4, 0, 1, 0.5, pose)
time.sleep(0.05)
check("left0.4", ok and abs(pose["x"] + 0.4) < 0.08 and abs(sim["y"] - 0.4) < 0.08,
      "pose.x=%.3f odom.y=%.3f" % (pose["x"], sim["y"]))

stop.set()
print("")
print("RESULT:", "ALL PASS" if not failures else "FAILED: " + ", ".join(failures))
sys.exit(0 if not failures else 1)
