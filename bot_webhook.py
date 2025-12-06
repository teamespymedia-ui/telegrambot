import json
import logging
import os
import asyncio
import threading
import time
from flask import Flask, request, jsonify
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Bot,
)
from telegram.error import BadRequest, TelegramError
from concurrent.futures import ThreadPoolExecutor

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Flask App ----------
app = Flask(__name__)

# ---------- Thread Pool for Fast Response ----------
executor = ThreadPoolExecutor(max_workers=2)

# ---------- Config ----------
CONFIG_PATH = "config.json"
config = {}
bot = None
TOKEN = ""

def load_config():
    global config, bot, TOKEN
    if not os.path.exists(CONFIG_PATH):
        default_config = {
            "TOKEN": "YOUR_BOT_TOKEN",
            "ADMIN_ID": 123456789,
            "ADMIN_PASSWORD": "admin123",
            "DEFAULT_UPI": "example@upi",
            "WELCOME_IMAGE": "",
            "CUSTOM_CAPTION": "",
            "PLANS": {
                "500A": {"NAME": "22 Channels Access", "QR_IMAGE": "", "DEMO_LINK": "", "LINKS": []},
                "500B": {"NAME": "35 Mega Collection", "QR_IMAGE": "", "DEMO_LINK": "", "LINKS": []},
                "800": {"NAME": "Premium Combo", "QR_IMAGE": "", "DEMO_LINK": "", "LINKS": []}
            }
        }
        with open(CONFIG_PATH, "w") as f:
            json.dump(default_config, f, indent=2)
        config = default_config
    else:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    
    TOKEN = config["TOKEN"]
    from telegram.request import HTTPXRequest

# Create bot with proper timeout settings
request = HTTPXRequest(
    connection_pool_size=8,
    connect_timeout=10.0,
    read_timeout=10.0,
    write_timeout=10.0,
    pool_timeout=10.0
)
bot = Bot(token=TOKEN, request=request)
    return config

def save_config():
    global config
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

# Load on startup
load_config()

# ---------- State Management ----------
user_states = {}

def get_state(user_id):
    if user_id not in user_states:
        user_states[user_id] = {}
    return user_states[user_id]

def clear_state(user_id):
    if user_id in user_states:
        auth = user_states[user_id].get("admin_auth", False)
        user_states[user_id] = {"admin_auth": auth} if auth else {}

def clear_input_state(user_id):
    state = get_state(user_id)
    for key in ["setting_qr", "setting_links", "setting_demo", "editing_welcome", 
                "setting_welcome_photo", "current_plan", "awaiting_password"]:
        state.pop(key, None)

# ---------- Deletion Manager ----------
pending_deletions = {}
deletion_lock = threading.Lock()

def deletion_worker():
    """Background worker for message deletion"""
    while True:
        try:
            now = time.time()
            to_delete = []
            
            with deletion_lock:
                for key, data in list(pending_deletions.items()):
                    if now >= data["time"]:
                        to_delete.append((key, data["chat_id"], data["msg_id"]))
            
            for key, chat_id, msg_id in to_delete:
                try:
                    delete_bot = Bot(token=TOKEN)
                    asyncio.run(delete_bot.delete_message(chat_id=chat_id, message_id=msg_id))
                    logger.info(f"🗑️ Deleted message {msg_id}")
                except Exception as e:
                    logger.warning(f"Delete failed {msg_id}: {e}")
                finally:
                    with deletion_lock:
                        pending_deletions.pop(key, None)
            
            time.sleep(3)
        except Exception as e:
            logger.error(f"Deletion worker error: {e}")
            time.sleep(5)

def schedule_delete(chat_id, msg_id, delay=120):
    with deletion_lock:
        pending_deletions[f"{chat_id}_{msg_id}"] = {
            "chat_id": chat_id,
            "msg_id": msg_id,
            "time": time.time() + delay
        }
    logger.info(f"📋 Scheduled delete: {msg_id} in {delay}s")

# Start deletion worker
threading.Thread(target=deletion_worker, daemon=True).start()

# ---------- Helpers ----------
def get_plan(code):
    return config.get("PLANS", {}).get(code, {})

def ensure_plan(code):
    if "PLANS" not in config:
        config["PLANS"] = {}
    if code not in config["PLANS"]:
        config["PLANS"][code] = {"NAME": f"Plan {code}", "QR_IMAGE": "", "DEMO_LINK": "", "LINKS": []}
    if "LINKS" not in config["PLANS"][code]:
        config["PLANS"][code]["LINKS"] = []
    return config["PLANS"][code]

def is_admin(user_id):
    return user_id == config.get("ADMIN_ID")

# ---------- Keyboards ----------
def main_kb(user_id):
    kb = [
        [KeyboardButton("🏁 Start"), KeyboardButton("📦 Plans")],
        [KeyboardButton("🎬 Demo"), KeyboardButton("❓ Help")]
    ]
    if is_admin(user_id):
        kb.append([KeyboardButton("🔧 Admin Panel")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📦 Manage 500A"), KeyboardButton("📦 Manage 500B")],
        [KeyboardButton("💎 Manage 800"), KeyboardButton("🧾 View Config")],
        [KeyboardButton("✏️ Edit Welcome"), KeyboardButton("🖼️ Set Welcome Photo")],
        [KeyboardButton("💳 Set UPI"), KeyboardButton("🔙 Exit Admin")]
    ], resize_keyboard=True)

def plan_kb(code):
    return ReplyKeyboardMarkup([
        [KeyboardButton(f"🖼️ QR {code}"), KeyboardButton(f"🎬 Demo {code}")],
        [KeyboardButton(f"🔗 Links {code}"), KeyboardButton(f"👁️ View {code}")],
        [KeyboardButton(f"📝 Name {code}"), KeyboardButton("🔙 Back")]
    ], resize_keyboard=True)

# ---------- Async Handlers ----------
async def cmd_start(chat_id, user_id, user):
    clear_state(user_id)
    
    text = config.get("CUSTOM_CAPTION") or (
        "🎉 *Welcome to Premium Bot!*\n\n"
        "📦 *Available Plans:*\n"
        "• ₹500 Plan A - 22 Channels\n"
        "• ₹500 Plan B - 35 Collections\n"
        "• ₹800 Premium - Both Combined\n\n"
        "👇 *Select a plan to pay:*"
    )
    
    # Add demo links
    demos = []
    for code in ["500A", "500B", "800"]:
        link = get_plan(code).get("DEMO_LINK", "")
        if link:
            name = get_plan(code).get("NAME", code)
            demos.append(f"• {name}: [Demo]({link})")
    if demos:
        text += "\n\n🎬 *Demos:*\n" + "\n".join(demos)
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 ₹500 - Plan A", callback_data="pay_500A")],
        [InlineKeyboardButton("💳 ₹500 - Plan B", callback_data="pay_500B")],
        [InlineKeyboardButton("💎 ₹800 - Premium", callback_data="pay_800")]
    ])
    
    img = config.get("WELCOME_IMAGE", "")
    try:
        if img:
            await bot.send_photo(chat_id, photo=img, caption=text, parse_mode="Markdown", reply_markup=kb)
        else:
            await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Start error: {e}")
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
    
    await bot.send_message(chat_id, "⬇️ Menu:", reply_markup=main_kb(user_id))

async def cmd_plans(chat_id, user_id):
    for code in ["500A", "500B", "800"]:
        p = get_plan(code)
        name = p.get("NAME", code)
        demo = p.get("DEMO_LINK", "")
        
        if code == "500A":
            txt = f"📦 *{name}*\n💰 ₹500\n📺 22 Premium Channels"
        elif code == "500B":
            txt = f"📦 *{name}*\n💰 ₹500\n📁 35 Mega Collections"
        else:
            txt = f"💎 *{name}*\n💰 ₹800\n🎁 Both Plans Combined!"
        
        if demo:
            txt += f"\n🎬 [View Demo]({demo})"
        
        await bot.send_message(chat_id, txt, parse_mode="Markdown", disable_web_page_preview=True)
    
    await bot.send_message(chat_id, "👆 Press /start to pay!", reply_markup=main_kb(user_id))

async def cmd_demo(chat_id, user_id):
    txt = "🎬 *Demo Links:*\n\n"
    found = False
    
    for code in ["500A", "500B", "800"]:
        p = get_plan(code)
        name = p.get("NAME", code)
        demo = p.get("DEMO_LINK", "")
        icon = "💎" if code == "800" else "📦"
        
        if demo:
            txt += f"{icon} *{name}:*\n{demo}\n\n"
            found = True
        else:
            txt += f"{icon} *{name}:* Not set\n\n"
    
    await bot.send_message(chat_id, txt if found else "⚠️ No demos yet.", parse_mode="Markdown", 
                          disable_web_page_preview=True, reply_markup=main_kb(user_id))

async def cmd_help(chat_id, user_id):
    txt = (
        "❓ *Help*\n\n"
        "1️⃣ Press /start\n"
        "2️⃣ Choose a plan\n"
        "3️⃣ Pay via UPI/QR\n"
        "4️⃣ Send screenshot with plan code\n"
        "5️⃣ Get your links!\n\n"
        "📞 Issues? Contact admin."
    )
    await bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=main_kb(user_id))

async def handle_payment(chat_id, code):
    p = get_plan(code)
    name = p.get("NAME", code)
    qr = p.get("QR_IMAGE", "")
    upi = config.get("DEFAULT_UPI", "not set")
    price = code.replace("A", "").replace("B", "")
    
    txt = (
        f"💳 *{name}*\n\n"
        f"💰 Amount: *₹{price}*\n"
        f"📱 UPI: `{upi}`\n\n"
        f"✅ Pay via QR or UPI ID\n"
        f"📸 Send screenshot with caption: *{code}*"
    )
    
    if qr:
        try:
            await bot.send_photo(chat_id, photo=qr, caption=txt, parse_mode="Markdown")
            return
        except:
            pass
    
    await bot.send_message(chat_id, txt, parse_mode="Markdown")

async def send_links(user_id, code):
    p = get_plan(code)
    links = p.get("LINKS", [])
    name = p.get("NAME", code)
    
    if not links:
        await bot.send_message(user_id, "⚠️ No links configured. Contact admin.")
        return
    
    txt = (
        f"{'🔐' if code == '500A' else '📁' if code == '500B' else '💎'} "
        f"*Your Links - {name}*\n\n" +
        "\n".join(links) +
        "\n\n⚠️ *AUTO-DELETE IN 120 SECONDS!*\n"
        "📲 *SAVE/JOIN NOW!*"
    )
    
    msg = await bot.send_message(user_id, txt, parse_mode="Markdown", disable_web_page_preview=True)
    schedule_delete(user_id, msg.message_id, 120)
    logger.info(f"✅ Links sent to {user_id} for {code}")

async def handle_screenshot(chat_id, user_id, user, photo_id, caption):
    cap = (caption or "").upper()
    
    code = "500A"
    if "800" in cap:
        code = "800"
    elif "500B" in cap:
        code = "500B"
    
    name = get_plan(code).get("NAME", code)
    admin_id = config.get("ADMIN_ID")
    
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"accept_{user_id}_{code}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")
    ]])
    
    await bot.send_photo(
        admin_id,
        photo=photo_id,
        caption=(
            f"📷 *Payment Received*\n\n"
            f"👤 @{user.username or 'N/A'}\n"
            f"📛 {user.first_name}\n"
            f"🆔 `{user_id}`\n"
            f"📦 *{name}* ({code})"
        ),
        parse_mode="Markdown",
        reply_markup=kb
    )
    
    await bot.send_message(chat_id, "✅ Screenshot sent!\n⏳ Waiting for admin approval...")

# ---------- Admin Handlers ----------
async def admin_manage(chat_id, code):
    ensure_plan(code)
    name = get_plan(code).get("NAME", code)
    await bot.send_message(
        chat_id,
        f"⚙️ *Managing {name} ({code})*\n\nSelect option:",
        parse_mode="Markdown",
        reply_markup=plan_kb(code)
    )

async def admin_view_plan(chat_id, code):
    p = get_plan(code)
    links = p.get("LINKS", [])
    
    txt = (
        f"📋 *Plan {code}*\n\n"
        f"📛 Name: {p.get('NAME', 'N/A')}\n"
        f"🖼️ QR: {'✅' if p.get('QR_IMAGE') else '❌'}\n"
        f"🎬 Demo: {'✅' if p.get('DEMO_LINK') else '❌'}\n"
        f"🔗 Links: {len(links)}\n"
    )
    
    if p.get("DEMO_LINK"):
        txt += f"\n🔗 {p['DEMO_LINK']}"
    
    if links:
        txt += "\n\n📝 *Links:*\n"
        for i, l in enumerate(links[:15], 1):
            txt += f"{i}. {l}\n"
        if len(links) > 15:
            txt += f"...+{len(links)-15} more"
    
    await bot.send_message(chat_id, txt, parse_mode="Markdown", disable_web_page_preview=True)
    
    if p.get("QR_IMAGE"):
        try:
            await bot.send_photo(chat_id, photo=p["QR_IMAGE"], caption="📱 QR Code")
        except:
            pass

async def admin_view_config(chat_id):
    cfg = json.dumps(config, indent=2, ensure_ascii=False)
    if len(cfg) > 3800:
        cfg = cfg[:3800] + "\n..."
    await bot.send_message(chat_id, f"<pre>{cfg}</pre>", parse_mode="HTML")

# ---------- Main Process ----------
async def process_update(update: Update):
    try:
        # Callback Query
        if update.callback_query:
            q = update.callback_query
            await q.answer()
            data = q.data
            chat_id = q.message.chat_id
            
            if data.startswith("pay_"):
                await handle_payment(chat_id, data[4:])
            
            elif data.startswith("accept_"):
                parts = data.split("_")
                uid, code = int(parts[1]), parts[2]
                name = get_plan(code).get("NAME", code)
                
                await bot.send_message(uid, f"🎉 *Payment Accepted!*\n\n📦 {name}\n\n⏳ Sending links...", parse_mode="Markdown")
                await send_links(uid, code)
                await bot.send_message(chat_id, f"✅ Approved! Links sent (auto-delete 120s)")
            
            elif data.startswith("reject_"):
                uid = int(data.split("_")[1])
                await bot.send_message(uid, "❌ Payment rejected. Send valid screenshot.")
                await bot.send_message(chat_id, "❌ Rejected")
            
            return
        
        # Message
        if not update.message:
            return
        
        msg = update.message
        chat_id = msg.chat_id
        user = msg.from_user
        user_id = user.id
        state = get_state(user_id)
        admin = is_admin(user_id)
        
        # Photo
        if msg.photo:
            photo_id = msg.photo[-1].file_id
            
            # Admin setting photos
            if admin and state.get("admin_auth"):
                if state.get("setting_welcome_photo"):
                    config["WELCOME_IMAGE"] = photo_id
                    save_config()
                    state.pop("setting_welcome_photo", None)
                    await bot.send_message(chat_id, "✅ Welcome photo saved!", reply_markup=admin_kb())
                    return
                
                if state.get("setting_qr"):
                    code = state["setting_qr"]
                    ensure_plan(code)
                    config["PLANS"][code]["QR_IMAGE"] = photo_id
                    save_config()
                    state.pop("setting_qr", None)
                    await bot.send_message(chat_id, f"✅ QR saved for {code}!", reply_markup=plan_kb(code))
                    return
            
            # User screenshot
            if not admin:
                await handle_screenshot(chat_id, user_id, user, photo_id, msg.caption)
            return
        
        # Text
        if not msg.text:
            return
        
        text = msg.text.strip()
        
        # ---------- Basic Commands ----------
        if text in ["/start", "🏁 Start"]:
            await cmd_start(chat_id, user_id, user)
            return
        
        if text == "📦 Plans":
            await cmd_plans(chat_id, user_id)
            return
        
        if text == "🎬 Demo":
            await cmd_demo(chat_id, user_id)
            return
        
        if text == "❓ Help":
            await cmd_help(chat_id, user_id)
            return
        
        # ---------- Admin Entry ----------
        if text == "🔧 Admin Panel" and admin:
            if state.get("admin_auth"):
                await bot.send_message(chat_id, "📋 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_kb())
            else:
                state["awaiting_password"] = True
                await bot.send_message(chat_id, "🔐 Enter password:")
            return
        
        # Password
        if state.get("awaiting_password") and admin:
            if text == config.get("ADMIN_PASSWORD"):
                state.pop("awaiting_password", None)
                state["admin_auth"] = True
                await bot.send_message(chat_id, "✅ *Welcome Admin!*", parse_mode="Markdown", reply_markup=admin_kb())
            else:
                state.pop("awaiting_password", None)
                await bot.send_message(chat_id, "❌ Wrong password!", reply_markup=main_kb(user_id))
            return
        
        # ---------- Admin Authenticated ----------
        if state.get("admin_auth") and admin:
            
            # === Input Handlers (FIRST!) ===
            
            if state.get("editing_welcome"):
                config["CUSTOM_CAPTION"] = text
                save_config()
                state.pop("editing_welcome", None)
                await bot.send_message(chat_id, "✅ Welcome text saved!", reply_markup=admin_kb())
                return
            
            if state.get("setting_upi"):
                config["DEFAULT_UPI"] = text
                save_config()
                state.pop("setting_upi", None)
                await bot.send_message(chat_id, f"✅ UPI saved: `{text}`", parse_mode="Markdown", reply_markup=admin_kb())
                return
            
            if state.get("setting_demo"):
                code = state["setting_demo"]
                if not text.startswith("http"):
                    await bot.send_message(chat_id, "⚠️ Send valid URL (http/https)")
                    return
                ensure_plan(code)
                config["PLANS"][code]["DEMO_LINK"] = text
                save_config()
                state.pop("setting_demo", None)
                await bot.send_message(chat_id, f"✅ Demo saved for {code}!", reply_markup=plan_kb(code))
                return
            
            if state.get("setting_name"):
                code = state["setting_name"]
                ensure_plan(code)
                config["PLANS"][code]["NAME"] = text
                save_config()
                state.pop("setting_name", None)
                await bot.send_message(chat_id, f"✅ Name saved: {text}", reply_markup=plan_kb(code))
                return
            
            if state.get("setting_links"):
                code = state["setting_links"]
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                if not lines:
                    await bot.send_message(chat_id, "⚠️ No links found. Send one per line.")
                    return
                ensure_plan(code)
                config["PLANS"][code]["LINKS"] = lines
                save_config()
                state.pop("setting_links", None)
                await bot.send_message(chat_id, f"✅ {len(lines)} links saved for {code}!", reply_markup=plan_kb(code))
                return
            
            # === Navigation ===
            
            if text == "🔙 Exit Admin":
                clear_state(user_id)
                await bot.send_message(chat_id, "👋 Exited admin.", reply_markup=main_kb(user_id))
                return
            
            if text == "🔙 Back":
                clear_input_state(user_id)
                await bot.send_message(chat_id, "📋 Admin Panel", reply_markup=admin_kb())
                return
            
            # === Admin Menu ===
            
            if text == "📦 Manage 500A":
                clear_input_state(user_id)
                await admin_manage(chat_id, "500A")
                return
            
            if text == "📦 Manage 500B":
                clear_input_state(user_id)
                await admin_manage(chat_id, "500B")
                return
            
            if text == "💎 Manage 800":
                clear_input_state(user_id)
                await admin_manage(chat_id, "800")
                return
            
            if text == "🧾 View Config":
                await admin_view_config(chat_id)
                return
            
            if text == "✏️ Edit Welcome":
                state["editing_welcome"] = True
                cur = config.get("CUSTOM_CAPTION", "") or "(default)"
                await bot.send_message(chat_id, f"📝 Send new welcome text:\n\nCurrent:\n{cur}")
                return
            
            if text == "🖼️ Set Welcome Photo":
                state["setting_welcome_photo"] = True
                await bot.send_message(chat_id, "📸 Send welcome photo:")
                return
            
            if text == "💳 Set UPI":
                state["setting_upi"] = True
                cur = config.get("DEFAULT_UPI", "not set")
                await bot.send_message(chat_id, f"💳 Send UPI ID:\n\nCurrent: `{cur}`", parse_mode="Markdown")
                return
            
            # === Plan Actions ===
            
            for code in ["500A", "500B", "800"]:
                if text == f"🖼️ QR {code}":
                    state["setting_qr"] = code
                    await bot.send_message(chat_id, f"📸 Send QR image for {code}:")
                    return
                
                if text == f"🎬 Demo {code}":
                    state["setting_demo"] = code
                    cur = get_plan(code).get("DEMO_LINK", "not set")
                    await bot.send_message(chat_id, f"🔗 Send demo link for {code}:\n\nCurrent: {cur}")
                    return
                
                if text == f"🔗 Links {code}":
                    state["setting_links"] = code
                    cur = len(get_plan(code).get("LINKS", []))
                    await bot.send_message(chat_id, f"📝 Send links for {code} (one per line):\n\nCurrent: {cur} links")
                    return
                
                if text == f"👁️ View {code}":
                    await admin_view_plan(chat_id, code)
                    return
                
                if text == f"📝 Name {code}":
                    state["setting_name"] = code
                    cur = get_plan(code).get("NAME", code)
                    await bot.send_message(chat_id, f"📝 Send new name for {code}:\n\nCurrent: {cur}")
                    return
            
            # Unknown admin command
            await bot.send_message(chat_id, "❓ Use buttons below:", reply_markup=admin_kb())
            return
        
        # ---------- Unknown ----------
        await bot.send_message(chat_id, "❓ Unknown command. Use /start", reply_markup=main_kb(user_id))
        
    except Exception as e:
        logger.exception(f"Process error: {e}")

# ---------- Fast Async Runner ----------
def run_async_fast(coro):
    """Run async code as fast as possible"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# ---------- Flask Routes ----------
@app.route(f"/{config['TOKEN']}", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, bot)
        
        # Process directly without thread pool
        run_async_fast(process_update(update))
        
        return "ok", 200
    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return "ok", 200  # Always return ok to Telegram

@app.route("/")
def index():
    return "✅ Bot Running!"

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "plans": list(config.get("PLANS", {}).keys()),
        "pending_deletions": len(pending_deletions)
    })

@app.route("/reload")
def reload_config():
    load_config()
    return jsonify({"status": "reloaded"})

if __name__ == "__main__":
    import os
    print("🚀 Bot starting...")
    print(f"👤 Admin: {config.get('ADMIN_ID')}")
    print(f"📦 Plans: {list(config.get('PLANS', {}).keys())}")
    print(f"🗑️ Deletion worker: Running")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)

