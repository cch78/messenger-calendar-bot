import sys
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

import discord
from discord.ext import tasks
import anthropic
import asyncio
import json
import os
import re
import glob
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────────
GUILD_ID        = int(os.environ["DISCORD_GUILD_ID"])
REPORT_CHANNEL  = int(os.environ["DISCORD_REPORT_CHANNEL_ID"])
DATA_DIR        = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

KST = timezone(timedelta(hours=9))

NOTION_TOKEN   = os.environ.get("NOTION_TOKEN", "")
NOTION_DB_ID   = os.environ.get("NOTION_DATABASE_ID", "")
notion = None
if NOTION_TOKEN:
    from notion_client import Client as NotionClient
    notion = NotionClient(auth=NOTION_TOKEN)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── 날짜 유틸 ─────────────────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(KST)

def date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")

def today_str() -> str:
    return date_str(now_kst().date())

def yesterday_str() -> str:
    return date_str((now_kst() - timedelta(days=1)).date())

def parse_date_from_question(text: str) -> list[str]:
    """질문에서 날짜를 파싱해 해당 날짜 파일 경로 목록을 반환한다."""
    today = now_kst().date()

    if "오늘" in text:
        return [date_str(today)]
    if "어제" in text:
        return [date_str(today - timedelta(days=1))]
    if "이번주" in text or "이번 주" in text:
        return [date_str(today - timedelta(days=i)) for i in range(7)]
    if "지난주" in text or "지난 주" in text:
        return [date_str(today - timedelta(days=i)) for i in range(7, 14)]
    if "이번달" in text or "이번 달" in text:
        return [date_str(today - timedelta(days=i)) for i in range(today.day)]

    # "5월 11일" / "5/11" 형식
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year
        try:
            return [date_str(date(year, month, day))]
        except ValueError:
            pass

    m = re.search(r"(\d{1,2})[./](\d{1,2})", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            return [date_str(date(today.year, month, day))]
        except ValueError:
            pass

    # 날짜 언급 없으면 최근 7일
    return [date_str(today - timedelta(days=i)) for i in range(7)]

# ── JSON 저장 ─────────────────────────────────────────────────────────
def save_message(msg: discord.Message):
    file = DATA_DIR / f"messages_{today_str()}.json"
    messages = json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
    messages.append({
        "time": now_kst().strftime("%p %I:%M").replace("AM", "AM").replace("PM", "PM"),
        "channel": msg.channel.name,
        "author": msg.author.display_name or msg.author.name,
        "content": msg.content,
    })
    file.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")

def load_messages(date_strs: list[str]) -> list[dict]:
    result = []
    for ds in date_strs:
        file = DATA_DIR / f"messages_{ds}.json"
        if file.exists():
            try:
                msgs = json.loads(file.read_text(encoding="utf-8"))
                for m in msgs:
                    m["_date"] = ds
                result.extend(msgs)
            except Exception:
                pass
    return result

# ── Claude 요약 ───────────────────────────────────────────────────────
async def generate_report(ds: str) -> str:
    messages = load_messages([ds])
    if not messages:
        return f"📋 **{ds} 일일 보고**\n수집된 메시지가 없습니다."

    authors  = {m["author"] for m in messages}
    channels = {m["channel"] for m in messages}

    sample = messages if len(messages) <= 300 else messages[:100] + messages[-100:]
    log = "\n".join(f"[{m['time']}] #{m['channel']} {m['author']}: {m['content']}" for m in sample)

    prompt = (
        f"날짜: {ds}\n"
        f"총 메시지: {len(messages)}개 | 참여자: {len(authors)}명 | 채널: {len(channels)}개\n\n"
        "아래 로그를 분석해 다음 형식으로 보고서를 작성해주세요:\n\n"
        "**⚠️ 특이사항**\n"
        '(중요 결정, 마감/일정 언급, 갈등, 긴급 이슈, 주요 공지. 없으면 "없음")\n\n'
        "**📌 채널별 요약**\n"
        "(활동이 있었던 채널별 핵심 내용 2~3줄)\n\n"
        "**✅ 언급된 할일 / 일정**\n"
        "(TODO, 마감일, 미팅 약속 등. 없으면 생략)\n\n"
        f"---\n{log}"
    )
    resp = await asyncio.to_thread(
        claude.messages.create,
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=(
            "당신은 한의원 Discord 서버의 일일 활동을 분석하는 어시스턴트입니다. "
            "메시지 로그를 분석해 한국어로 보고서를 작성하세요. "
            "Discord 마크다운 형식을 사용하세요 (**굵게**, `코드` 등)."
        ),
        messages=[{"role": "user", "content": prompt}],
    )

    stats = f"📋 **{ds} 일일 보고** | 메시지 {len(messages)}개 · 참여자 {len(authors)}명 · 채널 {len(channels)}개\n\n"
    return stats + resp.content[0].text

# ── Q&A ──────────────────────────────────────────────────────────────
async def answer_question(question: str) -> str:
    date_strs = parse_date_from_question(question)
    messages  = load_messages(date_strs)

    if not messages:
        period = ", ".join(date_strs[:3]) + ("..." if len(date_strs) > 3 else "")
        return f"❌ 해당 기간({period})에 저장된 메시지가 없습니다."

    log_lines = [
        f"[{m['_date']} {m['time']}] #{m['channel']} {m['author']}: {m['content']}"
        for m in messages
    ]
    # 너무 길면 앞뒤로 자름
    if len(log_lines) > 400:
        log_lines = log_lines[:200] + ["... (중략) ..."] + log_lines[-100:]
    log = "\n".join(log_lines)

    resp = await asyncio.to_thread(
        claude.messages.create,
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=(
            "당신은 한의원 Discord 채팅 기록을 검색하는 어시스턴트입니다.\n"
            "아래 대화 로그를 바탕으로 질문에 정확하고 간결하게 답하세요.\n"
            "날짜·시간·환자 이름을 명확히 언급하세요. 확실하지 않으면 '기록에 없음'이라고 하세요."
        ),
        messages=[{"role": "user", "content": f"질문: {question}\n\n대화 로그:\n{log}"}],
    )
    return resp.content[0].text

# ── Notion 동기화 ─────────────────────────────────────────────────────
async def sync_to_notion(ds: str) -> str:
    if not notion or not NOTION_DB_ID:
        return "⚠️ Notion 설정이 없습니다. .env에 NOTION_TOKEN과 NOTION_DATABASE_ID를 추가하세요."

    messages = load_messages([ds])
    if not messages:
        return f"❌ {ds} 데이터가 없습니다."

    log = "\n".join(
        f"[{m['time']}] #{m['channel']} {m['author']}: {m['content']}"
        for m in messages
    )

    # 요약 생성 (블로킹 → 스레드)
    resp = await asyncio.to_thread(
        claude.messages.create,
        model="claude-sonnet-4-6",
        max_tokens=600,
        system="한의원 Discord 로그를 간결하게 요약하세요. 주요 환자, 특이사항, 할일 위주로.",
        messages=[{"role": "user", "content": log[:8000]}],
    )
    summary = resp.content[0].text

    authors  = {m["author"] for m in messages}
    channels = {m["channel"] for m in messages}

    content_blocks = []
    for chunk in [log[i:i+2000] for i in range(0, min(len(log), 20000), 2000)]:
        content_blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
        })

    props = {
        "이름": {"title": [{"text": {"content": ds}}]},
        "날짜": {"date": {"start": ds}},
        "요약": {"rich_text": [{"text": {"content": summary[:2000]}}]},
        "메시지수": {"number": len(messages)},
        "채널수": {"number": len(channels)},
        "참여자수": {"number": len(authors)},
    }

    # Notion API 호출 (블로킹 → 스레드)
    existing = await asyncio.to_thread(
        notion.databases.query,
        database_id=NOTION_DB_ID,
        filter={"property": "날짜", "date": {"equals": ds}},
    )

    if existing["results"]:
        page_id = existing["results"][0]["id"]
        await asyncio.to_thread(notion.pages.update, page_id=page_id, properties=props)
        await asyncio.to_thread(notion.blocks.children.append, block_id=page_id, children=content_blocks)
        return f"✅ Notion 페이지 업데이트 완료 ({ds}, {len(messages)}개 메시지)"
    else:
        await asyncio.to_thread(
            notion.pages.create,
            parent={"database_id": NOTION_DB_ID},
            properties=props,
            children=content_blocks,
        )
        return f"✅ Notion 페이지 생성 완료 ({ds}, {len(messages)}개 메시지)"

# ── Discord 클라이언트 ─────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
client = discord.Client(intents=intents)

async def send_long(channel, text: str):
    """2000자 초과 시 분할 전송."""
    chunks = [text[i:i+1990] for i in range(0, len(text), 1990)]
    for chunk in chunks:
        await channel.send(chunk)

@tasks.loop(minutes=1)
async def daily_report():
    """매분 실행, 오전 8시(KST)에만 보고 전송."""
    now = now_kst()
    if now.hour != 8 or now.minute != 0:
        return
    channel = client.get_channel(REPORT_CHANNEL)
    if not channel:
        channel = await client.fetch_channel(REPORT_CHANNEL)
    ds = yesterday_str()
    print(f"일일 보고 생성: {ds}", flush=True)
    report = await generate_report(ds)
    await send_long(channel, report)
    if notion and NOTION_DB_ID:
        result = await sync_to_notion(ds)
        print(f"Notion 동기화: {result}", flush=True)

@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    notion_status = "연결됨" if notion else "미설정"
    lines = [
        f"봇 시작: {client.user}",
        f"서버: {guild.name if guild else GUILD_ID}",
        f"보고 채널: {REPORT_CHANNEL}",
        f"Notion: {notion_status}",
        f"데이터 폴더: {DATA_DIR}",
    ]
    for line in lines:
        print(line, flush=True)
    try:
        daily_report.start()
        print("daily_report 태스크 시작됨", flush=True)
    except Exception as e:
        print(f"daily_report 시작 오류: {e}", flush=True)

@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return
    if msg.guild is None or msg.guild.id != GUILD_ID:
        return

    save_message(msg)

    text = msg.content.strip()

    # !report — 오늘 즉시 보고
    if text == "!report":
        await msg.reply("보고서 생성 중...")
        report = await generate_report(today_str())
        await send_long(msg.channel, report)
        return

    # !질문 [내용] — 기간별 Q&A
    if text.startswith("!질문"):
        question = text[3:].strip()
        if not question:
            await msg.reply("사용법: `!질문 5월 11일에 이승훈 환자 기록 알려줘`")
            return
        await msg.reply("검색 중...")
        answer = await answer_question(question)
        await send_long(msg.channel, answer)
        return

    # !노션동기화 [날짜(선택)] — 특정 날 또는 어제 동기화
    if text.startswith("!노션동기화"):
        arg = text[7:].strip()
        if arg:
            ds_list = parse_date_from_question(arg) if not re.match(r"\d{4}-\d{2}-\d{2}", arg) else [arg]
        else:
            ds_list = [yesterday_str()]
        await msg.reply(f"{', '.join(ds_list)} 동기화 중...")
        for ds in ds_list:
            result = await sync_to_notion(ds)
            await msg.reply(result)
        return

    # !도움말
    if text == "!도움말":
        help_text = (
            "**사용 가능한 명령어**\n"
            "`!report` — 오늘 수집된 메시지 즉시 요약\n"
            "`!질문 [내용]` — 대화 기록 검색·질문 (예: `!질문 5월 11일 이승훈 환자`)\n"
            "`!노션동기화 [날짜]` — Notion에 업로드 (예: `!노션동기화 어제`)\n"
            "`!도움말` — 이 메시지\n\n"
            "매일 오전 8시에 전날 요약이 이 채널에 자동 게시됩니다."
        )
        await msg.reply(help_text)

# ── 헬스체크 HTTP 서버 (Render 무료 Web Service 슬립 방지) ───────────────
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass  # 액세스 로그 무음

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

client.run(os.environ["DISCORD_TOKEN"])
