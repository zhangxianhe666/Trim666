# -*- coding: utf-8 -*-
"""回归测试：单轮多工具调用的裁剪（max_tool_calls_per_round）。

背景：客户端（WorkBuddy）把一条消息里的多个 tool_calls 当成**一个名字**校验，
['PowerShell','PowerShell'] → 报 "PowerShellPowerShell does not exist"。
网关交付前按配置裁成 N 个，默认 1。
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import gateway_core as gw  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓', name)
    else:
        FAIL += 1
        print('  ✗', name, detail)


TWO_SAME = (
    '<tool_calls>'
    '<invoke name="PowerShell"><parameter name="command">Get-ChildItem</parameter></invoke>'
    '<invoke name="PowerShell"><parameter name="command">Get-Process</parameter></invoke>'
    '</tool_calls>'
)
TWO_DIFF = (
    '<tool_calls>'
    '<invoke name="Read"><parameter name="file_path">C:/a.txt</parameter></invoke>'
    '<invoke name="Glob"><parameter name="pattern">**/*.md</parameter></invoke>'
    '</tool_calls>'
)
ONE = ('<tool_calls><invoke name="PowerShell">'
       '<parameter name="command">Get-ChildItem</parameter></invoke></tool_calls>')

BODY = {
    'model': 'test',
    'messages': [{'role': 'user', 'content': '列一下当前目录的文件'}],
    'tools': [
        {'type': 'function', 'function': {
            'name': 'PowerShell',
            'description': 'run a shell command',
            'parameters': {'type': 'object',
                           'properties': {'command': {'type': 'string'},
                                          'description': {'type': 'string'}},
                           'required': ['command']}}},
        {'type': 'function', 'function': {
            'name': 'Read', 'description': 'read a file',
            'parameters': {'type': 'object',
                           'properties': {'file_path': {'type': 'string'}},
                           'required': ['file_path']}}},
        {'type': 'function', 'function': {
            'name': 'Glob', 'description': 'glob files',
            'parameters': {'type': 'object',
                           'properties': {'pattern': {'type': 'string'}},
                           'required': ['pattern']}}},
    ],
}


def run(text, limit):
    """让 call_upstream 返回固定文本，跑一次 handle_chat_completion。"""
    gw.update_config({'max_tool_calls_per_round': limit})
    gw.call_upstream = lambda msgs, cfg: {
        'choices': [{'message': {'role': 'assistant', 'content': text}}],
        'usage': {},
    }
    resp = gw.handle_chat_completion(json.loads(json.dumps(BODY)))
    msg = resp['choices'][0]['message']
    return msg.get('tool_calls') or []


print('\n[1] 默认配置项存在且为 1')
check('DEFAULT_CONFIG 含 max_tool_calls_per_round',
      'max_tool_calls_per_round' in gw.DEFAULT_CONFIG)
check('默认值是 1', gw.DEFAULT_CONFIG.get('max_tool_calls_per_round') == 1,
      gw.DEFAULT_CONFIG.get('max_tool_calls_per_round'))

print('\n[2] limit=1：同名两个调用只交付第一个')
calls = run(TWO_SAME, 1)
check('只返回 1 个调用', len(calls) == 1, len(calls))
check('名字是 PowerShell', calls and calls[0]['function']['name'] == 'PowerShell',
      calls)
check('参数是第一个调用的（Get-ChildItem）',
      calls and 'Get-ChildItem' in str(calls[0]['function']['arguments']),
      calls and calls[0]['function']['arguments'])

print('\n[3] limit=1：不同名两个调用也只交付第一个')
calls = run(TWO_DIFF, 1)
check('只返回 1 个调用', len(calls) == 1, len(calls))
check('保留 Read（第一个）', calls and calls[0]['function']['name'] == 'Read', calls)

print('\n[4] limit=0：不限制，两个都交付')
calls = run(TWO_DIFF, 0)
check('返回 2 个调用', len(calls) == 2, len(calls))
check('名字依次是 Read, Glob',
      [c['function']['name'] for c in calls] == ['Read', 'Glob'],
      [c['function']['name'] for c in calls])

print('\n[5] limit=2：不足上限时不裁剪')
calls = run(TWO_SAME, 2)
check('返回 2 个调用', len(calls) == 2, len(calls))

print('\n[6] 本来就只有一个调用时不受影响')
calls = run(ONE, 1)
check('返回 1 个调用', len(calls) == 1, len(calls))
check('名字是 PowerShell', calls and calls[0]['function']['name'] == 'PowerShell', calls)

print('\n[7] 纯文本回答不受影响')
gw.update_config({'max_tool_calls_per_round': 1})
gw.call_upstream = lambda msgs, cfg: {
    'choices': [{'message': {'role': 'assistant', 'content': '好的，已经完成。'}}],
    'usage': {},
}
resp = gw.handle_chat_completion(json.loads(json.dumps(BODY)))
m = resp['choices'][0]['message']
check('返回文本而非 tool_calls', not m.get('tool_calls'), m)
check('正文是原文本', m.get('content') == '好的，已经完成。', m)

print('\n[8] 每个 tool_call 都带唯一 id')
calls = run(TWO_SAME, 0)
ids = [c.get('id') for c in calls]
check('id 都存在', all(ids), ids)
check('id 互不相同', len(set(ids)) == len(ids), ids)

# 复原，避免影响后续
gw.DEFAULT_CONFIG.setdefault('max_tool_calls_per_round', 1)
gw.update_config({'max_tool_calls_per_round': 1})

print('\n结果: %d 通过, %d 失败' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
