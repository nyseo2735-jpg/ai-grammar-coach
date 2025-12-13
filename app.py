
import os, json, time, uuid, requests, difflib, html
import streamlit as st
os.environ["N8N_WEBHOOK_URL"] = "https://nyseo2735.app.n8n.cloud/webhook/grammar-report"

# 최대 추가 질문 개수
MAX_FOLLOWUP = 5

# ---------------------------
# 1) 안전한 키 로드: st.secrets -> .env -> os.environ
# ---------------------------
OPENAI_API_KEY = None
try:
    OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", None)
except Exception:
    OPENAI_API_KEY = None

if not OPENAI_API_KEY:
    try:
        from dotenv import load_dotenv, find_dotenv
        load_dotenv(find_dotenv())
    except Exception:
        pass
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

N8N_WEBHOOK_URL = None
try:
    N8N_WEBHOOK_URL = st.secrets.get("N8N_WEBHOOK_URL", None)
except Exception:
    N8N_WEBHOOK_URL = None
if not N8N_WEBHOOK_URL:
    N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")

# N8N_WEBHOOK_URL 로드 직후 보정
if N8N_WEBHOOK_URL:
    N8N_WEBHOOK_URL = N8N_WEBHOOK_URL.strip()

OPENAI_MODEL = None
try:
    OPENAI_MODEL = st.secrets.get("OPENAI_MODEL", None)
except Exception:
    OPENAI_MODEL = None
if not OPENAI_MODEL:
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")  # 필요 시 secrets에서 바꿔주세요

if not OPENAI_API_KEY:
    st.error("OPENAI_API_KEY가 설정되지 않았습니다. (Streamlit Secrets 또는 .env)")
    st.stop()


# ---------------------------
# 2) Streamlit UI
# ---------------------------
st.set_page_config(page_title="AI Grammar Coach", page_icon="📝")
st.title("AI Grammar Coach")

# 등록 상태
registered = st.session_state.get("registered", False)

with st.sidebar:
    st.header("학습자 정보")
    learner_id = st.text_input("이름 또는 ID", max_chars=50, placeholder="예: nayoung")
    phone4 = st.text_input("휴대폰 뒤 4자리", max_chars=4, placeholder="예: 1234")

    # ✅ 여기 난이도는 '문장의 수준'이 아니라 'AI 설명 난이도'
    level = st.selectbox(
        "설명 난이도 (AI 설명 수준)",
        ["초급", "중급", "고급"],
        index=1,
    )
    st.caption(
        "※ 이 난이도는 문장의 난이도가 아니라, "
        "AI가 설명·해설을 얼마나 쉽게/깊게 해 줄지 정하는 옵션입니다."
    )

    register_clicked = st.button("등록", type="primary")

    if register_clicked:
        if not learner_id.strip() or not phone4.strip():
            st.warning("이름/ID와 휴대폰 뒤 4자리를 모두 입력해주세요.")
            st.session_state["registered"] = False
        elif not phone4.isdigit() or len(phone4) != 4:
            st.warning("휴대폰 뒤 4자리는 숫자 4자리로 입력해주세요.")
            st.session_state["registered"] = False
        else:
            st.session_state["registered"] = True
            registered = True
            st.success("등록이 완료되었습니다. 이제 문장을 입력하고 분석할 수 있습니다.")

    if not st.session_state.get("registered", False):
        st.info("먼저 학습자 정보를 입력하고 [등록] 버튼을 눌러주세요.")

# 최신 등록 상태 다시 반영
registered = st.session_state.get("registered", False)

st.subheader("문장 입력")
user_sentence = st.text_area(
    "영어 문장을 입력하세요:",
    placeholder="예) She go to school yesterday.",
    height=120,
    disabled=not registered,
)

analyze = st.button("분석하기", type="primary", disabled=not registered)

# ---------------------------
# 3) 구조화 출력 스키마 (Responses API의 JSON 스키마)
# ---------------------------
schema = {
  "name": "GrammarCoachOutput",
  "schema": {
    "type": "object",
    "additionalProperties": False,
    "properties": {
      "corrected_sentence": {"type": "string"},
      "level": {"type": "string", "enum": ["beginner", "intermediate", "advanced"]},
      "explanations": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "object",
          "additionalProperties": False,
          "properties": {
            "step": {"type": "integer"},
            "focus": {"type": "string"},
            "what_is_wrong": {"type": "string"},
            "why": {"type": "string"},
            "better_alternatives": {
              "type": "array",
              "items": {"type": "string"},
              "minItems": 0
            },
            "nuance": {"type": "string"}
          },
          # 🔴 strict 모드 규칙: properties에 있는 키 전부를 required에 포함
          "required": [
            "step",
            "focus",
            "what_is_wrong",
            "why",
            "better_alternatives",
            "nuance"
          ]
        }
      },
      "quizzes": {
        "type": "array",
        "minItems": 5,
        "items": {
          "type": "object",
          "additionalProperties": False,
          "properties": {
            "id": {"type": "string"},
            "type": {"type": "string", "enum": ["mcq", "fill"]},
            "difficulty": {"type": "string", "enum": ["beginner", "intermediate", "advanced"]},
            "question": {"type": "string"},
            "options": {
              "type": "array",
              "items": {"type": "string"},
              "minItems": 0
            },
            "answer": {"type": "string"},
            "rationale": {"type": "string"}
          },
          # 🔴 여기에서도 모든 키를 required에 포함
          "required": [
            "id",
            "type",
            "difficulty",
            "question",
            "options",
            "answer",
            "rationale"
          ]
        }
      }
    },
    "required": ["corrected_sentence", "level", "explanations", "quizzes"]
  }
}

# ---------------------------
# 4) OpenAI 호출 함수
# ---------------------------
from openai import OpenAI
client = OpenAI(api_key=OPENAI_API_KEY, timeout=30)

# difflib 기반 하이라이트 함수 전체 붙여넣기
def highlight_diff(orig: str, corrected: str):
    """
    원문(orig)과 교정문(corrected)를 단어 단위 diff 방식으로 비교해
    - 원문에서 삭제/바뀐 부분: 빨간 배경 + 볼드
    - 교정문에서 새로 추가/바뀐 부분: 초록 배경 + 볼드
    로 표시한 HTML을 반환
    """
    import difflib, html

    orig_tokens = orig.split()
    corr_tokens = corrected.split()

    sm = difflib.SequenceMatcher(a=orig_tokens, b=corr_tokens)

    highlighted_orig = []
    highlighted_corr = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            highlighted_orig.extend(html.escape(w) for w in orig_tokens[i1:i2])
            highlighted_corr.extend(html.escape(w) for w in corr_tokens[j1:j2])

        elif tag == "delete":
            for w in orig_tokens[i1:i2]:
                highlighted_orig.append(
                    f"<span style='background-color:#ffe6e6; font-weight:bold;'>{html.escape(w)}</span>"
                )

        elif tag == "insert":
            for w in corr_tokens[j1:j2]:
                highlighted_corr.append(
                    f"<span style='background-color:#e6ffe6; font-weight:bold;'>{html.escape(w)}</span>"
                )

        elif tag == "replace":
            for w in orig_tokens[i1:i2]:
                highlighted_orig.append(
                    f"<span style='background-color:#ffe6e6; font-weight:bold;'>{html.escape(w)}</span>"
                )
            for w in corr_tokens[j1:j2]:
                highlighted_corr.append(
                    f"<span style='background-color:#e6ffe6; font-weight:bold;'>{html.escape(w)}</span>"
                )

    return " ".join(highlighted_orig), " ".join(highlighted_corr)

def analyze_sentence(sentence: str, explanation_level_label: str):
    """
    explanation_level_label:
      - 사용자가 사이드바에서 고른 '설명 난이도' (초급/중급/고급)
      - 문장 자체 난이도가 아니라, 설명/해설을 얼마나 쉽게/깊게 할지에 대한 옵션
    """
    level_map = {"초급": "beginner", "중급": "intermediate", "고급": "advanced"}
    explanation_level = level_map.get(explanation_level_label, "intermediate")

    # 설명 난이도에 따른 언어 설정
    if explanation_level == "beginner":
        language_instruction = """
        For beginner-level explanation:
        - All explanation fields (what_is_wrong, why, better_alternatives, nuance, rationale)
          must be written mainly in Korean.
        - Use short, simple Korean sentences that Korean adult learners can easily understand.
        - Include short English example sentences where helpful, but keep the explanation text in Korean.
        """
    else:
        language_instruction = """
        For intermediate/advanced-level explanation:
        - Explanations can be primarily in English, but you may add short Korean glosses if helpful.
        """

    system_prompt = f"""
    You are an expert English grammar tutor for Korean EFL learners.
    Return JSON strictly conforming to the provided schema.

    The learner chooses an *explanation level* (beginner / intermediate / advanced)
    that controls how simple or detailed your explanations should be.
    Explanation level parameter = {explanation_level}.

    Independently from that, you must also:
      - Estimate the difficulty of the learner's sentence itself
        (beginner / intermediate / advanced),
      - And store that judgment in the JSON field "level".
        This "level" is **your own assessment of the sentence difficulty**.

    Use the sentence difficulty in "level" as the main reference for:
      - The overall difficulty of the quizzes you generate,
      - And the "difficulty" field of each quiz item.

    {language_instruction}

    For explanations:
      - step-by-step
      - identify error spans exactly
      - explain why it's wrong
      - provide multiple better alternatives
      - include nuance (meaning/register) where relevant

    For quizzes (5–8 items):
      - mix mcq and fill
      - target the exact issues in the input
      - Base difficulty primarily on your own sentence-difficulty judgment ("level").
      - Mix quiz difficulties according to the sentence level:
        * If sentence level = beginner:
            - roughly half of the items should be beginner level
            - the remaining items should be intermediate level
        * If sentence level = intermediate:
            - the majority of items should be intermediate level
            - include some easier (beginner) and some harder (advanced) items
        * If sentence level = advanced:
            - the majority of items should be advanced level
            - include some intermediate items
      - Set each quiz item's "difficulty" field (beginner / intermediate / advanced)
        to your estimate of that quiz item's difficulty.
      - Include 1–2 transfer items (new sentences using the same rule).
    """

    user_prompt = f"""
    Learner sentence: {sentence}

    Requested explanation level (controls explanation style, NOT sentence level):
      {explanation_level}

    Task:
      1) Correct the sentence.
      2) Provide layered explanations at the requested explanation level.
      3) Generate quizzes (5-8) with immediate keys.
      4) In the JSON output field "level", write **your assessment of the difficulty
         of the learner's sentence** (beginner / intermediate / advanced).
      5) Use that sentence difficulty to set the overall difficulty of the quizzes
         and the "difficulty" field of each quiz item.

    Output must be valid JSON only.
    """

    # Chat Completions API + JSON Schema 강제
    chat = client.chat.completions.create(
        model=OPENAI_MODEL or "gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "GrammarCoachOutput",
                "schema": schema["schema"],
                "strict": True,
            },
        },
        temperature=0.2,
    )

    content = chat.choices[0].message.content
    return json.loads(content)

def answer_followup(question: str, sentence: str, corrected: str, level_label: str) -> str:
    """추가 질문에 대해 한국어로 짧게 답변."""
    level_map = {"초급": "beginner", "중급": "intermediate", "고급": "advanced"}
    lvl = level_map.get(level_label, "intermediate")

    system_prompt = """
    You are a friendly English grammar tutor for Korean adult learners.
    - Always answer in Korean.
    - Keep the explanation concise (약 3~6문장).
    - Use clear, simple Korean.
    - 필요하면 간단한 영어 예문 1~2개를 포함하세요.
    """

    user_prompt = f"""
    학습자의 영어 문장: {sentence}
    교정된 문장: {corrected}
    설정된 설명 난이도 옵션: {lvl}

    아래는 학습자의 추가 질문입니다. 한국어로 이해하기 쉽게 설명해 주세요.

    추가 질문:
    {question}
    """

    chat = client.chat.completions.create(
        model=OPENAI_MODEL or "gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
    )
    return chat.choices[0].message.content.strip()

# ---------------------------
# 5) 렌더링 & 즉시 채점
# ---------------------------
session_id = st.session_state.get("session_id") or str(uuid.uuid4())
st.session_state["session_id"] = session_id

result = st.session_state.get("result")  # 이전 분석 결과 유지

if analyze:
    if not st.session_state.get("registered", False):
        st.warning("먼저 학습자 정보를 등록해 주세요.")
    elif not user_sentence.strip():
        st.warning("문장을 입력하세요.")
    else:
        with st.spinner("분석 중..."):
            try:
                result = analyze_sentence(user_sentence, level)
                st.session_state["result"] = result
            except Exception as e:
                st.error(f"분석 중 오류: {e}")
                st.stop()

if result:
    st.divider()
    st.subheader("교정 결과")

    # AI가 판단한 문장 난이도만 표시
    ai_level_en = result.get("level", "intermediate")
    ai_level_ko_map = {
        "beginner": "초급",
        "intermediate": "중급",
        "advanced": "고급",
    }
    ai_level_ko = ai_level_ko_map.get(ai_level_en, ai_level_en)

    st.markdown(
        f"**AI가 판단한 문장 난이도:** {ai_level_ko} ({ai_level_en})"
    )

    col1, col2 = st.columns(2)

    # 하이라이트된 문장 HTML 생성
    orig_html, corr_html = highlight_diff(
        user_sentence,
        result["corrected_sentence"]
    )

    with col1:
        st.markdown("**입력 문장**")
        st.markdown(
            f"<div style='padding:0.75rem; border-radius:0.5rem; "
            f"background-color:#f8f9fa; line-height:1.6;'>{orig_html}</div>",
            unsafe_allow_html=True,
        )

    with col2:
        st.markdown("**교정 문장**")
        st.markdown(
            f"<div style='padding:0.75rem; border-radius:0.5rem; "
            f"background-color:#f0fff4; line-height:1.6;'>{corr_html}</div>",
            unsafe_allow_html=True,
        )

    st.markdown("### 단계별 설명")
    for exp in result["explanations"]:
        with st.expander(f"Step {exp['step']} · {exp['focus']}"):
            st.markdown(f"- **어디가 문제?** {exp['what_is_wrong']}")
            st.markdown(f"- **왜 틀렸나**: {exp['why']}")
            if exp.get("better_alternatives"):
                st.markdown("- **더 좋은 표현**:")
                for alt in exp["better_alternatives"]:
                    st.markdown(f"  - {alt}")
            if exp.get("nuance"):
                st.markdown(f"- **뉘앙스**: {exp['nuance']}")

    st.markdown("### 퀴즈 풀이 (즉시 채점)")

    quizzes = result.get("quizzes") if result else []
    if not quizzes:
        st.info("퀴즈가 생성되지 않았습니다.")
    else:
        # 퀴즈 난이도(문항별)를 한국어로 표시하기 위한 매핑
        quiz_level_ko_map = {
            "beginner": "초급",
            "intermediate": "중급",
            "advanced": "고급",
        }


        # 선택만으로는 rerun 발생 X, Submit 때만 처리
        with st.form("quiz_form", clear_on_submit=False):
            answers = {}
            # enumerate로 번호 생성
            for idx, q in enumerate(quizzes, start=1):
                diff_en = q.get("difficulty", "")
                diff_ko = quiz_level_ko_map.get(diff_en, diff_en)

                st.markdown(
                    f"**{idx}. [난이도: {diff_ko}] {q['question']}**"
                )
                key = f"q_{q['id']}"
                if q["type"] == "mcq" and q.get("options") is not None:
                    choice = st.radio("선택", q["options"], key=key, index=None)
                    answers[key] = choice
                else:
                    ans = st.text_input("정답 입력", key=key)
                    answers[key] = ans

            submitted = st.form_submit_button("채점하기")

        if submitted:
            score = 0
            total = len(quizzes)
            details = []

            # 채점 + 세부 정보 저장
            for idx, q in enumerate(quizzes, start=1):
                key = f"q_{q['id']}"
                user_ans = (answers.get(key) or "").strip()
                correct = (q.get("answer") or "").strip()
                is_correct = (user_ans.lower() == correct.lower())

                if is_correct:
                    score += 1

                details.append({
                    "no": idx,  # 번호 저장
                    "id": q["id"],
                    "question": q["question"],
                    "user_answer": user_ans,
                    "correct_answer": correct,
                    "is_correct": is_correct,
                    "rationale": q.get("rationale", "")
                })

            # 점수 표시
            st.success(f"점수: {score} / {total}")

            # 정답 전체 요약 표시
            st.markdown("#### 문항별 정답 확인")

            for d in details:
                icon = "✅" if d["is_correct"] else "❌"
                user_display = d["user_answer"] or "(무응답)"

                st.markdown(
                    f"**{d['no']}. {icon}**  "
                    f"(내 답: `{user_display}` / 정답: `{d['correct_answer']}`)"
                )

            # 필요 시, 자세한 해설은 접어서 보여줄 수도 있음 (선택)
            # for d in details:
            #     with st.expander(f"{d['no']}. 해설 보기"):
            #         st.markdown(f"- 질문: {d['question']}")
            #         st.markdown(f"- 내 답: `{d['user_answer'] or '(무응답)'}`")
            #         st.markdown(f"- 정답: `{d['correct_answer']}`")
            #         if d["rationale"]:
            #             st.markdown(f"- 해설: {d['rationale']}")

            # 세션에 저장
            st.session_state["last_score"] = score
            st.session_state["last_details"] = details

    # 추가 질문 섹션 (최대 MAX_FOLLOWUP개)

    st.markdown("### 추가 질문 (선택)")
    st.caption(
        f"문법 설명이나 퀴즈에 대해 더 궁금한 점이 있으면 적어 보세요. "
        f"관련된 질문 위주로 최대 {MAX_FOLLOWUP}개까지 받을 수 있습니다. "
        "AI가 한국어로 간단히 설명해 줍니다."
    )

    qa_history = st.session_state.get("qa_history") or []
    max_reached = len(qa_history) >= MAX_FOLLOWUP

    # 직전 실행에서 '입력창 초기화' 플래그가 켜져 있으면 먼저 비우고 플래그 해제
    if st.session_state.get("clear_followup_q", False):
        st.session_state["followup_q"] = ""
        st.session_state["clear_followup_q"] = False

    followup_q = st.text_area(
        "추가 질문 입력",
        key="followup_q",
        placeholder="예) 'go' 대신 'went'를 쓰는 이유를 좀 더 자세히 알고 싶어요.",
        height=80,
        disabled=max_reached,
    )

    if max_reached:
        st.info(
            f"추가 질문은 최대 {MAX_FOLLOWUP}개까지 가능합니다. "
            "이미 충분한 질문이 모였습니다. 🙂"
        )

    if st.button("질문 보내기", disabled=max_reached):
        if max_reached:
            st.info(f"더 이상 질문을 받을 수 없습니다. (최대 {MAX_FOLLOWUP}개)")
        elif not followup_q.strip():
            st.warning("질문을 입력해 주세요.")
        else:
            with st.spinner("추가 설명을 생성하고 있습니다..."):
                try:
                    answer_text = answer_followup(
                        followup_q,
                        user_sentence,
                        result["corrected_sentence"],
                        level
                    )
                    qa_item = {
                        "question": followup_q.strip(),
                        "answer": answer_text,
                        "ts": int(time.time())
                    }
                    qa_history.append(qa_item)

                    # 혹시라도 실수로 MAX_FOLLOWUP을 넘지 않도록 한 번 더 방어
                    if len(qa_history) > MAX_FOLLOWUP:
                        qa_history = qa_history[:MAX_FOLLOWUP]

                    st.session_state["qa_history"] = qa_history
                    st.session_state["clear_followup_q"] = True
                    st.success("답변이 생성되었습니다. 아래에서 확인해 주세요.")

                    # 바로 한 번 rerun 돌려서 비워진 입력창을 보여주기
                    rerun_fn = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
                    if rerun_fn:
                        rerun_fn()

                except Exception as e:
                    st.error(f"추가 질문 처리 중 오류: {e}")

    if qa_history:
        st.markdown("#### 지금까지의 질문 & 답변")
        for i, qa in enumerate(qa_history, start=1):
            label = (
                f"Q{i}. {qa['question'][:40]}"
                if len(qa["question"]) > 40
                else f"Q{i}. {qa['question']}"
            )
            with st.expander(label):
                st.markdown(f"**질문:** {qa['question']}")
                st.markdown(f"**답변:** {qa['answer']}")

    # 리포트 전송
    st.markdown("### 리포트 전송")
    st.caption("클릭 시 n8n으로 익명화된 학습 리포트가 전송/저장됩니다.")
    if st.button("리포트 보내기"):
        payload = {
            "session_id": session_id,
            "learner_id": learner_id or "anonymous",
            "phone4": phone4 or "",
            "level": level,
            "ai_level": ai_level_en,
            "input_sentence": user_sentence,
            "corrected_sentence": result["corrected_sentence"],
            "score": st.session_state.get("last_score"),
            "details": st.session_state.get("last_details"),
            "followup_qa": st.session_state.get("qa_history", []),
            "ts": int(time.time())
        }

        if not N8N_WEBHOOK_URL:
            st.error("N8N_WEBHOOK_URL이 설정되지 않아 전송할 수 없습니다.")
        else:
            try:
                r = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=30)
                if r.status_code < 300:
                    st.success("리포트가 전송되었습니다.")
                else:
                    st.error(f"리포트 전송 실패: {r.status_code} {r.text[:200]}")
            except Exception as e:
                st.error(f"리포트 전송 오류: {e}")

if st.button("🔄 새 문장 분석하기"):
    for k in ["result", "last_score", "last_details", "qa_history", "followup_q"]:
        st.session_state.pop(k, None)

    rerun_fn = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
    if rerun_fn:
        rerun_fn()
    else:
        st.toast("화면을 새로 고칠 수 없습니다. 페이지를 수동 새로고침(F5) 해주세요.")

# 하단 안내
st.caption("ⓘ 본 서비스는 OpenAI Responses API를 사용합니다.")
