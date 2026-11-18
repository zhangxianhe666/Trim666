# -*- coding: utf-8 -*-
"""交叉核对：模板里对「现有 9 条提示词」的摘要，是否都能在真实提示词里找到依据。"""
import re

BASE = r"D:\ds-trim-gateway-release - 副本"
prompt = open(BASE + r"\manual_patch\粘贴-1-提示词.txt", encoding='utf-8').read()

# 摘要里承诺的每条能力 -> 真实提示词里应出现的关键词
CHECKS = {
    '只输出 XML 工具调用': ['只输出 XML 工具调用块'],
    '拿到数据就停手': ['就不要再调用工具'],
    '失败别重复调': ['不要连续重复调用'],
    '搜索后用 WebFetch': ['用 WebFetch 打开'],
    '不许编数字': ['绝对禁止编造数据'],
    '别重复搜/读': ['不要反复用不同关键词重搜'],
    '优先用已有结果': ['此前已执行的工具结果'],
    'Windows 语法': ['Get-ChildItem'],
    '中文编码': ['OutputEncoding'],
    '写文件用 Write/Edit': ['用 Write', '用 Edit'],
    '不用 here-string': ['here-string'],
    'docx 拆 zip': ['Expand-Archive', 'document.xml'],
    'pptx 走 COM': ['Slides.Add', 'SaveAs'],
}
bad = []
for name, keys in CHECKS.items():
    miss = [k for k in keys if k not in prompt]
    if miss:
        bad.append((name, miss))
    print('%-22s %s' % (name, 'OK' if not miss else 'MISSING ' + str(miss)))

print('\n提示词实际字数(len):', len(prompt))
print('真实条数（按 "N)" 编号）:', len(re.findall(r'(?<![\d.])\b(\d)\)', prompt)))
print('\n结论:', '摘要全部有依据' if not bad else '有 %d 项对不上' % len(bad))
