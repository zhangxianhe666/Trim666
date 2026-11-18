"""
ds-trim-gateway — Gradio 配置界面

提供可视化维护：
- 上游 AFS 配置（base / key / model）
- 精简 system 提示词
- 精简工具列表（JSON 编辑，可直接粘贴 OpenAI 格式 function 定义数组）
- 是否丢弃客户端 system、system 长度上限
- 实时日志、健康检查

启动（与 server.py 分开，便于调试）：
    .venv/bin/python ui.py            # 默认 127.0.0.1:7860
"""
import argparse
import json

import gradio as gr

import gateway_core as core


def load_for_ui():
    cfg = core.get_config()
    tools = cfg.get('tools', [])
    return (
        cfg.get('upstream_base', ''),
        cfg.get('upstream_api_key', ''),
        cfg.get('upstream_model', ''),
        cfg.get('system_prompt', ''),
        json.dumps(tools, ensure_ascii=False, indent=2),
        bool(cfg.get('drop_client_system', True)),
        int(cfg.get('max_system_chars', 1500)),
        int(cfg.get('retry_backoff_ms', 60000)) // 1000,  # UI 显示秒
        int(cfg.get('min_request_interval_ms', 12000)) // 1000,  # UI 显示秒
        core.detect_preset_name(tools) or '',             # 预设回显
        bool(cfg.get('history_include_dialogue', True)),  # 带对话历史
        int(cfg.get('history_dialogue_msgs', 6)),         # 最近 N 条
        int(cfg.get('history_dialogue_total_chars', 3000)),  # 对话历史字符预算
    )


def apply_preset(name):
    """把选中预设的 system 提示词与工具列表填进表单（不落盘，需再点保存）。"""
    preset = core.TOOL_PRESETS.get(name)
    if not preset:
        return gr.update(), gr.update(), f'❌ 未知预设: {name}'
    tools_json = json.dumps(preset['tools'], ensure_ascii=False, indent=2)
    summary = preset.get('tool_summary') or preset['tools'][0]['name']
    return (
        preset['system_prompt'],
        tools_json,
        f"已载入「{name}」预设（{summary}）——"
        f"确认无误后点「保存配置」才会生效。",
    )


def save_from_ui(base, key, model, sys_prompt, tools_json, drop_sys, max_chars,
                 retry_ms, min_interval_s, include_dialogue, dialogue_msgs,
                 dialogue_chars):
    try:
        tools = json.loads(tools_json) if tools_json.strip() else []
        if not isinstance(tools, list):
            raise ValueError('工具列表必须是 JSON 数组')
        # 规范化：允许省略 type/function 包裹
        norm = []
        for t in tools:
            if isinstance(t, dict) and 'function' in t:
                norm.append(t['function'])
            elif isinstance(t, dict) and 'name' in t:
                norm.append(t)
            else:
                raise ValueError(f'无效工具项: {t}')
        core.update_config({
            'upstream_base': base.strip(),
            'upstream_api_key': key.strip(),
            'upstream_model': model.strip(),
            'system_prompt': sys_prompt,
            'tools': norm,
            'drop_client_system': bool(drop_sys),
            'max_system_chars': int(max_chars),
            'retry_backoff_ms': int(retry_ms) * 1000,  # UI 显示秒，内部存毫秒
            'min_request_interval_ms': int(min_interval_s) * 1000,  # UI 显示秒
            'history_include_dialogue': bool(include_dialogue),
            'history_dialogue_msgs': int(dialogue_msgs),
            'history_dialogue_total_chars': int(dialogue_chars),
        })
        # 网关（另一个进程）靠 config.json 的 mtime 变化自动重载，正常情况下改完即生效
        hot = bool(getattr(core, '_ENABLE_CONFIG_HOT_RELOAD', True))
        tail = '，网关会自动重新加载（无需重启）' if hot else '；⚠ 热重载已关闭，需重启网关才生效'
        return f"✅ 已保存（{len(norm)} 个工具）到 {core.CONFIG_PATH}{tail}"
    except Exception as e:  # noqa: BLE001
        return f"❌ 保存失败: {e}"


def health_check():
    cfg = core.get_config()
    try:
        import requests
        r = requests.get(f"{cfg['upstream_base'].rstrip('/')}/../health".replace('/v1/../', '/'),
                         timeout=5)
        return f"上游健康: HTTP {r.status_code} {r.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return f"上游不可达: {e}"


def build_ui():
    with gr.Blocks(title='ds-trim-gateway 配置') as demo:
        gr.Markdown("""
        # ds-trim-gateway
        **DeepSeek 网页端精简代理** — DSH → 本网关(8001) → AFS(8080) → DeepSeek 网页端。
        只把「最近几轮对话历史 + 工具结果 + 当前提问 + 精简 system + 精简工具列表」
        转发给上游，避免超长 system 触发禁言。
        """)

        with gr.Tab('配置'):
            upstream_base = gr.Textbox(label='上游 AFS 地址', value='http://127.0.0.1:8080/v1')
            upstream_key = gr.Textbox(label='上游 API Key（可空）', type='password')
            upstream_model = gr.Textbox(label='上游模型', value='deepseek-v4-pro')
            with gr.Row():
                preset_dd = gr.Dropdown(
                    label='客户端工具预设（选你正在用的客户端）',
                    choices=list(core.TOOL_PRESETS.keys()),
                    value=core.DEFAULT_PRESET_NAME,
                    info=('WorkBuddy 与 DSH 暴露的工具名/参数面不同，选错会满屏"工具调用失败"：'
                          'WorkBuddy 是 PowerShell / WebSearch(query)，'
                          'DSH 是 pwsh / web_search(queries)。两个预设都只针对 Windows。 '
                          f'当前配置识别为：{core.detect_preset_name(core.get_config().get("tools")) or "未识别"}'),
                    scale=4)
                preset_btn = gr.Button('应用预设', scale=1)
            sys_prompt = gr.Textbox(label='精简 System 提示词', lines=5)
            tools_json = gr.Code(label='精简工具列表 (JSON 数组)', language='json',
                                 lines=18)
            with gr.Row():
                drop_sys = gr.Checkbox(label='丢弃客户端(DSH)的 system，只用上面的精简 system', value=True)
                max_chars = gr.Slider(label='system 最大字符数', minimum=200, maximum=5000,
                                      step=100, value=1500)
                retry_ms = gr.Slider(label='空响应重试冷却(秒)', minimum=10, maximum=300,
                                     step=10, value=60)
                min_interval_s = gr.Slider(label='请求最小间隔(秒, 限流)', minimum=0, maximum=60,
                                           step=1, value=12)
            with gr.Row():
                include_dialogue = gr.Checkbox(
                    label='带上最近几轮对话历史（用户提问 + 助手回答）', value=True,
                    info=('关掉后只发最后一条提问：多轮追问里的"那深证成指呢？"、'
                          '"把刚才的结果整理成表格"会失去先行词，模型只能重新取数。'))
                dialogue_msgs = gr.Slider(label='对话历史条数(user+assistant 合计)', minimum=0,
                                          maximum=20, step=1, value=6)
                dialogue_chars = gr.Slider(label='对话历史字符预算', minimum=0, maximum=12000,
                                           step=500, value=3000)
            with gr.Row():
                load_btn = gr.Button('加载当前配置')
                save_btn = gr.Button('保存配置', variant='primary')
                health_btn = gr.Button('健康检查')
            save_msg = gr.Textbox(label='状态', interactive=False)

        with gr.Tab('日志'):
            logs_box = gr.Textbox(label='运行日志', lines=25, interactive=False)
            refresh_btn = gr.Button('刷新日志')

        load_btn.click(load_for_ui, outputs=[
            upstream_base, upstream_key, upstream_model, sys_prompt,
            tools_json, drop_sys, max_chars, retry_ms, min_interval_s, preset_dd,
            include_dialogue, dialogue_msgs, dialogue_chars,
        ])
        preset_btn.click(apply_preset, inputs=[preset_dd],
                         outputs=[sys_prompt, tools_json, save_msg])
        save_btn.click(save_from_ui, inputs=[
            upstream_base, upstream_key, upstream_model, sys_prompt,
            tools_json, drop_sys, max_chars, retry_ms, min_interval_s,
            include_dialogue, dialogue_msgs, dialogue_chars,
        ], outputs=save_msg)
        health_btn.click(health_check, outputs=save_msg)
        refresh_btn.click(lambda: '\n'.join(core.get_logs()), outputs=logs_box)

    return demo


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ds-trim-gateway UI')
    parser.add_argument('--port', type=int, default=7860)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()

    core.load_config()
    demo = build_ui()
    demo.launch(server_name=args.host, server_port=args.port)
