"""读取 result.json，将转写文本通过 PushPlus 推送到微信。

只依赖 Python 标准库，不额外安装依赖。
必填环境变量：
  PUSHPLUS_TOKEN  PushPlus 的 token（见 workflow，从 secret 注入）

用法（与 workflow 一致）：
    python pushplus_push.py --result result.json --template txt [--topic xxx] [--title xxx]

仅推送纯文本内容，不带跳转链接或下载地址。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PUSHPLUS_URL = "https://www.pushplus.plus/send"
# txt 模板单条消息的字符上限，超长时截断并附提示，避免推送失败
TXT_MAX_CHARS = 4000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="读取 result.json 并通过 PushPlus 推送文本到微信")
    parser.add_argument("--result", default="result.json", help="转写结果 JSON 文件，默认 result.json")
    parser.add_argument("--template", choices=["txt", "html", "markdown", "json"], default="txt", help="推送模板，默认 txt")
    parser.add_argument("--topic", default="", help="PushPlus 群组编码 topic（可选，留空推送给自己）")
    parser.add_argument("--title", default="", help="推送标题（可选，留空使用视频标题）")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if not token:
        print("::error::未配置 secret PUSHPLUS_TOKEN，请在仓库 Settings → Secrets 中添加。", file=sys.stderr)
        return 1

    result_path = Path(args.result)
    if not result_path.exists():
        print(f"::error::未找到结果文件：{result_path}", file=sys.stderr)
        return 1

    result = json.loads(result_path.read_text(encoding="utf-8"))
    text = (result.get("text") or "").strip()
    if not text:
        print("::error::转写文本为空，跳过推送。", file=sys.stderr)
        return 1

    title = args.title or result.get("title") or "转写结果"
    content = text
    if args.template == "txt" and len(content) > TXT_MAX_CHARS:
        content = content[:TXT_MAX_CHARS] + "\n…（内容过长，已截断，完整文本见 Actions artifact）"

    data = {
        "token": token,
        "title": title,
        "content": content,
        "template": args.template,
    }
    if args.topic:
        data["topic"] = args.topic

    body = urlencode(data).encode("utf-8")
    request = Request(
        PUSHPLUS_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            resp_body = response.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"::error::推送请求失败：{exc}", file=sys.stderr)
        return 1

    try:
        parsed = json.loads(resp_body)
    except json.JSONDecodeError:
        parsed = {"code": None, "msg": resp_body}

    if parsed.get("code") == 200:
        print("已成功推送到微信。")
        return 0

    print(f"::error::推送失败：{parsed.get('msg') or resp_body}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
