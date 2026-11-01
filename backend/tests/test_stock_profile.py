"""个股资料服务契约测试 (P1-13: 第一批 解禁/质押/回购/股东户数 + 第二批 十大股东/基金持仓/新股申购/业绩快报
+ 第三批 资金流向(新浪)/逐笔成交(腾讯))。

不依赖真实网络: 用假 client 返回 2026-11-01 真机录制的样例,
验证字段归一化(日期裁剪/单位口径/进度码映射/小数转百分数/方向码转中文)、
缓存 TTL(分级)/强制刷新、失败降级(旧缓存标 stale / 无缓存 source_unavailable)、
API 参数校验与 AI 工具封装。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugins.eastmoney.client import EastmoneyClient, EastmoneyError
from app.services import stock_profile as sp

# ---- 真机录制样例 (2026-11-01 datacenter-web 冒烟, 字段有删节) ----

# 限售解禁 RPT_LIFT_STAGE (000521 长虹美菱 2010-01-04)
_UNLOCK_ROW = {
    "SECUCODE": "000521.SZ", "SECURITY_CODE": "000521",
    "FREE_DATE": "2010-01-04 00:00:00", "BATCH_HOLDER_NUM": 3,
    "CURRENT_FREE_SHARES": 7407.4, "LIFT_MARKET_CAP": 97555.458,
    "FREE_SHARES_TYPE": "股权分置限售股份",
    "FREE_SHARES": 26081.02, "NON_FREE_SHARES": 3973.27,
    "FREE_RATIO": 0.284014946071, "TOTALSHARES_RATIO": 0.246467309659,
    "SECURITY_NAME_ABBR": "长虹美菱",
}

# 股权质押 RPT_CSDC_LIST (000001 平安银行 2026-10-30 周频)
_PLEDGE_ROW = {
    "SECUCODE": "000001.SZ", "SECURITY_CODE": "000001",
    "SECURITY_NAME_ABBR": "平安银行", "TRADE_DATE": "2026-10-30 00:00:00",
    "PLEDGE_RATIO": 0.13, "REPURCHASE_BALANCE": 2438.48,
    "PLEDGE_DEAL_NUM": 9, "REPURCHASE_UNLIMITED_BALANCE": 2438.48,
    "REPURCHASE_LIMITED_BALANCE": 0, "PLEDGE_MARKET_CAP": 28383.9072,
    "INDUSTRY": "银行Ⅱ",
}

# 股票回购 RPTA_WEB_GETHGLIST_NEW (000651 格力电器, 004=实施中)
_REPURCHASE_ROW = {
    "DIM_SCODE": "000651", "SECURITYSHORTNAME": "格力电器",
    "DIM_DATE": "2026-04-29 00:00:00", "REPURPROGRESS": "004",
    "REPURAMOUNT": 2324756128.19, "REPURNUM": 61974592,
    "SHARETYPE": "流通A股", "REPURENDDATE": "2027-06-30 00:00:00",
    "REPURPRICECAP": 54.55, "REPUROBJECTIVE": "基于对公司未来发展的信心……",
}

# 股东户数 RPT_HOLDERNUM_DET (600519 贵州茅台 2026 三季末)
_HOLDER_ROW = {
    "SECURITY_CODE": "600519", "SECUCODE": "600519.SH",
    "SECURITY_NAME_ABBR": "贵州茅台",
    "HOLDER_NUM": 278364, "PRE_HOLDER_NUM": 296404,
    "HOLDER_NUM_CHANGE": -18040, "HOLDER_NUM_RATIO": -6.086287634445,
    "INTERVAL_CHRATE": 6.16875722, "END_DATE": "2026-09-30 00:00:00",
    "AVG_MARKET_CAP": 5652231.26787451, "AVG_HOLD_NUM": 4490.81634478596,
    "HOLD_NOTICE_DATE": "2026-10-31 00:00:00", "CLOSE_PRICE": 1258.62,
}

# ---- P1-13 第二批 真机录制样例 (2026-11-01 datacenter-web 冒烟) ----

# 十大股东 RPT_F10_EH_HOLDERS (000651 格力电器 2026 三季末 第1名)
_TOP_HOLDER_ROW = {
    "SECUCODE": "000651.SZ", "SECURITY_CODE": "000651",
    "END_DATE": "2026-09-30 00:00:00", "HOLDER_RANK": 1,
    "HOLDER_NAME": "珠海明骏投资合伙企业(有限合伙)",
    "HOLD_NUM": 859566532, "HOLD_NUM_RATIO": 15.35,
    "HOLD_NUM_CHANGE": "不变", "CHANGE_RATIO": None,
    "SHARES_TYPE": "流通A股", "HOLDER_MARKET_CAP": 32904206844.96,
    "SECURITY_NAME_ABBR": "格力电器",
}

# 基金持仓 RPT_MAIN_ORGHOLD (000651 格力电器 2026 三季末 基金类型; 注意 EM 字段拼写 HOULD_NUM)
_FUND_HOLDING_ROW = {
    "SECURITY_CODE": "000651", "SECUCODE": "000651.SZ",
    "REPORT_DATE": "2026-09-30 00:00:00", "ORG_TYPE_NAME": "基金",
    "HOULD_NUM": 386, "TOTAL_SHARES": 303351573,
    "HOLD_VALUE": 11612298214.44, "FREESHARES_RATIO": 5.50258421,
    "HOLDCHA": "减仓", "SECURITY_NAME_ABBR": "格力电器",
}

# 新股申购 RPTA_APP_IPOAPPLY (688827 钶锐锶 2026-10-29 申购)
_IPO_ROW = {
    "SECUCODE": "688827.SH", "SECURITY_CODE": "688827",
    "APPLY_CODE": "787827", "APPLY_DATE": "2026-10-29 00:00:00",
    "LISTING_DATE": None, "MARKET_TYPE_NEW": "科创板",
    "ISSUE_PRICE": 45.37, "ISSUE_NUM": 1712.86, "TOTAL_RAISE_FUNDS": 7.771246,
    "AFTER_ISSUE_PE": 41.86, "INDUSTRY_PE": 38.85,
    "ONLINE_ISSUE_LWR": 0.02116291, "ONLINE_APPLY_UPPER": 4000,
    "EACHBALLOT_SHARES": "500", "INDUSTRY_NAME": "通用设备制造业",
    "SECURITY_NAME_ABBR": "钶锐锶",
}

# 业绩快报 RPT_FCI_PERFORMANCEE (000651 格力电器 2020 年报快报)
_PERF_ROW = {
    "SECURITY_CODE": "000651", "SECURITY_NAME_ABBR": "格力电器",
    "REPORT_DATE": "2020-12-31 00:00:00", "NOTICE_DATE": "2021-04-15 00:00:00",
    "DATATYPE": "2020年 年报", "BASIC_EPS": 3.71,
    "TOTAL_OPERATE_INCOME": 170497000000, "TOTAL_OPERATE_INCOME_SQ": 200508000000,
    "PARENT_NETPROFIT": 22175000000, "PARENT_NETPROFIT_SQ": 24697000000,
    "PARENT_BVPS": 19.15, "WEIGHTAVG_ROE": 18.78,
    "YSTZ": -14.967482594211, "JLRTBZCL": -10.211766611329,
}

# ---- P1-13 第三批 真机录制样例 (2026-11-01 新浪/腾讯冒烟) ----

# 新浪资金流 MoneyFlow.ssl_qsfx_zjlrqs (sh600519 贵州茅台 2026-10-30, 数值全是字符串,
# 比例字段是小数口径; r0x_ratio/cnt_r0x_ratio/turnover 服务未使用)
_MONEY_FLOW_ROW = {
    "opendate": "2026-10-30", "trade": "1392.0500",
    "changeratio": "0.0281019", "turnover": "47.9387",
    "netamount": "1573592085.3100", "ratioamount": "0.190568",
    "r0_net": "1377802257.6300", "r0_ratio": "0.16685704",
    "r0x_ratio": "80.44", "cnt_r0x_ratio": "2",
    "cate_ra": "0.203452", "cate_na": "6040487752.4600",
}

# 腾讯逐笔 stock.gtimg.cn detail (sz000651 格力电器 p=10 头部三条,
# 格式 "idx/时间/价/价差(元)/量(手)/额(元)/方向", 方向 B=买盘/S=卖盘/M=中性)
_TICK_ROWS = [
    "700/10:05:00/38.04/0.01/58/220586/B",
    "701/10:05:03/38.03/-0.01/150/570527/S",
    "702/10:05:06/38.04/0.01/56/212979/M",
]


class FakeClient:
    """记录调用参数并返回预置行的假 EM client。"""

    def __init__(self, rows=None, error=None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[dict] = []

    def datacenter_report(self, report_name, flt=None, **kw):
        self.calls.append({"report_name": report_name, "flt": flt, **kw})
        if self.error:
            raise self.error
        return self.rows


class FakeSinaClient:
    """记录调用参数并返回预置行的假新浪 client (P1-13 第三批)。"""

    def __init__(self, rows=None, error=None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[dict] = []

    def money_flow_daily(self, daima, num=60):
        self.calls.append({"daima": daima, "num": num})
        if self.error:
            raise self.error
        return self.rows


class FakeTxClient:
    """记录调用参数并返回预置原始 tick 的假腾讯 client (P1-13 第三批)。"""

    def __init__(self, ticks=None, error=None):
        self.ticks = ticks if ticks is not None else []
        self.error = error
        self.calls: list[dict] = []

    def tick_trades(self, code, pages=3):
        self.calls.append({"code": code, "pages": pages})
        if self.error:
            raise self.error
        return self.ticks


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


# ================================================================
# 归一化契约 (实测样例锁字段)
# ================================================================

class TestNormalize:
    def test_unlock_row(self):
        row = sp._norm_unlock([_UNLOCK_ROW])[0]
        assert row == {
            "free_date": "2010-01-04",
            "free_shares_wan": 7407.4,
            "free_market_cap_wan": 97555.458,
            "free_shares_type": "股权分置限售股份",
            # TOTALSHARES_RATIO 是小数占比 → 转百分数
            "ratio_of_total_pct": 24.6467,
            "holder_num": 3,
        }

    def test_pledge_row(self):
        row = sp._norm_pledge([_PLEDGE_ROW])[0]
        assert row == {
            "trade_date": "2026-10-30",
            # PLEDGE_RATIO 已是百分数, 不再乘 100
            "pledge_ratio_pct": 0.13,
            "pledge_shares_wan": 2438.48,
            "pledge_market_cap_wan": 28383.9072,
            "pledge_deal_num": 9,
        }

    def test_repurchase_row_progress_mapped(self):
        row = sp._norm_repurchase([_REPURCHASE_ROW])[0]
        assert row["notice_date"] == "2026-04-29"
        assert row["progress"] == "实施中"  # 004 → 中文
        assert row["amount"] == 2324756128.19
        assert row["num"] == 61974592.0
        assert row["price_cap"] == 54.55
        assert row["end_date"] == "2027-06-30"
        assert row["share_type"] == "流通A股"

    def test_repurchase_progress_full_mapping(self):
        # 001~007 全映射; 未知码原样透出(不丢数据)
        cases = {"001": "董事会预案", "002": "股东大会通过", "003": "股东大会否决",
                 "004": "实施中", "005": "停止实施", "006": "完成实施", "007": "提议"}
        for code, label in cases.items():
            row = sp._norm_repurchase([{**_REPURCHASE_ROW, "REPURPROGRESS": code}])[0]
            assert row["progress"] == label
        row = sp._norm_repurchase([{**_REPURCHASE_ROW, "REPURPROGRESS": "999"}])[0]
        assert row["progress"] == "999"

    def test_holder_number_row(self):
        row = sp._norm_holder_number([_HOLDER_ROW])[0]
        assert row == {
            "end_date": "2026-09-30",
            "holder_num": 278364,
            "holder_num_change": -18040,
            "holder_num_ratio_pct": -6.086287634445,
            "avg_hold_num": 4490.81634478596,
            "avg_market_cap": 5652231.26787451,
            "interval_chrate_pct": 6.16875722,
            "notice_date": "2026-10-31",
        }

    def test_dirty_values_become_none(self):
        # 空串/None/脏值 → None, 不炸
        row = sp._norm_unlock([{
            "FREE_DATE": None, "CURRENT_FREE_SHARES": "", "LIFT_MARKET_CAP": "abc",
            "FREE_SHARES_TYPE": None, "TOTALSHARES_RATIO": None, "BATCH_HOLDER_NUM": None,
        }])[0]
        assert row["free_date"] is None
        assert row["free_shares_wan"] is None
        assert row["free_market_cap_wan"] is None
        assert row["ratio_of_total_pct"] is None


# ================================================================
# 归一化 (P1-13 第二批: 十大股东/基金持仓/新股申购/业绩快报)
# ================================================================

class TestNormalizeBatch2:
    def test_top_holders_row(self):
        row = sp._norm_top_holders([_TOP_HOLDER_ROW])[0]
        assert row == {
            "end_date": "2026-09-30",
            "rank": 1,
            "holder_name": "珠海明骏投资合伙企业(有限合伙)",
            "hold_num": 859566532.0,
            "hold_ratio_pct": 15.35,  # 已是百分数, 不乘 100
            "hold_change": "不变",  # 混合类型: 文本原样透出
            "shares_type": "流通A股",
            "market_cap": 32904206844.96,
        }

    def test_fund_holding_row(self):
        row = sp._norm_fund_holding([_FUND_HOLDING_ROW])[0]
        assert row == {
            "report_date": "2026-09-30",
            "org_type": "基金",
            "org_num": 386,  # EM 原始字段是拼错的 HOULD_NUM
            "hold_shares": 303351573.0,
            "hold_value": 11612298214.44,
            "free_ratio_pct": 5.50258421,  # 已是百分数
            "hold_change": "减仓",
        }

    def test_ipo_info_row(self):
        row = sp._norm_ipo_info([_IPO_ROW])[0]
        assert row == {
            "apply_date": "2026-10-29",
            "apply_code": "787827",
            "name": "钶锐锶",
            "listing_date": None,  # 尚未上市
            "market": "科创板",
            "issue_price": 45.37,
            "issue_num_wan": 1712.86,  # 万股
            "raise_funds_yi": 7.771246,  # 亿元
            "pe_after_issue": 41.86,
            "industry_pe": 38.85,
            "win_rate_pct": 0.02116291,  # 已是百分数
            "apply_upper": 4000.0,
            "each_ballot_shares": "500",  # EM 给的是字符串, 原样
            "industry_name": "通用设备制造业",
        }

    def test_perf_express_row(self):
        row = sp._norm_perf_express([_PERF_ROW])[0]
        assert row == {
            "report_date": "2020-12-31",
            "notice_date": "2021-04-15",
            "data_type": "2020年 年报",
            "eps": 3.71,
            "revenue": 170497000000.0,
            "revenue_yoy_pct": -14.967482594211,  # YSTZ 已是百分数
            "net_profit": 22175000000.0,
            "net_profit_yoy_pct": -10.211766611329,  # JLRTBZCL 已是百分数
            "bvps": 19.15,
            "roe_pct": 18.78,
        }

    def test_batch2_dirty_values_become_none(self):
        # 第二批归一化同样抗脏值
        row = sp._norm_top_holders([{
            "END_DATE": None, "HOLDER_RANK": None, "HOLDER_NAME": None,
            "HOLD_NUM": "abc", "HOLD_NUM_RATIO": "", "HOLDER_MARKET_CAP": None,
        }])[0]
        assert row["end_date"] is None
        assert row["hold_num"] is None
        assert row["hold_ratio_pct"] is None
        row2 = sp._norm_perf_express([{"REPORT_DATE": "", "BASIC_EPS": "x"}])[0]
        assert row2["report_date"] is None
        assert row2["eps"] is None


# ================================================================
# client.datacenter_report (信封语义)
# ================================================================

class TestDatacenterReport:
    def _client_with(self, payload: dict) -> EastmoneyClient:
        c = EastmoneyClient(sleeper=lambda _: None)
        c._get_json = lambda base, path, params: payload  # type: ignore[method-assign]
        return c

    def test_success_returns_rows(self):
        c = self._client_with({"success": True, "result": {"pages": 1, "data": [{"A": 1}]}})
        assert c.datacenter_report("RPT_X") == [{"A": 1}]

    def test_success_null_result_returns_empty(self):
        c = self._client_with({"success": True, "result": None})
        assert c.datacenter_report("RPT_X") == []

    def test_code_9201_empty_is_legal(self):
        # 实测: 无记录时 success=false + code=9201 "返回数据为空" — 合法空结果不抛错
        c = self._client_with({"success": False, "code": 9201, "message": "返回数据为空", "result": None})
        assert c.datacenter_report("RPT_X") == []

    def test_other_failure_raises(self):
        c = self._client_with({"success": False, "code": 9501, "message": "报表配置不存在,RPT_X"})
        with pytest.raises(EastmoneyError, match="RPT_X"):
            c.datacenter_report("RPT_X")

    def test_params_assembled(self):
        captured = {}

        def fake_get(base, path, params):
            captured.update(params)
            return {"success": True, "result": {"data": []}}

        c = EastmoneyClient(sleeper=lambda _: None)
        c._get_json = fake_get  # type: ignore[method-assign]
        c.datacenter_report("RPT_X", flt='(SECURITY_CODE="600519")', page_size=60,
                            sort_columns="FREE_DATE", sort_types=-1)
        assert captured["reportName"] == "RPT_X"
        assert captured["filter"] == '(SECURITY_CODE="600519")'
        assert captured["pageSize"] == 60
        assert captured["sortColumns"] == "FREE_DATE"
        assert captured["sortTypes"] == -1
        assert captured["columns"] == "ALL"


# ================================================================
# get_profile: 缓存 / TTL / 降级
# ================================================================

class TestGetProfile:
    def test_fetch_normalizes_and_caches(self, data_dir):
        client = FakeClient(rows=[_HOLDER_ROW])
        r = sp.get_profile(data_dir, "holder_number", "600519.SH", client=client)
        assert r["state"] == "ok" and r["stale"] is False
        assert r["count"] == 1
        assert r["rows"][0]["holder_num"] == 278364
        # 过滤参数用裸 6 位码
        assert client.calls[0]["flt"] == '(SECURITY_CODE="600519")'
        assert client.calls[0]["report_name"] == "RPT_HOLDERNUM_DET"
        # 缓存已落盘
        cache = data_dir / "stock_profile" / "holder_number" / "600519.json"
        assert cache.exists()
        assert json.loads(cache.read_text())["rows"][0]["end_date"] == "2026-09-30"

    def test_fresh_cache_skips_network(self, data_dir):
        c1 = FakeClient(rows=[_HOLDER_ROW])
        sp.get_profile(data_dir, "holder_number", "600519.SH", client=c1)
        c2 = FakeClient(rows=[])
        r = sp.get_profile(data_dir, "holder_number", "600519.SH", client=c2)
        assert c2.calls == []  # 缓存新鲜, 没打接口
        assert r["count"] == 1

    def test_expired_cache_refetches(self, data_dir, monkeypatch):
        c1 = FakeClient(rows=[_HOLDER_ROW])
        sp.get_profile(data_dir, "holder_number", "600519.SH", client=c1)
        # 把缓存时间拨到 TTL 之外
        path = data_dir / "stock_profile" / "holder_number" / "600519.json"
        old = json.loads(path.read_text())
        old["fetched_at"] = time.time() - sp.CACHE_TTL_S - 10
        path.write_text(json.dumps(old))
        c2 = FakeClient(rows=[_HOLDER_ROW, _HOLDER_ROW])
        r = sp.get_profile(data_dir, "holder_number", "600519.SH", client=c2)
        assert len(c2.calls) == 1  # 过期 → 重拉
        assert r["count"] == 2

    def test_force_bypasses_cache(self, data_dir):
        sp.get_profile(data_dir, "holder_number", "600519.SH", client=FakeClient(rows=[_HOLDER_ROW]))
        c2 = FakeClient(rows=[])
        r = sp.get_profile(data_dir, "holder_number", "600519.SH", force=True, client=c2)
        assert len(c2.calls) == 1
        assert r["count"] == 0

    def test_failure_with_old_cache_returns_stale(self, data_dir):
        sp.get_profile(data_dir, "holder_number", "600519.SH", client=FakeClient(rows=[_HOLDER_ROW]))
        boom = FakeClient(error=EastmoneyError("接口挂了"))
        r = sp.get_profile(data_dir, "holder_number", "600519.SH", force=True, client=boom)
        assert r["state"] == "ok" and r["stale"] is True
        assert r["count"] == 1  # 旧数据还在
        assert "缓存" in r["warning"]

    def test_failure_without_cache_degrades(self, data_dir):
        boom = FakeClient(error=EastmoneyError("接口挂了"))
        r = sp.get_profile(data_dir, "unlock", "600519.SH", client=boom)
        assert r["state"] == "source_unavailable"
        assert r["rows"] == []
        assert "接口挂了" in r["message"]

    def test_corrupt_cache_refetches(self, data_dir):
        path = data_dir / "stock_profile" / "unlock" / "600519.json"
        path.parent.mkdir(parents=True)
        path.write_text("{不是json")
        r = sp.get_profile(data_dir, "unlock", "600519.SH", client=FakeClient(rows=[_UNLOCK_ROW]))
        assert r["state"] == "ok" and r["count"] == 1

    def test_rows_capped_at_max(self, data_dir, monkeypatch):
        monkeypatch.setattr(sp, "MAX_ROWS", 2)
        rows = [_UNLOCK_ROW] * 5
        r = sp.get_profile(data_dir, "unlock", "000521.SZ", client=FakeClient(rows=rows))
        assert r["count"] == 2

    def test_symbol_without_suffix_ok(self, data_dir):
        client = FakeClient(rows=[])
        sp.get_profile(data_dir, "unlock", "600519", client=client)
        assert client.calls[0]["flt"] == '(SECURITY_CODE="600519")'

    def test_repurchase_uses_dim_scode_filter(self, data_dir):
        client = FakeClient(rows=[])
        sp.get_profile(data_dir, "repurchase", "000651.SZ", client=client)
        assert client.calls[0]["flt"] == '(DIM_SCODE="000651")'
        assert client.calls[0]["report_name"] == "RPTA_WEB_GETHGLIST_NEW"

    def test_top_holders_multi_column_sort(self, data_dir):
        # P1-13 第二批: 十大股东需要 END_DATE 倒序 + HOLDER_RANK 升序(多列排序 "-1,1")
        client = FakeClient(rows=[])
        sp.get_profile(data_dir, "top_holders", "000651.SZ", client=client)
        call = client.calls[0]
        assert call["report_name"] == "RPT_F10_EH_HOLDERS"
        assert call["sort_columns"] == "END_DATE,HOLDER_RANK"
        assert call["sort_types"] == "-1,1"

    def test_batch2_registry_wiring(self, data_dir):
        # P1-13 第二批: 4 个新数据集都能走通 get_profile 且报表名正确
        expected = {
            "top_holders": "RPT_F10_EH_HOLDERS",
            "fund_holding": "RPT_MAIN_ORGHOLD",
            "ipo_info": "RPTA_APP_IPOAPPLY",
            "perf_express": "RPT_FCI_PERFORMANCEE",
        }
        for ds, report in expected.items():
            client = FakeClient(rows=[])
            result = sp.get_profile(data_dir, ds, "000651.SZ", client=client)
            assert result["state"] == "ok"
            assert client.calls[0]["report_name"] == report
            assert client.calls[0]["flt"] == '(SECURITY_CODE="000651")'

    def test_datasets_labels_registry_consistent(self):
        # 定义必须一致(10 个数据集), 防止加数据集时漏改:
        # 前八类走 _REGISTRY(EM datacenter), 第三批两类走专用 fetcher(新浪/腾讯)
        assert len(sp.DATASETS) == 10
        assert set(sp.DATASETS) == set(sp.DATASET_LABELS)
        assert set(sp._REGISTRY) | {"money_flow", "tick_trade"} == set(sp.DATASETS)
        assert set(sp.DATASET_SOURCE) == {"money_flow", "tick_trade"}

    def test_bad_symbol_raises(self, data_dir):
        with pytest.raises(ValueError, match="symbol"):
            sp.get_profile(data_dir, "unlock", "茅台", client=FakeClient())

    def test_unknown_dataset_raises(self, data_dir):
        with pytest.raises(ValueError, match="未知"):
            sp.get_profile(data_dir, "fund_hold", "600519.SH", client=FakeClient())

    def test_cache_write_failure_does_not_break(self, data_dir, monkeypatch):
        # 缓存盘写不进去(只读目录等) → 数据照常返回
        monkeypatch.setattr(sp, "atomic_write_text",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        r = sp.get_profile(data_dir, "unlock", "600519.SH", client=FakeClient(rows=[_UNLOCK_ROW]))
        assert r["state"] == "ok" and r["count"] == 1


# ================================================================
# P1-13 第三批: 资金流向(新浪) / 逐笔成交(腾讯)
# ================================================================

class TestNormalizeBatch3:
    def test_money_flow_row(self):
        [row] = sp._norm_money_flow([_MONEY_FLOW_ROW])
        assert row["date"] == "2026-10-30"
        assert row["close"] == 1392.05
        # changeratio 小数口径 → 百分数(与 EM 数据集 _pct 后缀约定一致)
        assert row["chg_pct"] == pytest.approx(2.8102)
        assert row["net_inflow"] == pytest.approx(1573592085.31)
        assert row["net_ratio_pct"] == pytest.approx(19.0568)
        assert row["main_net"] == pytest.approx(1377802257.63)
        assert row["main_ratio_pct"] == pytest.approx(16.6857)
        assert row["industry_chg_pct"] == pytest.approx(20.3452)
        assert row["industry_net"] == pytest.approx(6040487752.46)

    def test_tick_trade_rows(self):
        rows = sp._norm_tick_trade(_TICK_ROWS)
        assert len(rows) == 3
        assert rows[0] == {"time": "10:05:00", "price": 38.04, "change": 0.01,
                           "volume_lots": 58.0, "amount": 220586.0, "side": "买盘"}
        assert rows[1]["side"] == "卖盘" and rows[1]["change"] == -0.01
        assert rows[2]["side"] == "中性"

    def test_tick_malformed_skipped(self):
        # 段数不足的脏行直接跳过, 不影响好行
        rows = sp._norm_tick_trade(["700/10:05:00/38.04", "bad", _TICK_ROWS[0]])
        assert len(rows) == 1 and rows[0]["time"] == "10:05:00"

    def test_money_flow_dirty_values_become_none(self):
        [row] = sp._norm_money_flow([{"opendate": "", "trade": "-", "changeratio": None}])
        assert row["date"] is None and row["close"] is None and row["chg_pct"] is None


class TestBatch3Wiring:
    def test_money_flow_uses_sina_with_prefix(self, data_dir):
        sina = FakeSinaClient(rows=[_MONEY_FLOW_ROW])
        r = sp.get_profile(data_dir, "money_flow", "600519.SH", sina_client=sina)
        assert r["state"] == "ok" and r["source"] == "sina"
        assert sina.calls[0]["daima"] == "sh600519"
        assert r["count"] == 1 and r["rows"][0]["chg_pct"] == pytest.approx(2.8102)

    def test_tick_trade_uses_tencent_with_prefix(self, data_dir):
        tx = FakeTxClient(ticks=_TICK_ROWS)
        r = sp.get_profile(data_dir, "tick_trade", "000651.SZ", tx_client=tx)
        assert r["state"] == "ok" and r["source"] == "tencent"
        assert tx.calls[0]["code"] == "sz000651"
        assert r["count"] == 3

    def test_prefix_inference(self):
        # 6/9/5 开头 → sh(含科创板 688、B 股 900、沪市基金 5); 4/8 → bj; 其余 → sz
        for code, want in [("600519", "sh"), ("688827", "sh"), ("900901", "sh"),
                           ("510300", "sh"), ("920002", "bj"), ("430047", "bj"),
                           ("832000", "bj"), ("000651", "sz"), ("300750", "sz"),
                           ("200011", "sz")]:
            assert sp._infer_prefix(code) == want, code

    def test_bare_code_gets_prefix(self, data_dir):
        # 裸 6 位码(无 .SH 后缀)也能推断前缀
        sina = FakeSinaClient(rows=[])
        sp.get_profile(data_dir, "money_flow", "600519", sina_client=sina)
        assert sina.calls[0]["daima"] == "sh600519"

    def test_tick_ttl_60s(self, data_dir):
        tx1 = FakeTxClient(ticks=_TICK_ROWS)
        sp.get_profile(data_dir, "tick_trade", "000651.SZ", tx_client=tx1)
        # TTL 内 → 直接读缓存不打接口
        tx2 = FakeTxClient(ticks=[])
        r = sp.get_profile(data_dir, "tick_trade", "000651.SZ", tx_client=tx2)
        assert len(tx2.calls) == 0 and r["count"] == 3
        # 把缓存时间拨到逐笔专用 TTL(60s, 而非资料类 12h)之外 → 重拉
        path = data_dir / "stock_profile" / "tick_trade" / "000651.json"
        old = json.loads(path.read_text())
        old["fetched_at"] = time.time() - sp.DATASET_TTL_S["tick_trade"] - 1
        path.write_text(json.dumps(old))
        tx3 = FakeTxClient(ticks=_TICK_ROWS[:1])
        r = sp.get_profile(data_dir, "tick_trade", "000651.SZ", tx_client=tx3)
        assert len(tx3.calls) == 1 and r["count"] == 1

    def test_ttl_and_max_rows_config(self):
        # 分级 TTL/行数上限的配置契约(防止误改回全局 12h/60 条)
        assert sp.DATASET_TTL_S == {"money_flow": 1800, "tick_trade": 60}
        assert sp.DATASET_MAX_ROWS["tick_trade"] > sp.MAX_ROWS

    def test_sina_failure_degrades(self, data_dir):
        from app.plugins.sina.client import SinaError

        boom = FakeSinaClient(error=SinaError("限流"))
        r = sp.get_profile(data_dir, "money_flow", "600519.SH", sina_client=boom)
        assert r["state"] == "source_unavailable" and r["source"] == "sina"
        assert "新浪" in r["message"]

    def test_tencent_failure_with_cache_returns_stale(self, data_dir):
        from app.plugins.tencent.client import TencentError

        sp.get_profile(data_dir, "tick_trade", "000651.SZ", tx_client=FakeTxClient(ticks=_TICK_ROWS))
        boom = FakeTxClient(error=TencentError("熔断中"))
        r = sp.get_profile(data_dir, "tick_trade", "000651.SZ", force=True, tx_client=boom)
        assert r["stale"] is True and r["count"] == 3


# ================================================================
# API 端点
# ================================================================

class TestApi:
    def _request(self, data_dir):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir)))))

    def test_get_ok(self, data_dir, monkeypatch):
        from app.api.stock_profile import get_stock_profile

        monkeypatch.setattr(sp, "_get_client", lambda: FakeClient(rows=[_HOLDER_ROW]))
        r = get_stock_profile(self._request(data_dir), "holder_number", "600519.SH", False)
        assert r["state"] == "ok" and r["count"] == 1

    def test_bad_dataset_400(self, data_dir):
        from fastapi import HTTPException

        from app.api.stock_profile import get_stock_profile

        with pytest.raises(HTTPException) as ei:
            get_stock_profile(self._request(data_dir), "fund_hold", "600519.SH", False)
        assert ei.value.status_code == 400

    def test_bad_symbol_400(self, data_dir):
        from fastapi import HTTPException

        from app.api.stock_profile import get_stock_profile

        with pytest.raises(HTTPException) as ei:
            get_stock_profile(self._request(data_dir), "unlock", "bad-symbol", False)
        assert ei.value.status_code == 400


# ================================================================
# AI 工具封装
# ================================================================

class TestAiTool:
    def _ctx(self, data_dir):
        return SimpleNamespace(data_dir=data_dir)

    def test_tool_returns_rows(self, data_dir, monkeypatch):
        from app.custom.assistant.tools import _get_stock_profile

        monkeypatch.setattr(sp, "_get_client", lambda: FakeClient(rows=[_UNLOCK_ROW]))
        r = _get_stock_profile({"symbol": "000521.SZ", "dataset": "unlock"}, self._ctx(data_dir))
        assert r["label"] == "限售解禁" and r["count"] == 1

    def test_tool_default_dataset_unlock(self, data_dir, monkeypatch):
        from app.custom.assistant.tools import _get_stock_profile

        client = FakeClient(rows=[])
        monkeypatch.setattr(sp, "_get_client", lambda: client)
        _get_stock_profile({"symbol": "600519.SH"}, self._ctx(data_dir))
        assert client.calls[0]["report_name"] == "RPT_LIFT_STAGE"

    def test_tool_degraded_has_note(self, data_dir, monkeypatch):
        from app.custom.assistant.tools import _get_stock_profile

        monkeypatch.setattr(sp, "_get_client",
                            lambda: FakeClient(error=EastmoneyError("封禁中")))
        r = _get_stock_profile({"symbol": "600519.SH", "dataset": "pledge"}, self._ctx(data_dir))
        assert r["rows"] == [] and "封禁中" in r["note"]

    def test_tool_empty_rows_note(self, data_dir, monkeypatch):
        from app.custom.assistant.tools import _get_stock_profile

        monkeypatch.setattr(sp, "_get_client", lambda: FakeClient(rows=[]))
        r = _get_stock_profile({"symbol": "600519.SH", "dataset": "repurchase"}, self._ctx(data_dir))
        assert r["count"] == 0 and "暂无" in r["note"]

    def test_tool_bad_dataset_raises(self, data_dir):
        from app.custom.assistant.tools import _get_stock_profile

        with pytest.raises(ValueError, match="dataset"):
            _get_stock_profile({"symbol": "600519.SH", "dataset": "xx"}, self._ctx(data_dir))

    def test_tool_registered_with_schema(self):
        from app.custom.assistant.tools import _LOCAL_TOOLS, build_tool_schemas

        assert "get_stock_profile" in _LOCAL_TOOLS
        names = [s["function"]["name"] for s in build_tool_schemas()]
        assert "get_stock_profile" in names
