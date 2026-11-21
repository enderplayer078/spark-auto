"""Read back live Avallain interaction results/answers. Never submit unless 100% verified."""


def verify_screen(driver, expected_screen=None):
    """Verify current screen has total > 0, correct == total, all filled, and revealed == 0."""
    state = driver.execute_async_script(
        """
        const done = arguments[arguments.length - 1];
        const cur = window.a5 && window.a5.mainBuilder && window.a5.mainBuilder.curInteraction;
        if (!cur) {
            done({ ok: false, error: 'Missing curInteraction' });
            return;
        }
        $.when(cur.getResultsAsync(), typeof cur.getUserAnswers === 'function' ? cur.getUserAnswers() : null)
            .done(function(res, userAnswers) {
                done({
                    ok: true,
                    index: cur.index,
                    identifier: cur.identifier,
                    total: res.total(),
                    correct: res.correct(),
                    filled: res.filled(),
                    revealed: res.revealed(),
                    percent: res.percent(),
                    allFilled: res.areAllFilled(),
                    isPercent100: res.isPercent100(),
                    userAnswers: userAnswers
                });
            })
            .fail(function(err) {
                done({ ok: false, error: String(err) });
            });
        """
    )
    if not state or not state.get("ok"):
        raise RuntimeError("PRECHECK failed: " + repr(state))
    if expected_screen and state.get("identifier") != expected_screen.get("qti_identifier"):
        raise RuntimeError(
            f"PRECHECK identifier mismatch: expected {expected_screen.get('qti_identifier')}, got {state.get('identifier')}"
        )
    if (
        state["total"] <= 0
        or state["correct"] != state["total"]
        or not state["allFilled"]
        or state["revealed"] != 0
        or not state["isPercent100"]
    ):
        raise RuntimeError("PRECHECK mismatch: " + repr(state))
    return state


def verify_all_screens_before_submit(driver):
    """Verify every interaction screen in the activity is 100% correct with 0 revealed before submitting."""
    summary = driver.execute_async_script(
        """
        const done = arguments[arguments.length - 1];
        const mb = window.a5 && window.a5.mainBuilder;
        if (!mb) {
            done({ ok: false, error: 'Missing mainBuilder' });
            return;
        }
        const list = mb.getInteractionsWithoutEndResult();
        const promises = list.map(inter => inter.getResultsAsync().then(res => ({
            index: inter.index,
            identifier: inter.identifier,
            total: res.total(),
            correct: res.correct(),
            filled: res.filled(),
            revealed: res.revealed(),
            percent: res.percent(),
            allFilled: res.areAllFilled(),
            isPercent100: res.isPercent100()
        })));
        $.when.apply($, promises).done(function() {
            const screens = Array.prototype.slice.call(arguments);
            done({ ok: true, screens });
        }).fail(function(err) {
            done({ ok: false, error: String(err) });
        });
        """
    )
    if not summary or not summary.get("ok"):
        raise RuntimeError("PRECHECK all screens failed: " + repr(summary))
    screens = summary["screens"]
    if not screens:
        raise RuntimeError("PRECHECK all screens failed: empty interaction list")
    for s in screens:
        if (
            s["total"] <= 0
            or s["correct"] != s["total"]
            or not s["allFilled"]
            or s["revealed"] != 0
            or not s["isPercent100"]
        ):
            raise RuntimeError("PRECHECK all screens mismatch: " + repr(s))
    return screens
