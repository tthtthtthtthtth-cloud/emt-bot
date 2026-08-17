import os
import random
import asyncio
import logging
import datetime
from collections import defaultdict
from datetime import timezone, timedelta
from threading import Thread

import discord
from flask import Flask
from google import genai
from google.genai import types
from google.genai.errors import APIError

# ---------------------------------------------------------------------------
# 0. 基本設定
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
# 這些 library 每次請求都會印一行，長跑會把 log 洗爆
for _noisy in ('httpx', 'httpcore', 'google_genai.models', 'google_genai.types', 'werkzeug'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger('emt-bot')

TW_TZ = timezone(timedelta(hours=8))

# 閒置多久自動回收 session（分鐘）
IDLE_TIMEOUT_MIN = 45
# 清理排程間隔（分鐘）
CLEANUP_INTERVAL_MIN = 5
# 單一案例輪數：達到這個數字時提醒學員收尾
MAX_TURNS_WARN = 40
# 單一案例輪數硬上限：達到就強制結束，避免對話歷史吃爆 512MB
MAX_TURNS_HARD = 60

DISCORD_LIMIT = 1900

# ---------------------------------------------------------------------------
# 1. Google GenAI Client
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    raise SystemExit('錯誤：找不到 GEMINI_API_KEY 環境變數。')

genai_client = genai.Client(api_key=GEMINI_API_KEY)

# 依 2026 年現況排序：新的在前，最後放 alias 保底
MODEL_CANDIDATES = [
    'gemini-3.6-flash',
    'gemini-3.5-flash',
    'gemini-3.5-flash-lite',
    'gemini-flash-latest',
]
CACHED_MODEL = MODEL_CANDIDATES[0]

# 醫療模擬會出現大量出血、創傷、死亡等描述，需放寬安全門檻
SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold='BLOCK_NONE')
    for c in (
        'HARM_CATEGORY_DANGEROUS_CONTENT',
        'HARM_CATEGORY_HARASSMENT',
        'HARM_CATEGORY_HATE_SPEECH',
        'HARM_CATEGORY_SEXUALLY_EXPLICIT',
    )
]

# 本 bot 沒有使用任何 tools，關掉自動函式呼叫可省開銷並消除 SDK warning
AFC_OFF = types.AutomaticFunctionCallingConfig(disable=True)


def _is_model_missing(err_str):
    """區分「這個模型真的不存在」vs「只是暫時打不通（額度、忙碌、網路）」。"""
    return any(
        k in err_str
        for k in ('404', 'NOT_FOUND', 'not found', 'is not supported',
                  'no longer available', 'INVALID_ARGUMENT')
    )


def init_working_model():
    """開機時測試一次可用模型（同步函式，請用 asyncio.to_thread 呼叫）。"""
    global CACHED_MODEL
    for model_name in MODEL_CANDIDATES:
        try:
            genai_client.models.generate_content(
                model=model_name,
                contents='ping',
                config=types.GenerateContentConfig(automatic_function_calling=AFC_OFF),
            )
            CACHED_MODEL = model_name
            log.info('✅ 開機測試成功，全局採用模型：%s', CACHED_MODEL)
            return CACHED_MODEL
        except Exception as e:
            err = str(e)
            log.warning('模型 %s 測試失敗：%s', model_name, err[:300])
            if not _is_model_missing(err):
                # 429 額度用盡、503 忙碌、網路問題 → 模型本身沒事，不該降級
                CACHED_MODEL = model_name
                log.warning('   → 判定為暫時性錯誤，仍採用 %s（實際請求時會自動重試）', CACHED_MODEL)
                return CACHED_MODEL
            log.warning('   → 判定為模型已下架，嘗試下一個…')
    log.error('⚠️ 所有候選模型皆已下架，仍先採用 %s，請儘快更新 MODEL_CANDIDATES', CACHED_MODEL)
    return CACHED_MODEL


async def send_msg_with_retry(chat, text, max_retries=3):
    """非阻塞版重試：把同步的 SDK 呼叫丟到 thread，等待期間不卡住 event loop。"""
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.to_thread(chat.send_message, text)
        except APIError as e:
            err = str(e)
            retryable = any(
                k in err for k in ('503', 'UNAVAILABLE', '429', 'RESOURCE_EXHAUSTED')
            )
            if retryable and attempt < max_retries:
                wait_sec = 2 * (attempt + 1)
                log.warning('Google API 忙碌，%s 秒後進行第 %s 次重試…', wait_sec, attempt + 1)
                await asyncio.sleep(wait_sec)
                continue
            raise


def get_memory_mb():
    """讀取本 process 實際佔用的實體記憶體（MB）。免費層無 Metrics，靠這個自行監控。"""
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return -1.0


def extract_text(response):
    """回應可能因安全過濾或 finish_reason 而沒有 text，統一在這裡處理。"""
    if response is None:
        return None
    text = getattr(response, 'text', None)
    if text:
        return text.strip()
    return None


# ---------------------------------------------------------------------------
# 2. 教官 System Prompt
# ---------------------------------------------------------------------------
EMT_SYSTEM_PROMPT = """
你是一個嚴格的台灣「緊急醫療救護 (EMT)」模擬訓練教官。
請完全依照台灣衛福部法規、消防署 EMT-1/EMT-2 教科書與標準急救指引來進行評估。

【核心規則與互動機制】
1. 嚴格被動模式 (Passive Mode)：
   - 絕不主動提供未被詢問的生命徵象或病患內部狀況。
   - 保持對話的「破碎感」，一次只推進一點點進度，等待學員下達明確指令。
   - 學員沒問的，就不要說。學員問錯方向，就照實回報他問到的東西。
2. 考官與病患合一：
   - 當學員執行動作時（如：檢查意識、量血壓、哈姆立克、CPR），回報客觀事實。
   - 絕不主動「補丸」或代替學員做決定（例如：學員沒喊準備 AED，絕對不能自己出現 AED）。
   - 現場旁人、家屬只有被詢問時才回答，且回答可能不精確、情緒化。
3. 違規糾正：
   - 若學員做出越級處置（如 EMT-1 嘗試給藥或打針），必須以教官身分嚴厲制止並扣分。
   - 糾正時用【教官介入】標記，與情境描述區隔。
4. 結案報告 (AAR)：
   - 當學員完成任務、送醫或病患死亡時，提供詳細的 0-100 分考核報告與條列式檢討。
   - 報告需涵蓋：現場安全、意識評估、ABC、生命徵象、處置正確性、後送判斷、時間掌控。
   - 報告結尾請附上一行：「本案例已結束，輸入 !reset 後可開始新案例。」

【輸出格式限制】
- 每則回覆請控制在 500 字以內，不要一次倒出整份劇本。
- 除 AAR 外，不要主動揭露診斷或正確答案。
"""

CASE_TYPES = [
    '重大創傷', '內科急症', 'OHCA（到院前心跳停止）', '呼吸道異物阻塞',
    '兒科急症', '孕產婦急症', '藥物或一氧化碳中毒', '大面積燒燙傷',
    '熱傷害', '溺水', '癲癇發作', '過敏性休克', '低血糖', '腦中風',
]
CASE_PLACES = [
    '老舊公寓五樓', '施工中的工地', '國小操場', '大賣場停車場', '繁忙路口',
    '夜市攤位旁', '長照機構房間', '室內游泳池', '登山步道入口', 'KTV 包廂',
    '工廠產線旁', '獨居長者住處',
]
CASE_HOURS = ['清晨', '上午', '午後', '傍晚', '深夜']


def build_start_prompt():
    """注入亂數種子，避免每次都生成同一種案例。"""
    return (
        f"【學員已輸入 !start】請生成一個【{random.choice(CASE_TYPES)}】類、"
        f"發生地點在【{random.choice(CASE_PLACES)}】、"
        f"時間為【{random.choice(CASE_HOURS)}】的 EMT 模擬案例。\n"
        f"請直接輸出「派遣資訊」（報案內容、地點、時間、初步描述），"
        f"不要透露診斷，不要提供未被詢問的生命徵象，然後等待學員下達第一個指令。"
    )


# ---------------------------------------------------------------------------
# 3. Session 管理
# ---------------------------------------------------------------------------
class Session:
    __slots__ = ('chat', 'model', 'started_at', 'last_active', 'turns')

    def __init__(self, chat, model):
        now = datetime.datetime.now(TW_TZ)
        self.chat = chat
        self.model = model
        self.started_at = now
        self.last_active = now
        self.turns = 0

    def touch(self):
        self.last_active = datetime.datetime.now(TW_TZ)
        self.turns += 1

    def idle_minutes(self):
        delta = datetime.datetime.now(TW_TZ) - self.last_active
        return delta.total_seconds() / 60


sessions = {}
channel_locks = defaultdict(asyncio.Lock)


def create_session():
    chat = genai_client.chats.create(
        model=CACHED_MODEL,
        config=types.GenerateContentConfig(
            system_instruction=EMT_SYSTEM_PROMPT,
            safety_settings=SAFETY_SETTINGS,
            automatic_function_calling=AFC_OFF,
        ),
    )
    return Session(chat, CACHED_MODEL)


# ---------------------------------------------------------------------------
# 4. Discord Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

HELP_TEXT = (
    "🚑 **EMT 模擬訓練機器人**\n"
    "`!start` — 隨機生成一個新的 EMT 模擬案例\n"
    "`!reset` — 放棄目前案例並清除本頻道進度\n"
    "`!status` — 查看本頻道目前狀態\n"
    "`!help` — 顯示這則說明\n\n"
    "案例進行中，直接用自然語言下達處置指令即可"
    "（例如：`我先確認現場安全`、`檢查意識反應`、`量血壓`）。\n"
    f"⏱️ 閒置超過 {IDLE_TIMEOUT_MIN} 分鐘的案例會自動關閉，單一案例上限 {MAX_TURNS_HARD} 輪。"
)


def split_message(text, limit=DISCORD_LIMIT):
    """優先在換行處切，避免中文句子與 markdown 被切爛。"""
    chunks, current = [], ''
    for line in text.split('\n'):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ''
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f'{current}\n{line}' if current else line
    if current:
        chunks.append(current)
    return chunks or ['（空白回覆）']


async def send_long(channel, text):
    for chunk in split_message(text):
        await channel.send(chunk)


async def reply_api_error(channel, e, stage='對話'):
    """log 完整錯誤，但只回給使用者可讀的訊息。"""
    log.exception('%s 階段發生錯誤', stage)
    err = str(e)
    if '503' in err or 'UNAVAILABLE' in err:
        await channel.send(
            "⚠️ **【Google AI 伺服器忙碌中】** 已重試 3 次仍未成功，"
            "請稍候約 10 秒後**重新發送一次剛才的指令**。"
        )
    elif '429' in err or 'RESOURCE_EXHAUSTED' in err:
        await channel.send(
            "⚠️ **【觸發流量冷卻機制】** 已達每分鐘請求上限，請休息約 1 分鐘後再繼續。"
        )
    elif '404' in err or 'not found' in err.lower():
        await channel.send(
            f"❌ **【模型不可用】** 目前設定的模型 `{CACHED_MODEL}` 已無法使用，"
            "請管理員更新模型名稱後重新部署。"
        )
    else:
        await channel.send(f"❌ **【{stage}發生錯誤】** 請稍後再試，詳細訊息已記錄於伺服器日誌。")


# --- 背景任務：定期回收閒置 session ---------------------------------------
async def cleanup_task():
    await discord_client.wait_until_ready()
    while not discord_client.is_closed():
        await asyncio.sleep(CLEANUP_INTERVAL_MIN * 60)
        try:
            expired = [cid for cid, s in sessions.items() if s.idle_minutes() > IDLE_TIMEOUT_MIN]
            for cid in expired:
                sessions.pop(cid, None)
                channel_locks.pop(cid, None)
                channel = discord_client.get_channel(cid)
                if channel:
                    try:
                        await channel.send(
                            f"🧹 **【案例逾時關閉】** 本頻道案例閒置超過 {IDLE_TIMEOUT_MIN} 分鐘，"
                            "已自動清除。輸入 `!start` 可開始新案例。"
                        )
                    except discord.HTTPException:
                        pass
            if expired:
                log.info('已回收 %s 個閒置 session', len(expired))
            log.info('[監控] 記憶體 %.0fMB / 512MB，進行中 session：%d',
                     get_memory_mb(), len(sessions))
        except Exception:
            log.exception('cleanup_task 發生例外')


_bg_started = False


@discord_client.event
async def on_ready():
    """注意：discord.py 每次「重連」都會再觸發一次 on_ready，需自行防重入。"""
    global _bg_started
    log.info('🤖 EMT AI 專業教練已上線：%s', discord_client.user)
    if _bg_started:
        log.info('（偵測到重新連線，略過重複初始化）')
        return
    _bg_started = True
    await asyncio.to_thread(init_working_model)
    discord_client.loop.create_task(cleanup_task())


@discord_client.event
async def on_message(message):
    if message.author.bot:
        return

    channel_id = message.channel.id
    user_msg = message.content.strip()
    if not user_msg:
        return

    # --- !help ---
    if user_msg in ('!help', '!說明'):
        await message.channel.send(HELP_TEXT)
        return

    # --- !status ---
    if user_msg == '!status':
        s = sessions.get(channel_id)
        if not s:
            await message.channel.send(
                f"ℹ️ 本頻道目前沒有進行中的案例。\n"
                f"全局模型：`{CACHED_MODEL}`｜記憶體：{get_memory_mb():.0f}MB / 512MB"
            )
        else:
            await message.channel.send(
                f"📋 **【本頻道狀態】**\n"
                f"模型：`{s.model}`\n"
                f"開始時間：{s.started_at.strftime('%Y-%m-%d %H:%M')}（台灣時間）\n"
                f"已互動輪數：{s.turns} / {MAX_TURNS_HARD}\n"
                f"閒置：{s.idle_minutes():.0f} 分鐘（超過 {IDLE_TIMEOUT_MIN} 分鐘自動關閉）\n"
                f"記憶體：{get_memory_mb():.0f}MB / 512MB"
            )
        return

    # --- !reset ---
    if user_msg == '!reset':
        if sessions.pop(channel_id, None):
            channel_locks.pop(channel_id, None)
            await message.channel.send(
                "🔄 **【頻道已重置】** 本頻道的急救測驗已清除。請輸入 `!start` 開始新案例。"
            )
        else:
            await message.channel.send("ℹ️ 本頻道目前沒有進行中的測驗。可以輸入 `!start` 開始。")
        return

    # --- !start ---
    if user_msg == '!start':
        if channel_id in sessions:
            await message.channel.send(
                "⚠️ **【已有進行中的案例】** 本頻道目前已有急救測驗進行中！"
                "如欲放棄並開新局，請先輸入 `!reset`。"
            )
            return

        async with channel_locks[channel_id]:
            if channel_id in sessions:  # 雙重檢查，防連點
                return
            try:
                async with message.channel.typing():
                    session = await asyncio.to_thread(create_session)
                    response = await send_msg_with_retry(session.chat, build_start_prompt())
                    text = extract_text(response)

                if not text:
                    await message.channel.send(
                        "⚠️ **【案例生成失敗】** 回覆內容被安全機制擋下，請再輸入一次 `!start`。"
                    )
                    return

                sessions[channel_id] = session
                session.touch()
                await message.channel.send(
                    f"🚑 **【虛擬救護模擬系統啟動】** (模型：`{session.model}`)\n"
                    f"提示：輸入 `!help` 查看指令說明。\n"
                )
                await send_long(message.channel, text)
            except Exception as e:
                sessions.pop(channel_id, None)
                await reply_api_error(message.channel, e, stage='啟動')
        return

    # --- 一般互動 ---
    session = sessions.get(channel_id)
    if session is None:
        return

    async with channel_locks[channel_id]:
        session = sessions.get(channel_id)  # 取鎖後重新確認（可能已被 reset）
        if session is None:
            return
        try:
            async with message.channel.typing():
                response = await send_msg_with_retry(session.chat, user_msg)
                text = extract_text(response)

            if not text:
                await message.channel.send(
                    "⚠️ 這段回覆被安全機制擋下了，請換個方式描述你的處置，或輸入 `!reset` 重開。"
                )
                return

            session.touch()
            await send_long(message.channel, text)

            if session.turns >= MAX_TURNS_HARD:
                sessions.pop(channel_id, None)
                await message.channel.send(
                    f"🛑 **【案例強制結束】** 已達 {MAX_TURNS_HARD} 輪上限，"
                    "對話歷史過長會影響穩定性。請輸入 `!start` 開始新案例。"
                )
            elif session.turns == MAX_TURNS_WARN:
                await message.channel.send(
                    f"ℹ️ 本案例已進行 {MAX_TURNS_WARN} 輪，"
                    f"最多可到 {MAX_TURNS_HARD} 輪。建議儘快完成後送並請教官結案。"
                )
        except Exception as e:
            await reply_api_error(message.channel, e, stage='對話')


# ---------------------------------------------------------------------------
# 5. Keep-alive Web Server
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route('/')
def home():
    return (f'EMT Bot is active! sessions={len(sessions)} '
            f'model={CACHED_MODEL} mem={get_memory_mb():.0f}MB')


@app.route('/healthz')
def healthz():
    return 'ok', 200


def run_web():
    port = int(os.environ.get('PORT', 8080))
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=port)
    except ImportError:
        log.warning('未安裝 waitress，改用 Flask 內建 server（不建議用於正式環境）')
        app.run(host='0.0.0.0', port=port)


def keep_alive():
    Thread(target=run_web, daemon=True).start()


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    keep_alive()
    DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
    if not DISCORD_TOKEN:
        raise SystemExit('錯誤：找不到 DISCORD_TOKEN 環境變數。')
    # log_handler=None：沿用上面的 basicConfig，避免 discord.py 另掛 handler 導致重複輸出
    discord_client.run(DISCORD_TOKEN, log_handler=None)
