"""AI 签到（独立 Windows 程序）

功能：Trae / WorkBuddy / Qoder 三个平台的每日签到合到一个托盘程序里，
自动签到 + 签到记录 + 今日状态，不含其他统计。

用法：
    ai-checkin.exe                 打开窗口（可刷新 / 手动签到）
    ai-checkin.exe --silent        常驻后台，每天配置小时之后随机一分钟自动签到
    ai-checkin.exe --once          立即签到一次（已签的账号仍然跳过）
    ai-checkin.exe --status        查看今日状态与最近签到记录
    ai-checkin.exe --install       注册开机自启（以 --silent 启动）
    ai-checkin.exe --uninstall     取消开机自启

状态与记录目录：%USERPROFILE%\\.trae-checkin（沿用旧版，避免丢水位）
    state.json    已签水位 / 失败时间 / 最近 100 条记录
    checkin.log   追加式文本日志
"""
import argparse
import ctypes
import json
import os
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime

try:
    import msvcrt
except ImportError:      # 非 Windows 平台：保留导入能力，便于静态检查
    msvcrt = None

import checkin_core

# 管道 / 重定向输出时控制台编码可能是 GBK：非终端场景统一 UTF-8，
# 终端场景只做降级替换（控制台本身用宽字符写入，中文不受影响）
try:
    if sys.stdout.isatty():
        sys.stdout.reconfigure(errors="replace")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP_NAME = "AICheckin"
TITLE = "AI 签到"
PLATFORM_LINE = "Trae / WorkBuddy / Qoder"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SETTINGS_FILE = "settings.json"
DEFAULT_START_HOUR = 9
DEFAULT_INTERVAL = 60


def load_settings(directory):
    try:
        with open(os.path.join(directory, SETTINGS_FILE), encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_settings(directory, values):
    merged = load_settings(directory)
    merged.update(values)
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, SETTINGS_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(directory, SETTINGS_FILE))
    except OSError:
        pass


def configured_start_hour(state_dir=None):
    """签到起始小时：默认 9，界面/命令行可改"""
    raw = load_settings(state_dir or checkin_core.state_dir()).get("start_hour")
    try:
        return max(0, min(23, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_START_HOUR


def effective_start_hour(args, state_dir=None):
    """命令行显式 --start-hour 优先，否则读界面保存的配置"""
    value = getattr(args, "start_hour", None)
    if value is not None:
        return max(0, min(23, int(value)))
    return configured_start_hour(state_dir)


@contextmanager
def process_lock(directory, timeout=30):
    """跨进程签到锁：常驻与手动 --once 不会同时 claim"""
    if msvcrt is None:
        yield
        return
    os.makedirs(directory, exist_ok=True)
    fh = open(os.path.join(directory, "checkin.lock"), "a+b")
    if fh.tell() == 0:
        fh.write(b"0")
        fh.flush()
    deadline = time.time() + timeout
    while True:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.time() >= deadline:
                fh.close()
                raise TimeoutError("另一个签到实例正在运行")
            time.sleep(1)
    try:
        yield
    finally:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh.close()


def hide_console():
    """常驻模式隐藏控制台窗口（打包成 console exe 也能静默运行）"""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


LIGHT_THEME = {
    "dark": False,
    "BG": "#F4F7F5",
    "CARD": "#FFFFFF",
    "CARD_LINE": "#DEE9E2",
    "DIVIDER": "#E6EDE9",
    "FG": "#122019",
    "MUTED": "#5E6E65",
    "DIM": "#8A9890",
    "ACCENT": "#0B9152",
    "DANGER": "#C0392B",
    "FIELD": "#F2F6F4",
    "BTN_BG": "#FFFFFF",
    "BTN_LINE": "#DCE4DF",
    "BTN_ACTIVE": "#E6F8EF",
    "BADGE_BG": "#E6F8EF",
    "BADGE_FG": "#0A7C46",
    "BADGE_LINE": "#9FDFC0",
    "BTN_MUTED": "#F2F6F4",
    "TRACK": "#E7EEE9",
}

DARK_THEME = {
    "dark": True,
    "BG": "#0A0E0B",
    "CARD": "#0F1B14",
    "CARD_LINE": "#1C4A33",
    "DIVIDER": "#1B221D",
    "FG": "#E8F2EC",
    "MUTED": "#7C8A80",
    "DIM": "#5C6A60",
    "ACCENT": "#3DF08C",
    "DANGER": "#FF7B72",
    "FIELD": "#0E1511",
    "BTN_BG": "#14181A",
    "BTN_LINE": "#2A3136",
    "BTN_ACTIVE": "#1C2420",
    "BADGE_BG": "#143324",
    "BADGE_FG": "#3DF08C",
    "BADGE_LINE": "#1C4A33",
    "BTN_MUTED": "#1F2732",
    "TRACK": "#1A211C",
}


def system_prefers_light():
    """Windows 系统外观：设置 → 个性化 → 颜色 → 应用模式"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except Exception:
        return True


def resolve_theme(directory):
    """外观：settings.json 的 appearance（system/light/dark），缺省跟随系统"""
    mode = load_settings(directory).get("appearance", "system")
    if mode == "light":
        return dict(LIGHT_THEME)
    if mode == "dark":
        return dict(DARK_THEME)
    return dict(LIGHT_THEME if system_prefers_light() else DARK_THEME)


def apply_titlebar(root, dark, caption, text):
    """按当前主题设置标题栏颜色（Win10 1903+ 暗色，Win11 可自定义颜色）"""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if not hwnd:
            return
        enabled = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE（旧系统用 19）
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(enabled), ctypes.sizeof(enabled))

        def colorref(value):
            r = int(value[1:3], 16)
            g = int(value[3:5], 16)
            b = int(value[5:7], 16)
            return ctypes.c_int(r | (g << 8) | (b << 16))

        # Win11 22H2+：标题栏 / 文字 / 边框跟随界面配色
        for attr, color in ((35, caption), (36, text), (34, caption)):
            value = colorref(color)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def make_tray_icon():
    """托盘图标：绿色圆 + 深色对勾"""
    from PIL import Image, ImageDraw
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(50, 240, 140, 255))
    draw.line((17, 33, 28, 44, 47, 20), fill=(6, 19, 12, 255), width=7, joint="curve")
    return image


def build_tray(show=None, checkin=None, toggle_autostart=None, quit_app=None):
    """系统托盘（pystray / pillow 不可用时返回 None，程序照常运行）"""
    try:
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("打开窗口", show or (lambda: None), default=True),
            pystray.MenuItem("立即签到", checkin or (lambda: None)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", toggle_autostart or (lambda: None),
                             checked=lambda item: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", quit_app or (lambda: None)),
        )
        return pystray.Icon("ai-checkin", make_tray_icon(),
                            "AI 签到 · " + PLATFORM_LINE, menu)
    except Exception:
        return None


def launch_command(start_hour=None):
    """开机自启命令：--silent 常驻，可带 --start-hour 固定签到起始小时"""
    extra = " --silent"
    if start_hour is not None:
        extra += " --start-hour %d" % int(start_hour)
    if getattr(sys, "frozen", False):
        return '"%s"%s' % (sys.executable, extra)
    return '"%s" "%s"%s' % (sys.executable, os.path.abspath(__file__), extra)


def set_autostart(enable, start_hour=None):
    try:
        import winreg
    except ImportError:
        return False
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                              launch_command(start_hour))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    return True


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except Exception:
        return False


def print_status(store):
    today = datetime.now().strftime("%Y-%m-%d")
    print("AI 签到 · %s" % today)
    print("状态目录: %s" % store.dir)
    print("开机自启: %s" % ("已开启" if autostart_enabled() else "未开启"))
    try:
        import platforms
        accounts = platforms.discover(refresh_expired=False)
    except Exception:
        accounts = []
    if not accounts:
        print("账号: 未发现本机客户端凭据（请先登录 Trae / CodeBuddy / Qoder）")
    for acc in accounts:
        done = store.done_today(acc.key, today)
        slot = checkin_core.slot_minute(acc.key, today)
        print("账号 %s（%s）" % (acc.display_name, acc.platform))
        print("  今日: %s" % ("已签" if done else "未签，计划 %02d:%02d 后签到" % (
            configured_start_hour(store.dir), slot)))
    credits = store.today_credits(today)
    if credits:
        print("今日获得: +%g 积分" % credits)
    recent = store.recent(10)
    print("最近记录:")
    if not recent:
        print("  （无）")
    for rec in recent:
        print("  %s  uid=%s  %s  %s" % (
            datetime.fromtimestamp(rec["t"]).strftime("%Y-%m-%d %H:%M:%S"),
            rec["uid"],
            ("+%g" % rec["credits"]) if rec["credits"] else ("成功" if rec["ok"] else "失败"),
            rec["msg"]))


def run_ui(args, show=True):
    """托盘常驻 + 三平台账号窗口；关闭窗口只隐藏到托盘，退出走托盘菜单"""
    import threading

    # 后台自动签到只启动一次：切换主题会重建窗口，不能重复起线程
    threading.Thread(target=loop, args=(args,), daemon=True).start()

    while True:
        result = _run_ui_once(args, show)
        if result != "restart":
            return result
        show = True


def _run_ui_once(args, show=True):
    import queue
    import threading
    import tkinter as tk

    import platforms

    hide_console()
    store_dir = checkin_core.CheckinStore(args.state_dir).dir
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    theme = resolve_theme(store_dir)
    BG, CARD = theme["BG"], theme["CARD"]
    CARD_LINE, DIVIDER = theme["CARD_LINE"], theme["DIVIDER"]
    FG, MUTED, DIM = theme["FG"], theme["MUTED"], theme["DIM"]
    ACCENT, DANGER = theme["ACCENT"], theme["DANGER"]
    FIELD, BTN_BG = theme["FIELD"], theme["BTN_BG"]
    BTN_LINE, BTN_ACTIVE = theme["BTN_LINE"], theme["BTN_ACTIVE"]
    BADGE_BG, BADGE_FG = theme["BADGE_BG"], theme["BADGE_FG"]
    BADGE_LINE, BTN_MUTED = theme["BADGE_LINE"], theme["BTN_MUTED"]
    FONT = "Microsoft YaHei UI"

    root = tk.Tk()
    root.title(TITLE)
    root.geometry("460x440")
    root.minsize(460, 320)
    root.configure(bg=BG)
    root.after(60, lambda: apply_titlebar(root, theme["dark"], BG.lower(), FG))
    if not show:
        root.withdraw()

    restart = {"value": False}

    ui_queue = queue.Queue()

    def post(fn):
        ui_queue.put(fn)

    def pump():
        try:
            while True:
                ui_queue.get_nowait()()
        except queue.Empty:
            pass
        root.after(100, pump)

    def show_window():
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.focus_force()
        root.after(800, lambda: root.attributes("-topmost", False))

    def hide_window():
        root.withdraw()

    def hide_if_unfocused():
        if root.state() != "normal":
            return
        try:
            if root.focus_get() is None:
                hide_window()
        except (KeyError, tk.TclError):
            pass

    def on_focus_out(event=None):
        try:
            root.after(120, hide_if_unfocused)
        except tk.TclError:
            pass

    # ---- 头部 ----
    tk.Frame(root, bg=ACCENT, height=1).place(x=0, y=0, relwidth=1)
    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=16, pady=(14, 10))
    title_row = tk.Frame(head, bg=BG)
    title_row.pack(side="left", anchor="s")
    tk.Label(title_row, text=TITLE, bg=BG, fg=FG,
             font=(FONT, 14, "bold")).pack(side="left")
    tk.Label(title_row, text="· " + PLATFORM_LINE, bg=BG, fg=MUTED,
             font=(FONT, 8)).pack(side="left", padx=(6, 0), pady=(4, 0))
    tk.Label(head, text=datetime.now().strftime("%Y-%m-%d"), bg=BG, fg=DIM,
             font=(FONT, 8)).pack(side="right", pady=(4, 0))
    tk.Button(head, text="☀" if theme["dark"] else "☾",
              command=lambda: switch_theme(), relief="flat", bd=0,
              highlightthickness=1, highlightbackground=BTN_LINE, bg=FIELD,
              fg=ACCENT if theme["dark"] else MUTED,
              activebackground=BTN_ACTIVE, activeforeground=ACCENT,
              cursor="hand2", font=(FONT, 10), width=2, padx=0, pady=1).pack(
        side="right", padx=(0, 8), pady=(1, 0))

    # ---- 卡片区（高度超限时滚动）----
    holder = tk.Frame(root, bg=BG)
    holder.pack(fill="both", expand=True, padx=16)
    canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0,
                       yscrollincrement=16)
    scrollbar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
    body = tk.Frame(canvas, bg=BG)
    body_id = canvas.create_window((0, 0), window=body, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    canvas.bind("<Configure>",
                lambda event: canvas.itemconfigure(body_id, width=event.width))

    # ---- 底部信息 ----
    foot = tk.Frame(root, bg=BG)
    foot.pack(fill="x", padx=16, pady=(6, 12))
    tk.Frame(foot, bg=DIVIDER, height=1).pack(fill="x", pady=(0, 5))

    def info_row(label):
        row = tk.Frame(foot, bg=BG, height=32)
        row.pack(fill="x")
        row.pack_propagate(False)
        tk.Label(row, text=label, bg=BG, fg=MUTED, font=(FONT, 8),
                 width=7, anchor="w").pack(side="left")
        content = tk.Frame(row, bg=BG)
        content.pack(side="left", fill="x", expand=True)
        return content

    sign_row = info_row("签到")
    summary_label = tk.Label(sign_row, text="正在读取…", bg=BG, fg=MUTED,
                             font=(FONT, 8))
    summary_label.pack(side="left")

    time_row = info_row("签到时间")
    hour_var = tk.StringVar(value="%02d" % configured_start_hour(store_dir))
    hour_box = tk.Spinbox(time_row, from_=0, to=23, width=3, textvariable=hour_var,
                          command=lambda: on_hour_change(), font=(FONT, 8),
                          justify="center", bg=FIELD, fg=FG, insertbackground=FG,
                          relief="flat", bd=0, buttonbackground=BTN_LINE)
    hour_box.pack(side="left")
    hour_box.bind("<Return>", lambda event: on_hour_change())
    hour_box.bind("<FocusOut>", lambda event: on_hour_change())
    range_label = tk.Label(time_row, text="", bg=BG, fg=MUTED, font=(FONT, 8))
    range_label.pack(side="left", padx=(10, 0))

    auto_row = info_row("开机自启")
    auto_btn = tk.Button(auto_row, text="", command=lambda: toggle_autostart(),
                         relief="flat", bd=0, cursor="hand2", anchor="w", bg=BG,
                         fg=FG, activebackground=BG, activeforeground=ACCENT,
                         font=(FONT, 8), padx=0, pady=0)
    auto_btn.pack(side="left")
    tip_label = tk.Label(auto_row, text="", bg=BG, fg=DIM, font=(FONT, 8))
    tip_label.pack(side="left", padx=(12, 0))

    actions = tk.Frame(foot, bg=BG)
    actions.pack(fill="x", pady=(6, 0))

    def flat_button(text, command, primary=False):
        return tk.Button(
            actions, text=text, command=command, relief="flat", bd=0,
            highlightthickness=1,
            highlightbackground=BADGE_LINE if primary else BTN_LINE,
            cursor="hand2", padx=14, pady=4,
            font=(FONT, 8, "bold" if primary else "normal"),
            bg=BADGE_BG if primary else BTN_BG,
            fg=BADGE_FG if primary else FG,
            activebackground=BADGE_LINE if primary else BTN_ACTIVE,
            activeforeground=BADGE_FG if primary else FG)

    sign_btn = flat_button("立即签到", lambda: do_checkin(), primary=True)
    sign_btn.pack(side="right")
    refresh_btn = flat_button("刷新", lambda: load_usage(force=True))
    refresh_btn.pack(side="right", padx=(0, 8))

    state = {"busy": False, "signing": False}

    def fmt_money(value):
        number = round(float(value or 0), 2)
        if abs(number - round(number)) < 0.005:
            return "%.0f" % number
        return "%.2f" % number

    def fmt_count(value):
        return "%d" % int(value or 0)

    def make_card(row):
        qoder = row["platform"] == "Qoder"
        box = tk.Frame(body, bg=CARD, height=104 if qoder else 146)
        box.pack(fill="x", pady=(0, 10))
        box.pack_propagate(False)
        inner = tk.Frame(box, bg=CARD)
        inner.pack(fill="both", expand=True, padx=15, pady=12)

        top = tk.Frame(inner, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text=row.get("name") or "-", bg=CARD, fg=FG,
                 font=(FONT, 13, "bold"), anchor="w").pack(
            side="left", fill="x", expand=True)
        tk.Label(top, text="已签" if row.get("signed") else "待签",
                 bg=BADGE_BG if row.get("signed") else FIELD,
                 fg=BADGE_FG if row.get("signed") else MUTED,
                 font=(FONT, 8), padx=7, pady=1).pack(side="right")
        tk.Label(top, text=row["platform"], bg=CARD, fg=MUTED,
                 font=(FONT, 8)).pack(side="right", padx=(0, 8))

        if not row.get("ok"):
            tk.Label(inner, text=row.get("message") or "查询失败", bg=CARD,
                     fg=DANGER, font=(FONT, 8), wraplength=400,
                     justify="left").pack(anchor="w", pady=(10, 0))
            return

        primary = float(row.get("primary") or 0)
        if qoder:
            value_text, unit = fmt_count(primary), "个可领活动"
        elif row["platform"] == "WorkBuddy":
            value_text, unit = fmt_money(primary), "今日积分"
        else:
            value_text, unit = fmt_money(primary), "今日消耗"

        line = tk.Frame(inner, bg=CARD)
        line.pack(fill="x", pady=(5, 0))
        tk.Label(line, text=value_text, bg=CARD,
                 fg=ACCENT if primary > 0 else DIM,
                 font=(FONT, 21, "bold")).pack(side="left")
        tk.Label(line, text=unit, bg=CARD, fg=MUTED,
                 font=(FONT, 8)).pack(side="left", padx=(8, 8))
        tk.Frame(line, bg=CARD_LINE, width=1, height=16).pack(
            side="left", padx=(0, 8), pady=5)
        tk.Label(line, text=row.get("secondary") or "", bg=CARD, fg=FG,
                 font=(FONT, 8)).pack(side="left")

        if not qoder:
            balance, total = row.get("balance"), row.get("total")
            if balance is None:
                text = "余额  -"
            else:
                text = "余额  %s" % fmt_money(balance)
                if total is not None:
                    text += "   /   总额  %s" % fmt_money(total)
            tk.Label(inner, text=text, bg=CARD, fg=MUTED,
                     font=(FONT, 8)).pack(anchor="w", pady=(6, 0))
            if total and total > 0:
                ratio = max(0.0, min(1.0, float(balance or 0) / float(total)))
                track = tk.Frame(inner, bg=theme["TRACK"], height=5)
                track.pack(fill="x", pady=(8, 0))
                tk.Frame(track, bg=ACCENT, height=5).place(
                    x=0, y=0, relwidth=ratio, relheight=1)

    def update_scroll():
        if body.winfo_reqheight() > canvas.winfo_height() + 1:
            if not scrollbar.winfo_ismapped():
                scrollbar.pack(side="right", fill="y")
        elif scrollbar.winfo_ismapped():
            canvas.yview_moveto(0)
            scrollbar.pack_forget()

    def render(rows):
        for widget in body.winfo_children():
            widget.destroy()
        if not rows:
            box = tk.Frame(body, bg=CARD, height=104)
            box.pack(fill="x")
            box.pack_propagate(False)
            inner = tk.Frame(box, bg=CARD)
            inner.pack(fill="both", expand=True, padx=15, pady=14)
            tk.Label(inner, text="未发现本机客户端凭据", bg=CARD, fg=FG,
                     font=(FONT, 9, "bold")).pack(anchor="w")
            tk.Label(inner, text="请先登录 Trae / CodeBuddy / Qoder 客户端",
                     bg=CARD, fg=MUTED, font=(FONT, 8)).pack(anchor="w", pady=(4, 0))
        else:
            for row in rows:
                make_card(row)
        # 高度按卡片数自适应：头部 + 底部信息 + 卡片区；超过 660 交给滚动
        root.update_idletasks()
        needed = (body.winfo_reqheight() + head.winfo_reqheight()
                  + foot.winfo_reqheight() + 44)
        root.geometry("460x%d" % min(660, max(320, needed)))
        root.after(20, update_scroll)

    def update_summary(rows):
        today = datetime.now().strftime("%Y-%m-%d")
        store = checkin_core.CheckinStore(args.state_dir)
        total = len(rows)
        signed = sum(1 for row in rows if row.get("signed"))
        gained = store.today_credits(today)
        if not total:
            summary_label.configure(text="未发现账号", fg=MUTED)
            sign_btn.configure(state=tk.DISABLED, text="无账号", bg=BTN_MUTED,
                               fg=MUTED, disabledforeground=MUTED)
            return
        summary_label.configure(
            text="%d/%d 已签  ·  今日获得 %s 积分" % (
                signed, total, fmt_money(gained)),
            fg=FG)
        if signed >= total:
            # 今日全部账号已签：按钮禁用，避免重复领取
            sign_btn.configure(state=tk.DISABLED, text="今日已签 ✓", bg=BTN_MUTED,
                               fg=MUTED, disabledforeground=MUTED)
        else:
            sign_btn.configure(state=tk.NORMAL, text="立即签到",
                               bg=BADGE_BG, fg=BADGE_FG)

    def load_usage(force=False):
        if state["busy"]:
            return
        state["busy"] = True
        refresh_btn.configure(state=tk.DISABLED)
        if force:
            tip_label.configure(text="查询中…")

        def work():
            store = checkin_core.CheckinStore(args.state_dir)
            error = ""
            try:
                rows = platforms.usage_rows(store, force=force)
            except Exception as exc:
                rows, error = [], str(exc)
            post(lambda: done(rows, error))

        def done(rows, error):
            state["busy"] = False
            refresh_btn.configure(state=tk.NORMAL)
            render(rows)
            update_summary(rows)
            tip_label.configure(text=("查询失败：%s" % error) if error else
                                "更新于 " + datetime.now().strftime("%H:%M:%S"))

        threading.Thread(target=work, daemon=True).start()

    def do_checkin():
        if state["signing"]:
            return
        state["signing"] = True
        sign_btn.configure(state=tk.DISABLED, text="签到中…", bg=BTN_MUTED,
                           disabledforeground=MUTED)
        refresh_btn.configure(state=tk.DISABLED)

        def work():
            store = checkin_core.CheckinStore(args.state_dir)
            try:
                with process_lock(store.dir, timeout=10):
                    checkin_core.run_once(store, force=True, enabled=True,
                                          start_hour=effective_start_hour(args, store_dir),
                                          emit=lambda m: None)
            except Exception:
                pass
            post(done)

        def done():
            state["signing"] = False
            refresh_btn.configure(state=tk.NORMAL)
            load_usage(force=True)

        threading.Thread(target=work, daemon=True).start()

    def update_range_text():
        hour = configured_start_hour(store_dir)
        range_label.configure(text="点后  %02d:00–%02d:59 随机一分钟" % (hour, hour))

    def on_hour_change(*_):
        try:
            hour = max(0, min(23, int(float(hour_var.get()))))
        except (TypeError, ValueError):
            hour_var.set("%02d" % configured_start_hour(store_dir))
            return
        hour_var.set("%02d" % hour)
        save_settings(store_dir, {"start_hour": hour})
        if autostart_enabled():
            set_autostart(True, hour)   # 自启命令同步新的起始小时
        update_range_text()
        tip_label.configure(text="已保存：%d 点后随机一分钟自动签到" % hour)

    def update_autostart():
        enabled = autostart_enabled()
        auto_btn.configure(text="已开启（点击关闭）" if enabled else "未开启（点击开启）",
                           fg=ACCENT if enabled else FG)

    def toggle_autostart():
        set_autostart(not autostart_enabled(), configured_start_hour(store_dir))
        update_autostart()
        try:
            tray.update_menu()
        except Exception:
            pass

    def quit_app():
        try:
            tray.stop()
        except Exception:
            pass
        post(root.destroy)

    def switch_theme():
        """浅色 / 深色切换：记住选择，停掉托盘后重建窗口"""
        save_settings(store_dir, {
            "appearance": "light" if theme["dark"] else "dark"})
        restart["value"] = True
        try:
            tray.stop()
        except Exception:
            pass
        root.destroy()

    def on_wheel(event):
        if body.winfo_reqheight() <= canvas.winfo_height():
            return
        steps = int(-event.delta / 120)
        if steps == 0:
            steps = -1 if event.delta > 0 else 1
        canvas.yview_scroll(steps * 3, "units")

    body.bind("<Configure>", lambda event: canvas.configure(
        scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda event: (
        canvas.itemconfigure(body_id, width=event.width), update_scroll()))
    root.bind_all("<MouseWheel>", on_wheel)
    # 点窗口以外的空白处（切到其他程序）自动收起
    root.bind("<FocusOut>", on_focus_out)

    update_range_text()
    update_autostart()

    tray = build_tray(show=lambda: post(show_window),
                      checkin=lambda: post(do_checkin),
                      toggle_autostart=lambda: post(toggle_autostart),
                      quit_app=quit_app)
    if tray:
        try:
            tray.run_detached()
        except Exception:
            tray = None
    elif not show:
        # 托盘不可用时不能悄悄隐藏，否则进程会变成无窗口的后台僵尸
        show_window()

    # 关闭窗口只隐藏到托盘，真正退出走托盘菜单
    def on_close():
        if tray:
            hide_window()
        else:
            quit_app()

    root.protocol("WM_DELETE_WINDOW", on_close)
    pump()
    root.after(100, load_usage)
    # 双击后置前显示，避免窗口藏在浏览器/IDE 后面
    root.lift()
    root.attributes("-topmost", True)
    if show:
        root.focus_force()
    root.after(1200, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return "restart" if restart["value"] else 0


def loop(args):
    hide_console()
    interval = max(30, int(args.interval))
    while True:
        try:
            store = checkin_core.CheckinStore(args.state_dir)
            # 每轮重新读配置：界面上改了签到时间立即生效
            start_hour = effective_start_hour(args, args.state_dir)
            with process_lock(store.dir):
                checkin_core.run_once(
                    store, force=False, enabled=True, start_hour=start_hour,
                    emit=lambda m: None)   # 常驻模式不刷屏，记录在 state.json / checkin.log
        except TimeoutError:
            pass
        except Exception:
            try:
                with open(os.path.join(checkin_core.state_dir(), "error.log"),
                          "a", encoding="utf-8") as f:
                    f.write("%s\n%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                          traceback.format_exc()))
            except OSError:
                pass
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="AI 签到（Trae / WorkBuddy / Qoder 每日自动签到）")
    parser.add_argument("--silent", action="store_true",
                        help="托盘常驻 + 后台自动签到（不显示窗口）")
    parser.add_argument("--once", action="store_true", help="立即签到一次（已签账号仍跳过）")
    parser.add_argument("--status", action="store_true", help="查看今日状态与最近记录")
    parser.add_argument("--install", action="store_true", help="注册开机自启")
    parser.add_argument("--uninstall", action="store_true", help="取消开机自启")
    parser.add_argument("--start-hour", type=int, default=None,
                        help="每天起始小时（0-23；缺省读界面配置，未配置为 9）")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="常驻检查间隔秒数（默认 60）")
    parser.add_argument("--state-dir", default=None,
                        help="状态目录（默认 ~/.trae-checkin）")
    args = parser.parse_args(argv)

    if args.install:
        hour = effective_start_hour(args, args.state_dir)
        set_autostart(True, hour)
        print("已开启开机自启：%s" % launch_command(hour))
        return 0
    if args.uninstall:
        set_autostart(False)
        print("已取消开机自启")
        return 0

    store = checkin_core.CheckinStore(args.state_dir)
    if args.status:
        print_status(store)
        return 0
    if args.once:
        with process_lock(store.dir):
            results = checkin_core.run_once(
                store, force=True, enabled=True,
                start_hour=effective_start_hour(args, args.state_dir))
        if not results:
            print("没有可签到的账号")
        return 0
    if args.silent:
        run_ui(args, show=False)
        return 0
    run_ui(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
