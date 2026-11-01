"""个股资料数据服务 (P1-13) — 解禁/质押/回购/股东户数(第一批) + 十大股东/基金持仓/新股申购/业绩快报(第二批)
+ 资金流向/逐笔成交(第三批)。

照龙虎榜模式做的"非路由数据集": 不进能力集、不参与三层保险(这些是低频资料
不是行情关键链路), 直连数据源 client, 拉失败降级返回 source_unavailable。

数据源 (2026-11 实测十类全可用):
  EM datacenter-web (与 push2 独立限速/熔断/封禁池) — 前八类:
  - unlock        限售解禁时间表  RPT_LIFT_STAGE         按 SECURITY_CODE 过滤, FREE_DATE 倒序
  - pledge        股权质押比例    RPT_CSDC_LIST          按 SECURITY_CODE 过滤, TRADE_DATE 倒序 (中登周频)
  - repurchase    股票回购        RPTA_WEB_GETHGLIST_NEW 按 DIM_SCODE 过滤, DIM_DATE 倒序
  - holder_number 股东户数变化    RPT_HOLDERNUM_DET      按 SECURITY_CODE 过滤, END_DATE 倒序 (季频)
  - top_holders   十大股东        RPT_F10_EH_HOLDERS     按 SECURITY_CODE 过滤, END_DATE 倒序+HOLDER_RANK 升序 (季频)
  - fund_holding  基金持仓        RPT_MAIN_ORGHOLD       按 SECURITY_CODE 过滤, REPORT_DATE 倒序 (季频, 含基金/QFII/社保/保险/券商/信托分类型)
  - ipo_info      新股申购        RPTA_APP_IPOAPPLY      按 SECURITY_CODE 过滤, APPLY_DATE 倒序 (每标的通常仅 1 条)
  - perf_express  业绩快报        RPT_FCI_PERFORMANCEE   按 SECURITY_CODE 过滤, REPORT_DATE 倒序 (不定期, 部分公司常年不发)
  第三批改用其他免费源 (EM push2/push2his 集群封禁是间歇性的, 实测几分钟内从通到
  HTTP 000, 不能作稳定源; 腾讯 ff_ 资金流已废弃; 新浪逐笔 CN_Transactions 已死):
  - money_flow    资金流向(日频历史, 最近60交易日)  新浪 MoneyFlow.ssl_qsfx_zjlrqs
  - tick_trade    逐笔成交(最近交易日尾部3页~210条) 腾讯 stock.gtimg.cn detail 分页

缓存策略: 按需拉取 + JSON 落盘 (data/stock_profile/{dataset}/{code}.json)。
TTL 按数据集分级 (DATASET_TTL_S): 资料类 12 小时(日更已足够), 资金流 30 分钟
(日频数据盘中会更新当日行), 逐笔 60 秒(准实时)。
拉取失败但有旧缓存 → 返回旧数据并标 stale=True(有总比没有强); 无缓存 → 降级。

字段口径(实测样例锁定, 契约测试见 tests/test_stock_profile.py):
  - 日期字段 EM 返回 "YYYY-MM-DD 00:00:00", 统一裁成 "YYYY-MM-DD"
  - 解禁股数/市值单位是万股/万元; 质押余额单位万股、质押市值单位万元
  - 回购金额/数量单位是元/股; 股东户数为绝对户数, 户均持股为股
  - 十大股东持股数/市值单位是股/元; 基金持仓持股数单位股、市值单位元
  - 新股发行量单位万股、募资额单位亿元; 业绩快报营收/净利单位元
  - 比例字段 EM 口径不一: TOTALSHARES_RATIO 是小数占比, PLEDGE_RATIO/HOLDER_NUM_RATIO/
    HOLD_NUM_RATIO/FREESHARES_RATIO/YSTZ/JLRTBZCL 已是百分数 — 归一化时统一转成
    "百分数"并在键名带 _pct 后缀, 前端不再换算
  - 新浪资金流数值全是字符串且比例是小数(changeratio/ratioamount/r0_ratio/cate_ra),
    归一化统一转 float + 百分数; 净额单位元
  - 腾讯逐笔每条 "idx/时间/价/价差/量(手)/额(元)/方向": 价差是与上一笔的价差(元,
    不是百分比, 实测锁定); 方向 B=买盘/S=卖盘/M=中性 → 归一化转中文
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# 十类资料数据集(P1-13 第一批 4 类 + 第二批 4 类 + 第三批 2 类)。
DATASETS = (
    "unlock", "pledge", "repurchase", "holder_number",
    "top_holders", "fund_holding", "ipo_info", "perf_express",
    "money_flow", "tick_trade",
)

DATASET_LABELS = {
    "unlock": "限售解禁",
    "pledge": "股权质押",
    "repurchase": "股票回购",
    "holder_number": "股东户数",
    "top_holders": "十大股东",
    "fund_holding": "基金持仓",  # 实际是机构持仓分类型统计(基金/QFII/社保/保险/券商/信托/其他/机构汇总)
    "ipo_info": "新股申购",
    "perf_express": "业绩快报",
    "money_flow": "资金流向",
    "tick_trade": "逐笔成交",
}

# 缓存有效期: 资料类 12 小时(日更已足够, 避免频繁打免费接口)。
CACHE_TTL_S = 12 * 3600

# P1-13 第三批: 准实时数据集单独分级 TTL —
# 资金流是日频数据但盘中当日行会持续更新, 30 分钟够用;
# 逐笔是盘中滚动数据, 60 秒(再短会把免费接口打爆)。
DATASET_TTL_S = {
    "money_flow": 30 * 60,
    "tick_trade": 60,
}

# 每类资料最多缓存/返回条数: 个股资料看最近若干条即可, 全量历史无意义。
MAX_ROWS = 60

# P1-13 第三批: 逐笔一次拉尾部 3 页 ~210 条, 默认 60 会截断, 单独放宽。
DATASET_MAX_ROWS = {
    "tick_trade": 500,
}

# 数据集 → 数据源标识(降级提示/前端页脚用)。未列出的默认 eastmoney。
DATASET_SOURCE = {
    "money_flow": "sina",
    "tick_trade": "tencent",
}

_SOURCE_LABELS = {"eastmoney": "东方财富", "sina": "新浪财经", "tencent": "腾讯财经"}

# 回购进度码 → 中文(001~006 来自 EM 网页 JS 映射; 007 为新增码, 网页旧 JS 未收录,
# 按实测样例语义(董事长提议回购, 尚未走董事会预案)标为"提议")
REPURCHASE_PROGRESS = {
    "001": "董事会预案",
    "002": "股东大会通过",
    "003": "股东大会否决",
    "004": "实施中",
    "005": "停止实施",
    "006": "完成实施",
    "007": "提议",
}

_SYMBOL_RE = re.compile(r"^(\d{6})(?:\.(SH|SZ|BJ))?$")

# 模块级共享 client(懒建): 各主机限速+熔断在 client 实例内,
# 单例保证服务内所有请求共享同一令牌桶, 不会叠加打爆免费接口。
_client = None
_client_lock = threading.Lock()
_tx_client = None
_tx_client_lock = threading.Lock()
_sina_client = None
_sina_client_lock = threading.Lock()


def _get_client():
    """懒建共享 EastmoneyClient(测试可注入自己的 client, 不走这里)。"""
    global _client
    with _client_lock:
        if _client is None:
            from app.plugins.eastmoney.client import EastmoneyClient
            _client = EastmoneyClient()
        return _client


def _get_tx_client():
    """懒建共享 TencentClient(P1-13 第三批逐笔用; 测试可注入)。"""
    global _tx_client
    with _tx_client_lock:
        if _tx_client is None:
            from app.plugins.tencent.client import TencentClient
            _tx_client = TencentClient()
        return _tx_client


def _get_sina_client():
    """懒建共享 SinaClient(P1-13 第三批资金流用; 测试可注入)。"""
    global _sina_client
    with _sina_client_lock:
        if _sina_client is None:
            from app.plugins.sina.client import SinaClient
            _sina_client = SinaClient()
        return _sina_client


def _infer_prefix(code: str) -> str:
    """裸 6 位代码 → 市场前缀(sh/sz/bj)。新浪/腾讯接口都要带前缀, 而本服务接受裸码。

    规则与 tencent provider 的 symbol_to_tx_code 同源, 注意两个易混段:
    92 开头(920xxx)是北交所新号段, 但 90 开头(900xxx)是沪 B 股 — 都姓"9"要分开;
    6/5 开头沪市(含科创板 688、沪市基金 5), 4/8 开头北交所(原新三板号段), 其余深市。
    """
    if code.startswith("92") or code[0] in "48":
        return "bj"
    if code[0] in "569":
        return "sh"
    return "sz"


def _d10(v) -> str | None:
    """"YYYY-MM-DD 00:00:00" → "YYYY-MM-DD"; 空值返回 None。"""
    if not v:
        return None
    return str(v)[:10]


def _f(v) -> float | None:
    """数值字段安全转 float(None/空串/脏值 → None)。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v) -> float | None:
    """小数占比 → 百分数(保留 4 位)。用于 TOTALSHARES_RATIO 这类小数口径字段。"""
    f = _f(v)
    return None if f is None else round(f * 100, 4)


# ---- 各数据集的归一化: EM 原始行 → 精简稳定行(契约测试锁字段) ----

def _norm_unlock(rows: list[dict]) -> list[dict]:
    """限售解禁: 解禁日/本次解禁股数(万股)/解禁市值(万元)/类型/占总股本比%/涉户数。"""
    out = []
    for r in rows:
        out.append({
            "free_date": _d10(r.get("FREE_DATE")),
            "free_shares_wan": _f(r.get("CURRENT_FREE_SHARES")),
            "free_market_cap_wan": _f(r.get("LIFT_MARKET_CAP")),
            "free_shares_type": r.get("FREE_SHARES_TYPE"),
            "ratio_of_total_pct": _pct(r.get("TOTALSHARES_RATIO")),
            "holder_num": r.get("BATCH_HOLDER_NUM"),
        })
    return out


def _norm_pledge(rows: list[dict]) -> list[dict]:
    """股权质押(中登周频): 交易日/质押比例%(已是百分数)/质押余额(万股)/质押市值(万元)/笔数。"""
    out = []
    for r in rows:
        out.append({
            "trade_date": _d10(r.get("TRADE_DATE")),
            "pledge_ratio_pct": _f(r.get("PLEDGE_RATIO")),
            "pledge_shares_wan": _f(r.get("REPURCHASE_BALANCE")),
            "pledge_market_cap_wan": _f(r.get("PLEDGE_MARKET_CAP")),
            "pledge_deal_num": r.get("PLEDGE_DEAL_NUM"),
        })
    return out


def _norm_repurchase(rows: list[dict]) -> list[dict]:
    """股票回购: 公告日/进度(码→中文)/已回购金额(元)/数量(股)/价格上限/股份类型/截止日/目的。"""
    out = []
    for r in rows:
        code = r.get("REPURPROGRESS")
        out.append({
            "notice_date": _d10(r.get("DIM_DATE")),
            "progress": REPURCHASE_PROGRESS.get(code, code),
            "amount": _f(r.get("REPURAMOUNT")),
            "num": _f(r.get("REPURNUM")),
            "price_cap": _f(r.get("REPURPRICECAP")),
            "share_type": r.get("SHARETYPE"),
            "end_date": _d10(r.get("REPURENDDATE")),
            "objective": r.get("REPUROBJECTIVE"),
        })
    return out


def _norm_holder_number(rows: list[dict]) -> list[dict]:
    """股东户数(季频): 截止日/户数/较上期变化/变化%(已是百分数)/户均持股/户均市值(元)/区间涨跌%/公告日。"""
    out = []
    for r in rows:
        out.append({
            "end_date": _d10(r.get("END_DATE")),
            "holder_num": r.get("HOLDER_NUM"),
            "holder_num_change": r.get("HOLDER_NUM_CHANGE"),
            "holder_num_ratio_pct": _f(r.get("HOLDER_NUM_RATIO")),
            "avg_hold_num": _f(r.get("AVG_HOLD_NUM")),
            "avg_market_cap": _f(r.get("AVG_MARKET_CAP")),
            "interval_chrate_pct": _f(r.get("INTERVAL_CHRATE")),
            "notice_date": _d10(r.get("HOLD_NOTICE_DATE")),
        })
    return out


# ---- P1-13 第二批: 十大股东 / 基金持仓 / 新股申购 / 业绩快报 ----

def _norm_top_holders(rows: list[dict]) -> list[dict]:
    """十大股东(季频): 截止日/排名/股东名/持股数(股)/占总股本%(已是百分数)/变动/股份类型/持股市值(元)。

    HOLD_NUM_CHANGE 是混合类型: EM 有时给数字(增减股数)有时给文本("不变"), 原样透出。
    """
    out = []
    for r in rows:
        out.append({
            "end_date": _d10(r.get("END_DATE")),
            "rank": r.get("HOLDER_RANK"),
            "holder_name": r.get("HOLDER_NAME"),
            "hold_num": _f(r.get("HOLD_NUM")),
            "hold_ratio_pct": _f(r.get("HOLD_NUM_RATIO")),
            "hold_change": r.get("HOLD_NUM_CHANGE"),
            "shares_type": r.get("SHARES_TYPE"),
            "market_cap": _f(r.get("HOLDER_MARKET_CAP")),
        })
    return out


def _norm_fund_holding(rows: list[dict]) -> list[dict]:
    """基金持仓(季频, 分机构类型): 报告期/机构类型/机构家数/持股合计(股)/市值(元)/占流通股%(已是百分数)/持仓变动。

    注意 EM 原始字段名是 HOULD_NUM(官方拼写错误), 不是 HOLD_NUM。
    HOLDCHA 是文本: 增仓/减仓/新进/不变; 机构汇总行为 None。
    """
    out = []
    for r in rows:
        out.append({
            "report_date": _d10(r.get("REPORT_DATE")),
            "org_type": r.get("ORG_TYPE_NAME"),
            "org_num": r.get("HOULD_NUM"),
            "hold_shares": _f(r.get("TOTAL_SHARES")),
            "hold_value": _f(r.get("HOLD_VALUE")),
            "free_ratio_pct": _f(r.get("FREESHARES_RATIO")),
            "hold_change": r.get("HOLDCHA"),
        })
    return out


def _norm_ipo_info(rows: list[dict]) -> list[dict]:
    """新股申购: 申购日/申购代码/名称/上市日/板块/发行价(元)/发行量(万股)/募资(亿元)/发行PE/行业PE/中签率%(已是百分数)/申购上限(股)/每签股数/行业。"""
    out = []
    for r in rows:
        out.append({
            "apply_date": _d10(r.get("APPLY_DATE")),
            "apply_code": r.get("APPLY_CODE"),
            "name": r.get("SECURITY_NAME_ABBR"),
            "listing_date": _d10(r.get("LISTING_DATE")),
            "market": r.get("MARKET_TYPE_NEW"),
            "issue_price": _f(r.get("ISSUE_PRICE")),
            "issue_num_wan": _f(r.get("ISSUE_NUM")),
            "raise_funds_yi": _f(r.get("TOTAL_RAISE_FUNDS")),
            "pe_after_issue": _f(r.get("AFTER_ISSUE_PE")),
            "industry_pe": _f(r.get("INDUSTRY_PE")),
            "win_rate_pct": _f(r.get("ONLINE_ISSUE_LWR")),
            "apply_upper": _f(r.get("ONLINE_APPLY_UPPER")),
            "each_ballot_shares": r.get("EACHBALLOT_SHARES"),
            "industry_name": r.get("INDUSTRY_NAME"),
        })
    return out


def _norm_perf_express(rows: list[dict]) -> list[dict]:
    """业绩快报: 报告期/公告日/类型/EPS/营收(元)/营收同比%(已是百分数)/净利(元)/净利同比%/每股净资产/ROE%(已是百分数)。"""
    out = []
    for r in rows:
        out.append({
            "report_date": _d10(r.get("REPORT_DATE")),
            "notice_date": _d10(r.get("NOTICE_DATE")),
            "data_type": r.get("DATATYPE"),
            "eps": _f(r.get("BASIC_EPS")),
            "revenue": _f(r.get("TOTAL_OPERATE_INCOME")),
            "revenue_yoy_pct": _f(r.get("YSTZ")),
            "net_profit": _f(r.get("PARENT_NETPROFIT")),
            "net_profit_yoy_pct": _f(r.get("JLRTBZCL")),
            "bvps": _f(r.get("PARENT_BVPS")),
            "roe_pct": _f(r.get("WEIGHTAVG_ROE")),
        })
    return out


# ---- P1-13 第三批: 资金流向(新浪) / 逐笔成交(腾讯) ----

def _norm_money_flow(rows: list[dict]) -> list[dict]:
    """资金流向(新浪日频): 日期/收盘/涨跌%/净流入(元)/净流入占比%/主力净额(元)/主力净占比%/行业涨跌%/行业净额(元)。

    新浪数值全是字符串, 比例字段(changeratio/ratioamount/r0_ratio/cate_ra)是小数口径,
    统一转 float + 百分数(_pct), 与 EM 数据集的 _pct 后缀约定一致。
    主力 = 超大单(r0), 新浪没有 EM 那种五档分类, 只有主力/散户两级。
    """
    out = []
    for r in rows:
        out.append({
            "date": _d10(r.get("opendate")),
            "close": _f(r.get("trade")),
            "chg_pct": _pct(r.get("changeratio")),
            "net_inflow": _f(r.get("netamount")),
            "net_ratio_pct": _pct(r.get("ratioamount")),
            "main_net": _f(r.get("r0_net")),
            "main_ratio_pct": _pct(r.get("r0_ratio")),
            "industry_chg_pct": _pct(r.get("cate_ra")),
            "industry_net": _f(r.get("cate_na")),
        })
    return out


# 腾讯逐笔方向码 → 中文
_TICK_SIDE = {"B": "买盘", "S": "卖盘", "M": "中性"}


def _norm_tick_trade(ticks: list[str]) -> list[dict]:
    """逐笔成交(腾讯): 时间/价/价差(元, 与上一笔比)/量(手)/额(元)/方向(中文)。

    原始每条 "idx/时间/价/涨跌/量/额/方向", 7 段; 段数不足的脏行直接跳过。
    idx 是全天序号(3 秒一笔, 全天约 4800), 面板用不上不透出。
    """
    out = []
    for t in ticks:
        parts = t.split("/")
        if len(parts) < 7:
            continue
        out.append({
            "time": parts[1],
            "price": _f(parts[2]),
            "change": _f(parts[3]),
            "volume_lots": _f(parts[4]),
            "amount": _f(parts[5]),
            "side": _TICK_SIDE.get(parts[6], parts[6]),
        })
    return out


# 数据集 → (报表名, 过滤列, 排序列, 排序方向, 归一化函数)
# 排序列/方向支持逗号分隔多列(十大股东需要 END_DATE 倒序 + HOLDER_RANK 升序, 即 "-1,1")
_REGISTRY: dict[str, tuple[str, str, str, int | str, Callable[[list[dict]], list[dict]]]] = {
    "unlock": ("RPT_LIFT_STAGE", "SECURITY_CODE", "FREE_DATE", -1, _norm_unlock),
    "pledge": ("RPT_CSDC_LIST", "SECURITY_CODE", "TRADE_DATE", -1, _norm_pledge),
    "repurchase": ("RPTA_WEB_GETHGLIST_NEW", "DIM_SCODE", "DIM_DATE", -1, _norm_repurchase),
    "holder_number": ("RPT_HOLDERNUM_DET", "SECURITY_CODE", "END_DATE", -1, _norm_holder_number),
    "top_holders": ("RPT_F10_EH_HOLDERS", "SECURITY_CODE", "END_DATE,HOLDER_RANK", "-1,1", _norm_top_holders),
    "fund_holding": ("RPT_MAIN_ORGHOLD", "SECURITY_CODE", "REPORT_DATE", -1, _norm_fund_holding),
    "ipo_info": ("RPTA_APP_IPOAPPLY", "SECURITY_CODE", "APPLY_DATE", -1, _norm_ipo_info),
    "perf_express": ("RPT_FCI_PERFORMANCEE", "SECURITY_CODE", "REPORT_DATE", -1, _norm_perf_express),
}


def _cache_path(data_dir: Path, dataset: str, code: str) -> Path:
    return data_dir / "stock_profile" / dataset / f"{code}.json"


def _read_cache(path: Path) -> dict | None:
    """读缓存文件; 损坏/不存在返回 None(缓存永远不能挡住取数)。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fetch_rows(dataset: str, code: str, client, tx_client, sina_client) -> tuple[str, list[dict]]:
    """按数据集分发到对应数据源拉取并归一化 → (数据源标识, 行列表)。

    P1-13 第三批: 前八类走 EM datacenter(client 参数), 资金流走新浪, 逐笔走腾讯。
    client 均支持注入(测试用), 未注入时懒建模块级单例。
    """
    if dataset == "money_flow":
        sc = sina_client or _get_sina_client()
        # 新浪要带前缀代码(sz000651), num=最近 N 个交易日
        rows = sc.money_flow_daily(f"{_infer_prefix(code)}{code}", num=MAX_ROWS)
        return "sina", _norm_money_flow(rows)
    if dataset == "tick_trade":
        tc = tx_client or _get_tx_client()
        ticks = tc.tick_trades(f"{_infer_prefix(code)}{code}")
        return "tencent", _norm_tick_trade(ticks)
    report_name, filter_col, sort_col, sort_types, normalize = _REGISTRY[dataset]
    em = client or _get_client()
    raw = em.datacenter_report(
        report_name,
        flt=f'({filter_col}="{code}")',
        page_size=MAX_ROWS,
        sort_columns=sort_col,
        sort_types=sort_types,
    )
    return "eastmoney", normalize(raw)


def _fetch(data_dir: Path, dataset: str, symbol: str, client, tx_client=None, sina_client=None) -> dict:
    """真拉一次数据源并写缓存。返回完整响应 dict(state=ok)。"""
    code = _SYMBOL_RE.match(symbol).group(1)  # 调用方已校验过 symbol
    source, rows = _fetch_rows(dataset, code, client, tx_client, sina_client)
    rows = rows[: DATASET_MAX_ROWS.get(dataset, MAX_ROWS)]
    result = {
        "state": "ok",
        "source": source,
        "dataset": dataset,
        "symbol": symbol,
        "fetched_at": time.time(),
        "stale": False,
        "count": len(rows),
        "rows": rows,
    }
    # 缓存落盘(失败不影响返回 — 缓存只是省接口, 不是数据本身)
    try:
        path = _cache_path(data_dir, dataset, code)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(result, ensure_ascii=False))
    except OSError as e:
        logger.warning("个股资料缓存写入失败 (%s/%s): %s", dataset, symbol, e)
    return result


def get_profile(
    data_dir: Path,
    dataset: str,
    symbol: str,
    force: bool = False,
    client=None,
    tx_client=None,
    sina_client=None,
) -> dict:
    """取一只标的的一类资料(缓存优先)。

    返回 dict:
      - state=ok: 正常(可能 stale=True 表示用的是过期旧缓存)
      - state=source_unavailable: 拉取失败且无缓存可用(前端降级提示)
    任何异常都不外抛 — 资料是锦上添花, 不能因为它把页面打挂。
    client/tx_client/sina_client 可注入(测试用), 分别对应 EM/腾讯/新浪。
    """
    if dataset not in DATASETS:
        raise ValueError(f"未知个股资料数据集: {dataset}")
    if not _SYMBOL_RE.match(symbol or ""):
        raise ValueError(f"symbol 格式应为 6 位代码(可带 .SH/.SZ/.BJ 后缀): {symbol}")
    code = _SYMBOL_RE.match(symbol).group(1)
    path = _cache_path(data_dir, dataset, code)
    # P1-13 第三批: TTL 按数据集分级(资料 12h / 资金流 30min / 逐笔 60s)
    ttl = DATASET_TTL_S.get(dataset, CACHE_TTL_S)

    # 1) 新鲜缓存直接返回(不 force 时)
    cached = _read_cache(path)
    if cached and not force:
        age = time.time() - float(cached.get("fetched_at") or 0)
        if age < ttl:
            return cached

    # 2) 真拉一次
    try:
        return _fetch(data_dir, dataset, symbol, client, tx_client, sina_client)
    except Exception as e:  # noqa: BLE001 — 免费源任何异常都降级, 不影响页面
        logger.warning("个股资料拉取失败 (%s/%s): %s", dataset, symbol, e)
        # 3) 有旧缓存(哪怕过期)就凑合用, 标 stale 让前端提示
        if cached:
            cached["stale"] = True
            cached["warning"] = f"数据源暂不可用, 展示的是缓存数据: {e}"
            return cached
        source = DATASET_SOURCE.get(dataset, "eastmoney")
        return {
            "state": "source_unavailable",
            "source": source,
            "dataset": dataset,
            "symbol": symbol,
            "count": 0,
            "rows": [],
            "message": f"{_SOURCE_LABELS.get(source, source)}资料接口暂不可用: {e}",
        }
