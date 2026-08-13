import os
import discord
from google import genai
from google.genai import types
from google.genai.errors import APIError
from flask import Flask
from threading import Thread

# 1. 初始化 Google GenAI Client
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY)

# 全局快取使用的模型名稱，避免每次 !start 都浪費 API 額度
CACHED_MODEL = 'gemini-1.5-flash'

# 僅在機器人啟動時測試一次可用模型
def init_working_model():
    global CACHED_MODEL
    candidates = [
        'gemini-1.5-flash',
        'gemini-2.0-flash',
        'gemini-flash-latest',
        'gemini-3.5-flash-lite'
    ]
    for model_name in candidates:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents="ping"
            )
            if response:
                CACHED_MODEL = model_name
                print(f"✅ 開機測試成功！全局採用模型：{CACHED_MODEL}")
                return CACHED_MODEL
        except Exception as e:
            print(f"測試模型 {model_name} 額度受限或不可用，嘗試下一個...")
            
    return CACHED_MODEL

# 2. 核心教練設定
EMT_SYSTEM_PROMPT = """
你是一個嚴格的台灣「緊急醫療救護 (EMT)」模擬訓練教官。
請完全依照台灣衛福部法規、消防署 EMT-1/EMT-2 教科書與標準急救指引來進行評估。

【核心規則與互動機制】
1. 嚴格被動模式 (Passive Mode)：
   - 絕不主動提供未被詢問的生命徵象或病患內部狀況。
   - 保持對話的「破碎感」，一次只推進一點點進度，等待學員下達明確指令。
2. 考官與病患合一：
   - 當學員執行動作時（如：檢查意識、量血壓、哈姆立克、CPR），回報客觀事實。
   - 絕不主動「補丸」或代替學員做決定（例如：學員沒喊準備AED，絕對不能自己出現AED）。
3. 違規糾正：
   - 若學員做出越級處置（如 EMT-1 嘗試給藥或打針），必須以教官身分嚴厲制止並扣分。
4. 結案報告 (AAR)：
   - 當學員完成任務、送醫或病患死亡時，提供詳細的 0-100 分考核報告與條列式檢檢。

現在，請等待學員輸入「!start」來開始一個隨機的模擬案例。
"""

# 以 channel_id 為 Key 記錄不同頻道的對話階段，實現多頻道獨立運作
channel_chats = {}

# 3. 設定 Discord 機器人
intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

@discord_client.event
async def on_ready():
    print(f'🤖 EMT AI 專業教練已上線：{discord_client.user}')
    init_working_model()

@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return

    channel_id = message.channel.id
    user_msg = message.content.strip()

    # 指令：開始新案例
    if user_msg == '!start':
        # 🛡️ 防呆機制：如果本頻道已經有案例在進行，提示玩家先 reset
        if channel_id in channel_chats:
            await message.channel.send("⚠️ **【已有進行中的案例】** 本頻道目前已有急救測驗進行中！如欲放棄並開新局，請先輸入 `!reset`。")
            return

        async with message.channel.typing():
            try:
                # 建立專屬於此頻道 (channel_id) 的對話 Session
                chat = client.chats.create(
                    model=CACHED_MODEL,
                    config=types.GenerateContentConfig(
                        system_instruction=EMT_SYSTEM_PROMPT,
                        temperature=0.7
                    )
                )
                channel_chats[channel_id] = chat
                
                response = chat.send_message("請隨機生成一個新的 EMT 模擬案例（可選創傷或內科），並提供派遣資訊，保持被動與破碎化。")
                await message.channel.send(f"🚑 **【虛擬救護模擬系統啟動】** (採用模型: `{CACHED_MODEL}`)\n{response.text}")
            except APIError as e:
                if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e):
                    await message.channel.send("⚠️ **【觸發流量冷卻機制】** Google AI 免費版每分鐘請求次數上限，請等待約 **1 分鐘** 後再輸入 `!start`。")
                else:
                    await message.channel.send(f"❌ **【啟動發生錯誤】**:\n```{str(e)}```")
            except Exception as e:
                await message.channel.send(f"❌ **【啟動發生錯誤】**:\n```{str(e)}```")
        return

    # 指令：重置案例
    if user_msg == '!reset':
        if channel_id in channel_chats:
            del channel_chats[channel_id]
            await message.channel.send("🔄 **【頻道已重置】** 本頻道的急救測驗已清除。請輸入 `!start` 開始新案例。")
        else:
            await message.channel.send("ℹ️ 本頻道目前沒有進行中的測驗。可以輸入 `!start` 開始測驗。")
        return

    # 一般互動對話
    if channel_id in channel_chats:
        chat = channel_chats[channel_id]
        async with message.channel.typing():
            try:
                response = chat.send_message(user_msg)
                bot_reply = response.text
                
                if len(bot_reply) > 1900:
                    for i in range(0, len(bot_reply), 1900):
                        await message.channel.send(bot_reply[i:i+1900])
                else:
                    await message.channel.send(bot_reply)
            except APIError as e:
                if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e):
                    await message.channel.send("⚠️ **【對話頻率過快】** 已達到每分鐘請求上限，請稍微休息 **1 分鐘** 後繼續下達指令。")
                else:
                    await message.channel.send(f"❌ **【對話發生錯誤】**:\n```{str(e)}```")
            except Exception as e:
                await message.channel.send(f"❌ **【對話發生錯誤】**:\n```{str(e)}```")

# 4. 保持 Render 伺服器不休眠的網頁
app = Flask('')
@app.route('/')
def home():
    return "EMT Bot is active!"

def run_web():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

if __name__ == '__main__':
    keep_alive()
    DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
    if DISCORD_TOKEN:
        discord_client.run(DISCORD_TOKEN)
    else:
        print("錯誤：找不到 DISCORD_TOKEN 環境變數。")
