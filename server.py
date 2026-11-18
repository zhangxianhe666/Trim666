"""
ds-trim-gateway — Flask 服务入口

提供：
- GET  /health              健康检查
- GET  /v1/models           模型列表（让 DSH 能发现）
- POST /v1/chat/completions OpenAI 兼容端点（精简转发 → XML 解析 → tool_calls）
- POST /v1/chat/completions 支持 stream=false 与 stream=true（SSE 包装）

启动：
    .venv/bin/python server.py            # 默认 127.0.0.1:8001
    .venv/bin/python server.py --port 8002
"""
import argparse
import os
import sys
import threading

from flask import Flask, jsonify, request

import gateway_core as core

app = Flask(__name__)

# 简单的并发节流（DeepSeek 网页端禁言敏感，避免并发触发风控）
_forward_lock = threading.Lock()


@app.route('/health', methods=['GET'])
def health():
    cfg = core.get_config()
    return jsonify({"status": "ok", "upstream": cfg.get('upstream_base')})


@app.route('/v1/models', methods=['GET'])
def models():
    cfg = core.get_config()
    return jsonify({
        "object": "list",
        "data": [{
            "id": cfg.get('upstream_model', 'deepseek-v4-pro'),
            "object": "model",
            "created": int(__import__('time').time()),
            "owned_by": "ds-trim-gateway",
        }],
    })


@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    body = request.get_json(silent=True) or {}
    stream = bool(body.get('stream', False))
    with _forward_lock:
        try:
            result = core.handle_chat_completion(body)
            if not stream:
                return jsonify(result)
            return _sse_response(result)
        except Exception as e:  # noqa: BLE001
            core.log(f"处理失败: {e}")
            return jsonify({
                "error": {
                    "message": str(e),
                    "type": "gateway_error",
                    "param": None,
                    "code": None,
                }
            }), 500


def _sse_response(result):
    """把非流式 handle 结果包装成 OpenAI 兼容的 SSE 流。

    客户端在 stream=true 时解析 SSE（data: {...} 行 + [DONE]），不能直接给整包
    JSON。这里把 message 拆成一个完整 chunk + finish chunk。
    """
    from flask import Response

    choice = (result.get('choices') or [{}])[0]
    msg = choice.get('message') or {}
    model = result.get('model', 'ds-trim-gateway')
    created = result.get('created', int(__import__('time').time()))
    rid = result.get('id', 'chatcmpl-local')

    def chunk(delta, finish_reason=None):
        payload = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }],
        }
        return f"data: {__import__('json').dumps(payload, ensure_ascii=False)}\n\n"

    lines = []
    # 角色 + content / tool_calls 主体 chunk
    if msg.get('tool_calls'):
        # OpenAI 流式 tool_calls 需要 role chunk + 分片 tool_calls
        lines.append(chunk({"role": "assistant"}))
        for tc in msg['tool_calls']:
            fn = tc.get('function') or {}
            # 首片带 name，后续片补 arguments（完整 JSON 一次性给也可以，但需分片）
            lines.append(chunk({
                "tool_calls": [{
                    "index": 0,
                    "id": tc.get('id'),
                    "type": "function",
                    "function": {"name": fn.get('name'), "arguments": ""},
                }],
            }))
            lines.append(chunk({
                "tool_calls": [{
                    "index": 0,
                    "function": {"arguments": fn.get('arguments', '')},
                }],
            }))
    else:
        content = msg.get('content') or ''
        if content:
            lines.append(chunk({"role": "assistant", "content": content}))
        else:
            lines.append(chunk({"role": "assistant", "content": ""}))

    finish = choice.get('finish_reason') or 'stop'
    lines.append(chunk({}, finish_reason=finish))
    lines.append("data: [DONE]\n\n")
    return Response(''.join(lines), content_type='text/event-stream; charset=utf-8')


@app.route('/v1/internal/config', methods=['GET'])
def get_config_api():
    return jsonify(core.get_config())


@app.route('/v1/internal/config', methods=['POST'])
def set_config_api():
    patch = request.get_json(silent=True) or {}
    core.update_config(patch)
    return jsonify({"success": True, "config": core.get_config()})


def _safe_streams():
    """把可能阻塞的 stdout/stderr 落到文件，避免启动横幅卡死整个服务。

    背景（实测事故）：本进程常被「后台 / 管道方式」拉起（IDE 终端面板、
    计划任务、start.bat 的重定向）。父进程一旦不读管道，print 会在缓冲区
    写满后**永久阻塞**；而 Werkzeug 恰好在 serve_forever() 之前往 stderr
    打 "Running on http://..."，于是卡在那里——socket 已经 listen，却永远
    不 accept。外在表现极具迷惑性：
      · 端口在监听，进程活着，CPU 几乎为 0
      · 客户端连得上、但收不到任何响应（连接最终停在 CLOSE_WAIT）
      · 日志只有启动那三行，之后一条都没有（因为 log() 也卡在 print）
    """
    try:
        path = os.path.join(core._TMP_DIR, 'ds-gw-stdout.log')
        out = open(path, 'a', encoding='utf-8', buffering=1)
    except Exception:  # noqa: BLE001
        return
    try:
        if not sys.stdout.isatty():
            sys.stdout = out
    except Exception:  # noqa: BLE001
        pass
    try:
        # 无论是否 tty，stderr 都落盘：Werkzeug 的横幅只走这里
        sys.stderr = out
    except Exception:  # noqa: BLE001
        pass


if __name__ == '__main__':
    _safe_streams()
    parser = argparse.ArgumentParser(description='ds-trim-gateway')
    parser.add_argument('--port', type=int, default=8001)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()

    core.log(f"ds-trim-gateway 启动: http://{args.host}:{args.port}")
    cfg = core.get_config()
    core.log(f"上游: {cfg['upstream_base']} model={cfg['upstream_model']}")
    core.log(f"精简工具数: {len(cfg.get('tools', []))}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
