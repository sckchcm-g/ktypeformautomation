import json
import os
import random
import time
import base64
import sys
from pathlib import Path
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

load_dotenv()

# Config
EMAIL = os.getenv("EMAIL_USERNAME", "")
PASSWORD = os.getenv("EMAIL_APP_PASSWORD") or os.getenv("EMAIL_PASSWORD", "")
SEND_EMAIL_TO = os.getenv("SEND_EMAIL_TO", "")
AUTH_STATE_RAW = os.getenv("AUTH_STATE", "").strip()
SAVE_AUTH_STATE = os.getenv("SAVE_AUTH_STATE", "0").strip().lower() in {"1", "true", "yes"}
AUTH_STATE_FILE = Path("auth.json")
HEADLESS = os.getenv("HEADLESS", "0").strip().lower()  # '1' or 'true' enables headless
TEST_MODE = os.getenv("TEST_MODE", "0").strip().lower() in {"1", "true", "yes"}
REPORT_MODE = os.getenv("REPORT_MODE", "").strip().lower()  # filled | replay
CI_MODE = (
    os.getenv("CI_MODE", "").strip().lower() in {"1", "true", "yes"}
    or os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"
)
ALLOW_UI_LOGIN_IN_CI = os.getenv("ALLOW_UI_LOGIN_IN_CI", "0").strip().lower() in {"1", "true", "yes"}


def _load_auth_state():
    """
    Load auth state from env var AUTH_STATE or local auth.json.

    Supported AUTH_STATE formats:
    - Raw JSON string
    - Base64 encoded JSON string
    - File path pointing to a JSON file
    """
    # 1) Prefer AUTH_STATE from env/secrets
    if AUTH_STATE_RAW:
        # Attempt as direct JSON first
        try:
            state = json.loads(AUTH_STATE_RAW)
            print("🔐 Loaded AUTH_STATE from environment (raw JSON).")
            return state
        except json.JSONDecodeError:
            pass

        # Attempt as path
        env_path = Path(AUTH_STATE_RAW)
        if env_path.exists():
            try:
                state = json.loads(env_path.read_text())
                print(f"🔐 Loaded AUTH_STATE from file path in env: {env_path}")
                return state
            except Exception as e:
                print(f"⚠️  Failed to parse AUTH_STATE file path: {e}")

        # Attempt as base64 encoded JSON
        try:
            decoded = base64.b64decode(AUTH_STATE_RAW).decode("utf-8")
            state = json.loads(decoded)
            print("🔐 Loaded AUTH_STATE from environment (base64 JSON).")
            return state
        except Exception as e:
            print(f"⚠️  AUTH_STATE provided but could not parse (raw/path/base64): {e}")

    # 2) Fallback to auth.json in repo root
    if AUTH_STATE_FILE.exists():
        try:
            state = json.loads(AUTH_STATE_FILE.read_text())
            print(f"🔐 Loaded auth state from {AUTH_STATE_FILE}")
            return state
        except Exception as e:
            print(f"⚠️  Could not parse {AUTH_STATE_FILE}: {e}")

    return None


def _normalize_cookie(cookie):
    """Prepare cookie dict for Selenium add_cookie constraints."""
    allowed = {"name", "value", "path", "domain", "secure", "httpOnly", "expiry", "sameSite"}
    cleaned = {k: v for k, v in cookie.items() if k in allowed}

    # Selenium expects int expiry when present
    if "expiry" in cleaned:
        try:
            cleaned["expiry"] = int(cleaned["expiry"])
        except Exception:
            cleaned.pop("expiry", None)

    # Selenium supports sameSite: Strict/Lax/None
    if "sameSite" in cleaned and cleaned["sameSite"] not in {"Strict", "Lax", "None"}:
        cleaned.pop("sameSite", None)

    # name + value are mandatory
    if not cleaned.get("name"):
        return None
    if cleaned.get("value") is None:
        cleaned["value"] = ""

    return cleaned


def _apply_auth_state(driver, state):
    """
    Apply cookies + optional localStorage from auth state.

    Supports Playwright-like storage state format:
      {
        "cookies": [...],
        "origins": [{"origin": "https://...", "localStorage": [{"name":"k","value":"v"}]}]
      }
    """
    if not isinstance(state, dict):
        print("⚠️  AUTH_STATE is not a JSON object; ignoring.")
        return False

    cookies = state.get("cookies", [])
    origins = state.get("origins", [])

    if not cookies and not origins:
        print("⚠️  AUTH_STATE has no cookies/origins; ignoring.")
        return False

    # Group cookies by domain so we can navigate once per domain before add_cookie
    cookies_by_domain = {}
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if not domain:
            continue
        cookies_by_domain.setdefault(domain, []).append(c)

    applied_count = 0

    for domain, domain_cookies in cookies_by_domain.items():
        try:
            driver.get(f"https://{domain}")
            time.sleep(0.5)
            for raw_cookie in domain_cookies:
                cookie = _normalize_cookie(raw_cookie)
                if not cookie:
                    continue
                try:
                    driver.add_cookie(cookie)
                    applied_count += 1
                except Exception:
                    # Some cookies may be rejected due to domain/path constraints; continue best-effort
                    pass
        except Exception:
            continue

    # Apply localStorage entries for each origin if present
    for origin in origins:
        try:
            origin_url = origin.get("origin")
            if not origin_url:
                continue
            driver.get(origin_url)
            time.sleep(0.5)
            for item in origin.get("localStorage", []):
                key = item.get("name")
                value = item.get("value")
                if key is None:
                    continue
                driver.execute_script("localStorage.setItem(arguments[0], arguments[1]);", key, value)
        except Exception:
            continue

    print(f"🔐 Applied {applied_count} cookies from AUTH_STATE.")
    if origins:
        print(f"🧠 Restored localStorage for {len(origins)} origin(s).")

    return applied_count > 0 or len(origins) > 0


def _export_auth_state(driver, output_file: Path):
    """Export a reusable auth state JSON from key domains after successful login."""
    state = {"cookies": [], "origins": []}
    domains = ["https://kalvium.community", "https://accounts.google.com"]

    for domain in domains:
        try:
            driver.get(domain)
            time.sleep(0.5)
            cookies = driver.get_cookies() or []
            state["cookies"].extend(cookies)

            local_data = driver.execute_script(
                "return Object.entries(localStorage).map(([name, value]) => ({name, value}));"
            ) or []
            if local_data:
                state["origins"].append({"origin": domain, "localStorage": local_data})
        except Exception:
            continue

    output_file.write_text(json.dumps(state, indent=2))
    print(f"💾 Auth state exported to: {output_file}")
    print("📌 Copy this JSON into your AUTH_STATE secret for workflow runs.")


def _is_google_logged_in(driver):
    """Return True when Google account session appears active."""
    try:
        driver.find_element(By.CSS_SELECTOR, "input[type='email']")
        return False
    except Exception:
        return True


def _is_kalvium_profile_visible(driver):
    """Return True when Kalvium profile/home markers are visible."""
    return driver.execute_script("""
        let text = document.body.innerText;
        return text.includes('Hi Saksham Gupta') ||
               text.includes('Squad') ||
               text.includes('Class of') ||
               (text.includes('Hi ') && text.includes('👋'));
    """)


def _find_continue_with_google_button(driver):
    """Find and return the 'Continue with Google' button element, if present."""
    return driver.execute_script("""
        let buttons = Array.from(document.querySelectorAll('button'));
        return buttons.find(b => b.textContent.includes('Continue with Google'));
    """)


def _wait_for_kalvium_profile(driver, timeout_seconds=30):
    """Wait until Kalvium profile markers appear; returns True/False."""
    for i in range(timeout_seconds):
        time.sleep(1)
        if _is_kalvium_profile_visible(driver):
            return True
        if i % 5 == 0 and i > 0:
            print(f"⏳ Still waiting... ({timeout_seconds - i}s remaining)")
    return False

# Load work type from responseoption.json
RESPONSE_OPTION_FILE = Path("responseoption.json")
if RESPONSE_OPTION_FILE.exists():
    response_config = json.loads(RESPONSE_OPTION_FILE.read_text())
    selected_num = response_config.get("selected", 1)
    options = response_config.get("options", [])
    if 1 <= selected_num <= len(options):
        WORK_TYPE = options[selected_num - 1]
        print(f"✅ Work type from JSON: #{selected_num} - {WORK_TYPE}")
    else:
        WORK_TYPE = os.getenv("WORK_TYPE", "Working on-site (Company location)")
        print(f"⚠️  Invalid selection number, using env: {WORK_TYPE}")
else:
    WORK_TYPE = os.getenv("WORK_TYPE", "Working on-site (Company location)")
    print(f"⚠️  No responseoption.json found, using env: {WORK_TYPE}")

# Load responses from JSON file
RESPONSES_FILE = Path("responses.json")
if RESPONSES_FILE.exists():
    responses = json.loads(RESPONSES_FILE.read_text())
    WORK_DESCRIPTION = random.choice(responses)
    print(f"📝 Selected random response: {WORK_DESCRIPTION[:60]}...")
else:
    WORK_DESCRIPTION = "Completed assigned tasks."
    print("⚠️  No responses.json found, using default description")

# Start browser at 720p
print("🚀 Starting browser...")
options = webdriver.ChromeOptions()
options.add_argument("--window-size=1280,720")

# Use Chrome profile to persist Google login, with proper cookie prefs
profile_dir = Path.home() / ".selenium_chrome_profile"
profile_dir.mkdir(exist_ok=True)
options.add_argument(f"--user-data-dir={profile_dir}")

# Skip profile selection screen
options.add_argument("--no-first-run")
options.add_argument("--no-default-browser-check")

# Ensure cookies are enabled
prefs = {
    "profile.default_content_setting_values.cookies": 1,
    "profile.cookie_controls_mode": 0
}
options.add_experimental_option("prefs", prefs)

# Toggle headless via env
is_headless = HEADLESS in {"1", "true", "yes"}
if is_headless:
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    print("🎯 Headless: ON")
else:
    print("🎯 Headless: OFF")

driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 20)
auth_state = _load_auth_state()

try:
    # Step 0: Try restoring auth state first (CI-friendly)
    auth_state_applied = False
    auth_state_refresh_needed = False
    if auth_state:
        print("🔐 Step 0: Attempting session restore from AUTH_STATE...")
        auth_state_applied = _apply_auth_state(driver, auth_state)
        if auth_state_applied:
            driver.get("https://kalvium.community")
            time.sleep(3)
        else:
            auth_state_refresh_needed = True

    # Step 1: Check if Google is already logged in (via auth state or Chrome profile)
    print("🔐 Step 1: Checking Google account status...")
    driver.get("https://accounts.google.com")
    time.sleep(3)

    needs_login = not _is_google_logged_in(driver)
    if needs_login:
        print("❌ Not logged into Google, need to login")
    else:
        print("✅ Already logged into Google via Chrome profile!")
    
    if needs_login:
        if CI_MODE and not ALLOW_UI_LOGIN_IN_CI:
            if EMAIL and PASSWORD:
                print("⚠️  CI mode: AUTH_STATE appears invalid, falling back to credential login using app password.")
                print("⚠️  This is less reliable than AUTH_STATE; refresh AUTH_STATE after this run.")
                auth_state_refresh_needed = True
            else:
                raise Exception(
                    "CI mode detected and Google session is not authenticated. "
                    "Set a fresh AUTH_STATE secret (recommended) or provide EMAIL_USERNAME + EMAIL_APP_PASSWORD."
                )

        if not EMAIL or not PASSWORD:
            raise Exception(
                "Google login required, but EMAIL_USERNAME/EMAIL_PASSWORD are missing. "
                "Use AUTH_STATE secret for CI or provide credentials for local login."
            )

        print("\n📧 Logging into Google (first time only)...")
        driver.get("https://accounts.google.com/signin")
        time.sleep(2)
        
        print("📝 Filling email...")
        email_field = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']")))
        email_field.clear()
        email_field.send_keys(EMAIL)
        print(f"✅ Entered: {EMAIL}")
        time.sleep(1)
        email_field.send_keys(Keys.RETURN)
        
        print("⏳ Waiting for password page...")
        time.sleep(3)
        
        print("📝 Filling password...")
        pwd_field = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))
        pwd_field.clear()
        pwd_field.send_keys(PASSWORD)
        print("✅ Entered password")
        time.sleep(1)
        pwd_field.send_keys(Keys.RETURN)
        
        print("⏳ Complete 2FA if prompted (Chrome will remember this session)...")
        time.sleep(10)
        print("✅ Google login complete and saved in Chrome profile!")

        if SAVE_AUTH_STATE or auth_state_refresh_needed:
            _export_auth_state(driver, AUTH_STATE_FILE)
    
    # Step 2: Now visit Kalvium and use Google SSO
    print("\n📍 Step 2: Visiting Kalvium.community...")
    driver.get("https://kalvium.community")
    time.sleep(3)

    # Check if already logged into Kalvium
    profile_found = _is_kalvium_profile_visible(driver)
    
    if profile_found:
        print("✅ Already logged into Kalvium!")
    else:
        print("🔍 Looking for 'Continue with Google' button...")
        time.sleep(2)

        google_btn = _find_continue_with_google_button(driver)
        
        if not google_btn:
            raise Exception("Can't find Google button on Kalvium")
        
        print("🖱️  Clicking 'Continue with Google'...")
        driver.execute_script("arguments[0].click();", google_btn)
        time.sleep(3)

        # Wait for Kalvium profile to load after OAuth
        print("⏳ Waiting for Kalvium profile to load...")
        profile_found = _wait_for_kalvium_profile(driver, timeout_seconds=30)
        if profile_found:
            print("✅ Kalvium login complete! Profile found.")

        if not profile_found:
            print("⚠️  Timeout, but continuing...")
    
    # Go to internships
    print("📍 Navigating to /internships...")
    driver.get("https://kalvium.community/internships")
    time.sleep(3)
    
    if TEST_MODE:
        print("\n🧪 TEST MODE: Skipping form fill, only checking records...")
    else:
        print("🔍 Looking for 'Complete' button in table...")
        time.sleep(2)

    print("⏳ Watching for 'Complete' button (up to 60s)...")

    timeout = 60
    interval = 2
    end_time = time.time() + timeout
    complete_btn = None

    while time.time() < end_time:
        complete_btn = driver.execute_script("""
            let rows = document.querySelectorAll('tr');
            for (let row of rows) {
                let buttons = row.querySelectorAll('button');
                for (let btn of buttons) {
                    if (btn.textContent.toLowerCase().includes('complete')) {
                        return btn;
                    }
                }
            }
            return null;
        """)

        if complete_btn:
            print("✅ Found 'Complete' button!")
            break

    print("⏳ Not found yet, retrying...")
    time.sleep(interval)
    
    if complete_btn and not TEST_MODE:
        print("✅ Found 'Complete' button! Clicking...")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", complete_btn)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", complete_btn)
        
        print("⏳ Waiting for sidebar to open...")
        time.sleep(3)
        
        # Step 1: Click the work type dropdown
        print("🔍 Looking for work type dropdown...")
        try:
            work_type_btn = wait.until(EC.presence_of_element_located((By.ID, "workType")))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", work_type_btn)
            time.sleep(0.5)
            driver.execute_script("arguments[0].click();", work_type_btn)
            print("✅ Clicked work type dropdown")
            time.sleep(1)
            
            # Step 2: Extract and log all options
            print("\n📋 Available work type options:")
            options = driver.execute_script("""
                let select = document.querySelector('select[aria-hidden="true"]');
                if (!select) return [];
                return Array.from(select.options).map(opt => opt.value);
            """)
            
            for idx, opt in enumerate(options, 1):
                marker = "👉" if opt == WORK_TYPE else "  "
                print(f"{marker} {idx}. {opt}")
            
            if WORK_TYPE not in options:
                print(f"⚠️  Configured WORK_TYPE not found, using first option")
                selected = options[0] if options else None
            else:
                selected = WORK_TYPE
                print(f"\n✅ Selecting: {selected}")
            
            # Step 3: Click the matching option
            if selected:
                driver.execute_script("""
                    let options = Array.from(document.querySelectorAll('[role="option"]'));
                    let target = options.find(opt => opt.textContent.trim() === arguments[0]);
                    if (target) target.click();
                """, selected)
                time.sleep(1)
                print("✅ Option selected")
            
            # Step 4: Find and clear the contenteditable div
            print("\n📝 Filling work description...")
            editor = wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div.tiptap.ProseMirror[contenteditable='true']")
            ))
            
            # Clear existing content
            driver.execute_script("arguments[0].innerHTML = '';", editor)
            time.sleep(0.5)
            
            # Type new content
            driver.execute_script("arguments[0].focus();", editor)
            editor.send_keys(WORK_DESCRIPTION)
            print(f"✅ Entered description: {WORK_DESCRIPTION[:50]}...")
            time.sleep(1)
            
            print("\n📤 Submitting form...")
            # Find and click submit button
            submit_btn = wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "button[type='submit']")
            ))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_btn)
            time.sleep(0.5)
            driver.execute_script("arguments[0].click();", submit_btn)
            print("✅ Submit button clicked")
            
            # Refresh page before verification
            print("\n🔄 Refreshing page to verify submission...")
            time.sleep(3)
            driver.get("https://kalvium.community/internships")
            time.sleep(3)
            
        except Exception as e:
            print(f"❌ Form fill error: {e}")
    elif TEST_MODE:
        print("✅ Test mode: Skipped form filling")
    else:
        print("❌ No 'Complete' button found. Might be 'No pending worklogs'.")
    
    # Verification section (runs in both modes)
    print("\n📊 Verifying today's record...")
    from datetime import datetime, timedelta
    today = datetime.now().strftime("%d %b %Y")
    print(f"📅 Looking for: {today}")
    
    # Find today's record in the table
    todays_status = driver.execute_script("""
        let today = arguments[0];
        let rows = document.querySelectorAll('tbody tr');
        for (let row of rows) {
            let cells = row.querySelectorAll('td');
            if (cells.length >= 2 && cells[0].textContent.trim() === today) {
                return cells[1].textContent.trim();
            }
        }
        return null;
    """, today)
    
    form_filled_successfully = False
    
    if todays_status is None:
        print(f"❌ Error: Could not find today's date ({today}) in the table")
    elif todays_status in ["-", ""]:
        print(f"⏰ Status for {today} is '{todays_status}'")
        print("ℹ️  Form filling window has not activated yet.")
        print("💡 Tip: Try again at 12:00 PM when the form becomes available.")
    elif "week off" in todays_status.lower() or "holiday" in todays_status.lower():
        print(f"🏖️  Status for {today}: {todays_status}")
        print("ℹ️  No form filling required today (Week Off/Holiday).")
        form_filled_successfully = True  # Consider this successful since no action needed
    elif todays_status.lower() == "absent":
        print(f"⚠️  Status for {today}: {todays_status}")
        if not TEST_MODE:
            print("❌ Form submission may have failed or was not processed.")
    else:
        print(f"\n🎉 {'VERIFIED' if TEST_MODE else 'SUCCESS'}! Record found!")
        print(f"📊 Today's Record:")
        print(f"   Date: {today}")
        print(f"   Status: {todays_status}")
        form_filled_successfully = True
    
    if not TEST_MODE and form_filled_successfully and todays_status not in ["-", ""] and "week off" not in todays_status.lower():
        print("\n✅ Form filling complete!")

    if SEND_EMAIL_TO:
        print(f"📮 SEND_EMAIL_TO configured for: {SEND_EMAIL_TO}")
    
    # Interactive record viewer with timeout and loop
    import signal

    class TimeoutError(Exception):
        pass
    
    def timeout_handler(signum, frame):
        raise TimeoutError()

    def _open_completed_table():
        """Ensure the Completed accordion is open and return its table element."""
        try:
            completed_btn = driver.execute_script("""
                return Array.from(document.querySelectorAll('button'))
                    .find(b => b.textContent.trim().toLowerCase().startsWith('completed'));
            """)

            if not completed_btn:
                return None

            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", completed_btn)

            if driver.execute_script("return arguments[0].getAttribute('aria-expanded');", completed_btn) == 'false':
                driver.execute_script("arguments[0].click();", completed_btn)

            wait.until(lambda d: d.execute_script("""
                let tables = Array.from(document.querySelectorAll('table'));
                return tables.some(t => {
                    let headers = Array.from(t.querySelectorAll('th')).map(th => th.textContent.trim().toLowerCase());
                    return headers.includes('submitted at');
                });
            """))

            table = driver.execute_script("""
                let tables = Array.from(document.querySelectorAll('table'));
                return tables.find(t => {
                    let headers = Array.from(t.querySelectorAll('th')).map(th => th.textContent.trim().toLowerCase());
                    return headers.includes('submitted at');
                });
            """)

            return table
        except Exception as e:
            print(f"⚠️  Could not open Completed accordion: {e}")
            return None

    def fetch_submitted_map():
        try:
            table = _open_completed_table()
            if not table:
                return {}

            return driver.execute_script("""
                let target = arguments[0];
                let map = {};
                if (target) {
                    let rows = target.querySelectorAll('tbody tr');
                    for (let row of rows) {
                        let cells = row.querySelectorAll('td');
                        if (cells.length >= 2) {
                            let date = cells[0].textContent.trim();
                            let submitted = cells[1].textContent.trim();
                            map[date] = submitted;
                        }
                    }
                }
                return map;
            """, table) or {}
        except Exception as e:
            print(f"⚠️  Could not fetch submitted times: {e}")
            return {}

    def fetch_descriptions_for_dates(dates):
        """Return map of date string -> description text from the Completed 'view' sidebar."""
        results = {}
        try:
            table = _open_completed_table()
            if not table:
                print("⚠️  Completed table not found for descriptions")
                return results

            for date_str in dates:
                try:
                    row = driver.execute_script("""
                        let target = arguments[0];
                        let dt = arguments[1];
                        if (!target) return null;
                        let rows = Array.from(target.querySelectorAll('tbody tr'));
                        return rows.find(r => {
                            let cells = r.querySelectorAll('td');
                            return cells.length && cells[0].textContent.trim() === dt;
                        }) || null;
                    """, table, date_str)

                    if not row:
                        results[date_str] = None
                        continue

                    view_btn = driver.execute_script("""
                        let btn = arguments[0].querySelector('button');
                        return btn || null;
                    """, row)

                    if view_btn:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", view_btn)
                        driver.execute_script("arguments[0].click();", view_btn)
                    else:
                        results[date_str] = None
                        continue

                    # Wait for the dialog to show the correct date
                    matched = False
                    for _ in range(10):
                        time.sleep(0.4)
                        matched = driver.execute_script("""
                            let dt = arguments[0];
                            let dialog = document.querySelector('[role="dialog"]');
                            if (!dialog) return false;
                            let span = dialog.querySelector('span.text-xs');
                            return span && span.textContent.trim() === dt;
                        """, date_str)
                        if matched:
                            break

                    desc = driver.execute_script("""
                        let dialog = document.querySelector('[role="dialog"]');
                        if (!dialog) return null;
                        let prose = dialog.querySelector('.tiptap.ProseMirror');
                        return prose ? prose.innerText.trim() : null;
                    """) if matched else None

                    # Close dialog if close button exists
                    try:
                        close_btn = driver.execute_script("""
                            let dialog = document.querySelector('[role="dialog"]');
                            if (!dialog) return null;
                            return dialog.querySelector('button[type="button"]');
                        """)
                        if close_btn:
                            driver.execute_script("arguments[0].click();", close_btn)
                            time.sleep(0.2)
                    except Exception:
                        pass

                    results[date_str] = desc if desc else None
                except Exception:
                    results[date_str] = None
        except Exception as e:
            print(f"⚠️  Could not fetch descriptions: {e}")
        return results

    def format_description(desc: str) -> str:
        """Pretty-print description; box it if multiline."""
        if not desc:
            return ""
        if '\n' not in desc:
            return desc
        lines = desc.splitlines()
        width = max(len(line) for line in lines)
        top = "_" * (width + 4)
        body = [f"| {line.ljust(width)} |" for line in lines]
        bottom = "‾" * (width + 4)
        return "\n".join([top, *body, bottom])

    def fetch_main_records():
        """Return rows from the main worklog table (not the Completed accordion)."""
        try:
            return driver.execute_script("""
                // Find a table that looks like the main status table (Date + Status columns, no 'Submitted at')
                let tables = Array.from(document.querySelectorAll('table'));
                let main = tables.find(t => {
                    let headers = Array.from(t.querySelectorAll('th')).map(th => th.textContent.trim().toLowerCase());
                    return headers.includes('date') && headers.includes('status') && !headers.includes('submitted at');
                });

                if (!main) return [];

                let rows = main.querySelectorAll('tbody tr');
                let records = [];
                for (let row of rows) {
                    let cells = row.querySelectorAll('td');
                    if (cells.length >= 2) {
                        records.push({
                            date: cells[0].textContent.trim(),
                            status: cells[1].textContent.trim()
                        });
                    }
                }
                return records;
            """) or []
        except Exception as e:
            print(f"⚠️  Could not fetch main table records: {e}")
            return []

    def run_report_mode(mode: str):
        mode = (mode or "").strip().lower()
        if mode not in {"filled", "replay"}:
            print(f"ℹ️  REPORT_MODE '{mode}' not recognized. Supported: filled, replay")
            return

        if mode == "replay":
            all_records = fetch_main_records()
            if not all_records:
                print("❌ No records found in table")
                return

            print("\n📊 Monthly Replay Report")
            print("="*60)

            from collections import defaultdict
            monthly_data = defaultdict(lambda: {'working': 0, 'absent': 0, 'week_off': 0})
            seen_dates = set()

            for record in all_records:
                try:
                    date_str = record['date']
                    if date_str in seen_dates:
                        continue
                    seen_dates.add(date_str)

                    date_obj = datetime.strptime(date_str, "%d %b %Y")
                    month_key = date_obj.strftime("%B %Y")
                    status = record['status'].lower().strip()

                    if status in ['absent', '-', '']:
                        monthly_data[month_key]['absent'] += 1
                    elif 'week off' in status or 'holiday' in status or 'leave' in status:
                        monthly_data[month_key]['week_off'] += 1
                    else:
                        monthly_data[month_key]['working'] += 1
                except Exception as e:
                    print(f"⚠️  Could not parse record: {record.get('date')} - {e}")

            for month in sorted(monthly_data.keys(), key=lambda x: datetime.strptime(x, "%B %Y"), reverse=True):
                data = monthly_data[month]
                total = data['working'] + data['absent'] + data['week_off']
                print(f"\n📅 {month}")
                print(f"   ✅ Working Days:  {data['working']}")
                print(f"   ❌ Absents:       {data['absent']}")
                print(f"   🏖️  Week Offs:     {data['week_off']}")
                print(f"   📊 Total Days:    {total}")
                print("-"*60)
            return

        # mode == "filled"
        all_records = fetch_main_records()
        submitted_map = fetch_submitted_map()

        if not all_records:
            print("❌ No records found in table")
            return

        parsed = []
        for record in all_records:
            try:
                date_obj = datetime.strptime(record['date'], "%d %b %Y")
                parsed.append({
                    'date_str': record['date'],
                    'date_obj': date_obj,
                    'status': record['status']
                })
            except Exception:
                continue

        if not parsed:
            print("❌ No parsable records found")
            return

        current_week_start = datetime.now() - timedelta(days=datetime.now().weekday())
        week_records = sorted(
            [r for r in parsed if (r['date_obj'] - timedelta(days=r['date_obj'].weekday())).date() == current_week_start.date()],
            key=lambda r: r['date_obj']
        )

        if not week_records:
            print("ℹ️  No records for current week")
            return

        week_end = current_week_start + timedelta(days=6)
        week_label = f"Week: {current_week_start:%d %b %Y} - {week_end:%d %b %Y}"
        dates = [r['date_str'] for r in week_records]
        desc_map = fetch_descriptions_for_dates(dates)

        print(f"\n🧾 What you filled for {week_label}")
        printed = 0
        for rec in week_records:
            submitted_at = submitted_map.get(rec['date_str'], '---') if submitted_map else '---'
            desc = desc_map.get(rec['date_str'])
            if not desc:
                continue
            print("-"*50)
            print(f"📅 {rec['date_str']} | Status: {rec['status']} | Submitted: {submitted_at}")
            print("📝 Description:")
            print(format_description(desc))
            printed += 1
        if printed == 0:
            print("(No descriptions available for this week)")
        else:
            print("-"*50)

    interactive_enabled = (not CI_MODE) and (not is_headless) and sys.stdin.isatty()
    if not interactive_enabled:
        if REPORT_MODE:
            print(f"📋 Running non-interactive report mode: {REPORT_MODE}")
            run_report_mode(REPORT_MODE)
        else:
            print("ℹ️  Skipping interactive record viewer (CI/headless/non-interactive mode).")
        print("\n👋 Closing browser...")
        raise SystemExit(0)
    
    # Set up signal handler for timeout
    signal.signal(signal.SIGALRM, timeout_handler)
    
    while True:
        try:
            print("\n" + "="*50)
            signal.alarm(120)  # 2 minute timeout
            response = input("📋 Show records? (y/r/n) - y: weekly view, r: replay/monthly report, n: exit (2min timeout): ").strip().lower()
            signal.alarm(0)  # Cancel alarm after input received
            
            if response in ['n', 'no', 'exit']:
                print("✅ Thank you!")
                break
            
            elif response in ['y', 'yes']:
                # Fetch all records from the table
                all_records = fetch_main_records()

                submitted_map = fetch_submitted_map()
                
                if not all_records:
                    print("❌ No records found in table")
                else:
                    # Parse and group by ISO week (Mon-Sun), starting from current week
                    parsed = []
                    for record in all_records:
                        try:
                            date_obj = datetime.strptime(record['date'], "%d %b %Y")
                            parsed.append({
                                'date_str': record['date'],
                                'date_obj': date_obj,
                                'status': record['status']
                            })
                        except Exception as e:
                            print(f"⚠️  Skipping unparsable date {record['date']}: {e}")
                            continue

                    if not parsed:
                        print("❌ No parsable records found")
                        continue

                    # Sort desc by date
                    parsed.sort(key=lambda r: r['date_obj'], reverse=True)

                    # Bucket by week starting Monday
                    weeks = {}
                    for rec in parsed:
                        start = rec['date_obj'] - timedelta(days=rec['date_obj'].weekday())
                        weeks.setdefault(start, []).append(rec)

                    # Ordered week starts (latest first)
                    ordered_starts = sorted(weeks.keys(), reverse=True)
                    idx = 0

                    while idx < len(ordered_starts):
                        week_start = ordered_starts[idx]
                        week_end = week_start + timedelta(days=6)
                        week_label = f"Week: {week_start:%d %b %Y} - {week_end:%d %b %Y}"

                        # Sort week records ascending by date for readability
                        week_records = sorted(weeks[week_start], key=lambda r: r['date_obj'])

                        # Render the week block once per iteration
                        print("\n" + "="*50)
                        print(week_label)
                        for rec in week_records:
                            submitted_at = submitted_map.get(rec['date_str'], '---') if submitted_map else '---'
                            print(f"📅 {rec['date_str']:<15} | Status: {rec['status']:<30} | Submitted: {submitted_at}")

                        showed_details = False
                        user_aborted = False

                        while True:
                            prompt = "\n📋 Show previous week? (y/n): " if showed_details else "\n📋 Show previous week? (y/n) or show what you filled ? (a): "
                            choice = input(prompt).strip().lower()

                            if choice == 'a' and not showed_details:
                                dates = [r['date_str'] for r in week_records]
                                desc_map = fetch_descriptions_for_dates(dates)

                                print(f"\n🧾 What you filled for {week_label}")
                                printed = 0
                                for rec in week_records:
                                    submitted_at = submitted_map.get(rec['date_str'], '---') if submitted_map else '---'
                                    desc = desc_map.get(rec['date_str'])
                                    if not desc:
                                        continue
                                    print("-"*50)
                                    print(f"📅 {rec['date_str']} | Status: {rec['status']} | Submitted: {submitted_at}")
                                    print("📝 Description:")
                                    print(format_description(desc))
                                    printed += 1

                                if printed == 0:
                                    print("(No descriptions available for this week)")
                                else:
                                    print("-"*50)

                                showed_details = True
                                # After showing details, re-ask without the 'a' option
                                continue

                            if choice in ['y', 'yes']:
                                idx += 1
                                break

                            # Any non-yes response stops weekly navigation
                            idx = len(ordered_starts)
                            user_aborted = True
                            break

                        if idx >= len(ordered_starts) and not user_aborted:
                            print("\n✅ End of records reached!")
                            break
            
            elif response in ['r', 'replay']:
                # Fetch all records
                all_records = fetch_main_records()
                
                if not all_records:
                    print("❌ No records found in table")
                else:
                    print("\n📊 Monthly Replay Report")
                    print("="*60)
                    
                    # Group by month and deduplicate by date
                    from collections import defaultdict
                    monthly_data = defaultdict(lambda: {'working': 0, 'absent': 0, 'week_off': 0})
                    seen_dates = set()  # Track dates we've already processed
                    
                    for record in all_records:
                        try:
                            date_str = record['date']
                            
                            # Skip duplicate dates
                            if date_str in seen_dates:
                                continue
                            seen_dates.add(date_str)
                            
                            # Parse date format: "22 Dec 2025"
                            date_obj = datetime.strptime(date_str, "%d %b %Y")
                            month_key = date_obj.strftime("%B %Y")  # "December 2025"
                            
                            status = record['status'].lower().strip()
                            
                            # Categorize status
                            if status in ['absent', '-', ''] or status == 'absent':
                                monthly_data[month_key]['absent'] += 1
                            elif 'week off' in status or 'holiday' in status or 'leave' in status:
                                monthly_data[month_key]['week_off'] += 1
                            else:
                                # Everything else is considered working (Present, WFH, specific work descriptions, etc.)
                                monthly_data[month_key]['working'] += 1
                                
                        except Exception as e:
                            print(f"⚠️  Could not parse record: {record['date']} - {e}")
                            continue
                    
                    # Display monthly summaries
                    for month in sorted(monthly_data.keys(), key=lambda x: datetime.strptime(x, "%B %Y"), reverse=True):
                        data = monthly_data[month]
                        total = data['working'] + data['absent'] + data['week_off']
                        
                        print(f"\n📅 {month}")
                        print(f"   ✅ Working Days:  {data['working']}")
                        print(f"   ❌ Absents:       {data['absent']}")
                        print(f"   🏖️  Week Offs:     {data['week_off']}")
                        print(f"   📊 Total Days:    {total}")
                        print("-"*60)
                    
                    print("\n✅ Replay complete!")
            
            else:
                print("⚠️  Invalid option. Please choose y, r, or n.")
        
        except TimeoutError:
            signal.alarm(0)
            print("\n⏱️  Timeout reached (2 minutes). Exiting...")
            break
        except KeyboardInterrupt:
            signal.alarm(0)
            print("\n\n⚠️  Interrupted by user. Exiting...")
            break
    
    print("\n👋 Closing browser...")

except Exception as e:
    print(f"❌ Error: {e}")
    time.sleep(5)
finally:
    driver.quit()
    print("👋 Browser closed.")
