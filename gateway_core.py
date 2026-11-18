"""
ds-trim-gateway — DeepSeek 网页端精简代理

背景：DSH 经 AFS 转发给 DeepSeek 网页端的 system 消息可高达 ~60KB
（裁剪后的 system + 58 个工具的完整 JSON schema ≈ 58KB）。DeepSeek 网页端
对这种超长 system + 大量工具 schema 会触发风控"禁言"；而精简到只保留
「用户提问 + 精简工具列表 + 精简 system」时不触发（已实测对照）。

本程序作为 DSH 与 AFS 之间的精简层：
    DSH → ds-trim-gateway (默认 8001) → AFS (8080) → DeepSeek 网页端

- Gradio Web UI 维护「精简工具列表 + system 提示词 + 上游配置」
- OpenAI 兼容端点 /v1/chat/completions 接收 DSH 请求，精简后转发，
  解析上游返回的 XML 工具调用为 OpenAI tool_calls 返回给 DSH 执行。

作者：为用户自用搭建。配置持久化在 config.json。
"""
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid

import requests

# ═══════════════════════════════════════════════════════
# 路径与默认配置
# ═══════════════════════════════════════════════════════
# 打包成单文件 exe 后，__file__ 指向 PyInstaller 解压出来的临时目录（_MEIPASS），
# 在那儿读写 config.json 会在退出时被清掉。冻结态必须用 exe 自身所在目录，
# 用户才能像绿色软件一样「放在哪个文件夹就用哪个文件夹的配置」。
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

# ═══════════════════════════════════════════════════════
# 平台工具预设
# ═══════════════════════════════════════════════════════
# 工具名必须与**客户端实际暴露的工具名**逐字一致，否则网关解析出的调用会被
# 下游当作未知工具丢掉（日志 `忽略未知工具名: xxx`），白白浪费一轮请求。
#
# 实测客户端（WorkBuddy / DSH 共用同一套 harness）暴露的名字：
#   Windows：Bash、PowerShell、Read、Glob、WebSearch、WebFetch
#   macOS  ：Bash、Read、Glob、WebSearch、WebFetch（没有 PowerShell）
# 注意 Windows 上的 `Bash` 是 Git Bash（POSIX 语法），`PowerShell` 才是
# PowerShell 语法；本机是 Windows，所以预设指向 `PowerShell`。
#
# 历史坑：早期预设写 `pwsh` / `read_page`，这两个名字客户端里都不存在
# （resolve_name 返回 None），导致 shell 与网页正文读取永远调不通。
# 现在改用客户端真名，并保留 _TOOL_ALIAS_GROUPS 兼容旧名与模型口误。
#
# 预设不只是换工具名：描述与 system 提示里的命令语法也要跟着变，否则模型会在
# Windows 上生成 `ls`/`cat` 这类 POSIX 命令，用户看到的就是"工具调用失败"。

IS_WINDOWS = os.name == 'nt'

# 日志/落盘目录：POSIX 固定用 /tmp（与既有文档、排查流程一致，不要改成
# tempfile.gettempdir()——macOS GUI 会话下它是 /var/folders/...，会让文档失效）；
# Windows 没有 /tmp，用系统的临时目录（通常是 %TEMP%），否则写日志会静默失败。
_TMP_DIR = tempfile.gettempdir() if IS_WINDOWS else '/tmp'

# 两套 system 提示共用的主体（实际是同一份，按客户端替换工具名）
_SYS_HEAD = (
    "你是工具调用大师，你不直接操作，但你会根据用户的问题生成XML格式的工具调用指令。"
)

# {search} / {fetch} 会替换成**当前客户端真实的工具名**。写死名字会让模型
# 照着提示词去调一个客户端根本没有的工具，白跑一轮。
_SYS_RULES = (
    "当需要调用工具时，只输出 XML 工具调用块，不要输出任何解释文字。"
    "最重要的一点：一旦你已经获得回答用户问题所需的数据，就不要再调用工具，"
    "直接用普通文字给出完整回答——数据够了却继续调工具是最常见的错误。"
    "规则："
    "1) 工具调用失败或超时是正常现象，不要连续重复调用完全相同的工具和参数；"
    "2) 用 {search} 搜索后结果可能只列出来源摘要，需要具体数据时用 {fetch} 打开来源 URL"
    "（把要提取的字段写清楚，例如「重庆今天的气温、天气现象、湿度」）；"
    "3) 不要反复用不同关键词重搜同一个问题，也不要重复读取已经读过的 URL；"
    "4) 先看[此前已执行的工具结果]，里面已有的数据直接使用，不要重新获取；"
    "5) 绝对禁止编造数据：只有当工具结果里明确出现过某个数字时才能引用它。"
    "如果工具结果只有来源链接、没有具体数值，必须用 {fetch} 打开来源把数据取回来；"
    "确实取不到就如实说明未获取到——编造数字比回答不出来严重得多。"
)

# 目标平台只有 Windows（已不再区分 macOS）：WorkBuddy 与 DSH 的 shell 工具
# 都跑 PowerShell 语法，真正的差别只在**工具名**和搜索/读页的参数面。
_WINDOWS_SHELL_RULE = (
    "6) 本机是 Windows：shell 工具是 {shell}，命令必须用 PowerShell 语法"
    "（列目录用 Get-ChildItem、看文件用 Get-Content、搜索用 Select-String，"
    "不要用 ls / cat / grep / find 这类 POSIX 命令），"
    "路径用原生 Windows 形式（如 C:\\Users\\name\\Documents，反斜杠），"
    "环境变量用 $env:NAME。"
    "命令会碰到中文路径或中文输出时，先在开头加上 "
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8；"
    "不加的话返回给你的中文是乱码，你会误判成文件没生成而反复重做。"
)


# 文档 / PPT 工作流：只讲模型猜不到的那几件事（它默认会用 bash 习惯、
# 会用 Read 硬读 docx、会忘了 PowerPoint 的 SaveAs 格式码）。
_DOC_WORKFLOW_RULE = (
    "7) 写文件用 {write}（整篇覆盖）、改文件用 {edit}（局部替换），"
    "不要用 PowerShell here-string 写长文本——中文、引号、$ 全要转义，几乎必错。"
    "8) 读 .docx 不要用 {read}（它是 zip 二进制）：在 {shell} 里先 Copy-Item 改名成 .zip、"
    "Expand-Archive 解开、再读 word 目录下的 document.xml 去掉标签取正文；"
    "Add-Type 被安全策略拦截，别走 .NET 解压。正文很长时先落盘成 .txt，"
    "再用 {read} 的 offset/limit 分段读，不要一次拉全文。"
    "9) 生成 .pptx：New-Object -ComObject PowerPoint.Application，"
    "Slides.Add(序号, 2) 是标题+正文版式，SaveAs 要传绝对路径且格式参数 24，"
    "最后 Close 再 Quit，否则进程残留。"
)


def _windows_system_prompt(shell, search, fetch, write=None, edit=None, read=None):
    """按客户端实际工具名生成 Windows system 提示。

    write/edit/read 传了才追加 7-9 条（文档与 PPT 工作流）。网关**不把 tools schema
    发上游**，模型只能照这里的名字调工具；不给它这段说明，它就会去用 PowerShell
    写长文本、或者用 {read} 硬读 .docx，白跑十几轮。
    """
    body = (_SYS_HEAD
            + _SYS_RULES.format(search=search, fetch=fetch)
            + _WINDOWS_SHELL_RULE.format(shell=shell))
    if write and edit:
        body += '\n' + _DOC_WORKFLOW_RULE.format(shell=shell, write=write, edit=edit,
                                                 read=read or write)
    return body

# ── 工具定义：按客户端分开，名字与参数面都照客户端真实 schema 来 ───────────────
#
# 踩过的坑（2026-10-23）：同一台 Windows 上，WorkBuddy 与 DSH 暴露的工具名不同，
# 用错一套就是「工具调用永远失败」，而且现象很像网络问题：
#
#   能力        WorkBuddy          DSH(deepseek-harness)
#   ----------  -----------------  ---------------------
#   shell       PowerShell         pwsh
#   读文件      Read(file_path)    read(file_path)
#   找文件      Glob(pattern)      glob(pattern)
#   搜索        WebSearch(query)   web_search(queries[])
#   读网页      WebFetch(url,prompt) web_fetch(url)
#
# 名字对不上会被客户端当未知工具直接报错；名字对上但参数面不对，会在 schema
# 校验阶段被拒（同样不发起真实请求）。两种情况都是一轮空转。

_SHELL_DESC = ("Execute a PowerShell command and return its stdout/stderr. "
               "Use PowerShell cmdlets (Get-ChildItem to list files, Get-Content to read a file, "
               "Select-String to search); paths are native Windows paths like C:\\Users\\name, "
               "and environment variables are $env:NAME.")


def _shell_tool(name):
    return {
        "name": name,
        "description": _SHELL_DESC,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The PowerShell command to execute."},
                "description": {"type": "string",
                                "description": "Clear, concise description of what this command does "
                                               "in active voice, 5-10 words (shown in the UI)."},
            },
            "required": ["command", "description"],
        },
    }


def _file_tools(read, glob):
    """读文件 + 找文件；两个客户端只有大小写/下划线差别，参数面一致。"""
    return [
        {
            "name": read,
            "description": "Read a UTF-8 text file and return line-numbered content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the file"},
                    "offset": {"type": "integer", "description": "Line number to start reading from"},
                    "limit": {"type": "integer", "description": "Maximum number of lines to read"},
                },
                "required": ["file_path"],
            },
        },
        {
            "name": glob,
            "description": "Find files whose paths match a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. src/**/*.py"},
                    "path": {"type": "string", "description": "Directory to search in"},
                },
                "required": ["pattern"],
            },
        },
    ]


def _edit_tools(write, edit):
    """写文件 + 改文件。两个客户端的参数面完全一致（file_path/content、
    file_path/old_string/new_string），只有大小写差别，所以共用一份定义。

    为什么必须有：网关不把 tools schema 发上游，模型只能照 system 提示里的名字调。
    没有 write/edit 时，模型只能用 PowerShell here-string 落盘长文本 ——
    中文、引号、`$`、换行全要转义，几乎必然出错。
    """
    return [
        {
            "name": write,
            "description": "Create or fully replace a UTF-8 text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string",
                                  "description": "Absolute path to write. Provide file_path before content."},
                    "content": {"type": "string",
                                "description": "Full UTF-8 text content to write."},
                },
                "required": ["file_path", "content"],
            },
        },
        {
            "name": edit,
            "description": "Edit an existing UTF-8 text file by replacing literal text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string",
                                  "description": "Absolute path to edit. Provide file_path before old_string."},
                    "old_string": {"type": "string", "description": "Literal text to replace."},
                    "new_string": {"type": "string",
                                   "description": "Literal replacement text. Empty string deletes the match."},
                    "replace_all": {"type": "boolean",
                                    "description": "Replace all matches. Defaults to false."},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    ]


# WorkBuddy：搜索收单个 query 字符串（另支持 freshness）、读页要多给一个 prompt
_WB_TOOLS = (
    [_shell_tool("PowerShell")]
    + _file_tools("Read", "Glob")
    + _edit_tools("Write", "Edit")
    + [
        {
            "name": "WebSearch",
            "description": "Search the web for current information. The result lists sources; "
                           "use WebFetch to open a source URL for full content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Search query. Keep entity names, versions, dates and terms as-is."},
                    "freshness": {"type": "string",
                                  "description": "Optional time filter: d1 (today) / w1 (this week) / m1 (this month)"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "WebFetch",
            "description": "Fetch one web page and return its content. Use it to open a search-result "
                           "source URL when you need the full content of a page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The http(s) URL to read"},
                    "prompt": {"type": "string",
                               "description": "What to extract from the page, e.g. temperature, "
                                              "weather condition, humidity"},
                },
                "required": ["url", "prompt"],
            },
        },
    ]
)

# DSH：搜索收 queries 数组（1-4 条合并），读页只要 url
_DSH_TOOLS = (
    [_shell_tool("pwsh")]
    + _file_tools("read", "glob")
    + _edit_tools("write", "edit")
    + [
        {
            "name": "web_search",
            "description": "Search the web for current information. Provide 1-4 queries; their results "
                           "are merged. The result lists source URLs; use web_fetch to open one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {"type": "array", "items": {"type": "string"},
                                "description": "1-4 search queries; their results are merged."},
                },
                "required": ["queries"],
            },
        },
        {
            "name": "web_fetch",
            "description": "Fetch the content of a specific HTTP(S) URL and return it decoded to text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The HTTP(S) URL to fetch."},
                },
                "required": ["url"],
            },
        },
    ]
)

# 工具名别名：模型口误或不同客户端版本对同一能力叫法不同，命中同组即视为同一工具。
# 元素是**规范化名**（小写、去掉下划线/连字符/冒号），由 norm_tool_name() 生成。
_TOOL_ALIAS_GROUPS = (
    # shell：DSH 的 pwsh、WorkBuddy 的 PowerShell、POSIX 的 bash 互为别名
    {'bash', 'sh', 'shell', 'zsh', 'pwsh', 'powershell', 'terminal', 'cmd'},
    # 网页正文读取：read_page ↔ WebFetch
    {'readpage', 'webfetch', 'fetch', 'fetchurl', 'openpage', 'webread', 'urlread'},
    # 联网搜索：web_search ↔ WebSearch
    {'websearch', 'search', 'searchweb', 'searcher'},
    # 文件读取
    {'read', 'readfile', 'openfile'},
    # 写文件 / 改文件
    {'write', 'writefile', 'createfile', 'savefile'},
    {'edit', 'editfile', 'strreplace', 'stringreplace', 'replaceinfile'},
    # 路径匹配
    {'glob', 'findfiles', 'filesearch', 'listfiles'},
)


def norm_tool_name(name):
    """工具名归一化：小写 + 去掉下划线/连字符/冒号，用于容错比对。"""
    return str(name or '').lower().replace('_', '').replace('-', '').replace(':', '')


def _alias_group(norm):
    for group in _TOOL_ALIAS_GROUPS:
        if norm in group:
            return group
    return None

# 预设表：键为 UI 下拉里显示的名字（**只保留 Windows**，不再区分 macOS）。
# 真正要选的是「你用哪个客户端」——两者的工具名与参数面不通用。
TOOL_PRESETS = {
    'WorkBuddy（Windows）': {
        'client': 'workbuddy',
        'tools': _WB_TOOLS,
        'system_prompt': _windows_system_prompt('PowerShell', 'WebSearch', 'WebFetch',
                                                write='Write', edit='Edit', read='Read'),
        'tool_summary': 'PowerShell / Read / Write / Edit / Glob / WebSearch / WebFetch',
    },
    'DSH（Windows）': {
        'client': 'dsh',
        'tools': _DSH_TOOLS,
        'system_prompt': _windows_system_prompt('pwsh', 'web_search', 'web_fetch',
                                                write='write', edit='edit', read='read'),
        'tool_summary': 'pwsh / read / write / edit / glob / web_search / web_fetch',
    },
}
# 默认选 WorkBuddy：本项目的 models.json 场景就是它；DSH 用户在界面里一键切换。
DEFAULT_PRESET_NAME = 'WorkBuddy（Windows）'


def default_tools():
    return json.loads(json.dumps(TOOL_PRESETS[DEFAULT_PRESET_NAME]['tools']))


def default_system_prompt():
    return TOOL_PRESETS[DEFAULT_PRESET_NAME]['system_prompt']


def detect_preset_name(tools):
    """根据工具列表猜出它属于哪个客户端预设（用于 UI 回显），猜不出返回 None。

    判据是 shell 工具名：`PowerShell` → WorkBuddy；`pwsh` / `bash` → DSH。
    兼容极旧配置里的小写 `bash` / `pwsh`。
    """
    raw = {str(t.get('name') or '') for t in (tools or []) if isinstance(t, dict)}
    if 'PowerShell' in raw:
        return 'WorkBuddy（Windows）'
    names = {norm_tool_name(n) for n in raw}
    if 'pwsh' in names or 'bash' in names:
        return 'DSH（Windows）'
    return None


DEFAULT_CONFIG = {
    # 上游 AFS/chat2api 地址（AFS 再把消息转发给 DeepSeek 网页端）
    "upstream_base": "http://127.0.0.1:8080/v1",
    "upstream_api_key": "",
    "upstream_model": "deepseek-v4-pro",
    # 发给上游时使用的精简 system（可空；空则只发用户提问 + 工具列表）
    # 关键约束：既要让模型输出 XML 工具调用，也要在数据足够时停止调用、直接总结
    # （实测缺后者时模型会一直调工具不收敛，导致请求暴增并触发上游风控）。
    # 默认值按**本机操作系统**自动选择（Windows → PowerShell 版，其余 → Bash 版），
    # 用户可在 Gradio 界面用「工具预设」下拉切换。
    "system_prompt": default_system_prompt(),
    # 精简工具列表（用户手动维护，Gradio 中编辑）。每项含 name/description/参数。
    # 注意：名字与 schema 都必须与客户端真实工具一致，否则客户端按严格 schema
    # 校验会直接拒绝（例如 shell 工具要求 command 和 description 都必填）。
    "tools": default_tools(),
    # 是否丢弃客户端发来的 system（只用自己的精简 system）
    "drop_client_system": True,
    # system 长度上限（超过则截断；0 表示不截断）
    # 2000：文档/PPT 工作流那三条提示词加进来后大约 1000 字，留一倍余量
    "max_system_chars": 2000,
    # 空响应后的冷却等待（毫秒）：DeepSeek 网页端限流窗口约 60 秒，
    # 窗口内重试无效且增权；默认等待 60 秒再重试
    "retry_backoff_ms": 60000,
    # 上游请求最小间隔（毫秒）：DeepSeek 网页端约 >10 次/分钟触发风控禁言，
    # 强制两次请求间隔 ≥12 秒（≈5 次/分钟）以规避；0 表示不限流
    "min_request_interval_ms": 12000,
    # 单轮最多交付给客户端多少个工具调用（0 = 不限制，按模型给的原始数量）。
    # 为什么默认 1：客户端（WorkBuddy）会把一条消息里的多个 tool_calls 当成
    # **一个名字**去校验——['PowerShell','PowerShell'] 报
    # "PowerShellPowerShell does not exist"，['Read','Glob'] 报 "ReadGlob"。
    # 实测一次 63 次调用的会话里有 11 次是这种同名重复，每次都白跑一轮
    # （报错 → 客户端 error-recovery → 模型改成串行重发）。
    # 在网关交付前裁成 1 个，把「报错后再重试」变成「静默串行」，省掉红错和一轮请求。
    "max_tool_calls_per_round": 1,
    # 单个会话最多转发多少次上游请求（0 = 不限制）。
    # 为什么需要：客户端只在"模型不再发工具调用"时才结束一轮，它不数步数。
    # 遇到「检索-写作 + 硬判据」类任务（如"找 3 条官网原文，不足则少给"），
    # 模型会把"没凑够"读成"继续找"，于是每 12 秒一轮、可以连跑上百轮——
    # 实测一次 24 分钟 119 轮 0 正文，最后把账号打到上游风控。
    # 这里在"转发"之前硬性截断：第 limit+1 次请求不再发给上游，直接返回收尾文本，
    # 让客户端把这轮当正常回答结束，而不是当错误重试。
    # 计数取「messages 里的工具轮数」与「全局会话计数」的较大者：后者防止客户端
    # 自动压缩历史后 messages 归零导致上限失效，前者保证进程重启后仍能识别进度。
    "max_total_rounds": 20,
    # 工具历史累积：保留最近 N 轮工具调用与结果（避免模型"失忆"反复重搜）
    "history_max_rounds": 3,
    # 单条工具结果截断字符数（网页正文较长，太小会丢掉有效数据）
    # 8000 ≈ 一次 Read 读 200 行中文——读 Word 正文要靠分段读，3000 根本装不下一页
    "history_result_chars": 8000,
    # 工具历史总字符预算（超出则从最早一轮丢弃）
    # 24000 ≈ 三次满额读；再大就容易触发上游长上下文风控
    "history_total_chars": 24000,
    # 对话历史：把最近若干轮「用户提问 + 助手回答」一并带给上游模型。
    # 为什么需要：只发最后一条提问会让带指代的追问（"那深证成指呢？"、
    # "把刚才那个结果整理成表格"）失去先行词——模型既看不到自己上一条回答，
    # 也不知道用户此前问过什么，只能重新取数。
    "history_include_dialogue": True,
    # 保留最近 N 条对话消息（user + assistant 合计；0 表示不限）
    "history_dialogue_msgs": 6,
    # 单条对话消息截断字符数（长回答只留开头，够模型知道"我说过什么"即可）
    "history_dialogue_chars": 800,
    # 对话历史总字符预算（超出则从最早一条丢弃）
    "history_dialogue_total_chars": 3000,
    # 是否在 system 里显式注入当前日期。
    # 为什么默认开：实测上游 DeepSeek 网页端**自己会**注入当天日期（直连 8080 问日期
    # 也能答对），模型还会主动 bash date 兜底——所以缺日期时行为看起来正常。但那是个
    # **静默依赖**：一旦换上游（或换成不注入日期的裸 API），日期会无声退化成"靠模型猜"，
    # 不报错、只答错。显式注入把这个隐式依赖变成可配置的显式行为。
    "inject_current_date": True,
    # ── 本地图片引用（路线 A）──
    # 背景：DSH 的 @ 文件引用只把「文件路径」当文本塞进消息
    # （dsh-at-file 插件生成 <workspace-reference path="…" kind="file" />），
    # 图片字节从未进入请求，模型只看到一个文件名。网关与用户同机，因此把路径
    # 解析成绝对路径并注入标记，交给上游 AFS 读盘上传给 DeepSeek 网页端。
    # 关掉后行为与改造前完全一致。
    "local_image_enabled": True,
    # 会话 workspace 兜底目录：@ 引用多为相对路径，优先从 DSH 运行时上下文里
    # 自动解析（"…session workspace: "…""）；解析不到时用这里的值。
    "workspace_root": "",
    # 允许的图片扩展名（只做初筛，真正的判定看文件头魔数）
    "local_image_exts": [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"],
    # 单张图片字节上限（DeepSeek 网页端上传上限 8MB，留出余量）
    "local_image_max_bytes": 8388608,
    # 一次请求最多附带几张图片
    "local_image_max_count": 4,
    # 是否要求图片必须位于 workspace 内。默认否：@ 可以引用任意本地文件。
    # 多租户/共享部署下建议开启，避免提示词把任意路径的文件带出去。
    "local_image_restrict_to_workspace": False,
    # ── 网关代搜（客户端搜索不可用时的兜底）──
    # 背景：DSH 的 web_search 不在本地执行，而是内置插件直连
    # api.deepseek.com 的 Anthropic 端点（web_search_20250305），用客户端自己
    # 存的 API Key。那把 Key 一失效，搜索固定返回 401，模型重试几次后只能
    # 凭记忆作答——聊天链路走网关+AFF 完全正常，唯独搜索绕开网关。
    # 打开后：模型要 web_search 时由网关自己搜（必应）并抓正文，再把结果
    # 塞回对话。mode: auto=检测到客户端搜索鉴权失败才代搜 / on=总是代搜 / off=关闭
    "server_search_mode": "auto",
    # 一次请求里最多代搜几轮（每轮都要多调一次上游，受 min_request_interval 限流）
    "server_search_max_rounds": 2,
    # 每次最多用几个查询（模型的 queries + 改写变体都算在这里面之外）
    "server_search_max_queries": 2,
    # 搜索结果条数上限
    "server_search_max_results": 5,
    # 抓几个页面的正文（0=只给标题和摘要）
    "server_search_fetch_top": 2,
    # 单个页面正文截断字符
    "server_search_page_chars": 1200,
    # 日志长度
    "log_lines": 200,
}


# ═══════════════════════════════════════════════════════
# 配置读写（线程安全）
# ═══════════════════════════════════════════════════════
_lock = threading.Lock()
_config = dict(DEFAULT_CONFIG)
_log_buf = []  # type: list[str]

# config.json 的 mtime 快照，用来发现"配置被外部改了"（UI 是独立进程，改完只落盘）。
_config_mtime = 0.0

# 配置热重载开关。UI(7860) 与网关(8001) 是**两个独立进程**：UI 保存只写 config.json，
# 不会通知网关。默认开启 mtime 检查，让配置页保存后**立即生效**（无需重启网关）；
# 置 0 可关闭（例如多实例共享同一份 config.json 时想自行控制重载时机）。
_ENABLE_CONFIG_HOT_RELOAD = os.environ.get('DS_GW_CONFIG_HOT_RELOAD', '1') != '0'

# 上游请求速率限制状态（跨线程共享）
_rate_lock = threading.Lock()
_last_request_at = 0.0  # time.monotonic() 时间戳；0 表示尚未发过请求

# ── 单会话轮次计数（跨请求，对应 max_total_rounds）──
# 网关本身是无状态的：一次 HTTP 请求就是一轮，它不知道"这个任务已经跑了多少轮"。
# 两种来源互补取值：
#   1) messages 里 assistant.tool_calls 的条数 —— 客户端回传的完整历史，最准；
#      但客户端满载时会把早期消息压缩成摘要，轮数会突然归零。
#   2) 进程内全局计数 —— 按会话指纹累加，压缩不会让它回退，重发也会继续上涨；
#      但网关重启后丢失。
# 所以 st['n'] 取 max(全局自增, messages 轮数) —— 任一来源有效即可拦住失控循环。
_round_lock = threading.Lock()
_round_state = {}          # type: dict[str, dict]  # 会话指纹 -> {'n': int, 'ts': float}
_ROUND_IDLE_SEC = 1800     # 30 分钟没有新请求即视为新会话
_ROUND_MAX_TRACKED = 500   # 最多同时跟踪 500 个会话，超出先清理过期的


def _round_fingerprint(messages):
    """用第一条非 system 消息做会话指纹：同一任务的多轮请求指纹不变。"""
    if not isinstance(messages, list):
        messages = []
    for m in messages:
        if not isinstance(m, dict) or m.get('role') == 'system':
            continue
        content = m.get('content')
        if isinstance(content, (list, dict)):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        raw = f"{m.get('role', '')}:{str(content)[:1500]}"
        return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:12]
    return 'unknown'


def session_round_usage(messages, cfg=None):
    """只读查询：当前会话已用多少轮（不计数、不改变状态），供日志/UI 展示。"""
    cfg = cfg or get_config()
    if not isinstance(messages, list):
        messages = []
    from_msgs = sum(1 for m in messages
                    if isinstance(m, dict) and m.get('role') == 'assistant' and m.get('tool_calls'))
    key = _round_fingerprint(messages)
    with _round_lock:
        st = _round_state.get(key)
        n = st['n'] if st else 0
    return max(n, from_msgs)


def _bump_session_round(messages, cfg):
    """记一次请求，返回 (本次是第几轮, 是否已超上限)。上限 0 表示不限。"""
    limit = int(cfg.get('max_total_rounds', 0) or 0)
    if not isinstance(messages, list):
        messages = []
    from_msgs = sum(1 for m in messages
                    if isinstance(m, dict) and m.get('role') == 'assistant' and m.get('tool_calls'))
    key = _round_fingerprint(messages)
    now = time.time()
    with _round_lock:
        if len(_round_state) > _ROUND_MAX_TRACKED:
            for k, v in list(_round_state.items()):
                if now - v['ts'] > _ROUND_IDLE_SEC:
                    _round_state.pop(k, None)
        st = _round_state.get(key)
        if st is None or now - st['ts'] > _ROUND_IDLE_SEC:
            st = {'n': 0, 'ts': now}
            _round_state[key] = st
        # 两种来源取大者：压缩历史不会回退，客户端重发也会继续累加
        st['n'] = max(st['n'] + 1, from_msgs + 1)
        st['ts'] = now
        n = st['n']
    return n, (limit > 0 and n > limit)


def _deep_merge(base, override):
    """合并配置：dict 递归，其它直接覆盖。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _config_file_mtime():
    """config.json 的 mtime；取不到（不存在/无权限）返回 None。"""
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return None


def load_config():
    """把 config.json 读进内存。

    就地更新 `_config`（不重新绑定），这样已经在用 get_config() 的调用方
    不会被旧引用困住。读失败时保留当前配置，只用日志提示，不抛异常。
    """
    global _config_mtime
    if not os.path.exists(CONFIG_PATH):
        _config_mtime = 0.0
        return _config
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            saved = json.load(f)
    except Exception as e:  # noqa: BLE001
        log(f"config.json 读取失败，保留当前配置: {e}")
        return _config
    merged = _deep_merge(DEFAULT_CONFIG, saved)
    _config.clear()
    _config.update(merged)
    _config_mtime = _config_file_mtime() or 0.0
    return _config


def save_config():
    global _config_mtime
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(_config, f, ensure_ascii=False, indent=2)
    # 记下自己写入后的 mtime，免得下次 get_config() 把自己写的当成"外部改动"再读一遍
    _config_mtime = _config_file_mtime() or 0.0


def _reload_config_if_changed():
    """config.json 被外部改动过就重载。

    为什么需要：UI(7860) 与网关(8001) 是两个**独立进程**，UI 保存只写文件、不通知
    本进程；没有这层检查的话，在配置页改完必须手动重启网关才生效——表现为
    「改了却不生效」的静默不一致。读失败时保留旧配置（不因为一次坏文件就崩掉）。
    """
    if not _ENABLE_CONFIG_HOT_RELOAD:
        return
    mtime = _config_file_mtime()
    if mtime is None or mtime == _config_mtime:
        return
    with _lock:
        # 双检：并发请求可能同时发现变更
        if _config_file_mtime() == _config_mtime:
            return
        load_config()
    log("检测到 config.json 被外部修改，已热重载")


def get_config():
    _reload_config_if_changed()
    with _lock:
        return json.loads(json.dumps(_config))


def update_config(patch):
    with _lock:
        merged = _deep_merge(_config, patch)
        _config.clear()
        _config.update(merged)
        save_config()
    return _config


# 日志文件：server 与 UI 是独立进程，各自的内存 _log_buf 不互通。
# 统一写入日志文件，get_logs() 从文件读，保证 UI 能看到 server 的日志。
LOG_FILE = os.environ.get('DS_GW_LOG_FILE', os.path.join(_TMP_DIR, 'ds-gw-server.log'))
# 解析失败的上游原文落盘（用于定位模型新出现的 XML 格式变体）
PARSE_FAIL_FILE = os.environ.get('DS_GW_PARSEFAIL_FILE',
                                 os.path.join(_TMP_DIR, 'ds-gw-parsefail.log'))


def log(msg):
    ts = time.strftime('%H:%M:%S')
    line = f"[{ts}] {msg}"
    with _lock:
        _log_buf.append(line)
        if len(_log_buf) > _config.get('log_lines', 200):
            del _log_buf[: len(_log_buf) - _config.get('log_lines', 200)]
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        # 父进程关掉管道后 print 可能抛 BrokenPipeError；日志已进文件，不必炸
        pass
    # 追加到共享日志文件（供 UI 读取）
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:  # noqa: BLE001
        pass


def get_logs(limit=None):
    """返回日志：优先从共享日志文件读（UI 进程能看到 server 的日志）。"""
    lines = []
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            lines = [l.rstrip('\n') for l in f.readlines()]
    except Exception:  # noqa: BLE001
        with _lock:
            return list(_log_buf)
    limit = limit or _config.get('log_lines', 200)
    return lines[-limit:]


# ═══════════════════════════════════════════════════════
# 消息精简
# ═══════════════════════════════════════════════════════
# 中文星期名自己映射，不用 strftime('%A')：Python 默认是 C locale，
# %A 会输出英文 "Wednesday"；要拿到中文得 setlocale，那又会污染进程全局状态。
_WEEKDAYS_CN = ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')


def current_date_text(now=None):
    """生成「当前日期」提示行。

    必须在**每次调用时**实时求值，绝不能写成模块级常量——那样进程一跑起来
    日期就被冻结，跨天不更新（网关是常驻进程，可能连跑数天）。
    """
    now = now or datetime.datetime.now()
    return (f"【当前日期】今天是 {now.year}年{now.month}月{now.day}日 "
            f"{_WEEKDAYS_CN[now.weekday()]}。"
            f"涉及「今天/今日/最新/现在」的问题，一律以此日期为准，不要凭记忆推测。")


def _text_of(content):
    """OpenAI content 可能是 str 或 block 数组 → 取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get('type') == 'text':
                parts.append(c.get('text', ''))
        return '\n'.join(parts)
    return str(content or '')


# 运行时上下文/时间上下文里「纯噪音」的整行前缀。
# 必须列全：这些行若留下来，整条消息清理后仍有内容，就会被当成「当前用户问题」
# ——实测 DSH 真实请求里最后一条 user 是时间上下文，于是当前提问变成
# 「Time sampled while preparing turn 1…」，真正的提问和 @图片 一起掉进历史块，
# 图片永远识别不到（模型也就只能去 read 一个 jpg）。
_NOISE_LINE_PREFIXES = (
    'Current runtime context.',
    'Current working directory:',
    'Current DSH file policy:',
    'Approval policy:',
    'Current DeepSeek Harness implementation checkout:',
    'Current DeepSeek Harness Web GUI URL:',
    'Time sampled while preparing',
    'Browser time zone for this request:',
    'Elapsed since the preceding model-visible message:',
)


def _clean_user_text(text):
    """清理 user 消息里的 DSH 噪音，只保留真正的提问。

    去掉：
    - <system-reminder>...</system-reminder> 技能目录块
    - 运行时上下文整段（工作目录 / 沙箱策略 / 审批策略 / 检出路径 / GUI 地址）
    - 时间上下文整段（采样时间 / 浏览器时区 / 距上条消息的间隔）
    """
    text = text or ''
    # 去 system-reminder 块
    text = re.sub(r'<system-reminder>.*?</system-reminder>', '', text, flags=re.S)
    # 按行剔除噪音：比「整段正则」稳，DSH 各版本的段落顺序与行数会变
    kept = []
    for line in text.split('\n'):
        s = line.strip()
        if s and s.startswith(_NOISE_LINE_PREFIXES):
            continue
        kept.append(line)
    text = '\n'.join(kept)
    # 折叠空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ═══════════════════════════════════════════════════════
# 本地图片引用（路线 A）
# ═══════════════════════════════════════════════════════
# DSH 的 @ 文件引用只把路径当文本发出来，图片字节从未进入请求。网关和用户在同
# 一台机器上，于是把路径解析成绝对路径、注入一个标记，交给上游 AFS 读盘上传给
# DeepSeek 网页端（网页端原生通道：POST /v0/file/upload_file 拿 file_id →
# completion 带 ref_file_ids）。标记在 AFS 侧被消费掉，不会出现在模型看到的正文里。
_LOCAL_IMAGE_MARKER = '[[local-image:{path}]]'

# DSH 的结构化引用：<workspace-reference path="图片.png" kind="file" />
_WS_REF_RE = re.compile(r'<workspace-reference\b[^>]*?\bpath="([^"]+)"[^>]*?/?>', re.I)
# 运行时上下文里的 session workspace —— @ 用相对路径时靠它定位
_SESSION_WS_RE = re.compile(r'session workspace[：:]\s*"([^"]+)"', re.I)
# 兜底：同一段运行时上下文里的「当前工作目录」
_CWD_RE = re.compile(r'current working directory[：:]\s*"([^"]+)"', re.I)
# 客户端的「图片降级标签」：models.json 里 supportsImages=false 时，客户端不传图片
# 字节，只把本地路径降级成一段文本发出来：
#   <image_local_path>C:\Users\a\图片.png</image_local_path>
# 这个标签最可靠——它能把**带空格的路径**（C:\Program Files\…）整段框住，
# 而下面的通用路径正则遇到空格就断了。
_IMAGE_TAG_RE = re.compile(r'<image_local_path>\s*([^<>\n]+?)\s*</image_local_path>', re.I)
# 正文里的 @文件名.png 提及
#
# 为什么不再用「一整个正则把文件名框住」：
# 图片文件名常常是从网页另存/复制来的，里面带逗号、等号、与号，例如
#     @u=2448247082,481968747&fm=253&app=138&f=JPEG.jpg 理解内容
# 任何「排除若干标点」的字符类都会被这个逗号切断（实测该图整轮都没被识别，
# 模型只能用 read 去读 jpg，拿到 binary file 错误后回答"我看不了图片"）。
# 现在改为：先把 @ 后面的整段 token 抓下来（引号形式单独处理），再逐字符剥
# 尾部标点，命中图片扩展名即采纳；是否真是图片交给 _resolve_local_image 的
# 扩展名 + 文件头 + 存在性三重校验去判定，误报不会造成任何副作用。
_AT_MENTION_RE = re.compile(r'(?<![\w@])@(?:"([^"\n]+)"|([^\s]+))')
# 反问/句读标点：@图.png， / @图.png) / @图.png。
_TRAILING_PUNCT = '）)】」』”"\'，,。.;；:：!?！？、'
# 正文里的带路径图片引用
#   POSIX：/abs/a.png、~/a.png、./a.png、../a.png
#   Windows：C:\a.png、D:/a.png（盘符分支必须排在前面，否则 C:/x.png 会被
#            POSIX 分支从 "/" 处截成 /x.png，解析到当前盘符下变味）
_IMAGE_EXT_PAT = r'\.(?:png|jpe?g|webp|gif|bmp)'
_IMAGE_EXT_END_RE = re.compile(_IMAGE_EXT_PAT + r'$', re.I)
_IMAGE_EXT_FIND_RE = re.compile(_IMAGE_EXT_PAT, re.I)
_PATH_IMAGE_RE = re.compile(
    r'('
    r'[A-Za-z]:[\\/][^\s"\'<>|()\[\]]*?' + _IMAGE_EXT_PAT +
    r'|(?:~|\.{1,2})?/[^\s"\'<>|()\[\]]*?' + _IMAGE_EXT_PAT +
    r')', re.I)

# 文件头魔数 —— 只靠扩展名会让「把别的文件改名成 .png」也通过
_IMAGE_MAGIC = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
    (b'BM', 'image/bmp'),
)


def _sniff_image_mime(head):
    """按文件头判断图片类型；不是已知图片返回 ''。"""
    for magic, mime in _IMAGE_MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    return ''


def _session_workspace(messages):
    """取 DSH 运行时上下文里声明的会话 workspace 目录（解析不到则 ''）。

    必须在 _clean_user_text 之前读原始文本 —— 那个函数会把整段运行时上下文删掉。

    两处都能拿到根目录，按可靠度依次尝试：
      1. `…under the session workspace: "C:\\Users\\a\\工作区".`
      2. `Current working directory: "C:\\Users\\a\\工作区".`
    第 2 条是兜底：不同 DSH 版本的运行时上下文措辞不完全一致。路径里可能是
    转义过的双反斜杠（`C:\\Users\\…`），Windows 的 Win32 层会把重复分隔符合并，
    os.path.isfile / realpath 都能正确落到真实路径，因此不做反转义（反转义会把
    合法 UNC 路径 `\\\\server\\share` 弄坏）。
    """
    cwd = ''
    for m in messages or []:
        if m.get('role') != 'user':
            continue
        text = _text_of(m.get('content', ''))
        mt = _SESSION_WS_RE.search(text)
        if mt:
            return mt.group(1).strip()
        if not cwd:
            mc = _CWD_RE.search(text)
            if mc:
                cwd = mc.group(1).strip()
    return cwd


def _image_path_from_token(tok):
    """把一段 token 收窄成「图片文件引用」；不像图片返回 ''。

    两步：先看整段是否以图片扩展名收尾（最常见：`图.png`、`@u=x,1&y.jpg`）；
    否则在 token 内部找第一处图片扩展名，截到那里为止 —— 这样
    `@图.png，看看` / `@图.png)好的` 这类「引用后面紧贴正文、中间没空格」的
    写法也能取对，而中文逗号/括号不会被当成文件名的一部分。
    """
    tok = (tok or '').strip()
    if not tok:
        return ''
    cand = tok
    for _ in range(8):
        if not cand:
            return ''
        if _IMAGE_EXT_END_RE.search(cand):
            return cand
        if cand[-1] in _TRAILING_PUNCT:
            cand = cand[:-1].rstrip(_TRAILING_PUNCT)
            continue
        break
    m = _IMAGE_EXT_FIND_RE.search(tok)
    return tok[:m.end()] if m else ''


def _at_image_candidates(text):
    """抽出正文里 `@图片名` 提及中「看起来是图片文件」的那些路径。

    @ 后面按整段 token 抓取（`@"带 空格.jpg"` 走引号分支），再交给
    _image_path_from_token 收窄。文件名里的逗号/等号/与号得以保留 ——
    这正是老实现漏掉 `@u=2448247082,481968747&…JPEG.jpg` 的原因。
    """
    out = []
    for m in _AT_MENTION_RE.finditer(text or ''):
        tok = m.group(1) if m.group(1) is not None else (m.group(2) or '')
        hit = _image_path_from_token(tok)
        if hit:
            out.append(hit)
    return out


# 正文里不带 @、也不带路径的裸文件名：`看看 图片.png`
# 只收「单段 token、以图片扩展名结尾」的候选，是否采纳仍由存在性校验决定，
# 因此正文里提到一个不存在的名字不会产生任何副作用。
_BARE_IMAGE_RE = re.compile(r'(?<![\w@.\\/])([^\s/\\:*?"<>|]+' + _IMAGE_EXT_PAT + r')', re.I)


def _bare_image_candidates(text):
    out = []
    for tok in _BARE_IMAGE_RE.findall(text or ''):
        hit = _image_path_from_token(tok)
        if hit:
            out.append(hit)
    return out


def _has_workspace_ref(text):
    return bool(_WS_REF_RE.search(text or ''))


def _strip_workspace_refs(text):
    """去掉 <workspace-reference …/> 标签，用于判断一条消息是不是「纯附件消息」。"""
    return _WS_REF_RE.sub('', text or '').strip()


def _resolve_local_image(raw, workspace_root, cfg):
    """把一处图片引用解析成本机绝对路径。

    返回 dict(path, mime, bytes)；不是图片 / 不存在 / 超限 / 越界一律返回 None。
    宁可不发图片，也不要让网关去读一个意图不明的文件。
    """
    raw = (raw or '').strip().strip('"\'')
    if not raw:
        return None
    # 标记用 ]] 收尾，路径里带 ] 会让上游切不干净 → 宁可跳过
    if ']' in raw or '\n' in raw:
        return None
    exts = tuple(e.lower() for e in (cfg.get('local_image_exts') or ()))
    p = os.path.expanduser(raw)
    if not os.path.isabs(p):
        if not workspace_root:
            return None
        p = os.path.join(workspace_root, p)
    try:
        p = os.path.realpath(p)
    except OSError:
        return None
    if exts and not p.lower().endswith(exts):
        return None
    if not os.path.isfile(p):
        return None
    if cfg.get('local_image_restrict_to_workspace'):
        root = os.path.realpath(workspace_root) if workspace_root else ''
        if not root or not (p == root or p.startswith(root + os.sep)):
            return None
    try:
        size = os.path.getsize(p)
        if size <= 0 or size > int(cfg.get('local_image_max_bytes', 8388608)):
            return None
        with open(p, 'rb') as fh:
            mime = _sniff_image_mime(fh.read(16))
    except OSError:
        return None
    if not mime:
        return None
    return {'path': p, 'mime': mime, 'bytes': size}


def _spill_inline_images(messages, turn_start, cfg):
    """把 OpenAI 标准 `image_url` 图片块落成本地文件，返回路径列表。

    为什么需要：DSH 声明 `inputModalities: [text, image]`，上传图片后会发
    `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…"}}`。
    而网关只取 `type == 'text'` 的块（_text_of），于是**图片被静默丢弃**，
    模型看到图却"看不见"。真让 WorkBuddy 把 supportsImages 改 true 也一样。

    上游 AFS 支持从**本地文件**读图（`[[local-image:path]]`），所以这里把
    data URI 解码成临时文件，再走同一条本地图片通道——不需要上游支持多模态。
    """
    if not cfg.get('local_image_enabled', True):
        return []
    max_bytes = int(cfg.get('local_image_max_bytes', 8388608))
    max_count = int(cfg.get('local_image_max_count', 4))
    exts = tuple(e.lower() for e in (cfg.get('local_image_exts') or ()))
    spill_dir = os.path.join(_TMP_DIR, 'ds-gw-inline-img')
    out, seen = [], set()

    for m in (messages or [])[turn_start:]:
        content = m.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'image_url':
                continue
            if len(out) >= max_count:
                return out
            url = block.get('image_url')
            if isinstance(url, dict):
                url = url.get('url')
            url = str(url or '')
            if not url.lower().startswith('data:') or ';base64,' not in url:
                # 远程 URL 不下载：多一次网络请求就多一次失败面，留给模型自己 fetch
                continue
            head, b64 = url.split(';base64,', 1)
            mime = head[5:].strip().lower() or 'image/png'
            ext = {'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/png': '.png',
                   'image/webp': '.webp', 'image/gif': '.gif', 'image/bmp': '.bmp'}.get(mime)
            if not ext or (exts and ext not in exts):
                continue
            try:
                raw = base64.b64decode(b64, validate=False)
            except Exception:  # noqa: BLE001
                continue
            if not raw or len(raw) > max_bytes:
                continue
            digest = hashlib.sha256(raw).hexdigest()[:16]
            if digest in seen:
                continue
            seen.add(digest)
            try:
                os.makedirs(spill_dir, exist_ok=True)
                path = os.path.join(spill_dir, f'{digest}{ext}')
                if not os.path.isfile(path):
                    with open(path, 'wb') as fh:
                        fh.write(raw)
            except OSError:
                continue
            out.append(path)
    if out:
        log(f"🖼 收到 {len(out)} 个内联图片块（base64）→ 已落盘走本地图片通道")
    return out


def collect_local_images(question, ref_tags, messages, cfg):
    """解析本轮引用的本地图片，返回 [(abspath, mime, size), …]，按出现顺序去重。

    五类来源（按可靠度排序）：客户端的 <image_local_path> 标签、结构化标签、
    正文 @ 提及、正文里的带路径引用、正文里的裸文件名。
    """
    if not cfg.get('local_image_enabled', True):
        return []
    workspace_root = (cfg.get('workspace_root') or '').strip() or _session_workspace(messages)

    candidates = []
    # 标签优先：它是客户端明确指名的那张图，且能容纳带空格的路径
    candidates += _IMAGE_TAG_RE.findall(question or '')
    candidates += list(ref_tags or [])
    candidates += _at_image_candidates(question or '')
    candidates += _PATH_IMAGE_RE.findall(question or '')
    candidates += _bare_image_candidates(question or '')

    found, seen = [], set()
    max_count = int(cfg.get('local_image_max_count', 4))
    for raw in candidates:
        item = _resolve_local_image(raw, workspace_root, cfg)
        if not item or item['path'] in seen:
            continue
        seen.add(item['path'])
        found.append((item['path'], item['mime'], item['bytes']))
        if len(found) >= max_count:
            break
    return found


def _local_image_lines(images):
    """生成注入到「当前用户问题」之后的标记行（每张图一行）。"""
    return ['\n' + _LOCAL_IMAGE_MARKER.format(path=p) for p, _m, _b in images]


# 附上图片后必须明确告诉模型「图已经在视觉输入里」。
# 否则 DSH 那类客户端会照着 @路径 的指引去 read/Get-Content 读 jpg，
# 拿到 `binary file` 错误，再据此回答「我无法查看图片」——实测就是这样翻车的。
_LOCAL_IMAGE_HINT = (
    '\n[图片已附上] 上面 local-image 标记引用的图片已经作为图像内容随本条消息提供给你，'
    '你可以直接看到它，按图像本身作答即可。'
    '不要用 read / Get-Content / PowerShell 去读取 .jpg/.png 文件：'
    '它们是二进制，只会返回 binary file 之类的错误，看图不需要任何工具。'
)
# 拼好的提示里属于「附件区」的行首，_last_user_text 取查询词时要跳过
_ATTACH_NOISE_PREFIXES = ('[[local-image:', '[图片已附上]')


def strip_local_image_lines(text):
    """去掉 [[local-image:…]] 标记行与「图片已附上」说明（取回干净提问用）。"""
    return '\n'.join(
        ln for ln in (text or '').split('\n')
        if not ln.strip().startswith(_ATTACH_NOISE_PREFIXES)
    ).strip()


# 客户端两种网页工具的输出形态标志：WebFetch（旧名 read_page）结果含 Content:
# 正文段，WebSearch（旧名 web_search）结果只有来源列表。用于判断「模型手里是否
# 真的有数据」。换客户端后这些标记可能失效，诊断日志里的 S/C 标志就是为此加的。
_CONTENT_MARKERS = ('Content:', 'content:', '正文:')
_SOURCE_MARKERS = ('ranked result(s)', 'Read items for the sources', 'Sources:')

# WorkBuddy 的 WebSearch 输出的是正文摘录，不是纯链接列表：
#     # Search Results for "重庆今天天气 实时"
#     ## 1. [墨迹天气](url)  → 气温/湿度/逐小时表格
#     *Found 5 results • Provider: 1 • Search performed on ...*
# 也就是说这类结果**自带数据**，不存在「只有来源、没有数值」的缺口。原来的
# 判定只认 DSH 的 `Sources:` 形态，在 WorkBuddy 下永远不命中，防编造检测等于
# 失效（还会让模型白跑一轮 WebFetch）。这里用富结果标记直接短路。
_RICH_RESULT_MARKERS = ('# Search Results for', 'results • Provider:')


# 拼好的提示里各段落的分隔标记（_last_user_text 依赖它取回当前提问）
_CURRENT_Q_MARKER = '[当前用户问题]'


def _collect_dialogue_history(messages, stop_index=None, max_msgs=None,
                              per_msg_chars=None, total_chars=None):
    """收集最近若干轮「用户提问 → 助手回答」的纯文本，渲染成对话历史。

    为什么要带：trim_messages 原本只保留最后一条用户提问，于是
    「把刚才那个结果整理成表格」「那深证成指呢？」这类带指代的追问失去
    先行词——模型看不到自己上一条回答，也不知道此前问过什么，只能重新取数。

    只取纯文本轮次：
    - 跳过 system / tool 消息（tool 结果由 _collect_tool_history 单独渲染，
      避免同一份内容在提示里出现两次、白占字符预算）
    - assistant 若只有 tool_calls、没有 content，则整条跳过（同上）
    - user 消息先用 _clean_user_text 去掉运行时上下文噪音
    - 带工具调用痕迹的文本一律清洗，避免模型模仿损坏的 XML 格式
    """
    cfg = get_config()
    if max_msgs is None:
        max_msgs = int(cfg.get('history_dialogue_msgs', 6))
    if per_msg_chars is None:
        per_msg_chars = int(cfg.get('history_dialogue_chars', 800))
    if total_chars is None:
        total_chars = int(cfg.get('history_dialogue_total_chars', 3000))

    # stop_index 及之后的都是「当前提问 + 运行时上下文」，不属于历史
    end = len(messages) if stop_index is None else max(0, stop_index)

    turns = []
    for m in messages[:end]:
        role = m.get('role')
        if role not in ('user', 'assistant'):
            continue
        if role == 'user':
            text = _clean_user_text(_text_of(m.get('content', '')))
            label = '用户'
        else:
            text = _text_of(m.get('content', ''))
            label = '助手'
        if looks_like_tool_call(text):
            text = strip_tool_markup(text)
        text = _clean_xml_noise(text).strip()
        if not text:
            continue
        turns.append((label, text))

    if not turns:
        return ''

    # 从最新往回取，直到达到条数或总字符预算
    selected = []
    used = 0
    for label, text in reversed(turns[-max_msgs:] if max_msgs > 0 else turns):
        if per_msg_chars > 0 and len(text) > per_msg_chars:
            text = text[:per_msg_chars] + '…(已截断)'
        line = f"{label}: {text}"
        if used + len(line) > total_chars and selected:
            break
        selected.append(line)
        used += len(line)

    selected.reverse()
    return '\n'.join(selected)


def trim_messages(messages, cfg):
    """只保留「对话历史 + 工具结果 + 当前提问 + 精简 system」。

    上游是网页聊天，没有原生多轮 role 结构，因此历史只能扁平化进同一条
    user 消息里，按「此前对话历史 → 此前工具结果 → 当前提问」的顺序排列。

    返回新消息列表：0 或 1 条 system + 最后一条用户消息。
    """
    cfg_sys = (cfg.get('system_prompt') or '').strip()
    max_chars = int(cfg.get('max_system_chars') or 0)

    out = []
    # 精简 system：优先用自己的；否则用客户端第一条 system 截断
    if cfg.get('drop_client_system', True):
        sys_text = cfg_sys
    else:
        client_sys = ''
        for m in messages:
            if m.get('role') == 'system':
                client_sys += _text_of(m.get('content', '')) + '\n'
        sys_text = (cfg_sys + '\n' + client_sys).strip()

    if sys_text:
        if max_chars > 0 and len(sys_text) > max_chars:
            sys_text = sys_text[:max_chars]

    # 当前日期：**在截断之后**追加，否则长提示词被 max_system_chars 切掉时
    # 最先丢的就是它。开关关掉则完全不加，保持原有行为不变。
    # 即使没配 system_prompt 也照样注入：日期是运行期事实，不是提示词自定义内容。
    if cfg.get('inject_current_date', True):
        date_text = current_date_text()
        sys_text = f"{sys_text}\n\n{date_text}" if sys_text else date_text

    if sys_text:
        out.append({'role': 'system', 'content': sys_text})

    # 最后一条含真实提问的 user 消息（DSH 会把 system-reminder/运行时上下文
    # 作为额外的 user 消息追加在提问之后，需跳过纯噪音消息）
    #
    # 另一类要跳过的是「纯附件消息」：DSH 的 @ 文件引用会**单独**发一条
    # content 只有 <workspace-reference …/> 的 user 消息。若把它当成提问，
    # 真正的问题（"图里是什么内容"）就掉进历史里、当前提问只剩一个标签。
    question = ''
    q_index = -1
    for idx in range(len(messages) - 1, -1, -1):
        m = messages[idx]
        if m.get('role') != 'user':
            continue
        raw = _text_of(m.get('content', ''))
        cleaned = _clean_user_text(raw)
        if not cleaned and _has_workspace_ref(raw):
            continue
        if _has_workspace_ref(cleaned):
            # 标签和问题在同一条消息里 → 标签不算提问正文，只当引用
            cleaned = _strip_workspace_refs(cleaned)
            if not cleaned:
                continue
        if cleaned:
            question = cleaned
            q_index = idx
            break

    # 本轮的引用：只看最后一条 assistant/tool 之后的消息，避免把历史轮次的
    # 附件重复解析一遍（历史里的标签是上一轮的话题，不该再附一次图）。
    turn_start = 0
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get('role') in ('assistant', 'tool'):
            turn_start = idx + 1
            break
    ref_tags = []
    for m in messages[turn_start:]:
        if m.get('role') != 'user':
            continue
        for p in _WS_REF_RE.findall(_text_of(m.get('content', ''))):
            if p.strip() and p.strip() not in ref_tags:
                ref_tags.append(p.strip())

    # 内联图片块（DSH 发的 base64 image_url）先落盘，再和路径引用一起走本地图片通道
    for p in _spill_inline_images(messages, turn_start, cfg):
        if p not in ref_tags:
            ref_tags.append(p)

    images = collect_local_images(question, ref_tags, messages, cfg)

    # 纯图片消息（DSH 拖图上来可以没有文字）：question 会算成空，这里补一句
    # 中性指令，否则拼出来的提示只剩一个图片标记，模型不知道要干什么。
    if not question and images:
        question = '请看这张图片，描述其中的内容。'
        q_index = len(messages) - 1 if messages else 0

    if not question and not images:
        return out

    blocks = []

    # 1) 此前的对话历史：让带指代的追问（"那深证成指呢？"）有先行词，
    #    也让模型知道自己上一条回答过什么，不必重新取数。
    if cfg.get('history_include_dialogue', True):
        dialogue = _collect_dialogue_history(
            messages, stop_index=q_index,
            max_msgs=int(cfg.get('history_dialogue_msgs', 6)),
            per_msg_chars=int(cfg.get('history_dialogue_chars', 800)),
            total_chars=int(cfg.get('history_dialogue_total_chars', 3000)))
        if dialogue:
            blocks.append(f"[此前对话历史]\n{dialogue}")

    # 2) 此前的工具执行结果：若历史里存在「assistant tool_calls + tool 结果」，
    #    把最近几轮合并进提问，让上游模型知道此前已执行过什么。
    tool_history = _collect_tool_history(messages)
    hint = ''
    if tool_history:
        blocks.append(f"[此前已执行的工具结果]\n{tool_history}")
        hint = _data_gap_hint(tool_history, cfg)

    # 3) 本地图片：把绝对路径标记跟在提问后面，由上游 AFS 读盘上传给 DeepSeek。
    #    同时保留 DSH 原始的 <workspace-reference> 标签，让模型知道文件叫什么。
    attach = ''
    if images:
        for tag in ref_tags:
            attach += f"\n<workspace-reference path={json.dumps(tag, ensure_ascii=False)} kind=\"file\" />"
        attach += ''.join(_local_image_lines(images))
        attach += _LOCAL_IMAGE_HINT
        log(f"🖼 本地图片 {len(images)} 张 → 注入标记: {[p for p, _m, _b in images]}")

    if blocks:
        blocks.append(f"{_CURRENT_Q_MARKER}\n{question}{attach}{hint}")
        out.append({'role': 'user', 'content': '\n\n'.join(blocks)})
    else:
        out.append({'role': 'user', 'content': f"{_CURRENT_Q_MARKER}\n{question}{attach}{hint}"})

    return out


def _fetch_tool_name(cfg):
    """当前配置里「读网页」那个工具的真实名字（WorkBuddy: WebFetch / DSH: web_fetch）。

    提示词里绝不能写死名字：写错等于让模型去调一个客户端不存在的工具。
    """
    names = [str(t.get('name') or '') for t in (cfg.get('tools') or []) if isinstance(t, dict)]
    for cand in ('WebFetch', 'web_fetch', 'read_page'):
        if cand in names:
            return cand
    return '读网页工具'


def _data_gap_hint(history_text, cfg=None):
    """检测「工具结果只有来源链接、没有正文数据」并给出强制取数指令。

    实测：web_search 的结果只是一串来源标题+URL，模型拿到后会直接凭记忆编出
    点位/涨跌幅/成交额（实测编成 3812.51 / +0.42%，而真实为 3939.33 / -0.31%）。
    gateway 能在历史里看到这一点，所以主动要求它去读网页把数据取回来。

    判据说明：试过「数字密度」「链接占比」，两者在来源列表与网页正文之间几乎
    没有区分度（14.4% vs 13.7%、33% vs 55%）。可靠的是两种工具的**输出
    形态**——读网页的结果一定含 `Content:` 正文段，而 web_search 只有
    `ranked result(s)` / `Sources:` 列表。因此以「有来源标记、无正文标记」判定。
    """
    if not history_text:
        return ''
    # 富结果（WorkBuddy 的 WebSearch 直接带正文）→ 数据已在手里，不要再逼它取数
    if any(mk in history_text for mk in _RICH_RESULT_MARKERS):
        return ''
    cfg = cfg if cfg is not None else get_config()
    has_content = any(mk in history_text for mk in _CONTENT_MARKERS)
    has_sources = any(mk in history_text for mk in _SOURCE_MARKERS)
    if has_sources and not has_content:
        fetch = _fetch_tool_name(cfg)
        # 只有 WorkBuddy 的 WebFetch 需要 prompt；DSH 的 web_fetch 只收 url
        extra = '（prompt 参数写清要提取什么）' if fetch == 'WebFetch' else ''
        log(f"⚠ 工具结果仅有来源链接、无正文数据 → 注入强制取数指令（{fetch}）")
        return (f'\n\n[重要] 上面的工具结果只有来源链接、没有具体数值数据。'
                f'你必须用 {fetch} 打开其中一个来源 URL 把真实数据取回来{extra}，然后才回答。'
                '绝对不要凭记忆、推测或举例数字作答——'
                '编造数据比如实回答「未获取到」严重得多。')
    return ''


def _collect_tool_history(messages, max_rounds=None, per_result_chars=None, total_chars=None):
    """收集最近若干轮的 assistant tool_calls 与对应 tool 结果，渲染成文本。

    为什么要多轮累积：只保留「最近一轮」会让模型每轮都"失忆"——它不知道
    之前已经搜到/读到了什么，于是反复重搜同一问题（实测某次 A 股行情查询
    因此做了 45 次工具调用，最终触发上游风控）。这里改为累积最近 N 轮，
    并按总字符预算从最新往回裁剪。

    渲染时：
    - 清理 arguments 中的 XML/CDATA 痕迹（避免模型模仿损坏格式）
    - 工具结果先做导航噪声过滤（去掉纯导航链接行），再按 per_result_chars 截断
    """
    cfg = get_config()
    if max_rounds is None:
        max_rounds = int(cfg.get('history_max_rounds', 3))
    if per_result_chars is None:
        per_result_chars = int(cfg.get('history_result_chars', 3000))
    if total_chars is None:
        total_chars = int(cfg.get('history_total_chars', 8000))

    # 按顺序收集每一轮 (calls, results)
    rounds = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m.get('role') == 'assistant' and m.get('tool_calls'):
            calls = m['tool_calls']
            call_ids = {c.get('id') for c in calls if c.get('id')}
            results = []
            j = i + 1
            while j < n and messages[j].get('role') == 'tool':
                if messages[j].get('tool_call_id') in call_ids:
                    results.append(_text_of(messages[j].get('content', '')))
                j += 1
            rounds.append((calls, results))
            i = j
            continue
        i += 1

    if not rounds:
        return ''

    # 从最新一轮往回取，直到达到轮数或总字符预算
    selected = []
    used = 0
    for calls, results in reversed(rounds[-max_rounds:] if max_rounds > 0 else rounds):
        block_lines = []
        for c in calls:
            fn = c.get('function', {})
            args_str = _clean_xml_noise(fn.get('arguments', ''))
            block_lines.append(f"- 调用了 {fn.get('name')}({args_str})")
        for r in results:
            cleaned = _clean_tool_result(r, per_result_chars)
            if cleaned:
                block_lines.append(f"  结果: {cleaned}")
        block = '\n'.join(block_lines)
        if used + len(block) > total_chars and selected:
            break
        selected.append(block)
        used += len(block)

    selected.reverse()
    header = f"[此前已执行 {len(rounds)} 轮工具调用，以下是最近 {len(selected)} 轮的结果]"
    return header + '\n' + '\n'.join(selected)


# 纯导航/装饰行的匹配：整行只是 markdown 链接、图片、分隔符等
_NAV_LINK_LINE = re.compile(r'^\s*[-*|\\\s]*\[[^\]]{0,12}\]\([^)]{0,120}\)\s*$')
_NAV_IMAGE_LINE = re.compile(r'^\s*!\[[^\]]*\]\([^)]*\)\s*$')
_SEP_LINE = re.compile(r'^\s*[-*|\\_\s]{1,12}$')


def _is_nav_line(s):
    """判断一行是否只是站点导航/广告链接。

    做法：剥掉所有 markdown 链接、图片、裸 URL 与装饰符后，看还剩多少
    可读文本；剩余过少说明该行的价值全在链接上（导航/广告），丢弃。
    这样能覆盖 `- [![广告](img)](track)` 这类嵌套图片广告行。
    """
    if _NAV_LINK_LINE.match(s) or _NAV_IMAGE_LINE.match(s) or _SEP_LINE.match(s):
        return True
    stripped = re.sub(r'!?\[[^\]]*\]\([^)]*\)', '', s)   # [文字](url) / ![alt](url)
    stripped = re.sub(r'https?://\S+', '', stripped)       # 裸 URL
    stripped = re.sub(r'[\-*|\\_\s]+', '', stripped)       # 装饰符与空白
    return len(stripped) < 8


def _clean_tool_result(text, max_chars=3000):
    """过滤工具结果里的导航噪声，再按预算截断。

    实测 read_page 返回的网页（2 万+ 字符）里，前面几乎全是站点导航链接与
    广告图片（财经/股票/基金/…），有效数据被淹没。这里先按行过滤掉导航/广告
    行，保留正文，再截断到 max_chars。
    """
    if not text:
        return ''
    lines = text.split('\n')
    kept = []
    dropped = 0
    for line in lines:
        s = line.strip()
        if not s:
            kept.append('')
            continue
        if _is_nav_line(s):
            dropped += 1
            continue
        kept.append(line)

    result = '\n'.join(kept)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    if len(result) > max_chars:
        result = result[:max_chars] + '\n...(内容过长已截断)'
    if dropped and result:
        result = f"(已过滤 {dropped} 行导航/广告)\n" + result
    return result


def _clean_xml_noise(s):
    """清理文本中的 XML/CDATA 残留标记，避免模型模仿输出损坏格式。"""
    if not s:
        return s
    s = normalize_markup(s)   # 先归一化全角竖线，否则 DSML 残留清不掉
    # 去掉 <![CDATA[ 及截断变体开头、]]> 结尾、</|CHAT2API|*> / <||DSML||*> 标签
    s = re.sub(r'<!\[(?:CDATA|CDAT|CDA|CD|C|ATA|DATA|DAT|DA|A)?\[', '', s)
    s = s.replace(']]>', '').replace(']>', '').replace(']]', '')
    s = re.sub(r'</?\|+(?:CHAT2API|DSML)\|+[^>]*>', '', s)
    s = re.sub(r'</?\|?CHAT2API\|?(?:/?)>', '', s)
    # 折叠多余空白
    s = re.sub(r'\s{3,}', ' ', s).strip()
    return s


# ═══════════════════════════════════════════════════════
# 工具列表：Gradio 维护的精简列表 → OpenAI tools / 文本描述
# ═══════════════════════════════════════════════════════
def config_tools_as_openai(cfg):
    """把配置里的精简工具列表转成 OpenAI tools 数组。"""
    return [{"type": "function", "function": t} for t in cfg.get('tools', [])]


def config_tools_as_prompt(cfg):
    """把精简工具列表渲染成给模型的文本描述（附在 system 后）。

    引导模型输出 XML 工具调用块（兼容 AFS 的 managed_xml 解析）：
      <|CHAT2API|tool_calls><|CHAT2API|invoke name="工具名">...参数...</|CHAT2API|invoke></|CHAT2API|tool_calls>
    """
    lines = []
    for i, t in enumerate(cfg.get('tools', []), 1):
        name = t.get('name', '')
        desc = t.get('description', '')
        lines.append(f"{i}. {name}: {desc}")
    if not lines:
        return ''
    return (
        "\n\n可用工具：\n" + "\n".join(lines) +
        "\n\n当需要调用工具时，只输出如下 XML 块（工具名用上面列出的确切名称；"
        "每个 parameter 对应工具的必填参数，**必须全部提供**，"
        "如 bash 需要 command 和 description 两个参数）：\n"
        '<tool_calls><invoke name="工具名">'
        '<parameter name="参数名">参数值</parameter>'
        '</invoke></tool_calls>\n'
        "参数值直接写纯文本。\n"
        "重要：如果一次工具调用失败或没有结果，不要立即重复调用完全相同的工具和参数——"
        "先检查原因或换一种方式。"
    )


# ═══════════════════════════════════════════════════════
# XML 工具调用解析（DeepSeek 网页端经 AFS managed_xml 返回的格式）
# ═══════════════════════════════════════════════════════
_CHAT2API_INVOKE = re.compile(
    r'<\|CHAT2API\|invoke\s+name="([^"]+)"\s*>(.*?)</\|CHAT2API\|invoke>',
    re.S,
)
_CHAT2API_PARAM = re.compile(
    r'<\|CHAT2API\|parameter\s+name="([^"]+)"\s*>(.*?)</\|CHAT2API\|parameter>',
    re.S,
)
_GENERIC_INVOKE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.S)
_GENERIC_PARAM = re.compile(r'<parameter\s+name="([^"]+)"\s*>(.*?)</parameter>', re.S)

# ── 全角/异体字符归一化 ────────────────────────────────
# 实测重大发现：DeepSeek 网页端会输出它原生的 DSML 标记
# `<||DSML||tool_calls>` / `<||DSML||invoke name="X">`，而且那两条竖线常常是
# 全角 U+FF5C（字节 ef bd 9c），肉眼与 ASCII `|` 完全一样，导致正则永远匹配不上——
# 这正是「偶发解析失败、原始 XML 泄漏给客户端」的真正根因。
# 因此解析前先做字符归一化，并把 DSML 前缀与 CHAT2API 前缀同等对待。
_MARKUP_TRANS = {
    0xFF5C: '|',   # ｜ 全角竖线  → |
    0xFF1C: '<',   # ＜ 全角小于号 → <
    0xFF1E: '>',   # ＞ 全角大于号 → >
    0xFF0F: '/',   # ／ 全角斜杠  → /
    0xFF02: '"',   # ＂ 全角引号  → "
    0x201C: '"', 0x201D: '"',   # 弯引号
    0x2018: "'", 0x2019: "'",
    0x3000: ' ',   # 全角空格
    0xFF1D: '=',   # ＝
}
_MARKUP_DROP = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF}  # 零宽字符


def normalize_markup(text):
    """归一化全角/零宽的标记字符，并剥掉 DSML/CHAT2API 标签前缀。

    归一化后 `<||DSML|| invoke name="x">` 变成 `<invoke name="x">`，
    于是所有下游正则只需处理最朴素的 XML 形态。
    """
    if not text:
        return text or ''
    t = text.translate(_MARKUP_TRANS).translate({c: None for c in _MARKUP_DROP})
    t = re.sub(r'<\s*\|+\s*(?:CHAT2API|DSML)\s*\|+\s*', '<', t)
    t = re.sub(r'</\s*\|+\s*(?:CHAT2API|DSML)\s*\|+\s*', '</', t)
    return t


# ── 宽容解析：模型偶发输出残缺 XML（缺 <invoke> 开标签、属性无引号、
#    参数用 <name="x"> 而非 <parameter name="x">、闭合标签缺失）────────
_C2A = r'(?:\|+\s*(?:CHAT2API|DSML)\s*\|+)?'   # 可选前缀：<|CHAT2API| / <||DSML||
_ANY_INVOKE = re.compile(
    r'<' + _C2A + r'invoke\b([^>]*)>(.*?)(?=</' + _C2A + r'invoke>|</tool_calls>|\Z)', re.S)
_ANY_PARAM = re.compile(
    r'<' + _C2A + r'parameter\b([^>]*)>(.*?)'
    r'(?=</' + _C2A + r'parameter>|<' + _C2A + r'parameter\b|</' + _C2A + r'invoke>|</tool_calls>|\Z)',
    re.S)
_BARE_PARAM = re.compile(
    r'<name\s*=\s*["\']?\s*([A-Za-z_][\w.\-]*)["\']?\s*/?>(.*?)'
    r'(?=<name\s*=|</name>|</invoke>|</tool_calls>|\Z)', re.S)
_ATTR_NAME = re.compile(r'name\s*=\s*["\']?\s*([A-Za-z_][\w.\-]*)')
_NAME_TAG = re.compile(r'<name\s*=\s*["\']?\s*([A-Za-z_][\w.\-]*)', re.I)
_CLOSER = re.compile(r'</' + _C2A + r'invoke>|</tool_calls>', re.I)

# 判定「这段文本看起来是工具调用」——用于阻止残缺 XML 被当成回答正文泄漏。
# 刻意收窄：仅在正文里提一句 <tool_calls> 的教学性文字不应触发重试。
_TOOLCALL_HINT = re.compile(
    r'</?invoke\b|<parameter\b|<\|\s*(?:CHAT2API|DSML)\s*\|', re.I)


def looks_like_tool_call(text):
    return bool(text and _TOOLCALL_HINT.search(normalize_markup(text)))


def strip_tool_markup(text, valid_names=None):
    """把无法解析的工具调用 XML 残留清成可读文本（避免把 XML 泄漏给客户端）。"""
    t = normalize_markup(text)
    t = re.sub(r'</?' + _C2A + r'(?:tool_calls|invoke|parameter|name)\b[^>]{0,200}?>', ' ', t)
    t = re.sub(r'<name\s*=\s*["\']?[A-Za-z_][\w.\-]*["\']?\s*/?>', ' ', t)
    t = re.sub(r'<\|CHAT2API\|[^>]*>?', ' ', t)
    # 裸工具标签（<web_search"> 这类把工具名当标签的退化写法）
    for v in (valid_names or []):
        t = re.sub(r'</?' + re.escape(v) + r'\b[^>]{0,200}?>', ' ', t, flags=re.I)
    t = re.sub(r'[ \t]{2,}', ' ', t)
    return re.sub(r'\n{3,}', '\n\n', t).strip()


def _params_from(body):
    """从 invoke 体内提取 {参数名: 值}，兼容标准与残缺两种写法。

    同名参数重复出现时聚合成列表——模型常把多条查询各写一个 <parameter
    name="query">，聚合后正好能被 _fill_missing_required 从单数字段收进
    数组字段（web_search 的 queries）。
    """
    args = {}

    def put(key, val):
        if key in args:
            prev = args[key]
            args[key] = (prev if isinstance(prev, list) else [prev]) + [val]
        else:
            args[key] = val

    for pm in _ANY_PARAM.finditer(body):
        m = _ATTR_NAME.search(pm.group(1) or '')
        if not m:
            continue
        put(m.group(1), _parse_jsonish(pm.group(2)))
    if args:
        return args
    # 退化写法：<name="query">值</parameter> 或 <name="query">值
    for pm in _BARE_PARAM.finditer(body):
        put(pm.group(1), _parse_jsonish(pm.group(2)))
    return args


_CDATA_OPEN_RE = re.compile(r'^<!\[(?:CDATA|CDAT|CDA|CD|C|ATA|DATA|DAT|DA|A)?\[')

def _unwrap_cdata(v):
    """剥离参数值外层可能残留的 CDATA 标记。

    DeepSeek 网页端对 CDATA 的闭合经常不标准（缺 `]`、缺字符），历史多轮
    往返后还可能产生 `<![ATA[` 这类截断变体。这里做宽容剥离：
    - 开头匹配 <![CDATA[ 或它的截断变体（<![ATA[、<![DATA[ 等）
    - 结尾去掉 ]]> / ]> / ] 或裸 >
    - 若值内仍嵌有 CDATA 片段（来自历史污染），一并清理
    """
    v = v.strip()
    m = _CDATA_OPEN_RE.match(v)
    if m:
        v = v[m.end():]
        # 去掉尾部闭合标记的多种形态
        if v.endswith(']]>'):
            v = v[:-3]
        elif v.endswith(']>'):
            v = v[:-2]
        elif v.endswith(']'):
            v = v[:-1]
        elif v.endswith('>'):
            v = v[:-1]
    # 值内部可能残留 <![CDATA[ ... ]]> 片段（历史往返污染）：剥掉标记只留内容
    v = re.sub(r'<!\[(?:CDATA|CDAT|CDA|CD|C|ATA|DATA|DAT|DA|A)?\[', '', v)
    v = v.replace(']]>', '').replace(']>', '').replace(']]', '')
    return v.strip()


def _parse_jsonish(v):
    v = _unwrap_cdata(v)
    try:
        return json.loads(v)
    except Exception:  # noqa: BLE001
        return v


def extract_tool_calls(text, valid_names=None):
    """从上游文本中提取工具调用，返回 OpenAI 风格 tool_calls 列表。

    分 4 层递进，容忍模型偶发的格式退化：
      1) 标准 <invoke name="X"> 与 <|CHAT2API|invoke name="X">
      2) <invoke ...> 属性残缺 / 工具名写在体内 <name="X">
      3) 缺 <invoke> 开标签，只剩 </invoke>|</tool_calls> 闭合
      4) 裸工具标签 <web_search ...>
    解析到即返回；全部失败返回 []（调用方据此判定「疑似工具调用但解析失败」）。
    """
    if not text:
        return []
    text = normalize_markup(text)   # 全角竖线/引号归一化（DSML 标记的常见形态）
    calls = []

    def add(raw_name, body):
        name = resolve_name(raw_name, valid_names) if raw_name else None
        if name is None:
            if raw_name:
                log(f"忽略未知工具名: {raw_name}")
            return
        args = _params_from(body)
        if not args:
            log(f"工具 {name} 未解析到任何参数，忽略")
            return
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })

    # 1) 标准格式
    for im in _CHAT2API_INVOKE.finditer(text):
        add(im.group(1).strip(), im.group(2))
    if calls:
        return calls
    for im in _GENERIC_INVOKE.finditer(text):
        add(im.group(1).strip(), im.group(2))
    if calls:
        return calls

    # 2) 残缺 <invoke ...>：属性里没 name 就找体内的 <name="X">
    for im in _ANY_INVOKE.finditer(text):
        attrs, body = im.group(1) or '', im.group(2)
        m = _ATTR_NAME.search(attrs)
        raw = m.group(1) if m else None
        if not raw:
            m2 = _NAME_TAG.search(body)
            if m2:
                raw = m2.group(1)
                body = body[:m2.start()] + body[m2.end():]
        add(raw, body)
    if calls:
        return calls

    # 3) 没有 <invoke> 开标签，靠闭标签定位残缺块
    closing = _CLOSER.search(text)
    if closing:
        head = text[:closing.start()]
        raw, cut = None, None
        m = _NAME_TAG.search(head)
        if m:
            raw, cut = m.group(1), m
        if not raw and valid_names:
            for v in valid_names:
                if re.search(r'<' + re.escape(v) + r'\b', head, re.I):
                    raw = v
                    break
        if raw:
            if cut:
                head = head[:cut.start()] + head[cut.end():]
            add(raw, head)
    if calls:
        return calls

    # 4) 最后的兜底：裸工具标签 <web_search ...>
    for v in (valid_names or []):
        m = re.search(
            r'<' + re.escape(v) + r'\b[^>]*>(.*?)(?=</invoke>|</tool_calls>|</' + re.escape(v) + r'>|\Z)',
            text, re.S | re.I)
        if m:
            add(v, m.group(1))
    return calls


def resolve_name(name, valid_names):
    """精确优先，其次去下划线/大小写不敏感，再按别名组，最后前后缀（长度≥6）。

    别名组是关键：模型常按习惯吐 `Bash`（客户端在 Windows 同时暴露 Bash 与
    PowerShell），而配置里写的是 `PowerShell`；旧版配置还会吐 `read_page`。
    只做字面比对会导致这些调用被当作未知工具丢弃，日志里就是
    `忽略未知工具名: xxx`，然后整轮白跑。
    """
    if not valid_names:
        return name
    if name in valid_names:
        return name
    norm = norm_tool_name(name)
    for v in valid_names:
        if norm_tool_name(v) == norm:
            return v
    # 别名组：bash / pwsh / PowerShell 互认，read_page / WebFetch 互认
    group = _alias_group(norm)
    if group:
        for v in valid_names:
            if norm_tool_name(v) in group:
                return v
    # 前后缀（仅较长工具名，避免 read↔read_image 误配）
    for v in valid_names:
        vn = norm_tool_name(v)
        if len(vn) >= 6 and (vn.startswith(norm) or vn.endswith(norm)) and len(vn) > len(norm):
            return v
    return None


# ═══════════════════════════════════════════════════════
# 上游调用（转发给 AFS → DeepSeek 网页端）
# ═══════════════════════════════════════════════════════
def _last_user_text(messages):
    """取最后一条用户消息的纯文本（用于参数兜底）。

    trim_messages 会把「对话历史 + 工具结果 + 当前提问」拼成同一条 user 消息，
    整块拿去做查询词会很难看，所以优先只取 [当前用户问题] 之后的部分。
    """
    for m in reversed(messages or []):
        if m.get('role') != 'user':
            continue
        c = m.get('content')
        if isinstance(c, list):
            parts = [p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') == 'text']
            c = ' '.join(x for x in parts if x)
        if not isinstance(c, str) or not c.strip():
            continue
        text = c.strip()
        if _CURRENT_Q_MARKER in text:
            text = text.split(_CURRENT_Q_MARKER, 1)[1]
            text = text.split('[重要]', 1)[0]   # 去掉强制取数指令
        # 提问后面可能挂着本地图片标记与 @ 引用标签，都不是查询词
        text = strip_local_image_lines(text)
        text = _strip_workspace_refs(text)
        return text.strip()
    return ''


def _fallback_list(text, limit=200):
    """必填数组参数实在取不到值时，用用户原始提问兜底。

    空数组会被 DSH 按 schema（如 web_search 要求 1-4 条）直接拒绝，白费一轮
    上游请求；用提问本身代替虽然不是最优查询词，但保证调用可执行。
    """
    t = (text or '').strip()
    return [t[:limit]] if t else []


def _fill_missing_required(tool_calls, cfg, fallback_text=''):
    """参数补齐与类型修正（按 schema 类型感知）。

    模型常输出不合规参数，实测两类高频问题：
    - `queries` 应为数组却输出空串 `""`（或被写成字符串），DSH 校验报
      `"queries" must be an array`，白白浪费一轮请求
    - 必填参数缺失（如 bash 的 description）

    处理：
    - 数组类参数若为空串/字符串 → 转为数组（优先取同义单数字段如 query）
    - 必填缺失 → 按 schema 类型补合理默认值
    """
    by_name = {t.get('name'): t for t in cfg.get('tools', [])}
    valid = list(by_name)
    for call in tool_calls:
        fn = call.get('function') or {}
        name = fn.get('name', '')
        # 用 resolve_name 查 schema：模型/旧版本可能吐 `web_search`、`read_page`
        # 这类旧名，而配置里已经是 `WebSearch`、`WebFetch`，直接 dict 取值会查空，
        # 后续的参数修正与必填补齐整段跳过。
        schema = by_name.get(name)
        if not schema:
            resolved = resolve_name(name, valid) if valid else None
            schema = by_name.get(resolved or '')
        if not schema:
            continue
        params = schema.get('parameters') or {}
        props = params.get('properties') or {}
        required = params.get('required') or []
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except Exception:  # noqa: BLE001
            args = {}
        changed = False

        # 1) 类型修正：数组参数被写成空串/字符串时转为数组
        for prop, spec in props.items():
            if not isinstance(spec, dict) or spec.get('type') != 'array':
                continue
            val = args.get(prop)
            if isinstance(val, list):
                continue
            if val in (None, ''):
                # 尝试从同义单数字段（query 等）取一个值
                alt = None
                for cand in ('query', 'queries', 'q', 'search', 'keyword'):
                    if cand == prop:
                        continue
                    cval = args.get(cand)
                    if isinstance(cval, list) and cval:
                        alt = cval
                        break
                    if isinstance(cval, str) and cval.strip():
                        alt = [cval.strip()]
                        break
                args[prop] = alt if alt else _fallback_list(fallback_text)
                changed = True
                log(f"参数修正: {name}.{prop} 缺失/空 → 已补为 {json.dumps(args[prop], ensure_ascii=False)[:80]}")
            elif isinstance(val, str):
                s = val.strip()
                args[prop] = [s] if s else []
                changed = True
                log(f"参数修正: {name}.{prop} 是字符串 → 已转为数组")

        # 1b) 反过来：字符串参数被写成数组。
        #     客户端的 WebSearch.query 只接受单个字符串，模型却常按旧 schema 一次
        #     给多条查询（3 个 <parameter name="query">），数组会直接被 schema 校验
        #     拒绝。取第一条即可，至少这一轮能执行。
        for prop, spec in props.items():
            if not isinstance(spec, dict) or spec.get('type') != 'string':
                continue
            val = args.get(prop)
            if isinstance(val, list):
                args[prop] = str(val[0]) if val else ''
                changed = True
                log(f"参数修正: {name}.{prop} 是数组 → 只保留第一条")

        # 2) 必填补齐：按 schema 类型给合理默认值
        for prop in required:
            if prop not in args or args.get(prop) in (None, ''):
                spec = props.get(prop) or {}
                want = spec.get('type')
                # shell 工具各客户端/平台叫法不同（bash / pwsh / PowerShell），
                # 参数面一致：command 与 description 都必填。只认某一个名字会让
                # 另一个平台的调用落到下面的通用分支，被补成空串 ''，客户端按严格
                # schema 校验仍会报错，白跑一轮。
                if norm_tool_name(name) in _TOOL_ALIAS_GROUPS[0] and prop == 'description':
                    cmd = str(args.get('command', ''))
                    args['description'] = f"Run shell command: {cmd[:60]}" if cmd else 'Run a shell command'
                elif prop in ('query', 'prompt') and want == 'string' and fallback_text:
                    # WebSearch.query / WebFetch.prompt 是必填字符串，补空串会被
                    # 客户端拒绝；用当前提问兜底至少保证这一轮能执行。
                    args[prop] = str(fallback_text)[:200]
                elif want == 'array':
                    args[prop] = _fallback_list(fallback_text)
                elif want == 'boolean':
                    args[prop] = False
                elif want in ('number', 'integer'):
                    args[prop] = 0
                else:
                    args[prop] = ''
                changed = True
                log(f"参数补齐: {name} 缺少必填 '{prop}'，已自动补上")

        # 3) 删除 schema 未声明的参数。
        #    模型常额外编造同义字段（如 web_search 同时给 query 和 queries），
        #    DSH 侧按严格 schema 校验会直接报错，白费一轮。必须在同义取值（步骤 1）
        #    之后再删。仅当 schema 明确列出了 properties 时才裁剪，避免误删。
        if props:
            extra = [k for k in args if k not in props]
            for k in extra:
                args.pop(k, None)
            if extra:
                changed = True
                log(f"参数裁剪: {name} 删除未声明参数 {extra}")

        if changed:
            fn['arguments'] = json.dumps(args, ensure_ascii=False)


def call_upstream(messages, cfg):
    """非流式转发：返回 OpenAI 风格响应 dict。

    发请求前先做速率限制：DeepSeek 网页端对密集请求会触发风控禁言
    （实测约 >10 次/分钟即触发，账号级限制）。这里强制两次上游请求之间
    至少间隔 min_request_interval_ms，把整体速率压到安全区间。
    """
    # 速率限制：距上次上游请求不足最小间隔则等待
    global _last_request_at
    min_interval_ms = int(cfg.get('min_request_interval_ms', 12000))
    if min_interval_ms > 0:
        with _rate_lock:
            now = time.monotonic()
            elapsed_ms = (now - _last_request_at) * 1000 if _last_request_at else min_interval_ms
            wait_ms = min_interval_ms - elapsed_ms
            if wait_ms > 0:
                log(f"⏳ 限流: 距上次上游请求仅 {elapsed_ms:.0f}ms，等待 {wait_ms/1000:.1f}s")
                time.sleep(wait_ms / 1000)
            _last_request_at = time.monotonic()

    base = cfg.get('upstream_base', '').rstrip('/')
    headers = {'Content-Type': 'application/json'}
    key = cfg.get('upstream_api_key', '')
    if key:
        headers['Authorization'] = f'Bearer {key}'

    payload = {
        "model": cfg.get('upstream_model', 'deepseek-v4-pro'),
        "messages": messages,
        "stream": False,
        "temperature": 0.1,
    }
    url = f"{base}/chat/completions"
    log(f"→ 转发上游 {url} model={payload['model']} messages={len(messages)} 条 "
        f"(system={len(messages[0]['content']) if messages and messages[0].get('role')=='system' else 0}字符)")
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    log(f"← 上游状态 {resp.status_code}")
    if resp.status_code >= 400:
        log(f"← 上游错误: {resp.text[:300]}")
        resp.raise_for_status()
    return resp.json()


def _log_request_shape(messages):
    """诊断：记录本轮请求的消息形态（只看结构，不落正文）。

    用来回答「工具到底执行了没有、结果有没有回传」——这是天气类问题里
    唯一无法从既有日志判断的环节：
    - 没有 tool 消息 → 客户端根本没回传工具结果（工具没执行，或执行后丢了）
    - 有 tool 消息但 tool_call_id 与 assistant.tool_calls 配不上 → 回传了，
      但 id 对不上，模型读不到对应关系
    - 有 tool 消息、长度也正常 → 结果到了网关，问题在 trim_messages 有没有注入
    """
    msgs = messages or []
    parts = []
    call_ids, tool_ids = set(), set()
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get('role', '?')
        content = m.get('content')
        if isinstance(content, list):
            clen = sum(len(str(p.get('text') or '')) for p in content if isinstance(p, dict))
        else:
            clen = len(content) if isinstance(content, str) else 0
        if role == 'tool':
            tid = str(m.get('tool_call_id') or '')
            tool_ids.add(tid)
            text = content if isinstance(content, str) else ''
            # S=命中来源标记 / C=命中正文标记 / R=命中富结果标记（WebSearch 自带正文）。
            # 这三个布尔直接暴露「防编造检测」在当前客户端的输出格式下还灵不灵。
            has_s = int(any(k in text for k in _SOURCE_MARKERS))
            has_c = int(any(k in text for k in _CONTENT_MARKERS))
            has_r = int(any(k in text for k in _RICH_RESULT_MARKERS))
            parts.append(f"tool#{tid[-6:] or '-'}({clen},S{has_s}C{has_c}R{has_r})")
        elif role == 'assistant':
            tcs = m.get('tool_calls') or []
            for tc in tcs:
                if isinstance(tc, dict):
                    call_ids.add(str(tc.get('id') or ''))
            parts.append(f"assistant({clen},{len(tcs)}call)")
        else:
            parts.append(f"{role}({clen})")
    shown = ' '.join(parts[:24])
    if len(parts) > 24:
        shown += f' …(+{len(parts) - 24})'
    log(f"← 请求原始消息 {len(msgs)} 条: {shown}")
    if call_ids or tool_ids:
        matched = len(call_ids & tool_ids)
        log(f"   tool_call_id 配对: assistant 发起 {len(call_ids)}，"
            f"tool 回传 {len(tool_ids)}，配对成功 {matched}")


def _client_tool_names(body):
    """取客户端在请求体里声明的工具名（OpenAI 格式的 tools 数组）。"""
    names = []
    for t in (body.get('tools') or []):
        if not isinstance(t, dict):
            continue
        fn = t.get('function') if isinstance(t.get('function'), dict) else t
        name = str((fn or {}).get('name') or '').strip()
        if name:
            names.append(name)
    return names


def _remap_to_client(tool_calls, client_tools):
    """把工具名改写成**客户端实际拥有的**那个名字。

    配置里的预设决定了发给模型的 schema（也就是参数形状），但客户端到底是
    WorkBuddy（PowerShell/WebSearch）还是 DSH（pwsh/web_search），网关不必猜——
    请求体里的 tools 数组就是答案。名字按别名组对齐，能对上就换成客户端的名字，
    这样即便预设选错了，调用也还能落地，而不是被判「未知工具」。
    """
    if not client_tools:
        return tool_calls
    for call in tool_calls:
        fn = call.get('function')
        if not isinstance(fn, dict):
            continue
        name = str(fn.get('name') or '')
        if not name or name in client_tools:
            continue
        alt = resolve_name(name, client_tools)
        if alt:
            fn['name'] = alt
            log(f"工具名适配: {name} → {alt}（按客户端实际声明）")
        else:
            log(f"⚠ 客户端没有与 {name} 等价工具，本轮调用大概率被拒；"
                f"建议在配置界面切换成该客户端的预设")
    return tool_calls


def _client_tool_schemas(body):
    """客户端声明的 {工具名: 参数 schema}，用来把参数按它的形状对齐。"""
    out = {}
    for t in (body.get('tools') or []):
        if not isinstance(t, dict):
            continue
        fn = t.get('function') if isinstance(t.get('function'), dict) else t
        name = str((fn or {}).get('name') or '').strip()
        if name:
            out[name] = (fn or {}).get('parameters') or {}
    return out


def _adapt_args_to_client(tool_calls, schemas, fallback_text=''):
    """按客户端 schema 修参数形状，避免「名字对了、参数被拒」。

    两个客户端最典型的差异：
      - web_search 要 `queries: []`，WebSearch 要 `query: "..."`（单个字符串）
      - web_fetch 只要 `url`，WebFetch 还必填 `prompt`
    网关侧统一生成一种形状，这里按客户端真实 schema 转换/裁剪/补必填。
    """
    if not schemas:
        return tool_calls
    for call in tool_calls:
        fn = call.get('function')
        if not isinstance(fn, dict):
            continue
        props = ((schemas.get(str(fn.get('name') or '')) or {}).get('properties') or {})
        if not props:
            continue
        required = (schemas.get(str(fn.get('name') or '')) or {}).get('required') or []
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(args, dict):
            continue
        changed = False

        # 1) query(字符串) ↔ queries(数组)：必须在裁剪之前做，
        #    否则客户端没声明的那一侧会先被当成多余参数删掉，再补就只能用提问兜底。
        if 'queries' in props and 'query' in props:
            pass
        elif 'queries' in props and isinstance(args.get('query'), str):
            args['queries'] = [args.pop('query')]
            changed = True
        elif 'query' in props and isinstance(args.get('queries'), list):
            got = args.pop('queries')
            args['query'] = str(got[0]) if got else str(fallback_text or '')[:200]
            changed = True

        # 2) 去掉客户端没声明的参数（严格 schema 会把多余字段判错）
        for key in [k for k in args if k not in props]:
            args.pop(key)
            changed = True

        # 3) 必填项缺失时用当前提问兜底（客户端会在 schema 校验阶段直接拒）
        for key in required:
            if key in args and args[key] not in (None, '', []):
                continue
            if key == 'prompt' and fallback_text:
                args['prompt'] = f'提取与这个问题相关的主要内容：{str(fallback_text)[:120]}'
            elif key == 'query' and fallback_text:
                args['query'] = str(fallback_text)[:200]
            elif key == 'queries' and fallback_text:
                args['queries'] = [str(fallback_text)[:200]]
            elif key == 'description':
                cmd = str(args.get('command') or '')
                args['description'] = f'Run shell command: {cmd[:60]}' if cmd else 'Run a shell command'
            elif isinstance(props.get(key), dict) and props[key].get('type') == 'array':
                args[key] = []
            else:
                args[key] = str(fallback_text or '')[:200]
            changed = True

        if changed:
            fn['arguments'] = json.dumps(args, ensure_ascii=False)
            log(f"参数按客户端 schema 对齐: {fn.get('name')} → "
                f"{json.dumps(args, ensure_ascii=False)[:120]}")
    return tool_calls


# ═══════════════════════════════════════════════════════
# 网关代搜：客户端搜索不可用时的兜底
# ═══════════════════════════════════════════════════════
#
# 为什么需要这一层：DSH 的 web_search 并不是本地执行的工具，而是内置插件
# `dsh-web-search-deepseek` 把查询发到 api.deepseek.com 的 Anthropic 端点
# （web_search_20250305），用的是客户端自己保存的 API Key。于是出现一个很反直觉的
# 割裂：聊天链路走「网关 → AFS → DeepSeek 网页端」，不需要 Key 也能通；搜索链路却
# 直连 api.deepseek.com，Key 一失效就固定 401，模型重试几次后只能凭记忆作答：
#
#     Error: DeepSeek API error (HTTP 401): Authentication Fails,
#     Your api key: ****78bN is invalid
#
# 提示词层面救不了（工具根本不经过网关）。所以网关自己搜：模型要 web_search，
# 网关就用必应搜、抓正文，把「标题+URL+正文」塞回对话让它作答。
_SEARCH_TOOL_NAMES = ('web_search', 'WebSearch', 'websearch', 'search', 'web-search')

# 客户端搜索失败的特征串（DSH 的 401 原文 + 该插件的错误码）
_CLIENT_SEARCH_FAIL_MARKERS = ('WEB_PROVIDER_ERROR', 'Authentication Fails',
                               'Your api key', 'api.deepseek.com/anthropic')

# 本进程内记住「客户端搜索坏了」：一轮失败后，后续请求直接代搜，不再白等一整轮
_CLIENT_SEARCH_BROKEN = False


def _note_client_search_failure(messages):
    """扫描请求历史里的 tool 结果：发现搜索工具报鉴权/服务错误就翻开关。"""
    global _CLIENT_SEARCH_BROKEN
    if _CLIENT_SEARCH_BROKEN:
        return
    for m in messages or []:
        if not isinstance(m, dict) or m.get('role') != 'tool':
            continue
        content = m.get('content')
        if isinstance(content, list):
            content = ' '.join(str(p.get('text') or '') for p in content
                               if isinstance(p, dict))
        if not isinstance(content, str):
            continue
        if any(mk in content for mk in _CLIENT_SEARCH_FAIL_MARKERS):
            _CLIENT_SEARCH_BROKEN = True
            log("⚠ 客户端搜索工具报了鉴权/服务错误（如 401）→ 后续搜索改由网关代执行"
                "（server_search_mode=auto；不想代搜就改成 off）")
            return


def _should_server_search(tool_calls, cfg):
    """本轮工具调用是否该由网关代搜。"""
    mode = str(cfg.get('server_search_mode') or 'auto').lower()
    if mode in ('off', '0', 'false', 'no', 'close', '关闭'):
        return False
    names = [str((c.get('function') or {}).get('name') or '') for c in tool_calls]
    # 只在「整轮都是搜索」时接管：搜索+读文件这种混合调用原样交回客户端，
    # 否则客户端那半边调用被吞掉，模型会以为工具已经执行过。
    if not names or any(n not in _SEARCH_TOOL_NAMES for n in names):
        return False
    if mode in ('on', '1', 'true', 'yes', 'force', '打开', '总是'):
        return True
    return _CLIENT_SEARCH_BROKEN


def _search_queries(tool_calls):
    """取出模型给的查询词，兼容 query / queries / q、字符串 / 数组、逗号串。"""
    out = []
    for c in tool_calls:
        args = (c.get('function') or {}).get('arguments')
        try:
            data = json.loads(args) if isinstance(args, str) else (args or {})
        except Exception:  # noqa: BLE001
            data = {}
        if not isinstance(data, dict):
            data = {}
        raw = data.get('queries') or data.get('query') or data.get('q') or ''
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if not isinstance(item, str):
                continue
            # 模型经常把多个查询用逗号灌进同一个字符串
            for part in re.split(r'[,，;；\n]+', item):
                part = part.strip()
                if part and part not in out:
                    out.append(part)
    return out


def _run_server_search(tool_calls, cfg, fetch_tool='web_fetch'):
    """代执行本轮搜索，返回可拼进提示的文本块；失败返回 ''（调用方回退）。"""
    queries = _search_queries(tool_calls)
    if not queries:
        return ''
    try:
        import server_search
    except Exception as e:  # noqa: BLE001
        log(f"⚠ 代搜模块不可用: {e}")
        return ''
    log(f"🔎 网关代搜: {queries}")
    try:
        block = server_search.build_block(queries, cfg, fetch_tool=fetch_tool)
    except Exception as e:  # noqa: BLE001
        log(f"⚠ 代搜异常: {e}")
        return ''
    if block:
        log(f"🔎 代搜完成: {len(block)} 字符（含网页正文）")
    else:
        log("⚠ 代搜没有取到结果，改回把工具调用交回客户端")
    return block


def _merge_search_blocks(msgs, blocks):
    """把代搜结果并进最后一条 user 消息的最前面（保持「历史 → 结果 → 当前问题」）。"""
    out = [dict(m) if isinstance(m, dict) else m for m in msgs]
    extra = '\n\n'.join(blocks)
    if out and isinstance(out[-1], dict) and out[-1].get('role') == 'user':
        out[-1] = {'role': 'user',
                   'content': f"{extra}\n\n{out[-1].get('content') or ''}"}
    else:
        out.append({'role': 'user', 'content': extra})
    return out


def handle_chat_completion(body):
    """OpenAI 兼容入口：精简 → 转发 → 解析 XML → 返回 tool_calls。

    健壮性：DeepSeek 网页端对相同提示有概率性行为——约 2/3 输出 XML 工具调用，
    其余偶发空响应或纯文本。因此失败时自动重试（最多 3 次），重试时追加
    「必须输出 XML」强提示，直到解析到工具调用或耗尽次数。
    """
    cfg = get_config()
    messages = body.get('messages', [])
    stream = bool(body.get('stream', False))

    # 单会话轮数闸门：超限就不再转发上游（详见 DEFAULT_CONFIG['max_total_rounds']）。
    # 返回的是普通 OpenAI 响应而非报错：客户端会把它当成"助手决定收尾"，
    # 正常结束这一轮；报 4xx/5xx 反而会触发客户端的重试逻辑，越拦越多请求。
    if isinstance(messages, list):
        round_no, over_limit = _bump_session_round(messages, cfg)
        limit = int(cfg.get('max_total_rounds', 0) or 0)
        if over_limit:
            log(f"⛔ 会话第 {round_no} 次请求已达上限 {limit} 轮，停止转发上游")
            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get('model') or cfg.get('upstream_model'),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": (
                            f"[网关] 本会话已执行 {limit} 轮工具调用，达到轮数上限，"
                            "已停止向上游发送请求。\n"
                            "如需继续，请新建一个会话；若已有足够材料，直接让我用现有结果收尾即可。"
                        ),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {},
            }
        if limit > 0 and round_no % 5 == 0:
            log(f"会话进度 {round_no}/{limit} 轮")

    _log_request_shape(messages)
    _note_client_search_failure(messages)

    # 客户端声明的工具名就是「它是谁」的指纹，顺带打印出来便于排查预设选错
    client_tools = _client_tool_names(body)
    client_schemas = _client_tool_schemas(body)
    if client_tools:
        shown = ', '.join(client_tools[:10]) + (' …' if len(client_tools) > 10 else '')
        log(f"客户端工具 {len(client_tools)} 个: {shown}")

    trimmed = trim_messages(messages, cfg)
    tools_prompt = config_tools_as_prompt(cfg)
    valid_names = [t.get('name') for t in cfg.get('tools', [])]

    if tools_prompt:
        if trimmed and trimmed[0].get('role') == 'system':
            trimmed[0] = {'role': 'system', 'content': trimmed[0]['content'] + '\n\n' + tools_prompt}
        else:
            trimmed.insert(0, {'role': 'system', 'content': tools_prompt})

    # user 长度是判断「工具结果有没有被注入」的旁证：同一问题若前一轮只搜到
    # 来源、这一轮补了正文，user 长度会明显变大；两边一样则说明结果没进提示。
    trimmed_user_chars = sum(
        len(m.get('content') or '') for m in trimmed if m.get('role') == 'user'
    )
    log(f"精简后消息 {len(trimmed)} 条, system={len(trimmed[0]['content'])}字符, "
        f"user={trimmed_user_chars}字符")
    log(f"精简工具: {valid_names}")

    # 上游模型是固定的：call_upstream() 只读配置里的 upstream_model，忽略请求体的 model，
    # 而响应里回填的却是**请求时的** model —— 两边不一致时客户端完全看不出来。
    # 明写一条日志，把"静默替换"变成可观测。
    requested_model = body.get('model')
    upstream_model = cfg.get('upstream_model')
    if requested_model and upstream_model and requested_model != upstream_model:
        log(f"⚠ model 不一致: 请求={requested_model} 实际转发={upstream_model}"
            f"（本网关只服务配置里的那一个上游模型）")

    # 空响应重试策略（实验结论）：
    #   - DeepSeek 网页端在 ~10 次/分钟连发后会临时限流（HTTP 200 但 content 空）
    #   - 限流窗口约 60 秒；窗口内立即重试全部无效，且增加风控权重
    #   - 因此：空响应后必须等待 RETRY_BACKOFF_MS 再重试；重试上限降到 2 次
    MAX_ATTEMPTS = 2
    RETRY_BACKOFF_MS = int(cfg.get('retry_backoff_ms', 60000))
    MAX_SEARCH_ROUNDS = max(0, int(cfg.get('server_search_max_rounds', 2) or 0))
    fetch_tool = _fetch_tool_name(cfg)
    last_text = ''
    retry_hint = ''
    search_blocks = []       # 代搜结果块，逐轮累积进提示
    search_rounds = 0
    attempt = 0

    def _sleep_and_hint(attempt_no):
        """准备下一次尝试：先等限流窗口，再追加「必须输出 XML」强提示。"""
        nonlocal retry_hint
        log(f"第 {attempt_no} 次重试前等待 {RETRY_BACKOFF_MS / 1000:.0f} 秒（规避限流窗口）...")
        time.sleep(RETRY_BACKOFF_MS / 1000)
        retry_hint = ('\n\n[重要] 这个请求必须通过调用工具完成。'
                      '请只输出下面这种格式的 XML，不要输出任何解释文字、不要空回复：\n'
                      '<tool_calls><invoke name="工具名">'
                      '<parameter name="参数名">参数值</parameter></invoke></tool_calls>')
        log(f"第 {attempt_no} 次重试（追加强提示）")

    while True:
        attempt += 1
        msgs = list(trimmed)
        if retry_hint:
            if msgs and msgs[0].get('role') == 'system':
                msgs[0] = {'role': 'system', 'content': msgs[0]['content'] + retry_hint}
            else:
                msgs.insert(0, {'role': 'system', 'content': retry_hint})
        if search_blocks:
            msgs = _merge_search_blocks(msgs, search_blocks)

        upstream = call_upstream(msgs, cfg)
        try:
            text = upstream['choices'][0]['message'].get('content') or ''
        except Exception:  # noqa: BLE001
            text = ''

        tool_calls = extract_tool_calls(text, valid_names)
        if tool_calls:
            # 代搜分支：客户端搜索工具不可用时，由网关自己搜并把结果塞回对话，
            # 而不是把一个注定失败的工具调用交回客户端（DSH 的 web_search 会 401）。
            if search_rounds < MAX_SEARCH_ROUNDS and _should_server_search(tool_calls, cfg):
                names = [c['function']['name'] for c in tool_calls]
                block = _run_server_search(tool_calls, cfg, fetch_tool)
                if block:
                    search_blocks.append(block)
                    search_rounds += 1
                    log(f"🔁 第 {search_rounds} 轮代搜完成（原调用 {names}），"
                        f"把结果交给模型重答")
                    continue
            # 参数补齐兜底：模型可能漏掉必填参数（如 bash 的 description），
            # 自动补上，避免 DSH 执行时参数校验失败。
            _fill_missing_required(tool_calls, cfg, _last_user_text(trimmed))
            # 再按客户端真实工具名与 schema 改写：预设只决定给模型看什么，
            # 落到客户端时必须是它认识的名字 + 它声明的参数形状。
            tool_calls = _remap_to_client(tool_calls, client_tools)
            tool_calls = _adapt_args_to_client(
                tool_calls, client_schemas, _last_user_text(trimmed))

            # 一轮多调用裁剪：见 DEFAULT_CONFIG['max_tool_calls_per_round'] 的说明。
            # 放在最后一步，是因为前面的代搜/补参/改名都按「模型给了几个」处理，
            # 只有真正交付给客户端的那一刻才需要收敛成客户端能接受的形状。
            max_calls = int(cfg.get('max_tool_calls_per_round', 1) or 0)
            if max_calls > 0 and len(tool_calls) > max_calls:
                total = len(tool_calls)
                dropped = [c['function']['name'] for c in tool_calls[max_calls:]]
                tool_calls = tool_calls[:max_calls]
                log(f"✂ 本轮 {total} 个调用按配置只交付 {max_calls} 个，"
                    f"丢弃 {dropped}（多个 name 会被客户端拼成一个而报错）")

            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": tool_calls,
            }
            finish = "tool_calls"
            log(f"✓ 第 {attempt} 次解析到 {len(tool_calls)} 个工具调用: "
                f"{[c['function']['name'] for c in tool_calls]}")
            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get('model', cfg.get('upstream_model')),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": upstream.get('usage', {}),
            }

        # 非空文本：先判定是不是「残缺的工具调用」——若是，绝不能当回答正文泄漏
        if text and text.strip():
            if looks_like_tool_call(text):
                log(f"⚠ 上游文本疑似工具调用但解析失败({len(text)}字符): {text[:300]}")
                try:  # 落盘完整原文，便于定位新的格式变体
                    with open(PARSE_FAIL_FILE, 'a', encoding='utf-8') as f:
                        f.write(f"\n{'=' * 60}\n{time.strftime('%F %T')} ({len(text)}字符)\n{text}\n")
                except Exception:  # noqa: BLE001
                    pass
                last_text = text
                if attempt < MAX_ATTEMPTS:
                    log("按解析失败处理，冷却后重试")
                    _sleep_and_hint(attempt + 1)
                    continue
                cleaned = strip_tool_markup(text, valid_names)
                message = {
                    "role": "assistant",
                    "content": cleaned or '(模型返回了无法解析的工具调用格式，本次未执行任何工具，请重试)',
                }
                log(f"⚠ 重试耗尽，返回清理后的文本 {len(message['content'])}字符")
                return {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body.get('model', cfg.get('upstream_model')),
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": upstream.get('usage', {}),
                }
            message = {"role": "assistant", "content": text}
            log(f"✓ 第 {attempt} 次得到文本回答(无需工具), {len(text)}字符")
            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get('model', cfg.get('upstream_model')),
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": upstream.get('usage', {}),
            }

        # 空响应才重试
        last_text = text
        log(f"第 {attempt} 次空响应，{'继续重试' if attempt < MAX_ATTEMPTS else '放弃'}")
        if attempt >= MAX_ATTEMPTS:
            break
        _sleep_and_hint(attempt + 1)

    # 全部空响应
    message = {"role": "assistant", "content": '(上游多次空响应，请稍后重试)'}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get('model', cfg.get('upstream_model')),
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {},
    }


load_config()
