"""Multi-account automated runner for NGL Spark (learn.eltngl.com)."""
import argparse
import json
import random
import re
import time
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from account_session import ROOT, open_session, save_session
import answer_guard
import solver

ASSIGNMENTS_HOME = "https://learn.eltngl.com/dashboard/assignments"
UNIT_PREFIX_RE = re.compile(r"^\s*(Unit\s+\d+)\b", re.IGNORECASE)


def _extract_unit_label(name: str) -> str:
    m = UNIT_PREFIX_RE.match(str(name or ""))
    return m.group(1).strip().lower() if m else ""


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


def _paced_sleep(driver, seconds: float, heartbeat_callback=None):
    """Sleep in chunks while firing mousemove heartbeats inside the active frame to keep idleTime=0."""
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    next_hb = time.monotonic() + random.uniform(12.0, 18.0)
    next_active_ping = time.monotonic() + 45.0
    while True:
        now = time.monotonic()
        rem = deadline - now
        if rem <= 0:
            break
        time.sleep(min(rem, 3.0))
        now = time.monotonic()
        if now >= next_hb:
            try:
                driver.execute_script(
                    """
                    const x = Math.floor(180 + Math.random() * 520);
                    const y = Math.floor(140 + Math.random() * 340);
                    window.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: x, clientY: y }));
                    document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: x, clientY: y }));
                    """
                )
            except Exception:
                pass
            next_hb = now + random.uniform(12.0, 18.0)
        if heartbeat_callback and now >= next_active_ping:
            try:
                heartbeat_callback()
            except Exception:
                pass
            next_active_ping = now + 45.0


def _build_activity_budgets(count: int, target_total_minutes: float) -> list[float]:
    """Distribute target_total_minutes across remaining activities with natural random variance."""
    if count <= 0 or target_total_minutes <= 0:
        return [0.0] * max(0, count)
    # Subtract ~7s overhead per activity (loading + dashboard verify) so wall-clock matches target
    total_active_sec = max(0.0, target_total_minutes * 60.0 - count * 7.0)
    if total_active_sec <= 0:
        return [0.0] * count
    weights = [random.uniform(0.7, 1.3) for _ in range(count)]
    w_sum = sum(weights) or 1.0
    return [(w / w_sum) * total_active_sec for w in weights]


def _flush_activity_close(driver):
    """Trigger Avallain xAPI terminated statement (keepalive) cleanly after submit."""
    try:
        driver.execute_script(
            """
            try {
                if (window.a5 && window.a5.mainBuilder && typeof window.a5.mainBuilder.updateDuration === 'function') {
                    window.a5.mainBuilder.updateDuration();
                }
                if (window.a5 && window.a5.service && window.a5.service.lms && window.a5.service.lms.API && typeof window.a5.service.lms.API.close === 'function') {
                    window.a5.service.lms.API.close();
                }
            } catch (_) {}
            """
        )
    except Exception:
        pass


def run_activity(
    driver,
    url,
    row,
    folder,
    activity_db=None,
    quiet_logs=False,
    activity_budget_seconds: float = 0.0,
    heartbeat_callback=None,
):
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
    activity_started = time.monotonic()

    per_screen_wait = 0.0
    if activity_budget_seconds > 0 and total_screens > 0:
        # Allocate ~80% of budget across screens and ~20% for pre-submit review inside lo-frame
        per_screen_wait = (activity_budget_seconds * 0.8) / total_screens

    for s_idx in range(total_screens):
        WebDriverWait(driver, 20, poll_frequency=0.25).until(
            lambda d: d.execute_script(
                "return Boolean(window.a5 && window.a5.mainBuilder && window.a5.mainBuilder.curInteraction && window.a5.mainBuilder.curInteraction.buildDone);"
            )
        )
        cur_info = solver.get_screen_info(driver)
        expected = expected_screens[s_idx] if s_idx < len(expected_screens) else None
        if not quiet_logs:
            print(
                f"  [{name}] screen {s_idx + 1}/{total_screens} ({cur_info['identifier']})",
                flush=True,
            )

        if per_screen_wait > 0:
            _paced_sleep(driver, per_screen_wait * random.uniform(0.8, 1.2), heartbeat_callback)

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

    # Pace remaining activity budget INSIDE the open lo-frame BEFORE submit so xAPI duration counts it!
    if activity_budget_seconds > 0:
        rem_in_activity = activity_budget_seconds - (time.monotonic() - activity_started)
        if rem_in_activity > 0:
            _paced_sleep(driver, rem_in_activity, heartbeat_callback)

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

    _flush_activity_close(driver)
    time.sleep(2)


def run_remaining(
    driver,
    folder,
    username,
    filter_text=None,
    min_activity_seconds=0,
    progress_callback=None,
    heartbeat_callback=None,
    target_total_minutes: float = 0.0,
    max_units: int = 0,
    max_activities: int = 0,
    persist_session=True,
    quiet_logs=False,
):
    url = assignment_url(driver, folder)

    try:
        rows = load_dashboard_rows(driver, url)
        if not quiet_logs:
            print(f"Loaded {len(rows)} activities on dashboard", flush=True)
        total_count = len(rows)
        done_count = sum(1 for r in rows if is_row_completed(r) or is_row_attempts_exhausted(r))
        if progress_callback:
            progress_callback(done_count, total_count, "ตรวจสอบรายการแบบฝึกหัด...")

        candidate_names = [
            r["name"]
            for r in rows
            if (not filter_text or filter_text in r["name"])
            and not is_row_completed(r)
            and not is_row_attempts_exhausted(r)
        ]

        selected_names: list[str] = []
        if max_units and max_units > 0:
            selected_units: list[str] = []
            has_unit_labels = any(_extract_unit_label(nm) for nm in candidate_names)
            if has_unit_labels:
                for nm in candidate_names:
                    ulab = _extract_unit_label(nm)
                    if ulab:
                        if ulab not in selected_units:
                            if len(selected_units) >= max_units:
                                continue
                            selected_units.append(ulab)
                        selected_names.append(nm)
            else:
                selected_names = candidate_names[: max_units * 10]
        else:
            selected_names = list(candidate_names)

        if max_activities and max_activities > 0:
            selected_names = selected_names[:max_activities]

        selected_name_set = set(selected_names)
        is_limited_run = bool((max_units and max_units > 0) or (max_activities and max_activities > 0))

        # Scale pacing duration proportionally so partial runs + remaining runs add up naturally
        effective_target_minutes = float(target_total_minutes or 0.0)
        if effective_target_minutes > 0 and total_count > 0 and len(selected_names) < total_count:
            effective_target_minutes = effective_target_minutes * (len(selected_names) / float(total_count))

        budgets = _build_activity_budgets(len(selected_names), effective_target_minutes)
        budget_map = dict(zip(selected_names, budgets))
        ran_this_session = 0

        for row in rows:
            name = row["name"]
            if name not in selected_name_set:
                continue
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
            activity_budget = max(float(min_activity_seconds or 0), float(budget_map.get(name, 0.0)))

            for attempt in range(1, 4):
                try:
                    run_activity(
                        driver,
                        url,
                        row,
                        folder,
                        quiet_logs=quiet_logs,
                        activity_budget_seconds=activity_budget,
                        heartbeat_callback=heartbeat_callback,
                    )
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

            ran_this_session += 1
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
        completed_count = sum(1 for r in rows if is_row_completed(r) or is_row_attempts_exhausted(r))
        full_score_count = sum(1 for r in rows if is_row_completed(r))
        all_completed = len(rows) > 0 and completed_count >= len(rows)

        if not filter_text and not is_limited_run and not all_completed:
            raise RuntimeError(f"Incomplete activities remain ({completed_count}/{len(rows)})")

        if all_completed and not filter_text:
            report = save_completion_report(driver, folder, rows)
            if progress_callback:
                progress_callback(len(rows), len(rows), "ทำแบบฝึกหัดครบ 100% ทุกข้อแล้ว")
            if not quiet_logs:
                print(
                    f"ALL COMPLETE: {report['completed']} activities, {report['graded_full_score']} at 100%",
                    flush=True,
                )
        elif progress_callback:
            progress_callback(completed_count, len(rows), f"ทำเสร็จแล้ว {completed_count}/{len(rows)} ข้อ")

        return {
            "total": len(rows),
            "completed": completed_count,
            "graded_full_score": full_score_count,
            "all_completed": all_completed,
            "partial": not all_completed,
            "ran_this_session": ran_this_session,
        }
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
    parser.add_argument(
        "--max-units",
        type=int,
        default=0,
        help="Optional limit on number of incomplete units to complete (0 = all)",
    )
    parser.add_argument(
        "--max-activities",
        type=int,
        default=0,
        help="Optional hard limit on number of activities to complete (0 = all)",
    )
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    if not 0 <= args.min_activity_seconds <= 3600:
        parser.error("Minimum activity seconds must be between 0 and 3600")

    d, folder = open_session(args.username, headed=args.headed)
    try:
        summary = run_remaining(
            d,
            folder,
            args.username,
            filter_text=args.filter_text,
            min_activity_seconds=args.min_activity_seconds,
            max_units=max(0, args.max_units),
            max_activities=max(0, args.max_activities),
        )
        print("SUMMARY:", summary, flush=True)
    finally:
        d.quit()
