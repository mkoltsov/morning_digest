"""aliexpress_tracker.py — Extract AliExpress orders directly from the Orders page via CDP."""
from __future__ import annotations

import html as _html
import json
import math
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import websocket

from config import ALIEXPRESS_PASS, ALIEXPRESS_USER

CDP_URL = "http://127.0.0.1:9222"
BRAVE_EXE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
BRAVE_PROFILE = r"C:\temp\morning_digest_aliexpress"
CHROMIUM_PROFILE = Path.home() / ".cache" / "morning_digest_aliexpress_chromium"
ORDERS_URL = "https://www.aliexpress.com/p/order/index.html"


def _esc(value: str) -> str:
    return _html.escape(str(value or ""))


def _image_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith(("http://", "https://")):
        return ""
    if "272x80.png" in value or value.startswith("data:"):
        return ""
    return value


def _http_json(path: str):
    with urllib.request.urlopen(f"{CDP_URL}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ensure_browser():
    browser = (
        shutil.which("cmd.exe")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )
    if not browser:
        raise RuntimeError("No CDP-capable browser found for AliExpress automation")

    for _ in range(2):
        try:
            _http_json("/json/version")
            return
        except Exception:
            pass

        if browser.endswith("cmd.exe"):
            subprocess.run(
                [
                    browser,
                    "/c",
                    "start",
                    '""',
                    BRAVE_EXE,
                    "--remote-debugging-port=9222",
                    "--remote-allow-origins=*",
                    f"--user-data-dir={BRAVE_PROFILE}",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            CHROMIUM_PROFILE.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                [
                    browser,
                    "--remote-debugging-port=9222",
                    "--remote-allow-origins=*",
                    f"--user-data-dir={CHROMIUM_PROFILE}",
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "--no-first-run",
                    "--no-default-browser-check",
                    ORDERS_URL,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        time.sleep(5)

    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _http_json("/json/version")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Headless Chromium CDP is unavailable on port 9222")


class _CDPPage:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=40)
        self.msg_id = 0
        self._call("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        })

    def close(self):
        self.ws.close()

    def _call(self, method: str, params: dict | None = None):
        self.msg_id += 1
        msg_id = self.msg_id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            payload = json.loads(raw)
            if payload.get("id") == msg_id:
                return payload

    def eval(self, expression: str, await_promise: bool = False):
        result = self._call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        return result.get("result", {}).get("result", {}).get("value")

    def navigate(self, url: str):
        self._call("Page.navigate", {"url": url})

    def sleep_eval(self, ms: int, expression: str):
        wrapped = (
            "new Promise(r=>setTimeout(()=>r("
            + expression
            + f"),{ms}))"
        )
        return self.eval(wrapped, await_promise=True)


def _get_page_socket() -> str:
    pages = _http_json("/json/list")
    for page in pages:
        if page.get("title") == "Orders":
            return page["webSocketDebuggerUrl"]
    for page in pages:
        if "aliexpress" in page.get("url", ""):
            return page["webSocketDebuggerUrl"]
    if not pages:
        raise RuntimeError("No browser pages available for CDP")
    return pages[0]["webSocketDebuggerUrl"]


def _login_if_needed(page: _CDPPage):
    body = page.eval("(document.body && document.body.innerText) || ''") or ""
    if not any(x in body.lower() for x in ["sign in", "log in", "email or phone number", "password"]):
        return
    if not ALIEXPRESS_USER or not ALIEXPRESS_PASS:
        raise RuntimeError("AliExpress login required, but credentials are missing")

    js = f"""
    (() => {{
      const user = {json.dumps(ALIEXPRESS_USER)};
      const password = {json.dumps(ALIEXPRESS_PASS)};
      const valueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      function fill(el, value) {{
        if (!el) return false;
        el.focus();
        valueSetter.call(el, value);
        el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: value }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        return true;
      }}
      function byText(pattern) {{
        return [...document.querySelectorAll('button, [role="button"]')]
          .find(el => pattern.test((el.innerText || el.getAttribute('aria-label') || '').trim()));
      }}
      function click(el) {{
        if (!el) return false;
        el.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true }}));
        el.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true }}));
        el.click();
        return true;
      }}
      const email = document.querySelector('input[aria-label*="Email"], input[type="email"], input[name="email"], input[name="loginId"], input[type="text"]');
      const pass = document.querySelector('input[type="password"], input[aria-label*="Password"], input[name="password"]');
      const filledEmail = email ? fill(email, user) : false;
      const filledPass = pass ? fill(pass, password) : false;
      if (filledPass) {{
        click(byText(/^(sign in|log in|continue)$/i));
      }} else if (filledEmail) {{
        click(byText(/^continue$/i) || byText(/^(sign in|log in)$/i));
      }}
      return {{ filledEmail, filledPass }};
    }})()
    """
    state = page.eval(js) or {}
    time.sleep(5)
    _solve_slide_verification(page)
    if state.get("filledEmail") and not state.get("filledPass"):
        page.eval(js)
        time.sleep(8)
        _solve_slide_verification(page)
    after = page.eval("(document.body && document.body.innerText) || ''") or ""
    if any(x in after.lower() for x in ["verification code", "security verification", "enter the code"]):
        raise RuntimeError("AliExpress login requires a verification code in the browser profile")
    if any(x in after.lower() for x in ["email or phone number", "password"]) and "my orders" not in after.lower():
        raise RuntimeError("AliExpress login did not complete in headless browser")


def _solve_slide_verification(page: _CDPPage) -> bool:
    """Handle AliExpress' simple slide-to-verify challenge when it appears."""
    frame_tree = page._call("Page.getFrameTree").get("result", {}).get("frameTree", {})
    child_frames = frame_tree.get("childFrames") or []
    challenge = next(
        (
            child["frame"]
            for child in child_frames
            if "captcha" in child.get("url", "").lower()
            or "punish" in child.get("url", "").lower()
            or child.get("name") == "baxia-dialog-content"
        ),
        None,
    )
    if not challenge:
        return False

    ctx = page._call(
        "Page.createIsolatedWorld",
        {"frameId": challenge["id"], "worldName": "aliexpress-captcha", "grantUniveralAccess": True},
    ).get("result", {}).get("executionContextId")
    if not ctx:
        return False

    rect_result = page._call(
        "Runtime.evaluate",
        {
            "contextId": ctx,
            "returnByValue": True,
            "expression": """
            (() => {
              const handle = document.querySelector('.btn_slide, [class*="btn_slide"], [class*="nc_iconfont"]');
              const scale = document.querySelector('.nc_scale, [class*="nc_scale"]');
              if (!handle || !scale) return null;
              const hr = handle.getBoundingClientRect();
              const sr = scale.getBoundingClientRect();
              const frame = window.frameElement ? window.frameElement.getBoundingClientRect() : {x: 0, y: 0};
              return {
                startX: frame.x + hr.x + hr.width / 2,
                startY: frame.y + hr.y + hr.height / 2,
                endX: frame.x + sr.x + sr.width - hr.width / 2 - 2
              };
            })()
            """,
        },
    )
    rect = rect_result.get("result", {}).get("result", {}).get("value") or {}
    if not rect:
        return False

    start_x = float(rect["startX"])
    start_y = float(rect["startY"])
    end_x = float(rect["endX"])
    page._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": start_x, "y": start_y, "button": "none"})
    page._call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": start_x, "y": start_y, "button": "left", "buttons": 1, "clickCount": 1},
    )
    for step in range(1, 36):
        progress = step / 35
        eased = progress * progress * (3 - 2 * progress)
        x = start_x + (end_x - start_x) * eased
        y = start_y + math.sin(progress * math.pi * 4) * 1.5
        page._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": 1})
        time.sleep(0.025)
    page._call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": end_x, "y": start_y, "button": "left", "buttons": 0, "clickCount": 1},
    )
    time.sleep(5)
    return True


def _scrape_orders(page: _CDPPage) -> list[dict]:
    page.navigate(ORDERS_URL)
    time.sleep(8)
    _login_if_needed(page)
    page.navigate(ORDERS_URL)
    time.sleep(8)
    js = r"""
    (() => {
      const text = document.body.innerText || '';
      const anchors = [...document.querySelectorAll('a')].map(a => ({
        text: (a.innerText || '').trim(),
        href: a.href || '',
        image: (() => {
          const scope = a.closest('[class*="order"], [class*="card"], [class*="item"], li, div') || a;
          const img = scope.querySelector('img[src*="alicdn"], img[src*="aliexpress-media"], img[src]');
          return img ? (img.currentSrc || img.src || '') : '';
        })()
      }));
      const results = [];
      const chunks = text.split('Awaiting delivery').slice(1);

      for (const chunk of chunks) {
        const block = chunk.split(/Completed|Canceled|Cancelled|Closed|Refund complete|View orders/)[0].trim();
        if (!block) continue;
        const orderId = (block.match(/Order ID:\s*(\d+)/) || [])[1] || '';
        const orderDate = (block.match(/Order date:\s*(.+)/) || [])[1]?.split('\n')[0] || '';
        const total = (block.match(/Total:\s*([^\n]+)/) || [])[1] || '';
        const lines = block.split('\n').map(line => line.trim()).filter(Boolean);
        const storeIdx = lines.findIndex(line => /Store$/.test(line));
        const store = storeIdx >= 0 ? lines[storeIdx] : '';
        let productTitle = '';
        let variant = '';
        if (storeIdx >= 0 && lines[storeIdx + 1] && !/^Total:/.test(lines[storeIdx + 1])) {
          productTitle = lines[storeIdx + 1];
          if (lines[storeIdx + 2] && !lines[storeIdx + 2].startsWith('$') && !/^Total:/.test(lines[storeIdx + 2])) {
            variant = lines[storeIdx + 2];
          }
        }
        const productAnchor = anchors.find(a => a.href.includes('/item/') && a.text === productTitle)
          || anchors.find(a => a.href.includes('/item/') && productTitle && (a.text.includes(productTitle) || productTitle.includes(a.text)))
          || anchors.find(a => a.href.includes('/item/'))
          || { href: '', image: '' };
        const trackAnchor = anchors.find(a => a.text === 'Track order' && a.href.includes(orderId)) || { href: '' };
        results.push({
          status: 'Awaiting delivery',
          order_id: orderId,
          order_date: orderDate,
          store,
          product_title: productTitle,
          product_href: productAnchor.href,
          track_href: trackAnchor.href,
          total,
          variant,
          image: productAnchor.image || ''
        });
      }
      return results;
    })()
    """
    return page.eval(js) or []


def _resolve_missing_images(page: _CDPPage, items: list[dict]):
    for item in items:
        if item.get("image") and "272x80.png" not in item["image"]:
            continue
        if not item.get("product_href"):
            continue
        page.navigate(item["product_href"])
        time.sleep(5)
        image = page.eval(
            """
            (() => {
              const meta = document.querySelector('meta[property="og:image"]');
              if (meta && meta.content) return meta.content;
              const img = document.querySelector('img[src*="aliexpress-media"], img[src*="alicdn"]');
              return img ? img.src : '';
            })()
            """
        ) or ""
        if image:
            item["image"] = _image_url(image)


def _resolve_tracking_details(page: _CDPPage, items: list[dict]):
    for item in items:
        if not item.get("track_href"):
            continue
        page.navigate(item["track_href"])
        time.sleep(6)
        details = page.eval(
            r"""
            (() => {
              const body = (document.body && document.body.innerText) || '';
              const lines = body.split('\n').map(line => line.trim()).filter(Boolean);
              const tracking = (body.match(/Tracking number:\s*([A-Z0-9]+)/i) || [])[1] || '';
              const delivery = (body.match(/Delivery:\s*([^\n]+)/i) || [])[1] || '';
              let status = '';
              let latest_event = '';
              let latest_time = '';

              const idx = lines.findIndex(line => /^In transit$/i.test(line) || /^Delivered$/i.test(line) || /^Out for delivery$/i.test(line));
              if (idx >= 0) {
                status = lines[idx];
                if (lines[idx + 1]) latest_event = lines[idx + 1];
                if (lines[idx + 2]) latest_time = lines[idx + 2].replace(/\s+/g, ' ');
              }

              if (!status) {
                const fallback = lines.find(line => /in transit|delivered|out for delivery/i.test(line));
                if (fallback) status = fallback;
              }

              return { tracking, delivery, status, latest_event, latest_time };
            })()
            """
        ) or {}
        item["tracking_number"] = details.get("tracking", "")
        item["delivery_window"] = details.get("delivery", "")
        item["tracking_status"] = details.get("status", "")
        item["latest_event"] = details.get("latest_event", "")
        item["latest_time"] = details.get("latest_time", "")


def _render_orders(items: list[dict]) -> str:
    if not items:
        return "<div class='ae-orders'><p><em>No packages currently in transit.</em></p></div>"

    cards = []
    for item in items:
        title = _esc(item.get("product_title") or "Order in transit")
        store = _esc(item.get("store") or "")
        order_id = _esc(item.get("order_id") or "")
        order_date = _esc(item.get("order_date") or "")
        total = _esc(item.get("total") or "")
        variant = _esc(item.get("variant") or "")
        image = _image_url(item.get("image") or "")
        tracking_status = _esc(item.get("tracking_status") or "")
        latest_event = _esc(item.get("latest_event") or "")
        latest_time = _esc(item.get("latest_time") or "")
        delivery_window = _esc(item.get("delivery_window") or "")
        product_href = item.get("product_href") or item.get("track_href") or ""
        track_href = item.get("track_href") or product_href

        meta = " · ".join(part for part in [store, order_date, total] if part)
        variant_html = f"<div class='ae-order-variant'>{variant}</div>" if variant else ""
        tracking_html = ""
        if tracking_status or latest_event or latest_time or delivery_window:
            parts = []
            if tracking_status:
                parts.append(f"<div class='ae-order-status'><strong>{tracking_status}</strong></div>")
            if latest_event:
                parts.append(f"<div class='ae-order-status'>{latest_event}</div>")
            if latest_time:
                parts.append(f"<div class='ae-order-status ae-order-status--time'>{latest_time}</div>")
            if delivery_window:
                parts.append(f"<div class='ae-order-status ae-order-status--time'>Delivery: {delivery_window}</div>")
            tracking_html = "".join(parts)
        thumb = (
            f"<a href='{_esc(product_href)}'><img class='ae-order-thumb' src='{_esc(image)}' alt='{title}'></a>"
            if image and product_href else
            "<div class='ae-order-thumb ae-order-thumb--empty'>📦</div>"
        )

        cards.append(
            "<table class='ae-order-table' role='presentation' cellspacing='0' cellpadding='0'>"
            "<tr><td>"
            f"{thumb}"
            "</td></tr>"
            "<tr><td class='ae-order-body ae-order-body--stack'>"
            f"<div class='ae-order-title'><a href='{_esc(product_href)}'>{title}</a></div>"
            f"{variant_html}"
            f"{tracking_html}"
            f"<div class='ae-order-meta'>{_esc(meta)}</div>"
            f"<div class='ae-order-links'><a href='{_esc(track_href)}'>Track order</a>"
            f"{' · <span>Order ' + order_id + '</span>' if order_id else ''}</div>"
            "</td></tr>"
            "</table>"
        )
    return "<div class='ae-orders'>" + "".join(cards) + "</div>"


def fetch_aliexpress_orders() -> tuple[str, str]:
    label = "📦 AliExpress — Packages En Route"
    try:
        _ensure_browser()
        page = _CDPPage(_get_page_socket())
        try:
            orders = _scrape_orders(page)
            _resolve_missing_images(page, orders)
            _resolve_tracking_details(page, orders)
            orders = [
                order for order in orders
                if (order.get("tracking_status") or "").strip().lower() not in {"delivered"}
            ]
        finally:
            page.close()
        return label, _render_orders(orders)
    except Exception as e:
        return label, f"<ul><li><em>Error: {_esc(e)}</em></li></ul>"
