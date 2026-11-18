# -*- coding: utf-8 -*-
"""通用性校验：模板里不允许出现任何本机专属路径/状态数字；交叉引用必须自洽。"""
import re, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

P = r"D:\ds-trim-gateway-release - 副本\manual_patch\最终版-补工具与技能-模板.txt"
t = open(P, encoding='utf-8').read()
ok = True


def bad(msg):
    global ok
    ok = False
    print('  [!]', msg)


print('字符数 %d  行数 %d' % (len(t), t.count('\n') + 1))

# 1) 本机标识与绝对路径
print('\n[1] 本机专属内容')
for pat in ['37806', 'ds-trim-gateway-release', r'[A-Z]:\\Users\\[^你]']:
    for m in re.finditer(pat, t):
        bad('含本机路径/标识: %r' % t[max(0, m.start() - 20):m.start() + 25].replace('\n', ' '))

# 2) 写死的状态数字（本机实测值，模板里出现即不通用）
print('\n[2] 写死的状态数字')
for pat in ['1195', '1716', '705', '2000', '3000', '现有 7', '7 条', '9 条', '从 10)']:
    if pat in t:
        bad('写死: %r' % pat)

# 3) 交叉引用自洽
print('\n[3] 交叉引用')
defined = set(re.findall(r'§(\d)\s', t)) | set(re.findall(r'§(\d)', t))
heads = set(re.findall(r'═+ §(\d) ', t))
print('  定义章节:', sorted(heads, key=int))
refs = set(re.findall(r'§(\d)', t))
dangling = refs - heads
if dangling:
    bad('引用了不存在的章节: %s' % sorted(dangling))
if heads != {str(i) for i in range(1, 10)}:
    bad('章节编号不连续: %s' % sorted(heads, key=int))

# 4) 输出段齐全
print('\n[4] 输出段')
sec9 = t.split('§9 输出')[1]
for k in ['一、工具 JSON', '二、提示词', '需删除']:
    if k not in sec9:
        bad('输出段缺 %r' % k)
for k in ['自检', '参数变更', '四段']:
    if k in t:
        bad('残留已废弃内容: %r' % k)

# 5) 填写提示行
print('\n[5] 填写处')
tips = [l.rstrip() for l in t.split('\n') if l.strip().startswith('（提示：')]
print('  提示行 %d 条' % len(tips))
cont = [l for l in t.split('\n') if l.startswith('      ') or l.startswith(' ')]
if '（提示：」开头' not in t or '填写说明' not in t:
    bad('缺「（提示：」声明')
if '自己数' not in t:
    bad('缺「数字自己数」声明')

# 6) 已知失败模式是否落在 §7
print('\n[6] 失败模式节')
sec7 = t.split('§7 已验证')[1].split('§8')[0]
print('  条目 %d 条' % len([l for l in sec7.split('\n') if l.startswith('·')]))
for k in ['写死', '截断', 'pip', 'PowerShellPowerShell', 'system-reminder']:
    if k not in sec7:
        bad('§7 缺要点: %r' % k)

print('\n结果:', 'PASS' if ok else 'FAIL')
