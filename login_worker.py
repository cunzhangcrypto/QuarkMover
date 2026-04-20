#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QuarkLoginWorker - 后台线程托管一个 headless DrissionPage，负责：
  - 抓取夸克登录页二维码（canvas.toDataURL → base64 PNG）
  - 每 N 秒刷新一次 QR（避免过期）
  - 每 N 秒轮询登录状态（用当前 cookies 调 /config 验证）
  - 登录成功后提取所有 quark 域 cookies 写入文件
"""
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import base64
import io
import re

import httpx
from loguru import logger
from DrissionPage.errors import BrowserConnectError

from utils import find_chrome_path

try:
    from PIL import Image  # type: ignore
    from pyzbar.pyzbar import decode as _zbar_decode  # type: ignore
    _QR_DECODER_AVAILABLE = True
except Exception as _qr_err:  # pragma: no cover
    Image = None  # type: ignore
    _zbar_decode = None  # type: ignore
    _QR_DECODER_AVAILABLE = False
    logger.debug(f"pyzbar/Pillow not available, QR URL decoding disabled: {_qr_err}")


def _decode_qr_data_url(data_url: str) -> Optional[str]:
    """从 data:image/png;base64 的 QR 图片中解码出 URL。失败返回 None。"""
    if not _QR_DECODER_AVAILABLE or not data_url:
        return None
    m = re.match(r"data:image/[^;]+;base64,(.+)$", data_url, re.DOTALL)
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(1))
        img = Image.open(io.BytesIO(raw))  # type: ignore[union-attr]
        results = _zbar_decode(img)  # type: ignore[misc]
        for r in results:
            try:
                return r.data.decode("utf-8", errors="replace")
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"QR decode failed: {e}")
    return None

QUARK_DOMAINS = (".quark.cn", "quark.cn", "pan.quark.cn", "drive-pc.quark.cn", "drive.quark.cn")
QUARK_CHECK_URL = "https://drive-pc.quark.cn/1/clouddrive/config?pr=ucpro&fr=pc&uc_param_str="
QUARK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

QR_SCAN_AREA_XPATH = 'xpath://div[@class="scan-area"]'

QR_JS_FALLBACK = """
// 严格只在登录的 scan-area / qrcode-display 里找 canvas；
// 不再 fallback 到任意 >=100x100 的 canvas——主页上的"下载 App / 分享"二维码会误命中。
const el = document.querySelector('.scan-area canvas')
      || document.querySelector('.qrcode-display canvas');
if (!el) return null;
return el.toDataURL('image/png');
"""

class QuarkLoginWorker:
    """单例后台 worker。状态机:
      idle → starting → waiting_scan → logged_in
                                    ↘ failed
    """

    def __init__(self, cookies_path: Path, poll_interval: float = 3.0,
                 qr_refresh_interval: float = 60.0, timeout: float = 300.0):
        self.cookies_path = cookies_path
        self.poll_interval = poll_interval
        self.qr_refresh_interval = qr_refresh_interval
        self.timeout = timeout

        self._lock = threading.Lock()
        self._state = "idle"
        self._qr_data_url: Optional[str] = None
        self._error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ---- public read API ----
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "qr_data_url": self._qr_data_url,
                "error": self._error,
            }

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- public control API ----
    def start(self) -> None:
        with self._lock:
            if self.is_running():
                logger.info("login worker already running, skip start")
                return
            self._state = "starting"
            self._qr_data_url = None
            self._error = None
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="QuarkLoginWorker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            if self._state != "logged_in":
                self._state = "idle"

    # ---- worker thread ----
    def _set_state(self, state: str, qr: Optional[str] = None, error: Optional[str] = None) -> None:
        with self._lock:
            self._state = state
            if qr is not None:
                self._qr_data_url = qr
            if error is not None:
                self._error = error

    def _run(self) -> None:
        logger.info("login worker started")
        page = None
        try:
            page = self._launch_page()
            logger.info("chrome launched, navigating to pan.quark.cn")
            # 进入 about:blank 先清一次 cookies/storage，再 navigate，双保险
            try:
                page.get("about:blank")
                try:
                    page.run_cdp("Network.clearBrowserCookies")
                except Exception as e:
                    logger.debug(f"clearBrowserCookies failed: {e}")
            except Exception as e:
                logger.debug(f"pre-clean failed: {e}")

            page.get("https://pan.quark.cn/")
            time.sleep(2)

            # 早期检测：如果浏览器已经是登录态（极少数情况下 --incognito 仍复用），直接保存 cookies 跳过扫码
            try:
                cookies = self._extract_quark_cookies(page)
                cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                if cookie_str and self._is_logged_in(cookie_str):
                    logger.info(f"[LOGIN] 浏览器已是登录态，跳过扫码直接保存 cookies ({len(cookies)} 项)")
                    self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.cookies_path, "w", encoding="utf-8") as f:
                        f.write(cookie_str + "\n")
                    self._set_state("logged_in")
                    return
            except Exception as e:
                logger.debug(f"early login detect failed: {e}")

            # first QR extract
            self._extract_and_store_qr(page)
            self._set_state("waiting_scan")

            start = time.time()
            last_qr_refresh = start
            attempt = 0
            while not self._stop_event.is_set() and (time.time() - start) < self.timeout:
                attempt += 1

                # periodic QR refresh
                if time.time() - last_qr_refresh > self.qr_refresh_interval:
                    try:
                        self._extract_and_store_qr(page)
                        last_qr_refresh = time.time()
                        logger.debug(f"QR refreshed at attempt {attempt}")
                    except Exception as e:
                        logger.warning(f"QR refresh failed: {e}")

                # check login state
                try:
                    cookies = self._extract_quark_cookies(page)
                    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                    if cookie_str and self._is_logged_in(cookie_str):
                        logger.info(f"login detected at attempt {attempt} ({len(cookies)} cookies)")
                        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(self.cookies_path, "w", encoding="utf-8") as f:
                            f.write(cookie_str + "\n")
                        self._set_state("logged_in")
                        return
                except Exception as e:
                    logger.warning(f"login check failed: {e}")

                self._stop_event.wait(self.poll_interval)

            if self._stop_event.is_set():
                logger.info("login worker stopped by user")
                self._set_state("idle")
            else:
                logger.warning("login worker timeout")
                self._set_state("failed", error=f"超过 {int(self.timeout)} 秒未扫码，已取消")
        except Exception as e:
            logger.exception("login worker crashed")
            self._set_state("failed", error=f"{type(e).__name__}: {e}")
        finally:
            if page is not None:
                try:
                    page.quit()
                except Exception:
                    pass
            logger.info("login worker exited")

    # ---- helpers ----
    def _launch_page(self):
        from DrissionPage import ChromiumOptions, ChromiumPage

        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                co = ChromiumOptions()
                co.auto_port()
                co.headless(True)
                # 关键：强制隐私模式 + 禁用 Chrome Sign-In / Sync，避免复用本机 Chrome 的登录态
                # 之前的 bug：用户本机 Chrome 已登录夸克时，headless 实例也继承登录 → 访问 pan.quark.cn
                # 直接跳主页，登录页的 .scan-area 根本不存在，导致抓 QR 时拿到错的 canvas。
                co.set_argument("--incognito")
                co.set_argument("--disable-features=ChromeSignIn,ChromeSignInAndSync,SyncDisabledTests")
                co.set_argument("--no-first-run")
                co.set_argument("--no-default-browser-check")
                co.set_argument("--disable-background-networking")
                co.set_argument("--disable-component-update")
                co.set_argument("--disable-default-apps")
                co.set_argument("--disable-sync")
                co.set_argument("--metrics-recording-only")
                co.set_argument("--window-size=1280,900")

                chrome = find_chrome_path()
                if chrome:
                    logger.info(f"using chrome at {chrome}")
                    co.set_browser_path(chrome)

                logger.info(f"launching headless chrome (attempt {attempt}/3)")
                return ChromiumPage(co)
            except BrowserConnectError as e:
                last_error = e
                logger.warning(f"chrome connect failed on attempt {attempt}/3: {e}")
                if attempt < 3:
                    time.sleep(1.5)

        assert last_error is not None
        raise last_error

    def _extract_and_store_qr(self, page) -> None:
        data_url: Optional[str] = None
        # 首选：对 .scan-area 元素截图（避免误取到"下载 App"等其它 canvas）
        try:
            ele = page.ele(QR_SCAN_AREA_XPATH, timeout=2)
            if ele:
                b64 = ele.get_screenshot(as_base64="png")
                if b64:
                    data_url = f"data:image/png;base64,{b64}"
        except Exception as e:
            logger.debug(f"scan-area screenshot failed: {e}")
        # 兜底：canvas.toDataURL
        if not data_url:
            try:
                data_url = page.run_js(QR_JS_FALLBACK)
            except Exception as e:
                logger.debug(f"canvas JS fallback failed: {e}")
        if data_url:
            self._set_state(self._state, qr=data_url)
            # 解码二维码内容，便于排查扫码后无法跳转的问题
            decoded = _decode_qr_data_url(data_url)
            if decoded:
                logger.info(f"[QR] 二维码内容: {decoded}")
            elif not _QR_DECODER_AVAILABLE:
                logger.debug("[QR] 未安装 pyzbar，跳过二维码内容解码（pip install pyzbar Pillow）")
            else:
                logger.debug("[QR] 当前二维码图片解码失败")
        else:
            # 没找到登录 QR：大概率是已被自动登录跳转到主页，或页面结构变化。打印当前 URL 便于排查。
            try:
                cur_url = getattr(page, "url", "")
            except Exception:
                cur_url = "?"
            logger.warning(f"[QR] 未找到登录二维码（.scan-area 不存在），当前页面: {cur_url}")

    def _extract_quark_cookies(self, page) -> Dict[str, str]:
        raw = page.cookies(all_domains=True, all_info=False)
        out: Dict[str, str] = {}
        for c in raw:
            domain = (c.get("domain") or "").lstrip(".")
            if not any(domain == d.lstrip(".") or domain.endswith(d) for d in QUARK_DOMAINS):
                continue
            name = c.get("name")
            value = c.get("value", "")
            if name and name not in out:
                out[name] = value
        return out

    def _is_logged_in(self, cookie_str: str) -> bool:
        try:
            r = httpx.get(
                QUARK_CHECK_URL,
                headers={
                    "user-agent": QUARK_UA,
                    "origin": "https://pan.quark.cn",
                    "referer": "https://pan.quark.cn/",
                    "cookie": cookie_str,
                    "accept": "application/json, text/plain, */*",
                },
                timeout=6,
            )
            if r.status_code != 200:
                return False
            return r.json().get("status") == 200
        except Exception:
            return False
