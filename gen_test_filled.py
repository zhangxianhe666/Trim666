# -*- coding: utf-8 -*-
"""把模板按真实运行配置填满，生成实测输入。按行结构定位，不依赖提示行的具体措辞。"""
import json, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

TPL = r"D:\ds-trim-gateway-release - 副本\manual_patch\最终版-补工具与技能-模板.txt"
CFG = r"E:\ds-trim-gateway-release\ds-trim-gateway-release\config.json"
DST = r"D:\ds-trim-gateway-release - 副本\_test_filled.txt"

VALUES = {
    '我的客户端：': '我的客户端：WorkBuddy',
    '任务：': '任务：把「新建 DOCX 文档.docx」做成 10 页 PPT',
    '期望产物：': '期望产物：C:\\Users\\37806\\Desktop\\文件\\out.pptx，10 页，16:9，每页 3 条要点，标题 28 号',
    '输入素材：': '输入素材：C:\\Users\\37806\\Desktop\\文件\\新建 DOCX 文档.docx（含一段正文，可能有表格）',
    '已试过：': '已试过：Read 该 docx 报 Cannot display content of binary file',
    '技能目录：': 'C:\\Users\\37806\\.workbuddy\\skills',
}
BLOCKS = {'工具 JSON：', '提示词：'}


def fill(tpl, cap, tools, prompt):
    out, lines, i = [], tpl.split('\n'), 0
    while i < len(lines):
        line = lines[i]

        def skip_hint(j):
            while j < len(lines) and (lines[j].startswith('（提示：')
                                      or (lines[j].startswith(' ') and not lines[j].startswith(' ·'))):
                j += 1
            return j

        if line.startswith('上限 ='):
            out.append('上限 = %d' % cap)
            i += 1
        elif line in VALUES:
            out.append(VALUES[line])
            i = skip_hint(i + 1)
        elif line in BLOCKS:
            out.append(line)
            i = skip_hint(i + 1)
            out.append(tools if line == '工具 JSON：' else prompt)
        else:
            out.append(line)
            i += 1
    return '\n'.join(out)


c = json.load(open(CFG, encoding='utf-8'))
t = fill(open(TPL, encoding='utf-8').read(),
         c['max_system_chars'],
         json.dumps(c['tools'], ensure_ascii=False, indent=2),
         c['system_prompt'])
open(DST, 'w', encoding='utf-8').write(t)

print('已生成', DST, len(t), '字')
left = [l for l in t.split('\n') if l.startswith('（提示：')]
print('保留的提示行（未填项）:', len(left))
for l in left:
    print('   ', l[:52])
print('上限:', [l for l in t.split('\n') if l.startswith('上限')])
print('含 WorkBuddy:', 'WorkBuddy' in t, ' 含 out.pptx:', 'out.pptx' in t)
