"""在 GitHub Actions runner 上本地完成「下载 → 抽音 → 转写」的脚本。

仅依赖 workflow 已安装的依赖：yt-dlp、faster-whisper、ffmpeg。
无需自建服务、数据库或 Minio。

用法（与 .github/workflows/local_transcribe.yml 一致）：
    python local_transcribe.py "https://..." --model base --format txt \
        --output-dir transcript_output --result-json result.json [--lang zh] \
        [--player-client default] [--cookies cookies.txt] [--po-token TOKEN]

输出：
  - <output-dir>/<标题>.txt|.md  渲染后的转写文本文件
  - <result-json>                结构化结果，供 pushplus_push.py 读取后推送到微信
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional, Tuple
from urllib.request import Request, urlopen

import yt_dlp
from faster_whisper import WhisperModel

DEFAULT_MODEL = "base"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"

# 支持的模型与可选项，与 workflow 的 choice 保持一致
MODEL_CHOICES = ["tiny", "base", "small", "medium"]
PLAYER_CLIENT_CHOICES = ["default", "android", "ios", "tv", "web", "mweb"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地下载视频/音频并转写为文本")
    parser.add_argument("video_url", help="视频/音频链接（YouTube 或普通 http(s) 直链）")
    parser.add_argument("--model", choices=MODEL_CHOICES, default=DEFAULT_MODEL, help="Whisper 模型，默认 base")
    parser.add_argument("--format", choices=["txt", "markdown"], default="txt", help="输出格式，默认 txt")
    parser.add_argument("--output-dir", default="transcript_output", help="输出目录，默认 transcript_output")
    parser.add_argument("--result-json", default="result.json", help="结构化结果文件，默认 result.json")
    parser.add_argument("--lang", default="", help="语言提示，如 zh/en/ja；留空自动检测")
    parser.add_argument(
        "--player-client",
        choices=PLAYER_CLIENT_CHOICES,
        default="default",
        help="yt-dlp 使用的 YouTube player client，默认 default",
    )
    parser.add_argument(
        "--cookies",
        default="",
        help="yt-dlp cookies.txt 文件路径（可选）",
    )
    parser.add_argument(
        "--po-token",
        default="",
        help="YouTube PO token（可选，通常与 android 客户端配合使用）",
    )
    return parser.parse_args()


def _sanitize_filename(name: str) -> str:
    """简单清洗文件名，避免特殊字符导致路径问题。"""

    invalid = r'\\/:*?"<>|'
    cleaned = "".join("_" if ch in invalid else ch for ch in name)
    cleaned = cleaned.strip().replace(" ", "_")
    return cleaned or "transcript"


def _is_youtube(url: str) -> bool:
    """按域名精确判断是否为 YouTube 链接。"""

    host = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url)
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.split(":", 1)[0].lower().rstrip(".")
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host.endswith(".youtu.be")


def _proxy_url() -> Optional[str]:
    """从 YTDLP_PROXY 环境变量读取下载代理（可选）。"""

    proxy = (os.environ.get("YTDLP_PROXY") or "").strip()
    if not proxy:
        return None
    if not proxy.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return None
    return proxy


def _download_youtube(
    url: str,
    workdir: Path,
    proxy: Optional[str],
    player_client: str = "default",
    cookies: Optional[str] = None,
    po_token: Optional[str] = None,
) -> Tuple[Path, str]:
    """使用 yt-dlp 抽取最佳音频并转成 16k 单声道 wav。"""

    output_tpl = str(workdir / f"{uuid.uuid4().hex}.%(ext)s")

    # 与服务端 downloader 保持一致：android 没有 PO token 时回退到 default，
    # 避免 yt-dlp 因缺少 GVS PO token 直接失败。
    player_client = (player_client or "default").strip()
    po_token = (po_token or "").strip() or None
    if player_client.lower() == "android" and not po_token:
        player_client = "default"

    extractor_args = {"youtube": {"player_client": [player_client]}}
    if po_token:
        extractor_args["youtube"]["po_token"] = [po_token]

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_tpl,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "wav", "preferredquality": "0"}
        ],
        "postprocessor_args": ["-ac", "1", "-ar", "16000"],
        "noplaylist": True,
        "quiet": True,
        "retries": 3,
        "proxy": proxy,
        "extractor_args": extractor_args,
    }

    if cookies:
        cookies_path = Path(cookies).expanduser()
        if not cookies_path.is_file():
            raise RuntimeError(f"指定的 cookies 文件不存在：{cookies_path}")
        ydl_opts["cookiefile"] = str(cookies_path)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise RuntimeError(f"yt-dlp 下载失败：{exc}") from exc

    wav_files = list(workdir.glob("*.wav"))
    if not wav_files:
        raise RuntimeError("yt-dlp 未生成音频文件，请检查链接或代理")
    title = info.get("title") or wav_files[0].stem
    return wav_files[0], title


def _download_http(url: str, workdir: Path, proxy: Optional[str]) -> Tuple[Path, str]:
    """普通 HTTP/HTTPS 直链下载（走 urllib，可选代理）。"""

    local_path = workdir / f"{uuid.uuid4().hex}.media"
    headers = {"User-Agent": "Mozilla/5.0"}
    opener = None
    if proxy:
        from urllib.request import ProxyHandler, build_opener, install_opener

        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
        install_opener(opener)

    request = Request(url, headers=headers)
    response = urlopen(request, timeout=120)
    with response, open(local_path, "wb") as f:
        while True:
            chunk = response.read(8192)
            if not chunk:
                break
            f.write(chunk)

    title = Path(url.split("?")[0]).name or local_path.name
    return local_path, title


def _extract_audio_to_wav(input_path: Path, workdir: Path) -> Path:
    """用 ffmpeg 转成 16k 单声道 wav；已是 wav 则直接返回。"""

    if input_path.suffix.lower() == ".wav":
        return input_path
    output_path = workdir / f"{uuid.uuid4().hex}.wav"
    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-ar", "16000", "-ac", "1", str(output_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽取音频失败：{result.stderr.strip()[-500:]}")
    return output_path


def _transcribe(audio_path: Path, model_name: str, language: Optional[str]) -> Tuple[str, str]:
    """faster-whisper 转写，返回 (文本, 检测语言)。"""

    model = WhisperModel(model_name, device=DEFAULT_DEVICE, compute_type=DEFAULT_COMPUTE_TYPE)
    segments, info = model.transcribe(str(audio_path), language=language)
    text = "\n".join(seg.text.strip() for seg in segments).strip()
    return text, info.language


def _render(text: str, output_format: str) -> str:
    """根据目标格式渲染内容。"""

    if output_format == "markdown":
        return f"## 转写结果\n\n{text}"
    return text


def main() -> int:
    args = _parse_args()
    url = args.video_url
    language = args.lang.strip() or None
    output_format = args.format

    workdir = Path(args.output_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    # 下载用临时目录，避免污染最终输出目录
    tmpdir = workdir / f".tmp_{uuid.uuid4().hex}"
    tmpdir.mkdir(parents=True, exist_ok=True)

    try:
        proxy = _proxy_url()
        print(f"来源：{'YouTube' if _is_youtube(url) else '普通直链'}，模型：{args.model}，格式：{output_format}")

        if _is_youtube(url):
            media_path, title = _download_youtube(
                url,
                tmpdir,
                proxy,
                player_client=args.player_client,
                cookies=args.cookies,
                po_token=args.po_token,
            )
        else:
            media_path, title = _download_http(url, tmpdir, proxy)
        print(f"已下载媒体，标题：{title}")

        audio_path = _extract_audio_to_wav(media_path, tmpdir)
        print("音频已准备为 16k 单声道 wav，开始转写…")

        text, detected_lang = _transcribe(audio_path, args.model, language)
        if not text:
            raise RuntimeError("转写结果为空，请检查音频或模型")

        rendered = _render(text, output_format)
        base_name = _sanitize_filename(title)
        ext = "md" if output_format == "markdown" else "txt"
        filename = f"{base_name}.{ext}"
        out_file = workdir / filename
        out_file.write_text(rendered, encoding="utf-8")

        result = {
            "title": title,
            "text": text,
            "language": detected_lang,
            "format": output_format,
            "model": args.model,
            "video_url": url,
            "files": [
                {
                    "file_name": filename,
                    "format": output_format,
                    "path": str(out_file),
                }
            ],
        }
        Path(args.result_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"转写完成，检测语言：{detected_lang}")
        print(f"文本文件：{out_file}")
        print(f"结果 JSON：{args.result_json}")
        return 0
    finally:
        # 清理下载与中间音频
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 便于 Actions 日志定位问题
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
