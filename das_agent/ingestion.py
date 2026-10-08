"""파일 검사 · 열 자동 매핑 · 검증 · 정규화 (PRD 3장, 설계안 6장)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

# 표준 열 → 원본 별칭 후보 (소문자·공백제거 비교)
ALIASES: dict[str, list[str]] = {
    "date": ["실확정일자", "확정일자", "출고일", "출고일자", "일자", "날짜", "date", "ship_date", "shipdate"],
    "order_key": ["배송처차수", "배송처코드", "배송처", "주문번호", "주문키", "order_key", "orderkey", "order", "주문"],
    "sku": ["상품코드", "sku", "품목코드", "item", "itemcode", "product_code", "상품"],
    "order_line": ["order line", "orderline", "o/l", "ol", "오더라인", "주문라인", "라인", "line"],
    "pcs": ["pcs", "수량", "qty", "quantity", "출고수량", "피스"],
    "category": ["검토기준", "상품그룹", "category", "group", "구분"],
}
REQUIRED = ["date", "order_key", "sku", "order_line", "pcs"]
OPTIONAL = ["category"]
LABELS = {
    "date": "실확정일자(날짜)", "order_key": "배송처차수(주문 키)", "sku": "상품코드(SKU)",
    "order_line": "Order Line", "pcs": "PCS", "category": "검토기준(선택)",
}


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]", "", str(s)).lower()


def suggest_mapping(columns: list[str]) -> dict[str, Optional[str]]:
    """열 이름 목록에서 표준 열 매핑을 제안한다. 못 찾으면 None."""
    normed = {_norm(c): c for c in columns}
    mapping: dict[str, Optional[str]] = {}
    used: set[str] = set()
    for std, aliases in ALIASES.items():
        found = None
        for a in aliases:
            cand = normed.get(_norm(a))
            if cand is not None and cand not in used:
                found = cand
                break
        if found is None:  # 부분 일치 (예: 'Order Line 수')
            for a in aliases:
                for n, c in normed.items():
                    if c not in used and len(_norm(a)) >= 3 and _norm(a) in n:
                        found = c
                        break
                if found:
                    break
        mapping[std] = found
        if found:
            used.add(found)
    return mapping


def file_hash(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()[:16]


def list_sheets(path: str | Path) -> list[str]:
    p = Path(path)
    if p.suffix.lower() in (".csv", ".txt"):
        return ["csv"]
    from python_calamine import CalamineWorkbook
    return CalamineWorkbook.from_path(str(p)).sheet_names


def pick_sheet(sheets: list[str], preferred: str = "data") -> str:
    for s in sheets:
        if s.lower() == preferred.lower():
            return s
    return sheets[0]


def read_raw(path: str | Path, sheet: Optional[str] = None, nrows: Optional[int] = None) -> pd.DataFrame:
    """엑셀/CSV를 문자열 보존 위주로 읽는다. 주문 키·SKU 앞자리 0 유지."""
    p = Path(path)
    if p.suffix.lower() in (".csv", ".txt"):
        try:
            return pd.read_csv(p, nrows=nrows, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        except UnicodeDecodeError:
            return pd.read_csv(p, nrows=nrows, dtype=str, encoding="cp949", keep_default_na=False)
    sheets = list_sheets(p)
    sheet = sheet or pick_sheet(sheets)
    if sheet not in sheets:  # 대소문자 무시 매칭
        matches = [s for s in sheets if s.casefold() == sheet.casefold()]
        if not matches:
            raise ValueError(f"시트가 없습니다: {sheet}")
        sheet = matches[0]
    return pd.read_excel(p, sheet_name=sheet, engine="calamine", nrows=nrows, dtype=str, keep_default_na=False)


@dataclass
class Inspection:
    path: str
    file_hash: str
    sheets: list[str]
    sheet: str
    columns: list[str]
    sample: list[dict]
    mapping: dict[str, Optional[str]]
    missing_required: list[str]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def inspect_file(path: str | Path, sheet: Optional[str] = None, sample_rows: int = 5) -> Inspection:
    p = Path(path)
    sheets = list_sheets(p)
    sheet = sheet or pick_sheet(sheets)
    head = read_raw(p, sheet, nrows=sample_rows)
    sheet = next((s for s in sheets if s.casefold() == sheet.casefold()), sheet)
    cols = [str(c) for c in head.columns]
    mapping = suggest_mapping(cols)
    missing = [k for k in REQUIRED if mapping.get(k) is None]
    notes = []
    if mapping.get("order_key"):
        notes.append(f"주문 키 열로 '{mapping['order_key']}'를 제안합니다. 주문 단위(배송처차수/배송처) 의미를 확인하세요.")
    if mapping.get("category") is None:
        notes.append("검토기준 열이 없어 '미분류'로 처리합니다.")
    sample = head.astype(str).head(sample_rows).to_dict(orient="records")
    return Inspection(str(p), file_hash(p), sheets, sheet, cols, sample, mapping, missing, notes)


# ---------------------------------------------------------------- normalize
@dataclass
class Validation:
    row_count: int
    kept_rows: int
    errors: list[dict]          # 치명: {row, column, value, reason}
    warnings: list[str]
    date_min: Optional[str]
    date_max: Optional[str]
    duplicates: int

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_dates(s: pd.Series) -> pd.Series:
    """YYYYMMDD 정수/문자, ISO 문자열, Excel datetime 지원."""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.normalize()
    st = s.astype("string").str.strip()
    # 20250102 / 20250102.0
    st = st.str.replace(r"\.0$", "", regex=True)
    out = pd.to_datetime(st.where(st.str.fullmatch(r"\d{8}", na=False)), format="%Y%m%d", errors="coerce")
    rest = out.isna() & st.notna()
    if rest.any():
        allowed = st[rest].str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]00:00:00(?:\.0+)?)?$", na=False)
        out2 = pd.to_datetime(st[rest].where(allowed), errors="coerce", format="mixed")
        out = out.copy()
        out[rest] = out2
    return out


def normalize(raw: pd.DataFrame, mapping: dict[str, Optional[str]], max_errors: int = 50) -> tuple[pd.DataFrame, Validation]:
    """원본 프레임 → 표준 열 프레임 + 검증 결과. 치명 오류가 있으면 저장하면 안 된다."""
    missing = [k for k in REQUIRED if not mapping.get(k)]
    if missing:
        raise ValueError(f"필수 열 매핑이 없습니다: {[LABELS[m] for m in missing]}")
    cols = {k: v for k, v in mapping.items() if v}
    if raw.empty:
        raise ValueError("데이터 행이 없습니다.")
    if len(set(cols.values())) != len(cols):
        raise ValueError("한 열을 여러 항목에 중복 지정할 수 없습니다.")
    raw = raw.reset_index(drop=True)
    for k, v in cols.items():
        if v not in raw.columns:
            raise ValueError(f"열 '{v}'가 파일에 없습니다 ({LABELS[k]}).")

    df = pd.DataFrame(index=raw.index)
    df["src_row"] = raw.index + 2  # 엑셀 행 번호(1행 머리글)
    errors: list[dict] = []

    dates = _parse_dates(raw[cols["date"]])
    bad = dates.isna()
    for i in raw.index[bad][:max_errors]:
        errors.append({"row": int(i + 2), "column": cols["date"], "value": str(raw.at[i, cols["date"]]), "reason": "날짜 해석 불가"})
    df["date"] = dates.dt.date

    for std in ("order_key", "sku"):
        s = raw[cols[std]]
        s = s.astype("string").str.strip()
        empty = s.isna() | (s == "")
        for i in raw.index[empty][:max_errors]:
            errors.append({"row": int(i + 2), "column": cols[std], "value": "", "reason": f"{LABELS[std]} 비어 있음"})
        df[std] = s

    for std in ("order_line", "pcs"):
        n = pd.to_numeric(raw[cols[std]].astype("string").str.replace(",", "", regex=False), errors="coerce")
        bad_n = n.isna() | ~np.isfinite(n.astype(float)) | (n < 0) | ((n % 1) != 0) | (n > 10**9)
        for i in raw.index[bad_n][:max_errors]:
            errors.append({"row": int(i + 2), "column": cols[std], "value": str(raw.at[i, cols[std]]), "reason": f"{LABELS[std]}는 0 이상의 정수여야 함"})
        df[std] = n.mask(bad_n, 0).fillna(0).astype("int64")

    if cols.get("category"):
        df["category"] = raw[cols["category"]].astype("string").str.strip().fillna("미분류").replace("", "미분류")
    else:
        df["category"] = "미분류"

    warnings = []
    dup = int(df.duplicated(subset=["date", "order_key", "sku", "order_line", "pcs", "category"]).sum())
    if dup:
        warnings.append(f"완전히 같은 행 {dup}건 발견 (자동 삭제하지 않음).")
    zero_pcs = int((df["pcs"] == 0).sum())
    if zero_pcs:
        warnings.append(f"PCS가 0인 행 {zero_pcs}건.")

    valid_dates = df["date"].dropna()
    val = Validation(
        row_count=int(len(raw)), kept_rows=int(len(df)), errors=errors, warnings=warnings,
        date_min=str(valid_dates.min()) if len(valid_dates) else None,
        date_max=str(valid_dates.max()) if len(valid_dates) else None,
        duplicates=dup,
    )
    return df[["src_row", "date", "order_key", "sku", "order_line", "pcs", "category"]], val


def make_sample_file(path: str | Path, days: int = 30, seed: int = 7) -> Path:
    """출처 표시된 합성 샘플 xlsx 생성 (PRD 열 이름 그대로)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    rows = []
    start = pd.Timestamp("2025-01-02")
    skus = [f"FS3CPG{4300 + i}XBLK000" for i in range(40)]
    for d in range(days):
        day = start + pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        n_orders = int(rng.integers(20, 60))
        for _ in range(n_orders):
            site = int(rng.integers(14000, 14400))
            seq = int(rng.integers(301, 304))
            for _ in range(int(rng.integers(1, 4))):
                rows.append({
                    "실확정일자": int(day.strftime("%Y%m%d")),
                    "검토기준": "의류용품",
                    "배송처차수": f"{site} {seq}",
                    "상품코드": skus[int(rng.integers(0, len(skus)))],
                    "Order Line": 1,
                    "PCS": int(rng.integers(1, 12)),
                })
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_excel(out, sheet_name="data", index=False)
    return out


def make_golden_day_file(path: str | Path, date: str = "20250115") -> Path:
    """1일치 합성 정답 데이터 (PRD 성공 기준 2 대조용). 실제 출고 아님.

    의도적으로 넣은 경계 사례
    - 문자순 정렬: '10000 301' < '9000 301', 'A10 301' < 'A2 301'
    - 같은 배송처의 여러 차수 → 배송처 단위에서는 한 주문
    - 같은 주문·같은 SKU가 여러 행 → 합산 후 올림
    - PCS가 토트 용량(15)의 배수·배수+1 (15, 30, 31), 1
    - Order Line 2인 행
    - 셀 수 16 기준 4웨이브(16·16·16·2)
    """
    sites = ["10000", "9000", "A10", "A2", "A1", "B100", "14247", "14250", "14251", "14300",
             "15001", "15002", "15003", "15010", "15011", "15012", "15020", "15021", "15022",
             "16001", "16002", "16003", "16004", "16005", "16006", "16007", "16008", "16009",
             "17001", "17002", "17003", "17004", "17005", "17006", "17007", "17008", "17009",
             "18001", "18002", "18003", "18004", "18005", "18006", "18007", "18008", "18009",
             "19001", "19002", "19003"]
    assert len(sites) == 49
    skus = [f"GD{i:03d}SKU" for i in range(20)]
    rows: list[dict] = []

    def add(site, seq, sku, ol, pcs):
        rows.append({"실확정일자": int(date), "검토기준": "의류용품", "배송처차수": f"{site} {seq}",
                     "상품코드": sku, "Order Line": ol, "PCS": pcs})

    # 10000은 차수 2개 (배송처 단위에서 1주문), 나머지 1차수 → 주문 50개
    add("10000", 301, skus[0], 1, 15)
    add("10000", 302, skus[0], 1, 1)
    add("9000", 301, skus[0], 1, 30)
    add("A10", 301, skus[1], 2, 31)
    add("A2", 301, skus[1], 1, 14)
    add("A1", 301, skus[2], 1, 15)
    add("A1", 301, skus[2], 1, 15)      # 같은 주문·SKU 분할 행 → 합 30
    add("B100", 301, skus[3], 1, 1)
    for i, site in enumerate(sites[6:]):
        sku = skus[(i * 7) % len(skus)]
        add(site, 301, sku, 1, (i * 5) % 17 + 1)
        if i % 4 == 0:
            add(site, 301, skus[(i * 7 + 1) % len(skus)], 1, (i * 3) % 16 + 1)
        if i % 9 == 0:
            add(site, 301, sku, 1, 2)   # 분할 행
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_excel(out, sheet_name="data", index=False)
    return out
