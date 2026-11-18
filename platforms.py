"""三平台账号发现与今日状态汇总（供界面与签到调度共用）

Trae 的账号 key 沿用裸 uid（与旧版 trae-checkin 的 state.json 水位兼容），
WorkBuddy / Qoder 用 "wb:" / "qd:" 前缀，避免不同平台的 id 互相覆盖。
"""
from datetime import datetime

import qoder_checkin
import today_usage
import trae_client_credentials
import workbuddy_checkin


class PlatformAccount:
    def __init__(self, platform, key, uid, display_name, credential, source=""):
        self.platform = platform
        self.key = key
        self.uid = uid
        self.display_name = display_name
        self.credential = credential
        self.source = source

    def __repr__(self):
        return "PlatformAccount(platform=%r, key=%r)" % (self.platform, self.key)


def discover(now=None, refresh_expired=True):
    """本机三平台账号（按 平台 顺序：Trae / WorkBuddy / Qoder）"""
    accounts = []
    for cred in trae_client_credentials.discover(now=now):
        accounts.append(PlatformAccount(
            "Trae", cred.uid, cred.uid, cred.uid, cred,
            cred.source_app or "Trae"))
    for cred in workbuddy_checkin.discover(now=now, refresh_expired=refresh_expired):
        accounts.append(PlatformAccount(
            "WorkBuddy", "wb:%s" % cred.uid, cred.uid, cred.nickname or cred.uid,
            cred, "CodeBuddy CN"))
    auth = qoder_checkin.load_auth()
    if auth:
        uid = auth.uid or "account"
        accounts.append(PlatformAccount(
            "Qoder", "qd:%s" % uid, uid, auth.uid or "Qoder", auth, "Qoder"))
    return accounts


def usage_rows(store, force=False, now=None):
    """每个账号一行卡片数据，字段：

    platform / key / name / ok / signed / primary / secondary /
    balance / total / message
    """
    dt = now if isinstance(now, datetime) else datetime.now()
    today = dt.strftime("%Y-%m-%d")
    rows = []

    try:
        trae_items = today_usage.load(store.dir, force=force, now=dt)
    except Exception:
        trae_items = []
    for item in trae_items:
        uid = item.get("uid") or ""
        if not uid:
            continue
        rows.append({
            "platform": "Trae",
            "key": uid,
            "name": uid,
            "ok": bool(item.get("ok")),
            "signed": store.done_today(uid, today),
            "primary": float(item.get("credits") or 0),
            "secondary": "会话 %d" % int(item.get("sessions") or 0),
            "balance": item.get("balance"),
            "total": item.get("total"),
            "message": item.get("message") or "查询失败",
        })

    for cred in workbuddy_checkin.discover(now=dt):
        st = workbuddy_checkin.status(cred)
        balance = workbuddy_checkin.balance(cred) if st.ok else None
        primary = st.today_credit if st.today_credit > 0 else st.daily_credit
        rows.append({
            "platform": "WorkBuddy",
            "key": "wb:%s" % cred.uid,
            "name": cred.nickname or cred.uid,
            "ok": st.ok,
            "signed": st.signed,
            "primary": float(primary or 0),
            "secondary": "连签 %d 天" % st.streak,
            "balance": balance["remain"] if balance else None,
            "total": balance["total"] if balance else None,
            "message": st.message or "查询失败",
        })

    auth = qoder_checkin.load_auth()
    if auth:
        campaigns, unauthorized = qoder_checkin.campaigns(auth)
        if campaigns is None:
            campaigns, failed = [], True
        else:
            failed = False
        claimable = [c for c in campaigns if c.claimable]
        signed = not unauthorized and not failed and not claimable
        message = "登录已过期，请重新登录 Qoder" if unauthorized else (
            "活动查询失败" if failed else "")
        rows.append({
            "platform": "Qoder",
            "key": "qd:%s" % (auth.uid or "account"),
            "name": auth.uid or "Qoder",
            "ok": not unauthorized and not failed,
            "signed": signed,
            "primary": float(len(claimable)),
            "secondary": ("可领 %d 个活动" % len(claimable)) if claimable else "暂无新活动",
            "balance": None,
            "total": None,
            "message": message,
        })

    return rows
