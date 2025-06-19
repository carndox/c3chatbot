import os
import json
from datetime import datetime

import pyodbc
from facebook_scraper import get_posts
from openai import OpenAI


def get_db_conn():
    conn_str = (
        "Driver={ODBC Driver 17 for SQL Server};"
        f"Server={os.environ.get('DB_HOST')};"
        f"Database={os.environ.get('DB_NAME')};"
        f"UID={os.environ.get('DB_USER')};"
        f"PWD={os.environ.get('DB_PASS')}"
    )
    return pyodbc.connect(conn_str)


def summarize(text: str, client: OpenAI) -> str:
    if not text:
        return ""
    prompt = f"Summarize the following Facebook post in 3 sentences:\n{text}"
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.choices[0].message.content.strip()


def fetch_and_store(page: str, limit: int = 5):
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = OpenAI(api_key=openai_key)

    conn = get_db_conn()
    cursor = conn.cursor()

    count = 0
    for post in get_posts(page, pages=2, options={"posts_per_page": 5}):
        if count >= limit:
            break
        post_id = post.get("post_id")
        url = post.get("post_url")
        time_val = post.get("time")
        text = post.get("text", "")
        images = post.get("images")
        video = post.get("video")
        attachments = json.dumps({"images": images, "video": video}, default=str)
        summary = summarize(text, client)

        cursor.execute(
            """
            INSERT INTO FBPosts (post_id, post_url, post_time, text, summary, attachments)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            post_id, url, time_val, text, summary, attachments
        )
        conn.commit()
        count += 1
        print(f"Saved {url} ({time_val})")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    fetch_and_store("CEBECOIIIToledo")

