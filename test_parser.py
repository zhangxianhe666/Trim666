#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ds-trim-gateway 工具调用解析回归测试。

    .venv/bin/python test_parser.py

覆盖真实线上遇到过的全部格式变体，重点是 DeepSeek 网页端原生的 DSML 标记
(`<||DSML|| invoke ...>`)，它的竖线常被输出成全角 U+FF5C、前缀后还可能带空格，
且闭合标签常退化成 `</web_search>` —— 这些正是历史上「偶发解析失败、
原始 XML 泄漏成回答正文」的根因。
"""
from __future__ import annotations

import os
import sys

import gateway_core as g

# 解析类用例统一用「旧名」工具集（bash/read/glob/web_search/read_page）：
# 它们只验证 XML 解析能力，是纯字符串处理，与平台、客户端版本无关。
# 参数补齐类用例则用 CFG（真实配置，工具名已是客户端真名 PowerShell/WebFetch…），
# 靠 resolve_name 的别名组对齐——这正好覆盖「旧名能不能落到新 schema 上」。
PARSE_TOOLS = ['bash', 'read', 'glob', 'web_search', 'read_page']
# 默认预设（WorkBuddy）的 shell 工具名，用于「必填参数补齐」用例
SHELL = g.TOOL_PRESETS[g.DEFAULT_PRESET_NAME]['tools'][0]['name']
CFG = g.DEFAULT_CONFIG
FAILED = []


def check(label, cond, detail=''):
    print(f"{'✓' if cond else '✗'} {label}" + (f"  → {detail}" if detail and not cond else ''))
    if not cond:
        FAILED.append(label)


def parse(text):
    return g.extract_tool_calls(text, PARSE_TOOLS)


def one(text):
    """解析并断言恰好一个调用，返回 (name, args_dict)。"""
    calls = parse(text)
    if len(calls) != 1:
        return None, None
    fn = calls[0]['function']
    import json
    return fn['name'], json.loads(fn['arguments'])


# 真实捕获：DeepSeek 原生 DSML，全角竖线 + 前缀后空格 + 退化闭合标签
REAL_DSML = (
    '<\uff5c\uff5cDSML\uff5c\uff5c calls>\n'
    '<\uff5c\uff5cDSML\uff5c\uff5c invoke name="web_search">'
    '<parameter name="query">A股今日收盘 上证指数 深证成指 点位 涨跌幅 成交额</parameter>'
    '<parameter name="query">上证指数 今日收盘 成交额 2026年9月10日</parameter>'
    '<parameter name="query">深证成指 今日收盘 涨跌幅 成交额</parameter>'
    '</web_search></tool_calls>'
)

CASES = {
    '标准': (
        '<tool_calls><invoke name="web_search">'
        '<parameter name="query">A股行情</parameter></invoke></tool_calls>',
        'web_search', {'query': 'A股行情'}),
    'CHAT2API 前缀': (
        '<|CHAT2API|tool_calls><|CHAT2API|invoke name="read">'
        '<|CHAT2API|parameter name="file_path">/tmp/a</|CHAT2API|parameter>'
        '</|CHAT2API|invoke></|CHAT2API|tool_calls>',
        'read', {'file_path': '/tmp/a'}),
    'DSML ASCII 前缀': (
        '<||DSML||tool_calls><||DSML||invoke name="read">'
        '<||DSML||parameter name="file_path">/tmp/a</||DSML||parameter>'
        '</||DSML||invoke></||DSML||tool_calls>',
        'read', {'file_path': '/tmp/a'}),
    '缺 invoke 开标签': (
        '<web_search"><parameter name="query">A股 今日 行情</parameter>'
        '</invoke></tool_calls>',
        'web_search', {'query': 'A股 今日 行情'}),
    '缺 invoke + name 标签': (
        '<name="web_search"><parameter name="query">A股今日</parameter>'
        '</invoke></tool_calls>',
        'web_search', {'query': 'A股今日'}),
    '属性无引号': (
        '<invoke name=web_search><parameter name=query>A股</parameter></invoke>',
        'web_search', {'query': 'A股'}),
    '工具名在体内': (
        '<invoke ><name="read_page"><parameter name="url">https://x.com</parameter>'
        '</invoke>',
        'read_page', {'url': 'https://x.com'}),
    '参数用 name 标签': (
        '<invoke name="web_search"><name="query">A股行情</name></invoke>',
        'web_search', {'query': 'A股行情'}),
    '无闭合标签': (
        '<tool_calls><invoke name="web_search"><parameter name="query">A股行情',
        'web_search', {'query': 'A股行情'}),
    'bash 双参数': (
        '<tool_calls><invoke name="bash"><parameter name="command">ls -la</parameter>'
        '<parameter name="description">列目录</parameter></invoke></tool_calls>',
        'bash', {'command': 'ls -la', 'description': '列目录'}),
}

print('── 单调用解析 ──')
for label, (text, want_name, want_args) in CASES.items():
    name, args = one(text)
    check(label, name == want_name and args == want_args, f'{name} {args}')

print('\n── 真实 DSML 捕获 ──')
name, args = one(REAL_DSML)
check('DSML 解析为 web_search',
      name == 'web_search' and isinstance((args or {}).get('query'), list)
      and len(args['query']) == 3, f'{name} {args}')

calls = parse(REAL_DSML)
g._fill_missing_required(calls, CFG, '查看A股今日行情')
import json  # noqa: E402
final = json.loads(calls[0]['function']['arguments']) if calls else {}
# 客户端 WebSearch.query 只收单个字符串，模型给 3 条 query 时只能保留第一条；
# 若仍按旧 schema 输出 queries 数组，会被客户端 schema 校验直接拒绝。
q = final.get('query')
check('多条 query 只保留首个（WebSearch.query 为字符串）',
      isinstance(q, str) and bool(q), str(final))
check('未声明参数 queries 已被裁剪', 'queries' not in final, str(final))

print('\n── 多调用 ──')
calls = parse('<tool_calls><invoke name="web_search"><parameter name="query">a</parameter>'
              '</invoke><invoke name="read"><parameter name="file_path">/b</parameter>'
              '</invoke></tool_calls>')
check('两个调用都识别',
      [c['function']['name'] for c in calls] == ['web_search', 'read'], str(calls))

print('\n── 不应误判为工具调用 ──')
for label, text in {
    '普通中文回答': '今天上证指数收于3939.33点，下跌12.18点。',
    '讲解格式的文字': '调用工具请使用 <tool_calls> 标签。',
}.items():
    check(label, parse(text) == [] and not g.looks_like_tool_call(text), '误判')

print('\n── 泄漏防护 ──')
check('真实 DSML 被识别为工具调用', g.looks_like_tool_call(REAL_DSML))
check('普通回答不触发重试', not g.looks_like_tool_call('上证指数收于3939.33点。'))
check('教学文字不触发重试', not g.looks_like_tool_call('调用工具请使用 <tool_calls> 标签。'))

print('\n── 参数兜底 ──')
calls = parse('<tool_calls><invoke name="web_search"><parameter name="queries"></parameter>'
              '</invoke></tool_calls>')
g._fill_missing_required(calls, CFG, '查看A股今日行情')
q = json.loads(calls[0]['function']['arguments']).get('query') if calls else None
check('空查询用用户提问兜底', q == '查看A股今日行情', str(q))

calls = parse(f'<tool_calls><invoke name="{SHELL}"><parameter name="command">ls -la</parameter>'
              '</invoke></tool_calls>')
g._fill_missing_required(calls, CFG)
d = json.loads(calls[0]['function']['arguments']).get('description') if calls else None
check(f'{SHELL} 缺 description 自动补齐', isinstance(d, str) and bool(d), str(d))

print('\n── 历史渲染清理 ──')
noise = g._clean_xml_noise(REAL_DSML)
check('DSML 标记被清掉', 'DSML' not in noise and '\uff5c' not in noise, repr(noise[:80]))
check('参数正文保留', '成交额' in noise, repr(noise[:80]))

print('\n── 数据缺口检测（防编造数字）──')
SRC_ONLY = ('### A股 今日行情\n\nFirecrawl returned 8 ranked result(s) for "A股". '
            'Read items for the sources.\n\nSources:\n'
            '- [上证指数](https://quote.eastmoney.com/zs000001.html)')
PAGE = '上证指数 3938.94 -12.57(-0.32%)\n\nContent:\n\n[**上证**](https://x.com) 4018亿元'
check('只有来源列表 → 触发强制取数', bool(g._data_gap_hint(SRC_ONLY)))
check('已有正文页 → 不触发', not g._data_gap_hint(PAGE))
check('来源+正文 → 不触发', not g._data_gap_hint(SRC_ONLY + '\n\n' + PAGE))
check('无历史 → 不触发', not g._data_gap_hint(''))
# WorkBuddy 的 WebSearch 返回正文摘录（含表格/数值），不是纯链接列表：
# 旧判定只认 DSH 的 Sources: 形态，在这里永远不命中，会白跑一轮 WebFetch。
RICH = ('# Search Results for "重庆天气"\n\n## 1. [墨迹天气](https://x.com)\n'
        '当前阴,21.0°C,湿度77%\n\n*Found 5 results • Provider: 1*')
check('客户端富搜索结果 → 不触发强制取数', not g._data_gap_hint(RICH))

print('\n── 客户端工具预设（Windows-only）──')
WB = 'WorkBuddy（Windows）'
DSH = 'DSH（Windows）'
check('两个客户端预设都存在', WB in g.TOOL_PRESETS and DSH in g.TOOL_PRESETS)

check('已不再提供 macOS/Linux 预设',
      not any('macOS' in k or 'Linux' in k for k in g.TOOL_PRESETS),
      str(list(g.TOOL_PRESETS)))

wb_tools = [t['name'] for t in g.TOOL_PRESETS[WB]['tools']]
dsh_tools = [t['name'] for t in g.TOOL_PRESETS[DSH]['tools']]
check('WorkBuddy 预设工具名正确',
      wb_tools == ['PowerShell', 'Read', 'Glob', 'Write', 'Edit', 'WebSearch', 'WebFetch'],
      str(wb_tools))
check('DSH 预设工具名正确',
      dsh_tools == ['pwsh', 'read', 'glob', 'write', 'edit', 'web_search', 'web_fetch'],
      str(dsh_tools))

# 写/改文件工具：没有它，模型只能用 PowerShell here-string 落盘长文本
for label, key, wname, ename in (('WorkBuddy', WB, 'Write', 'Edit'),
                                 ('DSH', DSH, 'write', 'edit')):
    by_name = {t['name']: t for t in g.TOOL_PRESETS[key]['tools']}
    w, e = by_name.get(wname), by_name.get(ename)
    check(f'{label} 有写文件工具且参数面正确',
          bool(w) and w['parameters']['required'] == ['file_path', 'content'], str(w))
    check(f'{label} 有改文件工具且参数面正确',
          bool(e) and e['parameters']['required'] == ['file_path', 'old_string', 'new_string'],
          str(e))

# 提示词必须告诉模型新工具怎么用（网关不把 schema 发上游，提示词是唯一入口）
_p = g.TOOL_PRESETS[WB]['system_prompt']
check('提示词提到写/改文件工具', 'Write' in _p and 'Edit' in _p, _p[-300:])
check('提示词给出 docx 读取路线', '.docx' in _p and 'Expand-Archive' in _p, _p[-300:])
check('提示词给出 pptx 生成路线', 'PowerPoint.Application' in _p and 'SaveAs' in _p, _p[-300:])
# 实测教训：PowerShell 默认按 GBK 输出中文，回传后是乱码，模型会误判"文件没生成"而反复重做
check('提示词提醒 shell 输出中文要切 UTF-8', 'OutputEncoding' in _p, _p[-200:])
check('提示词未被 max_system_chars 截断',
      len(_p) <= g.DEFAULT_CONFIG['max_system_chars'],
      f"{len(_p)} > {g.DEFAULT_CONFIG['max_system_chars']}")

for label, key, shell in (('WorkBuddy', WB, 'PowerShell'), ('DSH', DSH, 'pwsh')):
    sh = g.TOOL_PRESETS[key]['tools'][0]
    req = sh['parameters'].get('required')
    check(f'{label} shell 名与工具名一致', sh['name'] == shell, sh['name'])
    check(f'{label} shell 必填 command+description',
          req == ['command', 'description'], str(req))

# 两个客户端的搜索/读页参数面不同，这是「名字对上了也可能被 schema 拒」的根源
check('WorkBuddy 搜索收单 query 字符串',
      [t['parameters']['required'] for t in g.TOOL_PRESETS[WB]['tools']
       if t['name'] == 'WebSearch'] == [['query']])
check('WorkBuddy 读网页必填 url+prompt',
      [t['parameters']['required'] for t in g.TOOL_PRESETS[WB]['tools']
       if t['name'] == 'WebFetch'] == [['url', 'prompt']])
check('DSH 搜索收 queries 数组',
      [t['parameters']['required'] for t in g.TOOL_PRESETS[DSH]['tools']
       if t['name'] == 'web_search'] == [['queries']]
      and [t['parameters']['properties']['queries']['type']
           for t in g.TOOL_PRESETS[DSH]['tools'] if t['name'] == 'web_search'] == ['array'])
check('DSH 读网页只要 url',
      [t['parameters']['required'] for t in g.TOOL_PRESETS[DSH]['tools']
       if t['name'] == 'web_fetch'] == [['url']])
check('Read/Glob 的 required 与客户端一致',
      [t['parameters']['required'] for t in g.TOOL_PRESETS[WB]['tools']
       if t['name'] in ('Read', 'Glob')] == [['file_path'], ['pattern']]
      and [t['parameters']['required'] for t in g.TOOL_PRESETS[DSH]['tools']
           if t['name'] in ('read', 'glob')] == [['file_path'], ['pattern']])

# system 提示里的工具名必须跟本预设一致，否则模型会去调客户端不存在的工具
check('WorkBuddy 提示词只提自己的工具名',
      'WebSearch' in g.TOOL_PRESETS[WB]['system_prompt']
      and 'web_search' not in g.TOOL_PRESETS[WB]['system_prompt'])
check('DSH 提示词只提自己的工具名',
      'web_search' in g.TOOL_PRESETS[DSH]['system_prompt']
      and 'WebSearch' not in g.TOOL_PRESETS[DSH]['system_prompt'])
check('两套提示词都要求 PowerShell 语法',
      all('Get-ChildItem' in g.TOOL_PRESETS[k]['system_prompt'] for k in (WB, DSH)))
check('两套提示词都警告不要用 POSIX 命令',
      all('不要用 ls / cat / grep / find' in g.TOOL_PRESETS[k]['system_prompt']
          for k in (WB, DSH)))
check('预设可被回显识别（round-trip）',
      g.detect_preset_name(g.TOOL_PRESETS[WB]['tools']) == WB
      and g.detect_preset_name(g.TOOL_PRESETS[DSH]['tools']) == DSH)
check('默认预设是 WorkBuddy', g.DEFAULT_PRESET_NAME == WB, g.DEFAULT_PRESET_NAME)

# 旧配置兼容：老版本用的 pwsh/read_page 也要能落到当前预设的工具上
check('旧名 pwsh/read_page 能映射到 WorkBuddy 工具',
      g.resolve_name('pwsh', wb_tools) == 'PowerShell'
      and g.resolve_name('read_page', wb_tools) == 'WebFetch',
      f"{g.resolve_name('pwsh', wb_tools)} / {g.resolve_name('read_page', wb_tools)}")
check('DSH 工具名在 WorkBuddy 下不会被误认',
      g.resolve_name('web_search', wb_tools) == 'WebSearch'
      and g.resolve_name('WebFetch', wb_tools) == 'WebFetch')
check('提示词的读网页工具名跟随配置',
      g._fetch_tool_name({'tools': g.TOOL_PRESETS[WB]['tools']}) == 'WebFetch'
      and g._fetch_tool_name({'tools': g.TOOL_PRESETS[DSH]['tools']}) == 'web_fetch')

# 客户端声明的工具名 = 「它是谁」的指纹。网关据此把调用改写成客户端认识的名字，
# 这样即使预设选错，调用也能落地，而不是被当成未知工具拒掉。
print('\n── 按客户端工具名适配 ──')
_wb_body = {'tools': [{'type': 'function', 'function': {'name': n}}
                      for n in wb_tools]}
_dsh_body = {'tools': [{'type': 'function', 'function': {'name': n}}
                       for n in dsh_tools]}
check('能从请求体读出客户端工具名',
      g._client_tool_names(_wb_body) == wb_tools
      and g._client_tool_names(_dsh_body) == dsh_tools)
check('无 tools 字段时返回空', g._client_tool_names({}) == [])

_calls = [{'function': {'name': 'PowerShell', 'arguments': '{}'}},
          {'function': {'name': 'WebSearch', 'arguments': '{}'}},
          {'function': {'name': 'WebFetch', 'arguments': '{}'}}]
_fixed = g._remap_to_client([dict(c, function=dict(c['function'])) for c in _calls], dsh_tools)
check('预设选成 WorkBuddy 但客户端是 DSH → 名字被改写',
      [c['function']['name'] for c in _fixed] == ['pwsh', 'web_search', 'web_fetch'],
      str([c['function']['name'] for c in _fixed]))

_calls2 = [{'function': {'name': 'pwsh', 'arguments': '{}'}}]
_fixed2 = g._remap_to_client(_calls2, wb_tools)
check('反过来（DSH 名字 → WorkBuddy 客户端）也能改写',
      _fixed2[0]['function']['name'] == 'PowerShell', _fixed2[0]['function']['name'])

_same = [{'function': {'name': 'Read', 'arguments': '{}'}}]
check('名字本来就对时不改动',
      g._remap_to_client([dict(c, function=dict(c['function'])) for c in _same],
                         wb_tools)[0]['function']['name'] == 'Read')

# 写/改文件：预设选错时也得能落到对的名字上（否则模型写了半天，客户端认不出）
for mdl, client_tools, expect in (
        ('Write', dsh_tools, 'write'), ('write', wb_tools, 'Write'),
        ('Edit', dsh_tools, 'edit'), ('edit', wb_tools, 'Edit')):
    _r = g._remap_to_client([{'function': {'name': mdl, 'arguments': '{}'}}], client_tools)
    check(f'写/改文件工具名跨客户端改写（{mdl} → {expect}）',
          _r[0]['function']['name'] == expect, _r[0]['function']['name'])

_unknown = [{'function': {'name': 'WebFetch', 'arguments': '{}'}}]
check('客户端完全没有等价工具时保持原样（只告警，不瞎改）',
      g._remap_to_client([dict(c, function=dict(c['function'])) for c in _unknown],
                         ['Bash', 'Read']) [0]['function']['name'] == 'WebFetch')

print('\n── 参数按客户端 schema 对齐 ──')
_schemas = g._client_tool_schemas(_dsh_body | {'tools': [
    {'type': 'function', 'function': {
        'name': 'web_search',
        'parameters': {'type': 'object', 'properties': {'queries': {'type': 'array'}},
                       'required': ['queries']}}},
    {'type': 'function', 'function': {
        'name': 'web_fetch',
        'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}},
                       'required': ['url']}}},
]})
check('能读出客户端参数 schema', set(_schemas) == {'web_search', 'web_fetch'}, str(list(_schemas)))

_c = [{'function': {'name': 'web_search', 'arguments': json.dumps({'query': '重庆天气'})}}]
_out = g._adapt_args_to_client(_c, _schemas, '重庆天气')
_a = json.loads(_out[0]['function']['arguments'])
check('query 字符串 → queries 数组', _a.get('queries') == ['重庆天气'] and 'query' not in _a, str(_a))

_c = [{'function': {'name': 'web_fetch',
                    'arguments': json.dumps({'url': 'https://a.com', 'prompt': '气温'})}}]
_out = g._adapt_args_to_client(_c, _schemas, '重庆天气')
_a = json.loads(_out[0]['function']['arguments'])
check('多余参数被裁掉（DSH 的 web_fetch 不认 prompt）',
      _a == {'url': 'https://a.com'}, str(_a))

_wb_schemas = g._client_tool_schemas(_wb_body | {'tools': [
    {'type': 'function', 'function': {
        'name': 'WebSearch',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                       'required': ['query']}}},
    {'type': 'function', 'function': {
        'name': 'WebFetch',
        'parameters': {'type': 'object',
                       'properties': {'url': {'type': 'string'}, 'prompt': {'type': 'string'}},
                       'required': ['url', 'prompt']}}},
]})
_c = [{'function': {'name': 'WebSearch', 'arguments': json.dumps({'queries': ['A股', '上证']})}},
      {'function': {'name': 'WebFetch', 'arguments': json.dumps({'url': 'https://a.com'})}}]
_out = g._adapt_args_to_client(_c, _wb_schemas, '查上证指数')
_a0 = json.loads(_out[0]['function']['arguments'])
_a1 = json.loads(_out[1]['function']['arguments'])
check('queries 数组 → query 字符串（取第一条）', _a0.get('query') == 'A股', str(_a0))
check('缺 prompt 时用提问兜底', bool(_a1.get('prompt')), str(_a1))

# 日志落盘路径：Windows 没有 /tmp，默认目录必须随平台切换，否则写日志静默失败
# （表现为 UI 日志面板空白、parsefail 文件不存在，且不报任何错）
check('日志默认路径为绝对路径且含文件名',
      os.path.isabs(g.LOG_FILE) and g.LOG_FILE.endswith('ds-gw-server.log'),
      g.LOG_FILE)
check('parsefail 默认路径为绝对路径且含文件名',
      os.path.isabs(g.PARSE_FAIL_FILE)
      and g.PARSE_FAIL_FILE.endswith('ds-gw-parsefail.log'),
      g.PARSE_FAIL_FILE)
check('POSIX 下日志仍落在 /tmp（不改变既有排查流程）',
      g.IS_WINDOWS or g.LOG_FILE.startswith('/tmp/'),
      g.LOG_FILE)
check('Windows 下日志不用 POSIX 的 /tmp',
      (not g.IS_WINDOWS) or not g.LOG_FILE.startswith('/tmp'),
      g.LOG_FILE)

print('\n── 对话历史（多轮上下文）──')
# 复刻一次真实的 DSH 多轮会话：问行情 → 工具调用 → 回答 → 追问整理 → 再追问
SRC_ONLY_RESULT = (
    '### A股 今日行情\n\n'
    'Firecrawl returned 8 ranked result(s) for "A股". Read items for the sources.\n\n'
    'Sources:\n- [上证指数](https://quote.eastmoney.com/zs000001.html)')
CONVO = [
    {'role': 'system', 'content': 'You are a coding agent. ' + 'x' * 400},
    {'role': 'user', 'content': '查一下今天A股行情'},
    {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call_1', 'type': 'function',
         'function': {'name': 'web_search', 'arguments': '{"queries":["A股今日行情"]}'}}]},
    {'role': 'tool', 'tool_call_id': 'call_1', 'content': SRC_ONLY_RESULT},
    {'role': 'assistant', 'content': '根据数据：上证指数收于 3939.33 点，深证成指收于 13659.09 点。'},
    {'role': 'user', 'content': '我想让你把刚才那个结果整理成表格'},
    {'role': 'assistant', 'content': '| 指数 | 点位 |\n| --- | --- |\n| 上证 | 3939.33 |\n| 深证 | 13659.09 |'},
    {'role': 'user', 'content': '那成交额呢？'},
    # DSH 追在提问之后的运行时上下文（应被当作噪音跳过，不能当成"当前提问"）
    {'role': 'user', 'content': ('Current runtime context. blah\n\n'
                                 'Current DSH file policy: workspace-write.\n\n'
                                 'Approval policy: ask.')},
]
CONVO_Q_INDEX = 7

trimmed = g.trim_messages(CONVO, CFG)
body = trimmed[-1]['content']

check('精简后仍是 1 条 system + 1 条 user',
      [m['role'] for m in trimmed] == ['system', 'user'], str([m['role'] for m in trimmed]))
check('当前提问有独立标记', f'{g._CURRENT_Q_MARKER}\n那成交额呢？' in body, repr(body[-200:]))
check('运行时上下文不算提问', 'Approval policy' not in body, repr(body[-200:]))
check('历史里保留了中间那句追问', '我想让你把刚才那个结果整理成表格' in body, repr(body[:300]))
check('历史里保留了助手此前的回答', '3939.33' in body, repr(body[:300]))
check('历史条目带角色标签',
      body.count('用户: ') == 2 and body.count('助手: ') == 2,
      f"用户 {body.count('用户: ')} / 助手 {body.count('助手: ')}")
check('只有 tool_calls 的 assistant 不写成对话',
      'web_search' not in body.split('[此前对话历史]')[1].split('[此前已执行的工具结果]')[0],
      repr(body.split('[此前对话历史]')[1].split('[此前已执行的工具结果]')[0]))
check('工具结果仍单独成段', '[此前已执行的工具结果]' in body, repr(body[:300]))
check('当前提问不在历史里重复', body.count('那成交额呢？') == 1,
      str(body.count('那成交额呢？')))
check('段落顺序：历史 → 工具结果 → 当前提问',
      body.index('[此前对话历史]') < body.index('[此前已执行的工具结果]')
      < body.index(g._CURRENT_Q_MARKER))
check('参数兜底只取当前提问（不含历史）',
      g._last_user_text(trimmed) == '那成交额呢？', repr(g._last_user_text(trimmed)))

OFF_CFG = dict(CFG)
OFF_CFG['history_include_dialogue'] = False
body_off = g.trim_messages(CONVO, OFF_CFG)[-1]['content']
check('开关关闭后不带对话历史',
      '[此前对话历史]' not in body_off and '把刚才那个结果整理成表格' not in body_off,
      repr(body_off[:200]))
check('开关关闭后当前提问仍在', '那成交额呢？' in body_off, repr(body_off[:200]))

d = g._collect_dialogue_history(CONVO, stop_index=CONVO_Q_INDEX, max_msgs=2)
check('条数上限只留最近 2 条',
      d.count('用户: ') == 1 and d.count('助手: ') == 1, repr(d))

d = g._collect_dialogue_history(
    [{'role': 'user', 'content': 'A' * 5000}, {'role': 'user', 'content': 'B' * 5000}],
    stop_index=2, max_msgs=6, per_msg_chars=100, total_chars=1000)
check('单条超长被截断', d.count('…(已截断)') == 2 and len(d) < 500, str(len(d)))

d = g._collect_dialogue_history(
    [{'role': 'user', 'content': f'q{i}' + 'X' * 400} for i in range(10)],
    stop_index=10, max_msgs=10, per_msg_chars=1000, total_chars=500)
check('总字符预算生效', d.count('用户: ') == 1, f'{d.count("用户: ")} 条 / {len(d)} 字符')

d = g._collect_dialogue_history(
    [{'role': 'user', 'content': '<tool_calls><invoke name="web_search">'
                                 '<parameter name="query">a</parameter></invoke></tool_calls>'}],
    stop_index=1)
check('历史里的 XML 残留被清洗', 'invoke' not in d and 'tool_calls' not in d, repr(d))

check('没有历史时不注入空段落',
      g.trim_messages([{'role': 'user', 'content': '你好'}], CFG)[-1]['content']
      == f'{g._CURRENT_Q_MARKER}\n你好',
      repr(g.trim_messages([{'role': 'user', 'content': '你好'}], CFG)[-1]['content']))

print('\n── 本地图片引用（路线 A）──')
import tempfile as _tf  # noqa: E402

_img_dir = _tf.mkdtemp(prefix='ds-gw-img-')
_ws = os.path.join(_img_dir, '测试工作区')
os.makedirs(_ws, exist_ok=True)
# 一张最小合法 PNG（魔数 + IHDR 够了，网关只嗅探文件头）
_png = os.path.join(_ws, '图片_20251204161442.png')
with open(_png, 'wb') as _f:
    _f.write(b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
# 同目录一个「伪装成 png 的文本文件」，必须被魔数挡掉
_fake = os.path.join(_ws, '假的.png')
with open(_fake, 'wb') as _f:
    _f.write(b'this is not an image at all')
# 一个非图片扩展名，扩展名初筛就该挡掉
_txt = os.path.join(_ws, '说明.txt')
with open(_txt, 'w', encoding='utf-8') as _f:
    _f.write('hello')
# workspace 之外的一张图（用于 restrict 开关）
_outside = os.path.join(_img_dir, '外面.png')
with open(_outside, 'wb') as _f:
    _f.write(b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)

# 网关会做 realpath 归一（macOS 上 /var → /private/var），断言按归一后的路径比
_png_real = os.path.realpath(_png)
_outside_real = os.path.realpath(_outside)

def _ctx(ws_path):
    return ('Current runtime context. blah\n\n'
            'Current DSH file policy: workspace-write. Any available operation enforced by the '
            f'DSH file sandbox may modify files under the session workspace: "{ws_path}". '
            'Some platform temporary areas may also be writable.\n\n'
            'Approval policy: ask.')

def _convo(q, ref, ws_path=_ws):
    return [
        {'role': 'system', 'content': 'agent'},
        {'role': 'user', 'content': q},
        {'role': 'user', 'content': f'<workspace-reference path="{ref}" kind="file" />'},
        {'role': 'user', 'content': _ctx(ws_path)},
    ]

body = g.trim_messages(_convo('@图片_20251204161442.png 图里是什么内容',
                              '图片_20251204161442.png'), CFG)[-1]['content']
check('相对引用按运行时 workspace 解析成绝对路径',
      f"[[local-image:{_png_real}]]" in body, repr(body[-260:]))
check('图片标记跟在当前提问之后',
      body.index(g._CURRENT_Q_MARKER) < body.index('[[local-image:'),
      repr(body[-260:]))
check('纯标签消息不会顶掉真正的提问',
      '图里是什么内容' in body and f'{g._CURRENT_Q_MARKER}\n@图片' in body, repr(body[-260:]))
check('原始 workspace-reference 标签保留给模型看文件名',
      '<workspace-reference path="图片_20251204161442.png"' in body, repr(body[-260:]))
check('参数兜底不含图片标记',
      g._last_user_text(g.trim_messages(_convo('@图片_20251204161442.png 图里是什么内容',
                                               '图片_20251204161442.png'), CFG))
      == '@图片_20251204161442.png 图里是什么内容',
      repr(g._last_user_text(g.trim_messages(_convo('@图片_20251204161442.png 图里是什么内容',
                                                    '图片_20251204161442.png'), CFG))))

body = g.trim_messages(_convo('安全?', '假的.png'), CFG)[-1]['content']
check('伪装成 .png 的非图片文件被魔数挡掉', '[[local-image:' not in body, repr(body[-200:]))

body = g.trim_messages(_convo('读一下', '说明.txt'), CFG)[-1]['content']
check('非图片扩展名不产生图片标记', '[[local-image:' not in body, repr(body[-200:]))

body = g.trim_messages(_convo('看看', '不存在的图.png'), CFG)[-1]['content']
check('文件不存在时不产生图片标记', '[[local-image:' not in body, repr(body[-200:]))

body = g.trim_messages(_convo('看看这张', _outside), CFG)[-1]['content']
check('绝对路径引用可直接解析',
      f"[[local-image:{_outside_real}]]" in body, repr(body[-200:]))

_restrict = dict(CFG)
_restrict['local_image_restrict_to_workspace'] = True
body = g.trim_messages(_convo('看看这张', _outside), _restrict)[-1]['content']
check('开启 workspace 限制后越界图片被拒', '[[local-image:' not in body, repr(body[-200:]))
body = g.trim_messages(_convo('@图片_20251204161442.png 看看',
                              '图片_20251204161442.png'), _restrict)[-1]['content']
check('开启限制后 workspace 内图片仍可用', f"[[local-image:{_png_real}]]" in body, repr(body[-200:]))

_explicit = dict(CFG)
_explicit['workspace_root'] = _ws
body = g.trim_messages([
    {'role': 'user', 'content': '@图片_20251204161442.png 图里是什么'},
    {'role': 'user', 'content': '<workspace-reference path="图片_20251204161442.png" kind="file" />'},
], _explicit)[-1]['content']
check('显式 workspace_root 可替代运行时上下文定位',
      f"[[local-image:{_png_real}]]" in body, repr(body[-200:]))

_off = dict(CFG)
_off['local_image_enabled'] = False
body = g.trim_messages(_convo('@图片_20251204161442.png 图里是什么内容',
                              '图片_20251204161442.png'), _off)[-1]['content']
check('开关关闭后完全不注入图片标记', '[[local-image:' not in body, repr(body[-200:]))

# 历史轮次里的旧引用不该被重复附加（否则每轮都会重传上一轮的图）
body = g.trim_messages([
    {'role': 'user', 'content': '上一轮'},
    {'role': 'user', 'content': '<workspace-reference path="图片_20251204161442.png" kind="file" />'},
    {'role': 'assistant', 'content': '好的，我看到图了'},
    {'role': 'user', 'content': '@图片_20251204161442.png 再看一次'},
    {'role': 'user', 'content': '<workspace-reference path="图片_20251204161442.png" kind="file" />'},
    {'role': 'user', 'content': _ctx(_ws)},
], CFG)[-1]['content']
check('只注入当前轮的引用（历史轮不重复附图）',
      body.count('[[local-image:') == 1, str(body.count('[[local-image:')))

# 用户只发一张图、不打字：仍要产出一条 user 消息
body = g.trim_messages([
    {'role': 'user', 'content': '<workspace-reference path="图片_20251204161442.png" kind="file" />'},
    {'role': 'user', 'content': _ctx(_ws)},
], CFG)[-1]['content']
check('只 @ 图片不打字时仍产出图片消息',
      f"[[local-image:{_png_real}]]" in body, repr(body[-200:]))

# ── DSH 的 @ 提及：文件名里带逗号/等号/与号 ──
# 真实案例：用户拖进来的图是从网页另存的，名字是
#   u=2448247082,481968747&fm=253&app=138&f=JPEG.jpg
# 老实现的字符类把 ASCII 逗号当分隔符排除掉，于是整轮都没认出这张图，
# 模型只能 read 一个 jpg，拿到 binary file 错误后回答「我无法查看图片」。
_AT_CASES = [
    ('@u=2448247082,481968747&fm=253&app=138&f=JPEG.jpg 理解内容',
     ['u=2448247082,481968747&fm=253&app=138&f=JPEG.jpg']),
    ('@图.png，看看', ['图.png']),
    ('@图.png)', ['图.png']),
    ('@"带 空格 的图.png" 是什么', ['带 空格 的图.png']),
    ('@说明.txt 读一下', []),
    ('邮箱 a@b.com 不是引用', []),
]
for _src, _want in _AT_CASES:
    check(f'@ 提及解析：{_src[:24]}',
          g._at_image_candidates(_src) == _want, str(g._at_image_candidates(_src)))

check('裸文件名也能当图片引用被抽出',
      '图片_20251204161442.png' in g._bare_image_candidates('看看 图片_20251204161442.png 这张'),
      str(g._bare_image_candidates('看看 图片_20251204161442.png 这张')))
check('带路径的引用不会被裸文件名规则重复收一遍',
      g._bare_image_candidates(r'C:\Users\a\图.png') == [],
      str(g._bare_image_candidates(r'C:\Users\a\图.png')))

# 端到端复刻 DSH 现场：@ 带逗号的文件名 + 运行时上下文只有 Current working directory
_comma_name = 'u=2448247082,481968747&fm=253&app=138&f=JPEG.jpg'
_comma = os.path.join(_ws, _comma_name)
with open(_comma, 'wb') as _f:
    _f.write(b'\xff\xd8\xff\xe0' + b'\x00' * 64)
_comma_real = os.path.realpath(_comma)
_dsh_ctx = ('Current runtime context. This snapshot supersedes earlier snapshots.\n\n'
            f'Current working directory: "{_ws}".\n\n'
            'Current DSH file policy: workspace-write. Operations may modify files under the '
            f'session workspace: "{_ws}".')
_body = g.trim_messages([
    {'role': 'system', 'content': 'agent'},
    {'role': 'user', 'content': f'@{_comma_name} 理解内容'},
    {'role': 'user', 'content': _dsh_ctx},
], CFG)[-1]['content']
check('带逗号的 DSH @ 引用能定位到真实图片',
      f'[[local-image:{_comma_real}]]' in _body, repr(_body[-300:]))
check('附上图片后明确告诉模型「图已可见、别读二进制」',
      '[图片已附上]' in _body and 'binary file' in _body, repr(_body[-200:]))
check('参数兜底不会把「图片已附上」当成查询词',
      '[图片已附上]' not in g._last_user_text([{'role': 'user', 'content': _body}]),
      repr(g._last_user_text([{'role': 'user', 'content': _body}])))

# 只有 Current working directory、没有 session workspace 时也要能定位
check('运行时上下文缺 session workspace 时回落到工作目录',
      g._session_workspace([{'role': 'user', 'content': _dsh_ctx}]) == _ws,
      repr(g._session_workspace([{'role': 'user', 'content': _dsh_ctx}])))

# 真实 DSH 请求的最后一条 user 是「时间上下文」，不能让它顶掉真正的提问。
# 老实现只删 runtime-context 首段与两条 policy 行，剩下的
# 「Current working directory / 检出路径 / GUI 地址」和整段时间上下文都留了下来，
# 于是当前提问变成 "Time sampled while preparing turn 1…"，
# 真提问和 @图片 一起被塞进历史块 —— 图片因此永远识别不到。
_real_ctx = ('Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n'
             f'Current working directory: "{_ws}".\n\n'
             'Current DSH file policy: workspace-write. Any available operation enforced by the '
             f'DSH file sandbox may modify files under the session workspace: "{_ws}". '
             'Some platform temporary areas may also be writable.\n\n'
             'Approval policy: ask. Operations that require approval may ask through the '
             'configured answerers; without an available answerer, the request fails closed.\n\n'
             'Current DeepSeek Harness implementation checkout: "C:\\Users\\a\\_npx\\abc\\".\n\n'
             'Current DeepSeek Harness Web GUI URL: "http://127.0.0.1:3080".')
_real_time = ('Time sampled while preparing turn 1, step 1: 2026-10-23T21:38:46+08:00[Etc/GMT-8]\n'
              'Browser time zone for this request: Etc/GMT-8. Interpret otherwise-unqualified '
              'dates and times in this zone.\n'
              'Elapsed since the preceding model-visible message: unavailable.')
_body = g.trim_messages([
    {'role': 'user', 'content': f'@{_comma_name} 理解内容'},
    {'role': 'user', 'content': _real_ctx},
    {'role': 'user', 'content': '<system-reminder>\n技能目录\n</system-reminder>'},
    {'role': 'user', 'content': _real_time},
], CFG)[-1]['content']
check('时间上下文不会被当成当前提问',
      'Time sampled' not in _body.split(g._CURRENT_Q_MARKER, 1)[-1], repr(_body[-300:]))
check('真实 DSH 请求（含时间上下文）仍能附上图片',
      f'[[local-image:{_comma_real}]]' in _body, repr(_body[-300:]))
check('运行时上下文残留不会漏进提问区',
      'Current working directory' not in _body.split(g._CURRENT_Q_MARKER, 1)[-1],
      repr(_body[-300:]))

# ── Windows / 客户端降级标签 ──
# 客户端在 supportsImages=false 时发的不是图片字节，而是：
#   <image_local_path>C:\Users\a\图片.png</image_local_path>
# 标签比通用路径正则更可靠：能把带空格的路径整段框住。
check('标签能取出带空格的 Windows 路径',
      g._IMAGE_TAG_RE.findall(r'<image_local_path>C:\Program Files\a b\图.png</image_local_path>')
      == [r'C:\Program Files\a b\图.png'],
      str(g._IMAGE_TAG_RE.findall(r'<image_local_path>C:\Program Files\a b\图.png</image_local_path>')))
check('标签前后空白会被裁掉',
      g._IMAGE_TAG_RE.findall('<image_local_path>  D:\\data\\图.png  </image_local_path>')
      == ['D:\\data\\图.png'],
      str(g._IMAGE_TAG_RE.findall('<image_local_path>  D:\\data\\图.png  </image_local_path>')))

# 盘符分支必须排在 POSIX 分支前，否则 C:/x/a.png 会被截成 /x/a.png
check('盘符路径整段匹配（反斜杠）',
      g._PATH_IMAGE_RE.findall(r'C:\Users\a\图.png') == [r'C:\Users\a\图.png'],
      str(g._PATH_IMAGE_RE.findall(r'C:\Users\a\图.png')))
check('盘符路径整段匹配（正斜杠，不被 POSIX 分支截断）',
      g._PATH_IMAGE_RE.findall('D:/data/图.png') == ['D:/data/图.png'],
      str(g._PATH_IMAGE_RE.findall('D:/data/图.png')))
check('POSIX 绝对路径仍照旧匹配',
      g._PATH_IMAGE_RE.findall('/Users/a/b.png') == ['/Users/a/b.png'],
      str(g._PATH_IMAGE_RE.findall('/Users/a/b.png')))

# 端到端：用本机真实绝对路径（Windows 是 C:\…，macOS 是 /private/var/…）
# 走客户端的降级标签，这条断言两个平台都必须通过
body = g.trim_messages([
    {'role': 'user', 'content': f'这张图是什么\n<image_local_path>{_png}</image_local_path>'},
], CFG)[-1]['content']
check('客户端图片降级标签能注入标记（跨平台）',
      f"[[local-image:{_png_real}]]" in body, repr(body[-200:]))

# 带空格的文件名：标签通道能处理，通用路径正则会断在空格处
_spaced = os.path.join(_ws, '带 空格 的图.png')
with open(_spaced, 'wb') as _f:
    _f.write(b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
_spaced_real = os.path.realpath(_spaced)
body = g.trim_messages([
    {'role': 'user', 'content': f'看看这张\n<image_local_path>{_spaced}</image_local_path>'},
], CFG)[-1]['content']
check('带空格的图片路径可注入（标签通道）',
      f"[[local-image:{_spaced_real}]]" in body, repr(body[-200:]))

print('\n── 内联图片块（DSH 的 base64 image_url）──')
import base64 as _b64  # noqa: E402

_png_bytes = open(_png, 'rb').read()
_data_url = 'data:image/png;base64,' + _b64.b64encode(_png_bytes).decode()

# DSH 声明 inputModalities 含 image，会把图作为 image_url 块发来。
# 旧实现 _text_of 只取 text 块 → 图片被静默丢弃，模型"看不到图"。
_body = g.trim_messages([
    {'role': 'user', 'content': [
        {'type': 'text', 'text': '这张图里有什么'},
        {'type': 'image_url', 'image_url': {'url': _data_url}},
    ]},
], CFG)[-1]['content']
check('image_url 块被转成本地图片标记',
      '[[local-image:' in _body, repr(_body[-260:]))
check('image_url 同时保留文字提问', '这张图里有什么' in _body, repr(_body[:120]))

# 纯图片消息没有文字 → 必须补一句中性指令，否则提示里只剩图片标记
_body = g.trim_messages([
    {'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': _data_url}},
    ]},
], CFG)[-1]['content']
check('纯图片消息会补兜底指令',
      '[[local-image:' in _body and len(_body.strip()) > 40, repr(_body))

# 同一张图在一轮里出现两次只该附一次
_body = g.trim_messages([
    {'role': 'user', 'content': [
        {'type': 'text', 'text': '看看'},
        {'type': 'image_url', 'image_url': {'url': _data_url}},
        {'type': 'image_url', 'image_url': {'url': _data_url}},
    ]},
], CFG)[-1]['content']
check('同一张内联图去重', _body.count('[[local-image:') == 1, str(_body.count('[[local-image:')))

# 非图片的 data URI（例如文本）必须被忽略，不能当真图落盘
_body = g.trim_messages([
    {'role': 'user', 'content': [
        {'type': 'text', 'text': '看这个'},
        {'type': 'image_url', 'image_url': {'url': 'data:text/plain;base64,aGVsbG8='}},
    ]},
], CFG)[-1]['content']
check('非图片 data URI 不落盘', '[[local-image:' not in _body, repr(_body[-160:]))

# 关闭图片开关后，内联块也不该落盘
_off = dict(CFG, local_image_enabled=False)
_body = g.trim_messages([
    {'role': 'user', 'content': [
        {'type': 'text', 'text': '这张图里有什么'},
        {'type': 'image_url', 'image_url': {'url': _data_url}},
    ]},
], _off)[-1]['content']
check('关闭 local_image_enabled 后内联图不注入',
      '[[local-image:' not in _body, repr(_body[-160:]))

print('\n── 当前日期注入 ──')
import datetime as _dt  # noqa: E402

_now = _dt.datetime.now()
_line = g.current_date_text()
check('日期行含年月日与星期',
      f'{_now.year}年{_now.month}月{_now.day}日' in _line
      and g._WEEKDAYS_CN[_now.weekday()] in _line, _line)
check('星期映射正确（2026-09-16 是星期三）',
      '2026年9月16日 星期三' in g.current_date_text(_dt.datetime(2026, 9, 16)),
      g.current_date_text(_dt.datetime(2026, 9, 16)))
check('日期按调用时刻求值（不是模块级常量）',
      g.current_date_text(_dt.datetime(2026, 9, 16))
      != g.current_date_text(_dt.datetime(2027, 1, 2)),
      '不同入参算出同一结果 → 日期被冻结了')

_sys = g.trim_messages([{'role': 'user', 'content': '今天几号'}], CFG)[0]['content']
check('日期注入进 system', '【当前日期】今天是' in _sys, repr(_sys[-70:]))
check('日期排在提示词之后', _sys.index('【当前日期】') > _sys.index('工具调用大师'),
      repr(_sys[-70:]))

_no_date = dict(CFG)
_no_date['inject_current_date'] = False
check('开关关闭后不注入日期',
      '【当前日期】' not in g.trim_messages([{'role': 'user', 'content': '你好'}], _no_date)[0]['content'],
      repr(g.trim_messages([{'role': 'user', 'content': '你好'}], _no_date)[0]['content'][-70:]))

_cut = dict(CFG)
_cut['system_prompt'] = 'X' * 500
_cut['max_system_chars'] = 40
check('提示词被 max_system_chars 截断后日期仍在',
      '【当前日期】' in g.trim_messages([{'role': 'user', 'content': '你好'}], _cut)[0]['content'],
      repr(g.trim_messages([{'role': 'user', 'content': '你好'}], _cut)[0]['content']))

_empty = dict(CFG)
_empty['system_prompt'] = ''
_msgs = g.trim_messages([{'role': 'user', 'content': '你好'}], _empty)
check('system_prompt 为空时仍注入日期',
      _msgs[0]['role'] == 'system' and '【当前日期】' in _msgs[0]['content'], str(_msgs)[:200])


# ────────────── 配置热重载（UI 与网关是两个独立进程）──────────────
# 背景：`start.sh --ui` 会起 ui.py(7860) 与 server.py(8001) 两个独立进程，
# UI 保存只写 config.json、不通知网关。网关靠 mtime 变化自动重载 ——
# 没有这层机制，在配置页改完必须重启网关才生效，而且界面上不会有任何提示。
#
# 这里用打桩验证判定逻辑，**刻意不碰文件系统**：受限环境会把"写文件 / 改 mtime /
# 删除"之类的操作直接 SIGKILL，而 stdout 是块缓冲 —— 一旦被杀，前面所有断言结果
# 都会一起丢失，表现为"整套测试零输出"，排查代价极高（本次就踩了）。
print('\n── 配置热重载 ──')

_real_getmtime = g._config_file_mtime
_real_load = g.load_config
_real_mtime = g._config_mtime
_real_hot = g._ENABLE_CONFIG_HOT_RELOAD
_load_calls = []
try:
    g._ENABLE_CONFIG_HOT_RELOAD = True
    g._config_mtime = 100.0
    # 包一层记录调用次数；真正加载仍走原函数
    g.load_config = lambda: (_load_calls.append(1), _real_load())[1]

    g._config_file_mtime = lambda: 100.0
    g.get_config()
    check('config.json 未变时不重载', _load_calls == [], str(_load_calls))

    g._config_file_mtime = lambda: 200.0
    g.get_config()
    check('检测到外部改动后自动重载一次', _load_calls == [1], str(_load_calls))

    g.get_config()
    check('重载后再调用不重复读盘（mtime 快照已更新）',
          _load_calls == [1], str(_load_calls))

    g._config_file_mtime = lambda: None
    g.get_config()
    check('拿不到 config.json 时不动配置且不抛异常',
          _load_calls == [1], str(_load_calls))

    g._ENABLE_CONFIG_HOT_RELOAD = False
    g._config_file_mtime = lambda: 300.0
    g.get_config()
    check('关闭热重载开关后不再检查', _load_calls == [1], str(_load_calls))
finally:
    g._config_file_mtime = _real_getmtime
    g.load_config = _real_load
    g._config_mtime = _real_mtime
    g._ENABLE_CONFIG_HOT_RELOAD = _real_hot

print()
# ═══════════════════════════════════════════════════════
# 网关代搜（server_search）
# ═══════════════════════════════════════════════════════
print('── 网关代搜 ──')


def _tc(name, args):
    return {'id': 'c1', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}


_base = {'server_search_mode': 'auto'}
g._CLIENT_SEARCH_BROKEN = False
check('auto + 客户端搜索正常 → 不接管',
      g._should_server_search([_tc('web_search', {'queries': ['x']})], _base) is False)
g._CLIENT_SEARCH_BROKEN = True
check('auto + 客户端搜索报过 401 → 接管',
      g._should_server_search([_tc('web_search', {'queries': ['x']})], _base) is True)
check('mode=off 一律不接管',
      g._should_server_search([_tc('web_search', {'queries': ['x']})],
                              {'server_search_mode': 'off'}) is False)
check('mode=on 无条件接管',
      g._should_server_search([_tc('web_search', {'queries': ['x']})],
                              {'server_search_mode': 'on'}) is True)
check('混合调用（搜索+pwsh）不接管，原样交回客户端',
      g._should_server_search([_tc('web_search', {'queries': ['x']}),
                               _tc('pwsh', {'command': 'dir'})],
                              {'server_search_mode': 'on'}) is False)
check('非搜索工具不接管',
      g._should_server_search([_tc('pwsh', {'command': 'dir'})],
                              {'server_search_mode': 'on'}) is False)
g._CLIENT_SEARCH_BROKEN = False

# 查询词提取：query / queries / 逗号串 / 数组都要能拆
check('queries 数组原样取出',
      g._search_queries([_tc('web_search', {'queries': ['a', 'b']})]) == ['a', 'b'])
check('query 字符串（逗号串）拆成多条',
      g._search_queries([_tc('web_search', {'query': '重庆今天天气, 重庆气温'})])
      == ['重庆今天天气', '重庆气温'])
check('query 单条不拆',
      g._search_queries([_tc('web_search', {'query': '重庆天气'})]) == ['重庆天气'])
check('参数是坏 JSON 时返回空（不抛异常）',
      g._search_queries([{'function': {'name': 'web_search', 'arguments': '{bad'}}]) == [])

# 代搜结果合并进最后一条 user 消息
_msgs = [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'Q'}]
_merged = g._merge_search_blocks(_msgs, ['[网关代搜结果]\nX'])
check('代搜结果并进当前提问之前',
      _merged[-1]['role'] == 'user'
      and _merged[-1]['content'].startswith('[网关代搜结果]')
      and 'Q' in _merged[-1]['content'], _merged[-1]['content'][:60])
check('合并不会改动原消息列表（避免重试时重复注入）',
      _msgs[-1]['content'] == 'Q', _msgs[-1]['content'])
_no_user = g._merge_search_blocks([{'role': 'system', 'content': 'S'}], ['B'])
check('没有 user 消息时补一条', _no_user[-1]['role'] == 'user', str(_no_user))

# 401 识别：翻开关
g._CLIENT_SEARCH_BROKEN = False
g._note_client_search_failure([{'role': 'tool', 'tool_call_id': 'x', 'content':
                                'Error: DeepSeek API error (HTTP 401): Authentication Fails, '
                                'Your api key: ****78bN is invalid'}])
check('tool 结果里的 401 会打开代搜开关', g._CLIENT_SEARCH_BROKEN is True)
g._CLIENT_SEARCH_BROKEN = False
g._note_client_search_failure([{'role': 'tool', 'content': 'binary file (FS_NOT_TEXT)'}])
check('普通工具错误不会误开代搜', g._CLIENT_SEARCH_BROKEN is False)

# 查询改写：长查询要能退化成精准短查询（必应长查询会掉到百科/门户）
try:
    import server_search as ss
    _v = ss.query_variants('重庆今天天气 气温')
    check('长查询改写能产出「重庆天气」这种精准短查询', '重庆天气' in _v, str(_v))
    check('原查询保留在第一位', _v[0] == '重庆今天天气 气温', str(_v))
    _v2 = ss.query_variants('重庆今天天气 2026年10月23日, 重庆今日气温 天气现象 湿度')
    check('日期/时间词被摘掉', all('2026年10月23日' not in x for x in _v2[1:]), str(_v2))
    check('正文抽取会去掉导航短词', '首页' not in ss.extract_text(
        '<html><body>首页\n预报\n预警\n' + '重庆今天阴天，气温 18℃ 到 24℃，湿度 80%。' * 3
        + '</body></html>', 400), ss.extract_text(
        '<html><body>首页\n预报\n预警\n' + '重庆今天阴天，气温 18℃ 到 24℃，湿度 80%。' * 3
        + '</body></html>', 120)[:60])
    check('正文抽取保留数字/温度',
          '18℃' in ss.extract_text('<p>重庆 18℃</p>', 200))
    _dense = ('导航\n' * 40) + ('23日（今天）\n阴\n18℃\n24日（明天）\n23℃ / 18℃\n' * 6)
    _got = ss.extract_text(_dense, 300)
    check('长页面优先取信息密度高的片段，而不是只截开头', '18℃' in _got, _got[:60])
except ImportError:  # 打包环境里没有该模块时跳过
    check('server_search 可导入', False, 'ImportError')

print()
if FAILED:
    print(f'✗ {len(FAILED)} 项失败: {FAILED}')
    sys.exit(1)
print('✓ 全部通过')
