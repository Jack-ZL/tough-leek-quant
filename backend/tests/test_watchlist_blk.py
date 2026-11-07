"""通达信板块文件（.blk / .ebk）导入：行形态判定、市场交叉校验、API 门禁、粘贴回退。

核心不变量是**绝不静默错译**：``.blk`` 的 7 位「市场标识 + 代码」与 ``.ebk`` 的
6 位裸码必须分别判定，任何"统一砍掉首字符"的实现都会把裸码 ``600000`` 砍成
``00000``、或把 ``000001`` 砍成 ``00001``，静默换成另一只证券。下面
``test_bare_six_digit_code_is_never_stripped`` 与
``test_market_prefixed_code_strips_exactly_one_char`` 就是锁这条的。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api.watchlist import ImportCodesRequest, import_from_blk, import_from_codes
from app.services import watchlist
from app.services.watchlist_blk import (
    _MAX_CODES,
    import_watchlist_blk,
    parse_blk_line,
    parse_blk_lines,
    suggest_group_name,
)


def _write_instruments(data_dir: Path, *, with_suffix: bool = True) -> None:
    inst = data_dir / "instruments"
    inst.mkdir(parents=True, exist_ok=True)
    symbols = ["600000.SH", "000001.SZ", "600519.SH", "515880.SH"]
    if not with_suffix:
        symbols = [s.split(".")[0] for s in symbols]
    pl.DataFrame(
        {
            "code": ["600000", "000001", "600519", "515880"],
            "symbol": symbols,
            "name": ["浦发银行", "平安银行", "贵州茅台", "通信ETF国泰"],
        }
    ).write_parquet(inst / "instruments.parquet")


def _mock_upload(*, content: bytes, filename: str, content_type: str = "application/octet-stream") -> MagicMock:
    file = MagicMock()
    file.content_type = content_type
    file.filename = filename
    # 端点是分块读取: 第一次返回全部内容, 之后返回 b"" 表示读尽
    file.read = AsyncMock(side_effect=[content, b""])
    return file


def _mock_request(data_dir: Path) -> MagicMock:
    request = MagicMock()
    request.app.state.repo.store.data_dir = data_dir
    request.app.state.repo.get_name_map = lambda _symbols: {}
    return request


# ---- 行形态判定 ----

@pytest.mark.parametrize(
    ("line", "code", "market"),
    [
        ("1600000", "600000", ".SH"),   # 沪
        ("0000001", "000001", ".SZ"),   # 深
        ("2430001", "430001", ".BJ"),   # 京
        ("600000", "600000", None),     # .ebk 裸码
        ("000001", "000001", None),
        ("sh600519", "600519", ".SH"),  # 字母前缀（同花顺/东财导出）
        ("SZ000001", "000001", ".SZ"),
    ],
)
def test_recognized_line_shapes(line: str, code: str, market: str | None):
    parsed = parse_blk_line(1, line)
    assert parsed is not None
    assert parsed.code == code
    assert parsed.market == market
    assert parsed.reason is None


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("1A0001", "6 位含字母"),      # 通达信指数代码，主数据里没有
        ("9600000", "市场标识非法"),   # 首位不是 0/1/2
        ("12345", "5 位"),
        ("12345678", "8 位"),
        ("60000", "5 位"),
        ("hello", "非数字"),
        ("6000000000", "10 位"),
    ],
)
def test_unrecognized_lines_are_rejected_with_reason(line: str, why: str):
    parsed = parse_blk_line(3, line)
    assert parsed is not None, why
    assert parsed.code is None, why
    assert parsed.reason, why
    assert parsed.lineno == 3


def test_bare_six_digit_code_is_never_stripped():
    """裸码绝不能被砍首字符: 600000 砍一位是 00000(无效), 000001 砍一位是 00001。"""
    assert parse_blk_line(1, "600000").code == "600000"
    assert parse_blk_line(1, "000001").code == "000001"


def test_market_prefixed_code_strips_exactly_one_char():
    assert parse_blk_line(1, "1600000").code == "600000"
    assert parse_blk_line(1, "0000001").code == "000001"


def test_empty_and_blank_lines_skipped():
    assert parse_blk_line(1, "") is None
    assert parse_blk_line(1, "   \t ") is None
    assert parse_blk_lines("\n\n  \n") == []


def test_line_with_trailing_name_takes_first_token():
    parsed = parse_blk_line(1, "  1600519 贵州茅台  ")
    assert parsed is not None
    assert parsed.code == "600519"
    assert parsed.market == ".SH"


def test_crlf_and_missing_trailing_newline():
    # mootdx 写盘用 "\n".join(...), 末行没有换行; Windows 通达信是 CRLF
    lines = parse_blk_lines("1600000\r\n0000001\r\n1600519")
    assert [ln.code for ln in lines] == ["600000", "000001", "600519"]
    assert [ln.lineno for ln in lines] == [1, 2, 3]


# ---- 解析 + 主数据校验 ----

def test_blk_happy_path(tmp_path: Path):
    _write_instruments(tmp_path)
    res = import_watchlist_blk("1600000\r\n0000001\r\n1600519\r\n", tmp_path, filename="我的自选.blk")
    assert res["provider"] == "blk"
    assert res["suggested_group_name"] == "我的自选"
    assert res["matched_count"] == 3
    assert res["unmatched_count"] == 0
    assert [c["symbol"] for c in res["candidates"]] == ["600000.SH", "000001.SZ", "600519.SH"]
    assert [c["name"] for c in res["candidates"]] == ["浦发银行", "平安银行", "贵州茅台"]


def test_ebk_bare_codes(tmp_path: Path):
    _write_instruments(tmp_path)
    res = import_watchlist_blk("600000\n515880\n", tmp_path, filename="block.ebk")
    assert res["matched_count"] == 2
    assert {c["symbol"] for c in res["candidates"]} == {"600000.SH", "515880.SH"}


def test_duplicate_codes_deduped_preserving_order(tmp_path: Path):
    _write_instruments(tmp_path)
    res = import_watchlist_blk("1600519\n1600000\n1600519\n", tmp_path)
    assert [c["code"] for c in res["candidates"]] == ["600519", "600000"]
    assert res["codes"] == ["600519", "600000"]


def test_market_conflict_is_reported_not_guessed(tmp_path: Path):
    """声明沪但主数据里 000001 是深 → 报冲突, 不静默给平安银行。"""
    _write_instruments(tmp_path)
    res = import_watchlist_blk("1600000\n1000001\n", tmp_path)
    by_code = {c["code"]: c for c in res["candidates"]}
    assert by_code["600000"]["matched"] is True
    assert by_code["000001"]["matched"] is False
    assert by_code["000001"]["symbol"] is None
    assert "市场标识" in by_code["000001"]["reason"]


def test_no_market_check_when_symbol_has_no_suffix(tmp_path: Path):
    """主数据 symbol 没带 .SH/.SZ 时无从校验, 不能凭空报一堆假冲突。"""
    _write_instruments(tmp_path, with_suffix=False)
    res = import_watchlist_blk("1600000\n", tmp_path)
    assert res["matched_count"] == 1
    assert res["candidates"][0]["reason"] is None


def test_unrecognized_line_becomes_visible_unmatched_candidate(tmp_path: Path):
    """认不出的行进候选并带原因, 而不是悄悄丢掉。"""
    _write_instruments(tmp_path)
    res = import_watchlist_blk("1600000\n1A0001\n", tmp_path)
    assert res["matched_count"] == 1
    assert res["unmatched_count"] == 1
    skipped = res["candidates"][1]
    assert skipped["code"] == "1A0001"
    assert skipped["matched"] is False
    assert skipped["reason"]


def test_already_in_watchlist_flag(tmp_path: Path):
    _write_instruments(tmp_path)
    res = import_watchlist_blk("1600000\n", tmp_path, existing_symbols={"600000.SH"})
    assert res["candidates"][0]["already_in_watchlist"] is True


def test_gbk_encoded_bytes_decoded(tmp_path: Path):
    _write_instruments(tmp_path)
    raw = "1600519 贵州茅台\n".encode("gb18030")
    res = import_watchlist_blk(raw, tmp_path)
    assert res["matched_count"] == 1
    assert res["candidates"][0]["name"] == "贵州茅台"


def test_utf8_bom_decoded(tmp_path: Path):
    _write_instruments(tmp_path)
    res = import_watchlist_blk("\ufeff1600000\n".encode("utf-8"), tmp_path)
    assert res["matched_count"] == 1


def test_blank_file_raises(tmp_path: Path):
    _write_instruments(tmp_path)
    with pytest.raises(ValueError, match="非空行"):
        import_watchlist_blk("  \n\n", tmp_path)


def test_all_unrecognized_raises_with_reason(tmp_path: Path):
    _write_instruments(tmp_path)
    with pytest.raises(ValueError) as ei:
        import_watchlist_blk("hello\nworld\n", tmp_path)
    assert "未识别" in str(ei.value)
    assert "首条原因" in str(ei.value)


def test_no_master_data_match_raises(tmp_path: Path):
    _write_instruments(tmp_path)
    with pytest.raises(ValueError, match="未识别"):
        import_watchlist_blk("1601234\n", tmp_path)


def test_over_max_codes_raises(tmp_path: Path):
    _write_instruments(tmp_path)
    text = "\n".join(f"16{i:05d}" for i in range(_MAX_CODES + 1))
    with pytest.raises(ValueError, match="最多"):
        import_watchlist_blk(text, tmp_path)


def test_raw_text_present_for_service_callers(tmp_path: Path):
    """服务层保留 raw_text（与 CSV/OCR 同口径），由 API 层负责剥离。"""
    _write_instruments(tmp_path)
    assert "raw_text" in import_watchlist_blk("1600000\n", tmp_path)


# ---- 建议分组名 ----

@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("我的自选.blk", "我的自选"),
        ("20251107123456.blk", "20251107123456"),
        ("block.ebk", "block"),
        (None, ""),
        ("", ""),
        ("C:\\T0002\\blocknew\\自选.blk", "自选"),
        ("/tmp/自选.blk", "自选"),
    ],
)
def test_suggest_group_name(filename: str | None, expected: str):
    assert suggest_group_name(filename) == expected


# ---- API 门禁 ----

@pytest.mark.asyncio
async def test_api_rejects_non_blk_extension(tmp_path: Path):
    request = _mock_request(tmp_path)
    file = _mock_upload(content=b"1600000\n", filename="watchlist.csv", content_type="text/csv")
    with pytest.raises(HTTPException) as ei:
        await import_from_blk(request, file)
    assert ei.value.status_code == 400
    assert ".blk" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_api_rejects_empty_file(tmp_path: Path):
    request = _mock_request(tmp_path)
    file = _mock_upload(content=b"", filename="x.blk")
    with pytest.raises(HTTPException) as ei:
        await import_from_blk(request, file)
    assert ei.value.status_code == 400
    assert "空文件" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_api_rejects_oversize(tmp_path: Path):
    request = _mock_request(tmp_path)
    file = MagicMock()
    file.filename = "x.blk"
    file.content_type = "application/octet-stream"
    file.read = AsyncMock(return_value=b"1" * (1024 * 1024))  # 每块 1MB, 永不读尽 → 必超限
    with pytest.raises(HTTPException) as ei:
        await import_from_blk(request, file)
    assert ei.value.status_code == 400
    assert "过大" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_api_success_strips_raw_text_and_suggests_group(tmp_path: Path, monkeypatch):
    _write_instruments(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    request = _mock_request(tmp_path)
    file = _mock_upload(content="1600000\n0000001\n".encode("gb18030"), filename="我的板块.blk")
    res = await import_from_blk(request, file)
    assert res["provider"] == "blk"
    assert res["suggested_group_name"] == "我的板块"
    assert res["matched_count"] == 2
    assert "raw_text" not in res


@pytest.mark.asyncio
async def test_api_suggested_group_name_keeps_original_case(tmp_path: Path, monkeypatch):
    """扩展名门禁按小写比较，但传给服务的必须是**原始**文件名。

    否则 ``MyBlock.BLK`` 会被建议成 ``myblock``。中文名不受 ``.lower()`` 影响，
    所以这条只能用拉丁名 + 大写扩展名来锁：同时覆盖"门禁大小写不敏感"和
    "建议名保留原大小写"两件事。
    """
    _write_instruments(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    request = _mock_request(tmp_path)
    file = _mock_upload(content=b"1600000\n", filename="MyBlock.BLK")
    res = await import_from_blk(request, file)
    assert res["suggested_group_name"] == "MyBlock"


@pytest.mark.asyncio
async def test_api_maps_value_error_to_400(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    request = _mock_request(tmp_path)
    file = _mock_upload(content=b"hello\n", filename="x.blk")
    with pytest.raises(HTTPException) as ei:
        await import_from_blk(request, file)
    assert ei.value.status_code == 400


# ---- 粘贴回退 ----

def test_paste_falls_back_to_blk_when_no_bare_codes(tmp_path: Path, monkeypatch):
    """粘贴整段 .blk 内容: 裸码抽取一个都抽不到 → 回退按板块口径解析。"""
    _write_instruments(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    request = _mock_request(tmp_path)
    res = import_from_codes(ImportCodesRequest(text="1600000\n0000001\n1600519"), request)
    assert res["provider"] == "blk"
    assert res["matched_count"] == 3


def test_paste_keeps_codes_path_when_bare_codes_found(tmp_path: Path, monkeypatch):
    """既有成功路径不受影响: 抽得到裸码就不走回退。"""
    _write_instruments(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    request = _mock_request(tmp_path)
    res = import_from_codes(ImportCodesRequest(text="600000\n600519"), request)
    assert res["provider"] == "codes"
    assert res["matched_count"] == 2


def test_paste_unmatched_bare_code_does_not_trigger_fallback(tmp_path: Path, monkeypatch):
    """抽到码但主数据没有 → 仍走 codes 路径（候选非空即不回退），行为与改动前一致。"""
    _write_instruments(tmp_path)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [])
    request = _mock_request(tmp_path)
    res = import_from_codes(ImportCodesRequest(text="600000\n999999"), request)
    assert res["provider"] == "codes"
    assert res["matched_count"] == 1
    assert res["unmatched_count"] == 1
