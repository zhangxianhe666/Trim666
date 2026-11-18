"""
ds-trim-gateway 启动器（图形界面，零依赖）

给最终用户用：双击 exe 就能选客户端、改配置、启停网关，不需要装 Python。

为什么不用 gradio：gradio 只为了一个配置表单，却要几百 MB 依赖、启动十几秒，
打包成单文件后体积和启动时间都不可接受。Tkinter 是标准库自带的，exe 只有
十几 MB，双击就开。

界面分五个页签：
  基础    —— 客户端预设、上游地址/模型、端口与限流
  提示词  —— 精简 system 的全文编辑（不再被预设悄悄覆盖）
  工具    —— 工具列表 JSON 直接编辑、校验、格式化
  上下文  —— 轮数/字符预算/对话历史/日期注入/本地图片等精简参数
  日志    —— 实时日志
"""
import json
import os
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from tkinter import messagebox, ttk

import gateway_core as core

APP_TITLE = 'ds-trim-gateway 启动器'
APP_SUBTITLE = 'AFS 套件之一'

# 界面上的中文标签 ↔ 配置值
SEARCH_MODE_LABELS = {
    'auto': '自动：客户端搜索失败过才由网关代搜',
    'on': '总是由网关代搜（客户端搜索确定不可用）',
    'off': '关闭：完全交给客户端搜索',
}
SEARCH_MODE_VALUES = {v: k for k, v in SEARCH_MODE_LABELS.items()}


def _show_fatal(exc: BaseException):
    """打包后没有控制台，异常必须落盘 + 弹窗，否则用户只看到「双击没反应」。"""
    detail = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        path = os.path.join(core.BASE_DIR, 'launcher-error.log')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(detail)
    except OSError:
        path = '(无法写入日志)'
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_TITLE, f'启动失败：{exc}\n\n详细信息：{path}')
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def _scrolled_text(parent, height=10, wrap='word', mono=False):
    """带纵向滚动条的 Text，返回 Text 本身（用 .frame 也无需关心，直接 pack）。"""
    frame = ttk.Frame(parent)
    frame.pack(fill='both', expand=True)
    font = ('Consolas', 9) if mono else ('Microsoft YaHei UI', 9)
    txt = tk.Text(frame, height=height, wrap=wrap, font=font,
                  undo=True, maxundo=-1, relief='solid', borderwidth=1)
    bar = ttk.Scrollbar(frame, orient='vertical', command=txt.yview)
    txt.configure(yscrollcommand=bar.set)
    bar.pack(side='right', fill='y')
    txt.pack(side='left', fill='both', expand=True)
    return txt


class Launcher:
    def __init__(self, root, server_module):
        self.root = root
        self.srv = server_module
        self.httpd = None
        self.thread = None
        # 上一次「由预设或保存」写进编辑器的内容，用于判断用户是否手改过
        self._last_loaded = ('', '')
        self._last_preset = ''

        root.title(APP_TITLE)
        # 窗口尺寸/位置按 Windows 工作区（已扣掉任务栏）算：
        # 高分屏 150% 缩放时逻辑屏只有 1280x720，写死 900x760 会让窗口底边落到任务栏下面，
        # 「保存配置/启动网关/停止」整条操作栏就看不见了（表现为"按钮不见了"）
        ax, ay, aw, ah = self._workarea(root)
        win_w = max(800, min(900, aw - 40))
        win_h = max(520, min(780, ah - 90))
        pos_x = ax + max(0, (aw - win_w) // 2)
        pos_y = ay + max(0, (ah - win_h - 60) // 2)
        root.geometry(f'{win_w}x{win_h}+{pos_x}+{pos_y}')
        root.minsize(min(800, win_w), min(520, win_h))

        cfg = core.get_config()
        self.cfg = cfg
        self.var_client = tk.StringVar(value=self._detect_client(cfg))
        self.var_base = tk.StringVar(value=cfg.get('upstream_base', 'http://127.0.0.1:8080/v1'))
        self.var_key = tk.StringVar(value=cfg.get('upstream_api_key', ''))
        self.var_model = tk.StringVar(value=cfg.get('upstream_model', 'DeepSeek-V4.1-Flash'))
        self.var_port = tk.StringVar(value='8001')
        self.var_interval = tk.IntVar(value=int(cfg.get('min_request_interval_ms', 12000) // 1000))
        self.var_retry = tk.IntVar(value=int(cfg.get('retry_backoff_ms', 60000) // 1000))
        self.var_maxsys = tk.IntVar(value=int(cfg.get('max_system_chars', 1500)))
        self.var_drop_sys = tk.BooleanVar(value=bool(cfg.get('drop_client_system', True)))

        self.var_h_rounds = tk.IntVar(value=int(cfg.get('history_max_rounds', 3)))
        self.var_h_result = tk.IntVar(value=int(cfg.get('history_result_chars', 3000)))
        self.var_h_total = tk.IntVar(value=int(cfg.get('history_total_chars', 8000)))
        self.var_dlg_on = tk.BooleanVar(value=bool(cfg.get('history_include_dialogue', True)))
        self.var_dlg_msgs = tk.IntVar(value=int(cfg.get('history_dialogue_msgs', 6)))
        self.var_dlg_chars = tk.IntVar(value=int(cfg.get('history_dialogue_chars', 800)))
        self.var_dlg_total = tk.IntVar(value=int(cfg.get('history_dialogue_total_chars', 3000)))
        self.var_date = tk.BooleanVar(value=bool(cfg.get('inject_current_date', True)))
        self.var_img_on = tk.BooleanVar(value=bool(cfg.get('local_image_enabled', True)))
        self.var_img_count = tk.IntVar(value=int(cfg.get('local_image_max_count', 4)))
        self.var_max_calls = tk.IntVar(value=int(cfg.get('max_tool_calls_per_round', 1)))
        self.var_max_rounds = tk.IntVar(value=int(cfg.get('max_total_rounds', 20)))
        self.var_search_mode = tk.StringVar(value=SEARCH_MODE_LABELS.get(
            str(cfg.get('server_search_mode') or 'auto'), SEARCH_MODE_LABELS['auto']))
        self.var_fetch_top = tk.IntVar(value=int(cfg.get('server_search_fetch_top', 2)))
        self.var_page_chars = tk.IntVar(value=int(cfg.get('server_search_page_chars', 1200)))

        self.var_status = tk.StringVar(value='未启动')

        self._build()
        self._load_from_config(cfg)

    # ── 界面 ────────────────────────────────────────────
    def _build(self):
        pad = {'padx': 10, 'pady': 6}

        head = ttk.Frame(self.root)
        head.pack(fill='x', **pad)
        ttk.Label(head, text='ds-trim-gateway', font=('Segoe UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(head, text=APP_SUBTITLE, foreground='#0a6',
                  font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(2, 0))

        # 底部固定操作条：先于 Notebook 布局（side='bottom'），
        # 这样窗口再矮也是按钮优先保留、挤压 Notebook，而不是按钮被挤出屏幕
        foot = ttk.Frame(self.root)
        foot.pack(side='bottom', fill='x', padx=10, pady=(0, 8))
        ttk.Label(foot, textvariable=self.var_status, foreground='#0a6').pack(side='left')
        ttk.Button(foot, text='打开 config.json', command=self._open_cfg_file).pack(side='right')
        ttk.Button(foot, text='打开配置目录', command=lambda: webbrowser.open(core.BASE_DIR)
                   ).pack(side='right', padx=6)
        ttk.Button(foot, text='打开日志', command=self._open_log).pack(side='right')
        self.btn_stop = ttk.Button(foot, text='停止', command=self._stop, state='disabled')
        self.btn_stop.pack(side='right', padx=6)
        self.btn_start = ttk.Button(foot, text='启动网关', command=self._start)
        self.btn_start.pack(side='right')
        self.btn_save = ttk.Button(foot, text='保存配置', command=self._save)
        self.btn_save.pack(side='right', padx=6)
        ttk.Button(foot, text='重新载入', command=self._reload_from_file).pack(side='right')

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill='both', expand=True, padx=10, pady=(2, 4))

        self.tab_basic = ttk.Frame(self.nb)
        self.tab_prompt = ttk.Frame(self.nb)
        self.tab_tools = ttk.Frame(self.nb)
        self.tab_ctx = ttk.Frame(self.nb)
        self.tab_log = ttk.Frame(self.nb)
        self.nb.add(self.tab_basic, text='  基础  ')
        self.nb.add(self.tab_prompt, text='  提示词  ')
        self.nb.add(self.tab_tools, text='  工具 (JSON)  ')
        self.nb.add(self.tab_ctx, text='  上下文精简  ')
        self.nb.add(self.tab_log, text='  日志  ')

        # 上下文精简页控件最多，窗口矮时会超出可视区：放进可纵向滚动容器
        self.ctx_body = self._scrollable(self.tab_ctx)

        self._build_basic()
        self._build_prompt()
        self._build_tools()
        self._build_ctx()
        self._build_log()

    @staticmethod
    def _workarea(root):
        """返回窗口可用区 (左, 上, 宽, 高)，已排除任务栏；单位与 Tk 的 winfo 一致。

        进程 DPI 感知与否会导致 ctypes 与 Tk 的单位不同，用屏幕宽求系数换算。
        """
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        try:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                w, h = r.right - r.left, r.bottom - r.top
                if w > 0 and h > 0:
                    k = sw / float(w)          # 物理 → 逻辑
                    if 0.4 < k < 2.5:
                        return (int(r.left * k), int(r.top * k),
                                int(w * k), int(h * k))
        except Exception:
            pass
        return 0, 0, sw, sh

    def _scrollable(self, parent):
        """把 parent 变成可纵向滚动的容器，返回内部 Frame（控件挂到它上面）。"""
        try:
            bg = ttk.Style().lookup('TFrame', 'background') or self.root.cget('background')
        except Exception:
            bg = '#f0f0f0'

        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0, background=bg)
        vsb = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')

        def _sync_region(_e=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
            # 内容比可视区矮时隐藏滚动条，避免右侧永远有一条空条
            need = inner.winfo_reqheight() > canvas.winfo_height()
            if need and not vsb.winfo_ismapped():
                vsb.pack(side='right', fill='y')
            elif not need and vsb.winfo_ismapped():
                vsb.pack_forget()

        def _fit_width(e):
            canvas.itemconfigure(win, width=e.width)

        def _wheel(e):
            if inner.winfo_reqheight() <= canvas.winfo_height():
                return
            canvas.yview_scroll(-3 if e.delta > 0 else 3, 'units')

        def _bind_wheel(w):
            w.bind('<MouseWheel>', _wheel)
            for c in w.winfo_children():
                _bind_wheel(c)

        def _on_inner(_e=None):
            _sync_region()
            _bind_wheel(inner)

        inner.bind('<Configure>', _on_inner)
        canvas.bind('<Configure>', _fit_width)
        return inner

    # --- 页签 1：基础 ---
    def _build_basic(self):
        pad = {'padx': 10, 'pady': 6}
        box = ttk.LabelFrame(self.tab_basic, text='1. 选择你在用的客户端')
        box.pack(fill='x', **pad)
        for name, preset in core.TOOL_PRESETS.items():
            ttk.Radiobutton(
                box, text=f'{name}   工具：{preset.get("tool_summary", "")}',
                value=name, variable=self.var_client, command=self._on_client_change,
            ).pack(anchor='w', padx=12, pady=3)
        ttk.Label(
            box,
            text='两个客户端的工具名和参数形状不通用：选错会表现为「工具调用总是失败」。\n'
                 '切换客户端会把该预设的提示词与工具填进对应页签；若你手改过，会先问你要不要覆盖。',
            foreground='#777', wraplength=820, justify='left',
        ).pack(anchor='w', padx=12, pady=(2, 8))

        box2 = ttk.LabelFrame(self.tab_basic, text='2. 上游（AFS）')
        box2.pack(fill='x', **pad)
        row = ttk.Frame(box2)
        row.pack(fill='x', padx=12, pady=4)
        ttk.Label(row, text='地址', width=8).pack(side='left')
        ttk.Entry(row, textvariable=self.var_base, width=46).pack(side='left')
        ttk.Button(row, text='测试连接', command=self._test_upstream).pack(side='left', padx=8)

        row_k = ttk.Frame(box2)
        row_k.pack(fill='x', padx=12, pady=4)
        ttk.Label(row_k, text='API Key', width=8).pack(side='left')
        ttk.Entry(row_k, textvariable=self.var_key, width=46, show='*').pack(side='left')
        ttk.Label(row_k, text='（AFS 免 key 可留空）', foreground='#888').pack(side='left', padx=6)

        row2 = ttk.Frame(box2)
        row2.pack(fill='x', padx=12, pady=4)
        ttk.Label(row2, text='模型', width=8).pack(side='left')
        ttk.Entry(row2, textvariable=self.var_model, width=32).pack(side='left')
        ttk.Button(row2, text='列出上游模型', command=self._list_models).pack(side='left', padx=8)
        ttk.Label(row2, text='必须是 /v1/models 里的 id，大小写敏感',
                  foreground='#888').pack(side='left')

        box3 = ttk.LabelFrame(self.tab_basic, text='3. 运行')
        box3.pack(fill='x', **pad)
        row3 = ttk.Frame(box3)
        row3.pack(fill='x', padx=12, pady=4)
        ttk.Label(row3, text='端口', width=8).pack(side='left')
        ttk.Entry(row3, textvariable=self.var_port, width=8).pack(side='left')
        ttk.Label(row3, text='   请求最小间隔(秒)').pack(side='left')
        ttk.Spinbox(row3, from_=0, to=120, textvariable=self.var_interval,
                    width=6).pack(side='left', padx=4)
        ttk.Label(row3, text='   空响应冷却(秒)').pack(side='left')
        ttk.Spinbox(row3, from_=0, to=600, textvariable=self.var_retry,
                    width=6).pack(side='left', padx=4)
        ttk.Label(box3, text='间隔越小越快，但太小易触发上游风控；冷却用于空响应后等限流窗口过去。',
                  foreground='#888').pack(anchor='w', padx=12, pady=(0, 8))

    # --- 页签 2：提示词 ---
    def _build_prompt(self):
        ttk.Label(
            self.tab_prompt,
            text='这就是最终发给上游的 system（客户端自己的 system 默认被丢弃）。\n'
                 '改完点底部「保存配置」即写入 config.json，网关会热重载。',
            foreground='#777', justify='left',
        ).pack(anchor='w', padx=12, pady=(10, 4))

        self.txt_prompt = _scrolled_text(self.tab_prompt, height=18, wrap='word')

        row = ttk.Frame(self.tab_prompt)
        row.pack(fill='x', padx=10, pady=8)
        ttk.Checkbutton(row, text='丢弃客户端的 system，只用上面这段',
                        variable=self.var_drop_sys).pack(side='left')
        ttk.Label(row, text='   system 最大字符数').pack(side='left')
        ttk.Spinbox(row, from_=0, to=20000, increment=100,
                    textvariable=self.var_maxsys, width=8).pack(side='left', padx=4)
        ttk.Label(row, text='（0=不截断）', foreground='#888').pack(side='left')
        ttk.Button(row, text='载入当前预设的提示词',
                   command=lambda: self._fill_prompt_from_preset(ask=True)).pack(side='right')

    # --- 页签 3：工具 JSON ---
    def _build_tools(self):
        ttk.Label(
            self.tab_tools,
            text='精简后的工具列表（OpenAI function 定义数组）。名字与参数必须和客户端真实工具一致，\n'
                 '否则客户端严格校验会直接拒绝。改完点「校验并格式化」，再点底部「保存配置」。',
            foreground='#777', justify='left',
        ).pack(anchor='w', padx=12, pady=(10, 4))

        self.txt_tools = _scrolled_text(self.tab_tools, height=18, wrap='none', mono=True)

        row = ttk.Frame(self.tab_tools)
        row.pack(fill='x', padx=10, pady=8)
        ttk.Button(row, text='校验并格式化', command=self._validate_tools).pack(side='left')
        ttk.Button(row, text='载入当前预设的工具',
                   command=lambda: self._fill_tools_from_preset(ask=True)).pack(side='left', padx=6)
        self.var_toolinfo = tk.StringVar(value='')
        ttk.Label(row, textvariable=self.var_toolinfo, foreground='#0a6').pack(side='left', padx=10)

    # --- 页签 4：上下文精简参数 ---
    def _build_ctx(self):
        pad = {'padx': 10, 'pady': 6}
        ttk.Label(
            self.ctx_body,
            text='这些参数决定「转发给上游的东西有多长」。太长触发上游禁言，太短模型会失忆重复取数。',
            foreground='#777',
        ).pack(anchor='w', padx=12, pady=(10, 2))

        g1 = ttk.LabelFrame(self.ctx_body, text='工具调用历史（此前已执行的工具与结果）')
        g1.pack(fill='x', **pad)
        r = ttk.Frame(g1)
        r.pack(fill='x', padx=12, pady=6)
        ttk.Label(r, text='保留最近轮数').pack(side='left')
        ttk.Spinbox(r, from_=0, to=20, textvariable=self.var_h_rounds,
                    width=6).pack(side='left', padx=(4, 18))
        ttk.Label(r, text='单条结果截断字符').pack(side='left')
        ttk.Spinbox(r, from_=0, to=50000, increment=500, textvariable=self.var_h_result,
                    width=8).pack(side='left', padx=(4, 18))
        ttk.Label(r, text='工具历史总字符预算').pack(side='left')
        ttk.Spinbox(r, from_=0, to=200000, increment=1000, textvariable=self.var_h_total,
                    width=9).pack(side='left', padx=4)
        ttk.Label(g1, text='0 表示不限。总预算超出时从最早一轮开始丢。',
                  foreground='#888').pack(anchor='w', padx=12, pady=(0, 6))

        g2 = ttk.LabelFrame(self.ctx_body, text='对话历史（用户提问 + 助手回答）')
        g2.pack(fill='x', **pad)
        ttk.Checkbutton(
            g2, text='带上最近几轮对话历史（关掉后只发最后一条提问，带指代的追问会失忆）',
            variable=self.var_dlg_on,
        ).pack(anchor='w', padx=12, pady=(6, 2))
        r2 = ttk.Frame(g2)
        r2.pack(fill='x', padx=12, pady=6)
        ttk.Label(r2, text='保留条数(user+assistant)').pack(side='left')
        ttk.Spinbox(r2, from_=0, to=50, textvariable=self.var_dlg_msgs,
                    width=6).pack(side='left', padx=(4, 18))
        ttk.Label(r2, text='单条截断字符').pack(side='left')
        ttk.Spinbox(r2, from_=0, to=20000, increment=100, textvariable=self.var_dlg_chars,
                    width=8).pack(side='left', padx=(4, 18))
        ttk.Label(r2, text='对话历史总字符预算').pack(side='left')
        ttk.Spinbox(r2, from_=0, to=100000, increment=500, textvariable=self.var_dlg_total,
                    width=9).pack(side='left', padx=4)

        g3 = ttk.LabelFrame(self.ctx_body, text='其他')
        g3.pack(fill='x', **pad)
        ttk.Checkbutton(g3, text='在 system 里注入当前日期（避免模型靠猜日期）',
                        variable=self.var_date).pack(anchor='w', padx=12, pady=(6, 2))
        srow = ttk.Frame(g3)
        srow.pack(fill='x', padx=12, pady=6)
        ttk.Checkbutton(srow, text='允许本地图片通道',
                        variable=self.var_img_on).pack(side='left')
        ttk.Label(srow, text='   单次最多图片数').pack(side='left')
        ttk.Spinbox(srow, from_=1, to=20, textvariable=self.var_img_count,
                    width=6).pack(side='left', padx=4)
        ttk.Label(srow, text='（客户端只发 @文件名 时，网关注入标记让 AFS 读取本地文件）',
                  foreground='#888').pack(side='left', padx=6)

        # 一轮多个工具调用会被客户端拼成一个名字：
        # ['PowerShell','PowerShell'] → "PowerShellPowerShell does not exist"。
        # 客户端侧改不了，只能在网关交付前收敛。
        crow = ttk.Frame(g3)
        crow.pack(fill='x', padx=12, pady=(0, 8))
        ttk.Label(crow, text='单轮最多工具调用').pack(side='left')
        ttk.Spinbox(crow, from_=0, to=10, textvariable=self.var_max_calls,
                    width=5).pack(side='left', padx=4)
        ttk.Label(crow, text='（0=不限；客户端会把多个名字拼成一个导致「工具不存在」，建议 1）',
                  foreground='#888').pack(side='left', padx=6)

        # 失控循环的硬闸门：模型不写正文、只反复调工具时，靠这一项把总请求数锁死。
        # 判据取 messages 里 assistant.tool_calls 的条数与进程内计数的大者，
        # 所以客户端自动压缩历史后计数器不会归零。
        rrow = ttk.Frame(g3)
        rrow.pack(fill='x', padx=12, pady=(0, 4))
        ttk.Label(rrow, text='单次任务最多轮数').pack(side='left')
        ttk.Spinbox(rrow, from_=0, to=200, textvariable=self.var_max_rounds,
                    width=6).pack(side='left', padx=4)
        ttk.Label(rrow, text='（0=不限；超限直接返回停止，不再打上游）',
                  foreground='#888').pack(side='left', padx=6)
        ttk.Label(g3, text='用途：模型陷在「反复调工具、始终不写正文」的循环里时，'
                           '用这个数字把它截断。正常任务（检索→核验→写）8–12 轮足够，'
                           '留余量给 20；它按单个任务计数，跨任务不累计。',
                  foreground='#888', wraplength=820,
                  justify='left').pack(anchor='w', padx=12, pady=(0, 8))

        # 搜索兜底：DSH 的 web_search 不在本地跑，是客户端插件直连 api.deepseek.com，
        # Key 失效就固定 401；这条路绕开了网关，只能由网关自己搜。
        g4 = ttk.LabelFrame(self.ctx_body, text='搜索（客户端搜索坏了就靠这里）')
        g4.pack(fill='x', **pad)
        sr1 = ttk.Frame(g4)
        sr1.pack(fill='x', padx=12, pady=(6, 2))
        ttk.Label(sr1, text='模式').pack(side='left')
        ttk.Combobox(sr1, textvariable=self.var_search_mode, state='readonly', width=36,
                     values=list(SEARCH_MODE_LABELS.values())).pack(side='left', padx=4)
        ttk.Label(g4, text='现象：客户端里让它查天气，它反复调搜索却拿不到数据、最后凭记忆作答。\n'
                           '原因：DSH 的 web_search 插件用客户端自己的 DeepSeek API Key 直连 '
                           'api.deepseek.com，Key 失效会固定返回 401（聊天链路走网关，不受影响）。',
                  foreground='#888', wraplength=820, justify='left').pack(anchor='w', padx=12)
        sr2 = ttk.Frame(g4)
        sr2.pack(fill='x', padx=12, pady=(2, 6))
        ttk.Label(sr2, text='抓取页面数').pack(side='left')
        ttk.Spinbox(sr2, from_=0, to=5, textvariable=self.var_fetch_top,
                    width=5).pack(side='left', padx=(4, 16))
        ttk.Label(sr2, text='每页正文字符').pack(side='left')
        ttk.Spinbox(sr2, from_=0, to=6000, increment=200, textvariable=self.var_page_chars,
                    width=7).pack(side='left', padx=4)
        ttk.Label(sr2, text='（0=只给标题和摘要）', foreground='#888').pack(side='left')

        r4 = ttk.Frame(self.ctx_body)
        r4.pack(fill='x', padx=10, pady=8)
        ttk.Button(r4, text='恢复默认精简参数', command=self._reset_ctx).pack(side='left')

    # --- 页签 5：日志 ---
    def _build_log(self):
        self.txt = tk.Text(self.tab_log, height=16, wrap='none', font=('Consolas', 9))
        bar = ttk.Scrollbar(self.tab_log, orient='vertical', command=self.txt.yview)
        self.txt.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.txt.pack(fill='both', expand=True, padx=(10, 0), pady=6)
        row = ttk.Frame(self.tab_log)
        row.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Button(row, text='刷新日志', command=self._dump_log_tail).pack(side='left')
        ttk.Button(row, text='清空窗口', command=lambda: self.txt.delete('1.0', 'end')
                   ).pack(side='left', padx=6)

    # ── 配置读写 ────────────────────────────────────────
    def _detect_client(self, cfg):
        got = core.detect_preset_name(cfg.get('tools'))
        return got or core.DEFAULT_PRESET_NAME

    def _preset_payload(self, name):
        preset = core.TOOL_PRESETS.get(name) or {}
        return (preset.get('system_prompt', ''),
                json.dumps(preset.get('tools', []), ensure_ascii=False, indent=2))

    def _editor_content(self):
        return (self.txt_prompt.get('1.0', 'end').strip(),
                self.txt_tools.get('1.0', 'end').strip())

    def _set_editors(self, prompt, tools_json, preset_name):
        self.txt_prompt.delete('1.0', 'end')
        self.txt_prompt.insert('1.0', prompt or '')
        self.txt_tools.delete('1.0', 'end')
        self.txt_tools.insert('1.0', tools_json or '')
        self._last_loaded = ((prompt or '').strip(), (tools_json or '').strip())
        self._last_preset = preset_name

    def _load_from_config(self, cfg):
        """把 config.json 的当前值灌进编辑器（外部手改过也能载进来）。"""
        tools = cfg.get('tools', []) or []
        name = core.detect_preset_name(tools) or self.var_client.get()
        prompt = (cfg.get('system_prompt') or '').strip()
        if not prompt:
            prompt = self._preset_payload(name)[0]
        self.var_client.set(name)
        self._set_editors(prompt, json.dumps(tools, ensure_ascii=False, indent=2), name)
        self._refresh_toolinfo()

    def _reload_from_file(self):
        core.load_config()
        cfg = core.get_config()
        self._load_from_config(cfg)
        self.var_base.set(cfg.get('upstream_base', ''))
        self.var_key.set(cfg.get('upstream_api_key', ''))
        self.var_model.set(cfg.get('upstream_model', ''))
        self.var_interval.set(int(cfg.get('min_request_interval_ms', 12000) // 1000))
        self.var_retry.set(int(cfg.get('retry_backoff_ms', 60000) // 1000))
        self.var_maxsys.set(int(cfg.get('max_system_chars', 1500)))
        self.var_drop_sys.set(bool(cfg.get('drop_client_system', True)))
        self.var_h_rounds.set(int(cfg.get('history_max_rounds', 3)))
        self.var_h_result.set(int(cfg.get('history_result_chars', 3000)))
        self.var_h_total.set(int(cfg.get('history_total_chars', 8000)))
        self.var_dlg_on.set(bool(cfg.get('history_include_dialogue', True)))
        self.var_dlg_msgs.set(int(cfg.get('history_dialogue_msgs', 6)))
        self.var_dlg_chars.set(int(cfg.get('history_dialogue_chars', 800)))
        self.var_dlg_total.set(int(cfg.get('history_dialogue_total_chars', 3000)))
        self.var_date.set(bool(cfg.get('inject_current_date', True)))
        self.var_img_on.set(bool(cfg.get('local_image_enabled', True)))
        self.var_img_count.set(int(cfg.get('local_image_max_count', 4)))
        self.var_max_calls.set(int(cfg.get('max_tool_calls_per_round', 1)))
        self.var_max_rounds.set(int(cfg.get('max_total_rounds', 20)))
        self.var_search_mode.set(SEARCH_MODE_LABELS.get(
            str(cfg.get('server_search_mode') or 'auto'), SEARCH_MODE_LABELS['auto']))
        self.var_fetch_top.set(int(cfg.get('server_search_fetch_top', 2)))
        self.var_page_chars.set(int(cfg.get('server_search_page_chars', 1200)))
        self._append_log(f'[载入] 已从 {core.CONFIG_PATH} 重新读取配置')
        self.var_status.set('配置已重新载入')

    def _on_client_change(self):
        """切客户端：把预设填进编辑器；用户手改过则先确认。"""
        name = self.var_client.get()
        if not core.TOOL_PRESETS.get(name):
            return
        prompt, tools_json = self._preset_payload(name)
        if self._editor_content() == (prompt.strip(), tools_json.strip()):
            self._set_editors(prompt, tools_json, name)
            return
        if self._editor_content() != self._last_loaded:
            if not messagebox.askyesno(
                APP_TITLE,
                f'提示词或工具列表已被你修改过。\n\n'
                f'是否用「{name}」预设覆盖它们？\n'
                f'选「否」会保留当前编辑内容（工具名可能与新客户端不匹配）。',
            ):
                self.var_client.set(self._last_preset)
                return
        self._set_editors(prompt, tools_json, name)
        self._append_log(f'[选中] {name}\n[工具] '
                         + ', '.join(t['name'] for t in core.TOOL_PRESETS[name]['tools']))
        self._refresh_toolinfo()

    def _fill_prompt_from_preset(self, ask=False):
        name = self.var_client.get()
        prompt, _ = self._preset_payload(name)
        if ask and self.txt_prompt.get('1.0', 'end').strip() != prompt.strip():
            if not messagebox.askyesno(APP_TITLE, f'用「{name}」预设提示词覆盖当前内容？'):
                return
        self.txt_prompt.delete('1.0', 'end')
        self.txt_prompt.insert('1.0', prompt)
        self._last_loaded = (prompt.strip(), self.txt_tools.get('1.0', 'end').strip())
        self.var_status.set(f'已载入「{name}」预设提示词（未保存）')

    def _fill_tools_from_preset(self, ask=False):
        name = self.var_client.get()
        _, tools_json = self._preset_payload(name)
        if ask and self.txt_tools.get('1.0', 'end').strip() != tools_json.strip():
            if not messagebox.askyesno(APP_TITLE, f'用「{name}」预设工具列表覆盖当前内容？'):
                return
        self.txt_tools.delete('1.0', 'end')
        self.txt_tools.insert('1.0', tools_json)
        self._last_loaded = (self.txt_prompt.get('1.0', 'end').strip(), tools_json.strip())
        self._refresh_toolinfo()

    def _parse_tools(self):
        raw = self.txt_tools.get('1.0', 'end').strip()
        if not raw:
            raise ValueError('工具列表不能为空（至少要有客户端真实存在的工具）')
        try:
            tools = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f'JSON 语法错误：第 {e.lineno} 行第 {e.colno} 列 — {e.msg}') from None
        if not isinstance(tools, list):
            raise ValueError('工具列表必须是 JSON 数组，例如 [ {"name": ...}, ... ]')
        norm = []
        for i, t in enumerate(tools, 1):
            if isinstance(t, dict) and 'function' in t:
                norm.append(t['function'])
            elif isinstance(t, dict) and 'name' in t:
                norm.append(t)
            else:
                raise ValueError(f'第 {i} 项无效：需要 name/description/parameters，'
                                 f'或带 function 包裹的 OpenAI 定义')
        return norm

    def _validate_tools(self):
        try:
            norm = self._parse_tools()
        except ValueError as e:
            self.var_toolinfo.set('')
            messagebox.showerror(APP_TITLE, str(e))
            return None
        pretty = json.dumps(norm, ensure_ascii=False, indent=2)
        self.txt_tools.delete('1.0', 'end')
        self.txt_tools.insert('1.0', pretty)
        names = ', '.join(str(t.get('name', '?')) for t in norm)
        self.var_toolinfo.set(f'✅ {len(norm)} 个工具：{names}')
        return norm

    def _refresh_toolinfo(self):
        try:
            norm = json.loads(self.txt_tools.get('1.0', 'end').strip() or '[]')
            n = len(norm) if isinstance(norm, list) else 0
            self.var_toolinfo.set(f'当前 {n} 个工具')
        except Exception:  # noqa: BLE001
            self.var_toolinfo.set('当前 JSON 有语法错误')

    def _reset_ctx(self):
        d = core.DEFAULT_CONFIG
        self.var_h_rounds.set(int(d['history_max_rounds']))
        self.var_h_result.set(int(d['history_result_chars']))
        self.var_h_total.set(int(d['history_total_chars']))
        self.var_dlg_on.set(bool(d['history_include_dialogue']))
        self.var_dlg_msgs.set(int(d['history_dialogue_msgs']))
        self.var_dlg_chars.set(int(d['history_dialogue_chars']))
        self.var_dlg_total.set(int(d['history_dialogue_total_chars']))
        self.var_date.set(bool(d['inject_current_date']))
        self.var_img_on.set(bool(d['local_image_enabled']))
        self.var_img_count.set(int(d['local_image_max_count']))
        self.var_max_calls.set(int(d.get('max_tool_calls_per_round', 1)))
        self.var_max_rounds.set(int(d.get('max_total_rounds', 20)))
        self.var_search_mode.set(SEARCH_MODE_LABELS[str(d.get('server_search_mode', 'auto'))])
        self.var_fetch_top.set(int(d['server_search_fetch_top']))
        self.var_page_chars.set(int(d['server_search_page_chars']))
        self.var_status.set('精简参数已恢复默认（未保存）')

    def _int(self, var, label):
        try:
            return int(var.get())
        except Exception:  # noqa: BLE001
            raise ValueError(f'「{label}」必须是整数') from None

    def _apply_form(self):
        """把界面全部值写进 config.json。

        注意：tools 与 system_prompt 取**编辑器里的内容**，不再被预设覆盖 ——
        否则用户改的东西会在保存瞬间被打回原形。
        """
        tools = self._parse_tools()  # 失败抛 ValueError，由调用方提示
        prompt = self.txt_prompt.get('1.0', 'end').strip()
        if not prompt:
            raise ValueError('system 提示词不能为空（模型要靠它输出 XML 工具调用）')
        patch = {
            'upstream_base': self.var_base.get().strip().rstrip('/'),
            'upstream_api_key': self.var_key.get().strip(),
            'upstream_model': self.var_model.get().strip(),
            'system_prompt': prompt,
            'tools': tools,
            'drop_client_system': bool(self.var_drop_sys.get()),
            'max_system_chars': self._int(self.var_maxsys, 'system 最大字符数'),
            'retry_backoff_ms': self._int(self.var_retry, '空响应冷却') * 1000,
            'min_request_interval_ms': self._int(self.var_interval, '请求最小间隔') * 1000,
            'history_max_rounds': self._int(self.var_h_rounds, '保留最近轮数'),
            'history_result_chars': self._int(self.var_h_result, '单条结果截断字符'),
            'history_total_chars': self._int(self.var_h_total, '工具历史总字符预算'),
            'history_include_dialogue': bool(self.var_dlg_on.get()),
            'history_dialogue_msgs': self._int(self.var_dlg_msgs, '对话保留条数'),
            'history_dialogue_chars': self._int(self.var_dlg_chars, '对话单条截断字符'),
            'history_dialogue_total_chars': self._int(self.var_dlg_total,
                                                     '对话历史总字符预算'),
            'inject_current_date': bool(self.var_date.get()),
            'local_image_enabled': bool(self.var_img_on.get()),
            'local_image_max_count': self._int(self.var_img_count, '单次最多图片数'),
            'max_tool_calls_per_round': self._int(self.var_max_calls, '单轮最多工具调用'),
            'max_total_rounds': self._int(self.var_max_rounds, '单次任务最多轮数'),
            'server_search_mode': SEARCH_MODE_VALUES.get(self.var_search_mode.get(), 'auto'),
            'server_search_fetch_top': self._int(self.var_fetch_top, '抓取页面数'),
            'server_search_page_chars': self._int(self.var_page_chars, '每页正文字符'),
        }
        core.update_config(patch)
        self._last_loaded = (prompt, self.txt_tools.get('1.0', 'end').strip())
        self._last_preset = self.var_client.get()
        return self.var_client.get(), len(tools)

    def _save(self):
        try:
            name, n = self._apply_form()
        except ValueError as e:
            messagebox.showerror(APP_TITLE, f'保存失败：{e}')
            self.var_status.set('保存失败')
            return
        self._append_log(f'[保存] 客户端={name} 工具 {n} 个 → {core.CONFIG_PATH}')
        self.var_status.set(f'配置已保存（{n} 个工具）· 网关热重载')

    # ── 动作 ────────────────────────────────────────────
    def _test_upstream(self):
        import requests
        base = self.var_base.get().strip().rstrip('/')
        try:
            r = requests.get(f'{base}/models', timeout=5)
            self._append_log(f'[上游] {base}/models → HTTP {r.status_code}')
            self.var_status.set(f'上游可达（HTTP {r.status_code}）')
        except Exception as e:  # noqa: BLE001
            self._append_log(f'[上游] 不可达：{e}')
            self.var_status.set('上游不可达')

    def _list_models(self):
        import requests
        base = self.var_base.get().strip().rstrip('/')
        try:
            r = requests.get(f'{base}/models', timeout=8)
            data = (r.json() or {}).get('data') or []
            ids = [str(m.get('id')) for m in data if isinstance(m, dict)]
        except Exception as e:  # noqa: BLE001
            self._append_log(f'[上游] 取模型列表失败：{e}')
            return
        if not ids:
            self._append_log('[上游] 模型列表为空')
            return
        self._append_log('[上游] 可用模型：' + ', '.join(ids))
        # 当前模型不在列表里时直接提示，这是最常见的一类 503 根因
        cur = self.var_model.get().strip()
        if cur and cur not in ids:
            self._append_log(f'[警告] 当前模型 {cur} 不在列表里，上游会返回 '
                             f'503 "No available account for model"')
            if messagebox.askyesno(APP_TITLE, f'当前模型 {cur} 不在上游列表里。\n'
                                              f'是否改用 {ids[0]}？'):
                self.var_model.set(ids[0])

    def _start(self):
        if self.httpd is not None:
            return
        try:
            name, n = self._apply_form()
        except ValueError as e:
            messagebox.showerror(APP_TITLE, f'配置有误：{e}')
            return
        try:
            port = int(self.var_port.get())
        except Exception:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, '端口必须是整数')
            return
        srv = self.srv
        try:
            from werkzeug.serving import make_server
            self.httpd = make_server('127.0.0.1', port, srv.app, threaded=True)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f'端口 {port} 启动失败：{e}')
            self.httpd = None
            return

        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        core.log(f'启动器已启动网关: http://127.0.0.1:{port}（客户端={name}，工具 {n} 个）')
        self._append_log(f'[启动] http://127.0.0.1:{port}  客户端={name}  工具 {n} 个')
        self.var_status.set(f'运行中 · http://127.0.0.1:{port}')
        self.btn_start.config(state='disabled')
        self.btn_stop.config(state='normal')
        self.nb.select(self.tab_log)
        self._poll_log()

    def _stop(self):
        if self.httpd is None:
            return
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.httpd = None
        self._append_log('[停止] 网关已停止')
        self.var_status.set('已停止')
        self.btn_start.config(state='normal')
        self.btn_stop.config(state='disabled')

    def _open_log(self):
        try:
            os.startfile(core.LOG_FILE)  # noqa: S606
        except Exception:  # noqa: BLE001
            self._append_log(f'日志文件：{core.LOG_FILE}')

    def _open_cfg_file(self):
        try:
            os.startfile(core.CONFIG_PATH)  # noqa: S606
        except Exception:  # noqa: BLE001
            self._append_log(f'配置文件：{core.CONFIG_PATH}')

    # ── 日志 ────────────────────────────────────────────
    def _append_log(self, text):
        self.txt.insert('end', text.rstrip() + '\n')
        self.txt.see('end')

    def _dump_log_tail(self, lines=200):
        """整个文件尾部重新灌一遍，方便用户翻完整日志。"""
        try:
            with open(core.LOG_FILE, 'r', encoding='utf-8', errors='replace') as fh:
                tail = fh.readlines()[-lines:]
        except Exception:  # noqa: BLE001
            self._append_log(f'读取日志失败：{core.LOG_FILE}')
            return
        self.txt.delete('1.0', 'end')
        self.txt.insert('1.0', ''.join(tail))
        self.txt.see('end')

    def _poll_log(self):
        """把网关日志的增量刷进窗口；比另开控制台省事，也方便用户截图反馈。"""
        try:
            if os.path.isfile(core.LOG_FILE):
                size = os.path.getsize(core.LOG_FILE)
                if getattr(self, '_log_pos', None) is None:
                    self._log_pos = max(0, size - 4000)
                if size < self._log_pos:
                    self._log_pos = 0
                if size > self._log_pos:
                    with open(core.LOG_FILE, 'rb') as fh:
                        fh.seek(self._log_pos)
                        chunk = fh.read()
                    self._log_pos = fh.tell()
                    text = chunk.decode('utf-8', 'replace')
                    if text:
                        self._append_log(text.rstrip())
        except Exception:  # noqa: BLE001
            pass
        if self.httpd is not None:
            self.root.after(1000, self._poll_log)


def _selftest():
    """无窗口自检：验证打包后的 flask/werkzeug/requests 都能正常工作。

    打出来的 exe 是 --windowed（没有控制台），出问题时用户只看到"双击没反应"。
    这个模式把结果写到文件里，维护者可以 `ds-trim-gateway.exe --selftest` 一次定性。
    """
    lines = []
    ok = True
    try:
        import requests
        from werkzeug.serving import make_server

        import server as srv
        lines.append(f'模块导入 OK: flask app = {type(srv.app).__name__}')
        cfg = core.get_config()
        lines.append(f'配置路径: {core.CONFIG_PATH}')
        lines.append(f'客户端预设识别: {core.detect_preset_name(cfg.get("tools"))}')
        lines.append(f'上游: {cfg.get("upstream_base")}  模型: {cfg.get("upstream_model")}')

        port = 8123
        httpd = make_server('127.0.0.1', port, srv.app, threaded=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        r = requests.get(f'http://127.0.0.1:{port}/health', timeout=10)
        lines.append(f'/health → HTTP {r.status_code} {r.text[:120]}')
        ok = ok and r.status_code == 200
        r2 = requests.get(f'http://127.0.0.1:{port}/v1/models', timeout=10)
        lines.append(f'/v1/models → HTTP {r2.status_code} {r2.text[:160]}')
        ok = ok and r2.status_code == 200

        # 真跑一次对话：验证打包后的 requests 到上游链路（含 AFS 鉴权/模型名）
        try:
            r3 = requests.post(
                f'http://127.0.0.1:{port}/v1/chat/completions',
                json={'model': cfg.get('upstream_model'),
                      'messages': [{'role': 'user', 'content': '回复 OK 两个字母'}],
                      'stream': False},
                timeout=120)
            body = r3.text[:200]
            lines.append(f'/v1/chat/completions → HTTP {r3.status_code} {body}')
            if r3.status_code != 200:
                lines.append('  ↑ 上游不通或模型名不对，网关本身是好的')
        except Exception as exc:  # noqa: BLE001
            lines.append(f'/v1/chat/completions 异常: {exc}')

        httpd.shutdown()
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append('异常: ' + ''.join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)))
    lines.append('SELFTEST ' + ('PASS' if ok else 'FAIL'))
    text = '\n'.join(lines)
    try:
        with open(os.path.join(core.BASE_DIR, 'selftest-result.txt'), 'w',
                  encoding='utf-8') as fh:
            fh.write(text + '\n')
    except OSError:
        pass
    return 0 if ok else 1


def _serve(port=8001):
    """无界面模式：直接在当前进程里把网关跑起来（常驻后台 / 自动化用）。

    为什么要这个模式：exe 是 --windowed 的，正常用法是双击后点「启动网关」；
    但自动化测试和「开机就让它躺着跑」的场景需要一条不需要点界面的路径。
    日志照旧走 core.log → %TEMP%\\ds-gw-server.log，与界面共用同一个文件。
    """
    import server as srv
    from werkzeug.serving import make_server

    srv._safe_streams()
    httpd = make_server('127.0.0.1', port, srv.app, threaded=True)
    core.log(f'无界面模式已启动: http://127.0.0.1:{port}'
             f'（客户端={core.detect_preset_name(core.get_config().get("tools"))}）')
    httpd.serve_forever()
    return 0


def main():
    if '--serve' in sys.argv:
        port = 8001
        if '--port' in sys.argv:
            idx = sys.argv.index('--port')
            if idx + 1 < len(sys.argv):
                port = int(sys.argv[idx + 1])
        return _serve(port)

    root = tk.Tk()
    try:
        import server as srv
    except Exception as e:  # noqa: BLE001
        _show_fatal(e)
        return
    Launcher(root, srv)
    root.mainloop()


if __name__ == '__main__':
    try:
        if '--selftest' in sys.argv:
            sys.exit(_selftest())
        main()
    except Exception as exc:  # noqa: BLE001
        _show_fatal(exc)
