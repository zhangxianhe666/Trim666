# -*- coding: utf-8 -*-
"""生成「旧 exe 手工修改」需要的粘贴素材。"""
import json
import os
import sys

sys.path.insert(0, r"D:\ds-trim-gateway-release - 副本")
import gateway_core as gw  # noqa

OUT = r"D:\ds-trim-gateway-release - 副本\manual_patch"
os.makedirs(OUT, exist_ok=True)

# 1) 提示词
prompt = gw.TOOL_PRESETS['WorkBuddy（Windows）']['system_prompt']
with open(os.path.join(OUT, '粘贴-1-提示词.txt'), 'w', encoding='utf-8', newline='\r\n') as f:
    f.write(prompt)

# 2) 完整 7 工具
tools = gw.TOOL_PRESETS['WorkBuddy（Windows）']['tools']
with open(os.path.join(OUT, '粘贴-2-工具JSON-完整7条.json'), 'w', encoding='utf-8', newline='\r\n') as f:
    f.write(json.dumps(tools, ensure_ascii=False, indent=2))

# 3) 只补 Write/Edit 两条（WorkBuddy 大小写）
wed = [t for t in tools if t['name'] in ('Write', 'Edit')]
with open(os.path.join(OUT, '粘贴-3-只补Write和Edit.json'), 'w', encoding='utf-8', newline='\r\n') as f:
    f.write(json.dumps(wed, ensure_ascii=False, indent=2))

# 4) DSH 小写版
dsh = gw.TOOL_PRESETS['DSH（Windows）']['tools']
with open(os.path.join(OUT, '粘贴-4-DSH工具JSON-完整7条.json'), 'w', encoding='utf-8', newline='\r\n') as f:
    f.write(json.dumps(dsh, ensure_ascii=False, indent=2))

print('prompt chars =', len(prompt))
print('max_system_chars 需要 >=', len(prompt))
print('wb tools =', [t['name'] for t in tools])
print('dsh tools =', [t['name'] for t in dsh])
print('written ->', OUT)
for n in sorted(os.listdir(OUT)):
    p = os.path.join(OUT, n)
    print('  %-34s %6d bytes' % (n, os.path.getsize(p)))
