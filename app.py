"""
스쿨로그 (School-Log)
---------------------
NEIS(나이스) 교육정보 개방 포털 오픈API를 이용해
- 오늘/특정일 급식 메뉴 + 칼로리/영양정보
- 이번 달 학사일정 (시험, 방학, 행사 등)
을 한 화면에서 확인할 수 있는 학생용 대시보드.

실행:
    pip install -r requirements.txt
    streamlit run app.py

배포:
    Streamlit Community Cloud (share.streamlit.io) 에
    이 폴더를 GitHub 저장소로 올린 뒤 그대로 연결하면 무료로 배포됩니다.
"""

import calendar
from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st

NEIS_BASE = "https://open.neis.go.kr/hub"

# ---------------------------------------------------------------------------
# NEIS API 호출 함수들
# ---------------------------------------------------------------------------


def neis_request(endpoint: str, params: dict) -> list[dict]:
    """NEIS Open API 공통 호출. 결과 row들의 리스트를 돌려주고, 없으면 빈 리스트."""
    url = f"{NEIS_BASE}/{endpoint}"
    try:
        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
    except Exception as e:  # 네트워크 오류, JSON 파싱 오류 등
        st.session_state["_last_error"] = str(e)
        return []

    if endpoint not in data:
        # 인증키 오류/결과없음 등은 RESULT 코드로 내려옴
        return []

    body = data[endpoint]
    # body[0] = head, body[1] = row 데이터
    if len(body) < 2 or "row" not in body[1]:
        return []
    return body[1]["row"]


@st.cache_data(ttl=3600, show_spinner=False)
def search_school(school_name: str, api_key: str) -> pd.DataFrame:
    """학교명으로 학교 검색 -> 시도교육청코드/표준학교코드 확보"""
    params = {"Type": "json", "pSize": 30, "SCHUL_NM": school_name}
    if api_key:
        params["KEY"] = api_key
    rows = neis_request("schoolInfo", params)
    if not rows:
        return pd.DataFrame()
    cols = [
        "SCHUL_NM",
        "ATPT_OFCDC_SC_CODE",
        "ATPT_OFCDC_SC_NM",
        "SD_SCHUL_CODE",
        "ORG_RDNMA",
        "SCHUL_KND_SC_NM",
    ]
    df = pd.DataFrame(rows)
    return df[[c for c in cols if c in df.columns]]


@st.cache_data(ttl=1800, show_spinner=False)
def get_meal(atpt_code: str, schul_code: str, ymd: str, api_key: str) -> list[dict]:
    """특정 날짜(YYYYMMDD)의 급식 정보"""
    params = {
        "Type": "json",
        "pSize": 5,
        "ATPT_OFCDC_SC_CODE": atpt_code,
        "SD_SCHUL_CODE": schul_code,
        "MLSV_YMD": ymd,
    }
    if api_key:
        params["KEY"] = api_key
    return neis_request("mealServiceDietInfo", params)


@st.cache_data(ttl=3600, show_spinner=False)
def get_schedule(atpt_code: str, schul_code: str, ymd_from: str, ymd_to: str, api_key: str) -> list[dict]:
    """기간(YYYYMMDD ~ YYYYMMDD) 학사일정"""
    params = {
        "Type": "json",
        "pSize": 100,
        "ATPT_OFCDC_SC_CODE": atpt_code,
        "SD_SCHUL_CODE": schul_code,
        "AA_YMD": ymd_from,
        "AA_TO_YMD": ymd_to,
    }
    if api_key:
        params["KEY"] = api_key
    return neis_request("SchoolSchedule", params)


# ---------------------------------------------------------------------------
# 화면 구성
# ---------------------------------------------------------------------------

st.set_page_config(page_title="스쿨로그 | School-Log", page_icon="🏫", layout="wide")

st.markdown(
    """
    <style>
    .block-container {padding-top: 2rem;}
    .meal-card {background: #fff7ed; border-radius: 14px; padding: 1.2rem 1.4rem;
                border: 1px solid #fed7aa; margin-bottom: 0.8rem;}
    .nutri-pill {display:inline-block; background:#ffedd5; color:#9a3412;
                 border-radius:999px; padding:2px 10px; margin:2px; font-size:0.85rem;}
    .event-row {padding:0.5rem 0.8rem; border-left:4px solid #f97316;
                background:#fafafa; border-radius:6px; margin-bottom:6px;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🏫 스쿨로그 School-Log")
st.caption("급식 · 영양정보 · 학사일정을 한 곳에서. 여러 곳을 찾아볼 필요 없이 오늘의 학교 정보를 확인하세요.")

with st.sidebar:
    st.header("⚙️ 설정")
    api_key = st.text_input(
        "NEIS API 인증키 (선택)",
        type="password",
        help="open.neis.go.kr 에서 무료 발급. 비워두면 공공 트래픽 한도 내에서 키 없이 조회를 시도합니다.",
    )

    st.subheader("학교 검색")
    default_query = st.session_state.get("query", "송양중학교")
    query = st.text_input("학교명", value=default_query)

    if st.button("검색", use_container_width=True) or "school_df" not in st.session_state:
        st.session_state["query"] = query
        st.session_state["school_df"] = search_school(query, api_key)

    school_df = st.session_state.get("school_df", pd.DataFrame())

    if school_df.empty:
        st.warning("검색 결과가 없어요. 학교명을 다시 확인해 주세요.")
        st.stop()

    options = [
        f"{row.SCHUL_NM} ({row.ATPT_OFCDC_SC_NM} · {row.SCHUL_KND_SC_NM})"
        for row in school_df.itertuples()
    ]
    idx = st.selectbox("학교 선택", range(len(options)), format_func=lambda i: options[i])
    selected = school_df.iloc[idx]

    st.success(f"선택된 학교: {selected.SCHUL_NM}")
    st.caption(selected.get("ORG_RDNMA", ""))

atpt_code = selected["ATPT_OFCDC_SC_CODE"]
schul_code = selected["SD_SCHUL_CODE"]

tab_meal, tab_calendar = st.tabs(["🍱 오늘의 급식", "📅 학사일정 · 행사"])

# --- 급식 탭 -----------------------------------------------------------
with tab_meal:
    picked_date = st.date_input("날짜 선택", value=date.today())
    ymd = picked_date.strftime("%Y%m%d")

    meals = get_meal(atpt_code, schul_code, ymd, api_key)

    if not meals:
        st.info("해당 날짜의 급식 정보가 없어요. (주말/방학이거나 아직 등록되지 않았을 수 있어요)")
    else:
        for m in meals:
            menu_items = [x.strip() for x in m.get("DDISH_NM", "").split("<br/>") if x.strip()]
            # 알레르기 표시 번호 제거 (예: '김치찌개 5.6.9.' -> '김치찌개')
            menu_items_clean = [item.split(" (")[0] for item in menu_items]

            st.markdown(f"#### {m.get('MMEAL_SC_NM', '급식')} · {m.get('MLSV_YMD', ymd)}")
            st.markdown('<div class="meal-card">', unsafe_allow_html=True)
            for item in menu_items:
                st.markdown(f"- {item}")
            st.markdown("</div>", unsafe_allow_html=True)

            col1, col2 = st.columns([1, 2])
            with col1:
                st.metric("칼로리", m.get("CAL_INFO", "정보 없음"))
            with col2:
                nutri_items = [x.strip() for x in m.get("NTR_INFO", "").split("<br/>") if x.strip()]
                if nutri_items:
                    pills = "".join(f'<span class="nutri-pill">{n}</span>' for n in nutri_items)
                    st.markdown(pills, unsafe_allow_html=True)

# --- 학사일정 탭 --------------------------------------------------------
with tab_calendar:
    today = date.today()
    year = st.selectbox("연도", list(range(today.year - 1, today.year + 2)), index=1)
    month = st.selectbox("월", list(range(1, 13)), index=today.month - 1)

    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    events = get_schedule(
        atpt_code,
        schul_code,
        first_day.strftime("%Y%m%d"),
        last_day.strftime("%Y%m%d"),
        api_key,
    )

    if not events:
        st.info("해당 월의 학사일정 정보가 없어요.")
    else:
        events_sorted = sorted(events, key=lambda e: e.get("AA_YMD", ""))
        for e in events_sorted:
            ymd_e = e.get("AA_YMD", "")
            pretty = f"{ymd_e[:4]}.{ymd_e[4:6]}.{ymd_e[6:]}" if len(ymd_e) == 8 else ymd_e
            name = e.get("EVENT_NM", "")
            note = e.get("SBTR_DD_SC_NM", "")
            st.markdown(
                f'<div class="event-row"><b>{pretty}</b> &nbsp;|&nbsp; {name} '
                f'<span style="color:#9ca3af;">({note})</span></div>',
                unsafe_allow_html=True,
            )

st.divider()
st.caption("데이터 출처: 교육부·한국교육학술정보원 나이스 교육정보 개방 포털 (open.neis.go.kr)")
