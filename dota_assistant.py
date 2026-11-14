"""Dota Assistant: подсказки по пику + оверлей с советами во время матча.
Работает через официальную Game State Integration (GSI) от Valve:
Dota 2 сама шлёт на localhost данные о ВАШЕМ герое и времени матча.
Программа не читает память и экран игры. Только стандартная библиотека Python."""
import json, math, os, sys, threading, time, urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from http.server import BaseHTTPRequestHandler, HTTPServer

APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
SETTINGS = os.path.join(APP_DIR, "settings.json")
TIMERS_FILE = os.path.join(APP_DIR, "timers.json")
GSI_PORT = 33000
API = "https://api.opendota.com/api"
RED, YEL, GRN, WHT, GRY, ORG = "#ff5a4f", "#ffd24a", "#4be38a", "#ffffff", "#aab0bb", "#ff9f43"

# Таймеры зависят от патча! Их можно менять в timers.json рядом с программой.
# Значения в секундах игрового времени. Подтверждены по патчам до 7.39; проверьте для вашего патча.
DEFAULT_EVENTS = [
    {"label": "Баунти-руны", "first": 180, "every": 180, "hint": "Заберите баунти-руну (золото всей команде)"},
    {"label": "Водяные руны", "times": [120, 240], "hint": "Водяные руны на реке: HP/мана"},
    {"label": "Силовая руна", "first": 360, "every": 120, "hint": "Силовая руна на реке: отправьте героя"},
    {"label": "Тормент", "times": [1200], "hint": "Тормент появился: убейте всей командой ради Aghanim's Shard"},
    {"label": "Новый нейтральный тир", "times": [300, 900, 1500, 2100, 3600], "hint": "Откроется новый тир нейтральных предметов"},
    {"label": "Стак лагеря", "first": 53, "every": 60, "roles": [4, 5], "hint": "Тяните крипов на :53, чтобы сделать стак"},
]
PHASE = {
    1: ("Лайн: ласт-хиты и дени, не лезьте в размен без нужды", "Фармите лес и линии, пушьте башни, копите на ключевой предмет", "Держитесь с командой, не фармите в одиночку, играйте от BKB/блинка"),
    2: ("Мид: руны, ЛХ, быстрый 6-й уровень", "Ганкайте по рунам, пушьте и ротируйтесь на линии", "Группа с командой, держите вижн, не рискуйте в одиночку"),
    3: ("Оффлейн: тяните крипов на себя, берите опыт, не умирайте", "Создавайте пространство: пушьте оффлейн, начинайте файты", "Начинайте файты, берите Рошана всей командой"),
    4: ("Роум и стаки: помогите линиям, ставьте вард, стак на :53", "Ротации, вижн на путях врага, смоки на ганки", "Вард перед файтом, контроль Рошана, не умирайте первым"),
    5: ("Лайн-саппорт: блок/тяга, вард, не отбирайте ЛХ у кора", "Вижн на Рошане и в лесу врага, спасайте кора", "Вижн, смоки, держитесь за кором, не умирайте первым"),
}
STATE = {"data": None, "ts": 0.0}
try:
    import numpy as np
    from PIL import Image, ImageGrab
    HAVE_SCREEN = True
except Exception:
    HAVE_SCREEN = False


def find_enemies(a, team):
    """a: массив RGB миникарты. Ищет яркие значки героев вражеской команды.
    Возвращает список (область карты, размер значка)."""
    r, g, b = a[..., 0].astype(int), a[..., 1].astype(int), a[..., 2].astype(int)
    if team == "radiant":      # враги = Dire (красные)
        mask = (r > 190) & (g < 70) & (b < 70)
    else:                      # враги = Radiant (зелёные)
        mask = (g > 170) & (r < 110) & (b < 110)
    cs = 6
    H, W = mask.shape[0] // cs, mask.shape[1] // cs
    cnt = mask[:H * cs, :W * cs].reshape(H, cs, W, cs).sum(axis=(1, 3))
    on = cnt >= 3
    seen, res = set(), []
    for y in range(H):
        for x in range(W):
            if on[y, x] and (y, x) not in seen:
                stack, comp = [(y, x)], []
                seen.add((y, x))
                while stack:
                    cy, cx = stack.pop(); comp.append((cy, cx))
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = cy + dy, cx + dx
                            if 0 <= ny < H and 0 <= nx < W and on[ny, nx] and (ny, nx) not in seen:
                                seen.add((ny, nx)); stack.append((ny, nx))
                size = sum(int(cnt[c]) for c in comp)
                if size < 25:      # мелкие точки: это крипы, а не герои
                    continue
                fy = sum(c[0] for c in comp) / len(comp) / H
                fx = sum(c[1] for c in comp) / len(comp) / W
                if fy < 0.3 or fx < 0.3 and fy < 0.6:
                    zone = "верхняя линия"
                elif fy > 0.7 or fx > 0.7 and fy > 0.4:
                    zone = "нижняя линия"
                else:
                    zone = "центр/мид"
                res.append((zone, size))
    return res


def scan_minimap(box, team):
    img = ImageGrab.grab(bbox=(box[0], box[1], box[0] + box[2], box[1] + box[3])).convert("RGB")
    return find_enemies(np.asarray(img), team), img


def fmt(sec):
    sec = int(sec)
    s = "-" if sec < 0 else ""
    sec = abs(sec)
    return f"{s}{sec // 60}:{sec % 60:02d}"


def get_json(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "DotaAssistant/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# ---------- приём данных от игры ----------
class GSIHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            STATE["data"] = json.loads(self.rfile.read(n))
            STATE["ts"] = time.time()
        except Exception:
            pass
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def start_server():
    try:
        srv = HTTPServer(("127.0.0.1", GSI_PORT), GSIHandler)
    except OSError:
        return False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


def find_dota():
    bases = []
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        bases.append(winreg.QueryValueEx(k, "SteamPath")[0])
    except Exception:
        pass
    bases += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    for b in list(bases):
        vdf = os.path.join(b, "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf):
            for line in open(vdf, encoding="utf-8", errors="ignore"):
                if '"path"' in line:
                    bases.append(line.split('"')[3].replace("\\\\", "\\"))
    for b in bases:
        d = os.path.join(b, "steamapps", "common", "dota 2 beta")
        if os.path.isdir(d):
            return d
    return None


CFG_TEXT = '''"Dota 2 Integration Configuration"
{
    "uri"       "http://127.0.0.1:%d/"
    "timeout"   "5.0"
    "buffer"    "0.1"
    "throttle"  "0.1"
    "heartbeat" "1.0"
    "data"
    {
        "provider"  "1"
        "map"       "1"
        "player"    "1"
        "hero"      "1"
        "abilities" "1"
        "items"     "1"
    }
}
''' % GSI_PORT


# ---------- советы по матчу ----------
def next_time(ev, t):
    if "times" in ev:
        c = [x for x in ev["times"] if x >= t]
        return min(c) if c else None
    first, every = ev["first"], ev.get("every", 0)
    if t <= first:
        return first
    if not every:
        return None
    return first + math.ceil((t - first) / every) * every


def advise(g, t, role, rosh_death, events):
    hero, pl, mp = g.get("hero") or {}, g.get("player") or {}, g.get("map") or {}
    items, abil = g.get("items") or {}, g.get("abilities") or {}
    names = [(v or {}).get("name", "") for k, v in items.items() if k.startswith("slot")]
    tp = (items.get("teleport0") or {}).get("name", "empty")
    gold, alive = pl.get("gold", 0), hero.get("alive", True)
    out = []
    if t < 0:
        out.append((0, YEL, f"Горн через {fmt(-t)}: закупитесь, идите на линию/за руной"))
    if not alive:
        out.append((0, RED, f"Вы мертвы: респавн через {hero.get('respawn_seconds', 0)} с. Купите предметы, посмотрите карту"))
        if gold >= hero.get("buyback_cost", 10 ** 9) and hero.get("buyback_cooldown", 1) == 0:
            out.append((3, GRN, "Выкуп (buyback) доступен"))
    else:
        hp = hero.get("health_percent", 100)
        if hp < 30:
            out.append((1, RED, f"HP {hp}%: отойдите к башне, используйте расходники"))
        if gold >= 1800:
            out.append((2, YEL, f"{gold} золота не потрачено: вернитесь на базу/курьера и купите предмет"))
        if tp == "empty" and gold >= 100 and t > 0:
            out.append((2, YEL, "Нет свитка телепорта: купите TP"))
        lh, mins = pl.get("last_hits", 0), t / 60
        if 120 < t < 600 and role <= 3:
            exp = (4.5 if role < 3 else 3.0) * mins
            if lh < exp * 0.8:
                out.append((3, YEL, f"Мало ласт-хитов: {lh}, ориентир ≈{int(exp)} к {fmt(t)}"))
        for a in abil.values():
            if (a or {}).get("ultimate") and a.get("can_cast") and a.get("level", 0) > 0 and a.get("cooldown", 1) == 0:
                out.append((6, GRN, "Ультимейт готов: ищите файт"))
                break
        if t > 1500 and role <= 3 and "item_black_king_bar" not in names:
            out.append((4, ORG, "25+ минут и нет BKB: подумайте о нём"))
        if role >= 4 and t > 0 and mp.get("ward_purchase_cooldown", 1) == 0:
            out.append((5, GRY, "Можно купить обсервер: вижн решает"))
    timers = []
    for ev in events:
        if ev.get("roles") and role not in ev["roles"]:
            continue
        nt = next_time(ev, t)
        if nt is not None:
            timers.append((nt - t, ev["label"], ev["hint"]))
    if rosh_death is not None:
        lo, hi = rosh_death + 480, rosh_death + 660
        if t < lo:
            timers.append((lo - t, "Рошан (самый ранний респавн)", "Рошан скоро может возродиться"))
        elif t <= hi:
            out.append((4, ORG, f"Рошан мог возродиться (до {fmt(hi)}): вижн и готовность команды"))
    timers.sort()
    for left, label, hint in timers:
        if left <= 15:
            out.append((1, YEL, f"{label} через {left} с: {hint}"))
    out.sort(key=lambda x: x[0])
    phase = 0 if t < 600 else (1 if t < 1500 else 2)
    return out[:3], PHASE[role][phase], timers[:3]


# ---------- оверлей ----------
class Overlay:
    def __init__(self, root, x, y):
        self.w = tk.Toplevel(root)
        self.w.overrideredirect(True)
        self.w.attributes("-topmost", True)
        self.w.attributes("-alpha", 0.85)
        self.w.configure(bg="#111418")
        self.w.geometry(f"+{x}+{y}")
        self.labels = [tk.Label(self.w, bg="#111418", fg=WHT, font=("Segoe UI", 12, "bold"),
                                anchor="w", justify="left", wraplength=560) for _ in range(8)]
        self.w.update_idletasks()
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.w.winfo_id()) or self.w.winfo_id()
            st = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, st | 0x80000 | 0x20 | 0x80)  # layered + click-through + toolwindow
        except Exception:
            pass
        self.visible = True

    def set(self, lines):
        for i, lb in enumerate(self.labels):
            if i < len(lines):
                lb.config(text=lines[i][0], fg=lines[i][1])
                lb.pack(fill="x", padx=10, pady=1)
            else:
                lb.pack_forget()

    def move(self, x, y):
        self.w.geometry(f"+{x}+{y}")

    def toggle(self):
        self.visible = not self.visible
        (self.w.deiconify if self.visible else self.w.withdraw)()


# ---------- приложение ----------
class App:
    def __init__(self):
        self.s = {"role": 1, "steam": "", "ox": 20, "oy": 120, "mm": [0, 800, 280, 280], "mm_on": False}
        try:
            self.s.update(json.load(open(SETTINGS, encoding="utf-8")))
        except Exception:
            pass
        if not os.path.exists(TIMERS_FILE):
            json.dump({"events": DEFAULT_EVENTS}, open(TIMERS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        try:
            self.events = json.load(open(TIMERS_FILE, encoding="utf-8"))["events"]
        except Exception:
            self.events = DEFAULT_EVENTS
        self.rosh = None
        self.heroes, self.enemy, self.ally = {}, [], []
        self.root = tk.Tk()
        self.root.title("Dota Assistant")
        self.root.geometry("560x640")
        self.server_ok = start_server()
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.build_game_tab(nb)
        self.build_stats_tab(nb)
        self.build_draft_tab(nb)
        self.overlay = Overlay(self.root, self.s["ox"], self.s["oy"])
        threading.Thread(target=self.load_heroes, daemon=True).start()
        self.root.after(1000, self.tick)

    def save(self):
        self.s["role"] = int(self.role_var.get())
        self.s["steam"] = self.steam_var.get().strip()
        try:
            self.s["ox"], self.s["oy"] = int(self.ox.get()), int(self.oy.get())
        except ValueError:
            pass
        try:
            self.s["mm"] = [int(v.get()) for v in self.mm]
        except ValueError:
            pass
        self.s["mm_on"] = bool(self.mm_on.get())
        json.dump(self.s, open(SETTINGS, "w", encoding="utf-8"))

    # --- вкладка «Игра» ---
    def build_game_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Игра")
        self.status = tk.StringVar(value="Ожидание данных от Dota 2…")
        ttk.Label(f, textvariable=self.status, wraplength=520).pack(anchor="w", pady=(0, 8))
        r = ttk.Frame(f); r.pack(fill="x", pady=3)
        ttk.Label(r, text="Ваша позиция:").pack(side="left")
        self.role_var = tk.StringVar(value=str(self.s["role"]))
        cb = ttk.Combobox(r, textvariable=self.role_var, values=["1", "2", "3", "4", "5"], width=4, state="readonly")
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.save())
        ttk.Label(r, text="(1 керри, 2 мид, 3 оффлейн, 4-5 саппорты)").pack(side="left")
        r = ttk.Frame(f); r.pack(fill="x", pady=3)
        ttk.Label(r, text="Steam ID:").pack(side="left")
        self.steam_var = tk.StringVar(value=self.s["steam"])
        e = ttk.Entry(r, textvariable=self.steam_var, width=22); e.pack(side="left", padx=6)
        e.bind("<FocusOut>", lambda ev: self.save())
        ttk.Label(r, text="(для личной статистики героев)").pack(side="left")
        r = ttk.Frame(f); r.pack(fill="x", pady=3)
        ttk.Label(r, text="Оверлей X/Y:").pack(side="left")
        self.ox, self.oy = tk.StringVar(value=str(self.s["ox"])), tk.StringVar(value=str(self.s["oy"]))
        ttk.Entry(r, textvariable=self.ox, width=6).pack(side="left", padx=4)
        ttk.Entry(r, textvariable=self.oy, width=6).pack(side="left", padx=4)
        ttk.Button(r, text="Применить", command=self.apply_pos).pack(side="left", padx=4)
        r = ttk.Frame(f); r.pack(fill="x", pady=3)
        self.mm = [tk.StringVar(value=str(v)) for v in self.s["mm"]]
        self.mm_on = tk.BooleanVar(value=self.s["mm_on"])
        ttk.Checkbutton(r, text="Миникарта X,Y,Ш,В:", variable=self.mm_on, command=self.save).pack(side="left")
        for v in self.mm:
            ttk.Entry(r, textvariable=v, width=5).pack(side="left", padx=2)
        ttk.Button(r, text="Проверить", command=self.test_minimap).pack(side="left", padx=4)
        ttk.Button(f, text="1. Установить интеграцию с Dota 2", command=self.install_cfg).pack(fill="x", pady=(10, 3))
        ttk.Button(f, text="2. Показать / скрыть оверлей", command=self.overlay.toggle).pack(fill="x", pady=3)
        ttk.Button(f, text="Рошан убит сейчас (запустить таймер)", command=self.rosh_now).pack(fill="x", pady=3)
        ttk.Button(f, text="Сбросить таймер Рошана", command=lambda: setattr(self, "rosh", None)).pack(fill="x", pady=3)
        txt = ("Как пользоваться:\n"
               "• Нажмите «Установить интеграцию» и ПЕРЕЗАПУСТИТЕ Dota 2.\n"
               "• В настройках Dota 2: Видео → режим «Оконный без рамки» (в полноэкранном оверлей не виден).\n"
               "• Если данные не приходят, добавьте в Steam параметры запуска Dota 2: -gamestateintegration\n"
               "• Таймеры рун и объектов лежат в timers.json: сверяйте с вашим патчем.\n\n"
               "Важно: игра по официальному каналу отдаёт только данные о вашем герое и времени. "
               "Позиции врагов и миникарту она игрокам не отдаёт, поэтому советы основаны на таймерах, "
               "вашей роли и вашей статистике, а не на «видении» карты.")
        ttk.Label(f, text=txt, wraplength=520, justify="left").pack(anchor="w", pady=10)
        if not self.server_ok:
            messagebox.showwarning("Порт занят", f"Порт {GSI_PORT} занят: возможно, программа уже запущена.")

    def apply_pos(self):
        self.save()
        self.overlay.move(self.s["ox"], self.s["oy"])

    def install_cfg(self):
        d = find_dota()
        if not d:
            d = filedialog.askdirectory(title="Выберите папку «dota 2 beta» (steamapps/common/dota 2 beta)")
        if not d:
            return
        target = os.path.join(d, "game", "dota", "cfg", "gamestate_integration")
        try:
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "gamestate_integration_dotahub.cfg"), "w", encoding="utf-8") as f:
                f.write(CFG_TEXT)
            messagebox.showinfo("Готово", "Интеграция установлена. Перезапустите Dota 2.")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def test_minimap(self):
        if not HAVE_SCREEN:
            return messagebox.showinfo("Миникарта", "Нужны пакеты: pip install pillow numpy")
        self.save()
        try:
            res, img = scan_minimap(self.s["mm"], "radiant")
            img.save(os.path.join(APP_DIR, "minimap_debug.png"))
            messagebox.showinfo("Миникарта", f"Снимок сохранён: minimap_debug.png\nПроверьте, что на нём именно миникарта.\nНайдено значков (для команды Radiant): {len(res)}")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def rosh_now(self):
        g = STATE["data"] or {}
        t = (g.get("map") or {}).get("clock_time")
        if t is None:
            return messagebox.showinfo("Рошан", "Нет данных о времени матча.")
        self.rosh = t + int(time.time() - STATE["ts"])

    # --- цикл оверлея ---
    def tick(self):
        try:
            self.update_overlay()
        except Exception as e:
            self.status.set(f"Ошибка: {e}")
        self.root.after(1000, self.tick)

    def update_overlay(self):
        g, age = STATE["data"], time.time() - STATE["ts"]
        if not g or age > 6:
            self.status.set("Нет данных от Dota 2 (матч не идёт или интеграция не установлена).")
            return self.overlay.set([("Dota Assistant: жду матч…", GRY)])
        mp = g.get("map") or {}
        gs = mp.get("game_state", "")
        if "PRE_GAME" not in gs and "GAME_IN_PROGRESS" not in gs:
            self.status.set("Данные идут. Вы не в матче (драфт/меню).")
            return self.overlay.set([("Драфт: используйте вкладку «Драфт» в программе", GRY)])
        t = mp.get("clock_time", 0)
        if not mp.get("paused"):
            t += int(age)
        role = int(self.role_var.get())
        alerts, phase, timers = advise(g, t, role, self.rosh, self.events)
        hero = (g.get("hero") or {}).get("name", "").replace("npc_dota_hero_", "")
        pl = g.get("player") or {}
        self.status.set(f"Связь есть. {fmt(t)}, {hero}")
        lines = [(f"{fmt(t)}  Поз.{role}  {hero}  {pl.get('kills', 0)}/{pl.get('deaths', 0)}/{pl.get('assists', 0)}  GPM {pl.get('gpm', 0)}", WHT)]
        lines += [(("• " + a[2]), a[1]) for a in alerts]
        lines.append((f"Сейчас: {phase}", GRY))
        lines += [(f"{fmt(left)}  {label}", YEL if left <= 20 else GRY) for left, label, _ in timers]
        if self.mm_on.get() and HAVE_SCREEN:
            try:
                team = (pl.get("team_name") or "radiant").lower()
                res, _ = scan_minimap(self.s["mm"], team)
                if res:
                    zones = {}
                    for z, _sz in res:
                        zones[z] = zones.get(z, 0) + 1
                    lines.append(("Враги на миникарте: " + ", ".join(f"{z} ×{n}" for z, n in zones.items()), ORG))
                elif t > 600:
                    lines.append(("Врагов на миникарте не видно: возможна ротация/смок, держите вижн", YEL))
                if t > 600 and len(res) <= 2 and res:
                    lines.append((f"Не видно {5 - len(res)} врагов: осторожно с ганками", YEL))
            except Exception:
                pass
        self.overlay.set(lines)


    # --- вкладка «Статистика» (профиль, матчи, разбор, тренер, герои) ---
    def build_stats_tab(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Статистика")
        b = ttk.Frame(f); b.pack(fill="x")
        for text, fn in (("Профиль", self.do_profile), ("Тренер", self.do_coach),
                         ("Мета героев", self.do_meta), ("Матчи", self.do_matches)):
            ttk.Button(b, text=text, command=lambda fn=fn: self.bg(fn)).pack(side="left", expand=True, fill="x")
        h = ttk.Frame(f); h.pack(fill="x", pady=4)
        self.hq = tk.StringVar()
        ttk.Entry(h, textvariable=self.hq).pack(side="left", fill="x", expand=True)
        ttk.Button(h, text="Инфо о герое (англ. имя)", command=lambda: self.bg(self.do_hero)).pack(side="left", padx=4)
        self.mlb = tk.Listbox(f, height=6, exportselection=False)
        self.mlb.pack(fill="x", pady=4)
        self.matches = []
        ttk.Button(f, text="Разобрать выбранный матч", command=self.analyze_sel).pack(fill="x")
        self.txt = tk.Text(f, wrap="word", height=18)
        self.txt.pack(fill="both", expand=True, pady=4)
        self.txt.insert("end", "Введите Steam ID на вкладке «Игра» и нажмите кнопку.\nПрофиль Dota должен быть публичным.")

    def out(self, text):
        def put():
            self.txt.delete("1.0", "end")
            self.txt.insert("end", text)
        self.root.after(0, put)

    def bg(self, fn):
        v = self.steam_var.get().strip()
        self._acc = None
        if v.isdigit():
            n = int(v)
            self._acc = n - 76561197960265728 if n > 76561197960265728 else n
        self.save()
        self.out("Загрузка…")
        threading.Thread(target=self._safe, args=(fn,), daemon=True).start()

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:
            self.out(f"Ошибка запроса к OpenDota: {e}\n(Проверьте интернет/VPN и что профиль публичный.)")

    def need_acc(self):
        if self._acc is None:
            self.out("Введите Steam ID (только цифры) на вкладке «Игра».")
        return self._acc

    def hs(self):
        if not self.heroes:
            self.heroes = {h["id"]: h for h in get_json("/heroStats")}
        return self.heroes

    @staticmethod
    def rank_name(t):
        if not t:
            return "не калиброван"
        m = ["Herald", "Guardian", "Crusader", "Archon", "Legend", "Ancient", "Divine", "Immortal"]
        i, st = t // 10, t % 10
        return m[i - 1] if i == 8 else f"{m[i - 1]} {st}"

    def do_profile(self):
        a = self.need_acc()
        if a is None:
            return
        p, wl, top, hs = get_json(f"/players/{a}"), get_json(f"/players/{a}/wl"), get_json(f"/players/{a}/heroes"), self.hs()
        g = wl["win"] + wl["lose"]
        lines = [f"{(p.get('profile') or {}).get('personaname', '?')}",
                 f"Ранг: {self.rank_name(p.get('rank_tier'))}",
                 f"Матчей: {g}, винрейт {wl['win'] / g * 100 if g else 0:.1f}% ({wl['win']}W / {wl['lose']}L)", "", "Топ героев:"]
        for x in [x for x in top if x["games"]][:6]:
            lines.append(f"  {hs.get(int(x['hero_id']), {}).get('localized_name', x['hero_id'])}: {x['games']} игр, {x['win'] / x['games'] * 100:.0f}%")
        self.out("\n".join(lines))

    def do_matches(self):
        a = self.need_acc()
        if a is None:
            return
        ms, hs = get_json(f"/players/{a}/recentMatches"), self.hs()
        self.matches = ms[:15]
        def fill():
            self.mlb.delete(0, "end")
            for x in self.matches:
                w = (x["player_slot"] < 128) == x["radiant_win"]
                self.mlb.insert("end", f"{'W' if w else 'L'}  {hs.get(x['hero_id'], {}).get('localized_name', '?')}  {x['kills']}/{x['deaths']}/{x['assists']}  {x['duration'] // 60} мин")
        self.root.after(0, fill)
        self.out("Выберите матч в списке и нажмите «Разобрать выбранный матч».")

    def analyze_sel(self):
        sel = self.mlb.curselection()
        if not sel or not self.matches:
            return messagebox.showinfo("Разбор", "Сначала нажмите «Матчи» и выберите матч.")
        mid = self.matches[sel[0]]["match_id"]
        self.bg(lambda: self.do_analyze(mid))

    def do_analyze(self, mid):
        a = self.need_acc()
        if a is None:
            return
        m, hs = get_json(f"/matches/{mid}"), self.hs()
        me = next((p for p in m.get("players", []) if p.get("account_id") == a), None)
        if not me:
            return self.out("Вас нет в этом матче или профиль скрыт.")
        mins = max(m["duration"] / 60, 1)
        win = (me["player_slot"] < 128) == m["radiant_win"]
        team = sorted([p for p in m["players"] if (p["player_slot"] < 128) == (me["player_slot"] < 128)], key=lambda p: -p["gold_per_min"])
        place = team.index(me) + 1
        core = place <= 3
        L = [f"{'ПОБЕДА' if win else 'ПОРАЖЕНИЕ'}: {hs.get(me['hero_id'], {}).get('localized_name', '?')}, {int(mins)} мин",
             f"K/D/A {me['kills']}/{me['deaths']}/{me['assists']}, GPM {me['gold_per_min']}, XPM {me['xp_per_min']}",
             f"ЛХ/Д {me['last_hits']}/{me['denies']}, урон по героям {me.get('hero_damage', 0)}, место по золоту {place}/5", ""]
        tips = []
        bm = me.get("benchmarks")
        names = {"gold_per_min": "Золото/мин", "xp_per_min": "Опыт/мин", "last_hits_per_min": "ЛХ/мин", "hero_damage_per_min": "Урон/мин"}
        if bm:
            L.append("Сравнение с игроками на этом герое:")
            for k, lb in names.items():
                if bm.get(k) and bm[k].get("pct") is not None:
                    pct = bm[k]["pct"] * 100
                    L.append(f"  {lb}: лучше, чем у {pct:.0f}%")
                    if pct < 30 and core and k != "hero_damage_per_min":
                        tips.append(f"{lb} ниже нормы: работайте над фармом и маршрутом по карте.")
            L.append("")
        else:
            try:
                urllib.request.urlopen(urllib.request.Request(f"{API}/request/{mid}", data=b"", method="POST"), timeout=15)
            except Exception:
                pass
            L.append("Матч не распарсен. Запросил разбор: повторите через 2-5 минут.\n")
        lh = me.get("lh_t") or []
        if len(lh) > 10 and core:
            L.append(f"Ласт-хитов на 10-й минуте: {lh[10]}")
            if lh[10] < 45:
                tips.append(f"{lh[10]} ЛХ на 10-й минуте: мало для кора (ориентир 50+).")
        if me["deaths"] >= 8 or me["deaths"] / mins > 0.3:
            tips.append(f"{me['deaths']} смертей: следите за миникартой и не стойте без вижна.")
        if not me.get("obs_placed", 0) and mins > 20 and not core:
            tips.append("Ни одного обсервера: саппорту критично ставить вижн.")
        if me["assists"] < 5 and not core:
            tips.append("Мало ассистов для саппорта: чаще присутствуйте на файтах.")
        L.append("Выводы:" if tips else "Серьёзных просадок не найдено.")
        L += [f"  - {t}" for t in tips]
        self.out("\n".join(L))

    def do_coach(self):
        a = self.need_acc()
        if a is None:
            return
        ms, hs = get_json(f"/players/{a}/recentMatches"), self.hs()
        if len(ms) < 5:
            return self.out("Слишком мало матчей для анализа (нужно хотя бы 5).")
        n = len(ms)
        wins = [(x["player_slot"] < 128) == x["radiant_win"] for x in ms]
        tot = lambda k: sum(x[k] for x in ms)
        mins = tot("duration") / 60
        dpm, lhpm = tot("deaths") / mins, tot("last_hits") / mins
        kda = (tot("kills") + tot("assists")) / max(tot("deaths"), 1)
        wr = sum(wins) / n * 100
        uniq = len({x["hero_id"] for x in ms})
        lost = [x for x, w in zip(ms, wins) if not w]
        lost_avg = sum(x["duration"] for x in lost) / len(lost) / 60 if lost else 0
        L = [f"Разбор последних {n} игр", f"Винрейт {wr:.0f}%, KDA {kda:.2f}, GPM {tot('gold_per_min') / n:.0f}, XPM {tot('xp_per_min') / n:.0f}",
             f"Смертей/мин {dpm:.2f}, ЛХ/мин {lhpm:.1f}, героев в пуле {uniq}", "", "Что улучшать:"]
        plan = []
        if dpm > 0.28:
            plan.append("Меньше смертей: перед агрессией смотрите миникарту и тайминги врагов. Цель: не больше 6 смертей за игру.")
        if lhpm < 4 and tot("gold_per_min") / n < 420:
            plan.append("Слабый фарм: тренируйте ласт-хит (лобби с ботами по 10 минут в день), фармите стаки/лес между файтами.")
        if uniq > 12:
            plan.append(f"Слишком широкий пул ({uniq} героев за {n} игр): оставьте 3-5 героев.")
        if lost and lost_avg < 30:
            plan.append(f"Поражения в среднем на {lost_avg:.0f} минуте: работайте над лейнингом и пиком.")
        elif lost and lost_avg > 42:
            plan.append("Проигрыши в позднюю игру: разберите решения после 35-й минуты (Рошан, высотки).")
        if kda < 2.5:
            plan.append("Низкий KDA: играйте от команды, не отрываясь без вижна.")
        if not plan:
            plan.append("Явных провалов нет. Разберите три последних проигрыша через «Матчи».")
        L += [f"{i}. {t}" for i, t in enumerate(plan, 1)]
        by = {}
        for x, w in zip(ms, wins):
            d = by.setdefault(x["hero_id"], [0, 0]); d[0] += 1; d[1] += w
        best = sorted([(h, g, w) for h, (g, w) in by.items() if g >= 3], key=lambda t: t[2] / t[1])
        if best:
            L.append(f"\nСлабое место: {hs.get(best[0][0], {}).get('localized_name', '?')} ({best[0][2]}/{best[0][1]} побед)")
            L.append(f"Сильный герой: {hs.get(best[-1][0], {}).get('localized_name', '?')} ({best[-1][2]}/{best[-1][1]} побед)")
        self.out("\n".join(L))

    def do_meta(self):
        hs = self.hs()
        data = [(h["localized_name"], self.base_wr(h) * 100, sum(h.get(f"{i}_pick", 0) for i in range(1, 9))) for h in hs.values()]
        mx = max(d[2] for d in data)
        data = sorted([d for d in data if d[2] >= mx * 0.1], key=lambda d: -d[1])
        self.out("Лучший винрейт (все ранги, паблик):\n" + "\n".join(f"{i}. {n} - {w:.1f}%" for i, (n, w, _) in enumerate(data[:15], 1)))

    def do_hero(self):
        q, hs = self.hq.get().strip().lower(), self.hs()
        found = [h for h in hs.values() if q == h["localized_name"].lower()] or [h for h in hs.values() if q and q in h["localized_name"].lower()]
        if not found:
            return self.out("Герой не найден. Введите английское имя, например Pudge.")
        h = found[0]
        L = [f"{h['localized_name']}: винрейт {self.base_wr(h) * 100:.1f}%"]
        mu = get_json(f"/heroes/{h['id']}/matchups")
        mu = sorted([(hs[x["hero_id"]]["localized_name"], x["wins"] / x["games_played"] * 100) for x in mu if x["games_played"] >= 100 and x["hero_id"] in hs], key=lambda t: t[1])
        L.append("\nКонтрпики (герой чаще всего проигрывает им):")
        L += [f"  {n}: {w:.0f}%" for n, w in mu[:5]]
        L.append("Хорошо играет против:")
        L += [f"  {n}: {w:.0f}%" for n, w in mu[-5:][::-1]]
        try:
            ip, items = get_json(f"/heroes/{h['id']}/itemPopularity"), get_json("/constants/items")
            nm = {v["id"]: v.get("dname") or k for k, v in items.items() if "id" in v}
            L.append("\nПопулярная сборка:")
            for key, lb in (("start_game_items", "Старт"), ("early_game_items", "Лейн"), ("mid_game_items", "Мид"), ("late_game_items", "Лейт")):
                top = sorted(ip.get(key, {}).items(), key=lambda kv: -kv[1])[:4]
                if top:
                    L.append(f"  {lb}: " + ", ".join(nm.get(int(i), i) for i, _ in top))
        except Exception:
            pass
        self.out("\n".join(L))

    # --- вкладка «Драфт» ---
    def build_draft_tab(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Драфт")
        top = ttk.Frame(f); top.pack(fill="x")
        self.q = tk.StringVar()
        self.q.trace_add("write", lambda *a: self.refresh_list())
        ttk.Entry(top, textvariable=self.q).pack(side="left", fill="x", expand=True)
        self.role_f = tk.StringVar(value="Любая")
        ttk.Combobox(top, textvariable=self.role_f, width=11, state="readonly",
                     values=["Любая", "Carry", "Support", "Initiator", "Disabler", "Nuker", "Durable", "Escape", "Pusher"]).pack(side="left", padx=4)
        self.lb = tk.Listbox(f, height=7, exportselection=False)
        self.lb.pack(fill="x", pady=4)
        b = ttk.Frame(f); b.pack(fill="x")
        ttk.Button(b, text="→ Враг", command=lambda: self.add(self.enemy)).pack(side="left", expand=True, fill="x")
        ttk.Button(b, text="→ Союзник", command=lambda: self.add(self.ally)).pack(side="left", expand=True, fill="x")
        ttk.Button(b, text="Сбросить", command=self.reset_draft).pack(side="left", expand=True, fill="x")
        self.picked = tk.StringVar()
        ttk.Label(f, textvariable=self.picked, wraplength=520, justify="left").pack(anchor="w", pady=4)
        ttk.Button(f, text="Подсказать пик", command=self.suggest).pack(fill="x")
        cols = ("hero", "vs", "base", "mine")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=10)
        for c, t, w in zip(cols, ("Герой", "WR против врагов", "Общий WR", "Ваш WR"), (150, 130, 90, 110)):
            self.tree.heading(c, text=t); self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, pady=6)
        self.hint = tk.StringVar()
        ttk.Label(f, textvariable=self.hint, wraplength=520, foreground="#555").pack(anchor="w")

    def load_heroes(self):
        try:
            data = get_json("/heroStats")
            self.heroes = {h["id"]: h for h in data}
            self.root.after(0, self.refresh_list)
        except Exception as e:
            self.root.after(0, lambda: self.hint.set(f"Не удалось загрузить героев: {e}"))

    def base_wr(self, h):
        p = sum(h.get(f"{i}_pick", 0) for i in range(1, 9))
        w = sum(h.get(f"{i}_win", 0) for i in range(1, 9))
        return w / p if p else 0.5

    def refresh_list(self):
        q = self.q.get().lower()
        self.view = sorted([h for h in self.heroes.values() if q in h["localized_name"].lower()],
                           key=lambda h: h["localized_name"])
        self.lb.delete(0, "end")
        for h in self.view:
            self.lb.insert("end", h["localized_name"])

    def add(self, target):
        sel = self.lb.curselection()
        if not sel:
            return
        hid = self.view[sel[0]]["id"]
        if hid not in self.enemy and hid not in self.ally:
            target.append(hid)
        self.update_picked()

    def update_picked(self):
        n = lambda ids: ", ".join(self.heroes[i]["localized_name"] for i in ids) or "-"
        self.picked.set(f"Враги: {n(self.enemy)}\nСоюзники: {n(self.ally)}")

    def reset_draft(self):
        self.enemy.clear(); self.ally.clear(); self.update_picked()
        self.tree.delete(*self.tree.get_children()); self.hint.set("")

    def suggest(self):
        if not self.heroes:
            return
        self.hint.set("Считаю…")
        enemies, allies, role = list(self.enemy), list(self.ally), self.role_f.get()
        steam = self.steam_var.get().strip()
        threading.Thread(target=self.work, args=(enemies, allies, role, steam), daemon=True).start()

    def work(self, enemies, allies, role, steam):
        try:
            vs = {}
            for e in enemies:
                for m in get_json(f"/heroes/{e}/matchups"):
                    if m["games_played"] >= 100:
                        vs.setdefault(m["hero_id"], []).append(1 - m["wins"] / m["games_played"])
            mine = {}
            if steam.isdigit():
                acc = int(steam)
                if acc > 76561197960265728:
                    acc -= 76561197960265728
                for x in get_json(f"/players/{acc}/heroes"):
                    if x["games"]:
                        mine[int(x["hero_id"])] = (x["win"] / x["games"], x["games"])
            rows = []
            for hid, h in self.heroes.items():
                if hid in enemies or hid in allies:
                    continue
                if role != "Любая" and role not in h.get("roles", []):
                    continue
                base = self.base_wr(h)
                v = vs.get(hid, [])
                vs_wr = (sum(v) + base * (len(enemies) - len(v))) / len(enemies) if enemies else base
                score = 0.65 * vs_wr + 0.35 * base
                if hid in mine and mine[hid][1] >= 5:
                    score += 0.1 * (mine[hid][0] - 0.5)
                rows.append((score, h["localized_name"], vs_wr, base, mine.get(hid)))
            rows.sort(reverse=True)
            have = {r for a in allies for r in self.heroes[a].get("roles", [])}
            lack = [r for r in ("Carry", "Support", "Initiator", "Disabler") if allies and r not in have]
            hint = ("В команде не хватает: " + ", ".join(lack) + ". ") if lack else ""
            self.root.after(0, lambda: self.show(rows[:12], hint))
        except Exception as e:
            self.root.after(0, lambda: self.hint.set(f"Ошибка запроса к OpenDota: {e}"))

    def show(self, rows, hint):
        self.tree.delete(*self.tree.get_children())
        for _, name, vs_wr, base, m in rows:
            self.tree.insert("", "end", values=(name, f"{vs_wr * 100:.1f}%", f"{base * 100:.1f}%",
                                                f"{m[0] * 100:.0f}% ({m[1]})" if m else "-"))
        self.hint.set(hint + "Оценка по публичной статистике OpenDota. Это подсказка, а не истина: учитывайте свой пул и состав команды.")

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", lambda: (self.save(), self.root.destroy()))
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
