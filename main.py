import os
import discord
import google.generativeai as genai
from flask import Flask
from threading import Thread

# 1. 設定 Google Gemini API
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
genai.configure(api_key=GEMINI_API_KEY)

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

# 使用目前最穩定的 Gemini 2.0 Flash 模型
model = genai.GenerativeModel('gemini-2.0-flash', system_instruction=EMT_SYSTEM_PROMPT)

# 記錄不同頻道的對話紀錄
channel_sessions = {}

# 3. 設定 Discord 機器人
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'🤖 EMT AI 教練已成功上線：{client.user}')

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    channel_id = message.channel.id
    user_msg = message.content.strip()

    # 指令：開始新案例
    if user_msg == '!start':
        chat = model.start_chat(history=[])
        channel_sessions[channel_id] = chat
        
        async with message.channel.typing():
            try:
                response = await chat.send_message_async("請隨機生成一個新的 EMT 模擬案例（可選創傷或內科），並提供派遣資訊，保持被動與破碎化。")
                await message.channel.send(f"🚑 **【虛擬救護模擬系統啟動】** 🚑\n{response.text}")
            except Exception as e:
                # 兼容不同版本 SDK 的備用寫法
                response = chat.send_message("請隨機生成一個新的 EMT 模擬案例（可選創傷或內科），並提供派遣資訊，保持被動與破碎化。")
                await message.channel.send(f"🚑 **【虛擬救護模擬系統啟動】** 🚑\n{response.text}")
        return

    # 指令：重置案例
    if user_msg == '!reset':
        if channel_id in channel_sessions:
            del channel_sessions[channel_id]
        await message.channel.send("🔄 模擬器已重置。請輸入 `!start` 開始新案例。")
        return

    # 一般互動對話
    if channel_id in channel_sessions:
        chat = channel_sessions[channel_id]
        async with message.channel.typing():
            try:
                try:
                    response = await chat.send_message_async(user_msg)
                except:
                    response = chat.send_message(user_msg)
                    
                bot_reply = response.text
                
                if len(bot_reply) > 1900:
                    for i in range(0, len(bot_reply), 1900):
                        await message.channel.send(bot_reply[i:i+1900])
                else:
                    await message.channel.send(bot_reply)
            except Exception as e:
                await message.channel.send(f"⚠️ 發生錯誤：{e}")

# 4. 保持 Render 雲端伺服器不休眠的網頁
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
        client.run(DISCORD_TOKEN)
    else:
        print("錯誤：找不到 DISCORD_TOKEN 環境變數。")
