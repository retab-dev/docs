# Cube AI Webhook Integration

When an automation triggers, Cube AI sends a **POST request** to your webhook endpoint.
Your backend should be able to receive this payload and process the extracted data.

---

## Request Details

- **Method**: `POST`
- **Content-Type**: `application/json`
- **Headers**:
  - `Cube AI-Signature`: HMAC-SHA256 signature (can be used to verify authenticity of the payload)

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
    "model": "gpt-5-nano",
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

- The **structured extraction result** is always inside:

  ```json
  completion.choices[0].message.content
  ```

  > This field is a **JSON string** — you must parse it (in Python: `json.loads(...)`, in Node: `JSON.parse(...)`).

- The **uploaded file** (if present) is in:

  ```json
  file_payload.url
  ```

  encoded as a base64 Data URL.

---

## Minimal Parsing Example

### 🟦 Node.js (Express)

```typescript
app.post("/webhook", (req, res) => {
  const body = req.body;
  const content = body?.completion?.choices?.[0]?.message?.content || "{}";
  const parsed = JSON.parse(content);

  console.log("📬 Webhook received:", parsed, {
    user: body?.user,
    file: body?.file_payload?.filename,
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

### 🐹 Go (net/http)

```go
package main

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
)

type webhookBody struct {
	Completion struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	} `json:"completion"`
	User        string `json:"user"`
	FilePayload struct {
		Filename string `json:"filename"`
	} `json:"file_payload"`
}

func handler(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	var body webhookBody
	if err := json.Unmarshal(raw, &body); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	var parsed map[string]any
	content := "{}"
	if len(body.Completion.Choices) > 0 {
		content = body.Completion.Choices[0].Message.Content
	}
	_ = json.Unmarshal([]byte(content), &parsed)

	log.Printf("Webhook received: %+v user=%s file=%s",
		parsed, body.User, body.FilePayload.Filename)

	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"status":"success"}`))
}

func main() {
	http.HandleFunc("/webhook", handler)
	log.Fatal(http.ListenAndServe(":8000", nil))
}
```

---

## Next Steps

1. Expose an endpoint in your backend that accepts POST requests.
2. Parse `completion.choices[0].message.content` into JSON.
3. Store the result in your database or trigger your business logic.

---

✅ With this, your backend can start receiving and integrating data from Cube AI.

---
