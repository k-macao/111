"""YouTube 链接自动识别功能检查。

直接从 app/services/downloader.py 源码中提取 is_youtube 函数进行测试，
确保验证的是仓库中真实代码（is_youtube 仅依赖 re，无需完整服务栈）。
"""
import ast
import re
from pathlib import Path

SRC = Path(__file__).with_name("app") / "services" / "downloader.py"


def load_is_youtube():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "is_youtube":
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {"re": re}
            exec(compile(ast.fix_missing_locations(mod), str(SRC), "exec"), ns)  # noqa: S102
            return ns["is_youtube"]
    raise RuntimeError("未在 downloader.py 中找到 is_youtube 函数")


is_youtube = load_is_youtube()


# —— 与 app/api/routes.py:create_task 中自动判断来源一致的逻辑 ——
def auto_source(url: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return "youtube" if is_youtube(url) else "url"


youtube_cases = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc&t=30s",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?si=abc123",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/abc123XYZ",
    "https://www.youtube.com/live/abc123XYZ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "http://YOUTUBE.COM/watch?v=dQw4w9WgXcQ",  # 大小写
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=youtu.be",
    "https://youtube.com:443/watch?v=dQw4w9WgXcQ",  # 带端口
]

non_youtube_cases = [
    "https://vimeo.com/123456789",
    "https://www.bilibili.com/video/BV1xx411c7mD",
    "https://example.com/not-a-video.mp4",
    "https://www.youtubee.com/watch?v=123",   # 形似但非 youtube.com
    "https://youtu.be.evil.com/watch",        # youtu.be 作为子域名
    "https://www.dailymotion.com/video/x123",
    "https://www.youtube.com.br.watch?v=1",   # 真实域名 br.watch
    "https://youtubecom/watch?v=1",
    "https://evil.com/youtube.com/watch",     # 路径中含 youtube.com
    "https://notyoutube.com/watch?v=1",
]


def run():
    print("=" * 60)
    print("YouTube 链接自动识别 — 功能检查（读取真实源码）")
    print("=" * 60)

    passed = failed = 0

    print("\n[1] 应识别为 YouTube（期望 source=youtube）")
    for url in youtube_cases:
        got = is_youtube(url)
        src = auto_source(url)
        ok = got is True and src == "youtube"
        flag = "✅" if ok else "❌"
        passed += ok
        failed += (not ok)
        print(f"  {flag} {url}\n       -> is_youtube={got}, video_source={src}")

    print("\n[2] 不应识别为 YouTube（期望 source=url）")
    for url in non_youtube_cases:
        got = is_youtube(url)
        src = auto_source(url)
        ok = got is False and src == "url"
        flag = "✅" if ok else "❌"
        passed += ok
        failed += (not ok)
        print(f"  {flag} {url}\n       -> is_youtube={got}, video_source={src}")

    print("\n[3] 显式 videoSource 覆盖自动识别")
    explicit_cases = [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "url", "url"),
        ("https://vimeo.com/123456789", "youtube", "youtube"),
    ]
    for url, explicit, expect in explicit_cases:
        src = auto_source(url, explicit)
        ok = src == expect
        flag = "✅" if ok else "❌"
        passed += ok
        failed += (not ok)
        print(f"  {flag} url={url!r} + videoSource={explicit!r} -> {src}")

    print("\n" + "=" * 60)
    print(f"结果：通过 {passed} 项，失败 {failed} 项")
    print("=" * 60)
    print("功能正常 ✅" if failed == 0 else "存在失败用例 ❌")
    return failed


if __name__ == "__main__":
    raise SystemExit(run())
