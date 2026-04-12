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

import httpx
from loguru import logger
from DrissionPage.errors import BrowserConnectError

from utils import find_chrome_path

QUARK_DOMAINS = (".quark.cn", "quark.cn", "pan.quark.cn", "drive-pc.quark.cn", "drive.quark.cn")
QUARK_CHECK_URL = "https://drive-pc.quark.cn/1/clouddrive/config?pr=ucpro&fr=pc&uc_param_str="
QUARK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

QR_JS = """
const el = document.querySelector('.qrcode-display canvas')
      || Array.from(document.querySelectorAll('canvas')).find(c => c.width >= 100 && c.height >= 100);
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
            page.get("https://pan.quark.cn/")
            time.sleep(2)

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
        data_url = page.run_js(QR_JS)
        if data_url:
            self._set_state(self._state, qr=data_url)

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
