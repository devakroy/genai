# helpers/dynamic_select.py
from __future__ import annotations

from playwright.async_api import Page, Locator

async def _closest_clickable_wrapper(target: Locator) -> Locator | None:
    """
    Find a sensible clickable wrapper near a control:
    - enclosing <label>
    - nearest ancestor with role or class that often acts as a card/label
    """
    # 1) direct label
    lbl = target.locator("xpath=ancestor::label[1]")
    if await lbl.count() > 0:
        return lbl

    # 2) nearest reasonable wrapper (very generic)
    wrap = target.locator(
        "xpath=ancestor::*["
        " @role='radio' or @role='checkbox' or @for or "
        " contains(@class,'label') or contains(@class,'card') or "
        " contains(@class,'option') or contains(@class,'select')"
        "][1]"
    )
    if await wrap.count() > 0:
        return wrap

    return None


async def _resolve_radio_like(locator: Locator) -> Locator:
    """
    From any point in the card/label tree, resolve a 'radio-like' target:
    - input[type=radio], input[type=checkbox]
    - element with role='radio' or role='checkbox'
    Prefer a **descendant**; fall back to **self**; then **ancestor**.
    """
    # descendant
    cand = locator.locator("xpath=.//input[@type='radio' or @type='checkbox'] | .//*[@role='radio' or @role='checkbox']")
    if await cand.count() > 0:
        return cand.first

    # self
    self_like = locator.locator("xpath=self::input[@type='radio' or @type='checkbox'] | self::*[@role='radio' or @role='checkbox']")
    if await self_like.count() > 0:
        return self_like.first

    # ancestor
    anc = locator.locator("xpath=ancestor::input[@type='radio' or @type='checkbox'] | ancestor::*[@role='radio' or @role='checkbox'][1]")
    if await anc.count() > 0:
        return anc.first

    # last resort: just return the original locator
    return locator


async def _is_checked(page: Page, loc: Locator) -> bool:
    """
    Checked semantics for both native and ARIA controls.
    """
    try:
        # native input
        tag = await loc.evaluate("el => el.tagName?.toLowerCase?.()")
        if tag == "input":
            typ = await loc.evaluate("el => el.type?.toLowerCase?.()")
            if typ in {"radio", "checkbox"}:
                return await loc.is_checked()

        # ARIA role
        role = await loc.get_attribute("role")
        if role in {"radio", "checkbox"}:
            aria = await loc.get_attribute("aria-checked")
            return (aria or "").lower() == "true"
    except Exception:
        pass
    return False


async def _stabilize_checked(page: Page, loc: Locator, ms: int = 350) -> bool:
    """
    Ensure the element stays checked for a brief 'stability' window (handles re-renders).
    """
    try:
        return await page.wait_for_function(
            """
            (node, ms) => new Promise(resolve => {
              if (!node || !node.isConnected) return resolve(false);
              const start = performance.now();
              const isChecked = (el) => {
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'input') {
                  const typ = (el.type || '').toLowerCase();
                  if (typ === 'radio' || typ === 'checkbox') return !!el.checked;
                }
                const role = (el.getAttribute('role') || '').toLowerCase();
                if (role === 'radio' || role === 'checkbox') {
                  const aria = (el.getAttribute('aria-checked') || '').toLowerCase();
                  return aria === 'true';
                }
                return false;
              };
              const tick = () => {
                if (!node.isConnected) return resolve(false);
                if (!isChecked(node)) return resolve(false);
                if (performance.now() - start >= ms) return resolve(true);
                requestAnimationFrame(tick);
              };
              requestAnimationFrame(tick);
            })
            """,
            loc,
            ms,
            timeout=2000,
        )
    except Exception:
        return False


async def ensure_selected(page: Page, any_locator: Locator) -> None:
    """
    Pure dynamic: make a radio/checkbox end up selected and remain selected briefly.
    Accepts ANY locator inside/near the control card; resolves the actual control,
    handles hydrate flips, wrapper clicking, and ARIA/native semantics.
    """
    # Allow initial hydration/flip to occur
    try:
        await page.wait_for_load_state("networkidle", timeout=2000)
    except Exception:
        pass
    await page.wait_for_timeout(80)

    target = await _resolve_radio_like(any_locator)

    # Fast pass: already selected?
    if await _is_checked(page, target):
        if await _stabilize_checked(page, target):  # stays selected
            return

    # 1) Try the semantic APIs where applicable
    try:
        # works for input[type=radio|checkbox]
        await target.check(timeout=2500)
    except Exception:
        # 2) Click the control directly
        try:
            await target.click(timeout=2500)
        except Exception:
            # 3) Click the nearest label/wrapper
            wrap = await _closest_clickable_wrapper(target)
            if wrap:
                try:
                    await wrap.click(timeout=2500)
                except Exception:
                    await wrap.click(force=True, timeout=2500)
            else:
                # 4) Last resort JS click
                try:
                    await target.evaluate("el => el.click()")
                except Exception:
                    pass

    # Let any subsequent re-render happen
    await page.wait_for_timeout(80)

    # Verify & stabilize; one optional retry via wrapper if needed
    if not await _is_checked(page, target):
        wrap = await _closest_clickable_wrapper(target)
        if wrap:
            try:
                await wrap.click(timeout=2000)
            except Exception:
                await wrap.click(force=True, timeout=2000)

    ok = await _stabilize_checked(page, target, ms=400)
    if not ok:
        raise RuntimeError("Selection didn’t stabilize (flipped back after click).")