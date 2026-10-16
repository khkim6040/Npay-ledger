#!/usr/bin/env python3
"""npay_ledger 변환·조회 로직 검사. 네트워크 없이 표본 데이터로 돈다.

사용법: python test_npay_ledger.py
"""
import csv
import json
import tempfile
from datetime import date, datetime
from pathlib import Path

from npay_ledger import (COLUMNS, FMT, KST, UNKNOWN_REFUND_DATE, ConsistencyError, FetchError, build_rows, fetch_card,
                         fetch_card_rows, fetch_details, fetch_json, fetch_money_cancels, logged_in, make_row, months_between,
                         same_card, shopping_payments, summarize, write_csv)

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
           "paymentMethod": {"easyCard": {"maskedNumber": "1111-****-****-****", "corpName": "비씨"}},
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
    pays = shopping_payments(SAMPLE_ITEMS, SAMPLE_DETAILS, "1111********2222")
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
    assert same_card("1111-****-****-****", "1111********2222") and same_card("****-****-****-2222", "1111********2222")
    assert not same_card("****-****-****-3333", "1111********2222") and not same_card(None, "1111********2222")
    # build_rows: 중복 제거·환불 행·요약
    rows, stats = build_rows(shopping_payments(SAMPLE_ITEMS, SAMPLE_DETAILS, "1111********2222"), SAMPLE_CARD_ROWS, SAMPLE_MONEY_CANCELS)
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
    # --since: 그 달 이전 결제와, 원거래가 그 이전인 환불은 뺀다. 그 이전 결제는 머니카드 행이 없어도 짝 못 찾음으로 세지 않는다
    since_cards = {"local": [r for r in SAMPLE_CARD_ROWS["local"] if r["tid"] not in ("T3", "T4")], "overseas": [],
                   "traffic": SAMPLE_CARD_ROWS["traffic"]}
    rows2, stats2 = build_rows(shopping_payments(SAMPLE_ITEMS, SAMPLE_DETAILS, "1111********2222"), since_cards, SAMPLE_MONEY_CANCELS,
                               since=date(2026, 7, 1))
    assert stats2 == {"matched": 1, "unmatched": 0, "unknown_refund_date": 1, "dropped_refunds": 3}, stats2
    assert all(r["일시"] >= "2026-07-01" for r in rows2)
    assert sum(r["금액"] for r in rows2) == sum(s["순지출"] for s in summarize(rows2))
    # 머니카드 결제가 있는데 머니카드 행이 0건이면 중단
    try:
        build_rows(shopping_payments(SAMPLE_ITEMS, SAMPLE_DETAILS, "1111********2222"), {"local": [], "overseas": [], "traffic": []}, {})
        assert False, "ConsistencyError expected"
    except ConsistencyError:
        pass
    # 조회 계층: 성공 / 데이터 없음 / 실패 구분
    class FakeResp:
        def __init__(self, status, body, url=""):
            self.status, self._body, self.url = status, body, url

        def text(self):
            return self._body

    class FakeReq:
        def __init__(self, responses):
            self.responses = list(responses)

        def get(self, url):
            return self.responses.pop(0)

        def post(self, url, data=None, headers=None):
            return self.responses.pop(0)

    assert fetch_json(FakeReq([FakeResp(200, '{"a": 1}')]), "u") == {"a": 1}
    for resp, needle in [(FakeResp(401, '{"code":"AUTH_40"}'), "로그인"), (FakeResp(403, '{"code":"25"}'), "로그인"),
                         (FakeResp(500, "oops"), "HTTP 500"), (FakeResp(200, "<html>"), "HTTP 200")]:
        try:
            fetch_json(FakeReq([resp]), "u")
            assert False, "FetchError expected"
        except FetchError as e:
            assert needle in str(e), (needle, str(e))
    assert logged_in(FakeReq([FakeResp(200, "{}"), FakeResp(400, '{"code":"99"}')]))
    assert not logged_in(FakeReq([FakeResp(401, '{"code":"AUTH_40"}')]))
    assert not logged_in(FakeReq([FakeResp(200, "{}"), FakeResp(403, '{"code":"25"}')]))
    try:
        logged_in(FakeReq([FakeResp(500, "oops")]))
        assert False, "FetchError expected"
    except FetchError:
        pass
    assert fetch_card_rows(FakeReq([FakeResp(200, '{"code":"C9","message":"결제 정보가 없습니다."}')]),
                           "C", "2026-01-01", "2026-01-31", "local") == []
    two_pages = [FakeResp(200, json.dumps({"code": "00", "result": {"isEndPage": False, "response": [{"tid": "A"}]}})),
                 FakeResp(200, json.dumps({"code": "00", "result": {"isEndPage": True, "response": [{"tid": "B"}]}}))]
    got = fetch_card_rows(FakeReq(two_pages), "C", "2026-01-01", "2026-01-31", "local")
    assert [(r["tid"], r["_filter"]) for r in got] == [("A", "local"), ("B", "local")]
    try:
        fetch_card_rows(FakeReq([FakeResp(400, '[{"code":"invalid_value"}]')]), "C", "2026-01-01", "2026-01-31", "local")
        assert False, "FetchError expected"
    except FetchError:
        pass
    # 주문 상세: 클레임 없는 주문은 환불 조회를 건너뛴다
    order_item = {"_id": "O9", "date": _ms("2026-09-01 10:00:00"), "additionalData": {"orderNo": "9"},
                  "orderDetailUrl": "https://orders.pay.naver.com/order/status/9"}
    ok = lambda result: FakeResp(200, json.dumps({"code": "00", "result": result}))
    no_claim = FakeReq([ok({"productOrders": [{"claim": None}]})])
    assert fetch_details(no_claim, [order_item])["9"]["refund"] == {} and no_claim.responses == []
    with_claim = FakeReq([ok({"productOrders": [{"claim": {"claimType": "RETURN"}}]}), ok({"refundOccurred": True})])
    assert fetch_details(with_claim, [order_item])["9"]["refund"] == {"refundOccurred": True}
    assert fetch_card(FakeReq([FakeResp(200, "<html>", url="https://card.pay.naver.com/prepaid/intro")])) is None
    page_html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
        {"props": {"pageProps": {"dehydratedState": {"queries": [
            {"queryKey": ["card", "info", "nf-card-02"],
             "state": {"data": {"status": "registered", "result": {"cardId": "C", "maskedCardNo": "1111********2222", "issuedDate": "2026-01-24"}}}}]}}}}
    ) + "</script>"
    assert fetch_card(FakeReq([FakeResp(200, page_html, url="https://card.pay.naver.com/prepaid/history/nf-card-02")]))["cardId"] == "C"
    money_pages = [FakeResp(200, json.dumps({"rtncd": 1000, "response": {"pagingInfo": {"TOTALPAGE": 1}, "list": [
        {"TYPE": "pmcancel", "PAYDTTM": "20260228143120", "LINKURL": "https://orders.pay.naver.com/instantPay/detail/P2?x=1"},
        {"TYPE": "pmuse", "PAYDTTM": "20260227211603", "LINKURL": "https://orders.pay.naver.com/instantPay/detail/P2"},
        {"TYPE": "nfccancel", "PAYDTTM": "20260221163250", "LINKURL": "https://new-m.pay.naver.com/prepaid-card/history/detail/local/T9"}]}}))]
    assert fetch_money_cancels(FakeReq(money_pages), None, date(2026, 10, 16)) == {"P2": datetime(2026, 2, 28, 14, 31, 20)}
    try:
        fetch_money_cancels(FakeReq([FakeResp(200, '{"code":"10","message":"실패"}')]), None, date(2026, 10, 16))
        assert False, "FetchError expected"
    except FetchError:
        pass
    assert list(months_between(date(2026, 1, 24), date(2026, 3, 2))) == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]
    # CSV 출력: 월별 파일과 summary.csv, utf-8-sig
    with tempfile.TemporaryDirectory() as tmp:
        months = write_csv(rows, summarize(rows), tmp)
        assert months == ["2026-01", "2026-02", "2026-06", "2026-07", "2026-08"], months
        with open(Path(tmp) / "2026-08.csv", encoding="utf-8-sig", newline="") as f:
            read = list(csv.DictReader(f))
        assert list(read[0].keys()) == COLUMNS and len(read) == 6, (read[0].keys(), len(read))
        with open(Path(tmp) / "summary.csv", "rb") as f:
            assert f.read(3) == b"\xef\xbb\xbf"


if __name__ == "__main__":
    selftest()
    print("selftest ok")
