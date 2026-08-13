import os
import discord
from google import genai
from google.genai import types
from flask import Flask
from threading import Thread

# 初始化
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY)

EMT_SYSTEM_PROMPT = """
你是一個嚴格的台灣「緊急醫療救護 (EMT)」模擬訓練教官。
請完全依照台灣衛福部法規、消防署 EMT-1/EMT-2 教科書與標準急救指引來進行評估。
"""

channel_chats = {}

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

@discord_client.event
async def on_ready():
    print(f'🤖 診斷機器人已上線：{discord_client.user}')

@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return

    channel_id = message.channel.id
    user_msg = message.content.strip()

    if user_msg == '!start':
        async with message.channel.typing():
            try:
                # 測試建立對話與呼叫模型 (使用目前官方清單中最穩定的 gemini-2.5-flash)
                chat = client.chats.create(
                    model='gemini-2.5-flash',
                    config=types.GenerateContentConfig(
                        system_instruction=EMT_SYSTEM_PROMPT,
                        temperature=0.7
                    )
                )
                channel_chats[channel_id] = chat
                
                response = chat.send_message("請隨機生成一個新的 EMT 模擬案例，並提供派遣資訊，保持被動。")
                await message.channel.send(f"🚑 **【系統啟動成功】**\n{response.text}")
                
            except Exception as e:
                # 如果這裡卡住，Discord 會直接把「真實的錯誤代碼」傳給你看！
                await message.channel.send(f"❌ **【程式在這裡卡住/發生錯誤】**:\n```{str(e)}```")
        return

    if user_msg == '!reset':
        if channel_id in channel_chats:
            del channel_chats[channel_id]
        await message.channel.send("🔄 已重置。")
        return

    if channel_id in channel_chats:
        chat = channel_chats[channel_id]
        async with message.channel.typing():
            try:
                response = chat.send_message(user_msg)
                await message.channel.send(response.text)
            except Exception as e:
                await message.channel.send(f"❌ **【對話時發生錯誤】**:\n```{str(e)}```")

# 保持 Render 不休眠
app = Flask('')
@app.route('/')
def home():
    return "Diagnostic Bot is active!"

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
        print("錯誤：找不到 DISCORD_TOKEN。")
