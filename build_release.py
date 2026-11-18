# -*- coding: utf-8 -*-
"""打一个给「别人」用的绿色分发包：exe + config.json + AFS 便携版 + 说明。

只在需要重新分发时跑一次：
    .venv/Scripts/python.exe build_release.py
产物：dist/分发包/ 以及 dist/分发包.zip
"""
import json
import os
import shutil
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gateway_core as core  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, 'dist')
OUT = os.path.join(DIST, '分发包')
AFS_SRC = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Temp',
                       '3KubNssW6EjsG8DrWxV2ikhVYfX')

PRESET_DEFAULT = 'DSH（Windows）'   # 分发包里预置的客户端（可在启动器里改）


def make_config(preset_name):
    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG, ensure_ascii=False))
    preset = core.TOOL_PRESETS[preset_name]
    cfg['tools'] = json.loads(json.dumps(preset['tools'], ensure_ascii=False))
    cfg['system_prompt'] = preset['system_prompt']
    cfg['upstream_base'] = 'http://127.0.0.1:8080/v1'
    cfg['upstream_api_key'] = ''
    cfg['upstream_model'] = 'DeepSeek-V4.1-Flash'
    cfg['workspace_root'] = ''
    cfg['server_search_mode'] = 'auto'
    return cfg


def main():
    exe = os.path.join(DIST, 'ds-trim-gateway.exe')
    if not os.path.exists(exe):
        sys.exit(f'找不到 {exe}，请先 PyInstaller 打包')

    # 不整体删除旧目录：里面有 AFS 的 100+ 个文件，批量删除会被安全策略拦，
    # 而且每次都重拷 230MB 太慢。改成「原地刷新」——只覆盖 exe / config / 文档，
    # AFS 子目录已存在就跳过。
    os.makedirs(OUT, exist_ok=True)

    shutil.copy2(exe, os.path.join(OUT, 'ds-trim-gateway.exe'))
    with open(os.path.join(OUT, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(make_config(PRESET_DEFAULT), f, ensure_ascii=False, indent=2)
    print('✓ exe + config.json（%s 预设）' % PRESET_DEFAULT)

    for name in ('1-先看这里.txt', '2-客户端接入.txt', '网关使用说明.txt',
                 '3-扩展工具与提示词.txt'):
        src = os.path.join(ROOT, 'package_docs', name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(OUT, name))
            print('✓ 文档', name)

    afs_dst = os.path.join(OUT, 'AFS')
    if os.path.isdir(AFS_SRC) and not os.path.isdir(afs_dst):
        print('  复制 AFS 便携版（约 230MB，稍等）…')
        shutil.copytree(AFS_SRC, afs_dst)
        print('✓ AFS/')
    elif os.path.isdir(afs_dst):
        print('✓ AFS/（已存在，跳过复制）')
    else:
        print('! 未找到 AFS 源目录，跳过（对方需自备 AFS）:', AFS_SRC)

    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _d, fs in os.walk(OUT) for f in fs)
    print('✓ 分发包体积 %.1f MB' % (total / 1048576))

    # 必须用 zipfile 打包：bsdtar / 资源管理器会把中文名按 OEM 码页写进去，
    # 到别人的 7-Zip / macOS 上就变成 ╖╓╖ó░ⁿ 这种乱码。zipfile 会置 UTF-8 标志位。
    zpath = os.path.join(DIST, 'ds-trim-gateway-分发包.zip')
    if os.path.exists(zpath):
        os.remove(zpath)
    print('  压缩中…')
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=1,
                         allowZip64=True) as z:
        for root, dirs, files in os.walk(OUT):
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(root, f)
                arc = os.path.relpath(full, DIST)
                z.write(full, arc)
    print('✓ %s（%.0f MB）' % (os.path.basename(zpath),
                              os.path.getsize(zpath) / 1048576))


if __name__ == '__main__':
    main()
