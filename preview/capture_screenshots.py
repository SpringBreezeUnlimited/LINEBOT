from playwright.sync_api import sync_playwright
import os

URL = "http://127.0.0.1:8000/preview/admin_preview.html"
OUT_DIR = "preview/screenshots"

os.makedirs(OUT_DIR, exist_ok=True)

viewports = {
    "mobile_portrait": (390, 844),
    "tablet_portrait": (768, 1024),
    "desktop": (1366, 768),
}

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    for name, (w, h) in viewports.items():
        page.set_viewport_size({"width": w, "height": h})
        page.goto(URL)
        page.wait_for_timeout(500)
        path = os.path.join(OUT_DIR, f"{name}.png")
        page.screenshot(path=path, full_page=True)
        print("Saved:", path)
    browser.close()
