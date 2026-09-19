"""
스쿨로그 (School-Log)
---------------------
NEIS(나이스) 교육정보 개방 포털 오픈API를 이용해
- 오늘/특정일 급식 메뉴 + 칼로리/영양정보
- 이번 달 학사일정 (시험, 방학, 행사 등)
- 이번 주 시간표
를 확인하고, 수행평가 마감일과 관련 자료를 직접 등록/관리할 수 있는 학생용 대시보드.

실행:
    pip install -r requirements.txt
    streamlit run app.py
"""

import calendar
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import streamlit as st

NEIS_BASE = "https://open.neis.go.kr/hub"
DB_PATH = os.path.join(os.path.dirname(__file__), "schoolog.db")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

SCHOOL_LEVEL_TIMETABLE_API = {
    "초등학교": "elsTimetable",
    "중학교": "misTimetable",
    "고등학교": "hisTimetable",
}

WEEKDAY_NAMES = ["월", "화", "수", "목", "금"]

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
    except Exception as e:
        st.session_state["_last_error"] = str(e)
        return []

    if endpoint not in data:
        return []

    body = data[endpoint]
    if len(body) < 2 or "row" not in body[1]:
        return []
    return body[1]["row"]


@st.cache_data(ttl=3600, show_spinner=False)
def search_school(school_name: str, api_key: str) -> pd.DataFrame:
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


def school_year_semester(d: date) -> tuple[int, int]:
    """해당 날짜 기준 학년도(AY), 학기(SEM) 계산"""
    if d.month >= 3:
        return d.year, (1 if d.month <= 8 else 2)
    return d.year - 1, 2


@st.cache_data(ttl=1800, show_spinner=False)
def get_timetable_day(
    level_api: str,
    atpt_code: str,
    schul_code: str,
    grade: str,
    class_nm: str,
    ymd: str,
    api_key: str,
) -> list[dict]:
    ay, sem = school_year_semester(datetime.strptime(ymd, "%Y%m%d").date())
    params = {
        "Type": "json",
        "pSize": 20,
        "ATPT_OFCDC_SC_CODE": atpt_code,
        "SD_SCHUL_CODE": schul_code,
        "AY": ay,
        "SEM": sem,
        "GRADE": grade,
        "CLASS_NM": class_nm,
        "ALL_TI_YMD": ymd,
    }
    if api_key:
        params["KEY"] = api_key
    return neis_request(level_api, params)


def build_week_timetable(level_api, atpt_code, schul_code, grade, class_nm, monday: date, api_key) -> pd.DataFrame:
    max_period = 9
    grid = pd.DataFrame(
        index=[f"{i}교시" for i in range(1, max_period + 1)],
        columns=WEEKDAY_NAMES,
    )
    grid[:] = ""
    for i, day_name in enumerate(WEEKDAY_NAMES):
        d = monday + timedelta(days=i)
        rows = get_timetable_day(level_api, atpt_code, schul_code, grade, class_nm, d.strftime("%Y%m%d"), api_key)
        for r in rows:
            perio = r.get("PERIO", "").strip()
            subject = r.get("ITRT_CNTNT", "").strip()
            if perio and f"{perio}교시" in grid.index:
                grid.loc[f"{perio}교시", day_name] = subject
    return grid


# ---------------------------------------------------------------------------
# 수행평가 저장소 (SQLite)
# ---------------------------------------------------------------------------


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_key TEXT,
            subject TEXT,
            title TEXT,
            due_date TEXT,
            memo TEXT,
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS assessment_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assessment_id INTEGER,
            filename TEXT,
            stored_path TEXT
        )"""
    )
    return conn


def add_assessment(school_key, subject, title, due_date_str, memo, uploaded_files):
    conn = db_conn()
    cur = conn.execute(
        "INSERT INTO assessments (school_key, subject, title, due_date, memo, created_at) VALUES (?,?,?,?,?,?)",
        (school_key, subject, title, due_date_str, memo, datetime.now().isoformat()),
    )
    assessment_id = cur.lastrowid
    for f in uploaded_files or []:
        ext = os.path.splitext(f.name)[1]
        stored_name = f"{uuid.uuid4().hex}{ext}"
        stored_path = os.path.join(UPLOAD_DIR, stored_name)
        with open(stored_path, "wb") as out:
            out.write(f.getbuffer())
        conn.execute(
            "INSERT INTO assessment_files (assessment_id, filename, stored_path) VALUES (?,?,?)",
            (assessment_id, f.name, stored_path),
        )
    conn.commit()
    conn.close()


def list_assessments(school_key):
    conn = db_conn()
    rows = conn.execute(
        "SELECT id, subject, title, due_date, memo FROM assessments WHERE school_key=? ORDER BY due_date ASC",
        (school_key,),
    ).fetchall()
    result = []
    for r in rows:
        files = conn.execute(
            "SELECT filename, stored_path FROM assessment_files WHERE assessment_id=?", (r[0],)
        ).fetchall()
        result.append(
            {
                "id": r[0],
                "subject": r[1],
                "title": r[2],
                "due_date": r[3],
                "memo": r[4],
                "files": [{"filename": f[0], "path": f[1]} for f in files],
            }
        )
    conn.close()
    return result


def delete_assessment(assessment_id):
    conn = db_conn()
    files = conn.execute(
        "SELECT stored_path FROM assessment_files WHERE assessment_id=?", (assessment_id,)
    ).fetchall()
    for (path,) in files:
        if os.path.exists(path):
            os.remove(path)
    conn.execute("DELETE FROM assessment_files WHERE assessment_id=?", (assessment_id,))
    conn.execute("DELETE FROM assessments WHERE id=?", (assessment_id,))
    conn.commit()
    conn.close()


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
    .task-card {border-radius:12px; padding:1rem 1.2rem; margin-bottom:0.8rem; border:1px solid #e5e7eb;}
    .task-urgent {background:#fef2f2; border-color:#fecaca;}
    .task-soon {background:#fffbeb; border-color:#fde68a;}
    .task-normal {background:#f0fdf4; border-color:#bbf7d0;}
    .task-done {background:#f3f4f6; border-color:#e5e7eb; color:#9ca3af;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🏫 스쿨로그 School-Log")
st.caption("급식 · 영양정보 · 학사일정 · 시간표 · 수행평가를 한 곳에서.")

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

    st.subheader("우리 반 정보")
    my_grade = st.text_input("학년", value=st.session_state.get("my_grade", "1"))
    my_class = st.text_input("반", value=st.session_state.get("my_class", "1"))
    st.session_state["my_grade"] = my_grade
    st.session_state["my_class"] = my_class

atpt_code = selected["ATPT_OFCDC_SC_CODE"]
schul_code = selected["SD_SCHUL_CODE"]
school_kind = selected.get("SCHUL_KND_SC_NM", "중학교")
school_key = f"{atpt_code}_{schul_code}"
level_api = SCHOOL_LEVEL_TIMETABLE_API.get(school_kind, "misTimetable")

tab_meal, tab_timetable, tab_calendar, tab_tasks = st.tabs(
    ["🍱 오늘의 급식", "🕒 시간표", "📅 학사일정 · 행사", "📝 수행평가 관리"]
)

# --- 급식 탭 -----------------------------------------------------------
with tab_meal:
    picked_date = st.date_input("날짜 선택", value=date.today(), key="meal_date")
    ymd = picked_date.strftime("%Y%m%d")

    meals = get_meal(atpt_code, schul_code, ymd, api_key)

    if not meals:
        st.info("해당 날짜의 급식 정보가 없어요. (주말/방학이거나 아직 등록되지 않았을 수 있어요)")
    else:
        for m in meals:
            menu_items = [x.strip() for x in m.get("DDISH_NM", "").split("<br/>") if x.strip()]

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

# --- 시간표 탭 ----------------------------------------------------------
with tab_timetable:
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    week_start = st.date_input("이번 주 월요일", value=this_monday, key="week_start")
    week_start = week_start - timedelta(days=week_start.weekday())  # 안전하게 월요일로 보정

    with st.spinner("시간표를 불러오는 중이에요..."):
        grid = build_week_timetable(level_api, atpt_code, schul_code, my_grade, my_class, week_start, api_key)

    if grid.replace("", pd.NA).dropna(how="all").empty:
        st.info("해당 주의 시간표 정보가 없어요. 학년/반 정보를 다시 확인해 주세요.")
    else:
        st.dataframe(grid, use_container_width=True, height=380)
    st.caption(f"{week_start.strftime('%Y-%m-%d')} (월) ~ {(week_start + timedelta(days=4)).strftime('%Y-%m-%d')} (금) 기준")

# --- 학사일정 탭 --------------------------------------------------------
with tab_calendar:
    today = date.today()
    year = st.selectbox("연도", list(range(today.year - 1, today.year + 2)), index=1, key="cal_year")
    month = st.selectbox("월", list(range(1, 13)), index=today.month - 1, key="cal_month")

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

# --- 수행평가 관리 탭 ----------------------------------------------------
with tab_tasks:
    st.subheader("➕ 새 수행평가 등록")
    with st.form("new_assessment", clear_on_submit=True):
        c1, c2, c3 = st.columns([1, 2, 1])
        subject = c1.text_input("과목")
        title = c2.text_input("평가명 / 과제명")
        due = c3.date_input("마감일", value=date.today())
        memo = st.text_area("메모 (제출 방법, 준비물, 채점 기준 등)")
        files = st.file_uploader("관련 자료 첨부 (여러 개 선택 가능)", accept_multiple_files=True)
        submitted = st.form_submit_button("등록하기", use_container_width=True)
        if submitted:
            if not subject or not title:
                st.warning("과목과 평가명은 꼭 입력해 주세요.")
            else:
                add_assessment(school_key, subject, title, due.strftime("%Y-%m-%d"), memo, files)
                st.success("등록되었습니다!")
                st.rerun()

    st.subheader("📋 등록된 수행평가")
    items = list_assessments(school_key)

    if not items:
        st.info("아직 등록된 수행평가가 없어요.")
    else:
        today = date.today()
        for item in items:
            due_date = datetime.strptime(item["due_date"], "%Y-%m-%d").date()
            d_day = (due_date - today).days

            if d_day < 0:
                css_class, badge = "task-done", "마감됨"
            elif d_day == 0:
                css_class, badge = "task-urgent", "D-DAY"
            elif d_day <= 3:
                css_class, badge = "task-urgent", f"D-{d_day}"
            elif d_day <= 7:
                css_class, badge = "task-soon", f"D-{d_day}"
            else:
                css_class, badge = "task-normal", f"D-{d_day}"

            st.markdown(f'<div class="task-card {css_class}">', unsafe_allow_html=True)
            col_a, col_b, col_c = st.columns([5, 1, 1])
            with col_a:
                st.markdown(f"**[{item['subject']}] {item['title']}**")
                st.caption(f"마감일: {item['due_date']}")
                if item["memo"]:
                    st.write(item["memo"])
            with col_b:
                st.markdown(f"### {badge}")
            with col_c:
                if st.button("삭제", key=f"del_{item['id']}"):
                    delete_assessment(item["id"])
                    st.rerun()

            for f in item["files"]:
                if os.path.exists(f["path"]):
                    with open(f["path"], "rb") as fp:
                        st.download_button(
                            f"📎 {f['filename']}",
                            data=fp.read(),
                            file_name=f["filename"],
                            key=f"dl_{item['id']}_{f['filename']}",
                        )
            st.markdown("</div>", unsafe_allow_html=True)

st.divider()
st.caption("데이터 출처: 교육부·한국교육학술정보원 나이스 교육정보 개방 포털 (open.neis.go.kr)")
