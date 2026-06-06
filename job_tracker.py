"""
JobTracker - Career page monitor with Claude/Grok AI experience extraction
Monitors company career pages for new technical job postings (SWE, AI/ML, Data, etc.)
in the United States with ≤ 4 years experience, and sends instant email notifications.
"""

import os
import re
import json
import time
import sqlite3
import smtplib
import logging
import hashlib
import argparse
import schedule
import platform
import subprocess
import configparser
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urljoin, urlparse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─── Optional AI clients ───────────────────────────────────────────────────────
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from openai import OpenAI
    HAS_OPENAI_SDK = True
except ImportError:
    HAS_OPENAI_SDK = False

# ─── Logging ───────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
Path("state").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/tracker.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def load_config(path="config.ini") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    env_map = {
        ("tracker", "poll_interval_minutes"):   "POLL_INTERVAL_MINUTES",
        ("tracker", "max_experience_years"):    "MAX_EXPERIENCE_YEARS",
        ("tracker", "delay_between_requests"):  "DELAY_BETWEEN_REQUESTS",
        ("ai",      "provider"):                "AI_PROVIDER",
        ("ai",      "claude_api_key"):          "CLAUDE_API_KEY",
        ("ai",      "grok_api_key"):            "GROK_API_KEY",
        ("email",   "smtp_host"):               "SMTP_HOST",
        ("email",   "smtp_port"):               "SMTP_PORT",
        ("email",   "smtp_user"):               "SMTP_USER",
        ("email",   "smtp_password"):           "SMTP_PASSWORD",
        ("email",   "to_address"):              "EMAIL_TO",
        ("email",   "resend_api_key"):          "RESEND_API_KEY",
    }
    for section, _ in env_map:
        if not cfg.has_section(section):
            cfg.add_section(section)
    if Path(path).exists():
        cfg.read(path)
        log.info(f"Config loaded from {path}")
    else:
        log.info("No config.ini found — reading from environment variables")
    for (section, key), env_var in env_map.items():
        val = os.environ.get(env_var, "").strip()
        if val:
            if not cfg.has_section(section):
                cfg.add_section(section)
            cfg.set(section, key, val)
    return cfg


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

class DB:
    def __init__(self, path="state/jobs.sqlite3"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                company   TEXT NOT NULL,
                url_hash  TEXT NOT NULL,
                url       TEXT NOT NULL,
                seen_at   TEXT NOT NULL,
                PRIMARY KEY (company, url_hash)
            );
            CREATE TABLE IF NOT EXISTS job_details (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                company     TEXT NOT NULL,
                url         TEXT NOT NULL,
                title       TEXT,
                min_years   REAL,
                raw_text    TEXT,
                notified    INTEGER DEFAULT 0,
                found_at    TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def known_urls(self, company: str) -> set:
        rows = self.conn.execute(
            "SELECT url_hash FROM snapshots WHERE company=?", (company,)
        ).fetchall()
        return {r["url_hash"] for r in rows}

    def save_urls(self, company: str, urls: list[str]):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            "INSERT OR IGNORE INTO snapshots (company, url_hash, url, seen_at) VALUES (?,?,?,?)",
            [(company, _hash(u), u, now) for u in urls],
        )
        self.conn.commit()

    def save_job(self, company, url, title, min_years, raw_text):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT OR IGNORE INTO job_details
               (company, url, title, min_years, raw_text, found_at)
               VALUES (?,?,?,?,?,?)""",
            (company, url, title, min_years, raw_text, now),
        )
        self.conn.commit()

    def pending_notifications(self, max_exp: float = 4.0) -> list:
        return self.conn.execute(
            "SELECT * FROM job_details WHERE notified=0 AND (min_years IS NULL OR min_years <= ?)",
            (max_exp,),
        ).fetchall()

    def mark_notified(self, job_id: int):
        self.conn.execute("UPDATE job_details SET notified=1 WHERE id=?", (job_id,))
        self.conn.commit()


def _hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPER
# ══════════════════════════════════════════════════════════════════════════════

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_page(url: str, timeout=20) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning(f"fetch_page failed for {url}: {e}")
        return None


# Pages that are definitely NOT individual job postings
NOISE_URL_PATTERNS = re.compile(
    r"/(about|faq|benefits|workplace|locations|diversity|culture|"
    r"saved|alerts|recommendations|search|signin|login|signup|"
    r"legal|privacy|terms|contact|blog|news|press|events|"
    r"leadership|principles|our-team|life-at)(/|$)",
    re.I,
)

NOISE_LINK_TEXT = re.compile(
    r"^(our workplace|leadership principles|faq|frequently asked|"
    r"benefits|diversity|locations|amazon locations|legal|"
    r"life at google|follow|sign in|sign up|search jobs|"
    r"impact the future|save job|apply now|back to search)$",
    re.I,
)

JOB_URL_MUST_MATCH = re.compile(
    r"/jobs?/|/careers?/|/positions?/|/openings?/|/requisition|"
    r"/apply|/posting|/listing|\d{6,}",
    re.I,
)

# ─── Role & location filters ───────────────────────────────────────────────────

TECHNICAL_ROLE_PATTERN = re.compile(
    r"\b("
    r"software\s+engineer|software\s+development\s+engineer|swe|"
    r"ml\s+engineer|ai\s+engineer|"
    r"machine\s+learning\s+engineer|deep\s+learning\s+engineer|"
    r"data\s+engineer|data\s+scientist|analytics\s+engineer|"
    r"backend\s+engineer|front[\s\-]?end\s+engineer|full[\s\-]?stack\s+engineer|"
    r"platform\s+engineer|infrastructure\s+engineer|cloud\s+engineer|"
    r"devops\s+engineer|site\s+reliability\s+engineer|sre|"
    r"research\s+engineer|applied\s+scientist|applied\s+engineer|"
    r"systems?\s+engineer|security\s+engineer|"
    r"computer\s+vision\s+engineer|nlp\s+engineer|llm\s+engineer|"
    r"generative\s+ai\s+engineer|robotics\s+engineer|"
    r"embedded\s+engineer|firmware\s+engineer|"
    r"mobile\s+engineer|ios\s+engineer|android\s+engineer|"
    r"forward[\s\-]?deployed\s+engineer|"
    r"algorithm\s+engineer|"
    r"ml\s+researcher|ai\s+researcher|machine\s+learning\s+researcher"
    r")\b",
    re.I,
)

NON_US_LOCATION_PATTERN = re.compile(
    r"\b("
    r"canada|united\s+kingdom|\buk\b|germany|france|india|australia|"
    r"singapore|brazil|mexico|netherlands|sweden|norway|denmark|finland|"
    r"switzerland|spain|italy|poland|japan|china|south\s+korea|"
    r"london|toronto|berlin|paris|bangalore|bengaluru|mumbai|hyderabad|"
    r"sydney|dublin|amsterdam|tel\s+aviv|dubai|"
    r"remote\s*[-–]\s*(?!(us|u\.s\.|united\s+states))"
    r")\b",
    re.I,
)


def extract_job_links(html: str, base_url: str) -> list[str]:
    """Extract only actual individual job-posting links from a careers page."""
    soup = BeautifulSoup(html, "html.parser")
    base_parsed = urlparse(base_url)
    links = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        # Build absolute URL without path doubling
        if href.startswith("http"):
            full = href
        elif href.startswith("/"):
            full = f"{base_parsed.scheme}://{base_parsed.netloc}{href}"
        else:
            # Relative path — join from domain root to avoid doubling
            full = f"{base_parsed.scheme}://{base_parsed.netloc}/{href}"

        parsed = urlparse(full)

        if parsed.netloc != base_parsed.netloc:
            continue

        path = parsed.path

        if NOISE_URL_PATTERNS.search(path):
            continue

        link_text = a.get_text(strip=True)
        if NOISE_LINK_TEXT.match(link_text):
            continue

        if JOB_URL_MUST_MATCH.search(path):
            clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            links.add(clean)

    return list(links)


def is_technical_role(title: str, text: str) -> bool:
    """Return True if the role is a technical engineering/science position."""
    return bool(
        TECHNICAL_ROLE_PATTERN.search(title)
        or TECHNICAL_ROLE_PATTERN.search(text[:800])
    )


def is_us_based(text: str) -> bool:
    """Return True unless the posting explicitly mentions a non-US location."""
    return not bool(NON_US_LOCATION_PATTERN.search(text[:2000]))


def extract_job_text(html: str) -> tuple[str, str]:
    """Returns (title, cleaned_body_text) from a job posting page."""
    soup = BeautifulSoup(html, "html.parser")

    # Try og:title first (works on JS-heavy pages like Google Careers)
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content", "").strip():
        title = og_title["content"].strip()

    GENERIC_TITLES = {"job details", "jobs", "careers", "search jobs", ""}
    if title.lower() in GENERIC_TITLES:
        for tag in ("h1", "h2", "title"):
            el = soup.find(tag)
            if el:
                candidate = el.get_text(strip=True)
                if candidate.lower() not in GENERIC_TITLES:
                    title = candidate
                    break

    # Extract JSON-LD structured data (Google/LinkedIn embed full job info here)
    extra_text = ""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                if title.lower() in GENERIC_TITLES and data.get("title"):
                    title = data["title"]
                desc = data.get("description", "")
                exp = str(data.get("experienceRequirements", ""))
                extra_text = f"{desc} {exp}"[:3000]
        except Exception:
            pass

    # body text — remove nav/footer noise
    for noise in soup.select("nav, footer, header, script, style, [class*=cookie], [class*=banner]"):
        noise.decompose()
    body = soup.get_text(separator=" ", strip=True)
    body = re.sub(r"\s+", " ", body)[:4000]
    full_text = f"{extra_text} {body}".strip()[:6000]
    return title, full_text


# ══════════════════════════════════════════════════════════════════════════════
#  AI – experience extraction
# ══════════════════════════════════════════════════════════════════════════════

EXPERIENCE_PROMPT = """\
You are a job-posting parser. Extract the MINIMUM years of experience required.

Rules:
- Return ONLY a JSON object: {{"min_years": <number or null>}}
- If the posting says "0-2 years", "entry level", "fresh grad", "new grad", "no experience required" → min_years: 0
- If it says "1+ years" → min_years: 1
- If it says "2-4 years" → min_years: 2
- If it says "5+ years" → min_years: 5
- If no experience is mentioned → min_years: null
- Do NOT include any explanation.

Job posting text:
{text}
"""


_AI_NULL = object()  # sentinel: AI ran successfully but found no experience requirement


def extract_experience_claude(text: str, api_key: str):
    """Use Anthropic Claude to extract min years of experience.

    Returns a float, _AI_NULL (Claude found nothing), or None (call failed).
    """
    if not HAS_ANTHROPIC:
        log.error("anthropic package not installed. Run: pip install anthropic")
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        system = (
            "You are a job-posting parser. Extract the MINIMUM years of experience required. "
            "Rules: "
            'Return ONLY a JSON object like {"min_years": 2} with no extra text. '
            "If entry level / fresh grad / no experience needed → min_years: 0. "
            "If a range like 2-4 years → use the lower number (2). "
            "If no experience mentioned → min_years: null. "
            "Never add explanation or markdown."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=system,
            messages=[{"role": "user", "content": text}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"```(?:json)?\n?|```", "", raw).strip()
        if not raw:
            return _AI_NULL
        data = json.loads(raw)
        val = data.get("min_years")
        return float(val) if val is not None else _AI_NULL
    except Exception as e:
        log.warning(f"Claude extraction error: {e}")
        return None


def extract_experience_grok(text: str, api_key: str):
    """Returns a float, _AI_NULL (Grok found nothing), or None (call failed)."""
    if not HAS_OPENAI_SDK:
        log.error("openai package not installed. Run: pip install openai")
        return None
    try:
        client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        resp = client.chat.completions.create(
            model="grok-3-mini",
            max_tokens=64,
            messages=[{"role": "user", "content": EXPERIENCE_PROMPT.format(text=text)}],
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?\n?|```", "", raw).strip()
        data = json.loads(raw)
        val = data.get("min_years")
        return float(val) if val is not None else _AI_NULL
    except Exception as e:
        log.warning(f"Grok extraction error: {e}")
        return None


def extract_experience_regex(text: str) -> float | None:
    patterns = [
        (r"(\d+)\s*[\+\-–]\s*\d+\s+years?", lambda m: float(m.group(1))),
        (r"(\d+)\s*\+\s*years?", lambda m: float(m.group(1))),
        (r"minimum\s+of\s+(\d+)\s+years?", lambda m: float(m.group(1))),
        (r"at\s+least\s+(\d+)\s+years?", lambda m: float(m.group(1))),
        (r"(\d+)\s+years?\s+of\s+experience", lambda m: float(m.group(1))),
        (r"(entry[- ]level|new\s+grad|fresh\s+grad|no\s+experience)", lambda m: 0.0),
    ]
    tl = text.lower()
    for pat, extractor in patterns:
        m = re.search(pat, tl)
        if m:
            try:
                return extractor(m)
            except Exception:
                pass
    return None


def _resolve_ai_result(result, provider_name: str):
    """Unpack AI result: returns (float|None, did_succeed).
    _AI_NULL means the AI ran fine but found no requirement → treat as None.
    None means the AI call itself failed.
    """
    if result is _AI_NULL:
        return None, True
    if result is None:
        log.info(f"{provider_name} call failed, falling back to regex")
        return None, False
    return result, True


def extract_experience(text: str, cfg: configparser.ConfigParser) -> float | None:
    provider = cfg.get("ai", "provider", fallback="claude").lower()

    if provider == "grok":
        key = cfg.get("ai", "grok_api_key", fallback="").strip()
        if key:
            val, ok = _resolve_ai_result(extract_experience_grok(text, key), "Grok")
            if ok:
                return val
        else:
            log.warning("Grok selected but no grok_api_key in config")

    elif provider == "claude":
        key = cfg.get("ai", "claude_api_key", fallback="").strip()
        if key:
            val, ok = _resolve_ai_result(extract_experience_claude(text, key), "Claude")
            if ok:
                return val
        else:
            log.warning("Claude selected but no claude_api_key in config")

    elif provider == "both":
        claude_key = cfg.get("ai", "claude_api_key", fallback="").strip()
        if claude_key:
            val, ok = _resolve_ai_result(extract_experience_claude(text, claude_key), "Claude")
            if ok:
                return val
        grok_key = cfg.get("ai", "grok_api_key", fallback="").strip()
        if grok_key:
            val, ok = _resolve_ai_result(extract_experience_grok(text, grok_key), "Grok")
            if ok:
                return val

    return extract_experience_regex(text)


# ══════════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _build_email_html(jobs: list) -> tuple[str, str]:
    """Returns (subject, html_body) for a job alert email."""
    rows = []
    for j in jobs:
        yrs = f"{j['min_years']:.0f}" if j['min_years'] is not None else "Not specified"
        rows.append(f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee"><b>{j['company']}</b></td>
          <td style="padding:8px;border-bottom:1px solid #eee">{j['title'] or 'N/A'}</td>
          <td style="padding:8px;border-bottom:1px solid #eee">{yrs} yrs</td>
          <td style="padding:8px;border-bottom:1px solid #eee"><a href="{j['url']}">View Job</a></td>
        </tr>""")

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333">
    <h2 style="color:#2563eb">New Job Alerts — {len(jobs)} posting(s) found!</h2>
    <p>These <b>US-based technical roles</b> have <b>&le; 4 years experience</b> required:</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
      <thead>
        <tr style="background:#f1f5f9">
          <th style="padding:10px;text-align:left">Company</th>
          <th style="padding:10px;text-align:left">Role</th>
          <th style="padding:10px;text-align:left">Min Exp</th>
          <th style="padding:10px;text-align:left">Link</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <p style="color:#888;font-size:12px;margin-top:20px">Sent by JobTracker at {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
    </body></html>"""

    subject = f"{len(jobs)} new US tech job(s) with <=4yr exp — act now for referrals!"
    return subject, html


def _send_via_resend(jobs: list, cfg: configparser.ConfigParser) -> bool:
    """Send via Resend HTTP API — works on Railway (no SMTP needed). Free: 3k/month."""
    api_key  = cfg.get("email", "resend_api_key", fallback="").strip()
    to_addr  = cfg.get("email", "to_address", fallback="").strip()
    if not api_key or not to_addr:
        return False

    subject, html = _build_email_html(jobs)
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": "JobTracker <onboarding@resend.dev>",
                "to": [to_addr],
                "subject": subject,
                "html": html,
            },
            timeout=30,
        )
        resp.raise_for_status()
        log.info(f"Email sent via Resend to {to_addr} with {len(jobs)} jobs")
        return True
    except Exception as e:
        log.error(f"Resend email failed: {e}")
        return False


def _send_via_smtp(jobs: list, cfg: configparser.ConfigParser) -> bool:
    """Send via SMTP with STARTTLS→SSL fallback."""
    smtp_host = cfg.get("email", "smtp_host", fallback="").strip()
    if not smtp_host:
        return False

    smtp_port = cfg.getint("email", "smtp_port", fallback=587)
    smtp_user = cfg.get("email", "smtp_user", fallback="")
    smtp_pass = cfg.get("email", "smtp_password", fallback="")
    to_addr   = cfg.get("email", "to_address", fallback=smtp_user)

    subject, html = _build_email_html(jobs)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, to_addr, msg.as_string())
        log.info(f"Email sent (STARTTLS) to {to_addr} with {len(jobs)} jobs")
        return True
    except Exception as e:
        log.warning(f"STARTTLS on port {smtp_port} failed ({e}), trying SSL/465…")

    try:
        with smtplib.SMTP_SSL(smtp_host, 465, timeout=30) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, to_addr, msg.as_string())
        log.info(f"Email sent (SSL/465) to {to_addr} with {len(jobs)} jobs")
        return True
    except Exception as e:
        log.error(f"SMTP email failed: {e}")
        return False


def send_email(jobs: list, cfg: configparser.ConfigParser):
    if not cfg.has_section("email"):
        return
    # Resend first (HTTP-based, works everywhere including Railway)
    if _send_via_resend(jobs, cfg):
        return
    _send_via_smtp(jobs, cfg)


def desktop_notify(title: str, message: str):
    system = platform.system()
    try:
        if system == "Darwin":
            script = f'display notification "{message}" with title "{title}" sound name "Glass"'
            subprocess.run(["osascript", "-e", script], check=False)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message, "--urgency=critical"], check=False)
        elif system == "Windows":
            try:
                from plyer import notification
                notification.notify(title=title, message=message, timeout=10)
            except ImportError:
                pass
    except Exception as e:
        log.debug(f"Desktop notify failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  CORE LOOP
# ══════════════════════════════════════════════════════════════════════════════

def load_companies(path="companies.csv") -> list[dict]:
    companies = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",", 1)]
            if len(parts) == 2:
                companies.append({"name": parts[0], "url": parts[1]})
    return companies


def run_once(cfg: configparser.ConfigParser, db: DB, companies: list[dict]):
    max_exp = cfg.getfloat("tracker", "max_experience_years", fallback=4.0)
    delay   = cfg.getfloat("tracker", "delay_between_requests", fallback=2.0)
    new_jobs = []

    for company in companies:
        name, url = company["name"], company["url"]
        log.info(f"Checking {name} → {url}")
        html = fetch_page(url)
        if not html:
            continue

        links = extract_job_links(html, url)
        log.info(f"  Found {len(links)} job links on career page")

        known = db.known_urls(name)
        new_links = [l for l in links if _hash(l) not in known]
        log.info(f"  {len(new_links)} new (unseen) links")

        db.save_urls(name, links)

        for job_url in new_links:
            time.sleep(delay)
            job_html = fetch_page(job_url)
            if not job_html:
                continue
            title, text = extract_job_text(job_html)

            if not is_technical_role(title, text):
                log.info(f"  [{name}] '{title}' — skipped (non-technical role)")
                continue
            if not is_us_based(text):
                log.info(f"  [{name}] '{title}' — skipped (non-US location)")
                continue

            min_years = extract_experience(text, cfg)
            log.info(f"  [{name}] '{title}' → min_years={min_years}")
            db.save_job(name, job_url, title, min_years, text)

    pending = db.pending_notifications(max_exp)
    if pending:
        log.info(f"\n{'='*50}")
        log.info(f"  🎯 {len(pending)} job(s) with ≤{max_exp:.0f} yrs experience found!")
        log.info(f"{'='*50}")
        for j in pending:
            log.info(f"  [{j['company']}] {j['title']} — {j['url']}")
            new_jobs.append(dict(j))
            db.mark_notified(j["id"])

        send_email(new_jobs, cfg)

        msg = "\n".join([f"{j['company']}: {j['title'] or j['url'][:60]}" for j in new_jobs[:5]])
        if len(new_jobs) > 5:
            msg += f"\n...and {len(new_jobs)-5} more"
        desktop_notify(f"🎯 {len(new_jobs)} new job alert(s)!", msg)
    else:
        log.info("No new jobs matching criteria this cycle.")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Job Tracker")
    parser.add_argument("--config",    default="config.ini")
    parser.add_argument("--companies", default="companies.csv")
    parser.add_argument("--db",        default="state/jobs.sqlite3")
    parser.add_argument("--once",      action="store_true")
    args = parser.parse_args()

    cfg       = load_config(args.config)
    db        = DB(args.db)
    companies = load_companies(args.companies)

    log.info(f"Loaded {len(companies)} companies to track")

    if args.once:
        run_once(cfg, db, companies)
        return

    interval = cfg.getint("tracker", "poll_interval_minutes", fallback=30)
    log.info(f"Scheduler started — checking every {interval} minutes")
    log.info("Press Ctrl+C to stop\n")

    run_once(cfg, db, companies)

    schedule.every(interval).minutes.do(run_once, cfg=cfg, db=db, companies=companies)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()