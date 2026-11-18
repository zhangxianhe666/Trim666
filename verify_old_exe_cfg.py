# -*- coding: utf-8 -*-
"""端到端验证：用旧 exe 里那份 gateway_core，配 7 工具（含 Write/Edit），
看它能不能把模型输出的 <Write .../> 解析成合法的 tool_call 并返回给客户端。"""
import json
import marshal
import os
import shutil
import sys
import types
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

EXE = r"E:\ds-trim-gateway-release\ds-trim-gateway-release\ds-trim-gateway.exe"
PYZ = r"C:\Users\37806\AppData\Local\Temp\old_pyz.pyz"
TMP = r"C:\Users\37806\AppData\Local\Temp\oldgwtest"

arch = CArchiveReader(EXE)
z = ZlibArchiveReader(PYZ)

shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
os.chdir(TMP)
sys.path.insert(0, TMP)


def run_module(name):
    code = z.extract(name)
    mod = types.ModuleType(name)
    mod.__file__ = os.path.join(TMP, name + '.py')
    sys.modules[name] = mod
    exec(code, mod.__dict__)
    return mod


ss = run_module('server_search')
print('server_search OK')
gw = run_module('gateway_core')
print('gateway_core OK, BASE_DIR =', gw.BASE_DIR)

# 写一份 7 工具的配置（模拟用户在界面里粘贴后保存的结果）
sys.path.insert(0, r"D:\ds-trim-gateway-release - 副本")
NEW_TOOLS = json.load(open(r"D:\ds-trim-gateway-release - 副本\manual_patch\粘贴-2-工具JSON-完整7条.json",
                           encoding='utf-8'))
cfg = dict(gw.DEFAULT_CONFIG)
cfg.update({'tools': NEW_TOOLS, 'max_system_chars': 2000,
            'history_result_chars': 8000, 'history_total_chars': 24000})
json.dump(cfg, open(os.path.join(TMP, 'config.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

print('\n--- 旧 exe 读到几个工具 ---')
print([t.get('name') for t in gw.get_config()['tools']])

prompt = gw.config_tools_as_prompt(gw.get_config())
print('\n--- 工具是否写进上游提示词 ---')
for n in ('Write', 'Edit'):
    print('  %-6s in prompt: %s' % (n, n in prompt))

VALID = {t['name'] for t in NEW_TOOLS}
SHAPES = {
    'Write(invoke)': ('<invoke name="Write"><parameter name="file_path">C:/tmp/a.txt</parameter>'
                      '<parameter name="content">hi</parameter></invoke>'),
    'Edit(invoke)': ('<invoke name="Edit"><parameter name="file_path">C:/tmp/a.txt</parameter>'
                     '<parameter name="old_string">hi</parameter>'
                     '<parameter name="new_string">yo</parameter></invoke>'),
    '未知工具': '<invoke name="Frobnicate"><parameter name="x">1</parameter></invoke>',
}
print('\n--- 旧 exe 解析 Write/Edit 工具调用 ---')
for tag, s in SHAPES.items():
    print('  %-14s -> %s' % (tag, json.dumps(gw.extract_tool_calls(s, VALID), ensure_ascii=False)))

print('\n--- 客户端回填（模拟 WorkBuddy 收到 tool_calls，参数传客户端工具名集合）---')
calls = gw.extract_tool_calls(SHAPES['Write(invoke)'], VALID)
for arg in (VALID, sorted(VALID)):
    try:
        print('  ->', gw._remap_to_client(calls, arg))
    except Exception as e:
        print('  异常:', repr(e))

