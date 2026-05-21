```mermaid
graph LR
    A["Trigger"] --> B["Processor"] --> C["Destination"]

    A1["Email"] -.-> A
    A2["Link"] -.-> A
    A3["Outlook Plugin"] -.-> A
    A4["API Endpoint"] -.-> A

    C -.-> C1["Webhook"]
    C -.-> C2["Your Database"]
    C -.-> C3["Google Sheets"]
```

An **automation** is a way to automatically send data to your processor when triggered by events like receiving an email, clicking a link, or using the Outlook plugin. Once triggered, the processor extracts structured data from your documents and sends the results to your chosen destination.

## Introduction

Retab uses HTTPS to send webhook events to your app as a JSON payload representing a `WebhookRequest` object.
You will need a server with a webhook endpoint that will receive the `webhook_request` payload, allowing you to process them as you want after that.

<ResponseField name="webhook_request" type="WebhookRequest Object">
  <Expandable title="properties">
    <ResponseField name="completion" type="ParsedChatCompletion">
      The parsed chat completion object, containing the extracted data.
    </ResponseField>

    <ResponseField name="user" type="EmailStr">
      The user email address.
    </ResponseField>


    <ResponseField name="file_payload" type="MIMEData">
      The file payload object, containing the file name, url and other metadata.
      <Expandable title="properties">
        <ResponseField name="filename" type="str">
          The filename of the file.
        </ResponseField>
        <ResponseField name="url" type="str">
          The URL of the file in base64 format.
        </ResponseField>
      </Expandable>
    </ResponseField>


    <ResponseField name="metadata" type="dict[str, Any]">
      Some additional metadata.
    </ResponseField>

  </Expandable>
</ResponseField>

To start receiving webhook events in your app:

- Create a new processor with your extraction configuration.
- Create a webhook endpoint handler to receive event data POST requests.
- Create a new automation sending data to your webhook endpoint.
- Test your webhook endpoint handler locally using the Retab SDK.
- Secure your webhook endpoint.

## Create your processor

Start by creating a **processor** with your extraction configuration.

<CodeGroup>
```python Python
import os
import requests

api_key = os.environ["RETAB_API_KEY"]
schema = {
"type": "object",
"properties": {
"invoice_number": {"type": "string"},
},
}

response = requests.post(
"https://api.retab.com/v1/processors",
headers={"Api-Key": api_key},
json={
"name": "Invoice Processor",
"model": "retab-small",
"json_schema": schema,
},
)
response.raise_for_status()
processor = response.json()

````

```go Go
// The Go SDK does not yet model the processors API. Call /v1/processors directly.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
)

func main() {
	schema := map[string]any{
		"type": "object",
		"properties": map[string]any{
			"invoice_number": map[string]any{"type": "string"},
		},
	}
	body, err := json.Marshal(map[string]any{
		"name":        "Invoice Processor",
		"model":       "retab-small",
		"json_schema": schema,
	})
	if err != nil {
		log.Fatal(err)
	}

	req, err := http.NewRequest(
		http.MethodPost,
		"https://api.retab.com/v1/processors",
		bytes.NewReader(body),
	)
	if err != nil {
		log.Fatal(err)
	}
	req.Header.Set("Api-Key", os.Getenv("RETAB_API_KEY"))
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Fatal(err)
	}
	defer resp.Body.Close()

	payload, _ := io.ReadAll(resp.Body)
	fmt.Println(string(payload))
}
````

</CodeGroup>

## Create your FastAPI server with a webhook

Then, set up a FastAPI route that will handle incoming webhook POST requests. You will need it to create an automation. Below is an example of a simple FastAPI application with a webhook endpoint:

<CodeGroup>
```python main.py
import json
from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
webhook_request = await request.json()
invoice_object = json.loads(
webhook_request["completion"]["choices"][0]["message"]["content"] or "{}"
)
print("📬 Webhook received:", invoice_object)
return {"status": "success", "data": invoice_object}

# To run the FastAPI app locally, use the command:

# uvicorn your_module_name:app --reload

if **name** == "**main**":
import uvicorn
uvicorn.run(app, host="0.0.0.0", port=8000)

````

You can test the webhook endpoint locally with a tool like curl or Postman. For example, using curl:

```bash testing locally
curl -X POST http://localhost:8000/webhook \
     -H "Content-Type: application/json" \
     -d '{"completion":{"id":"id","choices":[{"index":0,"message":{"content":"{\"name\" : \"Team Meeting!\", \"date\" : \"2023-12-31\" }","role":"assistant"}}],"created":0,"model":"gpt-5-nano","object":"chat.completion","likelihoods":{}},"file_payload":{"filename":"example.pdf","url":"data:application/pdf;base64,the_content_of_the_pdf_file"}}'
````

</CodeGroup>

## Secure your webhook endpoint

When you set up a webhook, you provide an **HTTP endpoint** on your server for Retab to send data to. If this endpoint is not secured (i.e., it accepts unauthenticated `POST` requests from anywhere), it essentially becomes a public door into your system. **Any actor** could attempt to call this URL and send fake data. This is inherently dangerous: a malicious party might send **forged webhook requests** that masquerade as Retab, but contain bogus or harmful data.

To secure webhook deliveries, Retab employs a **signature verification** mechanism using an HMAC-like scheme. Retab and your application share a **webhook secret** (a random string known only to Retab and you). This secret is available in your [Retab dashboard](https://www.retab.com/dashboard/settings) (Labeled as `WEBHOOKS_SECRET`). Retab uses this secret to include a special signature header with every webhook request. When your endpoint receives the webhook, your code should perform the same HMAC-SHA256 computation on the request body using the shared secret, then compare your computed signature to the value in the `X-Retab-Signature` header. If the signatures **match**, the request truly came from Retab and the payload was not altered in transit.

<Warning>Make sure to set your `WEBHOOKS_SECRET` environment variable with the secret from your [Retab dashboard](https://www.retab.com/dashboard/settings).</Warning>

Here's how to implement signature verification in your FastAPI webhook:

<CodeGroup>
```python Python {13-26}
import os
import json
from fastapi import FastAPI, Request, Response, HTTPException
from retab import Retab

app = FastAPI()

@app.post("/webhook")
async def webhook_handler(request: Request):
payload = await request.body()

    # Signature verification
    try:
        signature_header = request.headers.get("X-Retab-Signature")
        if not signature_header:
            raise HTTPException(status_code=400, detail="Missing X-Retab-Signature header")
        # Verify the signature using Retab SDK
        Retab.verify_event(
            event_body=payload,
            event_signature=signature_header,
            secret=os.getenv("WEBHOOKS_SECRET"),  # Get secret from environment variable
        )
    except Exception as e:
        return Response(status_code=400, content=f"Webhook error: {str(e)}")

    webhook_request = json.loads(payload.decode('utf-8'))

    invoice_object = json.loads(
        webhook_request["completion"]["choices"][0]["message"]["content"] or "{}"
    )
    print("📬 Webhook received:", invoice_object)
    return {"status": "success", "data": invoice_object}

````

```go Go
// retab.VerifyEvent validates the X-Retab-Signature HMAC and decodes the
// payload into a typed struct in one step.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"

	retab "github.com/retab-dev/retab/clients/go"
)

type WebhookRequest struct {
	Completion  json.RawMessage   `json:"completion"`
	User        string            `json:"user,omitempty"`
	FilePayload json.RawMessage   `json:"file_payload"`
	Metadata    map[string]any    `json:"metadata,omitempty"`
}

func handler(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	signature := r.Header.Get("X-Retab-Signature")
	if signature == "" {
		http.Error(w, "Missing X-Retab-Signature header", http.StatusBadRequest)
		return
	}
	event, err := retab.VerifyEvent[WebhookRequest](
		body,
		signature,
		os.Getenv("WEBHOOKS_SECRET"),
	)
	if err != nil {
		http.Error(w, fmt.Sprintf("Webhook error: %v", err), http.StatusBadRequest)
		return
	}
	log.Printf("Webhook received: %+v", event)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"status":"success"}`))
}

func main() {
	http.HandleFunc("/webhook", handler)
	log.Fatal(http.ListenAndServe(":8000", nil))
}
````

</CodeGroup>

## Exposing local server to the internet using ngrok

<Warning>To continue, you need to deploy your FastAPI app to a server to make your webhook endpoint publicly accessible. We recommend using [Replit](https://replit.com/) to get started quickly if you don't have a server yet. An alternative is to use [ngrok](https://ngrok.com/) to expose your local server to the internet.</Warning>

We have a very simple Dockerfile that fastapi+ngrok to get you started.
Check out the [webhook_server](https://github.com/retab-dev/retab/tree/main/examples/automations/webhook_server) folder for more details.

<Tip>You will need a ngrok auth token to run the docker container. You can get one [here](https://dashboard.ngrok.com/get-started)</Tip>

Start fastapi+ngrok server:

<CodeGroup>
```bash startup
git clone https://github.com/retab-dev/retab.git
cd retab/examples/webhook_server
docker build -t webhook_server .
docker run --rm -it -e NGROK_AUTH_TOKEN=[your_ngrok_auth_token] webhook_server
```

```logs {4} server logs
INFO:     Started server process [1]
INFO:     Waiting for application startup.
🌍 Ngrok tunnel established!
📬 Webhook URL: https://some-random-ngrok-url.ngrok-free.app/webhook
📬 Simple curl for testing: curl -X POST https://some-random-ngrok-url.ngrok-free.app/webhook -H "Content-Type: application/json" -d '{"completion":{"id":"id","choices":[{"index":0,"message":{"content":"{\"message\" : \"Hello, World!\"}","role":"assistant"}}],"created":0,"model":"gpt-5-nano","object":"chat.completion","likelihoods":{}},"file_payload":{"filename":"example.pdf","url":"data:application/pdf;base64,the_content_of_the_pdf_file"}}'
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

</CodeGroup>

Take note of the `webhook URL`, you will need it on the next steps.

## Create an automation

Now, you can create an automation that will use your processor to extract data from emails.

Create the mailbox automation from the dashboard and attach it to the processor
you created above. Use your deployed webhook URL as the automation destination.

At any email sent to `invoices@mailbox.retab.com`, the automation will use your processor configuration to extract data and send a POST request to your FastAPI webhook endpoint.

You can see the processor and automation you just created on your [dashboard](https://www.retab.com/dashboard/processors)!

### Test your automation

Finally, you can test the processor and automation rapidly with the test functions of the SDK:

<CodeGroup>
```python Python
import os
import requests

api_key = os.environ["RETAB_API_KEY"]
automation_id = "auto_abc"

# If you just want to send a test request to your webhook

webhook_log = requests.post(
f"https://api.retab.com/v1/processors/automations/tests/webhook/{automation_id}",
headers={"Api-Key": api_key},
)
webhook_log.raise_for_status()

# If you want to test the file processing logic:

with open("your_invoice_email.eml", "rb") as document:
upload_log = requests.post(
f"https://api.retab.com/v1/processors/automations/tests/upload/{automation_id}",
headers={"Api-Key": api_key},
files={"file": ("your_invoice_email.eml", document, "message/rfc822")},
)
upload_log.raise_for_status()

````

```go Go
// The Go SDK does not yet model the automations test API. Call the test
// endpoints directly via /v1/processors/automations/tests/...
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"

	retab "github.com/retab-dev/retab/clients/go"
)

func post(url string, body any) ([]byte, error) {
	var reader *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(raw)
	} else {
		reader = bytes.NewReader([]byte("{}"))
	}
	req, err := http.NewRequest(http.MethodPost, url, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Api-Key", os.Getenv("RETAB_API_KEY"))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

func main() {
	// Send a test request to your webhook
	if out, err := post(
		"https://api.retab.com/v1/processors/automations/tests/webhook/auto_abc",
		map[string]any{},
	); err != nil {
		log.Fatal(err)
	} else {
		fmt.Println(string(out))
	}

	// Test the file processing logic
	mime, err := retab.InferMIMEData("your_invoice_email.eml")
	if err != nil {
		log.Fatal(err)
	}
	if out, err := post(
		"https://api.retab.com/v1/processors/automations/tests/upload/auto_abc",
		map[string]any{"document": mime},
	); err != nil {
		log.Fatal(err)
	} else {
		fmt.Println(string(out))
	}

}
````

</CodeGroup>

You can also test your automation directly from the [dashboard](https://www.retab.com/dashboard/processors).

---

That's it! You can start processing documents at scale.
