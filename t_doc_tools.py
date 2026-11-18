# -*- coding: utf-8 -*-
"""端到端验证：网关照新配置把「写文件」工具交给模型，模型能否正确产出该工具调用。

只验证工具调用本身（不落盘），所以不会在磁盘上留垃圾。
"""
import json
import sys

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8001')

# 与 WorkBuddy 真实工具面一致的 7 个工具（只填名字，网关只按名字做校验/改写）
TOOLS = [
    {'type': 'function', 'function': {'name': n, 'description': n, 'parameters': {
        'type': 'object', 'properties': {}}}}
    for n in ('PowerShell', 'Read', 'Glob', 'Write', 'Edit', 'WebSearch', 'WebFetch')
]

CASES = [
    ('写文件任务',
     '请把 hello 这个字符串写入 D:\\tmp_trim_write_test.txt，用写文件工具，'
     '不要用 shell 命令。只要发起工具调用即可，不用解释。'),
    ('把 Word 做成 PPT 的第一步',
     '我有 D:\\办公软件相关\\邮箱配置.docx，要把它做成 PPT。'
     '第一步先把正文文字取出来，请按本机规则发起工具调用（不要用读文件工具直接读 docx）。'),
]


def run(prompt):
    cfg = requests.get(f'{BASE}/v1/internal/config', timeout=10).json()
    r = requests.post(f'{BASE}/v1/chat/completions', json={
        'model': cfg['upstream_model'], 'stream': False,
        'messages': [{'role': 'system', 'content': '你是工具调用助手'},
                     {'role': 'user', 'content': prompt}],
        'tools': TOOLS,
    }, timeout=300)
    d = r.json()
    ch = (d.get('choices') or [{}])[0]
    m = ch.get('message') or {}
    return r.status_code, ch.get('finish_reason'), m.get('tool_calls'), (m.get('content') or '')


for title, prompt in CASES:
    code, finish, tcs, content = run(prompt)
    print('=' * 60)
    print(title, '| HTTP', code, '| finish', finish)
    if tcs:
        for tc in tcs:
            fn = tc.get('function') or {}
            args = fn.get('arguments') or ''
            try:
                pretty = json.dumps(json.loads(args), ensure_ascii=False)
            except Exception:
                pretty = args
            print(f'  工具: {fn.get("name")}')
            print(f'  参数: {pretty[:300]}')
    else:
        print('  ⚠ 没有工具调用，正文:', content[:200])
