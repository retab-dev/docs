# Cube AI Webhook Integration

When an automation triggers, Cube AI sends a **POST request** to your webhook endpoint.
Your backend should be able to receive this payload and process the extracted data.

---

## Request Details

* **Method**: `POST`
* **Content-Type**: `application/json`
* **Headers**:

  * `Cube AI-Signature`: HMAC-SHA256 signature (can be used to verify authenticity of the payload)

---

## WebhookRequest Object Reference

| Field            | Type                   | Description                                                                |
| ---------------- | ---------------------- | -------------------------------------------------------------------------- |
| **completion**   | `ParsedChatCompletion` | The parsed chat completion object, containing the extracted data.          |
| **user**         | `EmailStr`             | The user email address.                                                    |
| **file_payload** | `MIMEData`             | The file payload object, containing the file name, url and other metadata. |
| ├── **filename** | `str`                  | The filename of the file.                                                  |
| └── **url**      | `str`                  | The file contents in base64 format (Data URL).                             |
| **metadata**     | `dict[str, Any]`       | Some additional metadata.                                                  |

---

## Example Payload

```json
{
  "completion": {
    "id": "id",
    "model": "gpt-4.1-nano",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "{\"name\":\"Team Meeting\",\"date\":\"2023-12-31\"}"
        }
      }
    ],
    "created": 0,
    "object": "chat.completion",
    "likelihoods": {}
  },
  "user": "user@example.com",
  "file_payload": {
    "filename": "example.pdf",
    "url": "data:application/pdf;base64,AAAA..."
  },
  "metadata": {}
}
```

---

## Where to Find the Data You Care About

* The **structured extraction result** is always inside:

  ```json
  completion.choices[0].message.content
  ```

  > This field is a **JSON string** — you must parse it (in Python: `json.loads(...)`, in Node: `JSON.parse(...)`).

* The **uploaded file** (if present) is in:

  ```json
  file_payload.url
  ```

  encoded as a base64 Data URL.

---

## Minimal Parsing Example

### 🟦 Node.js (Express)

```js
app.post("/webhook", (req, res) => {
  const body = req.body;
  const content = body?.completion?.choices?.[0]?.message?.content || "{}";
  const parsed = JSON.parse(content);

  console.log("📬 Webhook received:", parsed, {
    user: body?.user,
    file: body?.file_payload?.filename
  });

  res.json({ status: "success" });
});
```

---

### 🐍 Python (FastAPI)

```python
from fastapi import FastAPI, Request
import json

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    content = body.get("completion", {}) \
                  .get("choices", [{}])[0] \
                  .get("message", {}) \
                  .get("content", "{}")
    parsed = json.loads(content)

    print("📬 Webhook received:", parsed, {
        "user": body.get("user"),
        "file": body.get("file_payload", {}).get("filename")
    })

    return {"status": "success"}
```

---

## Next Steps

1. Expose an endpoint in your backend that accepts POST requests.
2. Parse `completion.choices[0].message.content` into JSON.
3. Store the result in your database or trigger your business logic.

---

✅ With this, your backend can start receiving and integrating data from Cube AI.

---
