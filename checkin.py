#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trae & WorkBuddy 每日自动签到脚本（GitHub Actions / 本地通用版）

原理：
  1. Trae 平台：
     使用 X-Cloudide-Session 会话 Cookie 调用 GetUserToken 换取 JWT，再调用 claim 签到。
  2. WorkBuddy 平台：
     使用 accessToken 与 refreshToken，自动通过 /v2/plugin/auth/token/refresh 刷新令牌，
     再调用 /v2/billing/meter/daily-checkin 完成签到并累计积分。

依赖：仅标准库，无第三方依赖。

环境变量说明：
  Trae 账号：
    TRAE_SESSION          账号 1 的 X-Cloudide-Session Cookie
    TRAE_DEVICE_ID        账号 1 的 16 位数字设备号（选填，缺省自动生成）
    TRAE_SESSION_N        第 N(N>=2) 个 Trae 账号的会话 Cookie
    TRAE_DEVICE_ID_N      第 N 个 Trae 账号的设备号

  WorkBuddy 账号：
    WORKBUDDY_TOKEN       账号 1 的 Access Token
    WORKBUDDY_REFRESH_TOKEN 账号 1 的 Refresh Token（用于长期自动续期）
    WORKBUDDY_UID         账号 1 的用户 UID（选填）
    WORKBUDDY_TOKEN_N     第 N 个 WorkBuddy 账号的 Access Token
    WORKBUDDY_REFRESH_TOKEN_N 第 N 个 WorkBuddy 账号的 Refresh Token
    WORKBUDDY_UID_N       第 N 个 WorkBuddy 账号的 UID

  通用：
    FEISHU_WEBHOOK        飞书机器人 Webhook 地址（选填，结果推送）
"""

import datetime
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

TRAE_BASE = "https://api.trae.cn"
CODEBUDDY_BASE = "https://www.codebuddy.cn"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def _http_post(url, headers, body=""):
    """POST 请求。返回 (status_code, body_str)"""
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, str(e)


# ==================== Trae 签到逻辑 ====================

def trae_get_token(session: str) -> str:
    headers = {
        "Cookie": "X-Cloudide-Session=" + session,
        "Referer": "https://www.trae.cn/",
        "Origin": "https://www.trae.cn",
        "User-Agent": "TraeCheckin/1.0",
        "Accept": "application/json, text/plain, */*",
    }
    status, text = _http_post(TRAE_BASE + "/cloudide/api/v3/common/GetUserToken", headers)
    if status == 401:
        raise RuntimeError("Trae 会话 Cookie 已失效 (HTTP 401)")
    data = json.loads(text)
    token = (data.get("Result") or {}).get("Token")
    if status != 200 or not token:
        raise RuntimeError(f"Trae GetUserToken 失败: HTTP {status} {text[:200]}")
    return token


def trae_checkin(token: str, device_id: str) -> dict:
    headers = {
        "Authorization": "Cloud-IDE-JWT " + token,
        "X-User-Region": "cn",
        "x-device-id": device_id,
        "Content-Type": "application/json",
        "User-Agent": "TraeCheckin/1.0",
    }
    status, text = _http_post(TRAE_BASE + "/trae/api/v2/ug/checkin_credits/claim", headers, "{}")
    try:
        return {"http": status, "body": json.loads(text)}
    except Exception:
        return {"http": status, "body": {"raw": text}}


def iter_trae_accounts():
    s = os.environ.get("TRAE_SESSION", "").strip()
    if s:
        yield 1, s, os.environ.get("TRAE_DEVICE_ID", "").strip()
    n = 2
    while True:
        s = os.environ.get(f"TRAE_SESSION_{n}", "").strip()
        if not s:
            break
        yield n, s, os.environ.get(f"TRAE_DEVICE_ID_{n}", "").strip()
        n += 1


# ==================== WorkBuddy 签到逻辑 ====================

def wb_refresh_token(access_token: str, refresh_token: str) -> str:
    """使用 refresh_token 换取全新 access_token"""
    if not refresh_token:
        return access_token
    headers = {
        "Authorization": "Bearer " + access_token,
        "X-Refresh-Token": refresh_token,
        "X-Auth-Refresh-Source": "ide-main",
        "User-Agent": BROWSER_UA,
        "Content-Type": "application/json",
    }
    status, text = _http_post(CODEBUDDY_BASE + "/v2/plugin/auth/token/refresh", headers, "{}")
    try:
        data = json.loads(text)
        if data.get("code") in (0, 200) and data.get("data", {}).get("accessToken"):
            return data["data"]["accessToken"]
    except Exception:
        pass
    return access_token


def wb_get_real_credits(token: str, uid: str = "") -> float:
    """查询 WorkBuddy 真实当前可用额度（CycleCapacityRemain）"""
    headers = {
        "Authorization": "Bearer " + token,
        "User-Agent": BROWSER_UA,
        "Content-Type": "application/json",
    }
    if uid:
        headers["X-User-Id"] = uid
    body = json.dumps({
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0],
        "OnlyValidPeriod": True
    })
    status, text = _http_post(CODEBUDDY_BASE + "/v2/billing/meter/get-user-resource", headers, body)
    try:
        data = json.loads(text)
        if data.get("code") == 0:
            accounts = data.get("data", {}).get("Response", {}).get("Data", {}).get("Accounts", [])
            total = 0.0
            for a in accounts:
                cr = a.get("CycleCapacityRemainPrecise") or a.get("CycleCapacityRemain") or a.get("CapacityRemainPrecise") or a.get("CapacityRemain") or 0
                total += float(cr)
            return total
    except Exception:
        pass
    return -1.0


def wb_check_and_claim(token: str, uid: str = "") -> dict:
    """查询状态并执行签到"""
    headers = {
        "Authorization": "Bearer " + token,
        "User-Agent": BROWSER_UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if uid:
        headers["X-User-Id"] = uid

    # 1. 检查状态
    st_code, st_text = _http_post(CODEBUDDY_BASE + "/v2/billing/meter/checkin-activity-status", headers, "{}")
    today_checked = False
    total_credits = 0
    streak_days = 0
    try:
        st_data = json.loads(st_text)
        if st_data.get("code") == 0 and "data" in st_data:
            today_checked = st_data["data"].get("today_checked_in", False)
            total_credits = st_data["data"].get("total_credits", 0)
            streak_days = st_data["data"].get("streak_days", 0)
    except Exception:
        pass

    real_credits = wb_get_real_credits(token, uid)
    credit_display = f"{real_credits:.2f}" if real_credits >= 0 else f"{total_credits}"

    if today_checked:
        return {
            "ok": True,
            "already": True,
            "credit": 0,
            "total": real_credits if real_credits >= 0 else total_credits,
            "streak": streak_days,
            "msg": f"今日已签到 (连签 {streak_days} 天, 剩余可用额度 {credit_display} 积分)"
        }

    # 2. 执行签到
    cl_code, cl_text = _http_post(CODEBUDDY_BASE + "/v2/billing/meter/daily-checkin", headers, "{}")
    try:
        cl_data = json.loads(cl_text)
        if cl_data.get("code") == 0:
            data = cl_data.get("data", {})
            credit = data.get("credit") or data.get("today_credit") or 0
            streak = data.get("streak_days") or (streak_days + 1)
            # 重新查一下最新额度
            real_after = wb_get_real_credits(token, uid)
            after_display = f"{real_after:.2f}" if real_after >= 0 else f"{total_credits + credit}"
            return {
                "ok": True,
                "already": False,
                "credit": credit,
                "total": real_after if real_after >= 0 else (total_credits + credit),
                "streak": streak,
                "msg": f"签到成功！获得 {credit} 积分 (已连签 {streak} 天, 剩余可用额度 {after_display} 积分)"
            }
        else:
            return {
                "ok": False,
                "msg": cl_data.get("msg") or f"code={cl_data.get('code')}"
            }
    except Exception as e:
        return {"ok": False, "msg": f"解析签到响应失败: {e}"}


def iter_workbuddy_accounts():
    t = os.environ.get("WORKBUDDY_TOKEN", "").strip()
    rt = os.environ.get("WORKBUDDY_REFRESH_TOKEN", "").strip()
    uid = os.environ.get("WORKBUDDY_UID", "").strip()
    if t or rt:
        yield 1, t, rt, uid
    n = 2
    while True:
        t = os.environ.get(f"WORKBUDDY_TOKEN_{n}", "").strip()
        rt = os.environ.get(f"WORKBUDDY_REFRESH_TOKEN_{n}", "").strip()
        uid = os.environ.get(f"WORKBUDDY_UID_{n}", "").strip()
        if not t and not rt:
            break
        yield n, t, rt, uid
        n += 1


# ==================== 通知与辅助 ====================

def notify_feishu(webhook: str, text: str):
    if not webhook:
        return
    try:
        payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
        req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except Exception as e:
        print(f"推送飞书失败: {e}")


def beijing_now_str():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def main():
    trae_accounts = list(iter_trae_accounts())
    wb_accounts = list(iter_workbuddy_accounts())

    if not trae_accounts and not wb_accounts:
        print("错误：未检测到任何可用的签到凭据（TRAE_SESSION 或 WORKBUDDY_TOKEN）")
        sys.exit(1)

    # 可选云端随机延迟（防整点风控）：
    # 手动触发 (workflow_dispatch) 或本地直接运行时不等待，秒级出结果；仅定时调度 (schedule) 时启用随机延迟
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    is_schedule = (event_name == "schedule")
    random_delay_max = int(os.environ.get("RANDOM_DELAY_MAX", "0") or 0)
    if is_schedule and random_delay_max > 0:
        delay_sec = random.randint(5, random_delay_max)
        print(f"[云端防风控] 定时调度触发，随机等待 {delay_sec} 秒后再开始签到…")
        time.sleep(delay_sec)

    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    ok_records, fail_records = [], []
    all_ok = True

    # 1. 运行 Trae 签到
    for idx, session, device_id in trae_accounts:
        name = f"[Trae] 账号 {idx}"
        device_id = device_id or str(random.randint(10**15, 10**16 - 1))
        print(f"\n===== 开始执行 {name} (device_id={device_id}) =====")
        try:
            token = trae_get_token(session)
            res = trae_checkin(token, device_id)
            body = res["body"]
            code = body.get("code", -1)
            checked = body.get("checked_in", False)
            credits = body.get("credits", 0)
            if (res["http"] == 200) and (code == 0 or checked):
                msg = f"签到成功，获得 {credits} 积分" if not checked else "今日已签到"
                print(f"[{name}] {msg}")
                ok_records.append(f"{name}: {msg}")
            else:
                reason = body.get("message") or f"HTTP {res['http']}"
                print(f"[{name}] 签到失败：{reason}")
                fail_records.append(f"{name}: {reason}")
                all_ok = False
        except Exception as e:
            print(f"[{name}] 异常: {e}")
            fail_records.append(f"{name}: {e}")
            all_ok = False

        # 随机抖动防风控
        time.sleep(random.uniform(1.5, 3.5))

    # 2. 运行 WorkBuddy 签到
    for idx, token, refresh_token, uid in wb_accounts:
        name = f"[WorkBuddy] 账号 {idx}"
        print(f"\n===== 开始执行 {name} =====")
        try:
            # 先尝试刷新 Token
            if refresh_token:
                print(f"[{name}] 尝试静默刷新 Token…")
                token = wb_refresh_token(token, refresh_token)

            res = wb_check_and_claim(token, uid)
            if res["ok"]:
                print(f"[{name}] {res['msg']}")
                ok_records.append(f"{name}: {res['msg']}")
            else:
                print(f"[{name}] 签到失败：{res.get('msg')}")
                fail_records.append(f"{name}: {res.get('msg')}")
                all_ok = False
        except Exception as e:
            print(f"[{name}] 异常: {e}")
            fail_records.append(f"{name}: {e}")
            all_ok = False

        time.sleep(random.uniform(1.5, 3.5))

    # 3. 汇总飞书推送
    summary = ["【自动签到助手】每日签到结果汇总", f"执行时间: {beijing_now_str()}"]
    if ok_records:
        summary.append("\n✅ 成功项:")
        for r in ok_records:
            summary.append(f"  • {r}")
    if fail_records:
        summary.append("\n⚠️ 失败项:")
        for r in fail_records:
            summary.append(f"  • {r}")

    print("\n" + "=" * 40)
    print("\n".join(summary))

    if webhook and (ok_records or fail_records):
        notify_feishu(webhook, "\n".join(summary))

    if not all_ok:
        sys.exit(1)
    print("\n全部任务执行完毕！")


if __name__ == "__main__":
    main()
