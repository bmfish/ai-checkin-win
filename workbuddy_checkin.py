"""WorkBuddy（CodeBuddy 国内版）每日签到

凭证：%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
  { "auth": {accessToken, refreshToken, expiresAtMs, ...},
    "account": {uid, nickname, editionType, ...},
    "domain": "..." }

接口：
  POST https://www.workbuddy.cn/v2/billing/meter/checkin-activity-status   预检
  POST https://www.workbuddy.cn/v2/billing/meter/daily-checkin             签到
  GET  https://www.workbuddy.cn/v2/billing/meter/credits                   余额（失败再试 query-balance）
  POST https://www.codebuddy.cn/v2/plugin/auth/token/refresh               RT 续签

只读本机客户端凭据；accessToken 过期时用 RT 续签并把新凭据写回原文件
（保留文件里的其他字段，只改 auth 内三项）。
"""
import hashlib
import json
import os
import time

import http_common

API_BASE = "https://www.workbuddy.cn"
REFRESH_URL = "https://www.codebuddy.cn/v2/plugin/auth/token/refresh"
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
CREDITS_PATH = "/v2/billing/meter/credits"
BALANCE_PATH = "/v2/billing/meter/query-balance"
USER_AGENT = "WorkBuddy/5.5.6"

# 测试注入：覆盖真实客户端凭据路径
info_override = None


class WorkBuddyAccount:
    """一个 WorkBuddy 账号（凭据 + 展示字段）"""

    def __init__(self, uid, nickname="", access_token="", refresh_token="",
                 expires_at_ms=0, domain="", path=""):
        self.uid = uid
        self.nickname = nickname or uid
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at_ms = int(expires_at_ms or 0)
        self.domain = domain or ""
        self.path = path or ""

    def __repr__(self):
        return "WorkBuddyAccount(uid=%r, nickname=%r, domain=%r)" % (
            self.uid, self.nickname, self.domain)


class CheckinStatus:
    def __init__(self, ok=False, active=True, signed=False, streak=0,
                 daily_credit=0.0, today_credit=0.0, message=""):
        self.ok = ok
        self.active = active
        self.signed = signed
        self.streak = streak
        self.daily_credit = daily_credit
        self.today_credit = today_credit
        self.message = message


class CheckinResult:
    def __init__(self, ok=False, already=False, message="", reward=0.0):
        self.ok = ok
        self.already = already
        self.message = message
        self.reward = reward


# ------------------------------------------------------------ 凭据

def info_path():
    if info_override is not None:
        return info_override
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    return os.path.join(local, "CodeBuddyExtension", "Data", "Public",
                        "auth", "workbuddy-desktop.info")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _int(value, default=0):
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None


def parse_credentials(obj, path=None):
    """凭据 JSON → WorkBuddyAccount；缺 token / uid 时返回 None（不联网）"""
    if not isinstance(obj, dict):
        return None
    auth = obj.get("auth") if isinstance(obj.get("auth"), dict) else {}
    account = obj.get("account") if isinstance(obj.get("account"), dict) else {}
    token = _text(auth.get("accessToken")) or _text(auth.get("access_token"))
    uid = _text(account.get("uid"))
    if not token or not uid:
        return None
    return WorkBuddyAccount(
        uid=uid,
        nickname=_text(account.get("nickname")),
        access_token=token,
        refresh_token=_text(auth.get("refreshToken")) or _text(auth.get("refresh_token")),
        expires_at_ms=_int(auth.get("expiresAtMs")) or _int(auth.get("expires_at_ms")),
        domain=_text(obj.get("domain")) or _text(auth.get("domain")),
        path=path or "")


def discover(now=None, post=None, refresh_expired=True):
    """读取本机凭据；过期时先 RT 续签（失败则沿用原 token）"""
    path = info_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError, TypeError):
        return []
    account = parse_credentials(obj, path)
    if not account:
        return []
    if refresh_expired and _expired(account, now):
        refreshed = refresh(account, post=post, now=now)
        if refreshed:
            account.access_token = refreshed["accessToken"]
            account.refresh_token = refreshed["refreshToken"]
            account.expires_at_ms = refreshed["expiresAtMs"]
            _write_back(account, path)
    return [account]


def _expired(account, now=None):
    if account.expires_at_ms <= 0:
        return False
    now_ms = int((time.time() if now is None else _epoch(now)) * 1000)
    return account.expires_at_ms < now_ms


def _epoch(now):
    if isinstance(now, (int, float)):
        return float(now)
    if hasattr(now, "timestamp"):
        return now.timestamp()
    return time.time()


def refresh(account, post=None, now=None):
    """RT 续签 → {accessToken, refreshToken, expiresAtMs}；失败返回 None"""
    if not account.refresh_token:
        return None
    send = post or http_common.http_post
    headers = {
        "Authorization": "Bearer " + account.access_token,
        "X-Refresh-Token": account.refresh_token,
        "X-Auth-Refresh-Source": "workbuddy",
        "Content-Type": "application/json",
    }
    try:
        status, _resp_headers, text = send(REFRESH_URL, headers=headers,
                                           body="{}", timeout=20)
    except Exception:
        return None
    if status != 200 or not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    token = _find_string(data, ("accessToken", "access_token"))
    if not token:
        return None
    refresh_token = _find_string(data, ("refreshToken", "refresh_token")) \
        or account.refresh_token
    expires_in = _find_number(data, ("expiresIn", "expires_in"))
    now_ms = int((time.time() if now is None else _epoch(now)) * 1000)
    expires_at_ms = now_ms + int(expires_in * 1000) if expires_in else 0
    return {"accessToken": token, "refreshToken": refresh_token,
            "expiresAtMs": expires_at_ms}


def _write_back(account, path):
    """新凭据写回客户端文件：保留其他字段，只改 auth 内三项"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            root = json.load(f)
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(root, dict):
        return
    auth = root.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        root["auth"] = auth
    auth["accessToken"] = account.access_token
    auth["refreshToken"] = account.refresh_token
    auth["expiresAtMs"] = account.expires_at_ms
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(root, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


# ------------------------------------------------------------ 请求头

def _fp(uid, kind):
    raw = hashlib.sha256(("wb-fingerprint:%s:%s" % (kind, uid)).encode("utf-8"))
    return raw.hexdigest()[:32]     # 前 16 字节


def headers(account):
    return {
        "Authorization": "Bearer " + account.access_token,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "X-User-Id": account.uid,
        "X-Domain": account.domain,
        "X-Machine-ID": _fp(account.uid, "machine"),
        "X-Session-ID": _fp(account.uid, "session"),
    }


# ------------------------------------------------------------ 预检 / 签到

def status(account, post=None):
    send = post or http_common.http_post
    try:
        http_status, _resp_headers, text = send(
            API_BASE + STATUS_PATH, headers=headers(account), body="{}", timeout=15)
    except Exception:
        return CheckinStatus(message="网络请求失败")
    if http_status != 200 or not text:
        return CheckinStatus(message="HTTP %s" % http_status if http_status
                             else "网络请求失败")
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return CheckinStatus(message="响应格式异常")
    if not isinstance(obj, dict):
        return CheckinStatus(message="响应格式异常")

    code = _int(obj.get("code"), None)
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    signed = bool(data.get("today_checked_in") or data.get("checked_in")
                  or data.get("did_checked_in")
                  or code == 10001 or _contains_already(_message(obj)))
    active = data.get("active")
    return CheckinStatus(
        ok=(code is None or code in (0, 200, 10001)),
        active=True if active is None else bool(active),
        signed=signed,
        streak=_find_int(data, ("streak_days", "streakDays", "streak",
                                "continuous_days", "consecutive_days",
                                "continuous_checkin_days", "continuousDays")) or 0,
        daily_credit=_find_number(data, ("daily_credit", "dailyCredit")) or 0.0,
        today_credit=_find_number(data, ("today_credit", "todayCredit")) or 0.0,
        message=_message(obj))


def checkin(account, post=None):
    send = post or http_common.http_post
    try:
        http_status, _resp_headers, text = send(
            API_BASE + CHECKIN_PATH, headers=headers(account), body="{}", timeout=15)
    except Exception:
        return CheckinResult(message="网络请求失败")
    if not text:
        return CheckinResult(message="HTTP %s" % http_status if http_status
                             else "网络请求失败")
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return CheckinResult(message="HTTP %s 响应格式异常" % http_status)
    if not isinstance(obj, dict):
        return CheckinResult(message="响应格式异常")

    code = _int(obj.get("code"), None)
    message = _message(obj)
    reward = _find_number(obj, ("reward", "credits", "points",
                                "reward_credits", "rewardCredits")) or 0.0
    if code == 10001 or _contains_already(message):
        return CheckinResult(True, already=True,
                             message=message or "今日已签", reward=reward)
    ok = code in (0, 200) or (http_status in (200, 201) and code is None)
    if ok:
        return CheckinResult(True, message=message or "签到成功", reward=reward)
    return CheckinResult(False, message=message or "HTTP %s" % http_status)


def balance(account, post=None, get=None):
    """余额：先 GET /credits，失败再 POST /query-balance；都失败返回 None"""
    targets = (
        ("GET", API_BASE + CREDITS_PATH, None, get or http_common.http_get),
        ("POST", API_BASE + BALANCE_PATH, "{}", post or http_common.http_post),
    )
    for method, url, body, send in targets:
        try:
            if method == "GET":
                http_status, _resp_headers, text = send(
                    url, headers=headers(account), timeout=15)
            else:
                http_status, _resp_headers, text = send(
                    url, headers=headers(account), body=body, timeout=15)
        except Exception:
            continue
        if http_status != 200 or not text:
            continue
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        data = obj.get("data")
        remain = _find_number(data, ("balance", "remain", "remaining", "available",
                                     "remain_credits", "remainCredits",
                                     "total_credits", "totalCredits"))
        if remain is None:
            remain = _num(data) if not isinstance(data, (dict, list)) else None
        if remain is None:
            continue
        total = _find_number(data, ("total", "total_amount", "totalAmount",
                                    "granted", "quota"))
        return {"remain": remain, "total": total}
    return None


# ------------------------------------------------------------ 宽松取值

def _message(obj):
    for key in ("message", "msg", "errMsg", "error", "statusMessage"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = obj.get("data")
    if isinstance(data, dict):
        for key in ("message", "msg"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _contains_already(message):
    lower = (message or "").lower()
    return any(token in lower for token in ("已签到", "已经签到", "repeat", "already"))


def _find_string(node, names, depth=0):
    """递归找第一个非空字符串（键名大小写不敏感）"""
    if depth > 6:
        return ""
    if isinstance(node, dict):
        for name in names:
            for key, value in node.items():
                if str(key).lower() == name.lower() and isinstance(value, str) \
                        and value.strip():
                    return value.strip()
        for value in node.values():
            found = _find_string(value, names, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_string(item, names, depth + 1)
            if found:
                return found
    return ""


def _find_number(node, names, depth=0):
    """递归找第一个数值（键名大小写不敏感；bool 不算数值）"""
    if depth > 6:
        return None
    if isinstance(node, dict):
        for name in names:
            for key, value in node.items():
                if str(key).lower() == name.lower():
                    number = _num(value)
                    if number is not None:
                        return number
        for value in node.values():
            found = _find_number(value, names, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_number(item, names, depth + 1)
            if found is not None:
                return found
    return None


def _find_int(node, names, depth=0):
    value = _find_number(node, names, depth)
    return int(value) if value is not None else None
