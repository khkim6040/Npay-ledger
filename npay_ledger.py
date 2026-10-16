#!/usr/bin/env python3
"""Npay-ledger: 네이버페이 지출 내역을 월별 CSV로 내보낸다.

사용법: python npay_ledger.py [--since YYYY-MM] [--out out] [--profile profile]
검증:   python test_npay_ledger.py
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PAY = "https://pay.naver.com"
ORDERS = "https://orders.pay.naver.com"
CARD = "https://card.pay.naver.com"
KST = timezone(timedelta(hours=9))
FMT = "%Y-%m-%d %H:%M:%S"
MATCH_BEFORE = timedelta(minutes=5)  # 머니카드 승인은 결제내역 시각보다 최대 3분쯤 앞선다
MATCH_AFTER = timedelta(minutes=1)
COLUMNS = ["일시", "구분", "금액", "가맹점", "상품명", "카드", "계좌", "머니", "포인트", "기타",
           "소스", "유형", "상태", "거래ID", "원거래ID", "원거래일시", "업종코드", "상세URL"]
UNKNOWN_REFUND_DATE = "환불일 미상(결제일 대체)"


# ---------- 날짜·행 헬퍼 ----------

def from_ms(ms):
    """epoch ms → KST naive datetime"""
    return datetime.fromtimestamp(ms / 1000, KST).replace(tzinfo=None)


def from_iso(s):
    """'2026-08-21T07:51:02' → datetime"""
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def from_compact(s):
    """'20260228143120' → datetime"""
    return datetime.strptime(s, "%Y%m%d%H%M%S")


def card_mask(s):
    """'1111-****-****-****' → '1111************' (자리별 비교용)"""
    return re.sub(r"[^0-9*]", "", s or "")


def same_card(a, b):
    """가려진 카드번호 두 개가 같은 카드인가. 현장결제는 앞 4자리만, 간편결제는 뒤 4자리만 보여 주므로
    길이가 같고 둘 다 보이는 자리가 4자리 이상 있으며 그 자리가 전부 같으면 같은 카드로 본다."""
    a, b = card_mask(a), card_mask(b)
    seen = [(x, y) for x, y in zip(a, b) if x != "*" and y != "*"]
    return bool(a) and len(a) == len(b) and len(seen) >= 4 and all(x == y for x, y in seen)


def make_row(dt, kind, amount, merchant, product, card=0, bank=0, money=0, point=0, other=None,
             source="", typ="", status="", tid="", origin_id="", origin_dt=None, mcc="", url=""):
    if other is None:
        other = amount - (card + bank + money + point)  # 모르는 수단이 와도 합이 총액과 맞는다
    return {"일시": dt.strftime(FMT), "구분": kind, "금액": amount, "가맹점": merchant, "상품명": product,
            "카드": card, "계좌": bank, "머니": money, "포인트": point, "기타": other,
            "소스": source, "유형": typ, "상태": status, "거래ID": tid,
            "원거래ID": origin_id, "원거래일시": origin_dt.strftime(FMT) if origin_dt else "",
            "업종코드": mcc, "상세URL": url}


# ---------- 변환: 결제내역 → 결제 레코드 ----------

def payment_kind(item):
    url = item.get("orderDetailUrl") or ""
    if "/instantPay/detail/" in url:
        return "instant"
    if "/order/status/" in url:
        return "order"
    return "other"


def shopping_payments(items, details, card_no):
    """결제내역 항목 + 상세 → 결제 레코드. 주문은 orderNo 단위로 묶는다."""
    pays, orders = [], {}
    for it in items:
        kind = payment_kind(it)
        add = it.get("additionalData") or {}
        rec = {"dt": from_ms(it["date"]), "merchant": it.get("merchantName") or "",
               "product": (it.get("product") or {}).get("name") or "",
               "typ": it.get("serviceType") or "", "status": (it.get("status") or {}).get("text") or "",
               "url": it.get("orderDetailUrl") or "",
               "card": 0, "bank": 0, "money": 0, "point": 0, "money_card": False,
               "refund": 0, "refund_card": 0, "refund_bank": 0, "refund_money": 0, "refund_point": 0,
               "refund_dt": None}
        if kind == "instant":
            d = details[add["payId"]]
            amt = d["amount"]
            pm = amt.get("paymentMethod") or {}
            ec = (d.get("paymentMethod") or {}).get("easyCard") or {}
            ra = d.get("refundAmount") or {}
            rpm = ra.get("paymentMethod") or {}
            rec.update(id=add["payId"], gross=amt["totalAmount"],
                       card=pm.get("easyCard") or 0, bank=pm.get("easyBank") or 0,
                       money=pm.get("chargePoint") or 0, point=pm.get("rewardPoint") or 0,
                       money_card=same_card(ec.get("maskedNumber"), card_no),
                       refund=ra.get("totalAmount") or 0,
                       refund_card=rpm.get("easyCard") or 0, refund_bank=rpm.get("easyBank") or 0,
                       refund_money=rpm.get("chargePoint") or 0, refund_point=rpm.get("rewardPoint") or 0)
        elif kind == "order":
            no = add["orderNo"]
            if no in orders:  # 같은 주문의 다른 상품 행
                o = orders[no]
                o["product"] += " / " + rec["product"]
                o["dt"] = min(o["dt"], rec["dt"])
                continue
            d = details[no]
            pay = d["order"]["pay"]
            rf = d["refund"]
            adm = pay.get("cardPayAdmission")
            general = pay.get("generalPayAmount") or 0
            claims = [po["claim"]["refundTreatmentDateTime"] for po in d["order"].get("productOrders") or []
                      if (po.get("claim") or {}).get("refundTreatmentDateTime")]
            rpv = rf.get("refundPayView") or {}
            rec.update(id=no, gross=pay["totalInitPayAmount"],
                       card=general if adm else 0, bank=0 if adm else general,
                       money=pay.get("chargePointPayAmount") or 0, point=pay.get("rewardPointPayAmount") or 0,
                       money_card=bool(adm) and (adm.get("cardCompanyName") == "N페이 머니카드"
                                                 or same_card(adm.get("maskedCardNo"), card_no)),
                       refund=(rf.get("totalRefundAmount") or 0) if rf.get("refundOccurred") else 0,
                       refund_card=rpv.get("cardRefundAmount") or 0, refund_bank=rpv.get("bankRefundAmount") or 0,
                       refund_money=rpv.get("chargedPointRefundAmount") or 0,
                       refund_point=rpv.get("rewardPointRefundAmount") or 0,
                       refund_dt=from_ms(max(claims)) if claims else None)
            orders[no] = rec
        else:  # CONTENTS 등: 목록 정보만 있다. 수단은 전부 "기타", 환불은 다루지 않는다
            rec.update(id=it["_id"], gross=(it.get("product") or {}).get("price") or 0)
        pays.append(rec)
    return pays


# ---------- 변환: 행 생성과 요약 ----------

class ConsistencyError(Exception):
    """받은 데이터끼리 맞지 않는다. 실행을 중단한다."""


def build_rows(pays, card_rows, money_cancels, since=None):
    """결제 레코드와 머니카드 행을 합쳐 CSV 행을 만든다. (rows, stats) 반환."""
    stats = {"matched": 0, "unmatched": 0, "unknown_refund_date": 0, "dropped_refunds": 0}
    originals = [r for r in card_rows["local"] + card_rows["overseas"] if r.get("cancelYn") != "y"]
    cancels = [r for r in card_rows["local"] + card_rows["overseas"] if r.get("cancelYn") == "y"]
    if any(p["money_card"] for p in pays) and not originals and not card_rows["traffic"]:
        # 머니카드 API는 잘못된 cardId에도 C9(없음)를 돌려주므로 여기서 걸러야 지출이 조용히 빠지지 않는다
        raise ConsistencyError("결제내역에 머니카드 결제가 있는데 머니카드 내역이 0건입니다. 카드 조회가 잘못됐을 수 있어 중단합니다.")

    # 1. 결제내역의 머니카드 결제 ↔ 머니카드 원거래 행 짝 맞추기 (같은 금액, 승인시각이 결제시각 −5분~+1분)
    matched = {}  # 머니카드 tid → 결제 레코드
    for p in pays:
        if not p["money_card"]:
            continue
        cands = [r for r in originals if r["tid"] not in matched and r["wonAmount"] == p["card"]
                 and p["dt"] - MATCH_BEFORE <= from_iso(r["approvalDateTime"]) <= p["dt"] + MATCH_AFTER]
        if cands:
            best = min(cands, key=lambda r: abs(from_iso(r["approvalDateTime"]) - p["dt"]))
            matched[best["tid"]] = p
            stats["matched"] += 1
        elif not since or p["dt"].date() >= since:  # since 이전 결제는 머니카드 내역을 안 받았으니 못 찾는 게 정상
            stats["unmatched"] += 1
    # 2. 짝이 된 결제의 환불일은 연결된 취소 행의 승인시각 (여러 번이면 마지막)
    for c in cancels:
        p = matched.get(c.get("originTid"))
        if p:
            dt = from_iso(c["approvalDateTime"])
            p["refund_dt"] = max(p["refund_dt"], dt) if p["refund_dt"] else dt

    rows = []
    # 3. 쇼핑 결제 행과 환불 행
    for p in pays:
        rows.append(make_row(p["dt"], "결제", p["gross"], p["merchant"], p["product"],
                             p["card"], p["bank"], p["money"], p["point"],
                             source="쇼핑", typ=p["typ"], status=p["status"], tid=p["id"], url=p["url"]))
        if p["refund"] > 0:
            dt, status = p["refund_dt"] or money_cancels.get(p["id"]), p["status"]
            if dt is None:
                dt, status = p["dt"], UNKNOWN_REFUND_DATE
                stats["unknown_refund_date"] += 1
            rows.append(make_row(dt, "환불", -p["refund"], p["merchant"], p["product"],
                                 -p["refund_card"], -p["refund_bank"], -p["refund_money"], -p["refund_point"],
                                 source="쇼핑", typ=p["typ"], status=status, tid=p["id"],
                                 origin_id=p["id"], origin_dt=p["dt"], url=p["url"]))
    # 4. 짝이 없는 머니카드 행 (카드로만 쓴 지출)과 그 취소 행
    for r in originals:
        if r["tid"] not in matched:
            rows.append(make_row(from_iso(r["approvalDateTime"]), "결제", r["wonAmount"], r.get("merchantName") or "", "",
                                 money=r.get("moneyAmount") or 0, point=r.get("pointAmount") or 0,
                                 source="머니카드", typ=r["_filter"], tid=r["tid"], mcc=r.get("merchantCategoryCode") or ""))
    for c in cancels:
        if c.get("originTid") not in matched:
            origin_dt = from_iso(c["originalTradeDateTime"]) if c.get("originalTradeDateTime") else None
            rows.append(make_row(from_iso(c["approvalDateTime"]), "환불", -abs(c["wonAmount"]), c.get("merchantName") or "", "",
                                 money=-abs(c.get("moneyAmount") or 0), point=-abs(c.get("pointAmount") or 0),
                                 source="머니카드", typ=c["_filter"], tid=c["tid"], origin_id=c.get("originTid") or "",
                                 origin_dt=origin_dt, mcc=c.get("merchantCategoryCode") or ""))
    # 5. 교통카드 (승차 시각 기준, 0원 행은 버린다)
    for t in card_rows["traffic"]:
        if t.get("amount"):
            product = " ".join(x for x in (t.get("transType"), t.get("stationName"), t.get("rideType")) if x)
            rows.append(make_row(from_iso(t["useDateTime"]), "결제", t["amount"], "교통카드", product,
                                 source="교통", typ="traffic", tid=t.get("tid") or ""))
    # 6. --since
    if since:
        cutoff = since.strftime(FMT)
        kept = []
        for r in rows:
            if r["구분"] == "결제" and r["일시"] < cutoff:
                continue
            if r["구분"] == "환불" and (r["원거래일시"] or r["일시"]) < cutoff:
                stats["dropped_refunds"] += 1
                continue
            kept.append(r)
        rows = kept
    rows.sort(key=lambda r: r["일시"])
    return rows, stats


def summarize(rows):
    """월별 요약. 환불은 원거래 월에서 뺀다."""
    months = defaultdict(lambda: {"건수": 0, "결제액": 0, "환불액": 0})
    for r in rows:
        if r["구분"] == "결제":
            m = months[r["일시"][:7]]
            m["건수"] += 1
            m["결제액"] += r["금액"]
        else:
            months[(r["원거래일시"] or r["일시"])[:7]]["환불액"] += -r["금액"]
    out = [{"월": k, **v, "순지출": v["결제액"] - v["환불액"]} for k, v in sorted(months.items())]
    if sum(r["금액"] for r in rows) != sum(s["순지출"] for s in out):
        raise ConsistencyError("거래내역 금액 합계와 요약 순지출 합계가 다릅니다")
    return out


# ---------- 조회 계층 ----------

class FetchError(Exception):
    """조회 실패. 빈 데이터로 취급하지 않고 실행을 중단한다."""


LOGIN_EXPIRED_CODES = {"AUTH_40", "25"}  # pay·card 도메인은 401 AUTH_40, orders 도메인은 403 code 25


def fetch_json(req, url, body=None):
    """HTTP 200이고 JSON이면 파싱 결과. 그 외는 전부 FetchError."""
    r = req.get(url) if body is None else req.post(url, data=body, headers={"content-type": "application/json"})
    text = r.text()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if r.status == 200 and data is not None:
        return data
    code = data.get("code") if isinstance(data, dict) else None
    if r.status in (401, 403) and code in LOGIN_EXPIRED_CODES:
        raise FetchError(f"로그인이 풀렸습니다 (HTTP {r.status}). 다시 실행해서 로그인해 주세요.")
    raise FetchError(f"{url}\n  HTTP {r.status}: {text[:200]}")


def orders_api(req, url):
    data = fetch_json(req, url)
    if not isinstance(data, dict) or data.get("code") != "00" or "result" not in data:
        raise FetchError(f"{url}\n  {json.dumps(data, ensure_ascii=False)[:200]}")
    return data["result"]


def fetch_payments(req, since):
    """결제내역 목록 전체(최신순). since보다 오래된 건만 남은 페이지에서 멈춘다."""
    items, page = [], 1
    while True:
        data = fetch_json(req, f"{PAY}/web-api/timeline/v3/payments/pages?page={page}")
        if not isinstance(data, dict) or data.get("success") is not True or "items" not in data:
            raise FetchError(f"결제내역 목록 응답이 예상과 다릅니다: {json.dumps(data, ensure_ascii=False)[:200]}")
        if not data["items"]:
            return items
        items += data["items"]
        print(f"결제내역 목록 {page}/{data.get('totalPage', '?')}페이지, 누적 {len(items)}건")
        if since and all(from_ms(i["date"]).date() < since for i in data["items"]):
            return items
        page += 1


def fetch_details(req, items):
    """즉시결제는 payId별, 주문은 orderNo별 상세."""
    details = {}
    for n, it in enumerate(items, 1):
        kind = payment_kind(it)
        add = it.get("additionalData") or {}
        if kind == "instant":
            pay_id = add.get("payId")
            if not pay_id:
                raise FetchError(f"결제번호가 없는 즉시결제 항목: {it.get('_id')}")
            if pay_id not in details:
                ym = from_ms(it["date"]).strftime("%Y-%m")
                details[pay_id] = orders_api(req, f"{ORDERS}/orderApi/payment/detail/info?paymentId={pay_id}&yearMonthFormatDate={ym}")
        elif kind == "order":
            no = add.get("orderNo")
            if not no:
                raise FetchError(f"주문번호가 없는 주문 항목: {it.get('_id')}")
            if no not in details:
                details[no] = {"order": orders_api(req, f"{ORDERS}/orderApi/orderSheet/detail/?orderNo={no}"),
                               "refund": orders_api(req, f"{ORDERS}/orderApi/orderSheet/detail/refund?orderNo={no}")}
        if n % 50 == 0:
            print(f"상세 {n}/{len(items)}")
    return details


def fetch_card(req):
    """머니카드 정보 {cardId, maskedCardNo, issuedDate, ...}. 카드가 없으면 None."""
    r = req.get(f"{CARD}/prepaid/history")
    if r.status != 200:
        raise FetchError(f"머니카드 페이지 HTTP {r.status}: {r.text()[:200]}")
    if "/prepaid/history/" not in r.url:
        return None  # 카드가 없으면 내역 페이지로 가지 않는다
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text(), re.S)
    if not m:
        raise FetchError("머니카드 페이지에서 __NEXT_DATA__를 찾지 못했습니다")
    queries = json.loads(m.group(1))["props"]["pageProps"]["dehydratedState"]["queries"]
    for q in queries:
        if (q.get("queryKey") or [])[:2] == ["card", "info"]:
            data = q["state"]["data"]
            return data["result"] if data.get("status") == "registered" else None
    raise FetchError("머니카드 페이지에 카드 정보가 없습니다")


def fetch_card_rows(req, card_id, start, end, flt):
    """기간·필터 하나의 머니카드 내역. C9(결제 정보 없음)는 빈 목록."""
    rows, page = [], 1
    while True:
        data = fetch_json(req, f"{CARD}/prepaid/api/history/list?startDate={start}&endDate={end}&filter={flt}&cardId={card_id}&pageNumber={page}")
        if isinstance(data, dict) and data.get("code") == "C9":
            return rows
        if not isinstance(data, dict) or data.get("code") != "00" or "result" not in data:
            raise FetchError(f"머니카드 내역 {flt} {start}~{end}: {json.dumps(data, ensure_ascii=False)[:200]}")
        for r in data["result"]["response"]:
            r["_filter"] = flt
            rows.append(r)
        if data["result"].get("isEndPage", True):
            return rows
        page += 1


def next_month(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def months_between(start, end):
    d = start.replace(day=1)
    while d <= end:
        yield d
        d = next_month(d)


def fetch_all_card_rows(req, card, since, today):
    rows = {"local": [], "overseas": [], "traffic": []}
    if card is None:
        print("머니카드가 없어 머니카드 내역은 0건으로 처리합니다")
        return rows
    issued = date.fromisoformat(card["issuedDate"][:10])
    start = max(issued, since) if since else issued
    for month_start in months_between(start, today):
        end = min(next_month(month_start) - timedelta(days=1), today)
        for flt in rows:
            got = fetch_card_rows(req, card["cardId"], month_start.isoformat(), end.isoformat(), flt)
            rows[flt] += got
            print(f"머니카드 {month_start:%Y-%m} {flt} {len(got)}건")
    return rows


def fetch_money_cancels(req, since, today):
    """머니 내역의 취소 행에서 즉시결제 환불 시각을 모은다. {payId: datetime}. 지출 집계에는 쓰지 않는다."""
    start = today - timedelta(days=360)  # 이 API는 1년 밖 범위를 code 10으로 거부한다
    if since and since > start:
        start = since
    out, page = {}, 0
    while True:
        body = {"minPaydttm": start.strftime("%Y%m%d") + "000000", "maxPaydttm": today.strftime("%Y%m%d") + "235959",
                "pagingSize": 100, "page": page, "sort": "desc", "isRpTrFg": False}
        data = fetch_json(req, f"{PAY}/paymoney/api/history/paymoneyList", body=body)
        if not isinstance(data, dict) or data.get("rtncd") != 1000:
            raise FetchError(f"머니 내역: {json.dumps(data, ensure_ascii=False)[:200]}")
        resp = data.get("response") or {}
        for row in resp.get("list") or []:
            m = re.search(r"/instantPay/detail/(\w+)", row.get("LINKURL") or "")
            if row.get("TYPE") == "pmcancel" and m:
                dt = from_compact(row["PAYDTTM"])
                out[m.group(1)] = max(out.get(m.group(1), dt), dt)
        page += 1
        if page >= ((resp.get("pagingInfo") or {}).get("TOTALPAGE") or 0):
            print(f"머니 내역 취소 {len(out)}건")
            return out


def logged_in(req):
    """pay와 orders 두 도메인 모두 로그인 상태인가. 로그인 직후 pay만 먼저 열리고 orders는 잠시 403을 주기도 한다."""
    r = req.get(f"{PAY}/web-api/timeline/v3/payments/pages?page=1")
    if r.status not in (200, 401, 403):
        raise FetchError(f"로그인 확인 실패: HTTP {r.status}: {r.text()[:200]}")
    if r.status != 200:
        return False
    # 없는 주문번호 조회: 로그인 상태면 HTTP 400(code 99), 아니면 HTTP 403(code 25)
    return req.get(f"{ORDERS}/orderApi/orderSheet/detail/refund?orderNo=0").status != 403


def ensure_login(page):
    """두 도메인이 모두 열릴 때까지 3초마다 확인하며, 창에서 로그인하라고 안내한다."""
    req = page.context.request
    if logged_in(req):
        return
    print("브라우저 창에서 네이버에 로그인해 주세요. 로그인되면 자동으로 이어집니다.")
    while not logged_in(req):
        page.wait_for_timeout(3000)


# ---------- 출력과 실행 ----------

def write_csv(rows, summary, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    by_month = defaultdict(list)
    for r in rows:
        by_month[r["일시"][:7]].append(r)
    for month, rs in by_month.items():
        with open(out / f"{month}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rs)
    with open(out / "summary.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["월", "건수", "결제액", "환불액", "순지출"])
        w.writeheader()
        w.writerows(summary)
    return sorted(by_month)


def run(args):
    from playwright.sync_api import sync_playwright
    since = datetime.strptime(args.since, "%Y-%m").date() if args.since else None
    today = date.today()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(args.profile, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{PAY}/pc/history")
        ensure_login(page)
        req = ctx.request
        items = fetch_payments(req, since)
        details = fetch_details(req, items)
        card = fetch_card(req)
        card_rows = fetch_all_card_rows(req, card, since, today)
        money_cancels = fetch_money_cancels(req, since, today)
        ctx.close()
    pays = shopping_payments(items, details, card["maskedCardNo"] if card else None)
    rows, stats = build_rows(pays, card_rows, money_cancels, since)
    summary = summarize(rows)
    months = write_csv(rows, summary, args.out)
    print(f"완료: {len(rows)}행, 월 파일 {len(months)}개 → {args.out}/")
    print(f"머니카드 중복 제거 {stats['matched']}건, 짝 못 찾음 {stats['unmatched']}건, "
          f"환불일 미상 {stats['unknown_refund_date']}건, since 이전 원거래라 뺀 환불 {stats['dropped_refunds']}건")


def main():
    ap = argparse.ArgumentParser(description="네이버페이 지출 내역을 월별 CSV로 내보낸다")
    ap.add_argument("--since", help="YYYY-MM. 이 달부터만 내보낸다 (기본: 전체)")
    ap.add_argument("--out", default="out", help="출력 디렉터리 (기본: out)")
    ap.add_argument("--profile", default="profile", help="브라우저 프로필 디렉터리 (기본: profile)")
    args = ap.parse_args()
    try:
        run(args)
    except (FetchError, ConsistencyError) as e:
        print(f"중단: {e}\nCSV를 쓰지 않았습니다.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
