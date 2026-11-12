"""Playwright browser sanity check (workflow step ke liye)."""

import sys


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = b.new_page()
        page.goto("about:blank")
        print("[+] browser OK:", b.version)
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
