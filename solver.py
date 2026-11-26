"""Reusable Avallain Buckminster frame switcher and solver for all 10 NGL Spark interaction types."""
import time
from selenium.webdriver.common.by import By


def switch_to_inner_frame(driver, timeout=30):
    """Switch into nested LTI -> Avallain lo-frame and wait until curInteraction.buildDone."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        driver.switch_to.default_content()
        outer_frames = driver.find_elements(
            By.CSS_SELECTOR, 'iframe.lti-loadable__fullscreen, iframe[src*="/api/interactives/"]'
        )
        if not outer_frames:
            outer_frames = driver.find_elements(By.TAG_NAME, "iframe")
        for outer in outer_frames:
            try:
                driver.switch_to.frame(outer)
                inner_frames = driver.find_elements(
                    By.CSS_SELECTOR, 'iframe.lo-frame, iframe[src*="/cdn_proxy/"]'
                )
                if not inner_frames:
                    inner_frames = driver.find_elements(By.TAG_NAME, "iframe")
                for inner in inner_frames:
                    try:
                        driver.switch_to.frame(inner)
                        ready = driver.execute_script(
                            """
                            return Boolean(
                                window.a5 &&
                                window.a5.mainBuilder &&
                                window.a5.mainBuilder.curInteraction &&
                                window.a5.mainBuilder.curInteraction.buildDone
                            );
                            """
                        )
                        if ready:
                            return True
                        driver.switch_to.parent_frame()
                    except Exception:
                        driver.switch_to.parent_frame()
                driver.switch_to.default_content()
            except Exception:
                driver.switch_to.default_content()
        time.sleep(0.5)
    return False


def get_screen_info(driver):
    return driver.execute_script(
        """
        const mb = window.a5.mainBuilder;
        const cur = mb.curInteraction;
        const list = mb.getInteractionsWithoutEndResult();
        return {
            index: cur.index,
            visibleIndex: cur.visibleIndex,
            identifier: cur.identifier,
            title: cur.title || '',
            totalScreens: list.length,
            isLast: cur === list[list.length - 1]
        };
        """
    )


def solve_current_screen(driver, expected_screen=None):
    """Solve all interaction models on the active Avallain screen without revealing answers."""
    res = driver.execute_script(
        """
        const cur = window.a5.mainBuilder.curInteraction;
        if (!cur || !cur.buildDone) {
            return { ok: false, error: 'Current interaction not ready' };
        }
        const items = [];
        const seen = new Set();
        const pushList = (arr) => {
            if (!Array.isArray(arr)) return;
            for (const x of arr) {
                if (x && !seen.has(x)) {
                    seen.add(x);
                    items.push(x);
                }
            }
        };
        if (cur.items && typeof cur.items.getItems === 'function') pushList(cur.items.getItems());
        if (cur.items && Array.isArray(cur.items.itemList)) pushList(cur.items.itemList);
        if (cur.blockItems && typeof cur.blockItems.getItems === 'function') pushList(cur.blockItems.getItems());
        if (cur.blockItems && Array.isArray(cur.blockItems.itemList)) pushList(cur.blockItems.itemList);

        const solved = [];
        const isPrefilled = (obj) => typeof obj.isPrefilled === 'function' && obj.isPrefilled();

        for (const dItem of items) {
            if ((dItem._class_name || '') === 'a5.models.interaction.ISDropdownModel' && !isPrefilled(dItem)) {
                if (dItem.selected && !dItem.selected.correct && typeof dItem.reset === 'function') {
                    dItem.reset();
                }
            }
        }

        for (const item of items) {
            const cls = item._class_name || '';

            // 1. Multiple Choice (Single Answer)
            if (cls === 'a5.models.interaction.ISRadiobuttonModel') {
                if (!isPrefilled(item) && item.correct) {
                    if (!item.correct.selected) {
                        item.correct.select();
                    }
                    solved.push({ cls, value: item.correct.text || item.correct.html });
                }
                continue;
            }

            // 2. Multiple Choice (Multiple Answer)
            if (cls === 'a5.models.interaction.ISCheckboxModel') {
                const picked = [];
                for (const ch of (item.choices || [])) {
                    if (isPrefilled(ch)) continue;
                    if (ch.correct) {
                        if (!ch.selected) ch.select();
                        picked.push(ch.text || ch.html);
                    } else if (ch.selected) {
                        ch.unselect();
                    }
                }
                solved.push({ cls, values: picked });
                continue;
            }

            // 3. Dropdown (preserves Attempt-1 correct items kept on retake / tryAgainKeep)
            if (cls === 'a5.models.interaction.ISDropdownModel') {
                if (!isPrefilled(item)) {
                    if (item.selected && item.selected.correct) {
                        solved.push({ cls, value: item.selected.text || item.selected.html, kept: true });
                        continue;
                    }
                    const choice = item.hasOrderedResponses
                        ? item.getCorrect()
                        : (typeof item.findNextCorrect === 'function' ? item.findNextCorrect() : item.getCorrect());
                    if (!choice) {
                        return { ok: false, error: 'Dropdown correct choice not found' };
                    }
                    item.setSelected(choice);
                    solved.push({ cls, value: choice.text || choice.html });
                }
                continue;
            }

            // 4. Fill in the Blank (Text gap)
            if (cls === 'a5.models.interaction.ICTextGap') {
                if (!isPrefilled(item) && typeof item.isCorrectable === 'function' && item.isCorrectable()) {
                    const ans = item.getCorrect();
                    item.setValue(ans);
                    if (typeof item.callViews === 'function') {
                        item.callViews('setInput');
                        item.callViews('_setFilled');
                    }
                    solved.push({ cls, value: ans });
                }
                continue;
            }

            // 5. FIB Drag & Drop (GapMatchInteraction — preserves Attempt-1 correct items on retake / tryAgainKeep)
            if (cls === 'a5.models.interaction.GapMatchInteraction') {
                for (const tda of (item.targetDropAreas || [])) {
                    const alreadyCorrect = typeof tda.isCorrect === 'function' && tda.isCorrect();
                    if (
                        !isPrefilled(tda)
                        && !alreadyCorrect
                        && tda.drag
                        && (typeof tda.isCorrectable !== 'function' || tda.isCorrectable())
                    ) {
                        const old = tda.drag;
                        tda.undrop(old);
                        if (old && old.home) old.home.drop(old);
                    }
                }
                const drops = [];
                for (const tda of (item.targetDropAreas || [])) {
                    if (isPrefilled(tda) || (typeof tda.isCorrectable === 'function' && !tda.isCorrectable())) {
                        continue;
                    }
                    if (typeof tda.isCorrect === 'function' && tda.isCorrect() && tda.drag) {
                        drops.push({ gap: tda.identifier, drag: tda.drag.identifier, kept: true });
                        continue;
                    }
                    let drag = typeof tda.getCorrect === 'function' ? tda.getCorrect() : null;
                    if (!drag) {
                        const candIds = (typeof tda.correct === 'function' ? tda.correct() : null)
                            || (item.mapGapToAnswer && item.mapGapToAnswer[tda.identifier])
                            || [];
                        for (const cid of candIds) {
                            const cand = item.getDragByIdentifier(cid);
                            if (cand && (cand.copyOnDrag || !_.some(item.targetDropAreas, o => o !== tda && o.drag === cand))) {
                                drag = cand;
                                break;
                            }
                        }
                    }
                    if (!drag) {
                        return { ok: false, error: 'Drag element not found for gap ' + tda.identifier };
                    }
                    if (tda.drag && tda.drag !== drag) {
                        const old = tda.drag;
                        tda.undrop(old);
                        if (old && old.home) old.home.drop(old);
                    }
                    if (tda.drag !== drag) {
                        if (drag.home && !drag.copyOnDrag) {
                            drag.home.undrop(drag);
                        }
                        tda.drop(drag);
                    }
                    drops.push({ gap: tda.identifier, drag: drag.identifier });
                }
                solved.push({ cls, drops });
                continue;
            }

            // 6. Crossword Puzzle
            if (cls === 'a5.model.interaction.ICCrossword') {
                let filledCount = 0;
                for (const cell of (item.cells || [])) {
                    if (!isPrefilled(cell) && cell.letter) {
                        cell.setValue(cell.letter);
                        filledCount += 1;
                    }
                }
                if (typeof item.notifyValueChange === 'function') {
                    item.notifyValueChange();
                }
                solved.push({ cls, filledCount });
                continue;
            }

            // 7. Sequencing
            if (cls === 'a5.models.interaction.ShuffleModel') {
                const len = (item.elements || []).length;
                for (let pos = 0; pos < len; pos++) {
                    const curEl = item.elements[pos];
                    if (curEl.fixed || isPrefilled(curEl)) continue;
                    const allowed = item.correct[pos] || [];
                    if (allowed.includes(curEl.identifier)) continue;
                    let targetIdx = -1;
                    for (let j = pos + 1; j < len; j++) {
                        if (!item.elements[j].fixed && allowed.includes(item.elements[j].identifier)) {
                            targetIdx = j;
                            break;
                        }
                    }
                    if (targetIdx === -1) {
                        return { ok: false, error: 'Sequencing target element not found at pos ' + pos };
                    }
                    item.swap(pos, targetIdx);
                }
                solved.push({ cls, order: item.elements.map(e => e.identifier) });
                continue;
            }

            // 8. Highlighting (IMMarkingBlock)
            if (cls === 'a5.model.interaction.IMMarkingBlock') {
                const marked = [];
                for (const node of (item.nodes || [])) {
                    if (isPrefilled(node)) continue;
                    if (typeof node.needsAnswer === 'function' && node.needsAnswer()) {
                        const colour = node.getCorrect();
                        if (!colour) {
                            return { ok: false, error: 'Marking colour not found for node' };
                        }
                        node.setMarked(colour);
                        marked.push(node.text || colour.identifier);
                    } else if (typeof node.isMarked === 'function' && node.isMarked()) {
                        node.clearUserData();
                    }
                }
                solved.push({ cls, marked });
                continue;
            }

            // 9. Text Correcting (ICTextCorrectionReplace / ICTextCorrectionSentence / ICTextCorrectionLine)
            if (cls === 'a5.model.interaction.ICTextCorrectionReplace') {
                const replaced = [];
                for (const node of (item.nodes || [])) {
                    if (isPrefilled(node)) continue;
                    if (typeof node.needsAnswer === 'function' && node.needsAnswer()) {
                        node.setValue(node.answers[0]);
                        replaced.push({ from: node.text, to: node.answers[0] });
                    } else if (typeof node.clearUserData === 'function') {
                        node.clearUserData();
                    }
                }
                if (item.userSelection === 'select_for_error' && typeof item.setChecked === 'function') {
                    item.setChecked(Boolean(item.hasAnswer()));
                } else if (item.userSelection === 'select_for_correct' && typeof item.setChecked === 'function') {
                    item.setChecked(!item.hasAnswer());
                }
                solved.push({ cls, replaced });
                continue;
            }

            if (cls === 'a5.models.interaction.ICTextCorrectionSentence') {
                if (!isPrefilled(item)) {
                    if (item.hasAnswer() && typeof item.getPermutations === 'function') {
                        const perms = item.getPermutations('', 0);
                        item.value = perms[0];
                        if (typeof item.callViews === 'function') item.callViews('updateValue');
                        item.notifyValueChange();
                    }
                    if (item.userSelection === 'select_for_error' && typeof item.setChecked === 'function') {
                        item.setChecked(Boolean(item.hasAnswer()));
                    } else if (item.userSelection === 'select_for_correct' && typeof item.setChecked === 'function') {
                        item.setChecked(!item.hasAnswer());
                    }
                }
                solved.push({ cls, value: item.value });
                continue;
            }

            // 10. Matching with Lines (LLModel / SDY15 / associateInteraction)
            if (cls === 'a5.models.interaction.LLModel') {
                if (!isPrefilled(item)) {
                    if (typeof item.clearUserData === 'function') {
                        item.clearUserData();
                    } else if (Array.isArray(item.lines)) {
                        item.lines = (item.prefilledLines || []).slice();
                    }
                    const pairs = typeof item.correctModels === 'function' ? item.correctModels() : [];
                    if (!pairs.length) {
                        return { ok: false, error: 'LLModel correctModels not found' };
                    }
                    for (const pair of pairs) {
                        if (Array.isArray(pair) && pair.length === 2 && pair[0] && pair[1]) {
                            item.addLine(pair[0], pair[1]);
                        }
                    }
                    solved.push({ cls, pairs: pairs.length });
                }
                continue;
            }
        }

        return { ok: true, identifier: cur.identifier, solved };
        """
    )
    if not res or not res.get("ok"):
        raise RuntimeError(f"Failed to solve screen: {res}")
    if expected_screen and res.get("identifier") != expected_screen.get("qti_identifier"):
        raise RuntimeError(
            f"QTI identifier mismatch: expected {expected_screen.get('qti_identifier')}, got {res.get('identifier')}"
        )
    return res
