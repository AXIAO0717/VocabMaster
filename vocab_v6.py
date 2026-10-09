# -*- coding: utf-8 -*-
"""
VocabMaster - 六级背单词工具
功能：艾宾浩斯复习 / 闪卡学习 / 打卡 / 测试 / 错词本 / 音标 / 发音 / 批量导入导出
"""

import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timedelta, date
import plotly.express as px
import plotly.graph_objects as go
import os
import random
import re

# ============================================================
# 数据库
# ============================================================
def _get_db_path():
    """尝试多个路径，找到第一个可写的"""
    candidates = []
    # 1. 脚本所在目录
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(script_dir, "vocab.db"))
    except Exception:
        pass
    # 2. 当前工作目录
    candidates.append(os.path.join(os.getcwd(), "vocab.db"))
    # 3. 用户主目录
    try:
        candidates.append(os.path.join(os.path.expanduser("~"), "vocab.db"))
    except Exception:
        pass
    # 4. 临时目录
    try:
        import tempfile
        candidates.append(os.path.join(tempfile.gettempdir(), "vocab.db"))
    except Exception:
        pass

    for path in candidates:
        try:
            db_dir = os.path.dirname(path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)
            # 测试是否可写
            test_conn = sqlite3.connect(path, check_same_thread=False)
            test_conn.close()
            return path
        except Exception:
            continue
    # 最后兜底
    return "vocab.db"

DB_PATH = _get_db_path()

def get_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception:
            pass
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def rows_to_dicts(rows):
    return [dict(r) for r in rows]

def get_columns(table):
    conn = get_db()
    c = conn.cursor()
    c.execute("PRAGMA table_info(" + table + ")")
    cols = [r[1] for r in c.fetchall()]
    conn.close()
    return cols

def add_col_if_missing(table, col, col_type, default_val=""):
    if col not in get_columns(table):
        conn = get_db()
        c = conn.cursor()
        dv = " DEFAULT '" + default_val + "'" if default_val else ""
        c.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " " + col_type + dv)
        conn.commit()
        conn.close()

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS words (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT NOT NULL,
        phonetic TEXT DEFAULT '',
        pos TEXT,
        meaning TEXT NOT NULL,
        example TEXT,
        difficulty TEXT DEFAULT 'medium',
        status TEXT DEFAULT 'new',
        review_stage INTEGER DEFAULT 0,
        next_review DATE,
        last_review DATE,
        learned_date DATE,
        correct_count INTEGER DEFAULT 0,
        wrong_count INTEGER DEFAULT 0,
        created_date DATE DEFAULT (date('now')),
        is_error INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS review_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word_id INTEGER,
        review_date DATE DEFAULT (date('now')),
        result TEXT,
        review_type TEXT,
        FOREIGN KEY (word_id) REFERENCES words(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS checkin (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        checkin_date DATE UNIQUE,
        new_words INTEGER DEFAULT 0,
        reviewed_words INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    # 造句练习记录表
    c.execute("""CREATE TABLE IF NOT EXISTS sentence_practice (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word_id INTEGER,
        user_sentence TEXT,
        ref_sentence TEXT,
        is_correct INTEGER DEFAULT 0,
        practice_date DATE DEFAULT (date('now')),
        FOREIGN KEY (word_id) REFERENCES words(id)
    )""")
    # 词汇量测试记录表
    c.execute("""CREATE TABLE IF NOT EXISTS vocab_test_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        test_date DATE DEFAULT (date('now')),
        estimated_size INTEGER,
        total_questions INTEGER,
        correct_count INTEGER,
        test_details TEXT
    )""")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_goal', '30')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('dark_mode', 'false')")
    # 修复：已学过但状态仍为 new 的单词（旧 bug 导致），改为 learning
    c.execute("UPDATE words SET status='learning', review_stage=0, next_review=? WHERE status='new' AND learned_date IS NOT NULL", (date.today().isoformat(),))
    conn.commit()
    conn.close()
    # 迁移旧表
    add_col_if_missing("words", "phonetic", "TEXT", "")
    add_col_if_missing("words", "learned_date", "DATE")
    add_col_if_missing("words", "is_error", "INTEGER", "0")
    add_col_if_missing("words", "spell_correct", "INTEGER", "0")
    add_col_if_missing("words", "spell_wrong", "INTEGER", "0")
    add_col_if_missing("words", "example_meaning", "TEXT", "")
    add_col_if_missing("words", "category", "TEXT", "cet6")

# ============================================================
# 艾宾浩斯复习间隔
# ============================================================
REVIEW_INTERVALS = [1, 2, 4, 7, 15]

def get_next_review(stage):
    if stage >= len(REVIEW_INTERVALS):
        return None
    return (date.today() + timedelta(days=REVIEW_INTERVALS[stage])).isoformat()

# ============================================================
# 设置
# ============================================================
def get_setting(key, default=""):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

# ============================================================
# 单词操作
# ============================================================
def add_word(word, pos, meaning, example="", difficulty="medium", phonetic="", example_meaning="", category="cet6"):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM words WHERE word=?", (word,))
    if c.fetchone():
        conn.close()
        return False
    nr = get_next_review(0)
    c.execute(
        "INSERT INTO words (word, phonetic, pos, meaning, example, example_meaning, category, difficulty, status, review_stage, next_review) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (word, phonetic, pos, meaning, example, example_meaning, category, difficulty, "new", 0, nr)
    )
    conn.commit()
    conn.close()
    return True

def get_new_words(limit=30):
    conn = get_db()
    c = conn.cursor()
    # 不依赖 SQLite 的 ORDER BY RANDOM()（部分环境下不可靠），改为 Python 端乱序
    c.execute("SELECT * FROM words WHERE status='new'")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return []
    rows = [dict(r) for r in rows]
    random.shuffle(rows)  # Python 端彻底打乱
    return rows[:limit]

def get_new_count():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM words WHERE status='new'")
    cnt = c.fetchone()["cnt"]
    conn.close()
    return cnt

def mark_learning(word_id):
    conn = get_db()
    c = conn.cursor()
    nr = get_next_review(0)
    today = date.today().isoformat()
    c.execute("UPDATE words SET status='learning', next_review=?, learned_date=?, last_review=? WHERE id=?",
              (nr, today, today, word_id))
    conn.commit()
    conn.close()

def mark_mastered(word_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE words SET status='mastered', review_stage=5, next_review=NULL WHERE id=?", (word_id,))
    conn.commit()
    conn.close()

def reset_to_new(word_id):
    conn = get_db()
    c = conn.cursor()
    nr = get_next_review(0)
    c.execute("UPDATE words SET status='new', review_stage=0, next_review=?, learned_date=NULL WHERE id=?",
              (nr, word_id))
    conn.commit()
    conn.close()

def get_today_learned():
    conn = get_db()
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("SELECT * FROM words WHERE learned_date=? ORDER BY id DESC", (today,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_due_reviews():
    conn = get_db()
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("SELECT * FROM words WHERE next_review <= ? AND status NOT IN ('mastered','new')", (today,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return []
    rows = [dict(r) for r in rows]
    random.shuffle(rows)
    return rows

def get_error_words():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM words WHERE is_error=1 ORDER BY wrong_count DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_words():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM words ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return rows

def review_word(word_id, correct, rtype="review"):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM words WHERE id=?", (word_id,))
    w = c.fetchone()
    if not w:
        conn.close()
        return
    stage = w["review_stage"]
    if correct:
        stage += 1
        cc = w["correct_count"] + 1
        if stage >= len(REVIEW_INTERVALS):
            status = "mastered"
            nr = None
        else:
            status = "learning"
            nr = get_next_review(stage)
    else:
        stage = 0
        cc = w["correct_count"]
        status = "learning"
        nr = get_next_review(0)
    wc = w["wrong_count"] + (0 if correct else 1)
    ie = 1 if (wc > 0 and not correct) else (0 if correct else w["is_error"])
    c.execute("UPDATE words SET review_stage=?, status=?, next_review=?, correct_count=?, wrong_count=?, is_error=?, last_review=? WHERE id=?",
              (stage, status, nr, cc, wc, ie, date.today().isoformat(), word_id))
    c.execute("INSERT INTO review_log (word_id, result, review_type) VALUES (?,?,?)",
              (word_id, "correct" if correct else "wrong", rtype))
    conn.commit()
    conn.close()

def update_difficulty(word_id, diff):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE words SET difficulty=? WHERE id=?", (diff, word_id))
    conn.commit()
    conn.close()

def delete_word(word_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM words WHERE id=?", (word_id,))
    c.execute("DELETE FROM review_log WHERE word_id=?", (word_id,))
    conn.commit()
    conn.close()

# ============================================================
# 拼写相关函数
# ============================================================
def get_spell_words(limit=20, mode="all"):
    """获取拼写练习的单词
    mode: all=所有词, review=复习到期, hard=不会的词
    """
    conn = get_db()
    c = conn.cursor()
    today = date.today().isoformat()
    if mode == "review":
        c.execute(
            "SELECT * FROM words WHERE status != 'new' AND next_review <= ?",
            (today,)
        )
    elif mode == "hard":
        c.execute(
            "SELECT * FROM words WHERE spell_wrong > 0"
        )
    else:
        c.execute("SELECT * FROM words WHERE status != 'new'")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return []
    rows = [dict(r) for r in rows]
    random.shuffle(rows)
    return rows[:limit]

def record_spell_result(word_id, correct):
    """记录拼写结果"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM words WHERE id=?", (word_id,))
    w = c.fetchone()
    if not w:
        conn.close()
        return
    sc = w["spell_correct"] + (1 if correct else 0)
    sw = w["spell_wrong"] + (0 if correct else 1)
    ie = 1 if sw > 0 else w["is_error"]
    c.execute("UPDATE words SET spell_correct=?, spell_wrong=?, is_error=? WHERE id=?",
              (sc, sw, ie, word_id))
    c.execute("INSERT INTO review_log (word_id, result, review_type) VALUES (?,?,?)",
              (word_id, "correct" if correct else "wrong", "spell"))
    conn.commit()
    conn.close()

def get_spell_stats():
    """获取拼写统计"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as total, SUM(spell_correct) as sc, SUM(spell_wrong) as sw FROM words WHERE spell_correct > 0 OR spell_wrong > 0")
    row = c.fetchone()
    conn.close()
    total = row["total"] if row else 0
    sc = row["sc"] if row and row["sc"] else 0
    sw = row["sw"] if row and row["sw"] else 0
    acc = round(sc / (sc + sw) * 100, 1) if (sc + sw) > 0 else 0
    return {"total": total, "correct": sc, "wrong": sw, "accuracy": acc}

def get_hard_spell_words(limit=50):
    """获取不会拼的词（拼错次数>0）"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM words WHERE spell_wrong > 0 ORDER BY spell_wrong DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

# ============================================================
# 打卡
# ============================================================
def check_today():
    conn = get_db()
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("SELECT * FROM checkin WHERE checkin_date=?", (today,))
    row = c.fetchone()
    conn.close()
    return row

def do_checkin(nw=0, rw=0):
    conn = get_db()
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("SELECT * FROM checkin WHERE checkin_date=?", (today,))
    ex = c.fetchone()
    if ex:
        c.execute("UPDATE checkin SET new_words=?, reviewed_words=? WHERE checkin_date=?",
                  (ex["new_words"] + nw, ex["reviewed_words"] + rw, today))
    else:
        c.execute("INSERT INTO checkin (checkin_date, new_words, reviewed_words) VALUES (?,?,?)", (today, nw, rw))
    conn.commit()
    conn.close()

def get_checkins():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM checkin ORDER BY checkin_date DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_streak():
    records = get_checkins()
    if not records:
        return 0
    dates = sorted([datetime.strptime(r["checkin_date"], "%Y-%m-%d").date() for r in records], reverse=True)
    if dates[0] != date.today():
        return 0
    streak = 1
    for i in range(1, len(dates)):
        if dates[i-1] - timedelta(days=1) == dates[i]:
            streak += 1
        else:
            break
    return streak

# ============================================================
# 统计
# ============================================================
def get_stats():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as t FROM words")
    total = c.fetchone()["t"]
    c.execute("SELECT COUNT(*) as m FROM words WHERE status='mastered'")
    mastered = c.fetchone()["m"]
    c.execute("SELECT COUNT(*) as l FROM words WHERE status='learning'")
    learning = c.fetchone()["l"]
    c.execute("SELECT COUNT(*) as n FROM words WHERE status='new'")
    new_cnt = c.fetchone()["n"]
    today = date.today().isoformat()
    c.execute("SELECT COUNT(*) as d FROM words WHERE next_review <= ? AND status NOT IN ('mastered','new')", (today,))
    due = c.fetchone()["d"]
    c.execute("SELECT COUNT(*) as e FROM words WHERE is_error=1")
    errors = c.fetchone()["e"]
    c.execute("SELECT SUM(correct_count) as cc, SUM(wrong_count) as wc FROM words")
    row = c.fetchone()
    ct = row["cc"] or 0
    wt = row["wc"] or 0
    ta = ct + wt
    acc = round(ct / ta * 100, 1) if ta > 0 else 0
    conn.close()
    return {"total": total, "mastered": mastered, "learning": learning, "new": new_cnt, "due": due, "errors": errors, "accuracy": acc, "correct": ct, "wrong": wt}

def get_history(days=30):
    conn = get_db()
    c = conn.cursor()
    sd = (date.today() - timedelta(days=days)).isoformat()
    c.execute("SELECT review_date, COUNT(*) as count, SUM(CASE WHEN result='correct' THEN 1 ELSE 0 END) as correct_count FROM review_log WHERE review_date >= ? GROUP BY review_date ORDER BY review_date", (sd,))
    rows = c.fetchall()
    conn.close()
    return rows

# ============================================================
# TTS 发音 - 不用f-string 避免反斜杠问题
# ============================================================
# TTS 发音 - 不用f-string 避免反斜杠问题
# ============================================================
def tts_speak(text):
    safe = str(text)
    safe = safe.replace(chr(92), "")
    safe = safe.replace("'", "")
    safe = safe.replace('"', "")
    safe = safe.replace("\n", " ")
    safe = safe.replace("\r", " ")
    html = (
        "<script>"
        "if ('speechSynthesis' in window) {"
        "  window.speechSynthesis.cancel();"
        "  var u = new SpeechSynthesisUtterance('" + safe + "');"
        "  u.lang = 'en-US';"
        "  u.rate = 0.9;"
        "  window.speechSynthesis.speak(u);"
        "}"
        "</script>"
    )
    st.components.v1.html(html, height=0)

def hide_word_in_sentence(word, sentence):
    """在句子中隐藏目标单词（整词匹配，避免替换派生词）
    比如 abandon 不会替换 abandoned 里的 abandon"""
    if not sentence or not word:
        return sentence
    # 用正则整词匹配，支持大小写
    pattern = r'\b' + re.escape(word) + r'\b'
    result = re.sub(pattern, "_____", sentence, flags=re.IGNORECASE)
    # 如果整词匹配没找到（可能是动词变形等），再做一次模糊匹配
    if result == sentence:
        # 尝试匹配常见的动词变形：+s, +ed, +ing
        for suffix in ['s', 'es', 'ed', 'd', 'ing']:
            pattern2 = r'\b' + re.escape(word + suffix) + r'\b'
            result = re.sub(pattern2, "_____", result, flags=re.IGNORECASE)
            if result != sentence:
                break
    return result

def _enter_to_click(input_key, btn_key):
    """注入JS：在指定输入框按回车时，自动点击指定按钮。"""
    js = (
        "<script>"
        "(function() {"
        "  function setup() {"
        "    var inputs = document.querySelectorAll('input[type=text], textarea');"
        "    for (var i = 0; i < inputs.length; i++) {"
        "      var el = inputs[i];"
        "      if (el.getAttribute('data-enter-bound') === '1') continue;"
        "      el.setAttribute('data-enter-bound', '1');"
        "      (function(el) {"
        "        el.addEventListener('keydown', function(e) {"
        "          if (e.key === 'Enter' || e.keyCode === 13) {"
        "            e.preventDefault();"
        "            e.stopPropagation();"
        "            var btn = null;"
        "            var sibs = el.closest('div[data-stale]') || el.parentElement;"
        "            var walker = el;"
        "            while (walker && walker.parentElement) {"
        "              walker = walker.parentElement;"
        "              var cands = walker.querySelectorAll('button[kind=primary], button');"
        "              if (cands.length > 0) {"
        "                for (var j = 0; j < cands.length; j++) {"
        "                  var b = cands[j];"
        "                  if (b.textContent.indexOf('提交') >= 0 || b.textContent.indexOf('下一个') >= 0) {"
        "                    btn = b; break;"
        "                  }"
        "                }"
        "                if (!btn && cands.length > 0) { btn = cands[0]; }"
        "                if (btn) break;"
        "              }"
        "            }"
        "            if (btn) { btn.click(); }"
        "          }"
        "        });"
        "      })(el);"
        "    }"
        "  }"
        "  setup();"
        "  setTimeout(setup, 500);"
        "  setTimeout(setup, 1500);"
        "})();"
        "</script>"
    )
    st.components.v1.html(js, height=0)

# ============================================================
# 页面：仪表盘
# ============================================================
def page_dashboard():
    st.header("📊 今日仪表盘")
    stats = get_stats()
    goal = int(get_setting("daily_goal", "30"))
    new_avail = get_new_count()
    today_learned = get_today_learned()
    due = get_due_reviews()
    due_cnt = len(due)
    streak = get_streak()
    checked = check_today()

    c1, c2, c3, c4 = st.columns(4)
    with c1: st.metric("总词数", stats["total"])
    with c2: st.metric("已掌握", stats["mastered"])
    with c3: st.metric("今日待复习", due_cnt)
    with c4: st.metric("连续打卡", str(streak) + "天")

    st.markdown("---")
    st.subheader("📅 今日任务")
    col1, col2 = st.columns(2)
    with col1:
        st.write("**📖 新词学习**")
        tc = len(today_learned)
        prog = min(tc / goal * 100, 100) if goal > 0 else 0
        st.progress(int(prog))
        st.caption("今日已学 " + str(tc) + " / 目标 " + str(goal) + " | 待学新词 " + str(new_avail) + " 个")
        if new_avail > 0 and tc < goal:
            if st.button("开始学习新词", type="primary", key="btn_learn_new"):
                for k in ["learn_index", "learn_results", "learn_show", "learn_words", "learn_round"]:
                    if k in st.session_state:
                        del st.session_state[k]
                st.session_state.current_page = "learn"
                st.rerun()
        elif new_avail == 0 and stats["total"] == 0:
            st.warning("词库为空，请先去词库管理导入词汇！")
            if st.button("去导入词汇"):
                st.session_state.current_page = "library"
                st.rerun()
        elif tc >= goal:
            st.success("🎉 今日新词目标已完成！")
        else:
            st.info("所有新词已学完！")
    with col2:
        st.write("**🔄 复习旧词**")
        if due_cnt > 0:
            st.progress(0)
            st.caption(str(due_cnt) + " 个待复习")
            if st.button("开始复习", type="primary", key="btn_review"):
                st.session_state.current_page = "review"
                st.rerun()
        else:
            st.progress(100)
            st.caption("全部完成！")
            if stats["total"] > 0:
                st.success("🎉 今日复习已完成！")

    st.markdown("---")
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("打卡日历")
        records = get_checkins()
        if records:
            today = date.today()
            cal_data = []
            for i in range(30):
                d = today - timedelta(days=29 - i)
                ds = d.isoformat()
                rec = next((r for r in records if r["checkin_date"] == ds), None)
                cal_data.append({
                    "日期": d.strftime("%m/%d"),
                    "打卡": 1 if rec else 0,
                    "新词": rec["new_words"] if rec else 0,
                    "复习": rec["reviewed_words"] if rec else 0
                })
            cal_df = pd.DataFrame(cal_data)
            fig = go.Figure(data=go.Heatmap(
                z=cal_df["打卡"].to_numpy().reshape(5, 6),
                x=["第" + str(i + 1) + "天" for i in range(6)],
                y=["第" + str(i * 6 + 1) + "-" + str(i * 6 + 6) + "天" for i in range(5)],
                colorscale=[[0, "#2d2d2d"], [1, "#00BFA5"]],
                showscale=False,
                text=cal_df["日期"].to_numpy().reshape(5, 6),
                texttemplate="%{text}",
                hovertemplate="日期: %{text}<br>已打卡",
            ))
            fig.update_layout(height=200, margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("还没有打卡记录，今天来第一个吧！")
    with col2:
        st.subheader("🔥 打卡")
        if checked:
            st.success("今日已打卡！")
            st.write("新词：" + str(checked["new_words"]))
            st.write("复习：" + str(checked["reviewed_words"]))
        else:
            if st.button("今日打卡", type="primary"):
                do_checkin(0, 0)
                st.success("打卡成功！连续 " + str(streak + 1) + " 天")
                st.rerun()

    st.markdown("---")
    st.subheader("📈 数据总览")
    c1, c2, c3, c4 = st.columns(4)
    with c1: st.metric("正确率", str(stats["accuracy"]) + "%")
    with c2: st.metric("错词本", str(stats["errors"]) + "个")
    with c3: st.metric("未学新词", str(stats["new"]) + "个")
    with c4: st.metric("学习中", str(stats["learning"]) + "个")

    history = get_history(30)
    if history:
        st.subheader("📊 近30天学习趋势")
        df_h = pd.DataFrame(rows_to_dicts(history))
        df_h["review_date"] = pd.to_datetime(df_h["review_date"])
        df_h["correct_rate"] = df_h["correct_count"] / df_h["count"] * 100
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df_h["review_date"], y=df_h["count"], name="复习量", marker_color="#00BFA5"))
        fig.add_trace(go.Scatter(x=df_h["review_date"], y=df_h["correct_rate"], name="正确率(%)", yaxis="y2", line=dict(color="#FF6B6B", width=2)))
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), yaxis2=dict(overlaying="y", side="right", range=[0, 105]), legend=dict(orientation="h", y=1.1))
        st.plotly_chart(fig, use_container_width=True)

# ============================================================
# 页面：学习新词
# ============================================================
def page_learn():
    st.header("📖 学习新词")
    tab1, tab2, tab3, tab4 = st.tabs(["🔤 闪卡学习", "✏️ 添加单词", "📋 今日学习", "📥 批量导入"])

    # --- 闪卡学习 ---
    with tab1:
        goal = int(get_setting("daily_goal", "30"))
        
        # 首次进入：从数据库取新词，存到 session_state
        if "learn_words" not in st.session_state or not st.session_state.learn_words:
            fresh_words = get_new_words(goal)  # 已在函数内 Python 端彻底乱序
            if fresh_words:
                word_list = [dict(w) for w in fresh_words]
                random.shuffle(word_list)  # 额外再打乱一次（三重保险）
                st.session_state.learn_words = word_list
            else:
                stats = get_stats()
                if stats["total"] == 0:
                    st.warning("词库为空！请先到「批量导入」导入词汇。")
                    if st.button("去导入词汇", type="primary"):
                        st.session_state.current_page = "library"
                        st.rerun()
                else:
                    st.success("🎉 所有新词已学完！去复习巩固吧。")
                    if st.button("去复习", type="primary"):
                        st.session_state.current_page = "review"
                        st.rerun()
                return
        
        new_words = st.session_state.learn_words
        
        if "learn_index" not in st.session_state:
            st.session_state.learn_index = 0
        if "learn_results" not in st.session_state:
            st.session_state.learn_results = []
        if "learn_show" not in st.session_state:
            st.session_state.learn_show = False

        round_num = st.session_state.get("learn_round", 1)
        if round_num > 1:
            st.info("🔄 第 " + str(round_num) + " 轮复习 - 共 " + str(len(new_words)) + " 个不认识的词")

        if st.session_state.learn_index >= len(new_words):
            # 把不认识的词存起来，避免 rerun 后丢失
            if "learn_unknown" not in st.session_state:
                st.session_state.learn_unknown = [new_words[i] for i, r in enumerate(st.session_state.learn_results) if not r]
                st.session_state.learn_known_cnt = sum(st.session_state.learn_results)
                st.session_state.learn_total_cnt = len(new_words)
                do_checkin(len(new_words), 0)
            
            unknown_words = st.session_state.learn_unknown
            known = st.session_state.learn_known_cnt
            total = st.session_state.learn_total_cnt
            
            st.success("🎉 本轮学习完成！")
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("总词数", total)
            with c2: st.metric("认识", known)
            with c3: st.metric("不认识", len(unknown_words))
            
            if unknown_words:
                st.warning("还有 " + str(len(unknown_words)) + " 个不认识的单词，继续复习！")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("🔄 复习不认识的词", type="primary"):
                        st.session_state.learn_words = unknown_words
                        random.shuffle(st.session_state.learn_words)
                        st.session_state.learn_index = 0
                        st.session_state.learn_round = st.session_state.get("learn_round", 1) + 1
                        st.session_state.learn_show = False
                        st.session_state.learn_results = []
                        if "learn_unknown" in st.session_state:
                            del st.session_state.learn_unknown
                        st.rerun()
                with c2:
                    if st.button("📝 去测试一下"):
                        for k in ["learn_index", "learn_results", "learn_show", "learn_words", "learn_round", "learn_unknown", "learn_known_cnt", "learn_total_cnt"]:
                            if k in st.session_state:
                                del st.session_state[k]
                        st.session_state.current_page = "test"
                        st.rerun()
            else:
                st.balloons()
                st.success("全部认识！太棒了！")
                if st.button("📝 去测试一下", type="primary"):
                    for k in ["learn_index", "learn_results", "learn_show", "learn_words", "learn_round", "learn_unknown", "learn_known_cnt", "learn_total_cnt"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.session_state.current_page = "test"
                    st.rerun()
            if st.button("返回仪表盘"):
                for k in ["learn_index", "learn_results", "learn_show", "learn_words", "learn_round", "learn_unknown", "learn_known_cnt", "learn_total_cnt"]:
                    if k in st.session_state:
                        del st.session_state[k]
                st.session_state.current_page = "dashboard"
                st.rerun()
            return

        cur = new_words[st.session_state.learn_index]
        prog = st.session_state.learn_index / len(new_words)
        st.progress(prog, "进度: " + str(st.session_state.learn_index + 1) + " / " + str(len(new_words)))
        st.markdown("---")

        col1, col2 = st.columns([4, 1])
        with col1:
            st.markdown("### " + cur["word"])
            if cur["phonetic"]:
                st.markdown("🔊 `" + cur["phonetic"] + "`")
            st.caption("词性: " + (cur["pos"] or "—"))
        with col2:
            if st.button("🔊 发音", key="learn_tts_" + str(cur["id"])):
                tts_speak(cur["word"])

        if not st.session_state.learn_show:
            if st.button("👀 显示意思", type="primary", use_container_width=True):
                st.session_state.learn_show = True
                st.rerun()
        else:
            st.markdown("**" + (cur["pos"] or "") + " " + cur["meaning"] + "**")
            if cur["example"]:
                st.info("📝 " + cur["example"])
                if cur["example_meaning"]:
                    st.caption("💬 " + cur["example_meaning"])
                if st.button("🔊 听例句", key="learn_ex_tts_" + str(cur["id"])):
                    tts_speak(cur["example"])
            st.markdown("---")
            st.write("**这个单词你掌握了吗？**")
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("✅ 已掌握", type="primary", key="lk_" + str(cur["id"])):
                    mark_mastered(cur["id"])
                    st.session_state.learn_results.append(True)
                    st.session_state.learn_index += 1
                    st.session_state.learn_show = False
                    st.rerun()
            with c2:
                if st.button("📖 认识", key="lk2_" + str(cur["id"])):
                    mark_learning(cur["id"])
                    st.session_state.learn_results.append(True)
                    st.session_state.learn_index += 1
                    st.session_state.learn_show = False
                    st.rerun()
            with c3:
                if st.button("❌ 不认识", key="lu_" + str(cur["id"])):
                    mark_learning(cur["id"])  # 也要标记为 learning，否则第二天无法复习
                    st.session_state.learn_results.append(False)
                    st.session_state.learn_index += 1
                    st.session_state.learn_show = False
                    st.rerun()

    # --- 添加单词 ---
    with tab2:
        with st.form("add_form"):
            c1, c2 = st.columns([2, 1])
            with c1: word = st.text_input("单词", placeholder="例如: contrive")
            with c2: pos = st.selectbox("词性", ["n.", "v.", "adj.", "adv.", "prep.", "conj.", "other"])
            phonetic = st.text_input("音标", placeholder="例如: /kənˈtraɪv/")
            meaning = st.text_input("中文意思", placeholder="例如: 设计;策划")
            example = st.text_input("例句（可选）", placeholder="例如: They contrived a plan.")
            example_meaning = st.text_input("例句翻译（可选）", placeholder="例如: 他们策划了一个计划。")
            cat = st.selectbox("分类", ["cet6", "translation", "writing"], format_func=lambda x: {"cet6": "六级词汇", "translation": "翻译词汇", "writing": "写作词汇"}[x])
            diff = st.selectbox("难度", ["easy", "medium", "hard"], format_func=lambda x: {"easy": "简单", "medium": "中等", "hard": "难"}[x])
            submitted = st.form_submit_button("添加", type="primary")
            if submitted and word:
                ok = add_word(word.strip(), pos, meaning.strip(), example.strip(), diff, phonetic.strip(), example_meaning.strip(), cat)
                if ok:
                    st.success("已添加: " + word)
                else:
                    st.warning("该单词已存在: " + word)

    # --- 今日学习列表 ---
    with tab3:
        today_learned = get_today_learned()
        if today_learned:
            st.write("今日共 " + str(len(today_learned)) + " 个单词")
            si = {"new": "🆕 未学", "learning": "📖 学习中", "mastered": "✅ 已掌握"}
            sc = {"new": "🟡", "learning": "🔵", "mastered": "🟢"}
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("🆕 未学", sum(1 for w in today_learned if w["status"] == "new"))
            with c2: st.metric("📖 学习中", sum(1 for w in today_learned if w["status"] == "learning"))
            with c3: st.metric("✅ 已掌握", sum(1 for w in today_learned if w["status"] == "mastered"))
            st.markdown("---")
            for w in today_learned:
                col1, col2, col3, col4 = st.columns([4, 2, 1, 1])
                with col1:
                    st.write("**" + w["word"] + "** " + (w["phonetic"] or ""))
                    st.caption((w["pos"] or "") + " " + w["meaning"])
                    if w["example"]:
                        st.caption("📝 " + w["example"])
                        if w["example_meaning"]:
                            st.caption("💬 " + w["example_meaning"])
                with col2:
                    st.write(sc.get(w["status"], "⚪") + " " + si.get(w["status"], w["status"]))
                with col3:
                    if st.button("🔊", key="tt_tts_" + str(w["id"])):
                        tts_speak(w["word"])
                with col4:
                    if w["status"] != "mastered":
                        if st.button("✅掌握", key="tt_m_" + str(w["id"])):
                            mark_mastered(w["id"])
                            st.rerun()
                    else:
                        if st.button("🔄重置", key="tt_r_" + str(w["id"])):
                            reset_to_new(w["id"])
                            st.rerun()
                st.divider()
        else:
            st.info("今日还没有学习记录，去闪卡学习开始吧！")

    # --- 批量导入 ---
    with tab4:
        st.write("**格式说明：** 每行一个单词，格式：`单词,音标,词性,中文意思,例句,例句翻译`")
        st.write("例：")
        st.code("abandon,/əˈbændən/,v.,放弃;抛弃,He abandoned his old car.,他废弃了他的旧车。\n"
                "absurd,/əbˈsɜːrd/,adj.,荒谬的,That is absurd.,那是荒谬的。")
        st.caption("注：音标和例句翻译可选（留空即可）。也兼容旧格式：单词,词性,中文意思,例句")
        uploaded = st.file_uploader("选择txt文件", type=["txt"])
        if uploaded is not None:
            content = uploaded.read().decode("utf-8")
            lines = content.strip().split("\n")
            cnt = 0
            skip = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",", 5)
                if len(parts) >= 6:
                    w, ph, p, m, e, em = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip(), parts[5].strip()
                elif len(parts) >= 5:
                    w, ph, p, m, e, em = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip(), ""
                elif len(parts) >= 4:
                    w, ph, p, m, e, em = parts[0].strip(), "", parts[1].strip(), parts[2].strip(), parts[3].strip(), ""
                elif len(parts) >= 3:
                    w, ph, p, m, e, em = parts[0].strip(), "", parts[1].strip(), parts[2].strip(), "", ""
                else:
                    continue
                if w and m:
                    ok = add_word(w, p, m, e, "medium", ph, em)
                    if ok:
                        cnt += 1
                    else:
                        skip += 1
            msg = "成功导入 " + str(cnt) + " 个单词！"
            if skip > 0:
                msg += "（跳过 " + str(skip) + " 个重复单词）"
            st.success(msg)

# ============================================================
# 页面：复习
# ============================================================
def page_review():
    st.header("🔄 复习单词")
    # 只在首次进入时从数据库取词并乱序，存到 session_state 避免每次 rerun 顺序变化
    if "rev_words" not in st.session_state or not st.session_state.rev_words:
        fresh = get_due_reviews()
        if not fresh:
            st.success("🎉 今日复习全部完成！没有待复习的单词了。")
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) as cnt FROM words WHERE status='mastered'")
            mastered = c.fetchone()["cnt"]
            conn.close()
            st.info("你已经掌握了 " + str(mastered) + " 个单词！")
            na = get_new_count()
            if na > 0:
                if st.button("去学习新词", type="primary"):
                    for k in ["learn_words", "learn_index", "learn_results", "learn_show", "learn_round", "learn_unknown", "learn_known_cnt", "learn_total_cnt"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.session_state.current_page = "learn"
                    st.rerun()
            return
        st.session_state.rev_words = [dict(w) for w in fresh]
        random.shuffle(st.session_state.rev_words)

    due = st.session_state.rev_words
    st.write("还有 **" + str(len(due)) + "** 个单词待复习")
    if "rev_idx" not in st.session_state:
        st.session_state.rev_idx = 0
    if "rev_results" not in st.session_state:
        st.session_state.rev_results = []
    if "rev_show" not in st.session_state:
        st.session_state.rev_show = False

    if st.session_state.rev_idx >= len(due):
        # 把没记住的词存起来，避免 rerun 后丢失
        if "rev_unknown" not in st.session_state:
            st.session_state.rev_unknown = [due[i] for i, r in enumerate(st.session_state.rev_results) if not r]
            st.session_state.rev_known_cnt = sum(st.session_state.rev_results)
            st.session_state.rev_total_cnt = len(due)
            do_checkin(0, len(due))

        unknown_words = st.session_state.rev_unknown
        known = st.session_state.rev_known_cnt
        total = st.session_state.rev_total_cnt
        rev_round = st.session_state.get("rev_round", 1)

        st.success("🎉 第 " + str(rev_round) + " 轮复习完成！")
        c1, c2, c3 = st.columns(3)
        with c1: st.metric("总词数", total)
        with c2: st.metric("记住了", known)
        with c3: st.metric("没记住", len(unknown_words))

        if unknown_words:
            st.warning("还有 " + str(len(unknown_words)) + " 个没记住的单词，再来一轮巩固！")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("🔄 复习没记住的词", type="primary"):
                    st.session_state.rev_words = unknown_words
                    random.shuffle(st.session_state.rev_words)
                    st.session_state.rev_idx = 0
                    st.session_state.rev_round = rev_round + 1
                    st.session_state.rev_show = False
                    st.session_state.rev_results = []
                    if "rev_unknown" in st.session_state:
                        del st.session_state.rev_unknown
                    st.rerun()
            with c2:
                if st.button("✅ 结束复习"):
                    for k in ["rev_idx", "rev_results", "rev_show", "rev_words", "rev_round", "rev_unknown", "rev_known_cnt", "rev_total_cnt"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.rerun()
        else:
            st.balloons()
            st.success("全部记住了！太棒了！")
            if st.button("返回仪表盘"):
                for k in ["rev_idx", "rev_results", "rev_show", "rev_words", "rev_round", "rev_unknown", "rev_known_cnt", "rev_total_cnt"]:
                    if k in st.session_state:
                        del st.session_state[k]
                st.session_state.current_page = "dashboard"
                st.rerun()
        return

    cur = due[st.session_state.rev_idx]
    prog = st.session_state.rev_idx / len(due)
    st.progress(prog, "进度: " + str(st.session_state.rev_idx + 1) + "/" + str(len(due)))
    st.markdown("---")

    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown("### " + cur["word"] + "  " + (cur["pos"] or ""))
        if cur["phonetic"]:
            st.caption("🔊 " + cur["phonetic"])
    with col2:
        if st.button("🔊 发音"):
            tts_speak(cur["word"])

    if not st.session_state.rev_show:
        if st.button("显示意思", type="primary"):
            st.session_state.rev_show = True
            st.rerun()
    else:
        st.markdown("**意思：** " + cur["meaning"])
        if cur["example"]:
            st.markdown("**例句：** " + cur["example"])
            if cur["example_meaning"]:
                st.markdown("**例句翻译：** " + cur["example_meaning"])
            if st.button("🔊 听例句"):
                tts_speak(cur["example"])
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("🟢 简单", key="d_easy"):
                update_difficulty(cur["id"], "easy")
        with c2:
            if st.button("🟡 中等", key="d_med"):
                update_difficulty(cur["id"], "medium")
        with c3:
            if st.button("🔴 困难", key="d_hard"):
                update_difficulty(cur["id"], "hard")
        st.markdown("---")
        st.write("**你记住了吗？**")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ 记住了", type="primary"):
                review_word(cur["id"], True, "review")
                st.session_state.rev_results.append(True)
                st.session_state.rev_idx += 1
                st.session_state.rev_show = False
                st.rerun()
        with c2:
            if st.button("❌ 没记住"):
                review_word(cur["id"], False, "review")
                st.session_state.rev_results.append(False)
                st.session_state.rev_idx += 1
                st.session_state.rev_show = False
                st.rerun()

# ============================================================
# 页面：测试
# ============================================================
def page_test():
    st.header("📝 单词测试")
    if "test_started" not in st.session_state or not st.session_state.test_started:
        st.subheader("选择测试范围")
        test_range = st.radio("测试范围", ["全部单词", "错词本", "待复习"])
        test_count = st.slider("题目数量", 5, 30, 10)
        test_mode = st.radio("测试方式", ["看词猜义（选择）", "看义猜词（选择）"])
        if st.button("开始测试", type="primary"):
            all_w = get_all_words()
            if test_range == "错词本":
                all_w = get_error_words()
            elif test_range == "待复习":
                all_w = get_due_reviews()
            if len(all_w) < 4:
                st.warning("单词量不足，至少需要4个单词才能测试。")
                return
            tw = random.sample(all_w, min(test_count, len(all_w)))
            st.session_state.test_words = tw
            st.session_state.test_idx = 0
            st.session_state.test_correct = 0
            st.session_state.test_answers = []
            st.session_state.test_started = True
            st.session_state.test_mode = test_mode
            st.rerun()
        return

    tw = st.session_state.test_words
    idx = st.session_state.test_idx
    if idx >= len(tw):
        correct = st.session_state.test_correct
        total = len(tw)
        acc = round(correct / total * 100, 1)
        st.success("🎉 测试完成！")
        st.metric("正确率", str(acc) + "%", str(correct) + "/" + str(total))
        st.subheader("答题详情")
        for ans in st.session_state.test_answers:
            icon = "✅" if ans["correct"] else "❌"
            st.write(icon + " " + ans["word"] + " → " + ans["your_answer"] + " (正确: " + ans["correct_answer"] + ")")
        do_checkin(0, total)
        st.session_state.test_started = False
        if st.button("再来一次"):
            st.rerun()
        return

    cur = tw[idx]
    st.progress((idx + 1) / len(tw), "第 " + str(idx + 1) + "/" + str(len(tw)) + " 题")
    all_w = get_all_words()

    if st.session_state.test_mode == "看词猜义（选择）":
        st.markdown("### " + cur["word"] + " " + (cur["pos"] or ""))
        if cur["phonetic"]:
            st.caption("🔊 " + cur["phonetic"])
        if st.button("🔊 发音"):
            tts_speak(cur["word"])
        wrong = random.sample([w for w in all_w if w["id"] != cur["id"]], min(3, len(all_w) - 1))
        opts = [cur["meaning"]] + [w["meaning"] for w in wrong]
        random.shuffle(opts)
        st.write("选择正确的意思：")
        for opt in opts:
            if st.button(opt, key="t1_" + str(idx) + "_" + opt):
                ok = (opt == cur["meaning"])
                if ok:
                    st.session_state.test_correct += 1
                review_word(cur["id"], ok, "test")
                st.session_state.test_answers.append({"word": cur["word"], "your_answer": opt, "correct_answer": cur["meaning"], "correct": ok})
                st.session_state.test_idx += 1
                st.rerun()
    else:
        st.markdown("### " + cur["meaning"])
        wrong = random.sample([w for w in all_w if w["id"] != cur["id"]], min(3, len(all_w) - 1))
        opts = [cur["word"]] + [w["word"] for w in wrong]
        random.shuffle(opts)
        st.write("选择正确的单词：")
        for opt in opts:
            if st.button(opt, key="t2_" + str(idx) + "_" + opt):
                ok = (opt == cur["word"])
                if ok:
                    st.session_state.test_correct += 1
                review_word(cur["id"], ok, "test")
                st.session_state.test_answers.append({"word": cur["meaning"], "your_answer": opt, "correct_answer": cur["word"], "correct": ok})
                st.session_state.test_idx += 1
                st.rerun()

# ============================================================
# 页面：统计
# ============================================================
def page_stats():
    st.header("📈 学习统计")
    stats = get_stats()
    c1, c2, c3, c4 = st.columns(4)
    with c1: st.metric("总词数", stats["total"])
    with c2: st.metric("已掌握", stats["mastered"])
    with c3: st.metric("正确率", str(stats["accuracy"]) + "%")
    with c4: st.metric("连续打卡", str(get_streak()) + "天")
    st.markdown("---")

    if stats["total"] > 0:
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("单词掌握情况")
            fig = px.pie(values=[stats["mastered"], stats["learning"], stats["new"]], names=["已掌握", "学习中", "未学"], color_discrete_sequence=["#00BFA5", "#FFB72D", "#FF6B6B"])
            fig.update_layout(height=300, margin=dict(l=20, r=20, t=20, b=20))
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            st.subheader("对错比例")
            fig = px.pie(values=[stats["correct"], stats["wrong"]], names=["答对", "答错"], color_discrete_sequence=["#00BFA5", "#FF6B6B"])
            fig.update_layout(height=300, margin=dict(l=20, r=20, t=20, b=20))
            st.plotly_chart(fig, use_container_width=True)

    st.subheader("📊 记忆曲线（近30天）")
    history = get_history(30)
    if history:
        df = pd.DataFrame(rows_to_dicts(history))
        df["review_date"] = pd.to_datetime(df["review_date"])
        df["accuracy"] = df["correct_count"] / df["count"] * 100
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["review_date"], y=df["accuracy"], mode="lines+markers", name="正确率(%)", line=dict(color="#00BFA5", width=3), fill="tozeroy", fillcolor="rgba(0,191,165,0.1)"))
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=20, b=20), yaxis=dict(range=[0, 105]))
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("📊 每日复习量")
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(x=df["review_date"], y=df["count"], marker_color="#00BFA5", name="复习量"))
        fig2.update_layout(height=250, margin=dict(l=20, r=20, t=20, b=20))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("还没有复习记录，开始学习后这里会显示记忆曲线。")

    st.subheader("📅 打卡历史")
    records = get_checkins()
    if records:
        df_c = pd.DataFrame(rows_to_dicts(records))
        df_c["checkin_date"] = pd.to_datetime(df_c["checkin_date"])
        df_c = df_c.sort_values("checkin_date", ascending=False).head(10)
        fig3 = go.Figure()
        fig3.add_trace(go.Bar(x=df_c["checkin_date"], y=df_c["new_words"], name="新词", marker_color="#00BFA5"))
        fig3.add_trace(go.Bar(x=df_c["checkin_date"], y=df_c["reviewed_words"], name="复习", marker_color="#FFB72D"))
        fig3.update_layout(barmode="stack", height=300, margin=dict(l=20, r=20, t=20, b=20), legend=dict(orientation="h", y=1.1))
        st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info("还没有打卡记录。")

# ============================================================
# 页面：词库管理
# ============================================================
def page_library():
    st.header("📚 词库管理")
    tab1, tab2, tab3 = st.tabs(["全部单词", "错词本", "导入/导出"])

    with tab1:
        all_w = get_all_words()
        st.write("共 " + str(len(all_w)) + " 个单词")
        if all_w:
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("🆕 未学", sum(1 for w in all_w if w["status"] == "new"))
            with c2: st.metric("📖 学习中", sum(1 for w in all_w if w["status"] == "learning"))
            with c3: st.metric("✅ 已掌握", sum(1 for w in all_w if w["status"] == "mastered"))
            st.markdown("---")
            search = st.text_input("搜索单词", placeholder="输入单词或意思...")
            si = {"new": "🆕", "learning": "📖", "mastered": "✅"}
            dl = {"easy": "🟢简单", "medium": "🟡中等", "hard": "🔴困难"}
            for w in all_w:
                if search and search.lower() not in w["word"].lower() and search not in (w["meaning"] or ""):
                    continue
                icon = si.get(w["status"], "⚪")
                with st.expander(icon + " " + w["word"] + "  " + (w["phonetic"] or "") + "  —  " + (w["pos"] or "") + " " + w["meaning"]):
                    c1, c2, c3, c4 = st.columns(4)
                    with c1: st.write("难度: " + dl.get(w["difficulty"], "🟡中等"))
                    with c2: st.write("状态: " + w["status"])
                    with c3: st.write("✅" + str(w["correct_count"]) + " ❌" + str(w["wrong_count"]))
                    with c4: st.write("复习: " + str(w["review_stage"]) + "/5")
                    if w["example"]:
                        st.write("📝 例句: " + w["example"])
                        if w["example_meaning"]:
                            st.write("💬 翻译: " + w["example_meaning"])
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        if st.button("🔊 发音", key="lib_tts_" + str(w["id"])):
                            tts_speak(w["word"])
                    with c2:
                        if st.button("删除", key="lib_del_" + str(w["id"])):
                            delete_word(w["id"])
                            st.rerun()
                    with c3:
                        if w["status"] != "mastered":
                            if st.button("✅ 标记掌握", key="lib_m_" + str(w["id"])):
                                mark_mastered(w["id"])
                                st.rerun()
                        else:
                            if st.button("🔄 重置为新词", key="lib_r_" + str(w["id"])):
                                reset_to_new(w["id"])
                                st.rerun()

    with tab2:
        ew = get_error_words()
        if ew:
            st.write("错词本: " + str(len(ew)) + " 个")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("🔄 开始复习错词", type="primary"):
                    st.session_state.rev_words = [dict(w) for w in ew]
                    random.shuffle(st.session_state.rev_words)
                    st.session_state.rev_idx = 0
                    st.session_state.rev_results = []
                    st.session_state.rev_show = False
                    st.session_state.rev_round = 1
                    st.session_state.current_page = "review"
                    st.rerun()
            with c2:
                if st.button("⌨️ 拼写练习错词"):
                    st.session_state.spell_words = [dict(w) for w in ew]
                    random.shuffle(st.session_state.spell_words)
                    st.session_state.spell_index = 0
                    st.session_state.spell_results = []
                    st.session_state.spell_input = ""
                    st.session_state.spell_checked = False
                    for k in ["spell_wrong_saved", "spell_correct_cnt", "spell_total_cnt"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.session_state.current_page = "spelling"
                    st.rerun()
            st.markdown("---")
            for w in ew:
                col1, col2 = st.columns([4, 1])
                with col1:
                    st.write("**" + w["word"] + "** " + (w["phonetic"] or "") + " " + (w["pos"] or "") + " " + w["meaning"])
                    st.caption("❌ 错误 " + str(w["wrong_count"]) + " 次 | ✅ 正确 " + str(w["correct_count"]) + " 次")
                with col2:
                    if st.button("🔊", key="err_tts_" + str(w["id"])):
                        tts_speak(w["word"])
                st.divider()
        else:
            st.success("🎉 错词本为空，太棒了！")

    with tab3:
        st.subheader("批量导入")
        st.write("格式: `单词,音标,词性,中文意思,例句`（每行一个，音标可选留空）")
        st.code("abandon,/əˈbændən/,v.,放弃;抛弃,He abandoned his old car.\n"
                "absurd,/əbˈsɜːrd/,adj.,荒谬的,That is absurd.")
        uploaded = st.file_uploader("选择txt文件", type=["txt"], key="import_lib")
        if uploaded:
            content = uploaded.read().decode("utf-8")
            lines = content.strip().split("\n")
            cnt = 0
            skip = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",", 4)
                if len(parts) >= 5:
                    w, ph, p, m, e = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip()
                elif len(parts) >= 4:
                    w, ph, p, m, e = parts[0].strip(), "", parts[1].strip(), parts[2].strip(), parts[3].strip()
                elif len(parts) >= 3:
                    w, ph, p, m, e = parts[0].strip(), "", parts[1].strip(), parts[2].strip(), ""
                else:
                    continue
                if w and m:
                    ok = add_word(w, p, m, e, "medium", ph)
                    if ok:
                        cnt += 1
                    else:
                        skip += 1
            msg = "导入 " + str(cnt) + " 个单词！"
            if skip > 0:
                msg += "（跳过 " + str(skip) + " 个重复）"
            st.success(msg)

        st.subheader("导出词库")
        if st.button("导出为txt"):
            all_w = get_all_words()
            lines = []
            for w in all_w:
                lines.append(w["word"] + "," + (w["phonetic"] or "") + "," + (w["pos"] or "") + "," + w["meaning"] + "," + (w["example"] or "") + "," + (w["example_meaning"] or ""))
            content = "\n".join(lines)
            st.download_button("下载词库文件", content, file_name="vocab_export_" + date.today().isoformat() + ".txt", mime="text/plain")

# ============================================================
# 页面：单词拼写
# ============================================================
def page_spelling():
    st.header("⌨️ 单词拼写")
    
    sstats = get_spell_stats()
    c1, c2, c3, c4 = st.columns(4)
    with c1: st.metric("拼写练习词数", sstats["total"])
    with c2: st.metric("拼对", sstats["correct"])
    with c3: st.metric("拼错", sstats["wrong"])
    with c4: st.metric("拼写正确率", str(sstats["accuracy"]) + "%")
    st.markdown("---")
    
    tab1, tab2, tab3 = st.tabs(["⌨️ 拼写练习", "🔄 拼写复习", "❌ 不会的词"])
    
    # ===== Tab1: 拼写练习 =====
    with tab1:
        if "spell_words" not in st.session_state or not st.session_state.spell_words:
            all_w = get_spell_words(limit=20, mode="all")
            if not all_w:
                st.warning("词库中没有已学单词，请先去学习新词！")
                if st.button("去学习新词", type="primary"):
                    for k in ["learn_words", "learn_index", "learn_results", "learn_show", "learn_round", "learn_unknown", "learn_known_cnt", "learn_total_cnt"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.session_state.current_page = "learn"
                    st.rerun()
                return
            st.session_state.spell_words = [dict(w) for w in all_w]
            random.shuffle(st.session_state.spell_words)
            st.session_state.spell_index = 0
            st.session_state.spell_results = []
            st.session_state.spell_input = ""
            st.session_state.spell_checked = False
            st.session_state.spell_last_correct = None
        
        words = st.session_state.spell_words
        
        if st.session_state.spell_index >= len(words):
            correct = sum(st.session_state.spell_results)
            wrong = len(words) - correct
            wrong_words = [words[i] for i, r in enumerate(st.session_state.spell_results) if not r]
            
            if "spell_wrong_saved" not in st.session_state:
                st.session_state.spell_wrong_saved = wrong_words
                st.session_state.spell_correct_cnt = correct
                st.session_state.spell_total_cnt = len(words)
                do_checkin(0, len(words))
            
            st.success("🎉 本轮拼写完成！")
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("总词数", st.session_state.spell_total_cnt)
            with c2: st.metric("拼对", st.session_state.spell_correct_cnt)
            with c3: st.metric("拼错", len(st.session_state.spell_wrong_saved))
            
            if st.session_state.spell_wrong_saved:
                st.warning("还有 " + str(len(st.session_state.spell_wrong_saved)) + " 个拼错的单词")
                st.write("**拼错的词：**")
                for w in st.session_state.spell_wrong_saved:
                    st.write("- " + w["word"] + " " + (w["phonetic"] or "") + " " + w["meaning"])
                
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("🔄 再拼一次错的", type="primary"):
                        st.session_state.spell_words = st.session_state.spell_wrong_saved
                        random.shuffle(st.session_state.spell_words)
                        st.session_state.spell_index = 0
                        st.session_state.spell_results = []
                        st.session_state.spell_input = ""
                        st.session_state.spell_checked = False
                        if "spell_wrong_saved" in st.session_state:
                            del st.session_state.spell_wrong_saved
                        st.rerun()
                with c2:
                    if st.button("📋 加入不会的词"):
                        for w in st.session_state.spell_wrong_saved:
                            record_spell_result(w["id"], False)
                        st.success("已记录到「不会的词」模块！")
                        for k in ["spell_words", "spell_index", "spell_results", "spell_input", "spell_checked", "spell_wrong_saved", "spell_correct_cnt", "spell_total_cnt"]:
                            if k in st.session_state:
                                del st.session_state[k]
                        st.rerun()
            else:
                st.balloons()
                st.success("全部拼对！太棒了！")
            
            if st.button("再来一轮"):
                for k in ["spell_words", "spell_index", "spell_results", "spell_input", "spell_checked", "spell_wrong_saved", "spell_correct_cnt", "spell_total_cnt"]:
                    if k in st.session_state:
                        del st.session_state[k]
                st.rerun()
            if st.button("返回仪表盘"):
                for k in ["spell_words", "spell_index", "spell_results", "spell_input", "spell_checked", "spell_wrong_saved", "spell_correct_cnt", "spell_total_cnt"]:
                    if k in st.session_state:
                        del st.session_state[k]
                st.session_state.current_page = "dashboard"
                st.rerun()
            return
        
        cur = words[st.session_state.spell_index]
        prog = st.session_state.spell_index / len(words)
        st.progress(prog, "进度: " + str(st.session_state.spell_index + 1) + " / " + str(len(words)))
        st.markdown("---")
        
        # 显示提示信息：音标+词性+中文意思
        st.write("**根据以下提示拼写单词：**")
        if cur["phonetic"]:
            st.markdown("🔊 音标: `" + cur["phonetic"] + "`")
        st.write("**词性:** " + (cur["pos"] or "—"))
        st.write("**中文意思:** " + cur["meaning"])
        if cur["example"]:
            # 例句中把单词隐藏，用 ___ 代替（整词匹配）
            example_hidden = hide_word_in_sentence(cur["word"], cur["example"])
            st.info("📝 例句: " + example_hidden)
            if cur["example_meaning"]:
                st.caption("💬 " + cur["example_meaning"])
        
        col1, col2 = st.columns([4, 1])
        with col1:
            if not st.session_state.spell_checked:
                # 未提交：用普通 text_input + button + JS 监听回车
                user_input = st.text_input(
                    "输入单词拼写 (按回车提交):",
                    key="spell_input_" + str(cur["id"]),
                    placeholder="在这里输入..."
                )
                submit_btn = st.button("✅ 提交拼写", type="primary", use_container_width=True,
                                       key="spell_submit_" + str(cur["id"]))
                if submit_btn:
                    val = (user_input or "").strip()
                    if val:
                        answer = val.lower()
                        correct_word = cur["word"].strip().lower()
                        is_correct = (answer == correct_word)
                        st.session_state.spell_results.append(is_correct)
                        st.session_state.spell_input = val
                        st.session_state.spell_checked = True
                        st.session_state.spell_last_correct = is_correct
                        record_spell_result(cur["id"], is_correct)
                        st.rerun()
                # JS：回车自动点击提交按钮
                _enter_to_click("spell_input_" + str(cur["id"]),
                                "spell_submit_" + str(cur["id"]))
            else:
                # 已提交：显示结果
                is_correct = st.session_state.spell_last_correct
                if is_correct:
                    st.success("✅ 拼对了！" + cur["word"])
                else:
                    st.error("❌ 拼错了！")
                    st.write("你的答案: " + st.session_state.spell_input)
                    st.write("正确答案: **" + cur["word"] + "**")
                    if cur["phonetic"]:
                        st.write("音标: " + cur["phonetic"])
                    if cur["example"]:
                        st.info("📝 " + cur["example"])
                        if cur["example_meaning"]:
                            st.caption("💬 " + cur["example_meaning"])
                        if st.button("🔊 听例句", key="spell_ex_tts_" + str(cur["id"])):
                            tts_speak(cur["example"])
                
                # 下一个：用普通 text_input + button + JS 监听回车
                st.text_input(
                    "按回车继续下一个 👇",
                    key="spell_next_input_" + str(cur["id"]),
                    placeholder="按回车键继续...",
                    label_visibility="collapsed"
                )
                next_clicked = st.button("➡️ 下一个", type="primary", use_container_width=True,
                                         key="spell_next_btn_" + str(cur["id"]))
                if next_clicked:
                    st.session_state.spell_index += 1
                    st.session_state.spell_input = ""
                    st.session_state.spell_checked = False
                    st.rerun()
                _enter_to_click("spell_next_input_" + str(cur["id"]),
                                "spell_next_btn_" + str(cur["id"]))
        with col2:
            st.write("")
            st.write("")
            if st.button("🔊 听发音", key="spell_tts_" + str(cur["id"])):
                tts_speak(cur["word"])
    
    # ===== Tab2: 拼写复习 =====
    with tab2:
        st.write("**复习到期的单词 - 拼写模式**")
        due_words = get_spell_words(limit=20, mode="review")
        
        if not due_words:
            st.success("🎉 没有待复习的单词！")
            return
        
        st.info("共有 " + str(len(due_words)) + " 个待复习单词")
        
        if "spell_review_words" not in st.session_state or not st.session_state.spell_review_words:
            st.session_state.spell_review_words = [dict(w) for w in due_words]
            random.shuffle(st.session_state.spell_review_words)
            st.session_state.spell_review_index = 0
            st.session_state.spell_review_results = []
            st.session_state.spell_review_input = ""
            st.session_state.spell_review_checked = False
            st.session_state.spell_review_last = None
        
        rwords = st.session_state.spell_review_words
        
        if st.session_state.spell_review_index >= len(rwords):
            # 把拼错的词存起来
            if "spell_rev_unknown" not in st.session_state:
                st.session_state.spell_rev_unknown = [rwords[i] for i, r in enumerate(st.session_state.spell_review_results) if not r]
                st.session_state.spell_rev_correct_cnt = sum(st.session_state.spell_review_results)
                st.session_state.spell_rev_total_cnt = len(rwords)
                do_checkin(0, len(rwords))

            unknown_r = st.session_state.spell_rev_unknown
            rc = st.session_state.spell_rev_correct_cnt
            rw_total = st.session_state.spell_rev_total_cnt
            rev_round = st.session_state.get("spell_rev_round", 1)

            st.success("🎉 第 " + str(rev_round) + " 轮拼写复习完成！")
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("总词数", rw_total)
            with c2: st.metric("拼对", rc)
            with c3: st.metric("拼错", len(unknown_r))

            if unknown_r:
                st.warning("还有 " + str(len(unknown_r)) + " 个拼错的单词，再来一轮巩固！")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("🔄 再拼一次错的", type="primary"):
                        st.session_state.spell_review_words = unknown_r
                        random.shuffle(st.session_state.spell_review_words)
                        st.session_state.spell_review_index = 0
                        st.session_state.spell_rev_round = rev_round + 1
                        st.session_state.spell_review_results = []
                        st.session_state.spell_review_input = ""
                        st.session_state.spell_review_checked = False
                        st.session_state.spell_review_last = None
                        if "spell_rev_unknown" in st.session_state:
                            del st.session_state.spell_rev_unknown
                        st.rerun()
                with c2:
                    if st.button("✅ 结束复习"):
                        for k in ["spell_review_words", "spell_review_index", "spell_review_results",
                                  "spell_review_input", "spell_review_checked", "spell_review_last",
                                  "spell_rev_round", "spell_rev_unknown", "spell_rev_correct_cnt",
                                  "spell_rev_total_cnt"]:
                            if k in st.session_state:
                                del st.session_state[k]
                        st.rerun()
            else:
                st.balloons()
                st.success("全部拼对了！太棒了！")
                if st.button("返回拼写练习"):
                    for k in ["spell_review_words", "spell_review_index", "spell_review_results",
                              "spell_review_input", "spell_review_checked", "spell_review_last",
                              "spell_rev_round", "spell_rev_unknown", "spell_rev_correct_cnt",
                              "spell_rev_total_cnt"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.rerun()
            return
        
        rcur = rwords[st.session_state.spell_review_index]
        rprog = st.session_state.spell_review_index / len(rwords)
        st.progress(rprog, "复习进度: " + str(st.session_state.spell_review_index + 1) + " / " + str(len(rwords)))
        st.markdown("---")
        
        if rcur["phonetic"]:
            st.markdown("🔊 音标: `" + rcur["phonetic"] + "`")
        st.write("**词性:** " + (rcur["pos"] or "—"))
        st.write("**中文意思:** " + rcur["meaning"])
        if rcur["example"]:
            example_hidden = hide_word_in_sentence(rcur["word"], rcur["example"])
            st.info("📝 例句: " + example_hidden)
            if rcur["example_meaning"]:
                st.caption("💬 " + rcur["example_meaning"])
        
        st.session_state["_spell_rev_cur_word"] = rcur["word"]
        st.session_state["_spell_rev_cur_id"] = rcur["id"]
        
        if not st.session_state.spell_review_checked:
            # 未提交：用普通 text_input + button + JS 监听回车
            r_input = st.text_input(
                "输入拼写 (按回车提交):",
                key="spell_rev_input_" + str(rcur["id"]),
                placeholder="在这里输入..."
            )
            rev_submitted = st.button("✅ 提交", type="primary", use_container_width=True,
                                      key="spell_rev_submit_" + str(rcur["id"]))
            if rev_submitted:
                val = (r_input or "").strip()
                if val:
                    answer = val.lower()
                    correct_word = rcur["word"].strip().lower()
                    is_c = (answer == correct_word)
                    st.session_state.spell_review_results.append(is_c)
                    st.session_state.spell_review_input = val
                    st.session_state.spell_review_checked = True
                    st.session_state.spell_review_last = is_c
                    record_spell_result(rcur["id"], is_c)
                    review_word(rcur["id"], is_c, "spell_review")
                    st.rerun()
            _enter_to_click("spell_rev_input_" + str(rcur["id"]),
                            "spell_rev_submit_" + str(rcur["id"]))
        else:
            # 已提交：显示结果
            is_c = st.session_state.spell_review_last
            if is_c:
                st.success("✅ 拼对了！" + rcur["word"])
            else:
                st.error("❌ 拼错了！")
                st.write("你的答案: " + st.session_state.spell_review_input)
                st.write("正确答案: **" + rcur["word"] + "**")
                if rcur["example"]:
                    st.info("📝 " + rcur["example"])
                    if rcur["example_meaning"]:
                        st.caption("💬 " + rcur["example_meaning"])
            
            # 下一个：用普通 text_input + button + JS 监听回车
            st.text_input(
                "按回车继续下一个 👇",
                key="spell_rev_next_" + str(rcur["id"]),
                placeholder="按回车键继续...",
                label_visibility="collapsed"
            )
            rev_next_clicked = st.button("➡️ 下一个", type="primary", use_container_width=True,
                                         key="spell_rev_next_btn_" + str(rcur["id"]))
            if rev_next_clicked:
                st.session_state.spell_review_index += 1
                st.session_state.spell_review_input = ""
                st.session_state.spell_review_checked = False
                st.rerun()
            _enter_to_click("spell_rev_next_" + str(rcur["id"]),
                            "spell_rev_next_btn_" + str(rcur["id"]))
    
    # ===== Tab3: 不会的词 =====
    with tab3:
        st.write("**不会拼的单词 - 集中练习**")
        hard_words = get_hard_spell_words(limit=50)
        
        if not hard_words:
            st.success("🎉 还没有不会拼的单词！去练习吧。")
            return
        
        st.warning("共有 " + str(len(hard_words)) + " 个不会拼的单词")
        
        c1, c2 = st.columns(2)
        with c1:
            st.metric("不会拼的词", len(hard_words))
        with c2:
            total_wrong = sum(w["spell_wrong"] for w in hard_words)
            st.metric("总拼错次数", total_wrong)
        
        st.markdown("---")
        
        if st.button("🔥 开始集中练习不会的词", type="primary"):
            st.session_state.spell_words = [dict(w) for w in hard_words]
            random.shuffle(st.session_state.spell_words)
            st.session_state.spell_index = 0
            st.session_state.spell_results = []
            st.session_state.spell_input = ""
            st.session_state.spell_checked = False
            for k in ["spell_wrong_saved", "spell_correct_cnt", "spell_total_cnt"]:
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()
        
        st.markdown("---")
        st.write("**不会拼的单词列表：**")
        for w in hard_words:
            col1, col2, col3 = st.columns([4, 1, 1])
            with col1:
                st.write("**" + w["word"] + "** " + (w["phonetic"] or "") + " " + (w["pos"] or "") + " " + w["meaning"])
                st.caption("❌ 拼错 " + str(w["spell_wrong"]) + " 次 | ✅ 拼对 " + str(w["spell_correct"]) + " 次")
            with col2:
                if st.button("🔊", key="hard_tts_" + str(w["id"])):
                    tts_speak(w["word"])
            with col3:
                if st.button("✅ 已掌握", key="hard_ok_" + str(w["id"])):
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("UPDATE words SET spell_wrong=0, is_error=0 WHERE id=?", (w["id"],))
                    conn.commit()
                    conn.close()
                    st.success("已移出不会的词！")
                    st.rerun()

# ============================================================
# 翻译词汇数据（内置）
# ============================================================
TRANSLATION_VOCAB = [
    ("interpretation", "/ɪnˌtɜːrprɪˈteɪʃn/", "n.", "口译;解释", "His interpretation of the contract was wrong.", "他对合同的解读是错误的。"),
    ("localization", "/ˌloʊkələˈzeɪʃn/", "n.", "本土化;本地化", "The game needs localization for the Chinese market.", "这款游戏需要针对中国市场进行本土化。"),
    ("adaptation", "/ˌædæpˈteɪʃn/", "n.", "改编;适应", "The film is an adaptation of a novel.", "这部电影改编自一部小说。"),
    ("equivalence", "/ɪˈkwɪvələns/", "n.", "对等;等值", "Translation seeks dynamic equivalence between languages.", "翻译追求语言间的动态对等。"),
    ("terminology", "/ˌtɜːrmɪˈnɑːlədʒi/", "n.", "术语;术语学", "Medical terminology is difficult to translate.", "医学术语很难翻译。"),
    ("transcription", "/trænˈskrɪpʃn/", "n.", "转写;转录", "Phonetic transcription helps pronunciation.", "语音转写有助于发音。"),
    ("dubbing", "/ˈdʌbɪŋ/", "n.", "配音", "The dubbing of this movie is excellent.", "这部电影的配音非常出色。"),
    ("subtitle", "/ˈsʌbtaɪtl/", "n.", "字幕", "I prefer watching movies with subtitles.", "我更喜欢看带字幕的电影。"),
    ("bilingual", "/ˌbaɪˈlɪŋɡwəl/", "adj.", "双语的", "She is bilingual in English and Chinese.", "她通晓中英双语。"),
    ("semantic", "/sɪˈmæntɪk/", "adj.", "语义的", "Semantic differences can cause translation errors.", "语义差异会导致翻译错误。"),
    ("idiomatic", "/ˌɪdiəˈmætɪk/", "adj.", "地道的;符合习惯用法的", "His English is very idiomatic.", "他的英语非常地道。"),
    ("connotation", "/ˌkɑːnəˈteɪʃn/", "n.", "内涵;隐含意义", "The word has a negative connotation.", "这个词有负面内涵。"),
    ("denotation", "/ˌdiːnoʊˈteɪʃn/", "n.", "字面意义;外延", "The denotation of 'snake' is a reptile.", "'snake'的字面意义是一种爬行动物。"),
    ("register", "/ˈredʒɪstər/", "n.", "语域;语体", "Formal register is required in academic writing.", "学术写作需要正式语体。"),
    ("calque", "/kælk/", "n.", "仿译;借译", "'Brainwash' is a calque from Chinese.", "'Brainwash'是中文的仿译词。"),
    ("transcreation", "/ˌtrænskriˈeɪʃn/", "n.", "创译", "Transcreation goes beyond literal translation.", "创译超越了字面翻译。"),
    ("source text", "/sɔːrs tekst/", "n.", "原文;源文本", "The translator must understand the source text fully.", "译者必须完全理解原文。"),
    ("target text", "/ˈtɑːrɡɪt tekst/", "n.", "译文;目标文本", "The target text should read naturally.", "译文应该读起来自然。"),
    ("literal translation", "/ˈlɪtərəl trænsˈleɪʃn/", "n.", "直译", "A literal translation may lose the original flavor.", "直译可能丢失原文韵味。"),
    ("free translation", "/friː trænsˈleɪʃn/", "n.", "意译", "Sometimes free translation is more appropriate.", "有时意译更为合适。"),
    ("domestication", "/dəˌmestɪˈkeɪʃn/", "n.", "归化", "Domestication adapts foreign texts to the target culture.", "归化将外来文本适应到目标文化。"),
    ("foreignization", "/ˌfɔːrənəˈzeɪʃn/", "n.", "异化", "Foreignization preserves the foreignness of the source text.", "异化保留了原文的异域色彩。"),
    ("back-translation", "/bæk trænsˈleɪʃn/", "n.", "回译", "Back-translation is used to verify translation quality.", "回译用于验证翻译质量。"),
    ("compensation", "/ˌkɑːmpenˈseɪʃn/", "n.", "补偿", "Compensation deals with untranslatable elements.", "补偿用于处理不可译的元素。"),
    ("explicitation", "/ˌeksplɪsɪˈteɪʃn/", "n.", "显化", "Explicitation makes implicit information explicit.", "显化将隐含信息明确表达出来。"),
    ("coherence", "/koʊˈhɪrəns/", "n.", "连贯性", "A good translation maintains coherence.", "好的翻译保持连贯性。"),
    ("cohesion", "/koʊˈhiːʒn/", "n.", "衔接", "Cohesion is achieved through connectors.", "衔接通过连接词实现。"),
    ("pragmatic", "/præɡˈmætɪk/", "adj.", "语用的", "Pragmatic equivalence is essential in translation.", "语用对等在翻译中至关重要。"),
    ("nuance", "/ˈnjuːɑːns/", "n.", "细微差别", "Translation must capture every nuance.", "翻译必须捕捉每一个细微差别。"),
    ("collocation", "/ˌkɑːləˈkeɪʃn/", "n.", "搭配", "Collocation errors are common in translation.", "搭配错误在翻译中很常见。"),
]

# ============================================================
# 写作模板数据（内置）
# ============================================================
WRITING_TEMPLATES = {
    "议论文模板": [
        ("开头段", "Nowadays, there is a growing concern over ___. Some people argue that ___, while others believe that ___. From my perspective, ___."),
        ("主体段1", "To begin with, ___. For one thing, ___. For another, ___. Therefore, ___."),
        ("主体段2", "Furthermore, ___. According to a recent survey, ___. This indicates that ___."),
        ("让步段", "Admittedly, ___. However, this does not mean that ___. On the contrary, ___."),
        ("结尾段", "In conclusion, ___. Only by doing so can we ___. It is high time that we ___."),
    ],
    "图表作文模板": [
        ("开头段", "As is vividly depicted in the chart, ___. The chart clearly shows that ___."),
        ("趋势描述", "From the chart, we can see that ___ experienced a dramatic increase/decrease from ___ to ___. Meanwhile, ___ remained stable at ___."),
        ("原因分析", "Several factors contribute to this phenomenon. Firstly, ___. Secondly, ___. Last but not least, ___."),
        ("预测与建议", "Based on the trend, it is reasonable to predict that ___. Therefore, we should ___."),
    ],
    "应用文模板（建议信）": [
        ("开头", "Dear ___, I am writing to express my views on ___. I would like to offer some suggestions."),
        ("主体", "First of all, I suggest that ___. In addition, it would be advisable to ___. Moreover, ___."),
        ("结尾", "I hope you will take my suggestions into consideration. I am looking forward to your reply. Yours sincerely, ___"),
    ],
}

WRITING_TRANSITIONS = {
    "递进": ["furthermore", "moreover", "in addition", "what is more", "besides", "apart from that", "additionally", "not only... but also"],
    "转折": ["however", "nevertheless", "on the contrary", "in contrast", "whereas", "while", "on the other hand", "despite", "in spite of"],
    "因果": ["therefore", "consequently", "as a result", "thus", "hence", "due to", "owing to", "on account of", "as a consequence"],
    "举例": ["for instance", "for example", "such as", "namely", "to illustrate", "a case in point is", "take... for example"],
    "总结": ["in conclusion", "to sum up", "in summary", "all in all", "in a word", "to summarize", "in short", "on the whole"],
    "顺序": ["first of all", "to begin with", "firstly", "secondly", "last but not least", "finally", "in the first place", "above all"],
    "条件": ["provided that", "on condition that", "as long as", "unless", "given that", "assuming that", "suppose"],
    "强调": ["it is... that...", "what...", "do/does/did + 动词原形", "above all", "most importantly", "without doubt"],
}

WRITING_HIGH_FREQ_PHRASES = [
    ("play a vital role in", "在...中起至关重要的作用"),
    ("be attributed to", "归因于"),
    ("give priority to", "优先考虑"),
    ("lay a solid foundation for", "为...打下坚实基础"),
    ("pose a threat to", "对...构成威胁"),
    ("arouse wide public concern", "引起公众广泛关注"),
    ("be aware of the importance of", "意识到...的重要性"),
    ("make every effort to", "尽一切努力去"),
    ("put forward constructive suggestions", "提出建设性建议"),
    ("adapt oneself to the development of", "使自己适应...的发展"),
    ("broaden one's horizons", "拓宽视野"),
    ("enhance the awareness of", "增强...的意识"),
    ("stem from", "源于"),
    ("be exposed to", "接触到;暴露于"),
    ("be immune to", "对...免疫;不受...影响"),
    ("be indispensable to", "对...不可或缺"),
    ("cater to the needs of", "满足...的需求"),
    ("strike a balance between", "在...之间取得平衡"),
    ("bear in mind that", "牢记"),
    ("it is universally acknowledged that", "众所周知"),
]

# ============================================================
# 造句练习辅助函数
# ============================================================
def get_sentence_practice_words(limit=10):
    """获取有例句的已学单词用于造句练习"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM words WHERE example != '' AND example IS NOT NULL AND status != 'new'")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return []
    rows = [dict(r) for r in rows]
    random.shuffle(rows)
    return rows[:limit]

def save_sentence_attempt(word_id, user_sentence, ref_sentence, is_correct):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO sentence_practice (word_id, user_sentence, ref_sentence, is_correct) VALUES (?,?,?,?)",
        (word_id, user_sentence, ref_sentence, 1 if is_correct else 0)
    )
    conn.commit()
    conn.close()

def get_sentence_stats():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as total, SUM(is_correct) as correct FROM sentence_practice")
    row = c.fetchone()
    conn.close()
    total = row["total"] if row else 0
    correct = row["correct"] if row and row["correct"] else 0
    return {"total": total, "correct": correct, "accuracy": round(correct / total * 100, 1) if total > 0 else 0}

# ============================================================
# 词汇量测试辅助函数
# ============================================================
# 词汇难度分级（8级，按词长+难度字段综合划分）
VOCAB_BANDS = [
    {"name": "L1 入门", "max_len": 4, "diff": "easy", "vocab_base": 1000, "desc": "最基础词汇，相当于初中入门"},
    {"name": "L2 基础", "max_len": 5, "diff": "easy", "vocab_base": 2000, "desc": "初中核心词汇"},
    {"name": "L3 初中", "max_len": 6, "diff": "easy", "vocab_base": 3000, "desc": "初中毕业水平"},
    {"name": "L4 高中", "max_len": 7, "diff": "medium", "vocab_base": 4000, "desc": "高中毕业水平"},
    {"name": "L5 四级", "max_len": 8, "diff": "medium", "vocab_base": 5000, "desc": "大学四级水平"},
    {"name": "L6 六级", "max_len": 9, "diff": "medium", "vocab_base": 6000, "desc": "大学六级核心"},
    {"name": "L7 六级+", "max_len": 11, "diff": "hard", "vocab_base": 7500, "desc": "六级高难 + 考研"},
    {"name": "L8 高阶", "max_len": 99, "diff": "hard", "vocab_base": 10000, "desc": "雅思/GRE 水平"},
]

BAND_QUESTIONS = 10  # 每个等级 10 道题

def get_vocab_test_words(band_idx, limit=10):
    """按难度分带获取测试词汇"""
    band = VOCAB_BANDS[band_idx]
    conn = get_db()
    c = conn.cursor()
    # 综合词长和难度字段筛选
    if band_idx <= 2:
        # 低级：短词 + easy难度
        c.execute(
            "SELECT * FROM words WHERE length(word) <= ? AND difficulty = ?",
            (band["max_len"], band["diff"])
        )
    elif band_idx <= 5:
        # 中级：中等词长
        c.execute(
            "SELECT * FROM words WHERE length(word) <= ? AND length(word) > ?",
            (band["max_len"], VOCAB_BANDS[band_idx-1]["max_len"])
        )
    else:
        # 高级：长词 + hard难度
        c.execute(
            "SELECT * FROM words WHERE length(word) > ? AND difficulty = ?",
            (VOCAB_BANDS[band_idx-1]["max_len"], band["diff"])
        )
    rows = c.fetchall()
    conn.close()
    if not rows:
        # 降级查询，确保每个band都有词
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM words WHERE length(word) <= ?", (band["max_len"],))
        rows = c.fetchall()
        conn.close()
    if not rows:
        return []
    rows = [dict(r) for r in rows]
    random.shuffle(rows)
    return rows[:limit]

def get_vocab_distractors(word, band_idx, count=3):
    """生成干扰项：从同难度等级选其他词的意思"""
    band = VOCAB_BANDS[band_idx]
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT meaning FROM words WHERE word != ? AND length(word) <= ? ORDER BY RANDOM() LIMIT ?",
        (word, band["max_len"], count + 5)
    )
    rows = c.fetchall()
    conn.close()
    meanings = [r["meaning"] for r in rows if r["meaning"] and r["meaning"].strip()]
    random.shuffle(meanings)
    return meanings[:count]

def save_vocab_test_result(estimated, total_q, correct, details=""):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO vocab_test_log (estimated_size, total_questions, correct_count, test_details) VALUES (?,?,?,?)",
        (estimated, total_q, correct, details)
    )
    conn.commit()
    conn.close()

def get_vocab_test_history():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM vocab_test_log ORDER BY test_date DESC, id DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    return rows

# ============================================================
# 页面：造句练习
# ============================================================
def page_sentence():
    st.header("✍️ 造句练习")

    sstats = get_sentence_stats()
    c1, c2, c3 = st.columns(3)
    with c1: st.metric("练习次数", sstats["total"])
    with c2: st.metric("造对", sstats["correct"])
    with c3: st.metric("正确率", str(sstats["accuracy"]) + "%")
    st.markdown("---")

    tab1, tab2 = st.tabs(["✍️ 造句练习", "📊 练习记录"])

    with tab1:
        # 每次取一个词
        if "sent_words" not in st.session_state or not st.session_state.sent_words:
            words = get_sentence_practice_words(limit=20)
            if not words:
                st.warning("还没有有例句的已学单词！请先学习一些单词。")
                if st.button("去学习新词", type="primary"):
                    for k in ["learn_words", "learn_index", "learn_results", "learn_show", "learn_round", "learn_unknown", "learn_known_cnt", "learn_total_cnt"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.session_state.current_page = "learn"
                    st.rerun()
                return
            st.session_state.sent_words = [dict(w) for w in words]
            random.shuffle(st.session_state.sent_words)
            st.session_state.sent_index = 0
            st.session_state.sent_results = []
            st.session_state.sent_show_ref = False
            st.session_state.sent_input = ""

        words = st.session_state.sent_words

        if st.session_state.sent_index >= len(words):
            correct = sum(st.session_state.sent_results)
            total = len(st.session_state.sent_results)
            st.success("🎉 造句练习完成！")
            c1, c2 = st.columns(2)
            with c1: st.metric("总题数", total)
            with c2: st.metric("自评通过", correct)
            st.balloons()
            for k in ["sent_words", "sent_index", "sent_results", "sent_show_ref", "sent_input"]:
                if k in st.session_state:
                    del st.session_state[k]
            if st.button("再来一轮"):
                st.rerun()
            return

        cur = words[st.session_state.sent_index]
        prog = st.session_state.sent_index / len(words)
        st.progress(prog, "进度: " + str(st.session_state.sent_index + 1) + " / " + str(len(words)))
        st.markdown("---")

        col1, col2 = st.columns([4, 1])
        with col1:
            st.markdown("### " + cur["word"])
            if cur["phonetic"]:
                st.markdown("🔊 `" + cur["phonetic"] + "`")
            st.caption("词性: " + (cur["pos"] or "—") + "  |  意思: " + cur["meaning"])
        with col2:
            if st.button("🔊 发音", key="sent_tts_" + str(cur["id"])):
                tts_speak(cur["word"])

        st.write("**请用「" + cur["word"] + "」造一个句子：**")
        user_sent = st.text_area(
            "你的造句：",
            value=st.session_state.get("sent_input", ""),
            height=80,
            placeholder="在这里输入你的句子...",
            key="sent_text_" + str(cur["id"])
        )

        if not st.session_state.sent_show_ref:
            if st.button("✅ 提交并查看参考例句", type="primary", use_container_width=True):
                st.session_state.sent_input = user_sent
                st.session_state.sent_show_ref = True
                st.rerun()
        else:
            st.markdown("---")
            st.write("**你的造句：**")
            if st.session_state.sent_input.strip():
                st.info(st.session_state.sent_input)
            else:
                st.warning("你没有输入句子！")

            st.write("**参考例句：**")
            st.info("📝 " + (cur["example"] or "无例句"))
            if cur.get("example_meaning"):
                st.caption("💬 " + cur["example_meaning"])

            st.markdown("---")
            st.write("**请自评你的造句：**")
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("✅ 造得好", type="primary", key="sent_ok_" + str(cur["id"])):
                    save_sentence_attempt(cur["id"], st.session_state.sent_input, cur["example"] or "", True)
                    st.session_state.sent_results.append(True)
                    st.session_state.sent_index += 1
                    st.session_state.sent_show_ref = False
                    st.session_state.sent_input = ""
                    st.rerun()
            with c2:
                if st.button("🤔 一般般", key="sent_mid_" + str(cur["id"])):
                    save_sentence_attempt(cur["id"], st.session_state.sent_input, cur["example"] or "", True)
                    st.session_state.sent_results.append(True)
                    st.session_state.sent_index += 1
                    st.session_state.sent_show_ref = False
                    st.session_state.sent_input = ""
                    st.rerun()
            with c3:
                if st.button("❌ 不太好", key="sent_bad_" + str(cur["id"])):
                    save_sentence_attempt(cur["id"], st.session_state.sent_input, cur["example"] or "", False)
                    st.session_state.sent_results.append(False)
                    st.session_state.sent_index += 1
                    st.session_state.sent_show_ref = False
                    st.session_state.sent_input = ""
                    st.rerun()

            if st.button("⏭️ 跳过", key="sent_skip_" + str(cur["id"])):
                st.session_state.sent_index += 1
                st.session_state.sent_show_ref = False
                st.session_state.sent_input = ""
                st.rerun()

    with tab2:
        st.subheader("📜 练习记录")
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT sp.*, w.word FROM sentence_practice sp
            LEFT JOIN words w ON sp.word_id = w.id
            ORDER BY sp.practice_date DESC, sp.id DESC LIMIT 20
        """)
        records = c.fetchall()
        conn.close()
        if records:
            for r in records:
                icon = "✅" if r["is_correct"] else "❌"
                st.write(icon + " **" + (r["word"] or "未知") + "** - " + r["practice_date"])
                st.caption("你的句子: " + (r["user_sentence"] or "（空）"))
                st.caption("参考例句: " + (r["ref_sentence"] or "无"))
                st.divider()
        else:
            st.info("还没有练习记录，去造句吧！")

# ============================================================
# 页面：翻译专属词汇
# ============================================================
def page_translation():
    st.header("🌐 翻译专属词汇")
    st.caption("翻译学习必备的专业术语和核心词汇")

    # 统计翻译词汇数量
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM words WHERE category='translation'")
    db_count = c.fetchone()["cnt"]
    conn.close()

    c1, c2 = st.columns(2)
    with c1: st.metric("已导入翻译词汇", db_count)
    with c2: st.metric("内置翻译词汇", len(TRANSLATION_VOCAB))
    st.markdown("---")

    tab1, tab2, tab3 = st.tabs(["📚 词汇浏览", "📥 导入翻译词汇", "✍️ 翻译练习"])

    with tab1:
        st.subheader("翻译核心术语")
        for item in TRANSLATION_VOCAB:
            word, phonetic, pos, meaning, example, ex_meaning = item
            with st.expander("📖 " + word + " " + phonetic + "  " + pos + "  " + meaning):
                st.write("**例句：** " + example)
                st.write("**翻译：** " + ex_meaning)
                if st.button("🔊 发音", key="trans_tts_" + word):
                    tts_speak(word)
                if st.button("➕ 加入我的词库", key="trans_add_" + word):
                    ok = add_word(word, pos, meaning, example, "medium", phonetic, ex_meaning, "translation")
                    if ok:
                        st.success("已添加「" + word + "」到词库！")
                    else:
                        st.info("「" + word + "」已在词库中。")

    with tab2:
        st.subheader("📥 一键导入翻译词汇")
        st.write("将以下 **" + str(len(TRANSLATION_VOCAB)) + "** 个翻译专业词汇导入你的词库：")
        st.write("导入后可以在「学习新词」「复习」「拼写」等模块中使用这些词汇。")
        if st.button("🚀 全部导入", type="primary"):
            cnt = 0
            skip = 0
            for item in TRANSLATION_VOCAB:
                word, phonetic, pos, meaning, example, ex_meaning = item
                ok = add_word(word, pos, meaning, example, "medium", phonetic, ex_meaning, "translation")
                if ok:
                    cnt += 1
                else:
                    skip += 1
            st.success("成功导入 " + str(cnt) + " 个翻译词汇！" + ("（跳过 " + str(skip) + " 个重复）" if skip > 0 else ""))

    with tab3:
        st.subheader("✍️ 翻译练习")
        st.write("看中文意思，翻译成英文，然后对照参考答案。")

        if "trans_prac_words" not in st.session_state or not st.session_state.trans_prac_words:
            prac = list(TRANSLATION_VOCAB)
            random.shuffle(prac)
            st.session_state.trans_prac_words = prac
            st.session_state.trans_prac_idx = 0
            st.session_state.trans_prac_show = False
            st.session_state.trans_prac_input = ""

        prac_words = st.session_state.trans_prac_words

        if st.session_state.trans_prac_idx >= len(prac_words):
            st.success("🎉 翻译练习完成！")
            for k in ["trans_prac_words", "trans_prac_idx", "trans_prac_show", "trans_prac_input"]:
                if k in st.session_state:
                    del st.session_state[k]
            if st.button("再来一轮"):
                st.rerun()
            return

        cur = prac_words[st.session_state.trans_prac_idx]
        word, phonetic, pos, meaning, example, ex_meaning = cur
        prog = st.session_state.trans_prac_idx / len(prac_words)
        st.progress(prog, "进度: " + str(st.session_state.trans_prac_idx + 1) + " / " + str(len(prac_words)))
        st.markdown("---")

        st.write("**请将以下中文翻译成英文：**")
        st.markdown("### " + meaning)
        st.caption("词性: " + pos + "  |  音标: " + phonetic)

        user_trans = st.text_input(
            "你的翻译：",
            value=st.session_state.get("trans_prac_input", ""),
            placeholder="输入英文翻译...",
            key="trans_input_" + str(st.session_state.trans_prac_idx)
        )

        if not st.session_state.trans_prac_show:
            if st.button("✅ 提交并查看答案", type="primary"):
                st.session_state.trans_prac_input = user_trans
                st.session_state.trans_prac_show = True
                st.rerun()
        else:
            st.markdown("---")
            st.write("**你的翻译：**")
            if st.session_state.trans_prac_input.strip():
                is_close = st.session_state.trans_prac_input.strip().lower() == word.lower()
                if is_close:
                    st.success("✅ 完全正确！" + word)
                else:
                    st.warning("你的: " + st.session_state.trans_prac_input)
            else:
                st.warning("你没有输入翻译！")

            st.write("**参考答案：** " + word)
            st.write("**例句：** " + example)
            st.write("**例句翻译：** " + ex_meaning)
            if st.button("🔊 听发音", key="trans_prac_tts_" + str(st.session_state.trans_prac_idx)):
                tts_speak(word)

            c1, c2 = st.columns(2)
            with c1:
                if st.button("➡️ 下一个", type="primary", key="trans_next_" + str(st.session_state.trans_prac_idx)):
                    st.session_state.trans_prac_idx += 1
                    st.session_state.trans_prac_show = False
                    st.session_state.trans_prac_input = ""
                    st.rerun()
            with c2:
                if st.button("⏭️ 跳过", key="trans_skip_" + str(st.session_state.trans_prac_idx)):
                    st.session_state.trans_prac_idx += 1
                    st.session_state.trans_prac_show = False
                    st.session_state.trans_prac_input = ""
                    st.rerun()

# ============================================================
# 页面：写作助手
# ============================================================
def page_writing():
    st.header("📝 写作助手")
    st.caption("六级写作模板、高频短语、连接词大全")

    tab1, tab2, tab3, tab4 = st.tabs(["📐 写作模板", "🔗 连接词", "💎 高频短语", "💡 写作技巧"])

    with tab1:
        st.subheader("📐 六级写作模板")
        for name, sections in WRITING_TEMPLATES.items():
            with st.expander("📋 " + name):
                for sec_name, template in sections:
                    st.markdown("**" + sec_name + "**")
                    st.code(template)
                    st.markdown("")

    with tab2:
        st.subheader("🔗 写作连接词大全")
        for cat, words in WRITING_TRANSITIONS.items():
            with st.expander("🔹 " + cat + "（" + str(len(words)) + "个）"):
                for w in words:
                    st.write("• " + w)
                # 听发音
                joined = "; ".join(words)
                if st.button("🔊 依次朗读", key="wt_tts_" + cat):
                    tts_speak(joined)

    with tab3:
        st.subheader("💎 六级写作高频短语")
        st.write("共 " + str(len(WRITING_HIGH_FREQ_PHRASES)) + " 个高频短语，点击可听发音")
        for i, (phrase, meaning) in enumerate(WRITING_HIGH_FREQ_PHRASES):
            c1, c2, c3 = st.columns([4, 3, 1])
            with c1:
                st.write("**" + phrase + "**")
            with c2:
                st.caption(meaning)
            with c3:
                if st.button("🔊", key="phrase_tts_" + str(i)):
                    tts_speak(phrase)
            st.divider()

    with tab4:
        st.subheader("💡 六级写作技巧")
        tips = [
            ("字数控制", "六级作文要求150-200词，建议写到180词左右。太少扣分，太多浪费时间。"),
            ("段落结构", "建议三段式：开头段（引出话题+表明观点）+ 主体段（2-3个论据）+ 结尾段（总结+建议）。"),
            ("开头技巧", "可以用以下方式开头：\n1. 提出问题：Should...?\n2. 描述现象：Nowadays, it is common to...\n3. 引用名言：As the saying goes,...\n4. 数据引入：According to a recent survey,..."),
            ("论据展开", "每个论点用\"观点+解释+举例\"展开。举例可以用for instance, such as, take...for example。"),
            ("高级词汇替换", "good → beneficial/advantageous\nbad → detrimental/harmful\nimportant → crucial/indispensable\nmany → a multitude of/numerous\nthink → maintain/contend/argue"),
            ("避免低级错误", "1. 主谓一致\n2. 时态一致\n3. 冠词使用\n4. 逗号拼接（两个完整句子不能只用逗号连接）\n5. 拼写错误"),
            ("结尾技巧", "结尾可以：\n1. 总结观点：In conclusion,...\n2. 提出建议：It is high time that...\n3. 展望未来：Only by...can we...\n4. 呼吁行动：Let us join hands to..."),
            ("时间分配", "建议：审题2分钟 → 列提纲3分钟 → 写作15分钟 → 检查5分钟。"),
        ]
        for title, content in tips:
            with st.expander("📌 " + title):
                st.write(content)

# ============================================================
# 页面：词汇量测试
# ============================================================
def page_vocab_test():
    st.header("📏 词汇量测试")
    st.caption("自适应词汇量估算 · 四选一 · 约 60-80 题 · 5-10 分钟")

    tab1, tab2 = st.tabs(["🧪 开始测试", "📊 历史成绩"])

    with tab1:
        if "vt_started" not in st.session_state or not st.session_state.vt_started:
            st.subheader("📋 测试说明")
            st.write("1. 测试分为 **" + str(len(VOCAB_BANDS)) + "** 个难度等级，从易到难")
            st.write("2. 每题四个选项，选择正确的中文意思")
            st.write("3. 系统根据你的正确率自动调整难度（自适应）")
            st.write("4. 共约 **60-80** 题，测试约需 **5-10 分钟**")
            st.write("5. 不确定的题可以猜，猜错不扣分")
            st.markdown("---")
            st.write("**难度等级预览：**")
            for i, band in enumerate(VOCAB_BANDS):
                st.write(str(i + 1) + ". " + band["name"] + " - " + band["desc"])
            st.markdown("---")
            if st.button("🚀 开始测试", type="primary"):
                st.session_state.vt_started = True
                st.session_state.vt_band = 0  # 从第1级开始
                st.session_state.vt_band_results = {}  # {band_idx: (correct, total)}
                st.session_state.vt_words_in_band = []
                st.session_state.vt_word_idx = 0
                st.session_state.vt_correct_in_band = 0
                st.session_state.vt_total_correct = 0
                st.session_state.vt_total_questions = 0
                st.session_state.vt_options = []  # 当前题的选项
                st.session_state.vt_selected = None  # 用户选的答案
                st.session_state.vt_show_answer = False  # 是否显示答案
                st.session_state.vt_direction = "up"  # 当前方向：上升/下降
                st.session_state.vt_consecutive_wrong = 0  # 连续错题数
                st.session_state.vt_highest_band = 0  # 达到的最高等级
                # 取第一个band的词
                words = get_vocab_test_words(0, limit=BAND_QUESTIONS)
                if words:
                    st.session_state.vt_words_in_band = [dict(w) for w in words]
                    random.shuffle(st.session_state.vt_words_in_band)
                st.rerun()
            return

        # 测试进行中
        band_idx = st.session_state.vt_band
        band = VOCAB_BANDS[band_idx]
        words = st.session_state.vt_words_in_band
        widx = st.session_state.vt_word_idx

        st.info("📊 当前等级: " + band["name"] + " | 第 " + str(widx + 1) + " / " + str(len(words)) + " 题")
        total_est = st.session_state.vt_total_questions
        prog = min(total_est / 70, 1.0)
        st.progress(int(prog * 100), "总进度: 约 " + str(total_est) + " / ~70 题")
        st.markdown("---")

        if widx >= len(words):
            # 当前band完成，记录结果，决定下一步
            st.session_state.vt_band_results[band_idx] = (st.session_state.vt_correct_in_band, len(words))
            correct_rate = st.session_state.vt_correct_in_band / len(words) if words else 0

            # 更新最高等级
            if correct_rate >= 0.6:
                st.session_state.vt_highest_band = max(st.session_state.vt_highest_band, band_idx)

            # 自适应逻辑
            # - 正确率 >= 80%：跳一级（快速上升）
            # - 正确率 >= 60%：升一级
            # - 正确率 >= 40%：同级别再测一组（但简化处理：还是升，估算时用正确率调整）
            # - 正确率 < 40%：如果是降级状态，停止；否则降级
            if correct_rate >= 0.8 and band_idx < len(VOCAB_BANDS) - 1:
                # 跳一级
                next_band = min(band_idx + 2, len(VOCAB_BANDS) - 1)
                st.session_state.vt_direction = "up"
            elif correct_rate >= 0.6 and band_idx < len(VOCAB_BANDS) - 1:
                next_band = band_idx + 1
                st.session_state.vt_direction = "up"
            elif correct_rate < 0.4 and st.session_state.vt_direction == "up" and band_idx > 0:
                # 上升途中遇到困难，降一级再确认
                next_band = band_idx - 1
                st.session_state.vt_direction = "down"
            else:
                # 正确率中等，或者已经在下降通道，结束测试
                _show_vocab_test_result()
                return

            # 进入下一个等级
            st.session_state.vt_band = next_band
            st.session_state.vt_word_idx = 0
            st.session_state.vt_correct_in_band = 0
            new_words = get_vocab_test_words(next_band, limit=BAND_QUESTIONS)
            if new_words:
                st.session_state.vt_words_in_band = [dict(w) for w in new_words]
                random.shuffle(st.session_state.vt_words_in_band)
            else:
                _show_vocab_test_result()
                return
            st.rerun()

        cur = words[widx]

        # 生成选项（如果还没生成）
        if not st.session_state.vt_options or st.session_state.get("_vt_cur_word_id") != cur["id"]:
            distractors = get_vocab_distractors(cur["word"], band_idx, count=3)
            options = [cur["meaning"]] + distractors
            random.shuffle(options)
            st.session_state.vt_options = options
            st.session_state._vt_cur_word_id = cur["id"]
            st.session_state.vt_selected = None
            st.session_state.vt_show_answer = False

        options = st.session_state.vt_options

        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown("### " + cur["word"])
            if cur["phonetic"]:
                st.caption("🔊 " + cur["phonetic"])
        with col2:
            if st.button("🔊 发音", key="vt_tts_" + str(cur["id"])):
                tts_speak(cur["word"])

        st.write("**请选择正确的中文意思：**")

        # 四选一
        for i, opt in enumerate(options):
            label = chr(65 + i) + ". " + opt
            if not st.session_state.vt_show_answer:
                if st.button(label, key="vt_opt_" + str(cur["id"]) + "_" + str(i), use_container_width=True):
                    st.session_state.vt_selected = i
                    st.session_state.vt_show_answer = True
                    is_correct = (opt == cur["meaning"])
                    if is_correct:
                        st.session_state.vt_correct_in_band += 1
                        st.session_state.vt_total_correct += 1
                        st.session_state.vt_consecutive_wrong = 0
                    else:
                        st.session_state.vt_consecutive_wrong += 1
                    st.session_state.vt_total_questions += 1
                    st.rerun()
            else:
                # 显示答案状态
                is_correct_choice = (opt == cur["meaning"])
                user_picked = (st.session_state.vt_selected == i)
                if is_correct_choice:
                    st.success("✅ " + label)
                elif user_picked and not is_correct_choice:
                    st.error("❌ " + label)
                else:
                    st.write(label)

        if st.session_state.vt_show_answer:
            st.markdown("---")
            # 显示正确答案和例句
            if cur.get("example"):
                st.caption("📝 例句: " + cur["example"])
                if cur.get("example_meaning"):
                    st.caption("💬 " + cur["example_meaning"])
            col1, col2 = st.columns([3, 1])
            with col2:
                if st.button("下一题 ➡️", type="primary", use_container_width=True, key="vt_next_" + str(cur["id"])):
                    st.session_state.vt_word_idx += 1
                    st.session_state.vt_options = []
                    st.session_state.vt_selected = None
                    st.session_state.vt_show_answer = False
                    st.rerun()
            # 回车跳下一题
            _enter_to_click("vt_next_" + str(cur["id"]), "vt_next_" + str(cur["id"]))
            return

    with tab2:
        st.subheader("📊 历史成绩")
        history = get_vocab_test_history()
        if history:
            for r in history:
                c1, c2, c3 = st.columns(3)
                with c1: st.metric("日期", r["test_date"])
                with c2: st.metric("估算词汇量", r["estimated_size"])
                with c3: st.metric("正确率", str(r["correct_count"]) + "/" + str(r["total_questions"]))
                st.divider()
        else:
            st.info("还没有测试记录，去测一次吧！")

def _show_vocab_test_result():
    """显示词汇量测试结果"""
    band_results = st.session_state.vt_band_results
    total_q = st.session_state.vt_total_questions
    total_correct = st.session_state.vt_total_correct

    # 估算词汇量：基于各等级的正确率加权计算
    # 思路：找到稳定通过的最高等级，用该等级的正确率微调
    highest_passed = 0
    for bi, (correct, total) in band_results.items():
        rate = correct / total if total > 0 else 0
        if rate >= 0.6:
            highest_passed = max(highest_passed, bi)

    # 基础词汇量 = 最高通过等级的基准
    base_vocab = VOCAB_BANDS[highest_passed]["vocab_base"]

    # 用更高等级的正确率向上延伸
    if highest_passed < len(VOCAB_BANDS) - 1:
        next_band = highest_passed + 1
        if next_band in band_results:
            nc, nt = band_results[next_band]
            next_rate = nc / nt if nt > 0 else 0
            next_base = VOCAB_BANDS[next_band]["vocab_base"]
            # 高等级正确率乘以该等级词汇量差
            bonus = int((next_base - base_vocab) * next_rate)
            estimated = base_vocab + bonus
        else:
            # 没有更高等级的数据，用当前等级的正确率微调
            if highest_passed in band_results:
                c, t = band_results[highest_passed]
                rate = c / t if t > 0 else 0
                # 当前等级超过60%的部分，按比例向上延伸
                extra_rate = max(0, rate - 0.6) / 0.4
                if highest_passed < len(VOCAB_BANDS) - 1:
                    next_base = VOCAB_BANDS[highest_passed + 1]["vocab_base"]
                    bonus = int((next_base - base_vocab) * extra_rate * 0.5)
                    estimated = base_vocab + bonus
                else:
                    estimated = base_vocab
            else:
                estimated = base_vocab
    else:
        # 已经到最高级了
        if highest_passed in band_results:
            c, t = band_results[highest_passed]
            rate = c / t if t > 0 else 0
            estimated = int(base_vocab + 2000 * rate)
        else:
            estimated = base_vocab

    # 保存结果
    details = "; ".join([VOCAB_BANDS[bi]["name"] + ":" + str(c) + "/" + str(t) for bi, (c, t) in sorted(band_results.items())])
    save_vocab_test_result(estimated, total_q, total_correct, details)

    st.success("🎉 测试完成！")
    st.markdown("---")

    c1, c2, c3 = st.columns(3)
    with c1: st.metric("📊 估算词汇量", str(estimated))
    with c2: st.metric("✅ 答对", total_correct)
    with c3: st.metric("📝 总题数", total_q)

    overall_acc = round(total_correct / total_q * 100, 1) if total_q > 0 else 0
    st.caption("总正确率: " + str(overall_acc) + "%")

    st.markdown("---")
    st.subheader("📋 各等级详情")
    for bi, (correct, total) in sorted(band_results.items()):
        rate = round(correct / total * 100, 1) if total > 0 else 0
        if rate >= 80:
            icon = "🏆"
        elif rate >= 60:
            icon = "✅"
        elif rate >= 40:
            icon = "⚠️"
        else:
            icon = "❌"
        st.write(icon + " " + VOCAB_BANDS[bi]["name"] + " - " + str(correct) + "/" + str(total) + " (" + str(rate) + "%)")

    # 评价
    st.markdown("---")
    if estimated < 2500:
        st.warning("⚠️ 你的词汇量处于入门水平，建议从基础词汇开始，每天坚持背 20-30 个！")
    elif estimated < 4000:
        st.info("📈 你的词汇量处于中等偏下，继续努力冲击四级 4500 词汇量！")
    elif estimated < 5500:
        st.info("📈 不错！接近四级水平，继续努力拿下六级！")
    elif estimated < 7000:
        st.success("👍 六级水平！词汇量扎实，继续冲高分！")
    elif estimated < 9000:
        st.success("🎉 词汇量很棒！六级高分 + 考研都很有竞争力。")
    else:
        st.balloons()
        st.success("🏆 词汇量惊人！雅思/托福水平，超越了大多数英语学习者！")

    # 清理
    for k in ["vt_started", "vt_band", "vt_band_results", "vt_words_in_band", "vt_word_idx",
              "vt_correct_in_band", "vt_total_correct", "vt_total_questions",
              "vt_options", "vt_selected", "vt_show_answer", "vt_direction",
              "vt_consecutive_wrong", "vt_highest_band", "_vt_cur_word_id"]:
        if k in st.session_state:
            del st.session_state[k]

    if st.button("🔄 再测一次", type="primary"):
        st.rerun()

# ============================================================
# 页面：设置
# ============================================================
def page_settings():
    st.header("⚙️ 设置")
    st.subheader("每日目标")
    cur_goal = int(get_setting("daily_goal", "30"))
    new_goal = st.number_input("每日新词目标", min_value=5, max_value=200, value=cur_goal, step=5)
    if st.button("保存"):
        set_setting("daily_goal", new_goal)
        st.success("设置已保存！")
    st.markdown("---")
    st.subheader("数据管理")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("📊 查看数据库统计"):
            st.json(get_stats())
    with col2:
        if st.button("⚠️ 清空所有数据", type="secondary"):
            st.warning("确定要清空所有数据吗？")
            if st.button("确认清空！不可恢复！", type="primary"):
                conn = get_db()
                c = conn.cursor()
                c.execute("DELETE FROM words")
                c.execute("DELETE FROM review_log")
                c.execute("DELETE FROM checkin")
                conn.commit()
                conn.close()
                st.success("所有数据已清空。")
                st.rerun()
    st.markdown("---")
    st.subheader("关于")
    st.info("VocabMaster 六级背单词工具 | Python + Streamlit + SQLite + Plotly")

# ============================================================
# 主程序
# ============================================================
def main():
    init_db()
    st.set_page_config(page_title="VocabMaster", page_icon="📚", layout="wide", initial_sidebar_state="expanded")

    with st.sidebar:
        st.title("📚 VocabMaster")
        st.caption("六级背单词工具")
        st.markdown("---")
        pages = {
            "📊 仪表盘": "dashboard",
            "📖 学习新词": "learn",
            "🔄 复习单词": "review",
            "⌨️ 单词拼写": "spelling",
            "✍️ 造句练习": "sentence",
            "📝 单词测试": "test",
            "📏 词汇量测试": "vocab_test",
            "🌐 翻译词汇": "translation",
            "📝 写作助手": "writing",
            "📈 学习统计": "stats",
            "📚 词库管理": "library",
            "⚙️ 设置": "settings"
        }
        if "current_page" not in st.session_state:
            st.session_state.current_page = "dashboard"
        for label, page in pages.items():
            if st.button(label, use_container_width=True, type="primary" if st.session_state.current_page == page else "secondary"):
                st.session_state.current_page = page
                # 切换页面时保留学习进度，不清空 session_state
                st.rerun()
        st.markdown("---")
        stats = get_stats()
        st.caption("📊 总词数: " + str(stats["total"]) + " | 已掌握: " + str(stats["mastered"]))
        st.caption("🆕 未学: " + str(stats["new"]) + " | 📖 学习中: " + str(stats["learning"]))
        streak = get_streak()
        if streak > 0:
            st.caption("🔥 连续打卡: " + str(streak) + "天")

    page = st.session_state.current_page
    if page == "dashboard": page_dashboard()
    elif page == "learn": page_learn()
    elif page == "review": page_review()
    elif page == "spelling": page_spelling()
    elif page == "sentence": page_sentence()
    elif page == "test": page_test()
    elif page == "vocab_test": page_vocab_test()
    elif page == "translation": page_translation()
    elif page == "writing": page_writing()
    elif page == "stats": page_stats()
    elif page == "library": page_library()
    elif page == "settings": page_settings()

if __name__ == "__main__":
    main()
