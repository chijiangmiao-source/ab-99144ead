#!/usr/bin/env python3
"""一次性验收脚本：HTTP 冒烟 + 中断恢复 + 状态机代码测试 + 构建检查。

流程：
1. 项目构建检查（compileall）与状态机单元测试（unittest discover）；
2. 对 BASE_URL 指向的真实服务做 HTTP 冒烟：
   先提交序号 2、再提交 0 和 1，验证序号 2 采用新规程；
   同内容重传回显原裁决；改标识内容/抢占序号/晚登记规程均 409 拒绝；
   等待记录与既有裁决不被改写；首个拒因可查；
3. 在本进程内以子进程方式真实中断（SIGTERM）并重开服务两次，
   验证恢复结果与不中断执行一致、每序号仅一份不可变裁决。

全部结束后自行退出，验收失败以退出码 1 报告。
"""

from __future__ import annotations

import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES: list[str] = []
PASSED = 0


def check(cond: bool, message: str) -> bool:
    if cond:
        global PASSED
        PASSED += 1
        print(f"  [PASS] {message}")
    else:
        FAILURES.append(message)
        print(f"  [FAIL] {message}")
    return bool(cond)


def http(base: str, method: str, path: str, body=None, timeout: float = 10.0):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        return exc.code, payload


def wait_health(base: str, attempts: int = 30) -> bool:
    for _ in range(attempts):
        try:
            status, body = http(base, "GET", "/health", timeout=2)
            if status == 200 and body.get("status") == "ok":
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.5)
    return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------- 构建与单测

def build_and_unit_checks() -> None:
    print("== 1. 项目构建检查（compileall） ==")
    rc = subprocess.call(
        [sys.executable, "-m", "compileall", "-q", "app", "tests",
         "scripts"], cwd=ROOT)
    check(rc == 0, "compileall：app/tests/scripts 全部编译通过")

    print("== 2. 状态机代码测试（unittest） ==")
    rc = subprocess.call(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-v"], cwd=ROOT)
    check(rc == 0, "unittest discover：状态机测试全部通过")


# ------------------------------------------------------------- HTTP 冒烟

def smoke_against_live_server(base: str) -> None:
    print(f"== 3. HTTP 冒烟（真实接口 {base}） ==")
    check(wait_health(base), "服务 /health 就绪")

    uid = f"{int(time.time())}-{random.randint(1000, 9999)}"
    drill = f"verify-{uid}"

    status, body = http(base, "POST", "/api/drills", {
        "drill_id": drill, "name": "验收演练",
        "base_procedure_code": "P-BASE", "base_threshold": 10.0})
    check(status == 201, "创建演练并登记基线规程（阈值 10）")
    check(body.get("water_level") == 0, "初始连续水位为 0")

    # 先提交序号 2（读数 7：基线下放行，新阈值 5 下应抑制）
    status, body = http(base, "POST", f"/api/drills/{drill}/observations",
                        {"delivery_id": "obs-2", "seq": 2, "reading": 7.0})
    check(status == 202, "序号 2 先到：202 入等待队列")
    check(body.get("state") == "WAITING"
          and body.get("water_level") == 0,
          "缺号时序号 2 持久化等待且水位不动")

    # 登记序号 2 起生效的新规程
    status, body = http(base, "POST", f"/api/drills/{drill}/procedures",
                        {"effective_from": 2, "code": "P-TIGHT",
                         "threshold": 5.0})
    check(status == 201, "登记序号 2 起生效的新规程 P-TIGHT(5)")

    # 提交序号 0
    status, body = http(base, "POST", f"/api/drills/{drill}/observations",
                        {"delivery_id": "obs-0", "seq": 0, "reading": 9.0})
    check(status == 200 and body.get("state") == "VERDICT",
          "序号 0 到达：立即裁决")
    v0 = (body or {}).get("verdict") or {}
    check(v0.get("seq") == 0 and v0.get("decision") == "PASS"
          and v0.get("procedure_code") == "P-BASE",
          "序号 0 按基线规程裁决 PASS（9 <= 10）")
    check(body.get("water_level") == 1, "水位推进到 1，缺口 1 仍阻止序号 2")

    # 提交序号 1：应在同一调用内排空 1、2
    status, body = http(base, "POST", f"/api/drills/{drill}/observations",
                        {"delivery_id": "obs-1", "seq": 1, "reading": 11.0})
    check(status == 200, "序号 1 到达：200 并依序排空等待队列")
    v1 = (body or {}).get("verdict") or {}
    drained = (body or {}).get("drained") or []
    check(v1.get("seq") == 1 and v1.get("decision") == "INHIBIT"
          and v1.get("procedure_code") == "P-BASE",
          "序号 1 按基线规程裁决 INHIBIT（11 > 10）")
    v2 = next((v for v in drained if v.get("seq") == 2), None)
    check(v2 is not None and v2.get("decision") == "INHIBIT"
          and v2.get("procedure_code") == "P-TIGHT",
          "排空时序号 2 固定采用新规程 P-TIGHT 裁决 INHIBIT（7 > 5）")
    check(body.get("water_level") == 3, "水位连续推进到 3")

    ts2 = v2["adjudicated_at"]

    status, snap = http(base, "GET", f"/api/drills/{drill}")
    check(status == 200, "查询演练全景")
    check([v["seq"] for v in snap["verdicts"]] == [0, 1, 2],
          "各序号实际采用的规程与裁决按序可见")
    check(snap["waiting"] == [], "等待队列已排空")
    check(len(snap["verdicts"]) == 3, "每个序号仅有一份裁决")

    # 同标识同内容重传：回显原裁决
    status, body = http(base, "POST", f"/api/drills/{drill}/observations",
                        {"delivery_id": "obs-2", "seq": 2, "reading": 7.0})
    check(status == 200 and body.get("duplicate") is True,
          "同标识同内容重传：回显（duplicate=true，200）")
    echo = (body or {}).get("verdict") or {}
    check(echo.get("procedure_code") == "P-TIGHT"
          and echo.get("decision") == "INHIBIT"
          and echo.get("adjudicated_at") == ts2,
          "重传回显原裁决（含原裁决时间戳，不重算）")

    # 水位越过 2 后再登记更早规程：拒绝
    status, body = http(base, "POST", f"/api/drills/{drill}/procedures",
                        {"effective_from": 1, "code": "P-LATE",
                         "threshold": 1.0})
    check(status == 409 and (body.get("error") or {}).get("code")
          == "PROCEDURE_LATE",
          "水位越过生效序号后的晚登记规程：409 PROCEDURE_LATE")

    # 同标识改动读数：拒绝
    status, body = http(base, "POST", f"/api/drills/{drill}/observations",
                        {"delivery_id": "obs-2", "seq": 2, "reading": 7.5})
    check(status == 409 and (body.get("error") or {}).get("code")
          == "DELIVERY_CONTENT_CHANGED",
          "同标识改动读数：409 DELIVERY_CONTENT_CHANGED")

    # 同标识改动序号：拒绝
    status, body = http(base, "POST", f"/api/drills/{drill}/observations",
                        {"delivery_id": "obs-2", "seq": 3, "reading": 7.0})
    check(status == 409 and (body.get("error") or {}).get("code")
          == "DELIVERY_CONTENT_CHANGED",
          "同标识改动序号：409 DELIVERY_CONTENT_CHANGED")

    # 同序号不同内容（不同标识）：拒绝
    status, body = http(base, "POST", f"/api/drills/{drill}/observations",
                        {"delivery_id": "intruder", "seq": 0,
                         "reading": 1.0})
    check(status == 409 and (body.get("error") or {}).get("code")
          == "SEQUENCE_TAKEN",
          "同序号出现不同投递：409 SEQUENCE_TAKEN")

    # 非法请求体：400
    status, _ = http(base, "POST", f"/api/drills/{drill}/observations",
                     {"delivery_id": "x", "seq": -1, "reading": 1.0})
    check(status == 400, "非法序号（负数）：400 拒绝")

    # 拒绝后等待记录与既有裁决不得改写
    status, after = http(base, "GET", f"/api/drills/{drill}")
    check(after["waiting"] == [], "冲突后等待队列未被改写（仍为空）")
    check([(v["seq"], v["delivery_id"], v["reading"], v["decision"],
            v["procedure_code"], v["adjudicated_at"])
           for v in after["verdicts"]]
          == [(v["seq"], v["delivery_id"], v["reading"], v["decision"],
               v["procedure_code"], v["adjudicated_at"])
              for v in snap["verdicts"]],
          "冲突后既有裁决逐字段不变（不可变，无重算）")
    first = after.get("first_rejection") or {}
    check(first.get("kind") == "PROCEDURE_LATE",
          f"首个拒因可查：{first.get('kind')}（按发生顺序）")
    check(len(after.get("rejections") or []) >= 4,
          "所有拒因均留痕，首个拒因固定不变")


# ------------------------------------------------------------- 中断恢复

class LocalServer:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = os.environ.copy()
        env.update({
            "DB_PATH": self.db_path, "HOST": "127.0.0.1",
            "PORT": str(self.port), "PYTHONPATH": ROOT,
        })
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.main"], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        if not wait_health(self.base):
            out = ""
            try:
                out = self.proc.stdout.read(4000) if self.proc.stdout else ""
            except Exception:
                pass
            raise RuntimeError(f"本地服务未就绪：\n{out}")

    def stop(self, hard: bool = False) -> None:
        assert self.proc is not None
        if hard:
            self.proc.kill()
        else:
            self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None


def recovery_check() -> None:
    print("== 4. 中断恢复（真实进程 SIGTERM 后重开） ==")
    import tempfile
    tmp = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmp.name, "recovery.db")
    srv = LocalServer(db_path)
    srv.start()

    uid = f"{int(time.time())}-{random.randint(1000, 9999)}"
    drill = f"crash-{uid}"
    try:
        status, _ = http(srv.base, "POST", "/api/drills", {
            "drill_id": drill, "name": "中断恢复演练",
            "base_procedure_code": "P-BASE", "base_threshold": 10.0})
        check(status == 201, "[恢复] 创建演练")

        # 入队后中断：序号 2 先到
        status, body = http(srv.base, "POST",
                            f"/api/drills/{drill}/observations",
                            {"delivery_id": "c-2", "seq": 2,
                             "reading": 7.0})
        check(status == 202 and body.get("water_level") == 0,
              "[恢复] 序号 2 入等待，水位 0")
        http(srv.base, "POST", f"/api/drills/{drill}/procedures",
             {"effective_from": 2, "code": "P-TIGHT", "threshold": 5.0})

        # 推进水位后中断：裁决序号 0
        status, body = http(srv.base, "POST",
                            f"/api/drills/{drill}/observations",
                            {"delivery_id": "c-0", "seq": 0,
                             "reading": 9.0})
        ts0 = body["verdict"]["adjudicated_at"]
        check(body["water_level"] == 1, "[恢复] 序号 0 裁决，水位 1")

        # 第一次中断重开（入队后 + 推进水位后两种状态都已落盘）
        srv.stop()
        srv.start()
        status, body = http(srv.base, "GET", f"/api/drills/{drill}")
        check(body["water_level"] == 1,
              "[恢复] 重开后水位保持 1")
        check([w["seq"] for w in body["waiting"]] == [2],
              "[恢复] 重开后序号 2 仍在等待队列")
        check(len(body["verdicts"]) == 1
              and body["verdicts"][0]["adjudicated_at"] == ts0,
              "[恢复] 既有裁决未重算（裁决时间戳不变）")

        # 补齐缺口：排空 1、2
        status, body = http(srv.base, "POST",
                            f"/api/drills/{drill}/observations",
                            {"delivery_id": "c-1", "seq": 1,
                             "reading": 11.0})
        drained = body.get("drained") or []
        v2 = next((v for v in drained if v["seq"] == 2), None)
        check(v2 is not None and v2["procedure_code"] == "P-TIGHT"
              and v2["decision"] == "INHIBIT",
              "[恢复] 补齐后序号 2 仍按新规程 P-TIGHT 裁决")

        # 排空后第二次中断重开
        srv.stop()
        srv.start()
        status, body = http(srv.base, "GET", f"/api/drills/{drill}")
        expected = [
            (0, "c-0", 9.0, "PASS", "P-BASE"),
            (1, "c-1", 11.0, "INHIBIT", "P-BASE"),
            (2, "c-2", 7.0, "INHIBIT", "P-TIGHT"),
        ]
        actual = [(v["seq"], v["delivery_id"], v["reading"],
                   v["decision"], v["procedure_code"])
                  for v in body["verdicts"]]
        check(body["water_level"] == 3 and body["waiting"] == [],
              "[恢复] 重开后水位 3、等待队列空")
        check(actual == expected,
              "[恢复] 重开结果与不中断执行完全一致（0/1 基线、2 新规程）")
        check(all(v["reason"] for v in body["verdicts"]
                  if v["decision"] == "INHIBIT"),
              "[恢复] 抑制裁决均带拒因说明")
        check(len({v["seq"] for v in body["verdicts"]}) == 3
              and len(body["verdicts"]) == 3,
              "[恢复] 每序号仅一份不可变裁决")

        # 重传在重开后依然回显原裁决
        status, body = http(srv.base, "POST",
                            f"/api/drills/{drill}/observations",
                            {"delivery_id": "c-2", "seq": 2,
                             "reading": 7.0})
        check(status == 200 and body.get("duplicate") is True
              and body["verdict"]["procedure_code"] == "P-TIGHT",
              "[恢复] 重开后同内容重传仍回显原裁决")
    finally:
        srv.stop()
        tmp.cleanup()


def main() -> int:
    print("########################################################")
    print("# 束流保护阈值规程切换系统：一次性验收 verify")
    print("########################################################")
    try:
        build_and_unit_checks()
        smoke_against_live_server(
            os.environ.get("BASE_URL", "http://127.0.0.1:8080"))
        recovery_check()
    except Exception as exc:  # 冒烟脚本自身异常也算验收失败
        import traceback
        traceback.print_exc()
        FAILURES.append(f"verify 执行异常：{exc!r}")

    print("========================================================")
    print(f"通过 {PASSED} 项，失败 {len(FAILURES)} 项")
    if FAILURES:
        for msg in FAILURES:
            print(f"  - {msg}")
        print("验收结果：FAIL")
        return 1
    print("验收结果：PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
