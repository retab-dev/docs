--- 

## Webhooks

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

- Create a webhook endpoint handler to receive event data POST requests.
- Test your webhook endpoint handler by marking a file as reviewed in the dashboard.
- Secure your webhook endpoint.


### Create your server with a webhook

Then, set up a route that will handle incoming webhook POST requests. You will need it to create an automation. Below are examples of webhook endpoints:

<CodeGroup>
```python Python (FastAPI)
import hashlib
import hmac
import json
import os

from fastapi import FastAPI, HTTPException, Request


app = FastAPI()


def verify_retab_signature(payload: bytes, signature: str | None, secret: str) -> None:
    """Constant-time HMAC-SHA256 verification against the X-Retab-Signature header."""
    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Retab-Signature header")
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


@app.post("/webhook")
async def webhook_handler(request: Request):
    payload = await request.body()
    verify_retab_signature(
        payload,
        request.headers.get("X-Retab-Signature"),
        os.environ["WEBHOOKS_SECRET"],
    )

    webhook_request = json.loads(payload.decode("utf-8"))
    invoice_object = json.loads(
        webhook_request["completion"]["choices"][0]["message"]["content"] or "{}"
    )
    print("📬 Webhook received:", invoice_object)
    return {"status": "success", "data": invoice_object}
```

```typescript TypeScript (Express)
import crypto from 'crypto';
import express from 'express';

const app = express();

app.use(express.raw({ type: 'application/json' }));

function verifyRetabSignature(payload, signature, secret) {
    if (!signature) {
        throw new Error('Missing X-Retab-Signature header');
    }
    const expected = crypto.createHmac('sha256', secret).update(payload).digest('hex');
    const a = Buffer.from(expected, 'hex');
    const b = Buffer.from(String(signature), 'hex');
    if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
        throw new Error('Invalid webhook signature');
    }
}

app.post('/webhook', async (req, res) => {
    const payload = req.body;
    try {
        verifyRetabSignature(
            payload,
            req.headers['x-retab-signature'],
            process.env.WEBHOOKS_SECRET,
        );
    } catch (error) {
        return res.status(401).send(`Signature verification failed: ${error.message}`);
    }

    const webhookRequest = JSON.parse(payload.toString('utf-8'));
    const invoiceObject = JSON.parse(
        webhookRequest.completion.choices[0].message.content || '{}',
    );
    console.log('📬 Webhook received:', invoiceObject);
    return res.status(200).json({ status: 'success', data: invoiceObject });
});
```

```ruby Ruby (Sinatra)
require 'sinatra'
require 'json'
require 'openssl'

# Constant-time HMAC-SHA256 verification against the X-Retab-Signature header.
def verify_retab_signature(payload, signature, secret)
  halt 400, 'Missing X-Retab-Signature header' if signature.nil? || signature.empty?
  expected = OpenSSL::HMAC.hexdigest('sha256', secret, payload)
  halt 401, 'Invalid webhook signature' unless Rack::Utils.secure_compare(expected, signature)
end

post '/webhook' do
  request.body.rewind
  payload = request.body.read
  verify_retab_signature(payload, request.env['HTTP_X_RETAB_SIGNATURE'], ENV['WEBHOOKS_SECRET'])

  webhook_request = JSON.parse(payload)
  invoice_object = JSON.parse(
    webhook_request.dig('completion', 'choices', 0, 'message', 'content') || '{}'
  )
  puts "📬 Webhook received: #{invoice_object}"

  content_type :json
  { status: 'success', data: invoice_object }.to_json
end

# Run with: ruby your_module_name.rb -p 8000
```




```go Go
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
)

func webhook(w http.ResponseWriter, r *http.Request) {
	payload, _ := io.ReadAll(r.Body)
	sig := r.Header.Get("X-Retab-Signature")
	mac := hmac.New(sha256.New, []byte(os.Getenv("WEBHOOKS_SECRET")))
	mac.Write(payload)
	if !hmac.Equal([]byte(hex.EncodeToString(mac.Sum(nil))), []byte(sig)) {
		http.Error(w, "invalid signature", http.StatusUnauthorized)
		return
	}

	var event map[string]any
	_ = json.Unmarshal(payload, &event)
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "success"})
}
```

```rust Rust
use axum::{body::Bytes, http::HeaderMap, response::IntoResponse};
use hmac::{Hmac, Mac};
use sha2::Sha256;

async fn webhook(headers: HeaderMap, payload: Bytes) -> impl IntoResponse {
    let signature = headers.get("X-Retab-Signature").and_then(|v| v.to_str().ok()).unwrap_or("");
    let secret = std::env::var("WEBHOOKS_SECRET").expect("WEBHOOKS_SECRET");
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("hmac key");
    mac.update(&payload);
    let expected = hex::encode(mac.finalize().into_bytes());
    if expected != signature {
        return (axum::http::StatusCode::UNAUTHORIZED, "invalid signature").into_response();
    }
    (axum::http::StatusCode::OK, "success").into_response()
}
```

```csharp C#
using System.Security.Cryptography;
using System.Text;

app.MapPost("/webhook", async (HttpRequest request) =>
{
    using var reader = new MemoryStream();
    await request.Body.CopyToAsync(reader);
    var payload = reader.ToArray();
    var signature = request.Headers["X-Retab-Signature"].ToString();
    var secret = Environment.GetEnvironmentVariable("WEBHOOKS_SECRET")!;

    using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(secret));
    var expected = Convert.ToHexString(hmac.ComputeHash(payload)).ToLowerInvariant();
    if (!CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(expected), Encoding.UTF8.GetBytes(signature)))
        return Results.Unauthorized();

    return Results.Json(new { status = "success" });
});
```

```php PHP
<?php
$payload = file_get_contents('php://input');
$signature = $_SERVER['HTTP_X_RETAB_SIGNATURE'] ?? '';
$expected = hash_hmac('sha256', $payload, getenv('WEBHOOKS_SECRET'));

if (!hash_equals($expected, $signature)) {
    http_response_code(401);
    echo 'invalid signature';
    exit;
}

header('Content-Type: application/json');
echo json_encode(['status' => 'success']);
```

```java Java
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public final class Example {
  static void verifyRetabSignature(byte[] payload, String signature, String secret) throws Exception {
    if (signature == null || signature.isBlank()) {
      throw new IllegalArgumentException("Missing X-Retab-Signature header");
    }
    Mac mac = Mac.getInstance("HmacSHA256");
    mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
    String expected = bytesToHex(mac.doFinal(payload));
    if (!MessageDigest.isEqual(expected.getBytes(StandardCharsets.UTF_8), signature.getBytes(StandardCharsets.UTF_8))) {
      throw new IllegalArgumentException("Invalid webhook signature");
    }
  }

  static String bytesToHex(byte[] bytes) {
    StringBuilder out = new StringBuilder(bytes.length * 2);
    for (byte b : bytes) {
      out.append(String.format("%02x", b));
    }
    return out.toString();
  }
}
```

</CodeGroup>






### Exposing local server to the internet using ngrok

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

That's it! You can start processing documents at scale. 

---
