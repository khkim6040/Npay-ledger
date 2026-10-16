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


if __name__ == "__main__":
    selftest()
    print("selftest ok")
