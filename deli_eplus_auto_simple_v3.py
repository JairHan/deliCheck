"""
得力 E+ 自动签到 - 精简单文件版 v3

功能：
1. 手机号 + 密码登录得力 E+；首次运行可通过短信验证码自动获取 trust_code
2. 自动查询组织并登录综合签到
3. 查询排班、当前 checkin/checkout、今日打卡记录
4. 使用固定办公点做 GPS 范围自检
5. 支持青龙环境变量和 DELI_MODE 自动运行
6. 真正提交时，时间取脚本当前毫秒，sig 按得力 App 算法本地计算（双重 MD5）
7. 支持启动时在设定秒数内随机延迟，避免每次都在固定时刻执行
8. 支持通过 QQ 邮箱 SMTP 推送本次执行日志

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
import importlib
import io
import json
import math
import os
import secrets
import smtplib
import sys
import time
import uuid
from contextlib import redirect_stdout
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
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
        if not sep or not key.startswith(("DELI_", "SMTP_")):
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def save_env_value(path, key, value):
    """将单个配置原子写入 .env，并将文件权限设置为仅当前用户可读写。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{key}={json.dumps(str(value), ensure_ascii=False)}"
    updated = []
    found = False

    for line in lines:
        candidate = line.strip()
        if candidate.startswith("export "):
            candidate = candidate[7:].strip()
        current_key = candidate.partition("=")[0].strip()
        if current_key == key:
            if not found:
                updated.append(replacement)
                found = True
            continue
        updated.append(line)

    if not found:
        updated.append(replacement)

    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)
    os.chmod(path, 0o600)


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
    "random_delay": 3600,  # 启动后随机等待 0～该秒数；0 表示不延迟
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
    "DELI_RANDOM_DELAY": "random_delay",
}

for env_name, key in ENV_MAP.items():
    value = os.getenv(env_name, "").strip()
    if value:
        CONFIG[key] = value


MAIN_BASE = "https://v2-app.delicloud.com"
CHECKIN_BASE = "https://checkin2-app.delicloud.com"
ENV_FILE = Path(__file__).with_name(".env")
SMS_CODE_TYPE_LOGIN = "3"
SMS_SIGN_SECRET = "7lgTlgfGK1pkzsOV1tIC"


class DeliError(RuntimeError):
    pass


class TeeOutput:
    """将控制台输出同时写入内存，供邮件正文复用。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def parse_bool_env(name, default=False):
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise DeliError(f"{name} 只能设置为 true 或 false")


def smtp_endpoint(use_ssl):
    """解析 SMTP_SERVER，兼容 smtp.qq.com 和 smtp.qq.com:465。"""
    server = os.getenv("SMTP_SERVER", "smtp.qq.com").strip() or "smtp.qq.com"
    port_text = os.getenv("SMTP_PORT", "").strip()

    host, separator, inline_port = server.rpartition(":")
    if separator and inline_port.isdigit():
        server = host
        if not port_text:
            port_text = inline_port

    try:
        port = int(port_text or (465 if use_ssl else 587))
    except ValueError as exc:
        raise DeliError("SMTP 端口必须是整数") from exc
    if not server or not 1 <= port <= 65535:
        raise DeliError("SMTP_SERVER 或 SMTP_PORT 配置无效")
    return server, port


def send_email_notification(title, log_text):
    """使用环境变量中的 SMTP 配置发送执行日志；未配置时直接跳过。"""
    sender = os.getenv("SMTP_EMAIL", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    if not sender and not password:
        return False
    if not sender or not password:
        raise DeliError("邮件推送需要同时配置 SMTP_EMAIL 和 SMTP_PASSWORD（授权码）")

    use_ssl = parse_bool_env("SMTP_SSL", default=True)
    server, port = smtp_endpoint(use_ssl)
    sender_name = os.getenv("SMTP_NAME", "青龙脚本运行通知").strip()
    recipient_text = os.getenv("SMTP_TO", sender)
    recipients = [
        item.strip()
        for item in recipient_text.replace(";", ",").split(",")
        if item.strip()
    ]
    if not recipients:
        recipients = [sender]

    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender))
    message["To"] = ", ".join(recipients)
    message["Subject"] = title
    message.set_content(
        f"打卡结果：{title}\n"
        f"通知时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "以下为本次完整执行日志：\n\n" + log_text
    )

    timeout = float(CONFIG.get("timeout", 15))
    if use_ssl:
        connection = smtplib.SMTP_SSL(server, port, timeout=timeout)
    else:
        connection = smtplib.SMTP(server, port, timeout=timeout)

    with connection as smtp:
        if not use_ssl:
            smtp.starttls()
        smtp.login(sender, password)
        smtp.send_message(message, from_addr=sender, to_addrs=recipients)
    return True


def find_push_sender():
    """兼容参考脚本使用的 rnl_push / notify.py 推送接口。"""
    for module_name in ("rnl_push", "notify"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        sender = getattr(module, "sendNotify", None) or getattr(module, "send", None)
        if callable(sender):
            return module_name, sender
    return None, None


def send_push_notification(title, log_text):
    """优先使用青龙通知模块；不存在时使用脚本内置的 QQ SMTP。"""
    module_name, sender = find_push_sender()
    if sender:
        sender(title, log_text)
        return module_name
    if send_email_notification(title, log_text):
        return "内置 QQ SMTP"
    return None


def action_name(action):
    """将服务端动作转换为适合手机通知标题的中文。"""
    return {"checkin": "签到", "checkout": "签退"}.get(action, "打卡")


def set_push_title(outcome, title):
    if outcome is not None:
        outcome["title"] = title


def apply_random_delay(max_seconds):
    """在 0～max_seconds（含）之间随机等待整数秒。"""
    try:
        max_seconds = int(str(max_seconds).strip() or "0")
    except (TypeError, ValueError) as exc:
        raise DeliError("random_delay / DELI_RANDOM_DELAY 必须是非负整数秒") from exc

    if max_seconds < 0:
        raise DeliError("random_delay / DELI_RANDOM_DELAY 不能小于 0")
    if max_seconds == 0:
        return 0

    delay_seconds = secrets.randbelow(max_seconds + 1)
    print(f"随机延迟      : {delay_seconds} 秒（配置范围 0～{max_seconds} 秒）")
    expected_start = datetime.fromtimestamp(time.time() + delay_seconds)
    print("预计执行时间  :", expected_start.strftime("%Y-%m-%d %H:%M:%S"))
    if delay_seconds:
        time.sleep(delay_seconds)
    return delay_seconds


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

    def trusted_password_login(self):
        """使用已保存的 trust_code 进行常规密码登录。"""
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
        self.set_main_session(data)

    def send_login_sms(self):
        """按官方客户端算法发送登录验证码。"""
        nonce = "".join(
            secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6)
        )
        time_ms = str(int(time.time() * 1000))
        sign_raw = (
            nonce
            + self.cfg["mobile"][::-1]
            + SMS_CODE_TYPE_LOGIN
            + time_ms
            + SMS_SIGN_SECRET
        )
        sign = hashlib.sha256(sign_raw.encode("utf-8")).hexdigest()

        self.api(
            "POST",
            MAIN_BASE + "/api/v3.0/auth/app/sms/send",
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.cfg["user_agent"],
                "client_id": "eplus_app",
                "X-Service-Id": "userauth",
                "sign": sign,
            },
            json={
                "code_type": SMS_CODE_TYPE_LOGIN,
                "mobile": self.cfg["mobile"],
                "timestamp": time_ms,
                "nonce": nonce,
            },
        )

    def sms_login_and_create_trust(self, verify_code):
        """用短信验证码登录，并请求服务端创建新的 trust_code。"""
        data = self.api(
            "POST",
            MAIN_BASE + "/api/v2.0/auth/loginMobileWithVerifyCode",
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.cfg["user_agent"],
                "client_id": "eplus_app",
                "X-Service-Id": "userauth",
            },
            json={
                "mobile": self.cfg["mobile"],
                "verify_code": verify_code,
                "need_trust_code": True,
                "trust_client": "deliCheck-Python",
            },
        )["data"]

        trust_code = str(data.get("trust_code") or "").strip()
        if not trust_code:
            raise DeliError("短信登录成功，但响应中没有 trust_code")

        self.cfg["trust_code"] = trust_code
        os.environ["DELI_TRUST_CODE"] = trust_code
        save_env_value(ENV_FILE, "DELI_TRUST_CODE", trust_code)
        self.set_main_session(data)

    def set_main_session(self, data):
        self.main_token = data["token"]
        self.user_id = str(data["user_id"])

    def ensure_terminal_id(self):
        """确保设备标识存在；为空时生成一次 UUID 并持久化到 .env。"""
        terminal_id = str(self.cfg.get("terminal_id") or "").strip()
        if terminal_id:
            return terminal_id, False

        terminal_id = str(uuid.uuid4()).upper()
        try:
            save_env_value(ENV_FILE, "DELI_TERMINAL_ID", terminal_id)
        except OSError as exc:
            raise DeliError(
                "无法保存自动生成的 terminal_id；请手动设置 DELI_TERMINAL_ID"
            ) from exc

        self.cfg["terminal_id"] = terminal_id
        os.environ["DELI_TERMINAL_ID"] = terminal_id
        return terminal_id, True

    def bootstrap_trust_code(self):
        """首次运行时发送短信、读取验证码并持久化 trust_code。"""
        print("未检测到 trust_code，开始首次可信设备验证。")
        verify_code = os.getenv("DELI_SMS_CODE", "").strip()
        if not verify_code:
            if not sys.stdin.isatty():
                raise DeliError(
                    "当前环境无法交互输入验证码；请在终端运行 login，"
                    "或使用官方 App 获取验证码后临时设置 DELI_SMS_CODE"
                )
            self.send_login_sms()
            print("验证码已发送至：", mask(self.cfg["mobile"]))
            try:
                verify_code = input("请输入短信验证码：").strip()
            except (EOFError, KeyboardInterrupt):
                raise DeliError("未输入短信验证码")
        else:
            print("使用 DELI_SMS_CODE 中的临时验证码，不再重复发送短信。")

        if not verify_code:
            raise DeliError("短信验证码不能为空")

        self.sms_login_and_create_trust(verify_code)
        print("trust_code 已安全保存到：", ENV_FILE)

    def login_main(self):
        for key in ("mobile", "password"):
            if not self.cfg[key]:
                raise DeliError(f"缺少配置：{key}")

        if self.cfg["trust_code"]:
            self.trusted_password_login()
        else:
            self.bootstrap_trust_code()

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
        self.ensure_terminal_id()
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
        self.ensure_terminal_id()
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


def run(mode, expected=None, execute=False, outcome=None):
    deli = Deli()

    if execute:
        set_push_title(outcome, f"{action_name(expected)}失败｜得力 E+")
    elif mode == "login":
        set_push_title(outcome, "登录检查完成｜得力 E+")
    elif mode == "status":
        set_push_title(outcome, "状态查询完成｜得力 E+")
    elif mode == "form":
        set_push_title(outcome, "表单预览完成｜得力 E+")
    else:
        set_push_title(outcome, "仅检查，未提交打卡｜得力 E+")

    print("========== 得力 E+ 自动签到 ==========")
    print("运行时间      :", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("运行模式      :", mode)
    print("实际提交      :", "是" if execute else "否")

    apply_random_delay(CONFIG.get("random_delay", 0))

    print("\n正在登录主 App...")
    deli.login_main()
    print("主 App 登录成功")

    print("正在查询组织...")
    deli.select_org()
    print("组织选择成功 :", deli.org_name)

    print("正在登录综合签到...")
    deli.login_checkin()
    print("综合签到登录成功")

    terminal_id, terminal_created = deli.ensure_terminal_id()
    if terminal_created:
        print("已生成 terminal_id:", mask(terminal_id))
        print("已安全保存到      :", ENV_FILE)

    if mode == "login":
        print_login(deli)
        return

    print("\n正在查询今日排班...")
    shift = deli.shift()

    print("正在查询当前动作...")
    action = deli.action()
    if execute:
        set_push_title(outcome, f"{action_name(action)}失败｜得力 E+")

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
        set_push_title(outcome, "已跳过：今日非工作日｜得力 E+")
        print_status(deli, shift, action, records, gps)
        raise DeliError("今天不是工作日")

    if not shift.get("has_scheduled"):
        set_push_title(outcome, "已跳过：今日没有排班｜得力 E+")
        print_status(deli, shift, action, records, gps)
        raise DeliError("今天没有排班")

    if expected and action != expected:
        set_push_title(outcome, f"已跳过：当前应{action_name(action)}｜得力 E+")
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
    set_push_title(outcome, f"{action_name(action)}成功｜得力 E+")

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
    log_buffer = io.StringIO()
    outcome = {"title": "脚本异常，未完成打卡｜得力 E+"}
    exit_code = 0

    with redirect_stdout(TeeOutput(sys.stdout, log_buffer)):
        try:
            mode, expected, execute = parse_args()
            run(mode, expected, execute, outcome)
        except (DeliError, requests.RequestException, ValueError, KeyError) as e:
            print("失败：", e)
            exit_code = 1

    try:
        push_channel = send_push_notification(outcome["title"], log_buffer.getvalue())
        if push_channel:
            print(f"消息推送      : 成功（{push_channel}）")
        else:
            print("消息推送      : 未配置，已跳过")
    except Exception as e:
        print("消息推送      : 失败 -", e)

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
