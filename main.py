import os
import discord
from google import genai
from google.genai import types
from flask import Flask
from threading import Thread

# 1. 初始化全新的 Google GenAI Client
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY)

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

# 記錄不同頻道的對話階段
channel_chats = {}

# 3. 設定 Discord 機器人
intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

@discord_client.event
async def on_ready():
    print(f'🤖 EMT AI 專業教練已上線：{discord_client.user}')

@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return

    channel_id = message.channel.id
    user_msg = message.content.strip()

    # 指令：開始新案例
    if user_msg == '!start':
        # 建立一個帶有 System Instruction 的新對話連線 (改用 gemini-2.0-flash)
        chat = client.chats.create(
            model='gemini-2.0-flash',
            config=types.GenerateContentConfig(
                system_instruction=EMT_SYSTEM_PROMPT,
                temperature=0.7
            )
        )
        channel_chats[channel_id] = chat
        
        async with message.channel.typing():
            try:
                response = chat.send_message("請隨機生成一個新的 EMT 模擬案例（可選創傷或內科），並提供派遣資訊，保持被動與破碎化。")
                await message.channel.send(f"🚑 **【虛擬救護模擬系統啟動】** 🚑\n{response.text}")
            except Exception as e:
                await message.channel.send(f"⚠️ 啟動錯誤：{e}")
        return

    # 指令：重置案例
    if user_msg == '!reset':
        if channel_id in channel_chats:
            del channel_chats[channel_id]
        await message.channel.send("🔄 模擬器已重置。請輸入 `!start` 開始新案例。")
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
            except Exception as e:
                await message.channel.send(f"⚠️ 發生錯誤：{e}")

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
