#!/usr/bin/env python3
"""Npay-ledger: 네이버페이 지출 내역을 월별 CSV로 내보낸다.

사용법: python npay_ledger.py [--since YYYY-MM] [--out out] [--profile profile]
       python npay_ledger.py --selftest
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


def last4(masked):
    digits = re.sub(r"\D", "", masked or "")
    return digits[-4:] if len(digits) >= 4 else None


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


def shopping_payments(items, details, card_last4):
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
                       money_card=bool(card_last4) and last4(ec.get("maskedNumber")) == card_last4,
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
                                                 or (bool(card_last4) and last4(adm.get("maskedCardNo")) == card_last4)),
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
        else:
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


# ---------- selftest 표본 ----------

def _ms(s):
    return int(datetime.strptime(s, FMT).replace(tzinfo=KST).timestamp() * 1000)


SAMPLE_ITEMS = [
    {"_id": "N1", "serviceType": "SIMPLE_PAYMENT", "status": {"text": "부분취소"}, "merchantName": "버스타고",
     "product": {"name": "시외버스", "price": 27400}, "date": _ms("2026-08-10 23:40:23"),
     "orderDetailUrl": "https://orders.pay.naver.com/instantPay/detail/P1", "additionalData": {"payId": "P1"}},
    {"_id": "N2", "serviceType": "SIMPLE_PAYMENT", "status": {"text": "결제취소"}, "merchantName": "번개장터",
     "product": {"name": "맥북", "price": 2350000}, "date": _ms("2026-02-27 21:16:03"),
     "orderDetailUrl": "https://orders.pay.naver.com/instantPay/detail/P2", "additionalData": {"payId": "P2"}},
    {"_id": "N3", "serviceType": "SIMPLE_PAYMENT", "status": {"text": "결제취소"}, "merchantName": "티머니GO",
     "product": {"name": "승차권", "price": 37600}, "date": _ms("2026-01-08 14:51:00"),
     "orderDetailUrl": "https://orders.pay.naver.com/instantPay/detail/P3", "additionalData": {"payId": "P3"}},
    {"_id": "O1a", "serviceType": "ORDER", "status": {"text": "반품환불완료"}, "merchantName": "투디터",
     "product": {"name": "안경", "price": 13810}, "date": _ms("2026-06-22 22:13:00"),
     "orderDetailUrl": "https://orders.pay.naver.com/order/status/O1", "additionalData": {"orderNo": "O1"}},
    {"_id": "O1b", "serviceType": "ORDER", "status": {"text": "반품환불완료"}, "merchantName": "투디터",
     "product": {"name": "렌즈", "price": 10000}, "date": _ms("2026-06-22 22:13:00"),
     "orderDetailUrl": "https://orders.pay.naver.com/order/status/O1", "additionalData": {"orderNo": "O1"}},
    {"_id": "C1", "serviceType": "CONTENTS", "status": {"text": "결제완료"}, "merchantName": "네이버 시리즈",
     "product": {"name": "영화", "price": 5500}, "date": _ms("2026-07-01 20:00:00"),
     "orderDetailUrl": None, "additionalData": {}},
]
SAMPLE_DETAILS = {
    "P1": {"amount": {"totalAmount": 27400, "paymentMethod": {"easyCard": 27400, "rewardPoint": 0, "chargePoint": 0}},
           "paymentMethod": {"easyCard": {"maskedNumber": "****-****-****-2222", "corpName": "비씨"}},
           "refundAmount": {"totalAmount": 23300, "paymentMethod": {"easyCard": 23300}}},
    "P2": {"amount": {"totalAmount": 2350000, "paymentMethod": {"easyBank": 352777, "chargePoint": 1997223}},
           "paymentMethod": {},
           "refundAmount": {"totalAmount": 2350000, "paymentMethod": {"easyBank": 352777, "chargePoint": 1997223}}},
    "P3": {"amount": {"totalAmount": 37600, "paymentMethod": {"easyCard": 37600}},
           "paymentMethod": {"easyCard": {"maskedNumber": "****-****-****-3333", "corpName": "신한"}},
           "refundAmount": {"totalAmount": 37600, "paymentMethod": {"easyCard": 37600}}},
    "O1": {"order": {"pay": {"totalInitPayAmount": 23810, "generalPayAmount": 22781, "chargePointPayAmount": 1029,
                             "rewardPointPayAmount": 0, "cardPayAdmission": {"cardCompanyName": "N페이 머니카드"}},
                     "productOrders": [{"claim": {"refundTreatmentDateTime": _ms("2026-06-30 15:59:00")}},
                                       {"claim": {"refundTreatmentDateTime": _ms("2026-06-30 15:59:00")}}]},
           "refund": {"refundOccurred": True, "totalRefundAmount": 23810,
                      "refundPayView": {"cardRefundAmount": 22781, "chargedPointRefundAmount": 1029}}},
}
SAMPLE_CARD_ROWS = {
    "local": [
        {"tid": "T1", "originTid": None, "cancelYn": "n", "approvalDateTime": "2026-08-10T23:40:22",
         "wonAmount": 27400, "moneyAmount": 27400, "pointAmount": 0, "merchantName": "버스타고_APP",
         "merchantCategoryCode": "4131", "_filter": "local"},
        {"tid": "T2", "originTid": "T1", "cancelYn": "y", "approvalDateTime": "2026-08-19T03:42:57",
         "originalTradeDateTime": "2026-08-10T23:40:22", "wonAmount": -23300, "moneyAmount": -23300,
         "pointAmount": 0, "merchantName": "버스타고_APP", "merchantCategoryCode": "4131", "_filter": "local"},
        {"tid": "T3", "originTid": None, "cancelYn": "n", "approvalDateTime": "2026-06-22T22:12:40",
         "wonAmount": 22781, "moneyAmount": 22781, "pointAmount": 0, "merchantName": "투디터",
         "merchantCategoryCode": "5995", "_filter": "local"},
        {"tid": "T4", "originTid": "T3", "cancelYn": "y", "approvalDateTime": "2026-06-30T15:59:00",
         "originalTradeDateTime": "2026-06-22T22:12:40", "wonAmount": -22781, "moneyAmount": -22781,
         "pointAmount": 0, "merchantName": "투디터", "merchantCategoryCode": "5995", "_filter": "local"},
        {"tid": "T5", "originTid": None, "cancelYn": "n", "approvalDateTime": "2026-08-24T17:03:45",
         "wonAmount": 11600, "moneyAmount": 10000, "pointAmount": 1600, "merchantName": "현장발권",
         "merchantCategoryCode": "4111", "_filter": "local"},
        {"tid": "T6", "originTid": "T5", "cancelYn": "y", "approvalDateTime": "2026-08-24T17:09:58",
         "originalTradeDateTime": "2026-08-24T17:03:45", "wonAmount": -11600, "moneyAmount": -10000,
         "pointAmount": -1600, "merchantName": "현장발권", "merchantCategoryCode": "4111", "_filter": "local"},
        {"tid": "T7", "originTid": None, "cancelYn": "n", "approvalDateTime": "2026-08-12T12:00:00",
         "wonAmount": 54900, "moneyAmount": 0, "pointAmount": 54900, "merchantName": "유니클로",
         "merchantCategoryCode": "5651", "_filter": "local"},
    ],
    "overseas": [],
    "traffic": [
        {"tid": "R1", "useDateTime": "2026-08-12T08:30:00", "payDateTime": "2026-08-13T09:00:00", "amount": 1500,
         "transType": "BUS", "stationName": "수내역", "rideType": "GETTING_ON"},
        {"tid": "R2", "useDateTime": "2026-08-12T08:50:00", "payDateTime": "2026-08-13T09:00:00", "amount": 0,
         "transType": "SUB", "stationName": "정자역", "rideType": "TRANSFER"},
    ],
}
SAMPLE_MONEY_CANCELS = {"P2": datetime(2026, 2, 28, 14, 31, 20)}


def selftest():
    pays = shopping_payments(SAMPLE_ITEMS, SAMPLE_DETAILS, "2222")
    assert [p["id"] for p in pays] == ["P1", "P2", "P3", "O1", "C1"], [p["id"] for p in pays]
    assert [p["money_card"] for p in pays] == [True, False, False, True, False]
    p1, p2, o1, c1 = pays[0], pays[1], pays[3], pays[4]
    assert (p1["gross"], p1["card"], p1["refund"], p1["refund_card"], p1["refund_dt"]) == (27400, 27400, 23300, 23300, None)
    assert (p2["bank"], p2["money"], p2["refund"]) == (352777, 1997223, 2350000)
    assert (o1["gross"], o1["card"], o1["bank"], o1["money"], o1["product"]) == (23810, 22781, 0, 1029, "안경 / 렌즈")
    assert o1["refund"] == 23810 and o1["refund_dt"] == datetime(2026, 6, 30, 15, 59)
    assert o1["dt"] == datetime(2026, 6, 22, 22, 13)
    assert (c1["gross"], c1["card"], c1["refund"]) == (5500, 0, 0)
    assert make_row(datetime(2026, 1, 1), "결제", 1000, "m", "p", card=300)["기타"] == 700
    # build_rows: 중복 제거·환불 행·요약
    rows, stats = build_rows(shopping_payments(SAMPLE_ITEMS, SAMPLE_DETAILS, "2222"), SAMPLE_CARD_ROWS, SAMPLE_MONEY_CANCELS)
    assert stats == {"matched": 2, "unmatched": 0, "unknown_refund_date": 1, "dropped_refunds": 0}, stats
    assert len(rows) == 13, len(rows)  # 결제 5(쇼핑)+2(머니카드)+1(교통), 환불 4(쇼핑)+1(머니카드)
    assert [r["일시"] for r in rows] == sorted(r["일시"] for r in rows)
    by = {(r["거래ID"], r["구분"]): r for r in rows}
    assert by[("P1", "환불")]["일시"] == "2026-08-19 03:42:57" and by[("P1", "환불")]["금액"] == -23300
    assert by[("P1", "환불")]["원거래ID"] == "P1" and by[("P1", "환불")]["원거래일시"] == "2026-08-10 23:40:23"
    assert by[("O1", "결제")]["금액"] == 23810 and by[("O1", "결제")]["카드"] == 22781
    assert by[("O1", "환불")]["일시"] == "2026-06-30 15:59:00" and by[("O1", "환불")]["머니"] == -1029
    assert by[("P2", "환불")]["일시"] == "2026-02-28 14:31:20"
    assert by[("P3", "환불")]["일시"] == "2026-01-08 14:51:00" and by[("P3", "환불")]["상태"] == UNKNOWN_REFUND_DATE
    assert ("T1", "결제") not in by and ("T3", "결제") not in by and ("T2", "환불") not in by and ("T4", "환불") not in by
    assert by[("T6", "환불")]["원거래ID"] == "T5" and by[("T6", "환불")]["원거래일시"] == "2026-08-24 17:03:45"
    assert by[("T7", "결제")]["포인트"] == 54900 and by[("T7", "결제")]["업종코드"] == "5651"
    assert by[("R1", "결제")]["소스"] == "교통" and by[("R1", "결제")]["기타"] == 1500 and ("R2", "결제") not in by
    assert by[("C1", "결제")]["기타"] == 5500 and by[("P2", "결제")]["기타"] == 0
    summary = {s["월"]: s for s in summarize(rows)}
    assert summary["2026-08"] == {"월": "2026-08", "건수": 4, "결제액": 95400, "환불액": 34900, "순지출": 60500}, summary["2026-08"]
    assert summary["2026-06"]["순지출"] == 0 and summary["2026-01"]["순지출"] == 0
    assert sum(r["금액"] for r in rows) == sum(s["순지출"] for s in summary.values())
    # --since: 그 달 이전 결제와, 원거래가 그 이전인 환불은 뺀다
    rows2, stats2 = build_rows(shopping_payments(SAMPLE_ITEMS, SAMPLE_DETAILS, "2222"), SAMPLE_CARD_ROWS, SAMPLE_MONEY_CANCELS,
                               since=date(2026, 7, 1))
    assert stats2["dropped_refunds"] == 3 and all(r["일시"] >= "2026-07-01" for r in rows2), stats2
    assert sum(r["금액"] for r in rows2) == sum(s["순지출"] for s in summarize(rows2))
    # 머니카드 결제가 있는데 머니카드 행이 0건이면 중단
    try:
        build_rows(shopping_payments(SAMPLE_ITEMS, SAMPLE_DETAILS, "2222"), {"local": [], "overseas": [], "traffic": []}, {})
        assert False, "ConsistencyError expected"
    except ConsistencyError:
        pass


if __name__ == "__main__":
    selftest()
    print("selftest ok")
