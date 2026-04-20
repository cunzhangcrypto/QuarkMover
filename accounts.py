#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多账号存储。
数据文件: config/accounts.json
  {"active_id": "xxx", "accounts": [{"id","hint","cookie","created_at","last_used_at"}...]}

兼容旧版: 始终把当前 active 账号的 cookie 写回 config/cookies.txt，
这样 quark_mover.py 中 load_cookie() 等既有逻辑无需改动。
"""
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class AccountStore:
    def __init__(self, json_path: Path, legacy_cookies: Path) -> None:
        self.json_path = json_path
        self.legacy_cookies = legacy_cookies
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._load()
        self._migrate_legacy()

    # ---- persistence ----
    def _load(self) -> Dict[str, Any]:
        if not self.json_path.exists():
            return {"active_id": "", "accounts": []}
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("active_id", "")
            d.setdefault("accounts", [])
            return d
        except Exception as e:
            logger.warning(f"accounts.json 解析失败，已重建: {e}")
            return {"active_id": "", "accounts": []}

    def _save(self) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.json_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.json_path)
        self._sync_legacy()

    def _sync_legacy(self) -> None:
        """把当前 active 账号的 cookie 写入 cookies.txt，保证既有代码读取路径不变。"""
        active = self._get_active_unlocked()
        self.legacy_cookies.parent.mkdir(parents=True, exist_ok=True)
        if active:
            with open(self.legacy_cookies, "w", encoding="utf-8") as f:
                f.write(active["cookie"] + "\n")
        else:
            try:
                self.legacy_cookies.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug(f"unlink legacy cookies failed: {e}")

    def _migrate_legacy(self) -> None:
        """首次启动：若 cookies.txt 已存在但 accounts.json 空，把它作为第一个账号导入。"""
        if self._data["accounts"]:
            return
        if not self.legacy_cookies.exists():
            return
        try:
            cookie = self.legacy_cookies.read_text(encoding="utf-8").strip()
            if cookie:
                aid = uuid.uuid4().hex[:12]
                self._data["accounts"].append({
                    "id": aid,
                    "hint": "已登录",
                    "cookie": cookie,
                    "created_at": int(time.time()),
                    "last_used_at": int(time.time()),
                })
                self._data["active_id"] = aid
                self._save()
                logger.info(f"迁移 cookies.txt → accounts.json (id={aid})")
        except Exception as e:
            logger.warning(f"迁移遗留 cookies 失败: {e}")

    # ---- internals ----
    def _get_active_unlocked(self) -> Optional[Dict[str, Any]]:
        for a in self._data["accounts"]:
            if a["id"] == self._data["active_id"]:
                return a
        return None

    # ---- public API ----
    def list_accounts(self) -> List[Dict[str, Any]]:
        with self._lock:
            active = self._data["active_id"]
            return [{
                "id": a["id"],
                "hint": a.get("hint", ""),
                "created_at": a.get("created_at", 0),
                "last_used_at": a.get("last_used_at", 0),
                "active": a["id"] == active,
            } for a in self._data["accounts"]]

    def active_cookie(self) -> str:
        with self._lock:
            a = self._get_active_unlocked()
            return a["cookie"] if a else ""

    def upsert(self, cookie: str, hint: str = "已登录") -> str:
        """按 cookie 去重：若已存在则更新 hint & 设为 active，否则新增为 active。返回 account id。"""
        cookie = cookie.strip()
        if not cookie:
            raise ValueError("cookie 为空")
        with self._lock:
            for a in self._data["accounts"]:
                if a["cookie"].strip() == cookie:
                    if hint and hint != "已登录":
                        a["hint"] = hint
                    a["last_used_at"] = int(time.time())
                    self._data["active_id"] = a["id"]
                    self._save()
                    return a["id"]
            aid = uuid.uuid4().hex[:12]
            self._data["accounts"].append({
                "id": aid,
                "hint": hint or "已登录",
                "cookie": cookie,
                "created_at": int(time.time()),
                "last_used_at": int(time.time()),
            })
            self._data["active_id"] = aid
            self._save()
            logger.info(f"新增账号 id={aid} hint={hint}")
            return aid

    def update_hint(self, aid: str, hint: str) -> bool:
        if not hint:
            return False
        with self._lock:
            for a in self._data["accounts"]:
                if a["id"] == aid:
                    a["hint"] = hint
                    self._save()
                    return True
        return False

    def switch(self, aid: str) -> bool:
        with self._lock:
            if not any(a["id"] == aid for a in self._data["accounts"]):
                return False
            self._data["active_id"] = aid
            for a in self._data["accounts"]:
                if a["id"] == aid:
                    a["last_used_at"] = int(time.time())
            self._save()
            logger.info(f"切换到账号 id={aid}")
            return True

    def remove(self, aid: str) -> bool:
        with self._lock:
            before = len(self._data["accounts"])
            self._data["accounts"] = [a for a in self._data["accounts"] if a["id"] != aid]
            if before == len(self._data["accounts"]):
                return False
            if self._data["active_id"] == aid:
                self._data["active_id"] = self._data["accounts"][0]["id"] if self._data["accounts"] else ""
            self._save()
            logger.info(f"删除账号 id={aid}")
            return True

    def clear_all(self) -> None:
        with self._lock:
            self._data = {"active_id": "", "accounts": []}
            self._save()
            logger.info("清空所有账号")
