# ds-trim-gateway — DeepSeek 网页端精简代理

## 解决什么问题

DSH 经 AFS 转发给 DeepSeek 网页端的 system 消息高达 ~60KB（10KB system +
58 个工具完整 JSON schema ≈ 58KB）。DeepSeek 网页端对这种超长 system 会触发
风控**禁言**；实测对照：只保留「用户提问 + 精简 system + 精简工具列表」
（几百字符）时，DeepSeek 能正常输出 XML 工具调用且**不触发禁言**。

本程序在 DSH 与 AFS 之间做消息精简：

```
DSH → ds-trim-gateway (8001) → AFS (8080) → DeepSeek 网页端
```

## 架构

| 入口 | 端口 | 作用 |
|---|---|---|
| OpenAI 兼容端点 `/v1/chat/completions` | 8001 | DSH 的 afs provider 指向这里；收 DSH 请求 → 精简 → 转发 AFS → 解析 XML → 返回 OpenAI tool_calls（DSH 执行工具） |
| Gradio 配置界面 | 7860 | 手动维护精简工具列表、system 提示词、上游配置、日志 |

## 快速开始

前置条件：Python 3.9+、一个运行中的 **AFS**（`127.0.0.1:8080`）。
纯文本链路要求 AFS **v1.5.8+**；`@` 本地图片识别要求 AFS **v1.7.0+**（详见「本地图片」一节）。

### macOS / Linux

```bash
# 1. 首次安装
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 启动（网关 8001；加 --ui 同时开配置界面 7860）
./start.sh --ui
```

也可以分开启动：
```bash
.venv/bin/python server.py --port 8001   # 网关
.venv/bin/python ui.py --port 7860       # 配置界面
```

> `./start.sh` 报 Permission denied 时（个别解压工具会丢可执行位），用 `bash start.sh --ui`。

### Windows（PowerShell / cmd）

venv 的可执行文件在 `Scripts\` 而非 `bin/`，`start.sh` 是 bash 脚本不能直接跑，
请用附带的 `start.bat`：

```bat
:: 1. 首次安装
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt

:: 2. 启动（网关 8001；加 --ui 同时开配置界面 7860）
start.bat --ui
```

> 用 `python` 而不是 `py -3`：`py` 是 Windows 的 Python Launcher，只有 python.org
> 的官方安装包才会附上；Microsoft Store 版 Python 不自带，执行 `py -3` 会直接报
> "无法将'py'项识别为 cmdlet..."。若 `python` 也报错，用 python.org 安装包重装并
> 勾选 "Add python.exe to PATH"。

也可以分开启动（开两个终端）：
```bat
.venv\Scripts\python.exe server.py --port 8001   :: 网关
.venv\Scripts\python.exe ui.py --port 7860       :: 配置界面
```

> Windows 没有 `/tmp`，网关已按平台自动选择落盘目录（POSIX → `/tmp`，
> Windows → 系统临时目录如 `%TEMP%`），也可用 `DS_GW_LOG_FILE` /
> `DS_GW_PARSEFAIL_FILE` 显式覆盖；`start.bat` 会固定指到 `%TEMP%`。

## 接入 DSH

把 DSH 的 afs provider 的 baseURL 指到网关（或改 settings.yaml）：

```yaml
llm-pi-ai:
  providers:
    afs-trim:
      api: openai-completions
      baseURL: http://127.0.0.1:8001/v1   # 指向本网关
      models:
        - id: deepseek-v4-pro
```

## 配置（三种方式）

**1. 双击启动器（最终用户）**：`dist\ds-trim-gateway.exe`，单文件、免装 Python。
窗口分五个页签，常用配置全在界面里，不必手改 JSON：

| 页签 | 内容 |
|---|---|
| **基础** | 客户端预设（WorkBuddy / DSH）、上游地址 / API Key / 模型、端口、请求间隔、空响应冷却 |
| **提示词** | 精简 system 全文编辑、丢弃客户端 system、system 最大字符数、「载入当前预设提示词」 |
| **工具 (JSON)** | 工具列表 JSON 直接编辑（可粘贴整段 OpenAI 函数定义），「校验并格式化」会检查语法并回显工具名 |
| **上下文精简** | 工具历史轮数 / 单条截断 / 总预算，对话历史开关 / 条数 / 截断 / 总预算，日期注入，本地图片开关与张数 |
| **日志** | 实时日志、刷新、清空 |

底部操作条常驻：`保存配置`（写 config.json，网关热重载）、`启动网关`、`停止`、
`重新载入`（外部手改 config.json 后拉回来）、`打开 config.json`、`打开配置目录`、`打开日志`。

> **保存语义**：`tools` 与 `system_prompt` 取的是**编辑器里的内容**，不会被预设悄悄覆盖。
> 切换客户端预设时，若你手改过提示词或工具，会先问你要不要覆盖——选「否」则保留你的编辑。

自查：`ds-trim-gateway.exe --selftest`（结果写到同目录 `selftest-result.txt`）。

**2. Gradio UI（开发调试）**：http://127.0.0.1:7860 ，配置项见下。

### 发给别人用（绿色分发包）

```
.venv/Scripts/python.exe build_release.py
```

产出 `dist/分发包/` 与 `dist/ds-trim-gateway-分发包.zip`（约 140 MB）：

| 内容 | 说明 |
|---|---|
| `ds-trim-gateway.exe` | 网关本体，单文件免装 Python |
| `config.json` | 预置 DSH 预设 + 上游 `127.0.0.1:8080` + 模型 `DeepSeek-V4.1-Flash` |
| `AFS/` | AFS 便携版（Electron，约 230 MB），对方直接双击即可 |
| `1-先看这里.txt` | 三步上手 + 排障清单 |
| `2-客户端接入.txt` | DSH（`cordis.patch.yml`）与 WorkBuddy（`models.json`）怎么指过来 |
| `3-扩展工具与提示词.txt` | 任务超出 7 个工具时，让 AI 帮补「工具 + 提示词」的填空模板 |
| `网关使用说明.txt` | 网关本身的完整说明 |

**三件事决定了对方能不能用，缺一不可**：

1. **exe 自己不含任何账号**。真正持有 DeepSeek 会话的是 AFS，网关只是翻译层。
   默认上游是 `http://127.0.0.1:8080/v1`，对方机器没有 AFS 就是 connection refused。
2. **exe + config.json 必须成对发**。冻结态读 `sys.executable` 同目录的 `config.json`；
   只发 exe 会回落 `DEFAULT_CONFIG`（空 Key + 本地 8080），必然失败。
3. **AFS 只监听 `127.0.0.1:8080`**，同网段别的机器连不上。所以每人装一份，别想共享。

**不要发 `%APPDATA%\chat2api`** —— 那是 AFS 的登录态与 Cookie，含你自己的账号。

> 打包时**必须用 `zipfile`**（`build_release.py` 已内置）。用 `bsdtar` / 资源管理器压缩
> 会把中文名按 OEM 码页写进去，到 7-Zip / macOS 上变成 `╖╓╖ó░ⁿ` 乱码。

### 客户端预设（最容易踩的坑）

工具名与参数形状必须与**客户端实际暴露的**一致，否则调用要么被当未知工具丢弃，
要么被 schema 校验拒绝——两种都是一轮白跑。**只支持 Windows**：

| 预设 | shell | 读文件 / 找文件 | 搜索 | 读网页 |
|---|---|---|---|---|
| **WorkBuddy（Windows）** | `PowerShell` | `Read` / `Write` / `Edit` / `Glob` | `WebSearch(query)` | `WebFetch(url, prompt)` |
| **DSH（Windows）** | `pwsh` | `read` / `write` / `edit` / `glob` | `web_search(queries[])` | `web_fetch(url)` |

注意两个客户端的**参数形状也不同**：搜索是 `query: "…"` vs `queries: ["…"]`；
读网页是 `url + prompt` vs 只要 `url`。只改名字不改形状，仍会被客户端拒。

预设还会同步 system 提示里的 shell 语法（都要求 `Get-ChildItem`/`Get-Content`/
`Select-String`、原生 `C:\...` 路径、`$env:NAME`，并警告不要用 POSIX 命令）。
应用预设后需再点「保存配置」才生效。

> **兜底层**：网关会读请求体里的 `tools`，按客户端实际声明把名字与参数形状改写
> 过去（日志 `工具名适配: PowerShell → pwsh`）。预设选错通常也能跑通，但会多绕
> 一步，且搜索参数仍以预设为准更稳。
>
> 历史坑：更早的版本按 macOS/Windows 分预设（`Bash`/`pwsh`），后来又误改成
> WorkBuddy 的名字生搬给 DSH——两次都是"工具调用永远失败"。现在按客户端区分。

### 文档 / PPT 工作流（2026-10-28）

目标场景：**把 Word 里的内容做成 PPT**。这套能力不需要额外装 Python 库——
本机 PowerPoint / Word / WPS 的 COM 都可用，实测生成一页 pptx 约 9 秒。

为此在预设里加了两个工具，并在 system 提示末尾追加三条规则（`_DOC_WORKFLOW_RULE`）：

| 工具 | WorkBuddy | DSH | 参数面（两者一致） |
|---|---|---|---|
| 写文件 | `Write` | `write` | `file_path` + `content` |
| 改文件 | `Edit` | `edit` | `file_path` + `old_string` + `new_string`(+`replace_all`) |

提示词里的三条（网关不发 `tools` schema 给上游，**这里是模型唯一的信息源**）：

7. 写/改文件用 `Write`/`Edit`，不要用 PowerShell here-string 写长文本（中文、引号、`$` 必错）
8. 读 `.docx` 不要用 `Read`：`Copy-Item` 改名 `.zip` → `Expand-Archive` → 读
   `word\document.xml` 去标签；**`Add-Type` 被安全策略拦截，别走 .NET 解压**；
   正文长时先落盘 `.txt`，再用 `Read` 的 `offset`/`limit` 分段读
9. 生成 `.pptx`：`New-Object -ComObject PowerPoint.Application` → `Slides.Add(序号, 2)`
   → `SaveAs(绝对路径, 24)` → `Close` + `Quit`

**配套放宽的截断**（不改的话长文档根本读不进来）：

| 键 | 旧 | 新 | 理由 |
|---|---|---|---|
| `history_result_chars` | 3000 | 8000 | 一次 `Read` 读 200 行中文 ≈ 8000 字符 |
| `history_total_chars` | 8000 | 24000 | 约三次满额读 |
| `max_system_chars` | 1500 | 2000 | 新增三条规则约 400 字，1500 会把它截掉 |

> **代价**：单轮提示变长约 3 倍，更接近上游风控阈值。若出现空响应/禁言，
> 先把 `history_result_chars` 调回 3000、`history_total_chars` 调回 8000，
> 改成"多轮少量读"而不是一次拉全文。
>
> 实测样本：《公司管理制度汇总》正文 **333,043 字 / 17,557 段**——
> 这种量级必须分段读 + 用 `Write` 把中间大纲落盘，靠一次工具结果是拿不到的。

- **上游 AFS 地址**：默认 `http://127.0.0.1:8080/v1`
- **上游模型**：默认 `deepseek-v4-pro`
- **精简 System 提示词**：默认用实测有效的「工具调用大师」措辞——明确要求
  只输出 XML 工具调用块（可自行修改；切换预设会一并替换）
- **精简工具列表**：JSON 数组，手动维护（默认 7 个：shell + Read/Write/Edit/Glob +
  WebSearch/WebFetch）。只把这里列的工具描述发给 DeepSeek，避免 58 个工具的巨型 schema
- **丢弃客户端 system**：默认勾选——忽略客户端（WorkBuddy / DSH）的 10KB system，只用精简版
- **system 最大字符数**：默认 2000（文档/PPT 工作流那三条提示词约 400 字，1500 会把它截掉）
- **带上最近几轮对话历史**：默认勾选。控制发给上游的提示里是否包含此前的
  「用户提问 + 助手回答」（**不含**工具结果，工具结果由下面那组参数单独管）
- **对话历史条数**：默认 6（user + assistant 合计）
- **对话历史字符预算**：默认 3000（超出从最早一条丢弃）

本地图片：开关与「单次最多张数」在界面「上下文精简」页；其余细项仍在 `config.json`：

| 键 | 默认 | 说明 |
|---|---|---|
| `local_image_enabled` | `true` | 总开关；关掉后行为与改造前一致 |
| `workspace_root` | `""` | 相对路径的兜底根目录（优先自动读运行时上下文） |
| `local_image_exts` | PNG/JPEG/WebP/GIF/BMP | 扩展名初筛 |
| `local_image_max_bytes` | 8388608（8MB） | 单张上限，与网页端上传上限一致 |
| `local_image_max_count` | 4 | 单次请求最多附带几张 |
| `local_image_restrict_to_workspace` | `false` | 限制图片必须在 workspace 内（共享部署建议开） |

配置保存在 `config.json`。

## 网关代搜（客户端搜索坏了时的兜底）

**现象**：在 DSH 里问「重庆今日天气如何」，模型反复调 `web_search`，最后给一段
没有出处的泛泛之谈——查数据类问题（天气、股价、新闻）全都答不出确切数字。

**根因（已实测定位）**：DSH 的 `web_search` **不是本地工具**，它由内置插件
`dsh-web-search-deepseek` 把查询发到 `https://api.deepseek.com/anthropic/v1/messages`，
用 Anthropic 的 `web_search_20250305` 让服务端去搜，凭据是客户端自己存的 API Key。
于是出现一个很割裂的局面：

| 链路 | 走向 | 是否需要 API Key | 实际状态 |
|---|---|---|---|
| 聊天 | 客户端 → 本网关(8001) → AFS(8080) → DeepSeek 网页端 | 不需要 | 正常 |
| 搜索 | 客户端 → `api.deepseek.com`（**绕开网关**） | 需要 | Key 失效 → 固定 401 |

原始报错（取自 `.dsh/sessions/**/session.v4.jsonl.zstd`）：

```text
Error: DeepSeek API error (HTTP 401): Authentication Fails,
Your api key: ****78bN is invalid
```

这种情况**改提示词救不了**——工具根本不经过网关。所以网关把搜索这一环接过来：
模型要 `web_search` 时，网关自己搜（必应 RSS）、抓回网页正文，把「标题 + URL + 正文」
塞回对话让模型作答，客户端那边不会再看到这个工具调用。

| 键 | 默认 | 说明 |
|---|---|---|
| `server_search_mode` | `auto` | `auto`=检测到客户端搜索报过鉴权错才代搜（进程内记住，之后一直代搜）；`on`=总是代搜；`off`=关闭 |
| `server_search_max_rounds` | 2 | 一次请求最多代搜几轮（每轮多调一次上游，受最小间隔限流） |
| `server_search_max_queries` | 2 | 最多用几个查询 |
| `server_search_max_results` | 5 | 结果条数上限 |
| `server_search_fetch_top` | 2 | 抓几个页面的正文（0=只给标题和摘要） |
| `server_search_page_chars` | 1200 | 单页正文截断字符 |

界面在「上下文精简 → 搜索」里可改模式、抓取页数、每页字符。

> **为什么查询要改写**：必应对长查询很敏感——模型爱写的
> `重庆今天天气 2026年10月23日, 重庆今日气温 天气现象 湿度` 会把中国天气网挤到第 6 条之后，
> 前 5 条是百度百科 / 政府门户 / 旅游攻略。网关会把日期、时间词、标点摘掉再搜
> （`重庆天气`），然后**轮流取**各变体的结果，保证精准结果能露脸。
>
> **为什么正文不能只截开头**：资讯页前几百字几乎全是导航（"首页/预报/预警/热门城市…"），
> 只截头部等于把带数字的表格挤出预算。网关按「数字 + ℃ + %」算密度挑种子，再从那里连续取。

### 配置改完立即生效（热重载）

UI(7860) 与网关(8001) 是**两个独立进程**：UI 保存只会写 `config.json`，不会通知网关。
所以网关在**每次读配置时比对 `config.json` 的 mtime**，发现被外部改动就自动重载 ——
在 7860 改完「上游模型 / 工具 / 提示词」**无需重启网关**即可生效。

- 想关掉：环境变量 `DS_GW_CONFIG_HOT_RELOAD=0`（多实例共享同一份 `config.json` 时，
  可自行控制重载时机）
- `config.json` 被写坏时**保留内存里的旧配置**并记日志，不会把网关弄崩
- 代价只是每次读配置多一次 `os.path.getmtime`，可忽略

> ⚠ 直接编辑 `config.json` 同样会触发重载，所以**手动改文件也立即生效**——
> 改的时候注意别写成半截（写到一半被读到会按坏文件处理，保留旧配置）。

## 关键设计

1. **消息精简**：只保留「精简 system + 最近几轮对话历史 + 最近几轮工具结果 +
   最后一条真实提问」。自动剥离 DSH 注入的 `<system-reminder>` 技能目录、
   `Current runtime context`、文件策略/审批策略等噪音（它们以额外 user 消息
   追加在提问后）。拼出来的提示长这样：

   ```
   [此前对话历史]
   用户: 查一下今天A股行情
   助手: 根据数据：上证指数收于 3939.33 点…

   [此前已执行的工具结果]
   - 调用了 web_search({"queries":["A股今日行情"]})
     结果: …

   [当前用户问题]
   那成交额呢？
   ```
2. **XML 解析（4 层递进 + 全角归一化）**：把上游返回的 XML 解析成 OpenAI 格式
   `tool_calls`。踩过的大坑见下节「DeepSeek DSML 全角竖线陷阱」。
3. **解析失败兜底**：上游文本若"看起来是工具调用"却没解析出来，**绝不透传**给
   客户端（否则 DSH 会把原始 XML 当回答正文显示），而是冷却后重试；重试耗尽则
   把 XML 残留清理成可读文本再返回。完整原文落盘到 `DS_GW_PARSEFAIL_FILE`
   （默认 POSIX 为 `/tmp/ds-gw-parsefail.log`，Windows 自动改用系统临时目录）
   便于定位新变体。
4. **参数规整**：按 schema 裁剪未声明参数（实测模型会给 `web_search` 同时输出
   `query` 和 `queries`，DSH 严格校验会报错白费一轮）、同名参数聚合成数组、
   必填缺失按类型补齐、空数组用用户提问兜底。
5. **防编造数据**：`web_search` 的结果只是一串来源链接，模型拿到后会凭记忆编
   数字（实测把 3939.33/-0.31% 编成 3812.51/+0.42%）。网关检测「历史里只有
   来源列表、没有正文」时注入强制指令，要求先 `read_page` 取回真实数据再回答。
6. **限流感知重试**（实验验证）：DeepSeek 网页端在 **~10 次/分钟连发**后会临时
   限流——表现为 HTTP 200 但 content 为空，限流窗口约 60 秒，**窗口内立即重试
   全部无效**且增加风控权重。因此空响应后**等待 60 秒（可配 `retry_backoff_ms`）
   再重试**，重试上限 2 次，避免重试风暴触发账号级风控。
7. **并发节流**：串行转发（`threading.Lock`），避免并发触发网页端风控。
8. **工具历史累积（多处修复）**：实测发现只保留「最近一轮」工具结果会让模型
   每轮"失忆"——它不知道此前已搜到/读到什么，于是反复重搜（某次 A 股行情查询因此
   做了 45 次工具调用并触发禁言）。现在：
   - 累积最近 `history_max_rounds`（默认 3）轮工具调用与结果
   - 单条结果先过滤站点导航/广告行，再按 `history_result_chars`（默认 8000）截断
     （实测某财经页面 21568 字符里绝大多数是导航链接，真正行情数据被淹没）
   - 历史总长受 `history_total_chars`（默认 24000）约束，超出从最早一轮丢弃
9. **请求速率限制**（实测结论）：DeepSeek 网页端在单分钟 >10 次请求时会触发风控
   （账号级禁言，网页手动访问也受限）。gateway 强制两次上游请求间隔
   ≥`min_request_interval_ms`（默认 12000ms ≈ 5 次/分钟），超速时排队等待。
   代价是任务变慢（多步任务如行情查询会从几分钟延长到十几分钟），但可避免禁言。
10. **对话历史**（补上原先缺的那一半上下文）：早期版本只发最后一条提问，于是
    带指代的追问失去先行词——「把刚才那个结果整理成表格」「那深证成指呢？」模型
    既看不到自己上一条回答，也不知道用户此前问过什么，只能重新搜一遍。现在把
    最近 `history_dialogue_msgs`（默认 6 条，user+assistant 合计）的**纯文本**
    轮次拼进提示，受 `history_dialogue_chars`（单条 800）与
    `history_dialogue_total_chars`（合计 3000）约束。
    - 只收纯文本：`system`/`tool` 消息跳过，只有 `tool_calls` 没有正文的
      assistant 也跳过——工具结果已由第 8 条单独渲染，重复出现只会白占预算
    - 历史文本同样过 `_clean_user_text` + XML 残留清洗，避免模型模仿损坏格式
    - 用 `history_include_dialogue: false` 可关掉，退回"只发最后一条提问"
    - 上游是网页聊天，没有原生多轮 role 结构，所以历史只能扁平化进同一条
      user 消息（`用户:` / `助手:` 前缀区分）

11. **当前日期显式注入**：system 提示词里原本没有任何日期信息，而模型对「今天」
    的推断恰恰是最容易悄悄出错的一类。实测发现上游 DeepSeek 网页端**自己会**
    注入当天日期（绕开 gateway 直连上游问日期也答得对），模型被直球问日期时还会
    主动 `bash date` 兜底——所以缺日期时行为看着是正常的。但那是个**静默依赖**：
    一旦换上游、或换成不注入日期的裸 API，日期会无声退化成「靠模型猜」，不报错、
    只答错。现在 gateway 自己在 system 末尾追加一行
    `【当前日期】今天是 YYYY年M月D日 星期X…`，把隐式依赖变成显式行为。
    - 每次请求**实时求值**，不是模块级常量——网关是常驻进程，可能连跑数天，
      写成常量会跨天不更新
    - 中文星期名自建映射，不用 `strftime('%A')`：Python 默认 C locale 会输出
      英文 `Wednesday`，而 `setlocale` 又会污染进程全局状态
    - 在 `max_system_chars` 截断**之后**追加，保证不会被截掉
    - `system_prompt` 为空时也照样注入（日期是运行期事实，不是提示词自定义内容）
    - 用 `inject_current_date: false` 可关掉

12. **单轮多工具调用裁剪**：客户端（WorkBuddy）把一条消息里的多个 `tool_calls`
    当成**一个名字**去校验——`['PowerShell','PowerShell']` 报
    `PowerShellPowerShell does not exist`，`['Read','Glob']` 报 `ReadGlob`，
    客户端侧改不了。实测一次 63 次调用的会话里 11 次是这种同名重复，每次都白跑
    一轮（报错 → 客户端 error-recovery → 模型改成串行重发）。网关在**交付前**
    按 `max_tool_calls_per_round`（默认 1）裁剪，把「报错后再重试」变成「静默串行」。
    - `0` = 不限制；设 2 表示允许同时交付两个（仅当确认客户端能接受时）
    - 被丢弃的调用名会写进日志（`✂ 本轮 N 个调用按配置只交付 M 个，丢弃 [...]`），
      便于确认是"模型想并行"还是别的问题
    - 裁剪放在补参/改名/代搜之后，只有真正交付给客户端的那一刻才收敛

## 本地图片（`@` 引用本地图片文件）

**为什么需要**：DSH 的 `@文件` 引用（`dsh-at-file` 插件）只把**文件名当文本**发出来
（`<workspace-reference path="图片.png" kind="file" />`），图片字节从未进入请求，
模型只看到一个文件名，于是答「我没有读取图片内容的能力」。
而 DeepSeek 网页端自己上传图片时走的是原生通道（`POST /v0/file/upload_file` 拿
`file_id` → completion 带 `ref_file_ids`），所以同一张图在网页端能识别。

网关和用户在同一台机器上，因此可以自己把路径补全成绝对路径，交给上游 AFS 读盘上传：

```
DSH @图片.png  →  网关解析成绝对路径并注入标记  →  AFS 读盘上传拿 file_id
                                                        →  completion 带 ref_file_ids
```

**标记协议**：网关在「当前用户问题」之后追加一行（每张图一行）

```
[[local-image:/Users/name/Downloads/zero/测试/图片.png]]
```

AFS 消费这个标记（上传 + `ref_file_ids`）并把标记整行剥掉，
**本地绝对路径绝不会出现在送给模型的正文里**。原始的
`<workspace-reference>` 标签会保留，让模型仍知道文件叫什么。

**引用来源**（按识别优先级去重，五类都支持）：

| 来源 | 例子 |
|---|---|
| 客户端图片降级标签 | `<image_local_path>C:\Users\a\图.png</image_local_path>` |
| DSH 结构化标签 | `<workspace-reference path="图片.png" kind="file" />` |
| 正文里的 `@` 提及 | `@图片.png 图里是什么内容` |
| 正文里的带路径引用 | `/Users/name/a.png`、`~/a.png`、`./a.png`、`C:\Users\a\a.png` |
| 正文里的裸文件名 | `看看 图片.png`（无 `@`、无路径） |

> **`@` 提及不再用「一整个正则框住文件名」**：图片名常常是网页另存来的，带逗号/等号/与号，
> 例如 `@u=2448247082,481968747&fm=253&app=138&f=JPEG.jpg`。任何「排除若干标点」的字符类
> 都会被其中的逗号切断，于是整轮认不出这张图，模型只能 `read` 一个 jpg、拿到
> `binary file` 错误，最后回答「我无法查看图片」。现在的做法是：`@` 后面整段 token 抓下来
> （引号形式 `@"带 空格.png"` 单独处理）→ 逐字符剥尾部标点 → 命中图片扩展名即采纳。
> 是否为真图片仍由存在性 + 扩展名 + 魔数三重校验决定，误报没有任何副作用。

> **为什么需要第一类**：客户端在模型声明 `supportsImages: false` 时不会传图片字节，
> 只把本地路径降级成 `<image_local_path>` 标签发出来。这个标签能把**带空格的路径**
> （`C:\Program Files\…`）整段框住，通用路径正则遇到空格就断了。
> Windows 盘符路径走独立分支，且**必须排在 POSIX 分支之前**：否则 `C:/x/a.png` 会被
> 从 `/` 处截成 `/x/a.png`，解析到当前盘符下，表现为"有时能识别、有时不能"。

**相对路径怎么定位**：优先从 DSH 注入的运行时上下文里读会话 workspace
（`…session workspace: "…"`），读不到再用同段的 `Current working directory: "…"`，
最后才是配置项 `workspace_root`。

> **运行时上下文必须按行剔干净**：DSH 每轮会把「运行时上下文 + 技能目录 + 时间上下文」
> 作为额外的 user 消息追加在提问之后。老实现只删 runtime-context 首段和两条 policy 行，
> 剩下的 `Current working directory:` / 检出路径 / GUI 地址，以及整条时间上下文
> （`Time sampled while preparing turn 1…`）都还在，于是**最后一条消息被当成当前提问**，
> 真正的提问和 `@图片` 一起被塞进历史块 —— 图片自然永远识别不到。
> 现在 `_clean_user_text` 按行前缀剔除全部噪音行，提问选择才稳定。

**附上图片后会给模型一句明确交代**：`[图片已附上] …不要用 read / Get-Content / PowerShell
去读取 .jpg/.png 文件……`。没有这句时，模型仍会照着 `@路径` 的指引去读二进制，
撞上 `binary file` 错误后倒推出「我看不了图片」——这是实测最容易复发的失败形态。

**安全闸门**：扩展名初筛 + **文件头魔数校验**（把别的文件改名成 `.png` 也会被挡）+
单张大小上限 + 单请求张数上限；可选 `local_image_restrict_to_workspace` 把图片限制在
workspace 内（多租户/共享部署建议开启）。任何一项不过就只是不附这张图，不影响对话。

**前提**：上游 AFS 必须实现图片上传（`[[local-image:…]]` → `/v0/file/upload_file` →
`ref_file_ids`），该能力自 **AFS v1.7.0** 起提供（DeepSeek / GLM 两条通道都已接入）。
AFS 未实现时，标记会被当成普通文本发给 DeepSeek，行为等同于改造前。

**AFS 侧实现要点**（踩过的坑，改 AFS 时务必照做）：

1. 完整顺序：建会话 → 取 PoW → 上传 → **轮询文件就绪** → completion
2. 上传的 PoW 挑战必须用 `target_path = '/api/v0/file/upload_file'` **单独申请**
3. completion 的 PoW 挑战要在**上传之后**申请（上传会申请新挑战，让先前的失效）
4. **必须声明 `vision`**：上传请求头 `x-model-type: vision`，且 completion 的
   `model_type: 'vision'`。DeepSeek 的上传类型只有 `default`/`expert`/`vision`，
   带图不写 `vision` 会退回纯文本语义 —— 表现是**模型对图片返回空内容，
   而上游既不报错也不解释**（极难排查）
5. **上传后必须轮询到文件就绪再引用**：`upload_file` 返回的
   `data.biz_data.status` 初始是 `PENDING`，此时引用会被判
   `biz_code 9 / invalid ref file id`。要用 `GET /api/v0/file/fetch_files?file_ids=<id>`
   轮询到状态离开 `PENDING`（通常 1-3 秒）再放进 `ref_file_ids`
6. `biz_code 9` 会**污染整个会话**（之后该会话每一轮都失败）→ 命中时要清掉会话缓存换新会话
7. 文件名后缀必须是受支持格式（png/jpg/jpeg/webp/gif）——服务端按**文件名后缀**判类型，
   multipart 的 content-type 不作数
8. 标记整行必须从 prompt 里剥掉，本地绝对路径不能进正文
9. 上传失败要退化成纯文本，不得阻断对话

## DeepSeek DSML 全角竖线陷阱（关键根因）

长期困扰的「**偶发**解析失败、原始 XML 泄漏成回答正文」的真正根因：

DeepSeek 网页端有时不按提示输出 `<tool_calls><invoke name="x">`，而是输出它
**原生的 DSML 标记**，并且竖线常常是**全角 `｜`(U+FF5C)**、前缀后还带空格：

```
<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="web_search"><parameter name="query">…</parameter></web_search></tool_calls>
```

十六进制 `3c ef bd 9c ef bd 9c 44 53 4d 4c` —— 肉眼与 ASCII `|` 完全一样，
终端、日志、复制粘贴都看不出来，但正则永远匹配不上。加上闭合标签还经常退化成
`</web_search>`（而非 `</invoke>`），于是每次都"看起来格式没问题却解析不出来"。

**修复**：解析前先做字符归一化（全角竖线/引号/空格 → ASCII，零宽字符删除），
并把 `<||DSML||`、`<|CHAT2API|` 前缀直接剥掉，让下游只面对最朴素的 XML 形态；
再配合 4 层递进解析（标准 → 残缺 `<invoke>` → 仅剩闭标签 → 裸工具标签）。

回归套件 `test_parser.py` 固化了这个真实捕获用例（165 项断言，含全部格式变体 +
对话历史 + 防编造检测 + 本地图片引用 + 网关代搜 + 平台预设/日志路径）；改动解析或精简逻辑后务必先跑它：

```bash
.venv/bin/python test_parser.py              # macOS / Linux
.venv\Scripts\python.exe test_parser.py      :: Windows
```

## 文件

| 文件 | 说明 |
|---|---|
| `gateway_core.py` | 核心：配置、精简、归一化、XML 解析、重试、上游转发、代搜调度 |
| `server_search.py` | 网关代搜：必应搜索（RSS/HTML 双源）+ 查询改写 + 网页正文抽取 |
| `server.py` | Flask OpenAI 兼容端点 (8001) |
| `launcher.py` | Tkinter 启动器（打包成 `dist\ds-trim-gateway.exe`） |
| `ui.py` | Gradio 配置界面 (7860)，开发调试用 |
| `test_parser.py` | 解析回归测试（含真实 DSML 捕获用例） |
| `build_release.py` | 打绿色分发包：exe + 预置 config.json + AFS 便携版 + 说明 → `dist/分发包.zip` |
| `package_docs/` | 分发包里的说明（先看这里 / 客户端接入 / 扩展工具与提示词 / 网关使用说明） |
| `start.sh` | 一键启动脚本（macOS / Linux） |
| `start.bat` | 一键启动脚本（Windows，等价 `start.sh`，日志走 `%TEMP%`） |
| `requirements.txt` | 依赖清单 |
| `config.json` | 运行时配置（首次启动按默认预设生成；分发时必须与 exe 成对，见上文「发给别人用」） |
| `交接部署说明.md` | 交接给接收方的部署步骤（含 Windows 命令） |
| `build_release.py` | 打包脚本（发送方用，不随包分发） |
| `fix_venv_paths.py` | **项目目录搬家/改名后**修复 `.venv` 内写死的旧绝对路径（开发维护用，不随包分发） |

### 项目目录搬家后要做什么

venv 会把**创建时**的绝对路径写进 `.venv/bin/*` 的 shebang 和 `activate` 脚本，
目录一改就失效（症状：`.venv/bin/pip -V` 报 `bad interpreter`）。
`.venv/bin/python` 本身是符号链接，不受影响，所以 `./start.sh` 通常还能跑。

```bash
.venv/bin/python fix_venv_paths.py --dry-run   # 先看会改哪些文件
.venv/bin/python fix_venv_paths.py             # 实际修复，并自动跑 pip 自检
```

脚本只重写路径中带 `.venv` 的那一段（`pyvenv.cfg` 的 `home =` 指向系统解释器，不动），
并跳过 macOS 写在 `~/Library/Caches/com.apple.python/` 下带 `../` 前缀的相对路径。
也可以直接删掉 `.venv` 重建：`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`。

`build_release.py` 的路径基于 `__file__` 推导（输出到项目目录上一级），换目录无需改动；
要指定别处输出可用 `python build_release.py /path/to/out.zip`。

## 启动器窗口显示不全（底部按钮看不见）

启动器现在按 **Windows 工作区**（已扣掉任务栏）自适应尺寸与位置：宽 `min(900, 工作区宽-40)`，
高 `min(780, 工作区高-90)`，窗口整体居中偏上。

- 底部操作条（重新载入 / 保存配置 / 启动网关 / 停止 / 打开日志 / 打开配置目录 /
  打开 config.json）**先于页签布局**（`side='bottom'`），窗口再矮也是按钮优先保留，
  被挤压的是页签内容。
- 「上下文精简」页控件最多，放不下时**右侧出现纵向滚动条**，滚轮可查看全部参数。

历史问题：2026-10-30 之前的 exe 写死 `900x760`。在 150% 缩放（逻辑屏仅 1280x720）的
笔记本上，窗口底边会落到任务栏下面，整条操作栏被遮住，看起来就像"启动网关/保存配置
不见了"。换用 2026-10-30 之后的 exe 即可。

## 已知限制

- 上游返回**真正的**纯文本回答（模型判断无需工具）时直接透传（finish=stop）。
  但"看起来像工具调用却解析失败"的文本不会被透传，见「关键设计 3」。
- **流式已支持**：`stream=true` 时网关把完整结果包装成 OpenAI 兼容 SSE 流
  （单个内容 chunk + finish_reason + `[DONE]`），DSH 等客户端可正常解析。
- 网关自身不执行工具：tool_calls 返回给 DSH 执行。**多轮协作已支持**——下一轮
  请求中若带 assistant tool_calls + tool 结果历史，网关会提取最近几轮执行结果
  合并进新提问；此前的「用户提问 + 助手回答」也会作为对话历史一并带上。
- **图片分三种情况**，别混为一谈：
  - **本地文件**（DSH 里 `@文件名`）→ 走上文「本地图片」通道，网关解析成绝对路径并
    注入标记，**模型能真正看到图**。
  - **base64 图片块**（`image_url` + `data:` URI）→ 网关把 base64 解码落盘到
    `%TEMP%\ds-gw-inline-img\`，再走同一条本地图片通道（`_spill_inline_images`），
    **模型同样能看到图**。
  - **远程图片 URL**（`image_url` 指向 http(s)）→ 不下载，只保留文字；需要时让模型
    用 WebFetch 自己取。理由：多一次网络请求就多一个失败面，且无法做魔数校验。
- **上游模型是固定的**：网关只用配置里的 `upstream_model` 调上游，**忽略请求体里的
  `model`**（见 `call_upstream()`）；而返回给客户端的响应里回填的却是**请求时的** model，
  所以客户端看不出差异。后果：DSH 里配多个模型 id 不会报错，但**只有配置里那一个真正生效**，
  其余都是静默替换。要做多模型入口需改 `call_upstream()` 的取值为
  `body.get('model', cfg.get('upstream_model'))`，但注意 system 裁剪与工具注入是为
  DeepSeek 调的、12 秒限流也是针对它风控的，透传前要一并评估。
- **`upstream_model` 必须与 AFS 的模型 id 逐字一致**（大小写敏感）：AFS 1.7.0 支持的
  id 见 `curl http://127.0.0.1:8080/v1/models`，例如 `DeepSeek-V4.1-Flash`、
  `deepseek-v4-pro`。写成列表里没有的名字（如 `deepseek-v4-flash`）时，AFS 返回
  **HTTP 503 `No available account for model: xxx`**，容易被误判成"AFS 没登录账号"，
  实际只是模型名对不上。
- **对话历史与工具历史都不按话题切分**：只按「最近 N 条 / N 轮」取。换话题后，
  上一个话题的工具结果仍可能被带进提示（既污染语义，也挤占字符预算）。
  追问跨度大时可先关掉「带上最近几轮对话历史」，或调小条数与预算。
- 工具历史单条结果截断到 `history_result_chars`（默认 8000）；很长的网页正文
  若把答案落在截断点之后，模型就看不到了。
- 当前日期由网关注入（`inject_current_date`，见「关键设计」）。关掉它以后模型只能
  靠搜索到的内容推断时间，容易给出过期数据——那种情况下才需要在 system 提示里自写日期。
- 工具列表是 Gradio 里手动维护的精简列表（默认 7 个），不跟随 DSH 发送的 tools。
  这也意味着上游请求里**没有** `tools` 字段，因此即使 AFS 没打 managedXml 补丁
  （v1.5.8 原版），它也会进入 disabled 模式、不注入任何内容，直接透传文本。
- 防编造检测依赖 DSH 两种工具的输出形态标记（`read_page` 含 `Content:`、
  `web_search` 含 `ranked result(s)`/`Sources:`）。换用别的 web_search/read_page
  实现时需要同步调整 `_CONTENT_MARKERS` / `_SOURCE_MARKERS`。
