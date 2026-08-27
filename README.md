# deliCheck

得力 E+ 综合签到接口的分析与自动化脚本。项目通过复现 App 的登录、组织选择、排班查询、签到状态判断、GPS 规则校验和请求签名流程，实现命令行检查与按条件提交，并保留了签名算法的逆向分析材料。

> 本项目仅供接口研究、个人学习和经授权的自动化使用。请遵守所在组织的考勤制度、得力 E+ 服务条款及当地法律法规。使用者需自行承担账号、数据与操作风险。

## 当前能力

- 使用手机号、密码和 `trust_code` 登录得力 E+
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
| `DELI_TRUST_CODE` | 是 | 可信设备登录所需的 `trust_code` |
| `DELI_TERMINAL_ID` | 提交时必填 | 设备唯一标识 |
| `DELI_ORG_ID` | 多组织账号必填 | 组织 ID；单组织账号可留空自动选择 |
| `DELI_MODE` | 无参数运行时使用 | `dry`、`auto`、`checkin` 或 `checkout` |

示例：

```bash
export DELI_MOBILE='你的手机号'
export DELI_PASSWORD='你的密码'
export DELI_TRUST_CODE='你的 trust_code'
export DELI_TERMINAL_ID='你的设备标识'
```

也可以修改脚本顶部的 `CONFIG`。其中 `gps_name`、`lat`、`lgt`、`gps_location` 和 `gps_range` 默认留空时，会使用服务端返回的第一条 GPS 规则；如果账号属于多个组织，需要明确填写 `org_id`。

> 安全提示：不要把真实账号信息写入 `CONFIG` 后提交。分享代码或抓包前，还应检查 HAR、日志和 Git 历史中是否残留 token、手机号、位置等敏感数据。若凭据曾被公开，应立即更换密码并撤销相关会话。

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
├── deli_eplus_auto_simple_v3.py  # 当前主程序
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

账号会话、`trust_code`、接口字段和签名规则都可能随客户端升级而变化。先检查账号能否在官方 App 正常使用，再通过本地抓包结果定位差异。请勿在日志或 issue 中公开完整 token、密码和 HAR。

## 免责声明

本仓库不是得力官方项目，与得力集团或得力 E+ 无隶属、合作或背书关系。项目按现状提供，不保证接口长期可用，也不保证自动化结果满足任何组织的考勤要求。请仅操作本人账号及获授权的数据。
