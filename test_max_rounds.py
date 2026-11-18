# -*- coding: utf-8 -*-
"""回归测试：单会话总轮数上限（max_total_rounds）。

背景：客户端只在「模型不再发工具调用」时才结束一轮，它不数步数。遇到
「检索-写作 + 硬判据」的任务（"找 3 条官网原文，不足则少给"），模型会把
"没凑够"读成"继续找"，每 12 秒一轮可以连跑上百轮——实测一次 24 分钟
119 轮、0 字正文，最终把上游账号打到风控。

网关在转发前按 max_total_rounds 截断，并把拦截包装成**正常回答**
（finish_reason=stop），而不是 4xx/5xx —— 后者会触发客户端重试，越拦越多。
"""
import io
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


def mk(n_rounds, user='找一个官方公告'):
    """造一份「已执行 n 轮工具调用」的 messages。"""
    msgs = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': user}]
    for i in range(n_rounds):
        msgs.append({
            'role': 'assistant', 'content': None,
            'tool_calls': [{'id': f'c{i}', 'type': 'function',
                            'function': {'name': 'WebSearch', 'arguments': '{}'}}],
        })
        msgs.append({'role': 'tool', 'tool_call_id': f'c{i}', 'content': 'r' * 50})
    return msgs


cfg = gw.get_config()
print('[1] 配置项存在')
check('DEFAULT_CONFIG 含 max_total_rounds', 'max_total_rounds' in gw.DEFAULT_CONFIG,
      list(gw.DEFAULT_CONFIG)[:5])
check('默认值为 20', int(gw.DEFAULT_CONFIG.get('max_total_rounds', -1)) == 20,
      gw.DEFAULT_CONFIG.get('max_total_rounds'))

print('\n[2] 线性累加：每次请求 messages 多一轮')
gw._round_state.clear()
seq = []
for i in range(22):
    seq.append(gw._bump_session_round(mk(i), cfg)[0])
check('计数从 1 连续递增', seq[:5] == [1, 2, 3, 4, 5], seq[:5])
check('第 20 次仍为 20', seq[19] == 20, seq[19])
check('第 21 次为 21', seq[20] == 21, seq[20])

print('\n[3] 拦截位置：第 limit+1 次')
gw._round_state.clear()
first_over = None
for i in range(25):
    _, over = gw._bump_session_round(mk(i), cfg)
    if over:
        first_over = i
        break
check('第 21 次请求(轮数=20)首次超限', first_over == 20, first_over)

print('\n[4] 客户端压缩历史后计数不回退')
gw._round_state.clear()
for i in range(12):
    gw._bump_session_round(mk(i), cfg)
n, over = gw._bump_session_round(mk(0), cfg)   # 历史被压成只剩 1 轮
check('计数继续增长到 13（未回到 2）', n == 13, n)
check('压缩后仍不误判超限', over is False, over)

print('\n[5] 新会话独立计数')
n2, over2 = gw._bump_session_round(mk(0, user='完全不同的新问题'), cfg)
check('新指纹从 1 开始', n2 == 1, n2)
check('新会话不继承旧计数', over2 is False, over2)

print('\n[6] max_total_rounds=0 表示不限制')
off = dict(cfg)
off['max_total_rounds'] = 0
gw._round_state.clear()
any_over = False
for i in range(40):
    _, o = gw._bump_session_round(mk(i), off)
    any_over = any_over or o
check('40 轮全程不拦截', any_over is False)

print('\n[7] 超限时返回的是正常回答，不是错误')
gw._round_state.clear()
resp = gw.handle_chat_completion({
    'model': 'DeepSeek-V4.1-Flash', 'stream': False, 'messages': mk(20),
})
choice = resp['choices'][0]
msg = choice['message']
check('finish_reason=stop', choice['finish_reason'] == 'stop', choice['finish_reason'])
check('role=assistant', msg['role'] == 'assistant', msg['role'])
check('没有 tool_calls（不会诱发下一轮）', not msg.get('tool_calls'), msg.get('tool_calls'))
check('正文说明达到上限', '上限' in (msg.get('content') or ''), (msg.get('content') or '')[:60])
check('响应含 id/object/created/model', all(k in resp for k in ('id', 'object', 'created', 'model')))

print('\n[8] 空闲超时后视为新会话')
gw._round_state.clear()
gw._bump_session_round(mk(0), cfg)
key = gw._round_fingerprint(mk(0))
with gw._round_lock:
    gw._round_state[key]['ts'] -= (gw._ROUND_IDLE_SEC + 10)
n3, _ = gw._bump_session_round(mk(0), cfg)
check('超时后计数重置为 1', n3 == 1, n3)

print('\n[9] 非 list 的 messages 不炸')
try:
    gw._bump_session_round(None, cfg)
    gw._bump_session_round('bad', cfg)
    ok = True
except Exception as e:  # noqa: BLE001
    ok = False
    print('   异常:', e)
check('None / str 输入安全返回', ok)

print('\n结果: %d 通过, %d 失败' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
