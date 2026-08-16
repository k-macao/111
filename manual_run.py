"""命令行手动运行入口：输入视频链接并创建转写任务。

只依赖 Python 标准库，通过 HTTP 调用已启动的视频转写服务：
- 直接传参：python manual_run.py "https://www.youtube.com/watch?v=xxxx"
- 交互输入：python manual_run.py
- 创建任务后监听 SSE 进度，完成后输出转写文本与结果文件下载地址。

示例：
    python manual_run.py
    python manual_run.py "https://youtu.be/xxxx" --format markdown --lang zh
    python manual_run.py --url "https://example.com/a.mp3" --no-watch
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_OUTPUT_FORMAT = "txt"
DEFAULT_MODEL = "tiny"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="手动输入链接，创建视频/音频转写任务")
    parser.add_argument("positional_url", nargs="?", help="视频/音频链接；也可使用 --url")
    parser.add_argument("--url", "-u", help="视频/音频链接（优先级高于位置参数）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"服务地址，默认 {DEFAULT_BASE_URL}")
    parser.add_argument(
        "--user-id",
        default=str(uuid.uuid4()),
        help="用户 ID（UUID）。默认随机生成；同一用户建议复用该值。",
    )
    parser.add_argument(
        "--format",
        choices=["txt", "markdown"],
        default=DEFAULT_OUTPUT_FORMAT,
        help="输出格式",
    )
    parser.add_argument("--lang", help="语言提示，如 zh/en/ja；留空自动检测")
    parser.add_argument("--model", help=f"faster-whisper 模型，服务端默认：{DEFAULT_MODEL}")
    parser.add_argument("--device", help=f"推理设备 cpu/cuda，服务端默认：{DEFAULT_DEVICE}")
    parser.add_argument(
        "--compute-type",
        help=f"计算精度，服务端默认：{DEFAULT_COMPUTE_TYPE}",
    )
    parser.add_argument("--source", choices=["youtube", "url"], help="来源；不传时由后端自动识别")
    parser.add_argument("--no-watch", action="store_true", help="创建任务后不监听 SSE 进度")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP 请求超时秒数，默认 30")
    return parser.parse_args()


def _prompt_url() -> str:
    while True:
        url = input("请输入视频/音频链接（输入 q 退出）：").strip()
        if url.lower() in {"q", "quit", "exit"}:
            print("已取消。")
            sys.exit(0)
        if url:
            return url
        print("链接不能为空，请重新输入。")


def _build_payload(args: argparse.Namespace, video_url: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "videoUrl": video_url,
        "userId": args.user_id,
        "output_format": args.format,
    }
    if args.source:
        payload["videoSource"] = args.source
    if args.lang:
        payload["language"] = args.lang.strip()
    if args.model:
        payload["model"] = args.model
    if args.device:
        payload["device"] = args.device
    if args.compute_type:
        payload["compute_type"] = args.compute_type
    return payload


def _http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        detail: Any = error_body
        try:
            parsed = json.loads(error_body)
            detail = parsed.get("detail", error_body)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接服务：{exc.reason}") from exc

    if not body:
        return {}
    return json.loads(body)


def _iter_sse_lines(url: str, timeout: float) -> Iterator[str]:
    """逐行读取 SSE。连接超时使用 timeout，读取阶段保持长连接不超时。"""

    request = Request(url, headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"}, method="GET")
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接服务：{exc.reason}") from exc

    with response:
        while True:
            raw = response.readline()
            if not raw:
                break
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")


def _watch_sse(base_url: str, task_id: str, timeout: float) -> Dict[str, Any]:
    url = f"{base_url}/api/tasks/{task_id}/stream"
    print(f"监听进度：{url}")

    last_state: Tuple[str, float, str] = ("", -1.0, "")
    final_message: Dict[str, Any] = {}
    event_data = ""

    for line in _iter_sse_lines(url, timeout):
        if line.startswith("data:"):
            event_data = line[5:].strip()
            continue
        if line == "" and event_data:
            try:
                msg = json.loads(event_data)
            except json.JSONDecodeError:
                event_data = ""
                continue
            event_data = ""

            status = str(msg.get("status") or "")
            progress = float(msg.get("progress") or 0)
            message = str(msg.get("message") or "")
            current = (status, progress, message)
            if current != last_state:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] {status:<10} {progress:>5.1f}%  {message}")
                last_state = current

            if status in {"completed", "failed"}:
                final_message = msg
                break

    if not final_message:
        raise RuntimeError("进度监听已结束，但未收到最终状态")
    if final_message.get("status") == "failed":
        raise RuntimeError(final_message.get("message") or "任务失败")
    return final_message


def _show_result(base_url: str, task_id: str, timeout: float) -> None:
    try:
        transcript = _http_json("GET", f"{base_url}/api/tasks/{task_id}/transcript", timeout=timeout)
        print("\n===== 转写结果 =====")
        print(transcript.get("text") or "")
    except RuntimeError as exc:
        print(f"\n暂无转写文本（{exc}）")

    try:
        info = _http_json("GET", f"{base_url}/api/tasks/{task_id}/download", timeout=timeout)
        print("\n结果文件：")
        print(f"  文件名：{info.get('file_name')}")
        print(f"  下载/路径：{info.get('download_url')}")
    except RuntimeError as exc:
        print(f"\n暂无结果文件（{exc}）")


def _run(args: argparse.Namespace) -> int:
    video_url = args.url or args.positional_url or _prompt_url()
    payload = _build_payload(args, video_url)

    print("服务地址：", args.base_url)
    print("视频链接：", video_url)
    print("用户 ID：", payload["userId"])
    print("输出格式：", payload["output_format"])
    if payload.get("language"):
        print("语言提示：", payload["language"])

    task = _http_json("POST", f"{args.base_url}/api/tasks", payload=payload, timeout=args.timeout)
    task_id = task["task_id"]
    print(f"\n任务已创建：{task_id}")
    print(f"初始状态：{task.get('status')}，进度：{float(task.get('progress') or 0):.1f}%")

    if args.no_watch:
        print("已按 --no-watch 跳过进度监听。可手动查询：")
        print(f"  GET {args.base_url}/api/tasks/{task_id}")
        return 0

    _watch_sse(args.base_url, task_id, args.timeout)
    _show_result(args.base_url, task_id, args.timeout)
    return 0


def main() -> None:
    args = _parse_args()
    try:
        raise SystemExit(_run(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001 - 命令行入口展示可读错误
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
