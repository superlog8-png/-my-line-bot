import os
from fastapi import FastAPI, Request
import requests
import json
import uvicorn

app = FastAPI()

# 建議將 Token 設為環境變數以增加安全性
LINE_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '6vB2cd5zqutMwNsNPwrxXlh5ZjspZPGZtimIti8eiXWWbQyOUwqGnb3svZMRiE3oXvNfMbi2+qWlnZFby4gvnf6tSUsBuPnuplNt5sBN6F/4U8ObZpt+dmzLECB/ZyNcMlSZ9QN7HT/P0UV8bGaEnwdB04t89/1O/w1cDnyilFU=')

def get_news_by_category(category):
    news_map = {
        "請給我財金重點": "📈 【財金重點】\n- SpaceX IPO 引發全球太空競賽熱潮\n- 聯準會維持利率不變，市場預期下半年降息",
        "請給我科技趨勢": "💻 【科技趨勢】\n- AI 監管新法規發布，生成式 AI 面臨轉型\n- 半導體供應鏈全球擴張計畫啟動",
        "請給我虛擬貨幣": "🪙 【虛擬貨幣】\n- 比特幣突破新高，現貨 ETF 流入量持續增長\n- 以太坊坎昆升級後，L2 手續費大幅下降",
        "請給我台灣新聞": "🇹🇼 【台灣新聞】\n- 夏季電價啟動，台電呼籲節約用電\n- 國內半導體人才培育計畫正式上線",
        "請給我國際新聞": "🌍 【國際新聞】\n- 全球氣候峰會達成新協議，加速能源轉型\n- 歐盟發布最新數據隱私保護條例",
        "請給我星座運勢": "♈ 【星座運勢】\n- 今日牡羊座財運極佳，適合投資\n- 雙子座靈感湧現，工作有大進展"
    }
    return news_map.get(category, "您好！我是您的 AI 助理，點擊下方選單即可獲取最新資訊。")

@app.get("/")
async def root():
    return {"message": "LINE Bot Server is running!"}

@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.json()
    events = body.get('events', [])
    
    for event in events:
        if event['type'] == 'message' and event['message']['type'] == 'text':
            user_text = event['message']['text']
            reply_token = event['replyToken']
            
            response_text = get_news_by_category(user_text)
            
            reply_url = "https://api.line.me/v2/bot/message/reply"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_TOKEN}"
            }
            payload = {
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": response_text}]
            }
            requests.post(reply_url, headers=headers, data=json.dumps(payload))
            
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
