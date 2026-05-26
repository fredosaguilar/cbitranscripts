import http.client
import urllib.parse
import json

from dotenv import load_dotenv
import os
load_dotenv()

# base url
BASE_URI= os.getenv("FRONTEND_BASE_URL")

# pushover.net API token for sending notifications
TOKEN = os.getenv("TOKEN")


# sed notification using pushover.net API
def send_push_notification(transcript_id: str, user_key: str, structured_data: dict):

    conn = http.client.HTTPSConnection("api.pushover.net", 443)
    final_url = BASE_URI + transcript_id

    print("Preparing to send notification for transcript ID:", transcript_id)
    print("User key for notification:", user_key)  
    print("token key for notification:", TOKEN)  
    print("Final URL for notification:", final_url) 

    message_payload = json.dumps(structured_data, ensure_ascii=True)

    params = urllib.parse.urlencode({
        "token": TOKEN,
        "user": user_key,
        "message": message_payload,
        "title": "Your transcript is ready!",
        "url": final_url,
        "url_title": "View Transcript"
    })


    headers = {
        "Content-type": "application/x-www-form-urlencoded"
    }

    conn.request("POST", "/1/messages.json", params, headers)

    response = conn.getresponse()
    print(response.status, response.reason)
    print(response.read().decode())
