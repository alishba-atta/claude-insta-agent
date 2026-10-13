#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
 instagram_claude_agent.py  --  "Astra" : FREE Instagram AI Agent (M2M)
=============================================================================
 Install :  pip install instagrapi flask requests
 Run     :  python instagram_claude_agent.py

 Features
   1. Auto-read Instagram DMs every 30 seconds
   2. Auto-reply to DMs using the AI brain (Pollinations, free, no API key)
   3. Auto-read and reply to comments on your posts
   4. Auto-like new comments
   5. Intelligent spam filter (rules + repeat/emoji/link checks)
   6. Session file so you stay logged in
   7. Flask web app for 24/7 hosting on Render.com (0.0.0.0:10000)
   8. Human-like random delays (2-5 seconds)

 Credentials: set environment variables on Render (recommended):
     IG_USERNAME   and   IG_PASSWORD
 or type them in the CONFIG section below for local testing only.

 NOTE: instagrapi uses Instagram's private API. This is unofficial and
 against Instagram's Terms of Service, so a ban/challenge is always possible.
 Use a secondary account first, keep the delays, and don't raise the limits.
=============================================================================
"""

import os
import re
import json
import time
import random
import logging
import threading
from datetime import datetime

import requests
from flask import Flask, jsonify

from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    ChallengeRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    ClientError,
)

# =============================================================================
# CONFIG
# =============================================================================
IG_USERNAME = os.environ.get("IG_USERNAME", "YOUR_USERNAME_HERE")
IG_PASSWORD = os.environ.get("IG_PASSWORD", "YOUR_PASSWORD_HERE")

SESSION_FILE = os.environ.get("SESSION_FILE", "ig_session.json")  # stay logged in
STATE_FILE = os.environ.get("STATE_FILE", "agent_state.json")     # remembers handled items

DM_CHECK_INTERVAL = 30        # seconds between DM checks (as requested)
COMMENT_CHECK_INTERVAL = 90   # seconds between comment sweeps (kept slower = safer)
POSTS_TO_SCAN = 5             # latest N posts scanned for comments
COMMENTS_PER_POST = 20        # latest N comments read per post

MIN_DELAY, MAX_DELAY = 2, 5   # human-like delay range (seconds)

REPLY_TO_OLD_COMMENTS = False  # False = on first run, ignore existing comments
AUTO_DELETE_SPAM_COMMENTS = False  # True = also delete spam comments (optional)

PORT = int(os.environ.get("PORT", 10000))  # Render.com port

# AI brain (FREE Pollinations, OpenAI-compatible, no key)
AI_URL = "https://text.pollinations.ai/openai"
AI_MODEL = "claude"
AI_TIMEOUT = 45

SYSTEM_PROMPT = (
    "You are Astra, my personal Instagram AI manager like Claude. "
    "You handle my Instagram DMs and comments. Be professional, concise, "
    "sales-focused, reply in Roman Urdu/English mix as user. You are M2M agent."
)

FALLBACK_DM = "Shukriya for your message! Main jaldi detail share karti/karta hoon. 😊"
FALLBACK_COMMENT = "Thank you so much! 💛"

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("astra")

# =============================================================================
# GLOBAL STATE
# =============================================================================
cl = Client()
cl.delay_range = [MIN_DELAY, MAX_DELAY]  # instagrapi built-in request delay too

state_lock = threading.Lock()
state = {
    "dm_done": [],           # DM message ids already answered
    "comment_done": [],      # comment pks already handled
    "baselined": False,      # first-run baseline for comments done?
}
stats = {
    "started": datetime.utcnow().isoformat() + "Z",
    "logged_in": False,
    "dm_replied": 0,
    "comments_replied": 0,
    "comments_liked": 0,
    "spam_blocked": 0,
    "last_dm_check": None,
    "last_comment_check": None,
    "last_error": None,
}
dm_history = {}  # thread_id -> short conversation memory for the AI
MY_USER_ID = None


# =============================================================================
# HELPERS
# =============================================================================
def human_delay():
    """Random 2-5 second pause to look human."""
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            state.update(loaded)
        except Exception as e:
            log.warning("Could not read state file: %s", e)


def save_state():
    with state_lock:
        # keep files small
        state["dm_done"] = state["dm_done"][-3000:]
        state["comment_done"] = state["comment_done"][-5000:]
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception as e:
            log.warning("Could not save state: %s", e)


def mark_done(kind, item_id):
    with state_lock:
        if str(item_id) not in state[kind]:
            state[kind].append(str(item_id))


def is_done(kind, item_id):
    return str(item_id) in state[kind]


# =============================================================================
# SPAM FILTER
# =============================================================================
SPAM_KEYWORDS = [
    "follow4follow", "f4f", "l4l", "like4like", "followback", "follow back",
    "free followers", "buy followers", "cheap followers", "get followers",
    "crypto", "bitcoin", "forex", "binary option", "investment plan",
    "earn money", "make money fast", "earn $", "work from home and earn",
    "dm for collab", "dm me for promo", "promo for you", "check my bio",
    "check my profile", "click my bio", "link in my bio", "onlyfans",
    "18+", "sugar daddy", "casino", "betting", "giveaway winner",
    "you won", "claim your prize", "whatsapp me", "telegram me",
    "t.me/", "wa.me/", "bit.ly/", "tinyurl", "shorturl",
]
URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
PHONE_RE = re.compile(r"(\+?\d[\d\s\-]{9,}\d)")
REPEAT_CHAR_RE = re.compile(r"(.)\1{7,}")
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]"
)


def is_spam(text: str):
    """Return (True, reason) if the text looks like spam, else (False, '')."""
    if not text or not text.strip():
        return True, "empty"
    t = text.lower().strip()

    for kw in SPAM_KEYWORDS:
        if kw in t:
            return True, f"keyword:{kw}"
    if URL_RE.search(t):
        return True, "link"
    if PHONE_RE.search(t):
        return True, "phone-number"
    if REPEAT_CHAR_RE.search(t):
        return True, "repeated-chars"
    if len(EMOJI_RE.findall(t)) > 12:
        return True, "emoji-flood"
    if len(t) > 700:
        return True, "too-long"
    # mostly one repeated word, e.g. "nice nice nice nice nice"
    words = t.split()
    if len(words) >= 6 and len(set(words)) <= max(1, len(words) // 4):
        return True, "repeated-words"
    return False, ""


# =============================================================================
# AI BRAIN  (FREE Pollinations)
# =============================================================================
def ask_ai(user_message: str, history=None, max_chars=500, retries=2):
    """Send a message to the free Pollinations API and return clean text."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history[-6:])
    messages.append({"role": "user", "content": user_message})

    payload = {"model": AI_MODEL, "messages": messages}

    for attempt in range(retries + 1):
        try:
            r = requests.post(
                AI_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=AI_TIMEOUT,
            )
            r.raise_for_status()
            try:
                data = r.json()
                text = data["choices"][0]["message"]["content"]
            except Exception:
                text = r.text  # some responses come back as plain text
            text = (text or "").strip()
            if text:
                return text[:max_chars]
        except Exception as e:
            log.warning("AI error (attempt %d): %s", attempt + 1, e)
            time.sleep(2 + attempt * 2)
    return None


# =============================================================================
# LOGIN (with session file)
# =============================================================================
def login():
    """Login using saved session if possible, otherwise fresh login."""
    global MY_USER_ID

    if IG_USERNAME == "YOUR_USERNAME_HERE" or IG_PASSWORD == "YOUR_PASSWORD_HERE":
        raise RuntimeError(
            "Set IG_USERNAME and IG_PASSWORD (env vars or in the CONFIG section)."
        )

    logged = False

    if os.path.exists(SESSION_FILE):
        try:
            log.info("Loading saved session...")
            session = cl.load_settings(SESSION_FILE)
            cl.set_settings(session)
            cl.login(IG_USERNAME, IG_PASSWORD)  # reuses session if valid
            cl.get_timeline_feed()              # cheap check that session works
            logged = True
            log.info("Logged in with saved session.")
        except LoginRequired:
            log.warning("Session expired, doing fresh login...")
            old_uuids = cl.get_settings().get("uuids")
            cl.set_settings({})
            if old_uuids:
                cl.set_uuids(old_uuids)  # keep same device = less suspicious
        except Exception as e:
            log.warning("Saved session failed (%s). Fresh login...", e)
            cl.set_settings({})

    if not logged:
        log.info("Logging in with username/password...")
        cl.login(IG_USERNAME, IG_PASSWORD)
        logged = True

    cl.dump_settings(SESSION_FILE)
    MY_USER_ID = str(cl.user_id)
    stats["logged_in"] = True
    log.info("Logged in as %s (id %s)", IG_USERNAME, MY_USER_ID)


def safe_relogin():
    try:
        log.info("Re-logging in...")
        login()
        return True
    except Exception as e:
        stats["last_error"] = f"relogin failed: {e}"
        log.error("Re-login failed: %s", e)
        return False


# =============================================================================
# DM HANDLER
# =============================================================================
def handle_dms():
    """Read unread DM threads and reply to the latest message in each."""
    stats["last_dm_check"] = datetime.utcnow().isoformat() + "Z"

    try:
        threads = cl.direct_threads(amount=20, selected_filter="unread")
    except Exception:
        raise

    # Also accept message requests (pending inbox) so new customers get a reply
    try:
        pending = cl.direct_pending_inbox(amount=10)
        threads = list(threads) + list(pending)
    except Exception as e:
        log.debug("Pending inbox skipped: %s", e)

    for thread in threads:
        if not thread.messages:
            continue

        msg = thread.messages[0]  # newest message
        if str(msg.user_id) == MY_USER_ID:
            continue  # last message is mine, nothing to answer
        if is_done("dm_done", msg.id):
            continue

        text = (msg.text or "").strip()
        sender = thread.users[0].username if thread.users else "user"

        # Non-text DMs (photos, reels, voice...) -> short polite reply
        if not text:
            text = "[user sent a non-text message like a photo/reel/voice note]"

        spam, reason = is_spam(text) if not text.startswith("[user sent") else (False, "")
        if spam:
            log.info("DM from @%s blocked as spam (%s)", sender, reason)
            stats["spam_blocked"] += 1
            mark_done("dm_done", msg.id)
            continue

        log.info("DM from @%s: %s", sender, text[:80])

        history = dm_history.setdefault(str(thread.id), [])
        reply = ask_ai(
            f"Instagram DM from @{sender}: {text}\n"
            "Reply as my assistant. Keep it short, helpful and sales-focused.",
            history=history,
            max_chars=500,
        ) or FALLBACK_DM

        human_delay()  # "reading" time
        try:
            cl.direct_answer(thread.id, reply)
            stats["dm_replied"] += 1
            log.info("Replied to @%s: %s", sender, reply[:80])
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            del history[:-10]
        except Exception as e:
            log.error("Failed to send DM reply: %s", e)
            stats["last_error"] = f"dm send: {e}"
            continue

        mark_done("dm_done", msg.id)
        save_state()
        human_delay()


# =============================================================================
# COMMENT HANDLER
# =============================================================================
def handle_comments():
    """Scan latest posts, like new comments, and reply to them."""
    stats["last_comment_check"] = datetime.utcnow().isoformat() + "Z"

    medias = cl.user_medias(int(MY_USER_ID), amount=POSTS_TO_SCAN)
    first_run = not state.get("baselined", False)

    for media in medias:
        try:
            comments = cl.media_comments(media.id, amount=COMMENTS_PER_POST)
        except Exception as e:
            log.warning("Could not read comments for %s: %s", media.pk, e)
            continue

        caption = (media.caption_text or "")[:200]

        for c in comments:
            cpk = str(c.pk)
            if is_done("comment_done", cpk):
                continue
            if str(c.user.pk) == MY_USER_ID:
                mark_done("comment_done", cpk)
                continue

            # First run: just remember old comments, don't spam old posts
            if first_run and not REPLY_TO_OLD_COMMENTS:
                mark_done("comment_done", cpk)
                continue

            username = c.user.username
            text = (c.text or "").strip()

            # ---- spam filter ----
            spam, reason = is_spam(text)
            if spam:
                log.info("Comment by @%s flagged as spam (%s): %s",
                         username, reason, text[:50])
                stats["spam_blocked"] += 1
                if AUTO_DELETE_SPAM_COMMENTS:
                    try:
                        cl.comment_bulk_delete(media.id, [c.pk])
                    except Exception as e:
                        log.warning("Could not delete spam comment: %s", e)
                mark_done("comment_done", cpk)
                human_delay()
                continue

            log.info("Comment by @%s: %s", username, text[:80])

            # ---- auto-like ----
            human_delay()
            try:
                cl.comment_like(c.pk)
                stats["comments_liked"] += 1
            except Exception as e:
                log.warning("Like failed: %s", e)

            # ---- AI reply ----
            reply = ask_ai(
                f"Public comment from @{username} on my post "
                f'(caption: "{caption}"): "{text}"\n'
                "Write a short public reply (max 25 words). Friendly, "
                "professional, sales-focused. If they ask price/details, "
                "tell them to DM us. Start with @" + username,
                max_chars=220,
            ) or f"@{username} {FALLBACK_COMMENT}"

            human_delay()
            try:
                cl.media_comment(media.id, reply, replied_to_comment_id=c.pk)
                stats["comments_replied"] += 1
                log.info("Replied to @%s: %s", username, reply[:80])
            except Exception as e:
                log.error("Comment reply failed: %s", e)
                stats["last_error"] = f"comment reply: {e}"

            mark_done("comment_done", cpk)
            save_state()
            human_delay()

    if first_run:
        state["baselined"] = True
    save_state()


# =============================================================================
# MAIN BOT LOOP
# =============================================================================
def run_safely(fn, name):
    """Run a handler with robust error handling. Returns extra seconds to wait."""
    try:
        fn()
        return 0
    except LoginRequired:
        log.warning("%s: login required, re-authenticating", name)
        safe_relogin()
        return 10
    except ChallengeRequired:
        stats["last_error"] = "Instagram challenge required - verify login in the app"
        log.error("Challenge required! Open Instagram app and approve the login.")
        return 600
    except (PleaseWaitFewMinutes, RateLimitError) as e:
        stats["last_error"] = f"rate limited: {e}"
        log.error("Rate limited. Cooling down 10 min.")
        return 600
    except ClientError as e:
        stats["last_error"] = f"{name}: {e}"
        log.error("%s client error: %s", name, e)
        return 60
    except Exception as e:
        stats["last_error"] = f"{name}: {e}"
        log.exception("%s unexpected error: %s", name, e)
        return 30


def bot_loop():
    """Background thread: DMs every 30s, comments every COMMENT_CHECK_INTERVAL."""
    # initial login (retry until it works)
    while True:
        try:
            login()
            break
        except ChallengeRequired:
            stats["last_error"] = "Challenge required at login - approve in app"
            log.error("Challenge at login. Retrying in 10 min.")
            time.sleep(600)
        except Exception as e:
            stats["last_error"] = f"login: {e}"
            log.error("Login failed: %s. Retrying in 2 min.", e)
            time.sleep(120)

    load_state()
    last_comment_run = 0

    log.info("Astra is live. Watching DMs and comments...")
    while True:
        extra = run_safely(handle_dms, "DMs")

        if time.time() - last_comment_run >= COMMENT_CHECK_INTERVAL:
            extra += run_safely(handle_comments, "comments")
            last_comment_run = time.time()

        time.sleep(DM_CHECK_INTERVAL + extra)


# =============================================================================
# FLASK APP  (keeps the service alive 24/7 on Render.com)
# =============================================================================
app = Flask(__name__)


@app.route("/")
def home():
    return (
        "<h2>Astra - Instagram AI Agent is running ✅</h2>"
        "<p>Open <a href='/status'>/status</a> to see live stats.</p>"
    )


@app.route("/health")
def health():
    return "OK", 200


@app.route("/status")
def status():
    return jsonify(stats)


def start_bot_thread():
    t = threading.Thread(target=bot_loop, daemon=True, name="astra-bot")
    t.start()


# Start bot when imported by gunicorn too (Render start command: gunicorn ...)
_started = False


def ensure_started():
    global _started
    if not _started:
        _started = True
        start_bot_thread()


ensure_started()

if __name__ == "__main__":
    # host 0.0.0.0, port 10000 for Render.com
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
