"""
Zebra SMS টেলিগ্রাম বট
─────────────────────────────────────────────────────────────
কাজ:
  • ইউজার সার্ভিস -> দেশ (লাইভ range) বেছে নাম্বার নেয়, OTP তার নিজের চ্যাটে আসে
  • গ্রুপে লাইভ OTP (নাম্বার আংশিক লুকানো) + নিয়মিত LIVE TRAFFIC সারাংশ
  • ইউজারকে আগে OTP গ্রুপে জয়েন করতে হয়; জয়েন রিকোয়েস্ট বট নিজে অ্যাপ্রুভ করে
  • Render-এ ঘুমিয়ে পড়া ঠেকাতে keepalive + WEBHOOK মোড

Environment ভ্যারিয়েবল (README.md-তে বিস্তারিত):
  BOT_TOKEN, ZEBRA_KEY, ZEBRA_BASE, SUPPORT, ADMIN_IDS,
  OTP_GROUP_ID, OTP_GROUP_URL, CHANNEL_URL, REQUIRED_CHANNELS, BRAND,
  GROUP_ALL_OTPS, LIVE_POST_EVERY, WEBHOOK, BLOCKED_PREFIXES
"""
from collections import OrderedDict
import asyncio, html, json, logging, math, os, random, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, ChatMigrated
try:  # নতুন PTB-তে সরাসরি আছে
    from telegram import CopyTextButton
except ImportError:
    CopyTextButton = None
from telegram.ext import Application, CallbackQueryHandler, ChatJoinRequestHandler, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv("zebra_env.txt")
BOT_TOKEN = os.environ["BOT_TOKEN"]
KEY = os.environ["ZEBRA_KEY"]
BASE = os.environ.get("ZEBRA_BASE", "https://api.zebrasms.com/api/v1/publicapi").rstrip("/")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
SUPPORT = os.environ.get("SUPPORT", "@Monir12255")
BLOCKED_PREFIXES = [x for x in os.environ.get("BLOCKED_PREFIXES", "44").replace(" ", "").split(",") if x]

# বট সার্ভিস -> (বাটনের নাম, sender-এর সম্ভাব্য নাম)
SERVICES = {
    "fb": ("📘 FACEBOOK", ["facebook", "fb"]),
    "ig": ("📸 INSTAGRAM", ["instagram", "insta"]),
    "wa": ("💬 WHATSAPP", ["whatsapp"]),
    "tg": ("✈️ TELEGRAM", ["telegram"]),
}
OTP_GROUP_URL = os.environ.get("OTP_GROUP_URL", "").strip()   # OTP গ্রুপ/চ্যানেলের লিংক (ঐচ্ছিক)
OWNER_URL = os.environ.get("OWNER_URL", f"https://t.me/{SUPPORT.lstrip('@')}")
OTP_GROUP_ID = os.environ.get("OTP_GROUP_ID", "").strip()      # যে গ্রুপে লাইভ OTP পোস্ট হবে (সংখ্যা, যেমন -100123...)
CHANNEL_URL = os.environ.get("CHANNEL_URL", "").strip() or OTP_GROUP_URL
BRAND = os.environ.get("BRAND", "OTP")                       # গ্রুপে নাম্বারের মাঝে বসবে: 224❖OTP❖066
BOT_USERNAME = ""
# WEBHOOK=1 দিলে Telegram নিজেই Render-এর URL-এ বার্তা পাঠায়, তাই ঘুমন্ত বট ইউজারের মেসেজেই জেগে ওঠে
WEBHOOK = os.environ.get("WEBHOOK", "").strip().lower() in ("1", "true", "yes", "on")
WEBHOOK_BASE = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_PATH = "/tg" + re.sub(r"\W", "", BOT_TOKEN)[-24:]
WH_APP = None
WH_LOOP = None
GROUP_FILE = "group.json"
# GROUP_ALL_OTPS=1: Zebra অ্যাকাউন্টে যত OTP আসে (শুধু বটের ইউজার নয়) সবই গ্রুপে যায়
GROUP_ALL_OTPS = os.environ.get("GROUP_ALL_OTPS", "1").strip().lower() not in ("0", "false", "no", "off")
# LIVE_POST_EVERY: কত সেকেন্ড পরপর গ্রুপে "LIVE TRAFFIC" সারাংশ যাবে (0 = বন্ধ)
LIVE_POST_EVERY = int(os.environ.get("LIVE_POST_EVERY", "900") or 0)

def parse_required():
    """REQUIRED_CHANNELS=chat_id|লিংক,@username|লিংক  (বট ওই চ্যানেলে অ্যাডমিন হতে হবে)"""
    out = []
    for item in os.environ.get("REQUIRED_CHANNELS", "").split(","):
        item = item.strip()
        if not item: continue
        cid, _, url = item.partition("|"); cid, url = cid.strip(), url.strip()
        if not url and cid.startswith("@"): url = f"https://t.me/{cid[1:]}"
        out.append((cid, url))
    return out
REQUIRED = parse_required()
join_cache: dict[int, tuple[float, bool]] = {}
KEEPALIVE_URL = os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
KEEP_EVERY = 240          # সেকেন্ড (Render ১৫ মিনিট চুপ থাকলে ঘুমায়)
PAGE = 8                   # এক পাতায় কয়টা দেশ
LIVE_EVERY = 60            # liveaccess cache ৬০ সেকেন্ড
POLL_EVERY, POLL_MAX = 4, 20 * 60
TRY_RANGES = 8
CODES = {  # কান্ট্রি কোড -> (নাম, ISO)
 "1":("USA/Canada","US"),"7":("Russia","RU"),"20":("Egypt","EG"),"27":("South Africa","ZA"),"30":("Greece","GR"),"31":("Netherlands","NL"),
 "32":("Belgium","BE"),"33":("France","FR"),"34":("Spain","ES"),"36":("Hungary","HU"),"39":("Italy","IT"),"40":("Romania","RO"),"41":("Switzerland","CH"),
 "43":("Austria","AT"),"44":("United Kingdom","GB"),"45":("Denmark","DK"),"46":("Sweden","SE"),"47":("Norway","NO"),"48":("Poland","PL"),"49":("Germany","DE"),
 "51":("Peru","PE"),"52":("Mexico","MX"),"54":("Argentina","AR"),"55":("Brazil","BR"),"56":("Chile","CL"),"57":("Colombia","CO"),"58":("Venezuela","VE"),
 "60":("Malaysia","MY"),"61":("Australia","AU"),"62":("Indonesia","ID"),"63":("Philippines","PH"),"64":("New Zealand","NZ"),"65":("Singapore","SG"),"66":("Thailand","TH"),
 "81":("Japan","JP"),"82":("South Korea","KR"),"84":("Vietnam","VN"),"86":("China","CN"),"90":("Turkey","TR"),"91":("India","IN"),"92":("Pakistan","PK"),
 "93":("Afghanistan","AF"),"94":("Sri Lanka","LK"),"95":("Myanmar","MM"),"98":("Iran","IR"),"212":("Morocco","MA"),"213":("Algeria","DZ"),"216":("Tunisia","TN"),
 "218":("Libya","LY"),"220":("Gambia","GM"),"221":("Senegal","SN"),"222":("Mauritania","MR"),"223":("Mali","ML"),"224":("Guinea","GN"),"225":("Ivory Coast","CI"),
 "226":("Burkina Faso","BF"),"227":("Niger","NE"),"228":("Togo","TG"),"229":("Benin","BJ"),"230":("Mauritius","MU"),"231":("Liberia","LR"),"232":("Sierra Leone","SL"),
 "233":("Ghana","GH"),"234":("Nigeria","NG"),"235":("Chad","TD"),"236":("Central African Rep.","CF"),"237":("Cameroon","CM"),"238":("Cape Verde","CV"),
 "240":("Equatorial Guinea","GQ"),"241":("Gabon","GA"),"242":("Congo","CG"),"243":("DR Congo","CD"),"244":("Angola","AO"),"245":("Guinea-Bissau","GW"),
 "249":("Sudan","SD"),"250":("Rwanda","RW"),"251":("Ethiopia","ET"),"252":("Somalia","SO"),"253":("Djibouti","DJ"),"254":("Kenya","KE"),"255":("Tanzania","TZ"),
 "256":("Uganda","UG"),"257":("Burundi","BI"),"258":("Mozambique","MZ"),"260":("Zambia","ZM"),"261":("Madagascar","MG"),"263":("Zimbabwe","ZW"),"264":("Namibia","NA"),
 "265":("Malawi","MW"),"266":("Lesotho","LS"),"267":("Botswana","BW"),"268":("Eswatini","SZ"),"351":("Portugal","PT"),"353":("Ireland","IE"),"358":("Finland","FI"),
 "359":("Bulgaria","BG"),"374":("Armenia","AM"),"375":("Belarus","BY"),"380":("Ukraine","UA"),"381":("Serbia","RS"),"420":("Czechia","CZ"),"421":("Slovakia","SK"),
 "508":("Saint Pierre & Miquelon","PM"),"855":("Cambodia","KH"),"856":("Laos","LA"),"880":("Bangladesh","BD"),"960":("Maldives","MV"),"962":("Jordan","JO"),
 "963":("Syria","SY"),"964":("Iraq","IQ"),"965":("Kuwait","KW"),"966":("Saudi Arabia","SA"),"967":("Yemen","YE"),"968":("Oman","OM"),"971":("UAE","AE"),
 "972":("Israel","IL"),"973":("Bahrain","BH"),"974":("Qatar","QA"),"977":("Nepal","NP"),"992":("Tajikistan","TJ"),"993":("Turkmenistan","TM"),
 "994":("Azerbaijan","AZ"),"995":("Georgia","GE"),"996":("Kyrgyzstan","KG"),"998":("Uzbekistan","UZ"),
}

logging.basicConfig(level=logging.INFO)
http = httpx.AsyncClient(timeout=20)
live: dict[str, dict] = {}          # service -> {range: hits}
watchers: dict[str, dict] = {}
chat_number: dict[int, str] = {}
last_raw_log = 0.0
live_total: dict[str, int] = {}     # service -> liveaccess-এর মোট count
feed_seen: "OrderedDict[str, None]" = OrderedDict()
feed_warm = False

# ---------- Zebra API (সফল হলে meta.code == 0) ----------
async def zapi(method, path, params=None, body=None):
    r = await http.request(method, f"{BASE}/{path}", headers={"MAuth": KEY}, params=params, json=body)
    try:
        j = r.json()
    except Exception:
        r.raise_for_status(); raise RuntimeError("JSON পাওয়া যায়নি")
    meta = j.get("meta") or {}
    if meta.get("code", 0) != 0:
        raise RuntimeError(f"{meta.get('code')}: {j.get('message') or meta.get('error') or 'error'}")
    r.raise_for_status()
    return j.get("data", j)

def digits(x): return re.sub(r"\D", "", str(x))
def flag(code):
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code) if len(code) == 2 and code.isalpha() else "🌍"
def country_of(rng):
    d = digits(rng)
    for n in (3, 2, 1):
        if d[:n] in CODES: return CODES[d[:n]]
    return ("Unknown", "??")
def blocked(rng): return any(digits(rng).startswith(p) for p in BLOCKED_PREFIXES)

# ---------- লাইভ range (ট্রাফিক অনুযায়ী) ----------
def add_rows(acc, rows):
    for row in rows:
        rs = row.get("ranges") or []
        per = max(1, math.ceil(int(row.get("count", 1) or 1) / max(1, len(rs))))
        for r in rs:
            if not blocked(r): acc[r] = acc.get(r, 0) + per

async def live_rows(sender=None):
    d = await zapi("GET", "liveaccess", params={"sender": sender} if sender else None)
    return d.get("rows", [])

async def live_loop():
    """সব চালু range নিজে নিজে আসে: আগে সব sender একসাথে, না পেলে নাম ধরে ধরে"""
    while True:
        all_rows = []
        try:
            all_rows = await live_rows()
        except Exception as e:
            logging.warning("liveaccess(all): %s", e)
        for key, (_, senders) in SERVICES.items():
            acc: dict[str, int] = {}
            rows_k = [r for r in all_rows if any(x in str(r.get("sender", "")).lower() for x in senders)]
            tot = sum(int(r.get("count", 0) or 0) for r in rows_k)
            add_rows(acc, rows_k)
            if not acc:
                for sd in senders:
                    try:
                        rows = await live_rows(sd)
                        add_rows(acc, rows); tot += sum(int(r.get("count", 0) or 0) for r in rows)
                    except Exception as e:
                        logging.warning("liveaccess %s: %s", sd, e)
                    await asyncio.sleep(1)
            if acc:                            # খালি এলে আগেরটা রেখে দেয়
                live[key] = acc; live_total[key] = tot
            logging.info("live %s: %d ranges", key, len(acc))
        await asyncio.sleep(LIVE_EVERY)

def country_groups(key):
    groups = {}
    for rng, hits in live.get(key, {}).items():
        name, iso = country_of(rng)
        g = groups.setdefault(iso, {"name": name, "ranges": [], "hits": 0})
        g["ranges"].append(rng); g["hits"] += hits
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]["ranges"]), -kv[1]["hits"]))

async def get_number(key, iso):
    g = dict(country_groups(key)).get(iso)
    if not g: raise RuntimeError("এই দেশে এখন range নেই")
    cands = sorted(g["ranges"], key=lambda r: -live[key].get(r, 0) * random.random())
    last = None
    for rng in cands[:TRY_RANGES]:
        try:
            d = await zapi("POST", "getnum", body={"range": rng})
            rows = d.get("rows") or []
            if rows and rows[0].get("number"):
                return rows[0]
            last = RuntimeError("নাম্বার পাওয়া যায়নি")
        except Exception as e:
            last = e; logging.warning("getnum %s: %s", rng, e)
    raise last or RuntimeError("নাম্বার পাওয়া যায়নি")

# ---------- OTP (getupdate, সব ইউজারের জন্য একটাই পোলার) ----------
def first(d, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""): return d[k]
    return None

def find_list(j):
    if isinstance(j, list): return j
    if isinstance(j, dict):
        for k in ("rows", "data", "messages", "updates", "results", "items", "sms"):
            if k in j:
                r = find_list(j[k])
                if r is not None: return r
    return None

def to_ms(v):
    try:
        v = float(v)
    except Exception:
        return None
    return v * 1000 if v < 1e11 else v

def parse_updates(d):
    out = []
    for it in find_list(d) or []:
        num = first(it, ("number", "phone", "to", "msisdn", "recipient"))
        txt = first(it, ("text", "message", "body", "sms", "content", "otp", "code"))
        if num is None or txt is None: continue
        ts = to_ms(first(it, ("at_ms", "time_ms", "timestamp", "ts", "time", "received_at_ms")))
        uid = first(it, ("id", "uuid", "message_id")) or f"{num}:{txt}:{ts}"
        out.append((digits(num), str(txt), str(uid), ts))
    return out

def add_watcher(chat_id, number, key="fb", iso="??", country=""):
    drop_watcher(chat_id)
    n = digits(number)
    watchers[n] = {"key": key, "iso": iso, "country": country, "chat": chat_id, "start": time.time() * 1000 - 5000, "expires": time.time() + POLL_MAX, "seen": set(), "got": False}
    chat_number[chat_id] = n

def drop_watcher(chat_id):
    n = chat_number.pop(chat_id, None)
    if n: watchers.pop(n, None)

last_notify: dict[str, float] = {}

def save_group():
    try: json.dump({"id": OTP_GROUP_ID}, open(GROUP_FILE, "w"))
    except Exception as e: logging.warning("save group: %s", e)

async def notify_admin(app, key, text, cooldown=600):
    """অ্যাডমিনকে ব্যক্তিগত চ্যাটে কারণ জানায় (বারবার না)"""
    if time.time() - last_notify.get(key, 0) < cooldown: return
    last_notify[key] = time.time()
    for aid in ADMIN_IDS:
        try: await app.bot.send_message(aid, text)
        except Exception as e: logging.warning("notify %s: %s", aid, e)

async def post_group(app, w, num, code):
    """গ্রুপে লাইভ OTP পোস্ট (নাম্বার আংশিক লুকিয়ে)"""
    global OTP_GROUP_ID
    if not OTP_GROUP_ID:
        logging.warning("OTP_GROUP_ID সেট নেই, গ্রুপে পোস্ট হচ্ছে না")
        await notify_admin(app, "nogroup", "⚠️ OTP গ্রুপ সেট নেই, তাই গ্রুপে OTP যাচ্ছে না।\n\nগ্রুপে বটকে Admin বানিয়ে গ্রুপে /setgroup লিখে পাঠান।")
        return
    mask = f"{num[:3]}❖{BRAND}❖{num[-3:]}"
    text = (f"🔥 <b>100% OTP RECEIVE</b>\n"
            f"{flag(w['iso'])} {w['iso']} | {svc_emoji(w['key'])} <code>{mask}</code> | 🔴 <b>LIVE</b>")
    rows = []
    if code: rows.append([copy_btn(f"✅ 📋 {code}", code)])
    bot_url = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else ""
    rows.append(url_row(("🔥 BOT", bot_url), ("🔗 CHANNEL", CHANNEL_URL)))
    for _ in range(2):
        try:
            await app.bot.send_message(int(OTP_GROUP_ID), text, parse_mode="HTML", reply_markup=M([r for r in rows if r]))
            return
        except ChatMigrated as e:           # গ্রুপ সুপারগ্রুপ হলে id বদলে যায়
            OTP_GROUP_ID = str(e.new_chat_id); save_group(); logging.info("গ্রুপের নতুন id: %s", OTP_GROUP_ID)
        except Exception as e:
            logging.warning("group post: %s", e)
            await notify_admin(app, "postfail", f"⚠️ গ্রুপে OTP পোস্ট হয়নি।\nকারণ: {e}\n\nবট গ্রুপে Admin আছে কিনা দেখুন, তারপর গ্রুপে /setgroup লিখুন।")
            return

def feed_mark(k):
    """গ্রুপ ফিডে একই OTP দুবার যাওয়া ঠেকায়; নতুন হলে True"""
    if k in feed_seen: return False
    feed_seen[k] = None
    while len(feed_seen) > 3000: feed_seen.popitem(last=False)
    return True

def otp_key(num, txt):
    m = re.search(r"\b\d{4,8}\b", txt)
    code = m.group(0) if m else None
    return code, f"{num}:{code or txt}"

async def otp_loop(app):
    """getupdate পড়ে: ইউজারের OTP তার চ্যাটে, আর সব OTP গ্রুপে"""
    global last_raw_log, feed_warm
    while True:
        try:
            if watchers or (GROUP_ALL_OTPS and OTP_GROUP_ID):
                d = await zapi("GET", "getupdate")
                items = parse_updates(d)
                if not items and time.time() - last_raw_log > 60:
                    last_raw_log = time.time(); logging.info("getupdate raw: %s", json.dumps(d, ensure_ascii=False)[:600])
                if not feed_warm:               # চালুর সময়ের পুরনো OTP গ্রুপে ঢালা হবে না
                    for num, txt, uid, ts in items: feed_mark(otp_key(num, txt)[1])
                    feed_warm = True
                for num, txt, uid, ts in items:
                    code, ckey = otp_key(num, txt)
                    w = watchers.get(num)
                    if w:                                         # বটের ইউজারের নাম্বার
                        if uid in w["seen"]: continue
                        if ts is not None and ts < w["start"]: continue     # নাম্বার নেওয়ার আগের মেসেজ বাদ
                        w["seen"].add(uid)
                        if ckey in w["seen"]: continue                       # একই কোড দ্বিতীয়বার বাদ
                        w["seen"].add(ckey); w["got"] = True
                        feed_mark(ckey)
                        logging.info("OTP পাঠানো হচ্ছে: %s id=%s", ckey, uid)
                        lines = ["🔥 <b>100% OTP RECEIVE</b>",
                                 f"{flag(w['iso'])} {w['iso']} | {svc_emoji(w['key'])} <code>+{num}</code> | 🔴 <b>LIVE</b>"]
                        if not code:
                            lines += ["", f"💬 {html.escape(txt)}"]
                        rows = []
                        if code: rows.append([copy_btn(f"✅ 📋 {code}", code)])
                        rows.append(url_row(("👤 OWNER", OWNER_URL), ("✅ OTP GROUP", OTP_GROUP_URL)))
                        try:
                            await app.bot.send_message(w["chat"], "\n".join(lines), parse_mode="HTML", reply_markup=M([r for r in rows if r]))
                        except Exception as e:
                            logging.warning("send: %s", e)
                        await post_group(app, w, num, code)
                    elif GROUP_ALL_OTPS and OTP_GROUP_ID:         # অ্যাকাউন্টের অন্য সব OTP -> শুধু গ্রুপে
                        if not feed_mark(ckey): continue
                        if ts is not None and time.time() * 1000 - ts > 10 * 60 * 1000: continue   # ১০ মিনিটের পুরনো বাদ
                        await post_group(app, {"iso": country_of(num)[1], "key": svc_from_text(txt)}, num, code)
        except Exception as e:
            logging.warning("getupdate error: %s", e)
        now = time.time()
        for n, w in list(watchers.items()):
            if now > w["expires"]:
                watchers.pop(n, None); chat_number.pop(w["chat"], None)
                if not w["got"]:
                    try: await app.bot.send_message(w["chat"], "⌛ সময় শেষ, OTP আসেনি। নতুন নাম্বার নিন।")
                    except Exception: pass
        await asyncio.sleep(POLL_EVERY if (watchers or (GROUP_ALL_OTPS and OTP_GROUP_ID)) else 10)

async def traffic_loop(app):
    """গ্রুপে নিয়মিত লাইভ ট্রাফিকের সারাংশ (Zebra-র liveaccess থেকে, কোনো বানানো তথ্য নয়)"""
    if not LIVE_POST_EVERY: return
    await asyncio.sleep(120)           # আগে live range আসতে দিই
    while True:
        try:
            if OTP_GROUP_ID and BOT_USERNAME and live:
                lines = ["📊 <b>LIVE TRAFFIC</b>", "এখন এই রেঞ্জগুলোতে OTP আসছে 👇", ""]
                shown = False
                for key in SERVICES:
                    groups = country_groups(key)
                    if not groups: continue
                    shown = True
                    tot = live_total.get(key)
                    head = f"{svc_emoji(key)} <b>{svc_name(key)}</b>" + (f" • {tot} OTP" if tot else "")
                    flags = " ".join(f"{flag(iso)} {iso}" for iso, _ in groups[:4])
                    n = sum(len(g["ranges"]) for _, g in groups)
                    lines.append(f"{head}\n{flags} • {n} live range{'s' if n != 1 else ''}")
                if shown:
                    lines += ["", "👇 নাম্বার নিন আমাদের বট থেকে"]
                    kb = M([url_row(("🔥 GET NUMBER", f"https://t.me/{BOT_USERNAME}"), ("🔗 CHANNEL", CHANNEL_URL))])
                    await app.bot.send_message(int(OTP_GROUP_ID), "\n".join(lines), parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logging.warning("traffic post: %s", e)
        await asyncio.sleep(LIVE_POST_EVERY)

# ---------- বাধ্যতামূলক জয়েন ----------
def required_list():
    """REQUIRED_CHANNELS + OTP গ্রুপ (গ্রুপ সেট ও লিংক থাকলে নিজে যোগ হয়)"""
    out = list(REQUIRED)
    if OTP_GROUP_ID and OTP_GROUP_URL and not any(str(c) == str(OTP_GROUP_ID) for c, _ in out):
        out.append((OTP_GROUP_ID, OTP_GROUP_URL))
    return out

async def is_joined(bot, uid):
    req = required_list()
    if not req or uid in ADMIN_IDS: return True
    c = join_cache.get(uid)
    if c and c[1] and time.time() - c[0] < 120: return True
    ok = True
    for cid, _ in req:
        try:
            cm = await bot.get_chat_member(int(cid) if re.fullmatch(r"-?\d+", cid) else cid, uid)
            if cm.status in ("left", "kicked"):
                ok = False; break
        except Exception as e:       # চেক করা না গেলে ইউজারকে আটকাই না
            logging.warning("join check %s: %s", cid, e)
    join_cache[uid] = (time.time(), ok)
    return ok

def join_prompt():
    rows = [[B("📢 JOIN CHANNEL", url=u)] for _, u in required_list() if u]
    rows.append([B("✅ CHECK JOINED", callback_data="chk")])
    return "⚠️ Please join our channels to use the bot!", M(rows)

# ---------- UI ----------
def copy_btn(label, text):
    """ট্যাপ করলেই কপি হয় (Telegram copy_text বাটন)"""
    if CopyTextButton is not None:
        return B(label, copy_text=CopyTextButton(text=text))
    return B(label, api_kwargs={"copy_text": {"text": text}})

def url_row(*pairs):
    return [B(t, url=u) for t, u in pairs if u]

def svc_emoji(key): return SERVICES[key][0].split()[0] if key in SERVICES else "📱"
def svc_from_text(txt):
    t = str(txt).lower()
    for k, (_, senders) in SERVICES.items():
        if any(x in t for x in senders): return k
    return None
def svc_name(key): return SERVICES[key][0].split(" ", 1)[1].title()

MAIN_KB = ReplyKeyboardMarkup([["📱 GET NUMBER"], ["🆘 SUPPORT"]], resize_keyboard=True)

def service_kb():
    rows = [[B(v[0], callback_data=f"svc:{k}")] for k, v in SERVICES.items()]
    rows.append([B("❌ CLOSE", callback_data="close")])
    return M(rows)

def country_view(key, page=0):
    groups = country_groups(key)
    name = SERVICES[key][0]
    if not groups:
        return f"❌ {name}-এর জন্য এখন কোনো চালু range নেই।\nসাপোর্ট: {SUPPORT}", M([[B("⬅️ BACK", callback_data="back")]])
    pages = max(1, math.ceil(len(groups) / PAGE)); page = min(max(page, 0), pages - 1)
    total_ranges = sum(len(g["ranges"]) for _, g in groups)
    lines = [f"🌍 Select a country for {name}:", "", f"{len(groups)} countries • {total_ranges} live ranges • page {page + 1}/{pages}"]
    btns = []
    for i, (iso, g) in enumerate(groups[page * PAGE:(page + 1) * PAGE], page * PAGE + 1):
        fire = " 🔥" if i == 1 else ""
        n = len(g["ranges"])
        lines.append(f"{i}. {flag(iso)} {g['name']} ({iso}) • {n} live range{'s' if n != 1 else ''}{fire}")
        btns.append(B(f"{flag(iso)} {iso} · {n}", callback_data=f"cty:{key}:{iso}"))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    nav = []
    if page > 0: nav.append(B("◀️ PREV", callback_data=f"pg:{key}:{page - 1}"))
    if page < pages - 1: nav.append(B("NEXT ▶️", callback_data=f"pg:{key}:{page + 1}"))
    if nav: rows.append(nav)
    rows.append([B("⬅️ BACK", callback_data="back")])
    return "\n".join(lines), M(rows)

async def safe_edit(q, text, **kw):
    try:
        await q.edit_message_text(text, **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            await q.message.reply_text(text, **kw)

async def start(u: Update, c):
    if not await is_joined(c.bot, u.effective_user.id):
        text, kb = join_prompt(); await u.message.reply_text(text, reply_markup=kb); return
    await u.message.reply_text("স্বাগতম! নিচের মেনু থেকে বেছে নিন 👇", reply_markup=MAIN_KB)
async def menu(u: Update, c):
    if not await is_joined(c.bot, u.effective_user.id):
        text, kb = join_prompt(); await u.message.reply_text(text, reply_markup=kb); return
    await u.message.reply_text("📍 Select a service:", reply_markup=service_kb())

def load_group():
    global OTP_GROUP_ID
    if OTP_GROUP_ID: return
    try:
        OTP_GROUP_ID = str(json.load(open(GROUP_FILE)).get("id", "")).strip()
    except Exception:
        pass

async def on_my_chat_member(u: Update, c):
    """বটকে গ্রুপে অ্যাডমিন বানালেই গ্রুপ নিজে সেট হয়ে যায় (id কপি করার দরকার নেই)"""
    global OTP_GROUP_ID
    cm = u.my_chat_member
    if not cm or cm.chat.type not in ("group", "supergroup", "channel"): return
    if cm.new_chat_member.status not in ("administrator", "member"): return
    adder = cm.from_user.id
    if ADMIN_IDS:
        if adder not in ADMIN_IDS: return
    elif OTP_GROUP_ID:        # ADMIN_IDS নেই: প্রথম গ্রুপই শেষ, অন্যরা বদলাতে পারবে না
        return
    OTP_GROUP_ID = str(cm.chat.id)
    save_group()
    logging.info("OTP গ্রুপ সেট: %s (%s)", cm.chat.title, OTP_GROUP_ID)
    try:
        await c.bot.send_message(adder, f"✅ OTP গ্রুপ সেট হয়েছে: {cm.chat.title}\nid: {cm.chat.id}\n\nবটকে অ্যাডমিন বানাতে ভুলবেন না। স্থায়ী করতে Render-এ OTP_GROUP_ID={cm.chat.id} বসান।")
    except Exception as e:
        logging.warning("notify adder: %s", e)

async def on_group_message(u: Update, c):
    """রিস্টার্টের পর গ্রুপে অ্যাডমিন যেকোনো মেসেজ দিলেই গ্রুপ আবার সেট হয়ে যায়"""
    global OTP_GROUP_ID
    if OTP_GROUP_ID or not u.effective_user or not u.effective_chat: return
    if ADMIN_IDS and u.effective_user.id not in ADMIN_IDS: return
    if not ADMIN_IDS: return          # ADMIN_IDS না থাকলে অচেনা কেউ গ্রুপ বদলাতে পারে, তাই /setgroup লাগবে
    OTP_GROUP_ID = str(u.effective_chat.id); save_group()
    logging.info("গ্রুপ আবার সেট: %s", OTP_GROUP_ID)

async def on_join_request(u: Update, c):
    """জয়েন রিকোয়েস্ট নিজে অ্যাপ্রুভ (বটকে গ্রুপে 'Invite Users via Link' অনুমতি দিতে হবে)"""
    r = u.chat_join_request
    targets = {str(OTP_GROUP_ID)} | {str(x) for x, _ in REQUIRED}
    if not r or str(r.chat.id) not in targets: return
    try:
        await r.approve()
        join_cache.pop(r.from_user.id, None)
    except Exception as e:
        logging.warning("join request approve: %s", e)

async def set_group(u: Update, c):
    """গ্রুপে /setgroup লিখলে ওই গ্রুপই OTP গ্রুপ হয়"""
    global OTP_GROUP_ID
    chat = u.effective_chat
    if chat.type not in ("group", "supergroup", "channel"):
        await u.message.reply_text("এই কমান্ড OTP গ্রুপের ভেতরে লিখুন।"); return
    uid = u.effective_user.id
    if (ADMIN_IDS and uid not in ADMIN_IDS) or (not ADMIN_IDS and OTP_GROUP_ID and str(chat.id) != OTP_GROUP_ID):
        await u.message.reply_text("শুধু অ্যাডমিন এটা করতে পারেন।"); return
    OTP_GROUP_ID = str(chat.id); save_group()
    await u.message.reply_text(f"✅ এই গ্রুপে এখন থেকে লাইভ OTP পোস্ট হবে।\nস্থায়ী করতে Render-এ OTP_GROUP_ID={chat.id} বসান।")

async def test_group(u: Update, c):
    """অ্যাডমিন: গ্রুপে পোস্ট হয় কিনা পরীক্ষা, না হলে কারণ বলে দেয়"""
    if u.effective_user.id not in ADMIN_IDS:
        await u.message.reply_text("শুধু অ্যাডমিন। ADMIN_IDS-এ আপনার id বসান (/id)।"); return
    if not OTP_GROUP_ID:
        await u.message.reply_text("❌ OTP_GROUP_ID বসানো নেই। গ্রুপে /groupid পাঠিয়ে সংখ্যাটা বসান।"); return
    try:
        await c.bot.send_message(int(OTP_GROUP_ID), "✅ টেস্ট: গ্রুপে পোস্ট কাজ করছে")
        await u.message.reply_text(f"✅ পোস্ট হয়েছে (গ্রুপ id: {OTP_GROUP_ID})")
    except Exception as e:
        await u.message.reply_text(f"❌ গ্রুপে পোস্ট হয়নি: {e}\n\nবটকে গ্রুপে অ্যাডমিন বানিয়েছেন? id ঠিক আছে?")

async def group_id(u: Update, c):
    await u.message.reply_text(f"এই চ্যাটের id: {u.effective_chat.id}")
async def support(u: Update, c): await u.message.reply_text(f"🆘 সাপোর্ট: {SUPPORT}")
async def my_id(u: Update, c): await u.message.reply_text(f"আপনার id: {u.effective_user.id}")

async def test(u: Update, c):
    if u.effective_user.id not in ADMIN_IDS:
        await u.message.reply_text("শুধু অ্যাডমিন। ADMIN_IDS-এ আপনার id বসান (/id)।"); return
    for name, path, params in (("getupdate", "getupdate", None), ("liveaccess(all)", "liveaccess", None)):
        try:
            d = await zapi("GET", path, params=params)
            await u.message.reply_text(f"[{name}]\n{json.dumps(d, ensure_ascii=False)[:1300]}")
        except Exception as e:
            await u.message.reply_text(f"[{name}] ❌ {e}")
    await u.message.reply_text("live: " + ", ".join(f"{k}={len(v)}" for k, v in live.items()))

async def on_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    chat = q.message.chat_id
    if q.data == "chk":
        join_cache.pop(u.effective_user.id, None)
        if await is_joined(c.bot, u.effective_user.id):
            await q.answer(); await safe_edit(q, "📍 Select a service:", reply_markup=service_kb())
            await c.bot.send_message(chat, "✅ ধন্যবাদ! এখন বট ব্যবহার করতে পারবেন 👇", reply_markup=MAIN_KB)
        else:
            await q.answer("❌ আপনি এখনও সব চ্যানেলে জয়েন করেননি", show_alert=True)
        return
    await q.answer()
    if q.data != "close" and not await is_joined(c.bot, u.effective_user.id):
        text, kb = join_prompt(); await safe_edit(q, text, reply_markup=kb); return
    if q.data == "close":
        await q.message.delete(); return
    if q.data == "back":
        drop_watcher(chat); await safe_edit(q, "📍 Select a service:", reply_markup=service_kb()); return
    if q.data.startswith("svc:"):
        drop_watcher(chat); text, kb = country_view(q.data[4:]); await safe_edit(q, text, reply_markup=kb); return
    if q.data.startswith("pg:"):
        _, key, pg = q.data.split(":"); text, kb = country_view(key, int(pg)); await safe_edit(q, text, reply_markup=kb); return
    if q.data.startswith("cty:"):
        _, key, iso = q.data.split(":"); drop_watcher(chat)
        try:
            row = await get_number(key, iso)
        except Exception as e:
            text, kb = country_view(key); await safe_edit(q, f"❌ {e}\n\n" + text, reply_markup=kb); return
        num = digits(row["number"])
        iso2 = str(row.get("iso2") or iso).upper(); cname = row.get("country", "")
        text = (f"{svc_emoji(key)} <b>{svc_name(key)} {html.escape(cname)}</b>\n"
                f"{flag(iso2)} {html.escape(cname)} ({iso2})\n"
                f"📶 {html.escape(str(row.get('operator', '')))}\n\n"
                f"⏳ OTP-র জন্য অপেক্ষা করছি...")
        rows = [[copy_btn(f"{flag(iso2)} 📋 +{num}", f"+{num}")],
                [B("🔄 CHANGE NUMBER", callback_data=f"cty:{key}:{iso}")] + url_row(("✅ OTP GROUP", OTP_GROUP_URL)),
                [B("⬅️ Back", callback_data=f"svc:{key}"), B("❌ Close", callback_data="close")]]
        await safe_edit(q, text, parse_mode="HTML", reply_markup=M(rows))
        add_watcher(chat, num, key, iso2, cname)

# ---------- Render ----------
def start_health_server():
    """GET / -> ok (UptimeRobot/Render-এর জন্য); POST WEBHOOK_PATH -> Telegram-এর আপডেট"""
    port = os.environ.get("PORT")
    if not port: return
    class H(BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        do_HEAD = do_GET
        def do_POST(self):
            if not WEBHOOK or self.path != WEBHOOK_PATH:
                self.send_response(404); self.end_headers(); return
            if WH_APP is None or WH_LOOP is None:       # বট এখনও চালু হচ্ছে, Telegram পরে আবার পাঠাবে
                self.send_response(503); self.end_headers(); return
            try:
                n = int(self.headers.get("Content-Length", 0))
                upd = Update.de_json(json.loads(self.rfile.read(n) or b"{}"), WH_APP.bot)
                WH_LOOP.call_soon_threadsafe(WH_APP.update_queue.put_nowait, upd)
                code = 200
            except Exception as e:
                logging.warning("webhook: %s", e); code = 500
            self.send_response(code); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()

async def keepalive():
    """নিজের URL-এ নিয়মিত ঢুকে Render-কে জাগিয়ে রাখে"""
    while KEEPALIVE_URL:
        await asyncio.sleep(KEEP_EVERY)
        try:
            await http.get(KEEPALIVE_URL)
        except Exception as e:
            logging.warning("keepalive: %s", e)

async def post_init(app):
    global BOT_USERNAME
    try:
        BOT_USERNAME = (await app.bot.get_me()).username or ""
    except Exception as e:
        logging.warning("get_me: %s", e)
    if not OTP_GROUP_ID:      # রিস্টার্টের পর গ্রুপ হারিয়ে গেলে অ্যাডমিনকে জানায়
        await notify_admin(app, "start_nogroup", "⚠️ বট চালু হয়েছে কিন্তু OTP গ্রুপ সেট নেই।\n\nস্থায়ীভাবে ঠিক করতে: গ্রুপে /groupid লিখে সংখ্যাটা Render-এর Environment-এ OTP_GROUP_ID নামে বসান।\nএখনকার মতো চালু করতে: গ্রুপে /setgroup লিখুন।", cooldown=0)
    asyncio.create_task(live_loop()); asyncio.create_task(otp_loop(app)); asyncio.create_task(traffic_loop(app)); asyncio.create_task(keepalive())

def build_app():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("groupid", group_id))
    app.add_handler(CommandHandler("testgroup", test_group))
    app.add_handler(CommandHandler("setgroup", set_group))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_group_message), group=2)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(MessageHandler(filters.Regex("GET NUMBER$"), menu))
    app.add_handler(MessageHandler(filters.Regex("SUPPORT$"), support))
    app.add_handler(CallbackQueryHandler(on_cb))
    return app

def build_and_run():
    app = build_app()
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()

async def webhook_main():
    global WH_APP, WH_LOOP
    if not WEBHOOK_BASE:
        raise RuntimeError("WEBHOOK=1 কিন্তু WEBHOOK_URL / RENDER_EXTERNAL_URL নেই")
    app = build_app()
    await app.initialize()
    await post_init(app)      # run_polling নিজে এটা চালায়, webhook মোডে আমাদের চালাতে হয় (live range, OTP পোলার, keepalive)
    await app.start()
    WH_APP, WH_LOOP = app, asyncio.get_running_loop()
    await app.bot.set_webhook(url=WEBHOOK_BASE + WEBHOOK_PATH, allowed_updates=["message", "callback_query", "my_chat_member", "chat_join_request"])
    logging.info("webhook মোড চালু: %s", WEBHOOK_BASE)
    await asyncio.Event().wait()

def main():
    start_health_server()
    load_group()
    while True:               # কোনো কারণে থেমে গেলে ৫ সেকেন্ডে নিজে আবার চালু হয়
        try:
            if WEBHOOK:
                asyncio.run(webhook_main())
            else:
                build_and_run()
            break
        except Exception as e:
            logging.error("বট থেমে গেছে, আবার চালু হচ্ছে: %s", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
