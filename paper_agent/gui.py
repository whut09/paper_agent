import cgi
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from asyncio import CancelledError
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import gradio as gr
import requests
from gradio_pdf import PDF
import logging

from paper_agent import __version__, sanitize_no_proxy_env
from paper_agent.config import ConfigManager
from paper_agent.harness.policy import DEFAULT_MAX_ASSETS
from paper_agent.harness.workflow import summarize_paper

logger = logging.getLogger(__name__)
sanitize_no_proxy_env()

# The following variable associate strings with page ranges
page_map = {
    "All": None,
    "First": [0],
    "First 5 pages": list(range(0, 5)),
    "Others": None,
}

# Check if this is a public demo, which has resource limits
flag_demo = False

# Limit resources
if ConfigManager.get("PAPER_AGENT_DEMO"):
    flag_demo = True
    page_map = {
        "First": [0],
        "First 20 pages": list(range(0, 20)),
    }
    client_key = ConfigManager.get("PAPER_AGENT_CLIENT_KEY")
    server_key = ConfigManager.get("PAPER_AGENT_SERVER_KEY")


# Configure about Gradio show keys
hidden_gradio_details: bool = bool(ConfigManager.get("HIDDEN_GRADIO_DETAILS"))


def get_config_or_env(key: str, default: str = "") -> str:
    value = ConfigManager.all().get(key) or os.environ.get(key)
    return str(value) if value else default


def get_config_bool_or_env(key: str, default: bool = False) -> bool:
    value = ConfigManager.all().get(key, os.environ.get(key))
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# Public demo control
def verify_recaptcha(response):
    """
    This function verifies the reCAPTCHA response.
    """
    recaptcha_url = "https://www.google.com/recaptcha/api/siteverify"
    data = {"secret": server_key, "response": response}
    result = requests.post(recaptcha_url, data=data).json()
    return result.get("success")


DOWNLOAD_CHUNK_SIZE = 64 * 1024
DOWNLOAD_RETRIES = 3
PARALLEL_DOWNLOAD_CHUNK_SIZE = 256 * 1024
PARALLEL_DOWNLOAD_WORKERS = 4
DOWNLOAD_CONNECT_TIMEOUT = 25
DOWNLOAD_READ_TIMEOUT = 120
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 PaperAgent/0.1 (+https://github.com/whut09/paper_agent)",
    "Accept": "application/pdf,application/octet-stream,*/*",
}
DownloadProgressCallback = Callable[[int, int | None], None]


def download_with_limit(
    url: str,
    save_path: Path,
    size_limit: int | None,
    progress_callback: DownloadProgressCallback | None = None,
) -> str:
    """
    This function downloads a file from a URL and saves it to a specified path.

    Inputs:
        - url: The URL to download the file from
        - save_path: The path to save the file to
        - size_limit: The maximum size of the file to download

    Returns:
        - The path of the downloaded file
    """
    url = _normalize_paper_url(url)
    local_result = _try_local_pdf_download(url, save_path, size_limit)
    if local_result:
        return local_result

    download_proxy = _download_proxy_config()
    curl_result = _try_curl_download(
        url, save_path, size_limit, download_proxy, progress_callback
    )
    if curl_result:
        return curl_result

    trust_env = _download_should_trust_env(url)
    parallel_result = _try_parallel_range_download(
        url,
        save_path,
        size_limit,
        trust_env,
        download_proxy,
        progress_callback,
    )
    if parallel_result:
        return parallel_result

    target: Path | None = None
    temp_target: Path | None = None
    last_error: requests.exceptions.RequestException | None = None
    timeout = _download_timeout()
    try:
        for mode, proxy, candidate_trust_env in _request_download_candidates(
            url, trust_env, download_proxy
        ):
            session = requests.Session()
            session.trust_env = candidate_trust_env
            if proxy:
                _set_session_proxy(session, proxy)

            for attempt in range(DOWNLOAD_RETRIES):
                resume_from = (
                    temp_target.stat().st_size
                    if temp_target and temp_target.exists()
                    else 0
                )
                headers = dict(DOWNLOAD_HEADERS)
                if resume_from:
                    headers["Range"] = f"bytes={resume_from}-"
                try:
                    logger.info("Downloading paper with requests (%s).", mode)
                    with session.get(
                        url, stream=True, timeout=timeout, headers=headers
                    ) as response:
                        response.raise_for_status()
                        if target is None:
                            target = save_path / _download_filename(url, response)
                            temp_target = target.with_name(f"{target.name}.part")
                            resume_from = (
                                temp_target.stat().st_size
                                if temp_target.exists()
                                else 0
                            )
                        if resume_from and response.status_code != 206:
                            temp_target.unlink(missing_ok=True)
                            resume_from = 0
                        expected_size = _download_expected_size(response, resume_from)
                        if size_limit and expected_size and expected_size > size_limit:
                            temp_target.unlink(missing_ok=True)
                            raise gr.Error("文件超过大小限制，请下载后使用文件上传。")
                        total_size = resume_from
                        if progress_callback:
                            progress_callback(total_size, expected_size)
                        mode_flag = "ab" if resume_from else "wb"
                        with open(temp_target, mode_flag) as file:
                            for chunk in response.iter_content(
                                chunk_size=DOWNLOAD_CHUNK_SIZE
                            ):
                                if not chunk:
                                    continue
                                total_size += len(chunk)
                                if size_limit and total_size > size_limit:
                                    temp_target.unlink(missing_ok=True)
                                    raise gr.Error(
                                        "文件超过大小限制，请下载后使用文件上传。"
                                    )
                                file.write(chunk)
                                if progress_callback:
                                    progress_callback(total_size, expected_size)
                        if expected_size and total_size < expected_size:
                            raise requests.exceptions.ChunkedEncodingError(
                                f"incomplete download: {total_size} bytes read, {expected_size} expected"
                            )
                        temp_target.replace(target)
                        return str(target)
                except requests.exceptions.RequestException as exc:
                    last_error = exc
                    logger.debug(
                        "requests download failed in %s mode, attempt %s/%s: %s",
                        mode,
                        attempt + 1,
                        DOWNLOAD_RETRIES,
                        exc,
                    )
                    if attempt + 1 >= DOWNLOAD_RETRIES:
                        break
                    time.sleep(0.8 * (attempt + 1))
            if temp_target and temp_target.exists():
                temp_target.unlink(missing_ok=True)
        raise last_error or requests.exceptions.ConnectionError("download failed")
    except gr.Error:
        raise
    except requests.exceptions.RequestException as exc:
        proxy_hint = ""
        if download_proxy:
            proxy_hint = (
                "当前论文下载请求已配置 PAPER_AGENT_DOWNLOAD_PROXY/PAPER_AGENT_DOWNLOAD_PROXIES，"
                "程序已自动展开并尝试 http、https、socks5h、socks5 等代理协议；"
                "如果代理链路不稳定，请检查代理地址，或先在浏览器下载 PDF 后使用文件上传。"
            )
        elif trust_env and (
            os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        ):
            proxy_hint = (
                "当前检测到 HTTP_PROXY/HTTPS_PROXY，下载请求会尝试环境代理；"
                '如果代理不稳定，可以在 config.local.json 中设置 "PAPER_AGENT_DOWNLOAD_NO_PROXY": true 后重启，'
                "或改用文件上传。"
            )
        retry_hint = (
            "已自动重试并切换下载路径但仍失败；可以再次点击生成，或先在浏览器下载 PDF 后使用文件上传。"
            "如需等待更慢的站点，可提高 PAPER_AGENT_DOWNLOAD_CONNECT_TIMEOUT 或 "
            "PAPER_AGENT_DOWNLOAD_READ_TIMEOUT。"
        )
        if _looks_like_dns_failure(exc):
            retry_hint += (
                "当前错误是系统 DNS 解析失败；浏览器能打开通常是因为浏览器插件、DoH 或代理软件接管了 DNS，"
                "但 Python/curl 不会自动继承这条链路。程序已优先搜索 paper_agent_files、Downloads 和 Desktop "
                "里的同名或近似同名 PDF；如果浏览器能下载，把 PDF 放在这些目录后再次点击生成即可自动复用。"
            )
        raise gr.Error(f"论文链接下载失败：{exc}。{retry_hint}{proxy_hint}") from exc


def _try_local_pdf_download(url: str, save_path: Path, size_limit: int | None) -> str | None:
    filename = _download_filename_from_url(url)
    candidate = _find_local_pdf_candidate(filename, save_path)
    if candidate is None:
        return None
    if size_limit and candidate.stat().st_size > size_limit:
        raise gr.Error("文件超过大小限制，请下载后使用文件上传。")
    save_path.mkdir(parents=True, exist_ok=True)
    target = save_path / filename
    try:
        if candidate.resolve() == target.resolve():
            logger.info("Using existing local PDF for download URL: %s", candidate)
            return str(candidate)
    except OSError:
        pass
    shutil.copy2(candidate, target)
    logger.info("Reusing local PDF %s for download URL.", candidate)
    return str(target)


def _find_local_pdf_candidate(filename: str, save_path: Path) -> Path | None:
    search_dirs = _local_pdf_search_dirs(save_path)
    for directory in search_dirs:
        candidate = directory / filename
        if _usable_local_pdf(candidate):
            return candidate
    for directory in search_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        try:
            matches = sorted(directory.glob(filename))
        except OSError:
            continue
        for candidate in matches:
            if _usable_local_pdf(candidate):
                return candidate
    expected_tokens = _filename_match_tokens(filename)
    if not expected_tokens:
        return None
    for directory in search_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        try:
            candidates = sorted(directory.glob("*.pdf"), key=lambda item: item.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for candidate in candidates[:80]:
            if not _usable_local_pdf(candidate):
                continue
            candidate_tokens = _filename_match_tokens(candidate.name)
            if expected_tokens.issubset(candidate_tokens):
                return candidate
    return None


def _local_pdf_search_dirs(save_path: Path) -> list[Path]:
    dirs = [
        save_path,
        Path.cwd() / "paper_agent_files",
        Path.home() / "Downloads",
        Path.home() / "Desktop",
    ]
    result: list[Path] = []
    seen: set[str] = set()
    for directory in dirs:
        try:
            key = str(directory.resolve()).lower()
        except OSError:
            key = str(directory).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(directory)
    return result


def _usable_local_pdf(path: Path) -> bool:
    try:
        return path.is_file() and path.suffix.lower() == ".pdf" and path.stat().st_size > 0
    except OSError:
        return False


def _filename_match_tokens(filename: str) -> set[str]:
    stem = Path(filename).stem.lower()
    stem = re.sub(r"\(\d+\)$", "", stem).strip()
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", stem)
        if len(token) >= 4 and token not in {"paper", "cvpr", "2026", "content"}
    }
    return tokens


def _looks_like_dns_failure(exc: BaseException) -> bool:
    text = repr(exc).lower()
    return any(
        marker in text
        for marker in (
            "nameresolutionerror",
            "failed to resolve",
            "getaddrinfo failed",
            "could not resolve host",
            "temporary failure in name resolution",
        )
    )


def _download_timeout() -> tuple[float, float]:
    return (
        _get_config_float(
            "PAPER_AGENT_DOWNLOAD_CONNECT_TIMEOUT", DOWNLOAD_CONNECT_TIMEOUT
        ),
        _get_config_float("PAPER_AGENT_DOWNLOAD_READ_TIMEOUT", DOWNLOAD_READ_TIMEOUT),
    )


def _get_config_float(key: str, default: float) -> float:
    value = get_config_or_env(key)
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _download_proxy_config() -> str:
    values = [
        get_config_or_env("PAPER_AGENT_DOWNLOAD_PROXIES"),
        get_config_or_env("PAPER_AGENT_DOWNLOAD_PROXY"),
    ]
    return " ".join(value for value in values if value)


def _request_download_candidates(
    url: str, default_trust_env: bool, configured_proxy: str
) -> list[tuple[str, str | None, bool]]:
    if get_config_bool_or_env("PAPER_AGENT_DOWNLOAD_NO_PROXY"):
        return [("noproxy", None, False)]

    candidates: list[tuple[str, str | None, bool]] = []
    seen: set[tuple[str, str]] = set()

    def add(mode: str, proxy: str | None, trust_env: bool) -> None:
        key = ("env" if trust_env else proxy or "", mode)
        if key in seen:
            return
        seen.add(key)
        candidates.append((mode, proxy, trust_env))

    for mode, proxy in _configured_proxy_candidates(configured_proxy):
        add(mode, proxy, False)
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        value = os.environ.get(name)
        for mode, proxy in _configured_proxy_candidates(value or "", f"env-{name}"):
            add(mode, proxy, False)
    system_proxy = _windows_system_proxy()
    for mode, proxy in _configured_proxy_candidates(system_proxy, "windows-system-proxy"):
        add(mode, proxy, False)
    add("requests-default", None, default_trust_env)
    if default_trust_env:
        add("noproxy", None, False)
    return candidates


def _set_session_proxy(session: requests.Session, proxy: str) -> None:
    try:
        session.proxies.update({"http": proxy, "https": proxy})
    except AttributeError:
        logger.debug("Session object has no proxies attribute; skipping proxy setup.")


def _configured_proxy_candidates(raw_value: str, label: str = "configured-proxy") -> list[tuple[str, str]]:
    proxies = _split_proxy_config(raw_value)
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, proxy in enumerate(proxies, 1):
        for variant_label, variant in _proxy_protocol_variants(proxy):
            normalized = variant.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            suffix = f"-{index}" if len(proxies) > 1 else ""
            candidates.append((f"{label}{suffix}-{variant_label}", variant))
    return candidates


def _split_proxy_config(raw_value: str) -> list[str]:
    result: list[str] = []
    for item in re.split(r"[\s,;]+", raw_value or ""):
        proxy = item.strip()
        if proxy:
            result.append(proxy)
    return result


def _proxy_protocol_variants(proxy: str) -> list[tuple[str, str]]:
    proxy = proxy.strip()
    if not proxy:
        return []
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    if not parsed.netloc:
        return [("as-is", proxy)]
    original = proxy if "://" in proxy else f"http://{proxy}"
    variants = [("as-is", original)]
    if get_config_bool_or_env("PAPER_AGENT_DOWNLOAD_EXPAND_PROXY_VARIANTS", True):
        for scheme in ("http", "https", "socks5h", "socks5"):
            variants.append((scheme, parsed._replace(scheme=scheme).geturl()))
    return variants


def _try_curl_download(
    url: str,
    save_path: Path,
    size_limit: int | None,
    download_proxy: str,
    progress_callback: DownloadProgressCallback | None,
) -> str | None:
    if get_config_bool_or_env("PAPER_AGENT_DISABLE_CURL_DOWNLOAD"):
        return None
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return None

    filename = _download_filename_from_url(url)
    target = save_path / filename
    temp_target = target.with_name(f"{target.name}.part")
    temp_target.unlink(missing_ok=True)

    command = [
        curl,
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--http1.1",
        "--speed-limit",
        get_config_or_env("PAPER_AGENT_CURL_SPEED_LIMIT", "32768"),
        "--speed-time",
        get_config_or_env("PAPER_AGENT_CURL_SPEED_TIME", "15"),
        "--connect-timeout",
        get_config_or_env("PAPER_AGENT_CURL_CONNECT_TIMEOUT", "20"),
        "--max-time",
        get_config_or_env("PAPER_AGENT_CURL_MAX_TIME", "180"),
        "-A",
        DOWNLOAD_HEADERS["User-Agent"],
        "-o",
        str(temp_target),
        url,
    ]

    for mode, proxy in _curl_proxy_candidates(download_proxy):
        temp_target.unlink(missing_ok=True)
        attempt_command = list(command)
        if proxy:
            attempt_command[1:1] = ["--proxy", proxy]
        elif mode == "noproxy":
            attempt_command[1:1] = ["--noproxy", "*"]

        try:
            logger.info("Downloading paper with curl (%s).", mode)
            process = subprocess.Popen(
                attempt_command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            while process.poll() is None:
                if progress_callback and temp_target.exists():
                    progress_callback(temp_target.stat().st_size, None)
                time.sleep(0.2)
            stderr = process.stderr.read() if process.stderr else ""
        except OSError as exc:
            logger.debug("curl download failed to start, falling back: %s", exc)
            temp_target.unlink(missing_ok=True)
            return None

        if process.returncode == 0:
            break
        logger.debug(
            "curl download failed with code %s in %s mode: %s",
            process.returncode,
            mode,
            stderr.strip(),
        )
    else:
        temp_target.unlink(missing_ok=True)
        return None

    if not temp_target.exists() or temp_target.stat().st_size <= 0:
        temp_target.unlink(missing_ok=True)
        return None
    if size_limit and temp_target.stat().st_size > size_limit:
        temp_target.unlink(missing_ok=True)
        raise gr.Error("文件超过大小限制，请下载后使用文件上传。")

    if progress_callback:
        progress_callback(temp_target.stat().st_size, temp_target.stat().st_size)
    temp_target.replace(target)
    return str(target)


def _curl_proxy_candidates(configured_proxy: str) -> list[tuple[str, str | None]]:
    if get_config_bool_or_env("PAPER_AGENT_DOWNLOAD_NO_PROXY"):
        return [("noproxy", None)]

    candidates: list[tuple[str, str | None]] = []
    seen_modes: set[str] = set()
    seen_proxies: set[str] = set()

    def add(mode: str, proxy: str | None) -> None:
        normalized_proxy = (proxy or "").strip().lower()
        if normalized_proxy:
            if normalized_proxy in seen_proxies:
                return
            seen_proxies.add(normalized_proxy)
        elif mode in seen_modes:
            return
        candidates.append((mode, proxy))
        seen_modes.add(mode)

    for mode, proxy in _configured_proxy_candidates(configured_proxy):
        add(mode, proxy)
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        value = os.environ.get(name)
        for mode, proxy in _configured_proxy_candidates(value or "", f"env-{name}"):
            add(mode, proxy)
    system_proxy = _windows_system_proxy()
    for mode, proxy in _configured_proxy_candidates(system_proxy, "windows-system-proxy"):
        add(mode, proxy)

    add("curl-default", None)
    return candidates


def _windows_system_proxy() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            proxy_enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not proxy_enabled:
                return ""
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return ""

    proxy = str(proxy_server or "").strip()
    if not proxy:
        return ""
    if ";" in proxy:
        entries = {}
        for item in proxy.split(";"):
            if "=" not in item:
                continue
            scheme, value = item.split("=", 1)
            entries[scheme.strip().lower()] = value.strip()
        proxy = (
            entries.get("https")
            or entries.get("http")
            or next(iter(entries.values()), "")
        )
    if proxy and "://" not in proxy:
        proxy = f"http://{proxy}"
    return proxy


def _try_parallel_range_download(
    url: str,
    save_path: Path,
    size_limit: int | None,
    trust_env: bool,
    download_proxy: str,
    progress_callback: DownloadProgressCallback | None,
) -> str | None:
    if get_config_bool_or_env("PAPER_AGENT_DISABLE_PARALLEL_DOWNLOAD"):
        return None

    head = None
    selected_mode = ""
    selected_proxy: str | None = None
    selected_trust_env = trust_env
    for mode, proxy, candidate_trust_env in _request_download_candidates(url, trust_env, download_proxy):
        head_session = requests.Session()
        head_session.trust_env = candidate_trust_env
        if proxy:
            _set_session_proxy(head_session, proxy)
        try:
            candidate_head = head_session.head(
                url,
                allow_redirects=True,
                timeout=(6, 20),
                headers=DOWNLOAD_HEADERS,
            )
            candidate_head.raise_for_status()
        except (AttributeError, requests.exceptions.RequestException) as exc:
            logger.debug("Parallel download HEAD failed in %s mode, falling back: %s", mode, exc)
            continue
        accept_ranges = (candidate_head.headers.get("Accept-Ranges") or "").lower()
        content_length = candidate_head.headers.get("Content-Length")
        if (
            "bytes" not in accept_ranges
            or not content_length
            or not content_length.isdigit()
        ):
            logger.debug("Parallel download skipped in %s mode: server did not advertise byte ranges.", mode)
            continue
        head = candidate_head
        selected_mode = mode
        selected_proxy = proxy
        selected_trust_env = candidate_trust_env
        break

    if head is None:
        return None

    content_length = head.headers.get("Content-Length") or "0"
    total_size = int(content_length)
    if total_size <= PARALLEL_DOWNLOAD_CHUNK_SIZE:
        return None
    if size_limit and total_size > size_limit:
        raise gr.Error("文件超过大小限制，请下载后使用文件上传。")

    target = save_path / _download_filename(url, head)
    temp_target = target.with_name(f"{target.name}.part")
    part_dir = target.with_name(f"{target.name}.parts")
    part_dir.mkdir(parents=True, exist_ok=True)

    ranges: list[tuple[int, int, Path]] = []
    part_size = (
        total_size + PARALLEL_DOWNLOAD_WORKERS - 1
    ) // PARALLEL_DOWNLOAD_WORKERS
    for index, start in enumerate(range(0, total_size, part_size)):
        end = min(start + part_size - 1, total_size - 1)
        ranges.append((start, end, part_dir / f"part-{index:02d}"))

    downloaded = 0
    lock = threading.Lock()

    def report(delta: int) -> None:
        nonlocal downloaded
        with lock:
            downloaded += delta
            current = downloaded
        if progress_callback:
            progress_callback(current, total_size)

    def download_range(start: int, end: int, part_path: Path) -> None:
        session = requests.Session()
        session.trust_env = selected_trust_env
        if selected_proxy:
            _set_session_proxy(session, selected_proxy)
        headers = dict(DOWNLOAD_HEADERS)
        headers["Range"] = f"bytes={start}-{end}"
        with session.get(
            url, stream=True, timeout=(8, 60), headers=headers
        ) as response:
            response.raise_for_status()
            if response.status_code != 206:
                raise requests.exceptions.RequestException(
                    f"range request returned {response.status_code}"
                )
            written = 0
            with open(part_path, "wb") as file:
                for chunk in response.iter_content(
                    chunk_size=PARALLEL_DOWNLOAD_CHUNK_SIZE
                ):
                    if not chunk:
                        continue
                    file.write(chunk)
                    written += len(chunk)
                    report(len(chunk))
            expected = end - start + 1
            if written != expected:
                raise requests.exceptions.ChunkedEncodingError(
                    f"incomplete range {start}-{end}: {written} bytes read, {expected} expected"
                )

    try:
        if progress_callback:
            progress_callback(0, total_size)
        logger.info("Downloading paper with parallel range requests (%s).", selected_mode)
        with ThreadPoolExecutor(
            max_workers=min(PARALLEL_DOWNLOAD_WORKERS, len(ranges))
        ) as executor:
            futures = [
                executor.submit(download_range, start, end, part_path)
                for start, end, part_path in ranges
            ]
            for future in as_completed(futures):
                future.result()

        with open(temp_target, "wb") as output:
            for _, _, part_path in ranges:
                with open(part_path, "rb") as part:
                    shutil.copyfileobj(part, output)
        if temp_target.stat().st_size != total_size:
            raise requests.exceptions.ChunkedEncodingError(
                f"incomplete merged download: {temp_target.stat().st_size} bytes read, {total_size} expected"
            )
        temp_target.replace(target)
        shutil.rmtree(part_dir, ignore_errors=True)
        return str(target)
    except (OSError, requests.exceptions.RequestException) as exc:
        logger.debug("Parallel download failed, falling back: %s", exc)
        temp_target.unlink(missing_ok=True)
        shutil.rmtree(part_dir, ignore_errors=True)
        return None


def _download_should_trust_env(url: str) -> bool:
    if get_config_bool_or_env("PAPER_AGENT_DOWNLOAD_NO_PROXY"):
        return False
    if get_config_bool_or_env("PAPER_AGENT_DOWNLOAD_USE_ENV_PROXY"):
        return True
    if _download_proxy_config():
        return False
    host = urlparse(url).netloc.lower()
    if host.endswith("arxiv.org"):
        return False
    return True


def _normalize_paper_url(url: str) -> str:
    normalized = url.strip()
    parsed = urlparse(normalized)
    if parsed.netloc.lower().endswith("arxiv.org") and parsed.path.startswith("/abs/"):
        paper_id = parsed.path.removeprefix("/abs/").strip("/")
        if paper_id:
            return f"https://arxiv.org/pdf/{paper_id}"
    return normalized


def _download_filename_from_url(url: str) -> str:
    filename = os.path.basename(unquote(urlparse(url).path)).strip() or "paper"
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return filename


def _download_filename(url: str, response: requests.Response) -> str:
    content = response.headers.get("Content-Disposition")
    try:
        _, params = cgi.parse_header(content)
        filename = params["filename"]
    except Exception:
        filename = Path(unquote(urlparse(url).path)).name or "paper"
    return os.path.splitext(os.path.basename(filename))[0] + ".pdf"


def _download_expected_size(
    response: requests.Response, resume_from: int
) -> int | None:
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isdigit():
        length = int(content_length)
        return resume_from + length if response.status_code == 206 else length
    return None


def _cancel_active_summary_sessions(exclude_session_id=None) -> int:
    with cancellation_event_lock:
        active = [
            (session_id, event)
            for session_id, event in cancellation_event_map.items()
            if session_id != exclude_session_id and not event.is_set()
        ]
    for session_id, event in active:
        logger.info("Stopping summary for session %s", session_id)
        event.set()
    return len(active)


def _start_summary_session(state: dict) -> tuple[uuid.UUID, threading.Event]:
    if not flag_demo:
        _cancel_active_summary_sessions()
    session_id = uuid.uuid4()
    cancellation_event = threading.Event()
    with cancellation_event_lock:
        cancellation_event_map[session_id] = cancellation_event
    state["session_id"] = session_id
    return session_id, cancellation_event


def stop_summary_file(state: dict) -> None:
    """
    This function stops the summary process.

    Inputs:
        - state: The state of the summary process

    Returns:- None
    """
    session_id = state.get("session_id")
    if session_id is None:
        if not flag_demo:
            _cancel_active_summary_sessions()
        return
    _cancel_active_summary_sessions()
    state["session_id"] = None


def summarize_file(
    file_type,
    file_input,
    link_input,
    page_range,
    page_input,
    max_assets,
    recaptcha_response,
    state,
    progress=gr.Progress(),
):
    session_id, cancellation_event = _start_summary_session(state)
    try:
        if flag_demo and not verify_recaptcha(recaptcha_response):
            raise gr.Error("reCAPTCHA fail")

        progress(0, desc="Preparing paper...")
        output = Path("paper_agent_files")
        output.mkdir(parents=True, exist_ok=True)

        if file_type == "File":
            if not file_input:
                raise gr.Error("No input")
            progress(0.03, desc="Preparing uploaded paper...")
            file_path = shutil.copy(file_input, output)
        else:
            if not link_input:
                raise gr.Error("No input")
            progress(0.01, desc="Downloading paper...")

            def download_progress(downloaded: int, total: int | None) -> None:
                if cancellation_event.is_set():
                    raise CancelledError("task cancelled")
                if total:
                    ratio = min(downloaded / max(total, 1), 1.0)
                    downloaded_mb = downloaded / 1024 / 1024
                    total_mb = total / 1024 / 1024
                    progress(
                        0.01 + ratio * 0.09,
                        desc=f"Downloading paper... {downloaded_mb:.1f}/{total_mb:.1f} MB",
                    )
                else:
                    downloaded_mb = downloaded / 1024 / 1024
                    progress(0.03, desc=f"Downloading paper... {downloaded_mb:.1f} MB")

            file_path = download_with_limit(
                link_input,
                output,
                5 * 1024 * 1024 if flag_demo else None,
                progress_callback=download_progress,
            )
            progress(0.1, desc="Download complete. Parsing paper...")

        if cancellation_event.is_set():
            raise CancelledError("task cancelled")

        if page_range != "Others":
            selected_page = page_map[page_range]
        else:
            selected_page = []
            for page in page_input.split(","):
                page = page.strip()
                if not page:
                    continue
                if "-" in page:
                    start, end = page.split("-")
                    selected_page.extend(range(int(start) - 1, int(end)))
                else:
                    selected_page.append(int(page) - 1)

        try:
            max_assets_value = int(max_assets)
        except (TypeError, ValueError):
            max_assets_value = DEFAULT_MAX_ASSETS

        def progress_bar(value: float, desc: str):
            progress(value, desc=desc)

        docx_path = summarize_paper(
            file_path,
            output,
            pages=selected_page,
            summary_language="中文",
            codex_envs={
                "CODEX_BASE_URL": get_config_or_env("CODEX_BASE_URL"),
                "CODEX_API_KEY": get_config_or_env("CODEX_API_KEY"),
                "CODEX_MODEL": get_config_or_env("CODEX_MODEL"),
                "CODEX_USE_PROXY": get_config_or_env("CODEX_USE_PROXY"),
                "CODEX_PROXY": get_config_or_env("CODEX_PROXY"),
            },
            max_assets=max_assets_value,
            progress=progress_bar,
            cancellation_event=cancellation_event,
        )
    except CancelledError:
        raise gr.Error("Summary cancelled")
    except RuntimeError as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        with cancellation_event_lock:
            cancellation_event_map.pop(session_id, None)
        if state.get("session_id") == session_id:
            state["session_id"] = None

    preview_path = str(file_path) if str(file_path).lower().endswith(".pdf") else None
    return (
        str(docx_path),
        gr.update(value=preview_path, visible=bool(preview_path)),
        gr.update(visible=True),
        gr.update(visible=True),
        gr.update(visible=not bool(preview_path)),
    )


# Global setup
custom_blue = gr.themes.Color(
    c50="#E8F3FF",
    c100="#BEDAFF",
    c200="#94BFFF",
    c300="#6AA1FF",
    c400="#4080FF",
    c500="#165DFF",  # Primary color
    c600="#0E42D2",
    c700="#0A2BA6",
    c800="#061D79",
    c900="#03114D",
    c950="#020B33",
)

custom_css = """
    .secondary-text {color: #999 !important;}
    footer {visibility: hidden}
    .env-warning {color: #dd5500 !important;}
    .env-success {color: #559900 !important;}

    /* Add dashed border to input-file class */
    .input-file {
        border: 1.2px dashed #165DFF !important;
        border-radius: 6px !important;
    }

    .progress-bar-wrap {
        border-radius: 8px !important;
    }

    .progress-bar {
        border-radius: 8px !important;
    }

    .pdf-canvas canvas {
        width: 100%;
    }

    """

demo_recaptcha = """
    <script src="https://www.google.com/recaptcha/api.js?render=explicit" async defer></script>
    <script type="text/javascript">
        var onVerify = function(token) {
            el=document.getElementById('verify').getElementsByTagName('textarea')[0];
            el.value=token;
            el.dispatchEvent(new Event('input'));
        };
    </script>
    """

tech_details_string = f"""
                    <summary>技术细节</summary>
                    - GUI: 论文总结助手<br>
                    - 版本: {__version__}
                """
cancellation_event_map = {}
cancellation_event_lock = threading.RLock()


# The following code creates the GUI
with gr.Blocks(
    title="论文总结助手",
    theme=gr.themes.Default(
        primary_hue=custom_blue, spacing_size="md", radius_size="lg"
    ),
    css=custom_css,
    head=demo_recaptcha if flag_demo else "",
) as demo:
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("## 文件 | < 5 MB" if flag_demo else "## 文件")
            file_type = gr.Radio(
                choices=[("文件", "File"), ("链接", "Link")],
                label="类型",
                value="File",
            )
            file_input = gr.File(
                label="文件",
                file_count="single",
                file_types=[".pdf", ".doc", ".docx"],
                type="filepath",
                elem_classes=["input-file"],
            )
            link_input = gr.Textbox(
                label="链接",
                visible=False,
                interactive=True,
            )
            gr.Markdown("## 总结选项")
            page_range = gr.Radio(
                choices=[
                    ("全部", "All"),
                    ("第一页", "First"),
                    ("前5页", "First 5 pages"),
                    ("其他", "Others"),
                ],
                label="页面",
                value="All",
            )

            page_input = gr.Textbox(
                label="页面范围",
                visible=False,
                interactive=True,
            )

            max_assets = gr.Number(
                label="最多写入图表截图数",
                value=DEFAULT_MAX_ASSETS,
                precision=0,
                interactive=True,
            )

            def on_select_filetype(file_type):
                return (
                    gr.update(visible=file_type == "File"),
                    gr.update(visible=file_type == "Link"),
                    gr.update(value=None, visible=file_type == "File"),
                    gr.update(visible=file_type == "Link"),
                )

            def on_select_page(choice):
                if choice == "Others":
                    return gr.update(visible=True)
                else:
                    return gr.update(visible=False)

            output_title = gr.Markdown("## 已生成论文总结", visible=False)
            output_file_mono = gr.File(label="下载 Word 总结文档", visible=False)
            recaptcha_response = gr.Textbox(
                label="reCAPTCHA响应", elem_id="verify", visible=False
            )
            recaptcha_box = gr.HTML('<div id="recaptcha-box"></div>')
            with gr.Row():
                summary_btn = gr.Button("生成论文总结", variant="primary")
                cancellation_btn = gr.Button("取消", variant="secondary")
            gr.Markdown("""
### ⚠️ 使用说明：
- 总结会调用 config.json 中的 CODEX_BASE_URL、CODEX_API_KEY、CODEX_MODEL
- 程序会从 PDF 中抽取正文，并将识别到的图、表和关键公式截图直接写入 Word 文档
- 如果浏览器崩溃或页面刷新，再次点击生成会自动取消遗留任务并启动新任务
- 如遇到 API 频率限制，系统会自动重试（最多5次，间隔递增）
- 总结完成后可下载 docx 文档
""")
            page_range.select(on_select_page, page_range, page_input)

        with gr.Column(scale=2):
            gr.Markdown("## 预览")
            preview = PDF(label="文档预览", visible=True, height=2000)
            preview_hint = gr.Markdown(
                "链接模式会在生成时先下载论文，生成完成后再显示 PDF 预览。",
                visible=False,
            )

    file_type.select(
        on_select_filetype,
        file_type,
        [file_input, link_input, preview, preview_hint],
        js=(
            f"""
            (a,b)=>{{
                try{{
                    grecaptcha.render('recaptcha-box',{{
                        'sitekey':'{client_key}',
                        'callback':'onVerify'
                    }});
                }}catch(error){{}}
                return [a];
            }}
            """
            if flag_demo
            else ""
        ),
    )

    # Event handlers
    file_input.upload(
        lambda x: (gr.update(value=x, visible=True), gr.update(visible=False)),
        inputs=file_input,
        outputs=[preview, preview_hint],
        js=(
            f"""
            (a,b)=>{{
                try{{
                    grecaptcha.render('recaptcha-box',{{
                        'sitekey':'{client_key}',
                        'callback':'onVerify'
                    }});
                }}catch(error){{}}
                return [a];
            }}
            """
            if flag_demo
            else ""
        ),
    )

    state = gr.State({"session_id": None})

    summary_event = summary_btn.click(
        summarize_file,
        inputs=[
            file_type,
            file_input,
            link_input,
            page_range,
            page_input,
            max_assets,
            recaptcha_response,
            state,
        ],
        outputs=[
            output_file_mono,
            preview,
            output_file_mono,
            output_title,
            preview_hint,
        ],
        concurrency_limit=2,
    )
    summary_event.then(lambda: None, js="()=>{grecaptcha.reset()}" if flag_demo else "")

    cancellation_btn.click(
        stop_summary_file,
        inputs=[state],
        queue=False,
        cancels=[summary_event],
    )


def parse_user_passwd(file_path: str) -> tuple:
    """
    Parse the user name and password from the file.

    Inputs:
        - file_path: The file path to read.
    Outputs:
        - tuple_list: The list of tuples of user name and password.
        - content: The content of the file
    """
    tuple_list = []
    content = ""
    if not file_path:
        return tuple_list, content
    if len(file_path) == 2:
        try:
            with open(file_path[1], "r", encoding="utf-8") as file:
                content = file.read()
        except FileNotFoundError:
            print(f"Error: File '{file_path[1]}' not found.")
    try:
        with open(file_path[0], "r", encoding="utf-8") as file:
            tuple_list = [
                tuple(line.strip().split(",")) for line in file if line.strip()
            ]
    except FileNotFoundError:
        print(f"Error: File '{file_path[0]}' not found.")
    return tuple_list, content


def _has_ipv6() -> bool:
    """Check whether the system can bind an IPv6 socket."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.close()
        return True
    except OSError:
        return False


def setup_gui(
    share: bool = False, auth_file: list = ["", ""], server_port=7860
) -> None:
    """
    Setup the GUI with the given parameters.

    Inputs:
        - share: Whether to share the GUI.
        - auth_file: The file path to read the user name and password.

    Outputs:
        - None
    """
    user_list, html = parse_user_passwd(auth_file)

    auth_kwargs = {}
    if len(user_list) > 0:
        auth_kwargs = {"auth": user_list, "auth_message": html}

    if flag_demo:
        demo.launch(server_name="0.0.0.0", max_file_size="5mb", inbrowser=True)
        return

    _ensure_local_proxy_bypass()

    # Try binding addresses in order: "0.0.0.0" for IPv4, fallback to loopback
    bind_addresses = ["0.0.0.0", "127.0.0.1"]

    for addr in bind_addresses:
        try:
            demo.launch(
                server_name=addr,
                debug=True,
                inbrowser=True,
                share=share,
                server_port=server_port,
                **auth_kwargs,
            )
            return
        except Exception:
            print(
                f"Error launching GUI using {addr}.\n"
                "This may be caused by global mode of proxy software."
            )

    if not share:
        raise RuntimeError(
            "Unable to launch local Gradio GUI. If proxy software is running in "
            "global mode, add 127.0.0.1, localhost, and 0.0.0.0 to the proxy "
            "bypass list, or run with a different --serverport."
        )

    # Last resort for explicit share mode only.
    demo.launch(
        debug=True,
        inbrowser=True,
        share=True,
        server_port=server_port,
        **auth_kwargs,
    )


def _ensure_local_proxy_bypass() -> None:
    local_hosts = [
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
    ]
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [part.strip() for part in existing.split(",") if part.strip()]
    seen = {part.lower() for part in parts}
    for host in local_hosts:
        if host.lower() not in seen:
            parts.append(host)
            seen.add(host.lower())
    value = ",".join(parts)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


# For auto-reloading while developing
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    setup_gui()
