# -*- coding: utf-8 -*-
"""把「写文件/改文件工具 + 文档工作流提示词 + 放宽截断」同步到正在运行的部署。

只升不降：已有的更大取值不会被覆盖；已有的自定义提示词只追加、不重写。
用法：python patch_live_config.py [目标 config.json]
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gateway_core as g

LIVE = sys.argv[1] if len(sys.argv) > 1 else \
    r'E:\ds-trim-gateway-release\ds-trim-gateway-release\config.json'

cfg = json.load(open(LIVE, encoding='utf-8'))
bak = LIVE + '.bak-' + time.strftime('%Y%m%d-%H%M')
shutil.copy2(LIVE, bak)

names = [t.get('name') for t in cfg['tools']]
print('原工具:', names)
print('原提示词长度:', len(cfg.get('system_prompt', '')),
      '| 已含文档说明:', 'PowerPoint.Application' in cfg.get('system_prompt', ''))

preset = g.TOOL_PRESETS[g.detect_preset_name(cfg['tools']) or g.DEFAULT_PRESET_NAME]
print('识别为预设:', g.detect_preset_name(cfg['tools']))

for t in preset['tools']:
    if t['name'] not in names:
        cfg['tools'].append(t)
        names.append(t['name'])
order = {t['name']: i for i, t in enumerate(preset['tools'])}
cfg['tools'].sort(key=lambda t: order.get(t.get('name'), 99))
print('新工具:', [t['name'] for t in cfg['tools']])

if 'PowerPoint.Application' not in cfg.get('system_prompt', ''):
    client = preset['client']
    w, e, r = (('Write', 'Edit', 'Read') if client == 'workbuddy'
               else ('write', 'edit', 'read'))
    shell = 'PowerShell' if client == 'workbuddy' else 'pwsh'
    cfg['system_prompt'] = (cfg.get('system_prompt', '').rstrip() + '\n'
                            + g._DOC_WORKFLOW_RULE.format(shell=shell, write=w,
                                                          edit=e, read=r))
elif cfg['system_prompt'].lstrip().startswith(g._SYS_HEAD[:16]):
    # 这份提示词是「预设自动生成的」，不是用户手写的 → 整体刷新成最新预设版，
    # 才能带上新增的 shell 输出编码提示等修正。用户手改过的不动。
    cfg['system_prompt'] = preset['system_prompt']
    print('提示词：识别为预设自动生成 → 已整体刷新')
else:
    print('提示词：检测到自定义内容 → 保持原样，只做最小补充')
print('新提示词长度:', len(cfg['system_prompt']))

for k, v in (('max_system_chars', 2000), ('history_result_chars', 8000),
             ('history_total_chars', 24000)):
    old = cfg.get(k)
    cfg[k] = max(int(old or 0), v)
    print(f'{k}: {old} → {cfg[k]}')

with open(LIVE, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print('已写入', LIVE)
print('备份', os.path.basename(bak))
