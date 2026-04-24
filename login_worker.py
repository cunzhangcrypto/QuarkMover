#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from loguru import logger
from DrissionPage import ChromiumPage, ChromiumOptions
from DrissionPage.errors import BrowserConnectError
import httpx

from utils import find_chrome_path

QUARK_DOMAINS = (".quark.cn", "quark.cn", "pan.quark.cn", "drive-pc.quark.cn", "drive.quark.cn")
QUARK_CHECK_URL = "https://drive-pc.quark.cn/1/clouddrive/config?pr=ucpro&fr=pc"
QUARK_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

class QuarkLoginWorker:
    def __init__(self, cookies_path: Path, poll_interval: float = 2.0,
                 qr_refresh_interval: float = 60.0, timeout: float = 120.0,
                 on_success=None):
        self.cookies_path = cookies_path
        self.poll_interval = poll_interval
        self.qr_refresh_interval = qr_refresh_interval
        self.timeout = timeout
        self.on_success = on_success

        self._lock = threading.Lock()
        self._state = "idle"
        self._qr_data_url: Optional[str] = None
        self._error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "qr_data_url": self._qr_data_url,
                "error": self._error,
            }

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_running(): return
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

    def _set_state(self, state: str, qr: Optional[str] = None, error: Optional[str] = None) -> None:
        with self._lock:
            self._state = state
            if qr is not None: self._qr_data_url = qr
            if error is not None: self._error = error

    def _run(self) -> None:
        logger.info("login worker started (Visible Mode)")
        page = None
        try:
            # 1. 启动浏览器
            co = ChromiumOptions()
            co.auto_port()
            co.headless(False)  # 可见模式，确保扫码信号正常
            co.set_argument("--disable-blink-features=AutomationControlled")
            co.set_argument("--no-first-run")
            co.set_argument("--no-default-browser-check")
            co.set_argument("--window-size=1000,800")
            
            chrome = find_chrome_path()
            if chrome: co.set_browser_path(chrome)
            
            page = ChromiumPage(co)
            
            # 2. 访问页面
            page.get("https://pan.quark.cn/")
            time.sleep(3)
            
            self._set_state("waiting_scan")
            start_time = time.time()
            
            # --- 修正后的循环缩进 ---
            while not self._stop_event.is_set() and (time.time() - start_time) < self.timeout:
                # 3. 提取 Cookie 判定逻辑
                cookies = page.cookies(all_domains=True)
                cookie_map = {c['name']: c['value'] for c in cookies}
                
                # ✅ 关键：检测到 Token 立刻收网
                if "p_token" in cookie_map or "p_utoken" in cookie_map:
                    logger.info("✅ [判定成功] 捕获到登录 Token")
                    cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_map.items()])
                    self._finalize_login(cookie_str)
                    return

                # ✅ 辅助：URL 模糊匹配
                curr_url = page.url
                if "pan.quark.cn/list" in curr_url or "pan.quark.cn/external" in curr_url:
                    logger.info(f"✅ [判定成功] 检测到跳转目标页面: {curr_url}")
                    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                    self._finalize_login(cookie_str)
                    return

                self._stop_event.wait(self.poll_interval)

            if not self._stop_event.is_set():
                self._set_state("failed", error="登录超时")

        except Exception as e:
            logger.exception("login worker crashed")
            self._set_state("failed", error=str(e))
        finally:
            if page:
                try: page.quit()
                except: pass
            logger.info("login worker exited")

    def _finalize_login(self, cookie_str: str) -> None:
        """保存Cookie并更新状态"""
        try:
            self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cookies_path, "w", encoding="utf-8") as f:
                f.write(cookie_str + "\n")
            
            if self.on_success:
                try: self.on_success(cookie_str)
                except Exception as e: logger.error(f"Callback error: {e}")
                
            self._set_state("logged_in")
        except Exception as e:
            logger.error(f"Finalize login failed: {e}")
            self._set_state("failed", error="保存Cookie失败")
