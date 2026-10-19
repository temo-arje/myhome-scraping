import json
import os
import re
import sqlite3
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

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


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("შეამოწმე .env ფაილში TELEGRAM_BOT_TOKEN და TELEGRAM_CHAT_ID.")

    setup_database()

    try:
        send_telegram("✅ MyHome ბოტი გაეშვა და Telegram-თან კავშირი მუშაობს.")
    except Exception as error:
        raise RuntimeError(f"Telegram-ზე გაგზავნა ვერ მოხერხდა (შეამოწმე TOKEN/CHAT_ID): {error}")

    while True:
        try:
            check_site()
        except Exception as error:
            print(f"შეცდომა: {error}")
        print(f"\nშემდეგი შემოწმება {CHECK_INTERVAL} წამში.")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()