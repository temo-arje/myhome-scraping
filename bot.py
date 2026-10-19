import json
import os
import re
import sqlite3
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import asyncio
from threading import RLock

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

LINKS_LOCK = RLock()
NAME, URL, PAGES, FIRST_RUN, FIRST_RUN_LIMIT = range(5)


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LINKS_FILE = "links.json"
DATABASE = "listings.db"
CHECK_INTERVAL = 60  # წამებში

# განცხადების ლინკის ფორმატი: /udzravi-qoneba/<slug>-<ID>/
# (საძიებო გვერდის ლინკი /udzravi-qoneba/qiravdeba/bina/... ამას არ ემთხვევა)
DEFAULT_LINK_REGEX = r"^/udzravi-qoneba/[^/?#]+-(\d{6,})/?$"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ka,en;q=0.8",
}

def load_searches():
    """links.json-ის უსაფრთხოდ წაკითხვა."""
    with LINKS_LOCK:
        with open(LINKS_FILE, encoding="utf-8") as f:
            data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("links.json უნდა შეიცავდეს JSON მასივს.")

    return data



def save_searches(searches):
    """JSON-ის დროებით ფაილში ჩაწერა და უსაფრთხოდ ჩანაცვლება."""
    with LINKS_LOCK:
        temp_file = LINKS_FILE + ".tmp"

        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(
                    searches,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_file, LINKS_FILE)

        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)
# ---------------------------------------------------------- telegram menu ----

def is_authorized(update: Update) -> bool:
    """ბოტის მართვა მხოლოდ კონფიგურირებულმა ჩატმა შეძლოს."""
    return (
        update.effective_chat is not None
        and str(update.effective_chat.id) == str(CHAT_ID)
    )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return ConversationHandler.END

    keyboard = [
        [
            InlineKeyboardButton(
                "➕ ლინკის დამატება",
                callback_data="add",
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 ლინკების სია",
                callback_data="list",
            ),
        ],
        [
            InlineKeyboardButton(
                "🗑️ ლინკის წაშლა",
                callback_data="delete_menu",
            ),
        ],
    ]

    await update.effective_message.reply_text(
        "🏠 MyHome განცხადებების მონიტორინგი\n\n"
        "აირჩიე სასურველი მოქმედება:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    return ConversationHandler.END


async def start_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_authorized(update):
        await query.answer("წვდომა აკრძალულია.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    context.user_data.clear()

    await query.message.reply_text(
        "➕ ახალი ძიების დამატება\n\n"
        "შეიყვანე ძიების სახელი.\n\n"
        "მაგალითად:\n"
        "დიდი დიღომი - არჩილ მების ქუჩა\n\n"
        "გასაუქმებლად გამოიყენე /cancel"
    )

    return NAME


async def receive_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    name = update.message.text.strip()

    if not name or len(name) > 150:
        await update.message.reply_text(
            "სახელი ცარიელი არ უნდა იყოს და 150 სიმბოლოს არ უნდა აღემატებოდეს."
        )
        return NAME

    context.user_data["search_name"] = name

    await update.message.reply_text(
        "ახლა გამომიგზავნე საძიებო გვერდის სრული URL.\n\n"
        "მაგალითად:\n"
        "https://www.myhome.ge/..."
    )

    return URL


async def receive_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    url = update.message.text.strip()

    parsed = urlparse(url)

    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "myhome.ge"
        and not parsed.hostname.lower().endswith(".myhome.ge")
    ):
        await update.message.reply_text(
            "❌ არასწორი მისამართია.\n"
            "გამომიგზავნე MyHome.ge-ს HTTPS საძიებო გვერდის სრული URL."
        )
        return URL

    context.user_data["search_url"] = url

    await update.message.reply_text(
        "რამდენი გვერდი შევამოწმო?\n\n"
        "გამომიგზავნე დადებითი მთელი რიცხვი, მაგალითად: 3"
    )

    return PAGES


async def receive_pages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        pages = int(update.message.text.strip())

        if not 1 <= pages <= 100:
            raise ValueError

    except ValueError:
        await update.message.reply_text(
            "შეიყვანე რიცხვი 1-დან 100-მდე."
        )
        return PAGES

    context.user_data["search_pages"] = pages

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ დიახ",
                callback_data="first_yes",
            ),
            InlineKeyboardButton(
                "❌ არა",
                callback_data="first_no",
            ),
        ]
    ]

    await update.message.reply_text(
        "პირველივე შემოწმებისას არსებული განცხადებებიდან "
        "ბოლო განცხადებები გამოგიგზავნო?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    return FIRST_RUN


async def receive_first_run(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_authorized(update):
        await query.answer("წვდომა აკრძალულია.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    if query.data == "first_yes":
        context.user_data["send_on_first_run"] = True

        await query.message.reply_text(
            "რამდენი განცხადება გამოგიგზავნო პირველ გაშვებაზე?\n\n"
            "მაგალითად: 10"
        )

        return FIRST_RUN_LIMIT

    context.user_data["send_on_first_run"] = False
    context.user_data["first_run_limit"] = 10

    return await finish_add(query, context)


async def receive_first_run_limit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        limit = int(update.message.text.strip())

        if not 1 <= limit <= 100:
            raise ValueError

    except ValueError:
        await update.message.reply_text(
            "შეიყვანე რიცხვი 1-დან 100-მდე."
        )
        return FIRST_RUN_LIMIT

    context.user_data["first_run_limit"] = limit

    searches = load_searches()

    url = context.user_data["search_url"]

    if any(s.get("url") == url for s in searches):
        await update.message.reply_text(
            "⚠️ ეს URL უკვე დამატებულია."
        )
        context.user_data.clear()
        return ConversationHandler.END

    search = {
        "name": context.user_data["search_name"],
        "url": url,
        "pages": context.user_data["search_pages"],
        "send_on_first_run": context.user_data["send_on_first_run"],
        "first_run_limit": limit,
    }

    searches.append(search)
    save_searches(searches)

    await update.message.reply_text(
        "✅ ძიება წარმატებით დაემატა!\n\n"
        f"📌 სახელი: {search['name']}\n"
        f"📄 გვერდები: {search['pages']}\n"
        f"📨 პირველ გაშვებაზე გაგზავნა: "
        f"{'დიახ' if search['send_on_first_run'] else 'არა'}\n"
        f"🔢 პირველი გაგზავნის ლიმიტი: {search['first_run_limit']}"
    )

    context.user_data.clear()

    return ConversationHandler.END


async def finish_add(query, context):
    """ინახავს ძიებას, როდესაც პირველი გაშვების შეტყობინებები გამორთულია."""
    searches = load_searches()

    url = context.user_data["search_url"]

    if any(s.get("url") == url for s in searches):
        await query.message.reply_text(
            "⚠️ ეს URL უკვე დამატებულია."
        )
        context.user_data.clear()
        return ConversationHandler.END

    search = {
        "name": context.user_data["search_name"],
        "url": url,
        "pages": context.user_data["search_pages"],
        "send_on_first_run": False,
        "first_run_limit": 10,
    }

    searches.append(search)
    save_searches(searches)

    await query.message.reply_text(
        f"✅ ძიება დაემატა!\n\n"
        f"📌 {search['name']}\n"
        f"📄 გვერდები: {search['pages']}\n\n"
        "არსებული განცხადებები პირველ შემოწმებაზე "
        "ჩუმად შეინახება."
    )

    context.user_data.clear()

    return ConversationHandler.END


async def cancel_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        return ConversationHandler.END

    context.user_data.clear()

    await update.message.reply_text(
        "❌ დამატება გაუქმებულია."
    )

    return ConversationHandler.END


async def list_searches(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_authorized(update):
        await query.answer("წვდომა აკრძალულია.", show_alert=True)
        return

    await query.answer()

    searches = load_searches()

    if not searches:
        await query.message.reply_text(
            "📋 ჯერ არცერთი ძიება არ არის დამატებული."
        )
        return

    for index, search in enumerate(searches):
        message = (
            f"📌 {search.get('name', 'უსახელო')}\n\n"
            f"🔗 {search['url']}\n"
            f"📄 გვერდები: {search.get('pages', 1)}\n"
            f"📨 პირველ გაშვებაზე გაგზავნა: "
            f"{'დიახ' if search.get('send_on_first_run', False) else 'არა'}"
        )

        await query.message.reply_text(message)


async def delete_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_authorized(update):
        await query.answer("წვდომა აკრძალულია.", show_alert=True)
        return

    await query.answer()

    searches = load_searches()

    if not searches:
        await query.message.reply_text(
            "წასაშლელი ძიებები არ არის."
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                f"🗑️ {search.get('name', 'უსახელო')[:50]}",
                callback_data=f"delete:{index}",
            )
        ]
        for index, search in enumerate(searches)
    ]

    await query.message.reply_text(
        "აირჩიე წასაშლელი ძიება:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_authorized(update):
        await query.answer("წვდომა აკრძალულია.", show_alert=True)
        return

    await query.answer()

    index = int(query.data.split(":", 1)[1])

    searches = load_searches()

    if index < 0 or index >= len(searches):
        await query.message.reply_text(
            "❌ ძიება ვერ მოიძებნა. გახსენი მენიუ თავიდან."
        )
        return

    removed = searches.pop(index)

    save_searches(searches)

    await query.message.reply_text(
        f"✅ წაიშალა: {removed.get('name', 'უსახელო')}"
    )

# ------------------------------------------------------------ config ----
def load_searches():
    """links.json ყოველ ციკლზე ხელახლა იკითხება: ბოტის გადატვირთვა არ სჭირდება."""
    with open(LINKS_FILE, encoding="utf-8") as f:
        return json.load(f)


def with_page(url, page):
    """URL-ში page პარამეტრის ჩანაცვლება."""
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    q["page"] = str(page)
    return urlunparse(p._replace(query=urlencode(q)))


# ---------------------------------------------------------- telegram ----
def send_telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


# ---------------------------------------------------------------- db ----
def setup_database():
    with sqlite3.connect(DATABASE) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS seen_listings (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                search TEXT NOT NULL
            )
        """)


def known_ids():
    with sqlite3.connect(DATABASE) as c:
        return {r[0] for r in c.execute("SELECT id FROM seen_listings")}


def search_has_rows(name):
    with sqlite3.connect(DATABASE) as c:
        return c.execute(
            "SELECT 1 FROM seen_listings WHERE search=? LIMIT 1", (name,)
        ).fetchone() is not None


def save_listing(listing_id, url, title, search_name):
    with sqlite3.connect(DATABASE) as c:
        c.execute(
            "INSERT OR IGNORE INTO seen_listings (id, url, title, search) VALUES (?,?,?,?)",
            (listing_id, url, title, search_name),
        )


# ---------------------------------------------------------- scraping ----
NOISE = re.compile(r"(ფოტო|^building|^VIP$|^SUPER|^/udzravi)", re.I)
PRICE = re.compile(r"^[\d,\.\s]+\s*[₾$€]$")
AREA = re.compile(r"^\d+([.,]\d+)?\s*მ²$")
DATE = re.compile(r"\d{1,2}\s+\S+\.?,\s*\d{1,2}:\d{2}")


def describe_card(card):
    """ბარათის ტექსტიდან მოკლე, წასაკითხი აღწერა."""
    parts = []
    for s in card.stripped_strings:
        s = s.strip()
        if s and not NOISE.search(s) and s not in parts:
            parts.append(s)

    price = next((p for p in parts if PRICE.match(p)), "")
    per_m2 = next((p for p in parts if "/მ²" in p), "")
    area = next((p for p in parts if AREA.match(p)), "")
    date = next((p for p in parts if DATE.search(p)), "")
    title = next((p for p in parts if re.search(r"ქირავდება|იყიდება|გირავდება", p)), "")

    used = {price, per_m2, area, date, title}
    rest = [p for p in parts if p not in used]

    head = " | ".join(x for x in [title, price, per_m2, area] if x)
    tail = " · ".join(rest[:6])
    lines = [head or "ახალი განცხადება"]
    if tail:
        lines.append(tail)  # ოთახები / საძინებლები / სართული / მისამართი
    if date:
        lines.append(f"🕒 {date}")
    return "\n".join(lines)[:500]


def card_for(a_tag, link_re, listing_id):
    """ავდივართ ზემოთ, სანამ მშობელი მხოლოდ ამ ერთ განცხადებას შეიცავს."""
    node = a_tag
    for _ in range(6):
        parent = node.parent
        if parent is None:
            break
        ids = set()
        for a in parent.find_all("a", href=True):
            m = link_re.search(a["href"])
            if m:
                ids.add(m.group(1))
        if ids - {listing_id}:
            break
        node = parent
    return node


def parse_page(page_html, page_url, link_re):
    """აბრუნებს {id: {"url":..., "title":...}} ერთი გვერდიდან."""
    soup = BeautifulSoup(page_html, "html.parser")
    found = {}
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        path = urlparse(urljoin(page_url, href)).path
        m = link_re.search(path)
        if not m:
            continue
        lid = m.group(1)
        if lid in found:
            continue
        url = urljoin(page_url, path)
        found[lid] = {"url": url, "title": describe_card(card_for(a, link_re, lid))}
    return found


def get_listings(search, session):
    """გადის გვერდებს 1-დან pages-ის ჩათვლით და აბრუნებს {id: item}."""
    link_re = re.compile(search.get("link_regex", DEFAULT_LINK_REGEX))
    pages = int(search.get("pages", 1))
    results = {}

    for page in range(1, pages + 1):
        page_url = with_page(search["url"], page)
        response = session.get(page_url, headers=HEADERS, timeout=30)
        print(f"  გვერდი {page}: HTTP {response.status_code}, {len(response.content)} ბაიტი")
        response.raise_for_status()

        items = parse_page(response.text, page_url, link_re)
        new_here = {k: v for k, v in items.items() if k not in results}
        print(f"  გვერდი {page}: {len(items)} განცხადება ({len(new_here)} ახალი ამ გაშვებაში)")

        if not items:
            with open("last_page_debug.html", "w", encoding="utf-8") as f:
                f.write(response.text)
            print("  ⚠ 0 განცხადება. HTML შენახულია last_page_debug.html-ში")
            break
        if not new_here:  # ბოლო გვერდს მიღმა საიტი ძველ გვერდს აბრუნებს -> ვჩერდებით
            break

        results.update(new_here)
        time.sleep(2)

    return results


# -------------------------------------------------------------- main ----
def check_site():
    session = requests.Session()
    seen = known_ids()  # ბაზა + ამ ციკლში დამუშავებული: დუბლიკატი ვერ გამეორდება

    for search in load_searches():
        name = search.get("name", search["url"][:40])
        print(f"\n[{name}] ვამოწმებ...")

        try:
            listings = get_listings(search, session)
        except Exception as error:
            print(f"  შეცდომა: {error}")
            continue

        print(f"  სულ მიღებულია {len(listings)} განცხადება.")
        if not listings:
            continue

        first_run = not search_has_rows(name)
        new = {i: it for i, it in listings.items() if i not in seen}
        print(f"  ახალი: {len(new)}")

        to_send = sorted(new, key=int)  # ძველიდან ახლისკენ

        if first_run:
            if search.get("send_on_first_run", False):
                # გავაგზავნოთ მხოლოდ ბოლო N (first_run_limit), დანარჩენი ჩუმად შევინახოთ
                limit = int(search.get("first_run_limit", 0)) or len(to_send)
                silent = to_send[:-limit] if limit < len(to_send) else []
                to_send = to_send[len(silent):]
                print(f"  პირველი გაშვებაა: ვაგზავნი {len(to_send)}-ს, ჩუმად ვინახავ {len(silent)}-ს.")
                for i in silent:
                    save_listing(i, new[i]["url"], new[i]["title"], name)
                    seen.add(i)
            else:
                print("  პირველი გაშვებაა: არსებული განცხადებები შეტყობინების გარეშე შეინახება "
                      '(გასაგზავნად links.json-ში დაამატე "send_on_first_run": true).')
                for i, it in listings.items():
                    save_listing(i, it["url"], it["title"], name)
                    seen.add(i)
                continue

        for i in to_send:
            it = new[i]
            message = f"🏠 {name}\n\n📌 {it['title']}\n\n🔗 {it['url']}"
            try:
                send_telegram(message)
                save_listing(i, it["url"], it["title"], name)
                seen.add(i)
                print(f"  ✅ გაიგზავნა: {i}")
                time.sleep(1)
            except Exception as error:
                print(f"  ❌ გაგზავნა ვერ მოხერხდა: {error}")


async def periodic_checker():
    """საიტების შემოწმება ფონურ ამოცანაში."""
    while True:
        try:
            await asyncio.to_thread(check_site)
        except Exception as error:
            print(f"შემოწმების შეცდომა: {error}")

        print(f"\nშემდეგი შემოწმება {CHECK_INTERVAL} წამში.")
        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app: Application):
    """ბოტის გაშვებისას მენიუს შექმნა."""
    await app.bot.set_my_commands([
        ("start", "მთავარი მენიუ"),
        ("cancel", "მოქმედების გაუქმება"),
    ])


async def main_async():
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "შეამოწმე .env ფაილში TELEGRAM_BOT_TOKEN და TELEGRAM_CHAT_ID."
        )

    setup_database()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_add, pattern="^add$"),
        ],
        states={
            NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name),
            ],
            URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url),
            ],
            PAGES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pages),
            ],
            FIRST_RUN: [
                CallbackQueryHandler(
                    receive_first_run,
                    pattern="^first_(yes|no)$",
                ),
            ],
            FIRST_RUN_LIMIT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_first_run_limit,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_add),
        ],
        per_chat=True,
        per_user=True,
    )

    app.add_handler(conversation)
    app.add_handler(CommandHandler("start", show_menu))
    app.add_handler(CallbackQueryHandler(list_searches, pattern="^list$"))
    app.add_handler(
        CallbackQueryHandler(delete_menu, pattern="^delete_menu$")
    )
    app.add_handler(
        CallbackQueryHandler(delete_search, pattern=r"^delete:\d+$")
    )

    async with app:
        await app.start()
        await app.updater.start_polling()

        try:
            # საწყისი შეტყობინება
            send_telegram(
                "✅ MyHome ბოტი გაეშვა!\n"
                "გამოიყენე /start მენიუს გასახსნელად."
            )

            # საიტების შემოწმება პარალელურად
            await periodic_checker()

        finally:
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main_async())