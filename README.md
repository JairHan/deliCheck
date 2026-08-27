# deliCheck

得力 E+ 综合签到接口的分析与自动化脚本。项目通过复现 App 的登录、组织选择、排班查询、签到状态判断、GPS 规则校验和请求签名流程，实现命令行检查与按条件提交，并保留了签名算法的逆向分析材料。

> 本项目仅供接口研究、个人学习和经授权的自动化使用。请遵守所在组织的考勤制度、得力 E+ 服务条款及当地法律法规。使用者需自行承担账号、数据与操作风险。

## 当前能力

- 首次通过短信验证码获取并保存 `trust_code`，后续使用手机号和密码登录
- 自动获取组织信息，支持多组织时指定 `org_id`
- 登录综合签到服务
- 查询今日排班、当前应执行动作和今日打卡记录
- 获取服务端 GPS 打卡规则并进行距离校验
- 本地生成 `gps_info.sig` 双重 MD5 签名
- 预览完整提交表单，不发送打卡请求
- 支持签到、签退和按服务端状态自动提交
- 支持通过环境变量在青龙等定时任务平台运行

## 环境要求

- Python 3.9 或更高版本
- 可访问得力 E+ 相关接口的网络环境
- 一个有效且有综合签到权限的得力 E+ 账号

项目运行时仅依赖 `requests`：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## 配置

主程序为 `deli_eplus_auto_simple_v3.py`。推荐通过环境变量提供敏感信息：

| 环境变量 | 必填 | 说明 |
| --- | --- | --- |
| `DELI_MOBILE` | 是 | 得力 E+ 登录手机号 |
| `DELI_PASSWORD` | 是 | 登录密码 |
| `DELI_TRUST_CODE` | 首次可留空 | 留空时通过短信验证码获取并自动保存 |
| `DELI_TERMINAL_ID` | 可留空 | 留空时生成一次稳定 UUID 并自动保存 |
| `DELI_ORG_ID` | 多组织账号必填 | 组织 ID；单组织账号可留空自动选择 |
| `DELI_MODE` | 无参数运行时使用 | `dry`、`auto`、`checkin` 或 `checkout` |

安装依赖后，复制配置模板并填写一次即可。脚本会自动读取与自身位于同一目录的 `.env`：

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
DELI_MOBILE='你的手机号'
DELI_PASSWORD='你的原始登录密码'
DELI_TRUST_CODE=''
DELI_TERMINAL_ID=''
DELI_ORG_ID=''
DELI_MODE='dry'
```

系统环境变量和青龙环境变量的优先级高于 `.env`。单组织账号可将 `DELI_ORG_ID` 留空；多组织账号需填写目标组织 ID。`DELI_MODE` 首次建议使用 `dry`。

首次运行建议执行：

```bash
python3 deli_eplus_auto_simple_v3.py login
```

如果 `.env` 中的 `DELI_TRUST_CODE` 为空，脚本会自动发送登录短信并提示输入验证码。验证成功后，服务端返回的 `trust_code` 会写回 `.env`，文件权限会设置为 `600`；以后运行将直接使用密码和已保存的 `trust_code`，不再要求验证码。

如果 `DELI_TERMINAL_ID` 为空，脚本会在首次成功登录后生成一个大写 UUID 并写回 `.env`。该值只生成一次，以后始终复用；如果用户已经配置了官方 App 的设备标识，脚本不会覆盖。

非交互环境可先通过官方 App 获取验证码，再临时提供已经收到的验证码：

```bash
DELI_SMS_CODE='短信验证码' python3 deli_eplus_auto_simple_v3.py login
```

`DELI_SMS_CODE` 只用于当次验证，不会写入 `.env`。设置该变量时脚本不会再次发送短信。首次初始化更推荐在可交互终端中完成，再把生成的 `DELI_TRUST_CODE` 配置到青龙环境变量。青龙等非交互环境如果没有提供验证码，会在发送短信之前停止，避免定时任务反复发送验证码。

`gps_name`、`lat`、`lgt`、`gps_location` 和 `gps_range` 默认留空时，会使用服务端返回的第一条 GPS 规则。如需自定义这些非敏感高级选项，可修改脚本顶部的 `CONFIG`。

> `.env` 已被 `.gitignore` 排除。不要使用 `git add -f .env`，也不要把真实账号信息写回脚本或 `.env.example`。分享代码或抓包前，还应检查 HAR、日志和 Git 历史中是否残留 token、手机号、位置等敏感数据。若凭据曾被公开，应立即更换密码并撤销相关会话。

## 配置字段从哪里获取

手机号和原始登录密码由用户自行填写；`trust_code` 可由脚本在首次运行时通过短信验证自动获取。`terminal_id` 留空时会生成并持久化一个随机 UUID，也可以改为本人官方 App 请求中的真实设备标识。多组织账号的 `org_id` 可以在登录后根据脚本列出的组织信息填写。不要照搬他人的字段：这些值与账号、组织、设备或登录会话有关。

| 配置字段 | 获取位置 | JSON 路径 | 是否长期配置 |
| --- | --- | --- | --- |
| `mobile` | 本人的登录手机号 | 无需抓包 | 是 |
| `password` | 本人的原始登录密码 | 无需抓包 | 是 |
| `trust_code` | 首次短信登录响应或可信设备登录请求体 | `data.trust_code` | 可由脚本首次自动获取 |
| `org_id` | 组织列表响应 | `data[].org_id` | 仅多组织账号需要 |
| `terminal_id` | 自动生成，或官方 App 打卡提交请求体 | `terminal_id` | 留空时自动生成并保存 |
| `phone_model` | 打卡提交请求体 | `phone_model` | 可选，脚本已有默认值 |
| GPS 地点信息 | GPS 支持接口响应 | `data.gps_list[]` | 默认由脚本自动获取 |

注意以下字段不能混用：

- 环境变量 `DELI_PASSWORD` 应填写原始密码。抓包中的 `password` 已经过客户端编码，不要把它复制到环境变量中；脚本会自行完成编码。
- `Authorization`、`sourceToken` 和签到服务返回的 `token` 都是临时会话凭据，不需要写入 `CONFIG`。
- `member_id`、`user_id` 和组织成员序号会在登录过程中自动获取，不是 `DELI_ORG_ID`。
- 单组织账号可以不设置 `DELI_ORG_ID`，脚本会自动选择唯一组织。

## 抓包接口与参数

脚本涉及两个服务域名：

```text
https://v2-app.delicloud.com
https://checkin2-app.delicloud.com
```

不同客户端版本可能调整接口字段或版本号，应以本人官方 App 的实际请求为准。

### 1. 可信设备登录

```http
POST https://v2-app.delicloud.com/api/v3.0/auth/app/trusted/login
Content-Type: application/json
client_id: eplus_app
X-Service-Id: userauth
```

请求体结构：

```json
{
  "trust_code": "<需要提取的 trust_code>",
  "mobile": "<手机号>",
  "password": "<客户端编码后的密码>"
}
```

脚本通常不再需要从这里手工提取 `trust_code`；该请求主要用于理解后续可信密码登录。响应中的 `data.token` 和 `data.user_id` 会由脚本在每次登录时自动获取，不需要保存。

### 2. 查询组织列表

```http
GET https://v2-app.delicloud.com/api/v3.0/org/list?user_id=<user_id>
Authorization: <主 App 临时 token>
user_id: <user_id>
client_id: eplus_app
X-Service-Id: organization
```

响应中需要关注：

```json
{
  "data": [
    {
      "org_id": "<组织 ID>",
      "org_name": "<组织名称>",
      "seq_no": "<组织成员序号>"
    }
  ]
}
```

如果 `data` 只有一项，无需设置 `DELI_ORG_ID`；如果存在多个组织，把目标组织的 `org_id` 配置为 `DELI_ORG_ID`。

### 3. 登录综合签到服务

```http
POST https://checkin2-app.delicloud.com/api/v2.0/auth/login
Content-Type: application/json
client_id: eplus_app
x-service-id: auth
```

请求体结构：

```json
{
  "memberId": "<组织成员序号>",
  "sourceToken": "<主 App 临时 token>",
  "sourceId": "deli",
  "orgId": "<组织 ID>"
}
```

该接口用于换取综合签到服务的临时 token，并返回签到服务使用的 `member_id` 和 `org_id`。这些值由脚本自动处理，不需要手工配置。

### 4. 获取 GPS 打卡规则

```http
POST https://checkin2-app.delicloud.com/ass/api/v2.0/phone/checkin/support
Authorization: <综合签到临时 token>
member_id: <签到成员 ID>
org_id: <签到组织 ID>
x-service-id: ass
Content-Type: application/json
```

请求体为空对象。响应中的 `data.gps_list[]` 通常包含以下字段：

```json
{
  "name": "<打卡点名称>",
  "location": "<地址描述>",
  "lat": "<纬度>",
  "lgt": "<经度>",
  "range": 200
}
```

脚本默认自动读取这些规则，因此通常不需要手工填写 GPS 配置。

### 5. 获取设备标识

在官方 App 执行一次正常打卡，查找以下请求：

```http
POST https://checkin2-app.delicloud.com/ass/api/v2.1/phone/checkin/execute
Authorization: <综合签到临时 token>
member_id: <签到成员 ID>
org_id: <签到组织 ID>
x-service-id: ass
Content-Type: application/json
```

请求体结构：

```json
{
  "terminal_id": "<需要提取的设备标识>",
  "gps_info": {
    "time": "<毫秒时间戳>",
    "sig": "<本次请求签名>",
    "lat": "<纬度>",
    "lgt": "<经度>",
    "name": "<打卡点名称>",
    "location": "<地址描述>"
  },
  "checkin_type": "gps",
  "phone_model": "<设备型号>"
}
```

自动生成的 UUID 会作为 `terminal_id` 使用。如果服务端或所在组织不接受该值，可把请求体顶层的真实 `terminal_id` 保存为 `DELI_TERMINAL_ID`，它会覆盖自动生成值。`gps_info.time` 和 `gps_info.sig` 每次请求都会变化，不能复制为固定配置。

## Charles / Proxyman 抓包教程

以下流程仅适用于本人设备、本人账号或已明确授权的测试环境。

1. 在电脑上安装并启动 Charles 或 Proxyman，确保电脑与手机连接同一局域网。
2. 在抓包工具中查看电脑的局域网 IP 和代理端口，通常为 `8888`；以工具实际显示为准。
3. 打开手机当前 Wi-Fi 的代理设置，选择“手动”，服务器填写电脑 IP，端口填写抓包工具端口。
4. 按抓包工具的 iOS/Android 设备指引，在手机上安装其 CA 证书。iOS 还需要在“关于本机 → 证书信任设置”中启用完全信任。
5. 在抓包工具中为以下域名启用 SSL Proxying/HTTPS 解密：

   ```text
   v2-app.delicloud.com
   checkin2-app.delicloud.com
   ```

6. 完全关闭并重新打开得力 E+，使用本人账号正常登录，然后进入综合签到页面。
7. 使用 URL 关键字依次筛选 `trusted/login`、`org/list`、`auth/login`、`checkin/support` 和 `checkin/execute`。
8. 在请求详情的 JSON Body 中提取 `terminal_id`；多组织账号再从 `org/list` 响应中确定 `org_id`。`trust_code` 默认由脚本通过短信验证自动获取。
9. 配置完成后移除手机 Wi-Fi 代理，并根据需要删除或停用抓包 CA 证书。

如果只能看到 CONNECT、请求失败或 App 提示网络异常，通常表示证书没有正确安装/信任，或当前客户端启用了证书绑定。Android 7 及以上版本的 App 也可能默认不信任用户安装的 CA。请优先使用抓包工具提供的官方设备教程和已授权测试设备，不要在不属于自己的设备或账号上绕过安全控制。

抓取 `checkin/execute` 会伴随一次真实的官方 App 打卡。请在正常考勤时间、正确地点和符合所在组织制度的情况下操作，避免为获取参数反复提交。

### HAR 保存与脱敏

HAR 通常包含完整请求头、登录 token、手机号、设备标识、组织信息、定位和签到记录，应视为账号密码同等级别的敏感文件：

- 不要提交到 Git、网盘公开链接或公开 issue。
- 不要把完整 HAR 发给不可信的第三方。
- 分享排障片段前，删除 `Authorization`、Cookie、token、手机号、坐标和设备 ID。
- 完成提取后可删除 HAR，或保存在加密目录中。
- 如果 HAR 曾被公开，应更换密码、重新登录以刷新会话，并撤销仍有效的设备或 token。

## 使用方法

建议先依次执行只读命令，确认账号、排班和 GPS 规则均符合预期，再考虑提交。

```bash
# 仅验证登录并显示组织信息
python3 deli_eplus_auto_simple_v3.py login

# 查看今日排班、当前动作、打卡记录和 GPS 校验结果
python3 deli_eplus_auto_simple_v3.py status

# 展示将要发送的请求头和请求体，但不提交
python3 deli_eplus_auto_simple_v3.py form

# 完整检查，默认 dry-run，不提交
python3 deli_eplus_auto_simple_v3.py check
```

确认无误后，可以显式添加 `--execute`：

```bash
# 按服务端返回的当前动作提交
python3 deli_eplus_auto_simple_v3.py check --execute

# 只有当前动作是签到时才提交
python3 deli_eplus_auto_simple_v3.py check --expected checkin --execute

# 只有当前动作是签退时才提交
python3 deli_eplus_auto_simple_v3.py check --expected checkout --execute
```

程序会在以下情况拒绝提交：

- 当天不是工作日
- 当天没有排班
- 当前动作与 `--expected` 不一致
- 指定坐标不在服务端允许的 GPS 范围内
- 缺少登录、组织或设备配置
- 接口请求或响应校验失败

### 无参数运行与青龙

无参数运行时，程序读取 `DELI_MODE`：

| `DELI_MODE` | 行为 |
| --- | --- |
| `dry` 或其他值 | 只检查，不提交 |
| `auto` | 按服务端当前动作真实提交 |
| `checkin` | 当前动作为 `checkin` 时真实提交 |
| `checkout` | 当前动作为 `checkout` 时真实提交 |

```bash
DELI_MODE=dry python3 deli_eplus_auto_simple_v3.py
```

> 未设置 `DELI_MODE` 且不带命令行参数时，程序默认采用 `dry`，不会提交。真实提交仍应通过明确设置模式或添加 `--execute` 来启用。

在青龙面板中，可将上述账号配置和 `DELI_MODE` 添加为环境变量，再按需要设置定时任务。例如，先使用以下命令观察日志：

```bash
DELI_MODE=dry python3 deli_eplus_auto_simple_v3.py
```

确认运行结果后，再根据实际制度选择 `checkin`、`checkout` 或 `auto`。不建议在尚未验证排班和账号配置时直接启用自动提交。

## 工作流程

```text
手机号登录
   ↓
获取并选择组织
   ↓
登录综合签到服务
   ↓
查询排班、当前动作与今日记录
   ↓
获取并校验 GPS 规则
   ↓
生成当前时间戳与 gps_info.sig
   ↓
dry-run / 表单预览 / 条件提交
```

签名逻辑位于 `Deli.gps_proof()`。它使用本次请求的毫秒时间戳以及 `lat`、`lgt`、`location`、`name` 生成内层 MD5，再加入客户端固定后缀生成最终 MD5。字段值和顺序必须与请求体一致，否则服务端会拒绝签名。

## 项目结构

```text
.
├── .env.example                   # 不含真实值的配置模板
├── .gitignore                     # Git 忽略规则
├── README.md                      # 使用说明
├── requirements.txt               # Python 依赖
└── deli_eplus_auto_simple_v3.py  # 主程序
```

HAR、IPA、提取后的 JS、反编译输出和一次性验证脚本属于本地研究材料，不参与程序运行，并通过 `.gitignore` 排除，以避免泄露个人信息或提交大文件。

## 实现说明

- 登录密码会按照客户端格式进行 Base64 编码并反转后发送。
- 请求使用两个服务域名：主 App 服务负责登录与组织查询，综合签到服务负责排班、规则和打卡。
- GPS 坐标为空时，代码会选用服务端第一条规则的中心点；指定坐标时，会使用 Haversine 公式检查距离。
- `form` 模式会生成新的时间戳和签名，但只打印请求，不发送。
- Authorization 输出会被掩码；其他日志仍可能含组织、位置与成员信息，请谨慎保存和分享。
- 签名算法来自客户端行为分析，后续 App 或服务端升级可能使其失效。

## 常见问题

### 提示“账号有多个组织”

为 `DELI_ORG_ID` 设置目标组织 ID，或在 `CONFIG['org_id']` 中填写。错误信息会列出当前账号可用的组织名称和 ID。

### GPS 校验失败

先执行 `status` 查看服务端规则。如果手动配置了经纬度，确认坐标在允许范围内；也可以将 GPS 配置留空，让程序读取服务端规则。

### 当前动作与预期不一致

这是 `--expected` 的保护行为。例如指定 `--expected checkout` 时，如果服务端认为当前应签到，程序不会提交。

### 登录或签名突然失效

账号会话、`trust_code`、接口字段和签名规则都可能随客户端升级而变化。如果 `trust_code` 已失效，可将 `.env` 中的 `DELI_TRUST_CODE` 清空，再运行一次 `login` 重新完成短信验证。仍然失败时，先检查账号能否在官方 App 正常使用，再通过本地抓包结果定位差异。请勿在日志或 issue 中公开完整 token、密码和 HAR。

## 免责声明

本仓库不是得力官方项目，与得力集团或得力 E+ 无隶属、合作或背书关系。项目按现状提供，不保证接口长期可用，也不保证自动化结果满足任何组织的考勤要求。请仅操作本人账号及获授权的数据。
