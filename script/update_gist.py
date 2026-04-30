import os, json, requests

GIST_ID = os.getenv("GIST_ID")
TOKEN = os.getenv("GITHUB_TOKEN")

HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github+json"
}

def push():
    with open("state.json") as f:
        state = f.read()

    with open("today.csv") as f:
        csv_data = f.read()

    payload = {
        "files": {
            "state.json": {"content": state},
            "today.csv": {"content": csv_data}
        }
    }

    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers=HEADERS,
        json=payload
    )

if __name__ == "__main__":
    push()