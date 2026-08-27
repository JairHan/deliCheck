"""
得力 E+ 自动签到 - 精简单文件版 v3

功能：
1. 手机号 + 密码 + trust_code 登录得力 E+
2. 自动查询组织并登录综合签到
3. 查询排班、当前 checkin/checkout、今日打卡记录
4. 使用固定办公点做 GPS 范围自检
5. 支持青龙环境变量和 DELI_MODE 自动运行
6. 真正提交时，时间取脚本当前毫秒，sig 按得力 App 算法本地计算（双重 MD5）

依赖：
    pip install -r requirements.txt

运行：
    python3 deli_eplus_auto_simple_v3.py login                                 # 仅登录，打印登录状态
    python3 deli_eplus_auto_simple_v3.py status                                # 打印登录状态 + 今日排班 + 当前动作 + 今日记录 + GPS 状态
    python3 deli_eplus_auto_simple_v3.py form                                  # 打印提交表单（不提交）
    python3 deli_eplus_auto_simple_v3.py check                                 # 检查今日排班 + 当前动作 + 今日记录 + GPS 状态
    python3 deli_eplus_auto_simple_v3.py check --execute                       # 执行检查并提交
    python3 deli_eplus_auto_simple_v3.py check --expected checkout --execute   # 仅当当前动作为 checkout 时提交

青龙直接运行：
    DELI_MODE=dry       -> 只检查
    DELI_MODE=auto      -> 按服务端当前动作提交
    DELI_MODE=checkin   -> 仅当当前动作为 checkin 时提交
    DELI_MODE=checkout  -> 仅当当前动作为 checkout 时提交
"""

import base64
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests


def load_env_file(path):
    """加载简单的 KEY=VALUE 配置，且不覆盖系统/青龙已有环境变量。"""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()

        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.startswith("DELI_"):
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


# ============================================================
# 配置区
# ============================================================

# 服务端 /ass/api/v2.0/phone/checkin/support 会返回包含地点名称、地址、
# 经纬度和允许范围的 gps_list 规则。
CONFIG = {
    # ===== 必填（无法自动获取）=====
    "mobile": "",
    "password": "",
    "trust_code": "",
    "terminal_id": "",
    # ===== 可选（有默认值/可自动获取，可留空）=====
    "org_id": "",  # 多组织时填；单组织留空自动选
    "phone_model": "iPhone18,1",  # 伪装机型，任意
    "user_agent": "smartoffice/3.5.5 (iPhone; iOS 27.0; Scale/3.00)",
    "timeout": 15,
    # 以下 5 项留空 = 自动从服务端 GPS 规则获取（规则已含 lat/lgt/name/location/range）
    "gps_name": "",  # 指定规则名；留空取第一条规则中心点
    "lat": "",  # 自定义坐标；留空用规则中心点（distance 0 必过）
    "lgt": "",
    "gps_location": "",  # 地址；留空用规则里的 location
    "gps_range": "",  # 范围上限(米)；留空用规则 range
}


# 自动加载脚本同目录下的 .env；系统/青龙环境变量优先级更高。
load_env_file(Path(__file__).with_name(".env"))


# 青龙环境变量或 .env 覆盖
ENV_MAP = {
    "DELI_MOBILE": "mobile",
    "DELI_PASSWORD": "password",
    "DELI_TRUST_CODE": "trust_code",
    "DELI_ORG_ID": "org_id",
    "DELI_TERMINAL_ID": "terminal_id",
}

for env_name, key in ENV_MAP.items():
    value = os.getenv(env_name, "").strip()
    if value:
        CONFIG[key] = value


MAIN_BASE = "https://v2-app.delicloud.com"
CHECKIN_BASE = "https://checkin2-app.delicloud.com"


class DeliError(RuntimeError):
    pass


def encode_password(password):
    """得力 App 登录密码格式：Base64 后反转。"""
    return base64.b64encode(password.encode()).decode()[::-1]


def distance_m(lat1, lng1, lat2, lng2):
    """Haversine 两点距离，单位米。"""
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def mask(value):
    value = str(value or "")
    return value[:5] + "…" + value[-4:] if len(value) > 10 else "***"


class Deli:
    def __init__(self):
        self.cfg = CONFIG
        self.http = requests.Session()
        self.main_token = ""
        self.user_id = ""
        self.source_org_id = ""
        self.source_member_id = ""
        self.org_name = ""
        self.checkin_token = ""
        self.checkin_org_id = ""
        self.checkin_member_id = ""

    # ---------- 通用请求 ----------

    def api(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.cfg["timeout"])
        r = self.http.request(method, url, **kwargs)

        try:
            data = r.json()
        except Exception:
            raise DeliError(f"HTTP {r.status_code} 非 JSON：{r.text[:300]}")

        if r.status_code != 200:
            raise DeliError(f"HTTP {r.status_code}: {data}")

        if data.get("code") not in (None, 0):
            raise DeliError(f"API 失败：{data}")

        return data

    # ---------- 1. 主 App 登录 ----------

    def login_main(self):
        for key in ("mobile", "password", "trust_code"):
            if not self.cfg[key]:
                raise DeliError(f"缺少配置：{key}")

        data = self.api(
            "POST",
            MAIN_BASE + "/api/v3.0/auth/app/trusted/login",
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.cfg["user_agent"],
                "client_id": "eplus_app",
                "X-Service-Id": "userauth",
            },
            json={
                "trust_code": self.cfg["trust_code"],
                "mobile": self.cfg["mobile"],
                "password": encode_password(self.cfg["password"]),
            },
        )["data"]

        self.main_token = data["token"]
        self.user_id = str(data["user_id"])

    # ---------- 2. 查询组织 ----------

    def select_org(self):
        data = self.api(
            "GET",
            MAIN_BASE + "/api/v3.0/org/list",
            headers={
                "Authorization": self.main_token,
                "user_id": self.user_id,
                "User-Agent": self.cfg["user_agent"],
                "client_id": "eplus_app",
                "X-Service-Id": "organization",
            },
            params={"user_id": self.user_id},
        )["data"]

        if not data:
            raise DeliError("账号没有可用组织")

        wanted = self.cfg["org_id"]
        if wanted:
            org = next((x for x in data if str(x["org_id"]) == wanted), None)
            if not org:
                raise DeliError(f"找不到组织 {wanted}")
        elif len(data) == 1:
            org = data[0]
        else:
            names = [f"{x.get('org_name')}({x.get('org_id')})" for x in data]
            raise DeliError("账号有多个组织，请填写 org_id：" + ", ".join(names))

        self.source_org_id = str(org["org_id"])
        self.source_member_id = str(org["seq_no"])
        self.org_name = str(org.get("org_name", ""))

    # ---------- 3. 登录综合签到 ----------

    def login_checkin(self):
        data = self.api(
            "POST",
            CHECKIN_BASE + "/api/v2.0/auth/login",
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.cfg["user_agent"],
                "client_id": "eplus_app",
                "x-service-id": "auth",
            },
            json={
                "memberId": self.source_member_id,
                "sourceToken": self.main_token,
                "sourceId": "deli",
                "orgId": self.source_org_id,
            },
        )["data"]

        info = data["member_infos"][0]

        self.checkin_token = data["token"]
        self.checkin_org_id = str(info["org_id"])
        self.checkin_member_id = str(info.get("member_id") or info.get("user_id"))

    def login(self):
        self.login_main()
        self.select_org()
        self.login_checkin()

    # ---------- 综合签到请求 ----------

    def checkin_api(self, path, body, service="ass"):
        return self.api(
            "POST",
            CHECKIN_BASE + path,
            headers={
                "Authorization": self.checkin_token,
                "member_id": self.checkin_member_id,
                "org_id": self.checkin_org_id,
                "Content-Type": "application/json",
                "User-Agent": self.cfg["user_agent"],
                "client_id": "eplus_app",
                "x-service-id": service,
            },
            json=body,
        )

    # ---------- 4. 业务状态 ----------

    def shift(self):
        return self.checkin_api(
            "/ass/api/v2.0/schedule/index/shifts/query",
            {
                "day": datetime.now().strftime("%Y%m%d"),
                "member_id": self.checkin_member_id,
                "org_id": self.checkin_org_id,
            },
        )["data"]

    def action(self):
        return self.checkin_api(
            "/ass-rule/api/v2.0/rule/predicate",
            {
                "member_id": self.checkin_member_id,
                "org_id": self.checkin_org_id,
            },
            "ass-rule",
        )["data"]

    def records(self):
        day = datetime.now().strftime("%Y%m%d")
        rows = self.checkin_api(
            "/ass/api/v2.1/schedule/time/find",
            {
                "member_id": self.checkin_member_id,
                "org_id": self.checkin_org_id,
                "start_day": day,
                "end_day": day,
            },
            "ass-report",
        )["data"]

        return rows[0].get("checkin_record", []) if rows else []

    def gps_rules(self):
        return self.checkin_api(
            "/ass/api/v2.0/phone/checkin/support",
            {},
        )["data"].get("gps_list", [])

    # ---------- 5. 固定 GPS 自检 ----------

    def validate_fixed_gps(self):
        """解析/校验打卡 GPS 点。

        服务端 GPS 规则已包含 lat/lgt/name/location/range，故 CONFIG 里这 5 项留空即可——
        本方法自动从规则取值并回填 self.cfg，后续 gps_proof/print_status 直接复用。

        - CONFIG.lat/lgt 留空：取规则中心点（distance 0，必过）。
        - CONFIG.lat/lgt 填了：校验其在某规则范围内，沿用配置坐标 + 该规则 name/location。
        - CONFIG.gps_name 填了：按名称匹配规则（与坐标二选一，优先名称）。
        """
        rules = self.gps_rules()
        if not rules:
            raise DeliError("服务端未配置任何 GPS 打卡点")

        want_name = (self.cfg.get("gps_name") or "").strip()
        want_lat = (self.cfg.get("lat") or "").strip()
        want_lgt = (self.cfg.get("lgt") or "").strip()

        rule = None
        if want_name:
            rule = next((r for r in rules if str(r.get("name")) == want_name), None)
            if not rule:
                names = ", ".join(str(r.get("name", "")) for r in rules)
                raise DeliError(f"找不到名为 {want_name} 的 GPS 规则；可用：{names}")

        # 未指定坐标 -> 用规则中心点，回填 CONFIG
        if not want_lat or not want_lgt:
            rule = rule or rules[0]
            self.cfg["lat"] = str(rule["lat"])
            self.cfg["lgt"] = str(rule["lgt"])
            self.cfg["gps_name"] = str(rule.get("name", ""))
            self.cfg["gps_location"] = str(rule.get("location", ""))
            self.cfg["gps_range"] = float(rule.get("range", 200))
            return {
                "name": self.cfg["gps_name"],
                "distance_m": 0.0,
                "allowed_m": float(rule.get("range", 200)),
            }

        # 指定了坐标 -> 校验范围
        lat = float(want_lat)
        lng = float(want_lgt)
        local_range = float(self.cfg.get("gps_range") or 0)
        for r in rules:
            d = distance_m(lat, lng, float(r["lat"]), float(r["lgt"]))
            allowed = min(local_range or float(r["range"]), float(r["range"]))
            if d <= allowed:
                self.cfg["gps_name"] = str(r.get("name", ""))
                self.cfg["gps_location"] = str(r.get("location", ""))
                if not self.cfg.get("gps_range"):
                    self.cfg["gps_range"] = float(r["range"])
                return {
                    "name": self.cfg["gps_name"],
                    "distance_m": round(d, 2),
                    "allowed_m": allowed,
                }

        raise DeliError("固定 GPS 坐标不在服务端允许范围内")

    # ---------- 6. 获取新鲜 GPS 证明 ----------

    def gps_proof(self):
        """
        生成本次提交所需的 GPS 数据并按得力 App 算法计算 gps_info.sig。

        sig 算法（反编译自得力 E+ Android 客户端 com.delicloud.app.smartoffice，
        已用 2026-08-25 抓包的三条真实打卡样本 100% 验证）：

            inner = md5(time + "-lat=" + lat + "&lgt=" + lgt
                        + "&location=" + location + "&name=" + name)
            sig   = md5(inner + "-checkin2")

        其中 time 为毫秒时间戳；lat/lgt/location/name 即 gps_info 中同名字段。
        """
        lat = self.cfg["lat"]
        lgt = self.cfg["lgt"]
        name = self.cfg["gps_name"]
        location = self.cfg["gps_location"]
        time_ms = str(int(time.time() * 1000))

        inner = hashlib.md5(
            (
                time_ms
                + "-lat="
                + lat
                + "&lgt="
                + lgt
                + "&location="
                + location
                + "&name="
                + name
            ).encode("utf-8")
        ).hexdigest()
        sig = hashlib.md5((inner + "-checkin2").encode("utf-8")).hexdigest()

        return {
            "time": time_ms,
            "sig": sig,
            "lat": lat,
            "lgt": lgt,
            "name": name,
            "location": location,
        }

    # ---------- 7. 真正提交 ----------

    def execute_request(self, proof):
        """构建提交给 /phone/checkin/execute 的完整请求（方法/URL/请求头/请求体）。

        form 模式打印它；execute() 用它真正提交，保证“打印的表单”与“提交的表单”完全一致。
        """
        path = "/ass/api/v2.1/phone/checkin/execute"
        body = {
            "terminal_id": self.cfg["terminal_id"],
            "gps_info": {
                "time": str(proof["time"]),
                "sig": str(proof["sig"]),
                "lat": str(proof["lat"]),
                "lgt": str(proof["lgt"]),
                "name": str(proof.get("name") or self.cfg["gps_name"]),
                "location": str(proof.get("location") or self.cfg["gps_location"]),
            },
            "checkin_type": "gps",
            "phone_model": self.cfg["phone_model"],
        }
        return {
            "method": "POST",
            "url": CHECKIN_BASE + path,
            "service": "ass",
            "headers": {
                "Authorization": self.checkin_token,
                "member_id": self.checkin_member_id,
                "org_id": self.checkin_org_id,
                "Content-Type": "application/json",
                "User-Agent": self.cfg["user_agent"],
                "client_id": "eplus_app",
                "x-service-id": "ass",
            },
            "body": body,
        }

    def execute(self, proof):
        if not self.cfg["terminal_id"]:
            raise DeliError("缺少 terminal_id")
        req = self.execute_request(proof)
        return self.api(
            req["method"], req["url"], headers=req["headers"], json=req["body"]
        )


def print_login(deli):
    print("\n========== 登录状态 ==========")
    print("主 App        : 登录成功")
    print("  token       :", mask(deli.main_token))
    print("  user_id     :", deli.user_id)

    print("\n组织信息")
    print("  org_name    :", deli.org_name)
    print("  org_id      :", deli.source_org_id)
    print("  seq_no      :", deli.source_member_id)

    print("\n综合签到      : 登录成功")
    print("  token       :", mask(deli.checkin_token))
    print("  org_id      :", deli.checkin_org_id)
    print("  member_id   :", deli.checkin_member_id)


def print_status(deli, shift, action, records, gps):
    print_login(deli)

    print("\n========== 今日排班 ==========")
    print("工作日        :", "是" if shift.get("workday") else "否")
    print("已排班        :", "是" if shift.get("has_scheduled") else "否")

    shifts = shift.get("shifts_info") or {}
    if shifts:
        print("班次名称      :", shifts.get("name", ""))
        work_times = shifts.get("work_times") or []
        for i, item in enumerate(work_times, 1):
            start_min = item.get("start")
            end_min = item.get("end")

            def fmt(minutes):
                if minutes is None:
                    return "-"
                return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"

            print(f"工作时段 {i}   :", f"{fmt(start_min)} - {fmt(end_min)}")

    print("\n========== 当前状态 ==========")
    print("当前动作      :", action)
    print("今日记录数    :", len(records))

    if records:
        for i, ts in enumerate(records, 1):
            try:
                dt = datetime.fromtimestamp(int(ts) / 1000)
                readable = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                readable = "无法解析"
            print(f"记录 {i:<8}: {ts}  ({readable})")
    else:
        print("今日记录      : 无")

    print("\n========== GPS 状态 ==========")
    print("固定纬度      :", CONFIG["lat"])
    print("固定经度      :", CONFIG["lgt"])
    print("地点名称      :", CONFIG["gps_name"])
    print("地址          :", CONFIG["gps_location"])
    print("匹配规则      :", gps.get("name", ""))
    print("距离打卡点    :", f"{gps.get('distance_m', 0)} 米")
    print("允许范围      :", f"{gps.get('allowed_m', 0)} 米")
    print("GPS 校验      : 通过")


def print_submit_form(req):
    print("\n========== 提交表单（仅展示，不提交）==========")
    print("方法          :", req["method"])
    print("URL           :", req["url"])
    print("x-service-id  :", req["service"])
    print("\n请求头 Headers：")
    for k, v in req["headers"].items():
        if k.lower() == "authorization":
            print(f"  {k:14}: {mask(v)}   # 综合签到 token，每次登录后变化")
        else:
            print(f"  {k:14}: {v}")
    print("\n请求体 Body（服务端收到的 JSON）：")
    print(json.dumps(req["body"], ensure_ascii=False, indent=2))
    g = req["body"]["gps_info"]
    inner = hashlib.md5(
        (
            g["time"]
            + "-lat="
            + g["lat"]
            + "&lgt="
            + g["lgt"]
            + "&location="
            + g["location"]
            + "&name="
            + g["name"]
        ).encode("utf-8")
    ).hexdigest()
    sig2 = hashlib.md5((inner + "-checkin2").encode("utf-8")).hexdigest()
    print("\ngps_info.sig 本地复算校验：")
    print("  inner md5    :", inner)
    print("  复算 sig     :", sig2)
    print(
        "  body 内 sig  :", g["sig"], " ", "一致 ✓" if sig2 == g["sig"] else "不一致 ✗"
    )
    print("\n字段说明：")
    print("  terminal_id        : 设备唯一标识（CONFIG，固定）")
    print("  gps_info.time      : 脚本当前毫秒时间戳")
    print(
        "  gps_info.sig       : md5(md5(time+'-lat='+lat+'&lgt='+lgt+'&location='+location+'&name='+name)+'-checkin2')"
    )
    print("  gps_info.lat/lgt   : CONFIG 固定办公点经纬度")
    print("  gps_info.name      : CONFIG 地点名称（需与服务端 GPS 规则名一致）")
    print("  gps_info.location  : CONFIG 地址描述")
    print("  checkin_type       : 固定 'gps'")
    print("  phone_model        : CONFIG 伪装机型")
    print("  Authorization      : 综合签到登录后的 token（敏感，已掩码）")
    print("  member_id / org_id : 综合签到账号标识")


def run(mode, expected=None, execute=False):
    deli = Deli()

    print("========== 得力 E+ 自动签到 ==========")
    print("运行时间      :", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("运行模式      :", mode)
    print("实际提交      :", "是" if execute else "否")

    print("\n正在登录主 App...")
    deli.login_main()
    print("主 App 登录成功")

    print("正在查询组织...")
    deli.select_org()
    print("组织选择成功 :", deli.org_name)

    print("正在登录综合签到...")
    deli.login_checkin()
    print("综合签到登录成功")

    if mode == "login":
        print_login(deli)
        return

    print("\n正在查询今日排班...")
    shift = deli.shift()

    print("正在查询当前动作...")
    action = deli.action()

    print("正在查询今日记录...")
    records = deli.records()

    print("正在校验固定 GPS...")
    gps = deli.validate_fixed_gps()

    if mode == "status":
        print_status(deli, shift, action, records, gps)
        return

    if mode == "form":
        print_status(deli, shift, action, records, gps)
        print("\n正在构建提交表单（仅展示，不提交）...")
        proof = deli.gps_proof()
        req = deli.execute_request(proof)
        print_submit_form(req)
        return

    if not shift.get("workday"):
        print_status(deli, shift, action, records, gps)
        raise DeliError("今天不是工作日")

    if not shift.get("has_scheduled"):
        print_status(deli, shift, action, records, gps)
        raise DeliError("今天没有排班")

    if expected and action != expected:
        print_status(deli, shift, action, records, gps)
        raise DeliError(f"当前动作是 {action}，不是 {expected}")

    print_status(deli, shift, action, records, gps)

    print("\n========== 执行结果 ==========")

    if not execute:
        print("Dry-run       : 是")
        print("提交签到      : 否")
        print("结果          : 检查完成，未提交")
        return

    print("正在准备 GPS 签名...")
    proof = deli.gps_proof()

    print("GPS 时间戳    :", proof["time"])
    print("GPS sig       :", mask(proof["sig"]))

    print("正在提交签到...")
    result = deli.execute(proof)

    print("Dry-run       : 否")
    print("提交签到      : 是")
    print("服务端响应：")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def parse_args():
    args = sys.argv[1:]

    # 青龙直接执行脚本时使用 DELI_MODE
    if not args:
        mode = os.getenv("DELI_MODE", "dry").lower()
        if mode == "checkin":
            return "check", "checkin", True
        if mode == "checkout":
            return "check", "checkout", True
        if mode == "auto":
            return "check", None, True
        return "check", None, False

    mode = args[0]

    if mode in ("login", "status", "form"):
        return mode, None, False

    if mode != "check":
        raise DeliError("用法：login | status | form | check")

    expected = None
    execute = "--execute" in args

    if "--expected" in args:
        i = args.index("--expected")
        if i + 1 >= len(args) or args[i + 1] not in ("checkin", "checkout"):
            raise DeliError("--expected 只能是 checkin 或 checkout")
        expected = args[i + 1]

    return "check", expected, execute


def main():
    try:
        mode, expected, execute = parse_args()
        run(mode, expected, execute)
    except (DeliError, requests.RequestException, ValueError, KeyError) as e:
        print("失败：", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
