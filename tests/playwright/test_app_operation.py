#!/usr/bin/env python3
"""
Playwright によるアプリ起動確認・ログイン・ログアウト自動化スクリプト

前提: `.env` に `APP_USERNAME` と `APP_PASSWORD` を設定しておくこと。
使い方: `python tests/playwright/test_app_operation.py`
"""
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import os
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv()

URL = os.getenv("APP_URL", "http://localhost:8080").rstrip("/")

def http_check():
    try:
        r = requests.get(URL + "/", timeout=3, allow_redirects=True)
        return r.status_code >= 200 and r.status_code < 400
    except Exception:
        return False

def try_fill(page, selectors, value):
    for sel in selectors:
        try:
            locator = page.locator(sel)
            if locator.count() > 0:
                locator.fill(value)
                return True
        except Exception:
            continue
    return False

def main():
    print(f"チェック: {URL}")
    if not http_check():
        print("NOT_RUNNING: http://localhost:8080 が起動していません。SKILL.md の手順で起動してください。")
        sys.exit(2)

    username = os.getenv("APP_USERNAME")
    password = os.getenv("APP_PASSWORD")
    if not username or not password:
        print("APP_USERNAME / APP_PASSWORD が .env に設定されていません。設定してください。")
        sys.exit(3)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded")
        except Exception as e:
            print("ページ遷移に失敗しました:", e)
            browser.close()
            sys.exit(4)

        # ログイン画面判定
        is_login_page = page.url.rstrip('/').endswith('/login') or page.locator("text=Login").count() > 0 or page.locator("text=ログイン").count() > 0

        if is_login_page:
            print("ログイン画面を検出しました。ログインを試行します。")
            user_selectors = ['#username', "input[placeholder*='user']", "input[name='username']", "input[type='text']"]
            pass_selectors = ['#password', "input[placeholder*='pass']", "input[name='password']", "input[type='password']"]

            filled_user = try_fill(page, user_selectors, username)
            filled_pass = try_fill(page, pass_selectors, password)

            if not (filled_user and filled_pass):
                print("入力欄が見つかりませんでした。スクリーンショットを取得します。")
                path = "./tests/playwright/login_not_found.png"
                page.screenshot(path=path, full_page=True)
                print("スクリーンショット:", path)
                browser.close()
                sys.exit(5)

            # ボタンをクリック
            try:
                if page.locator("role=button[name=Login]").count() > 0:
                    page.locator("role=button[name=Login]").click()
                elif page.locator("text=Login").count() > 0:
                    page.locator("text=Login").click()
                else:
                    page.locator("button").first.click()
            except PlaywrightTimeout:
                print("Login ボタンのクリックに失敗しました。")

            # 成功判定: /home への遷移 または Logout 表示
            try:
                page.wait_for_timeout(1500)
                logged_in = ("/home" in page.url) or page.locator("text=Logout").count() > 0 or page.locator("text=ログアウト").count() > 0
                if not logged_in:
                    path = "./tests/playwright/login_failed.png"
                    page.screenshot(path=path, full_page=True)
                    print("ログイン失敗。スクリーンショット:", path)
                    browser.close()
                    sys.exit(6)
                print("ログイン成功を検出しました。")
            except Exception:
                print("ログイン判定中にエラーが発生しました。")
                browser.close()
                sys.exit(7)
        else:
            # ログイン不要・既にログイン済みの可能性
            if page.locator("text=Logout").count() > 0 or page.locator("text=ログアウト").count() > 0:
                print("既にログイン済みと見なしました。")
            else:
                print("ログイン画面ではありませんが、ログイン状態を確認できませんでした。URL=", page.url)

        # ログアウト処理
        if page.locator("text=Logout").count() > 0:
            page.locator("text=Logout").click()
        elif page.locator("text=ログアウト").count() > 0:
            page.locator("text=ログアウト").click()
        else:
            print("ログアウト導線が見つかりませんでした。スキップします。")
            browser.close()
            print("完了")
            return

        # ログアウト成功判定
        try:
            page.wait_for_timeout(1000)
            if page.url.rstrip('/').endswith('/login') or page.locator("text=Login").count() > 0 or page.locator("text=ログイン").count() > 0:
                print("ログアウト成功を検出しました。")
            else:
                print("ログアウトが完了しているか確認できませんでした。URL=", page.url)
        finally:
            browser.close()

if __name__ == '__main__':
    main()
