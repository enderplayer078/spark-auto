"""Multi-account automated runner for NGL Spark (learn.eltngl.com)."""
import argparse
import json
import re
import time
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from account_session import ROOT, open_session, save_session
import answer_guard
import solver

ASSIGNMENTS_HOME = "https://learn.eltngl.com/dashboard/assignments"


def _validate_ngl_url(url: str) -> str:
    if not isinstance(url, str) or not url.startswith("https://learn.eltngl.com/"):
        raise RuntimeError("Unexpected assignment URL host")
    return url


def assignment_url(driver, folder):
    config = folder / "assignment.json"
    if config.exists():
        return _validate_ngl_url(json.loads(config.read_text())["url"])

    driver.switch_to.default_content()
    driver.get(ASSIGNMENTS_HOME)
    WebDriverWait(driver, 30).until(
        lambda d: d.find_elements(By.CSS_SELECTOR, '[data-qa-id="assignmentCard"] a[href]')
    )
    links = [
        e.get_attribute("href")
        for e in driver.find_elements(By.CSS_SELECTOR, '[data-qa-id="assignmentCard"] a[href]')
        if e.is_displayed() and (e.get_attribute("href") or "").startswith("https://learn.eltngl.com/")
    ]
    if not links:
        raise RuntimeError(f"No assignment card found on dashboard ({config})")
    url = _validate_ngl_url(links[0])
    config.write_text(json.dumps({"url": url}, indent=2) + "\n")
    return url


def load_dashboard_rows(driver, url):
    driver.switch_to.default_content()
    driver.get(url)
    WebDriverWait(driver, 30).until(
        lambda d: len(d.find_elements(By.CSS_SELECTOR, '[data-qa-id="activityRow"]')) > 0
    )
    time.sleep(2)
    if "/dashboard" not in driver.current_url.lower():
        raise RuntimeError("Session expired; log in again before resuming")
    rows = driver.execute_script(
        """
        return Array.from(document.querySelectorAll('[data-qa-id="activityRow"]')).map((row, idx) => {
            const titleEl = row.querySelector('span[title]');
            const name = titleEl ? titleEl.getAttribute('title').trim() : '';
            const text = (row.innerText || '').trim();
            return { idx, name, text };
        });
        """
    )
    return rows


def is_row_completed(row):
    text = row["text"]
    if re.search(r"Score\s+100\s*%", text):
        return True
    m = re.search(r"Score\s+(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        return den > 0 and num >= den
    return False


def is_row_attempts_exhausted(row):
    m = re.search(r"Attempts\s+(\d+)\s*/\s*(\d+)", row.get("text", ""))
    if m:
        used, allowed = int(m.group(1)), int(m.group(2))
        return allowed > 0 and used >= allowed
    return False


def verify_saved(driver, url, name):
    for delay in (3, 6, 12):
        rows = load_dashboard_rows(driver, url)
        match = next((r for r in rows if r["name"] == name), None)
        if match and is_row_completed(match):
            return rows
        time.sleep(delay)
    raise RuntimeError(f"Dashboard did not confirm 100% completion for: {name}")


def save_completion_report(driver, folder, rows):
    for row in rows:
        if not is_row_completed(row) and not is_row_attempts_exhausted(row):
            raise RuntimeError(f"Activity not complete or exhausted: {row['name']}")
    completed_count = sum(1 for r in rows if is_row_completed(r) or is_row_attempts_exhausted(r))
    full_score_count = sum(1 for r in rows if is_row_completed(r))
    report = {
        "completed": completed_count,
        "graded_full_score": full_score_count,
        "verified_at": time.time(),
        "activities": rows,
    }
    (folder / "completion_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    driver.save_screenshot(str(folder / "completed.png"))
    return report


def run_activity(driver, url, row, folder, activity_db=None, quiet_logs=False):
    name = row["name"]
    driver.switch_to.default_content()
    clicked = driver.execute_script(
        """
        const targetName = arguments[0];
        const rows = Array.from(document.querySelectorAll('[data-qa-id="activityRow"]'));
        const row = rows.find(r => {
            const el = r.querySelector('span[title]');
            return el && el.getAttribute('title').trim() === targetName;
        });
        if (!row) return false;
        row.scrollIntoView({ block: 'center' });
        row.click();
        return true;
        """,
        name,
    )
    if not clicked:
        raise RuntimeError(f"Activity row not found: {name}")

    if not solver.switch_to_inner_frame(driver, timeout=35):
        raise RuntimeError(f"Activity inner frame did not load for: {name}")

    # Ensure we start from the first interaction screen (in case a5_restore jumped ahead)
    driver.execute_script(
        """
        const mb = window.a5.mainBuilder;
        const list = mb.getInteractionsWithoutEndResult();
        if (list.length > 0 && mb.curInteraction !== list[0]) {
            mb.goTo(list[0].index);
        }
        """
    )
    time.sleep(0.5)

    info = solver.get_screen_info(driver)
    total_screens = info["totalScreens"]
    expected_screens = activity_db.get("screens", []) if activity_db else []

    for s_idx in range(total_screens):
        WebDriverWait(driver, 20, poll_frequency=0.25).until(
            lambda d: d.execute_script(
                "return Boolean(window.a5 && window.a5.mainBuilder && window.a5.mainBuilder.curInteraction && window.a5.mainBuilder.curInteraction.buildDone);"
            )
        )
        cur_info = solver.get_screen_info(driver)
        expected = expected_screens[s_idx] if s_idx < len(expected_screens) else None
        print(
            f"  [{name}] screen {s_idx + 1}/{total_screens} ({cur_info['identifier']})",
            flush=True,
        )

        solver.solve_current_screen(driver, expected_screen=expected)
        verified = answer_guard.verify_screen(driver, expected_screen=expected)

        with (folder / "verified_answers.jsonl").open("a") as out:
            out.write(
                json.dumps(
                    {
                        "activity": name,
                        "screen": s_idx + 1,
                        "identifier": verified["identifier"],
                        "correct": verified["correct"],
                        "total": verified["total"],
                        "revealed": verified["revealed"],
                        "userAnswers": verified.get("userAnswers"),
                        "time": time.time(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        if s_idx < total_screens - 1:
            prev_idx = cur_info["index"]
            driver.execute_script("return window.a5.mainBuilder.goNext();")
            WebDriverWait(driver, 20, poll_frequency=0.25).until(
                lambda d: d.execute_script(
                    "const c = window.a5.mainBuilder.curInteraction; return Boolean(c && c.buildDone && c.index !== arguments[0]);",
                    prev_idx,
                )
            )

    all_screens = answer_guard.verify_all_screens_before_submit(driver)
    total_pts = sum(s["total"] for s in all_screens)
    correct_pts = sum(s["correct"] for s in all_screens)
    if not quiet_logs:
        print(f"  [{name}] pre-submit verified: {correct_pts}/{total_pts} across {len(all_screens)} screens", flush=True)

    submit_res = driver.execute_async_script(
        """
        const done = arguments[arguments.length - 1];
        const mb = window.a5.mainBuilder;
        let submitPayload = null;
        const onSubmit = function(data) {
            submitPayload = {
                correct: data && data.score ? data.score.correct() : null,
                total: data && data.score ? data.score.total() : null,
                scaled: data && data.score ? data.score.scaled() : null,
                percent: data && data.score ? data.score.percent() : null
            };
        };
        window.a5.eventBus.bind('lo:submit', onSubmit);
        $.when(mb.submit({ noConfirm: true }))
            .done(function() {
                setTimeout(function() {
                    window.a5.eventBus.unbind('lo:submit', onSubmit);
                    const r = mb.resultsGenerator && mb.resultsGenerator.getResults ? mb.resultsGenerator.getResults() : null;
                    done({
                        ok: true,
                        submitPayload: submitPayload,
                        generator: r ? {
                            correct: r.correct(),
                            total: r.total(),
                            scaled: r.scaled(),
                            percent: r.percent()
                        } : null
                    });
                }, 1500);
            })
            .fail(function(err) {
                window.a5.eventBus.unbind('lo:submit', onSubmit);
                done({ ok: false, error: String(err) });
            });
        """
    )
    if not quiet_logs:
        print(f"  RESULT {name}: {submit_res}", flush=True)
    if not submit_res or not submit_res.get("ok"):
        raise RuntimeError(f"Submit failed for {name}")

    score_info = submit_res.get("submitPayload") or submit_res.get("generator")
    if not score_info or score_info["correct"] != score_info["total"] or score_info["percent"] != 100:
        raise RuntimeError(f"Score needs review for {name}")

    with (folder / "results.jsonl").open("a") as out:
        out.write(
            json.dumps(
                {
                    "activity": name,
                    "score": [score_info["correct"], score_info["total"]],
                    "percent": score_info["percent"],
                    "time": time.time(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    time.sleep(2)


def run_remaining(
    driver,
    folder,
    username,
    filter_text=None,
    min_activity_seconds=0,
    progress_callback=None,
    persist_session=True,
    quiet_logs=False,
):
    url = assignment_url(driver, folder)

    try:
        rows = load_dashboard_rows(driver, url)
        print(f"Loaded {len(rows)} activities on dashboard", flush=True)
        total_count = len(rows)
        done_count = sum(1 for r in rows if is_row_completed(r) or is_row_attempts_exhausted(r))
        if progress_callback:
            progress_callback(done_count, total_count, "ตรวจสอบรายการแบบฝึกหัด...")

        for row in rows:
            name = row["name"]
            if filter_text and filter_text not in name:
                continue
            if is_row_completed(row):
                continue
            if is_row_attempts_exhausted(row):
                if not quiet_logs:
                    print(f"SKIP {name} (attempts already exhausted on dashboard)", flush=True)
                continue

            if not quiet_logs:
                print(f"START {name}", flush=True)
            if progress_callback:
                progress_callback(done_count, total_count, f"กำลังทำ: {name}")
            started = time.monotonic()

            for attempt in range(1, 4):
                try:
                    run_activity(driver, url, row, folder, quiet_logs=quiet_logs)
                    rows = verify_saved(driver, url, name)
                    break
                except Exception as exc:
                    if quiet_logs:
                        print(f"RETRY {attempt}/3: {type(exc).__name__}", flush=True)
                    else:
                        print(f"RETRY {attempt}/3 {name}: {type(exc).__name__}", flush=True)
                    if attempt == 3:
                        if quiet_logs:
                            print(f"STOP: {type(exc).__name__}", flush=True)
                        else:
                            print(f"STOP {name}: {type(exc).__name__}", flush=True)
                        if persist_session:
                            try:
                                driver.switch_to.default_content()
                                (folder / "failure.html").write_text(driver.page_source)
                                driver.save_screenshot(str(folder / "failure.png"))
                            except Exception:
                                pass
                        raise
                    time.sleep(4)
                    rows = load_dashboard_rows(driver, url)
                    latest = next((r for r in rows if r["name"] == name), None)
                    if latest and (is_row_completed(latest) or is_row_attempts_exhausted(latest)):
                        break
                    if latest:
                        row = latest

            if persist_session:
                save_session(driver, username, folder)
            done_count = sum(1 for r in rows if is_row_completed(r) or is_row_attempts_exhausted(r))
            if not quiet_logs:
                print(f"VERIFIED 100% on dashboard: {name}", flush=True)
            if progress_callback:
                progress_callback(done_count, total_count, f"สำเร็จแล้ว: {name}")

            remaining = min_activity_seconds - (time.monotonic() - started)
            if remaining > 0:
                if not quiet_logs:
                    print(f"PACE waiting {remaining:.1f}s before next activity", flush=True)
                time.sleep(remaining)

        rows = load_dashboard_rows(driver, url)
        (folder / "progress.txt").write_text(
            "\n".join(f"{r['name']} -> {r['text']}" for r in rows) + "\n"
        )
        if not filter_text:
            report = save_completion_report(driver, folder, rows)
            if progress_callback:
                progress_callback(len(rows), len(rows), "ทำแบบฝึกหัดครบ 100% ทุกข้อแล้ว")
            print(
                f"ALL COMPLETE: {report['completed']} activities, {report['graded_full_score']} at 100%",
                flush=True,
            )
    finally:
        if persist_session:
            save_session(driver, username, folder)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run NGL Spark assignments for an account")
    parser.add_argument("username")
    parser.add_argument("filter_text", nargs="?")
    parser.add_argument(
        "--min-activity-seconds",
        type=float,
        default=0,
        help="Minimum wall-clock time per activity, including post-save waiting",
    )
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    if not 0 <= args.min_activity_seconds <= 3600:
        parser.error("Minimum activity seconds must be between 0 and 3600")

    d, folder = open_session(args.username, headed=args.headed)
    try:
        run_remaining(d, folder, args.username, args.filter_text, args.min_activity_seconds)
    finally:
        d.quit()
