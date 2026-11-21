"""Account-isolated Selenium session for NGL Spark (learn.eltngl.com); passwords are never saved."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parent
BASE_URL = "https://learn.eltngl.com/"
ASSIGNMENTS_URL = "https://learn.eltngl.com/dashboard/assignments"


def session_folder(username):
    folder = ROOT / ".sessions" / hashlib.sha256(username.lower().encode()).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    return folder


def _is_logged_in(driver):
    url = driver.current_url.lower()
    if "/dashboard" in url:
        has_signin = driver.execute_script(
            "return Boolean(document.querySelector('input[name=\"identifier\"], #sign-in'));"
        )
        return not has_signin
    return False


def open_session(username, password=None, headed=False):
    folder = session_folder(username)
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={folder / 'profile'}")
    options.add_argument("--window-size=1440,1000")
    if not headed and os.environ.get("NGL_HEADED") != "1":
        options.add_argument("--headless=new")
    options.add_argument("--restore-last-session")
    if os.environ.get("CHROME_NO_SANDBOX") == "1":
        options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    chrome_bin = os.environ.get("CHROME_BIN")
    if chrome_bin and os.path.exists(chrome_bin):
        options.binary_location = chrome_bin

    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    service = Service(driver_path) if (driver_path and os.path.exists(driver_path)) else Service()
    driver = webdriver.Chrome(service=service, options=options)

    state = folder / "session.json"
    if state.exists():
        saved = json.loads(state.read_text())
        if saved.get("username") != username.lower():
            driver.quit()
            raise RuntimeError("Session account mismatch")
        driver.get(BASE_URL)
        time.sleep(2)
        now = time.time()
        for cookie in saved.get("cookies", []):
            c = {k: v for k, v in cookie.items() if v is not None}
            if "expiry" not in c or c["expiry"] > now:
                try:
                    driver.add_cookie(c)
                except Exception:
                    pass
        if saved.get("storage"):
            driver.execute_script(
                "for (const [k,v] of Object.entries(arguments[0])) localStorage.setItem(k,v);",
                saved["storage"],
            )
        driver.get(ASSIGNMENTS_URL)
        time.sleep(6)
    else:
        driver.get(ASSIGNMENTS_URL)
        time.sleep(6)

    if not _is_logged_in(driver):
        if password is None:
            password = os.environ.get("NGL_PASSWORD")
        if not password:
            password = getpass.getpass("NGL Spark password: ")

        for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
            if btn.is_displayed() and btn.text.strip().lower() == "got it":
                btn.click()
                time.sleep(1)
                break

        user_inputs = [
            e for e in driver.find_elements(By.CSS_SELECTOR, 'input[name="identifier"]') if e.is_displayed()
        ]
        if not user_inputs:
            sign_in_btns = [e for e in driver.find_elements(By.ID, "sign-in") if e.is_displayed()]
            if not sign_in_btns:
                sign_in_btns = [
                    e
                    for e in driver.find_elements(By.CSS_SELECTOR, "button")
                    if e.is_displayed() and e.text.strip().lower() == "sign in"
                ]
            if sign_in_btns:
                sign_in_btns[0].click()
                WebDriverWait(driver, 15).until(
                    lambda d: any(
                        e.is_displayed() for e in d.find_elements(By.CSS_SELECTOR, 'input[name="identifier"]')
                    )
                )

        user_el = next(
            e for e in driver.find_elements(By.CSS_SELECTOR, 'input[name="identifier"]') if e.is_displayed()
        )
        pass_el = next(
            e for e in driver.find_elements(By.CSS_SELECTOR, 'input[name="password"]') if e.is_displayed()
        )
        user_el.clear()
        user_el.send_keys(username)
        pass_el.clear()
        pass_el.send_keys(password, Keys.ENTER)
        del password

        try:
            WebDriverWait(driver, 40).until(lambda d: "dashboard" in d.current_url.lower())
        except Exception:
            driver.quit()
            raise RuntimeError("Login was not confirmed") from None
        time.sleep(6)
        driver.get(ASSIGNMENTS_URL)
        time.sleep(6)

    if not _is_logged_in(driver):
        driver.quit()
        raise RuntimeError("Login was not confirmed on dashboard/assignments")

    save_session(driver, username, folder)
    return driver, folder


def save_session(driver, username, folder):
    driver.switch_to.default_content()
    state = {
        "username": username.lower(),
        "cookies": driver.get_cookies(),
        "storage": driver.execute_script("return {...localStorage}"),
    }
    path = folder / "session.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(state, out)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test NGL Spark session persistence")
    parser.add_argument("username")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    d, f = open_session(args.username, headed=args.headed)
    try:
        print("URL:", d.current_url)
        print("FOLDER:", f)
        print(d.find_element(By.TAG_NAME, "body").text[:1500])
        d.save_screenshot(str(f / "dashboard.png"))
    finally:
        d.quit()
